#!/usr/bin/env python3
"""
usf_pyemu - эмулятор Nintendo 64 для USF/miniusf на чистом Python (без компиляторов).

Замена нативному usf_tracer/usftrace (lazyusf2 + патч) для usf2xm-midi-wav.py:
    python3 usf_pyemu.py in.miniusf out_prefix seconds [out.raw]
пишет те же out_prefix.trace / out_prefix.pcm (и out.raw - стерео 16 бит 44.1 кГц).

Что внутри (порт lazyusf2 / mupen64plus pure interpreter, бит в бит по логике):
  * загрузка PSF-контейнера (+ _lib), секций ROM и savestate Project64;
  * интерпретатор MIPS R4300i: целочисленные и 64-битные команды, COP0 (с TLB и исключениями),
    COP1 (FPU), отложенные переходы, учёт Count как в mupen (по переходам), пропуск холостых циклов;
  * система: RDRAM, DMEM/IMEM RSP, регистры MI/VI/AI/PI/SI/SP/DP/RI, очередь событий и прерывания;
  * RSP: HLE аудио-микрокодов как в lazyusf2 при enable_hle_audio=1: ABI1 ("audio ABI", самый частый),
    ABI2 ("nead": Zelda, Mario Kart, Star Fox, F-Zero X, ...), ABI3 ("naudio": Banjo, DK64, Perfect Dark,
    Conker, ...; MP3-команды пропускаются) и MusyX v1/v2 (007 TWINE, Rogue Squadron, Indiana Jones, ...).
    Порты ABI2/ABI3 сверены с C-реализацией lazyusf2 на случайных alist-программах; MusyX сверен
    по трассе. Графические и неизвестные задачи только завершаются (как в lazyusf2).

Скорость: порядка 1 млн инструкций N64 в секунду (нативный lazyusf2 - сотни миллионов), на практике
в несколько раз медленнее реального времени.
"""
import array
import math
import os
import struct
import sys
import zlib

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF

# ═══════════════════════════════════════════════════════════════
# PSF / USF
# ═══════════════════════════════════════════════════════════════


def psf_read(path):
    """-> (reserved bytes, program bytes, tags dict, исходные строки тега)"""
    with open(path, 'rb') as f:
        d = f.read()
    if d[:3] != b'PSF':
        raise ValueError('не PSF: ' + path)
    res, prog, crc = struct.unpack('<III', d[4:16])
    reserved = d[16:16 + res]
    program = d[16 + res:16 + res + prog]
    tags = {}
    t = d[16 + res + prog:]
    if t[:5] == b'[TAG]':
        for ln in t[5:].decode('utf-8', 'replace').split('\n'):
            if '=' in ln:
                k, v = ln.split('=', 1)
                tags[k.strip().lower()] = v.strip()
    return reserved, program, tags


def _le32(b, o):
    return struct.unpack_from('<I', b, o)[0]


class Usf:
    """Содержимое USF (после загрузки всех _lib): ROM и сохранённое состояние."""

    def __init__(self):
        self.rom = bytearray()
        self.save = bytearray(0x80275C)
        self.tags = {}

    def load(self, path, _seen=None):
        _seen = _seen or set()
        path = os.path.abspath(path)
        if path in _seen:
            return
        _seen.add(path)
        reserved, program, tags = psf_read(path)
        d = os.path.dirname(path)
        libs = sorted([(k, v) for k, v in tags.items() if k.startswith('_lib')])
        first = [v for k, v in libs if k == '_lib']
        later = [v for k, v in libs if k != '_lib']
        for v in first:
            self.load(os.path.join(d, v), _seen)
        if program:
            raise ValueError('USF не должен содержать exe-секцию')
        self.upload(reserved)
        for v in later:
            self.load(os.path.join(d, v), _seen)
        for k, v in tags.items():
            if not k.startswith('_lib'):
                self.tags[k] = v

    def upload(self, data):
        size = len(data)
        if size < 4:
            raise ValueError('битая секция USF')
        p = 4
        temp = _le32(data, 0)
        if temp == 0x34365253:
            length = _le32(data, p)
            p += 4
            while length:
                start = _le32(data, p)
                p += 4
                if start + length > len(self.rom):
                    self.rom.extend(b'\x00' * (start + length - len(self.rom)))
                self.rom[start:start + length] = data[p:p + length]
                p += length
                length = _le32(data, p)
                p += 4
        temp = _le32(data, p)
        p += 4
        if temp == 0x34365253:
            length = _le32(data, p)
            p += 4
            while length:
                start = _le32(data, p)
                p += 4
                self.save[start:start + length] = data[p:p + length]
                p += length
                length = _le32(data, p)
                p += 4


def _pad_rom(rom):
    """Как lazyusf2: размер ROM - степень двойки, не меньше 1 МБ."""
    if not rom:
        return bytearray()
    size = 1024 * 1024
    while size < len(rom):
        size *= 2
    return rom + bytearray(size - len(rom))


# ═══════════════════════════════════════════════════════════════
# Машина
# ═══════════════════════════════════════════════════════════════

CP0_INDEX, CP0_RANDOM, CP0_ENTRYLO0, CP0_ENTRYLO1, CP0_CONTEXT, CP0_PAGEMASK, CP0_WIRED = 0, 1, 2, 3, 4, 5, 6
CP0_BADVADDR, CP0_COUNT, CP0_ENTRYHI, CP0_COMPARE, CP0_STATUS, CP0_CAUSE, CP0_EPC, CP0_PREVID = 8, 9, 10, 11, 12, 13, 14, 15
CP0_CONFIG, CP0_LLADDR, CP0_WATCHLO, CP0_WATCHHI, CP0_XCONTEXT = 16, 17, 18, 19, 20
CP0_TAGLO, CP0_TAGHI, CP0_ERROREPC = 28, 29, 30

VI_INT, COMPARE_INT, CHECK_INT, SI_INT, PI_INT, SPECIAL_INT, AI_INT, SP_INT, DP_INT = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100
HW2_INT, NMI_INT = 0x2000, 0x4000

MI_INTR_SP, MI_INTR_SI, MI_INTR_AI, MI_INTR_VI, MI_INTR_PI, MI_INTR_DP = 1, 2, 4, 8, 0x10, 0x20

AI_STATUS_FULL = 0x80000000
AI_STATUS_BUSY = 0x40000000


class CpuException(Exception):
    """Исключение процессора внутри команды (TLB, FPU unusable, SYSCALL...)."""

    def __init__(self, kind, addr=0, w=0, cause=0):
        self.kind = kind          # 'tlb', 'general'
        self.addr = addr
        self.w = w
        self.cause = cause


class Redirect(Exception):
    """Устройство/событие изменило PC (вход в обработчик прерывания)."""

    def __init__(self, pc):
        self.pc = pc


class Stop(Exception):
    pass


def s32(x):
    return ((x & M32) ^ 0x80000000) - 0x80000000


def s64(x):
    return ((x & M64) ^ 0x8000000000000000) - 0x8000000000000000


def f32_round(x):
    try:
        return struct.unpack('<f', struct.pack('<f', x))[0]
    except OverflowError:
        return math.inf if x > 0 else -math.inf


_PACK_F = struct.Struct('<f')
_PACK_I = struct.Struct('<I')
_PACK_D = struct.Struct('<d')
_PACK_Q = struct.Struct('<Q')


def bits_to_f(b):
    return _PACK_F.unpack(_PACK_I.pack(b & M32))[0]


def f_to_bits(f):
    try:
        return _PACK_I.unpack(_PACK_F.pack(f))[0]
    except OverflowError:
        return 0x7F800000 if f > 0 else 0xFF800000


def bits_to_d(b):
    return _PACK_D.unpack(_PACK_Q.pack(b & M64))[0]


def d_to_bits(d):
    return _PACK_Q.unpack(_PACK_D.pack(d))[0]


class N64:
    def __init__(self, usf):
        self.usf = usf
        self.rom = _pad_rom(usf.rom)
        self.rom_size = len(self.rom)
        self.save = bytes(usf.save)
        # регистры CPU
        self.R = [0] * 32
        self.hi = 0
        self.lo = 0
        self.cp0 = [0] * 32
        self.fgr = [0] * 32            # 64-битные образы FPR
        self.FCR0 = 0x511
        self.FCR31 = 0
        self.llbit = 0
        self.tlb_e = [dict(mask=0, vpn2=0, g=0, asid=0, pfn_even=0, c_even=0, d_even=0, v_even=0, pfn_odd=0,
                           c_odd=0, d_odd=0, v_odd=0, start_even=0, end_even=0, phys_even=0,
                           start_odd=0, end_odd=0, phys_odd=0) for _ in range(32)]
        self.tlb_r = {}
        self.tlb_w = {}
        # событие/счёт
        self.last_addr = 0
        self.cycle_count = 0
        self.next_interupt = 0
        self.queue = []                # [type, count] по возрастанию
        self.delay_slot = 0
        self.skip_jump = 0
        self.count_per_op = 2
        self.stop = False
        self.interupt_unsafe_state = 0
        # устройства
        self.rdram_size = 0x800000
        self.rd = None                 # array('I'), значения слов (big-endian семантика)
        self.spmem = array.array('I', [0]) * 0x800
        self.pif_ram = bytearray(64)
        self.rdram_regs = [0] * 10
        self.mi = [0] * 4
        self.audio_intr_reg = 0
        self.pi = [0] * 13
        self.sp = [0] * 8
        self.sp2 = [0, 0]
        self.si = [0] * 8
        self.vi = [0] * 14
        self.vi_delay = 5000
        self.vi_next = 5000
        self.vi_field = 0
        self.ri = [0] * 8
        self.ai = [0] * 6
        self.ai_fifo = [dict(address=0, length=0, duration=0), dict(address=0, length=0, duration=0)]
        self.ai_format_changed = 1
        self.dpc = [0] * 8
        self.dps = [0] * 4
        self.g_delay_sp = 1
        self.g_delay_ai = 0
        self.g_delay_pi = 0
        self.g_delay_si = 0
        self.g_delay_dp = 0
        self.enablecompare = 1 if usf.tags.get('_enablecompare') else 0
        self.enable_fifo_full = 1 if usf.tags.get('_enablefifofull') else 0
        # ROM params
        self.systemtype = 'NTSC'
        self.vilimit = 60
        self.aidacrate = 48681812
        # выход аудио
        self.audio_rate = 0
        self.vi_without_audio = 0
        self.audio_out = []            # list of bytes chunks (стерео 16 бит, little-endian пары L,R после перестановки)
        self.audio_samples = 0
        # трассировка
        self.trace = None
        # кэш декодированных инструкций
        self.icache = {}
        self.code_pages = bytearray(0x800)
        self.cur_ppc = 0
        self.rounding_mode = 0x33F
        self.debug_count = 0
        self.icount = 0
        self.stop_request = False
        self.resume_pc = None
        self.target_samples = 0
        self.dump = None
        self._load_state()
        self._build_decoder()

    # ── загрузка savestate PJ64 ──
    def _load_state(self):
        s = self.save
        if _le32(s, 4) == 0x400000:
            self.rdram_size = 0x400000
            s = s[:0x40275C]
        else:
            self.rdram_size = 0x800000
        if s[:4] != bytes((0xC8, 0xA6, 0xD8, 0x23)):
            raise ValueError('неизвестный формат savestate')
        rdram_bytes = _le32(s, 4)
        p = 8
        hdr = s[p:p + 0x40]
        self._parse_header(hdr)
        p += 0x40

        def u32():
            nonlocal p
            v = _le32(s, p)
            p += 4
            return v

        def u64():
            nonlocal p
            v = struct.unpack_from('<Q', s, p)[0]
            p += 8
            return v

        vi_timer = u32()
        self.last_addr = u32()
        for i in range(32):
            self.R[i] = s64(u64())
        for i in range(32):
            self.fgr[i] = u64()
        for i in range(32):
            self.cp0[i] = u32()
        self.FCR0 = s32(u32())
        p += 30 * 4
        self.FCR31 = s32(u32())
        self.hi = s64(u64())
        self.lo = s64(u64())
        for i in range(10):
            self.rdram_regs[i] = u32()
        for i in range(8):
            self.sp[i] = u32()
        self.sp2 = [u32(), u32()]
        for i in range(8):
            self.dpc[i] = u32()
        p += 8
        for i in range(4):
            self.mi[i] = u32()
        for i in range(14):
            self.vi[i] = u32()
        for i in range(6):
            self.ai[i] = u32()
        self.ai_format_changed = 1
        self.ai[3] = 0                                   # XXX USF
        for i in range(13):
            self.pi[i] = u32()
        for i in range(8):
            self.ri[i] = u32()
        self.si[0] = u32()
        self.si[1] = u32()
        self.si[4] = u32()
        self.si[6] = u32()
        self.tlb_r = {}
        self.tlb_w = {}
        for i in range(32):
            p += 4                                       # EntryDefined
            pagemask = u32()
            entryhi = u32()
            lo0 = u32()
            lo1 = u32()
            e = self.tlb_e[i]
            e['g'] = lo0 & lo1 & 1
            e['pfn_even'] = (lo0 & 0x3FFFFFC0) >> 6
            e['pfn_odd'] = (lo1 & 0x3FFFFFC0) >> 6
            e['c_even'] = (lo0 & 0x38) >> 3
            e['c_odd'] = (lo1 & 0x38) >> 3
            e['d_even'] = (lo0 & 4) >> 2
            e['d_odd'] = (lo1 & 4) >> 2
            e['v_even'] = (lo0 & 2) >> 1
            e['v_odd'] = (lo1 & 2) >> 1
            e['asid'] = entryhi & 0xFF
            e['vpn2'] = (entryhi & 0xFFFFE000) >> 13
            e['mask'] = (pagemask & 0x1FFE000) >> 13
            self._tlb_fill(e)
            self._tlb_map(e)
        self.pif_ram[:] = s[p:p + 64]
        p += 64
        words = rdram_bytes // 4
        self.rd = array.array('I', [0]) * (0x800000 // 4)
        self.rd[:words] = array.array('I', s[p:p + rdram_bytes])
        p += rdram_bytes
        self.spmem = array.array('I', s[p:p + 0x2000])
        # прерывания и таймеры
        count = self.cp0[CP0_COUNT]
        vi_timer = (vi_timer + count) & M32
        self.vi_next = vi_timer
        self.vi_field = 0
        count_per_scanline = int(f32_round(f32_round(self.aidacrate) / f32_round(self.vilimit))) // (self.vi[6] + 1)
        self.vi_delay = (self.vi[6] + 1) * count_per_scanline
        self._init_interupt_queue_from_state(vi_timer)
        self.pc = self.last_addr
        self.rounding_mode = 0x33F

    def _parse_header(self, hdr):
        h = bytearray(hdr)
        if h[:4] == bytes((0x37, 0x80, 0x40, 0x12)):
            for i in range(0, len(h), 2):
                h[i], h[i + 1] = h[i + 1], h[i]
        elif h[:4] == bytes((0x40, 0x12, 0x37, 0x80)):
            for i in range(0, len(h), 4):
                h[i], h[i + 3] = h[i + 3], h[i]
                h[i + 1], h[i + 2] = h[i + 2], h[i + 1]
        cc = h[0x3E] if len(h) > 0x3E else 0
        if cc in (0x44, 0x46, 0x49, 0x50, 0x53, 0x55, 0x58, 0x59):
            self.systemtype = 'PAL'
            self.vilimit = 50
            self.aidacrate = 49656530
        else:
            self.systemtype = 'NTSC'
            self.vilimit = 60
            self.aidacrate = 48681812

    def _tlb_fill(self, e):
        e['start_even'] = e['vpn2'] << 13
        e['end_even'] = e['start_even'] + (e['mask'] << 12) + 0xFFF
        e['phys_even'] = e['pfn_even'] << 12
        e['start_odd'] = e['end_even'] + 1
        e['end_odd'] = e['start_odd'] + (e['mask'] << 12) + 0xFFF
        e['phys_odd'] = e['pfn_odd'] << 12

    def _tlb_map(self, e):
        if e['v_even']:
            if e['start_even'] < e['end_even'] and not (e['start_even'] >= 0x80000000 and e['end_even'] < 0xC0000000) \
                    and e['phys_even'] < 0x20000000:
                for a in range(e['start_even'], e['end_even'], 0x1000):
                    self.tlb_r[a >> 12] = (e['phys_even'] + (a - e['start_even'])) >> 12
                if e['d_even']:
                    for a in range(e['start_even'], e['end_even'], 0x1000):
                        self.tlb_w[a >> 12] = (e['phys_even'] + (a - e['start_even'])) >> 12
        if e['v_odd']:
            if e['start_odd'] < e['end_odd'] and not (e['start_odd'] >= 0x80000000 and e['end_odd'] < 0xC0000000) \
                    and e['phys_odd'] < 0x20000000:
                for a in range(e['start_odd'], e['end_odd'], 0x1000):
                    self.tlb_r[a >> 12] = (e['phys_odd'] + (a - e['start_odd'])) >> 12
                if e['d_odd']:
                    for a in range(e['start_odd'], e['end_odd'], 0x1000):
                        self.tlb_w[a >> 12] = (e['phys_odd'] + (a - e['start_odd'])) >> 12

    def _tlb_unmap(self, e):
        if e['v_even']:
            for a in range(e['start_even'], e['end_even'], 0x1000):
                self.tlb_r.pop(a >> 12, None)
            if e['d_even']:
                for a in range(e['start_even'], e['end_even'], 0x1000):
                    self.tlb_w.pop(a >> 12, None)
        if e['v_odd']:
            for a in range(e['start_odd'], e['end_odd'], 0x1000):
                self.tlb_r.pop(a >> 12, None)
            if e['d_odd']:
                for a in range(e['start_odd'], e['end_odd'], 0x1000):
                    self.tlb_w.pop(a >> 12, None)

    # ── очередь событий (порт interupt.c) ──
    def _before_event(self, evt1, evt2):
        count = self.cp0[CP0_COUNT]
        if self.cycle_count > 0:
            count -= self.cycle_count
        return ((evt1 - count) & M32) < ((evt2 - count) & M32)

    def add_event_count(self, typ, count):
        count &= M32
        q = self.queue
        ev = [typ, count]
        cnt = self.cp0[CP0_COUNT]
        if not q:
            q.append(ev)
            self.next_interupt = count
            self.cycle_count = s32(cnt - count)
        elif self._before_event(count, q[0][1]):
            q.insert(0, ev)
            self.next_interupt = count
            self.cycle_count = s32(cnt - count)
        else:
            i = 0
            while i + 1 < len(q) and not self._before_event(count, q[i + 1][1]):
                i += 1
            if i + 1 >= len(q):
                q.append(ev)
            else:
                while i + 1 < len(q) and q[i + 1][1] == count:
                    i += 1
                q.insert(i + 1, ev)

    def add_event(self, typ, delay):
        self.add_event_count(typ, self.cp0[CP0_COUNT] + delay)

    def get_event(self, typ):
        for t, c in self.queue:
            if t == typ:
                return c
        return 0

    def remove_event(self, typ):
        for i, (t, c) in enumerate(self.queue):
            if t == typ:
                del self.queue[i]
                return

    def _remove_first(self):
        q = self.queue
        q.pop(0)
        cnt = self.cp0[CP0_COUNT]
        self.next_interupt = q[0][1] if q else 0
        self.cycle_count = s32(cnt - q[0][1]) if q else 0

    def _init_interupt_queue_from_state(self, vi_timer):
        self.queue = []
        self.add_event_count(VI_INT, vi_timer)
        self.add_event_count(COMPARE_INT, self.cp0[CP0_COMPARE])
        self.remove_event(SPECIAL_INT)
        self.add_event_count(SPECIAL_INT, (self.cp0[CP0_COUNT] & 0x80000000) ^ 0x80000000)
        c = self.cp0
        self.next_interupt = self.queue[0][1]
        self.cycle_count = s32(c[CP0_COUNT] - self.queue[0][1])

    def update_count(self, pc):
        count = ((((pc - self.last_addr) & M32) >> 2) * self.count_per_op) & M32
        c = self.cp0
        c[CP0_COUNT] = (c[CP0_COUNT] + count) & M32
        self.cycle_count += count
        self.last_addr = pc

    def check_interupt(self, pc):
        """check_interupt из interupt.c: вставляет CHECK_INT в голову очереди, если нужно."""
        mi = self.mi
        c = self.cp0
        mi[2] &= ~MI_INTR_AI
        mi[2] |= self.audio_intr_reg & MI_INTR_AI
        if mi[2] & mi[3]:
            c[CP0_CAUSE] = (c[CP0_CAUSE] | 0x400) & 0xFFFFFF83
        else:
            c[CP0_CAUSE] &= ~0x400
        if (c[CP0_STATUS] & 7) != 1:
            return
        if c[CP0_STATUS] & c[CP0_CAUSE] & 0xFF00:
            cnt = c[CP0_COUNT]
            self.next_interupt = cnt
            self.queue.insert(0, [CHECK_INT, cnt])
            self.cycle_count = 0

    def raise_maskable_interrupt(self, cause, pc):
        c = self.cp0
        c[CP0_CAUSE] = (c[CP0_CAUSE] | cause) & 0xFFFFFF83
        if not (c[CP0_STATUS] & c[CP0_CAUSE] & 0xFF00):
            return pc
        if (c[CP0_STATUS] & 7) != 1:
            return pc
        return self.exception_general(pc)

    def exception_general(self, pc):
        """exception_general: PC - значение 'PC->addr' (адрес EPC до поправки на delay slot)."""
        self.update_count(pc)
        c = self.cp0
        c[CP0_STATUS] |= 2
        c[CP0_EPC] = pc
        if self.delay_slot == 1 or self.delay_slot == 3:
            c[CP0_CAUSE] |= 0x80000000
            c[CP0_EPC] = (c[CP0_EPC] - 4) & M32
        else:
            c[CP0_CAUSE] &= 0x7FFFFFFF
        vec = 0x80000180
        self.last_addr = vec
        if self.delay_slot:
            self.skip_jump = vec
            self.next_interupt = 0
            self.cycle_count = 0
        return vec

    def tlb_refill_exception(self, address, w, pc):
        """TLB_refill_exception. pc - 'PC->addr' в момент исключения. w: 0 чтение, 1 запись, 2 выборка."""
        c = self.cp0
        if w != 2:
            self.update_count(pc)
        c[CP0_CAUSE] = (3 << 2) if w == 1 else (2 << 2)
        c[CP0_BADVADDR] = address
        c[CP0_CONTEXT] = (c[CP0_CONTEXT] & 0xFF80000F) | ((address >> 9) & 0x007FFFF0)
        c[CP0_ENTRYHI] = address & 0xFFFFE000
        if c[CP0_STATUS] & 2:
            vec = 0x80000180
            if self.delay_slot == 1 or self.delay_slot == 3:
                c[CP0_CAUSE] |= 0x80000000
            else:
                c[CP0_CAUSE] &= 0x7FFFFFFF
        else:
            c[CP0_EPC] = address if w == 2 else pc
            c[CP0_CAUSE] &= ~0x80000000 & M32
            c[CP0_STATUS] |= 2
            usual = 0x80000000 <= address < 0xC0000000
            for e in self.tlb_e:
                if e['start_even'] <= address <= e['end_even'] or e['start_odd'] <= address <= e['end_odd']:
                    usual = True
            vec = 0x80000180 if usual else 0x80000000
        if self.delay_slot == 1 or self.delay_slot == 3:
            c[CP0_CAUSE] |= 0x80000000
            c[CP0_EPC] = (c[CP0_EPC] - 4) & M32
        else:
            c[CP0_CAUSE] &= 0x7FFFFFFF
        if w != 2:
            c[CP0_EPC] = (c[CP0_EPC] - 4) & M32
        self.last_addr = vec
        if self.delay_slot:
            self.skip_jump = vec
            self.next_interupt = 0
            self.cycle_count = 0
        return vec

    # ═══════════ память ═══════════
    def virt_to_phys(self, a, w, pc):
        """Адрес -> физический. w: 0 чтение, 1 запись, 2 выборка. При промахе TLB - CpuException."""
        a &= M32
        if (a & 0xC0000000) == 0x80000000:
            return a & 0x1FFFFFFF
        if w == 1:
            pg = self.tlb_w.get(a >> 12)
            if pg is not None:
                return (pg << 12) | (a & 0xFFF)
            if True:                       # g_disable_tlb_write_exception = 1
                return 0
        pg = self.tlb_r.get(a >> 12)
        if pg is not None:
            return (pg << 12) | (a & 0xFFF)
        raise CpuException('tlb', a, w)

    def read_phys(self, p):
        """Слово из физической памяти/устройств."""
        if p < self.rdram_size:
            return self.rd[p >> 2]
        if p >= 0x10000000:
            if p < 0x1FC00000:
                off = p - 0x10000000
                if off + 4 <= self.rom_size:
                    return int.from_bytes(self.rom[off & ~3:(off & ~3) + 4], 'little')
                return 0
            if 0x1FC007C0 <= p < 0x1FC00800:
                i = (p & ~3) - 0x1FC007C0
                return int.from_bytes(self.pif_ram[i:i + 4], 'big')
            return 0
        if p < 0x04000000:
            if 0x03F00000 <= p < 0x03F00400:
                return self.rdram_regs[((p & 0x3FF) >> 2) % 10] if ((p & 0x3FF) >> 2) < 10 else 0
            return 0
        reg = (p & 0xFFFF) >> 2
        r = p >> 16
        if r == 0x0400:
            return self.spmem[(p & 0x1FFC) >> 2]
        if r == 0x0404:
            if reg == 7:
                v = self.sp[7]
                self.sp[7] = 1
                return v
            return self.sp[reg] if reg < 8 else 0
        if r == 0x0408:
            return self.sp2[reg] if reg < 2 else 0
        if r == 0x0410:
            return self.dpc[reg] if reg < 8 else 0
        if r == 0x0420:
            return self.dps[reg] if reg < 4 else 0
        if r == 0x0430:
            return self.mi[reg] if reg < 4 else 0
        if r == 0x0440:
            if reg == 4:
                self.update_count_now()
                v = ((self.vi_delay - ((self.vi_next - self.cp0[CP0_COUNT]) & M32)) & M32) // 1500
                self.vi[4] = (v & ~1) | self.vi_field
            return self.vi[reg] if reg < 14 else 0
        if r == 0x0450:
            if reg == 1:
                return self.ai_remaining_len()
            return self.ai[reg] if reg < 6 else 0
        if r == 0x0460:
            return self.pi[reg] if reg < 13 else 0
        if r == 0x0470:
            return self.ri[reg] if reg < 8 else 0
        if r == 0x0480:
            return self.si[reg] if reg < 8 else 0
        if r == 0x0500 and p == 0x05000508:
            return 0xFFFFFFFF
        return 0

    def update_count_now(self):
        self.update_count(self.cur_ppc)

    def write_phys(self, p, v, mask, retpc):
        """Слово (с маской) в физическую память/устройства; retpc - PC следующей команды (для прерываний)."""
        v &= M32
        if p < self.rdram_size:
            i = p >> 2
            if mask == M32:
                self.rd[i] = v
            else:
                self.rd[i] = (self.rd[i] & ~mask & M32) | (v & mask)
            return
        if p < 0x04000000:
            if 0x03F00000 <= p < 0x03F00400:
                i = (p & 0x3FF) >> 2
                if i < 10:
                    self.rdram_regs[i] = (self.rdram_regs[i] & ~mask & M32) | (v & mask)
            return
        if p >= 0x10000000:
            if 0x1FC007C0 <= p < 0x1FC00800:
                i = (p & ~3) - 0x1FC007C0
                cur = int.from_bytes(self.pif_ram[i:i + 4], 'big')
                nv = (cur & ~mask & M32) | (v & mask)
                self.pif_ram[i:i + 4] = nv.to_bytes(4, 'big')
            return
        reg = (p & 0xFFFF) >> 2
        r = p >> 16
        self.cur_ppc = retpc
        if r == 0x0400:
            i = (p & 0x1FFC) >> 2
            self.spmem[i] = (self.spmem[i] & ~mask & M32) | (v & mask)
        elif r == 0x0404:
            self.write_sp_regs(reg, v & mask if reg == 4 else v, mask, retpc)
        elif r == 0x0408:
            if reg < 2:
                self.sp2[reg] = (self.sp2[reg] & ~mask & M32) | (v & mask)
        elif r == 0x0410:
            self.write_dpc(reg, v, mask, retpc)
        elif r == 0x0420:
            if reg < 4:
                self.dps[reg] = (self.dps[reg] & ~mask & M32) | (v & mask)
        elif r == 0x0430:
            self.write_mi(reg, v, mask, retpc)
        elif r == 0x0440:
            self.write_vi(reg, v, mask, retpc)
        elif r == 0x0450:
            self.write_ai(reg, v, mask, retpc)
        elif r == 0x0460:
            self.write_pi(reg, v, mask, retpc)
        elif r == 0x0470:
            if reg < 8:
                self.ri[reg] = (self.ri[reg] & ~mask & M32) | (v & mask)
        elif r == 0x0480:
            self.write_si(reg, v, mask, retpc)

    # ═══════════ устройства ═══════════
    def raise_rcp_interrupt(self, bit, pc):
        self.mi[2] |= bit
        if self.mi[2] & self.mi[3]:
            return self.raise_maskable_interrupt(0x400, pc)
        return pc

    def signal_rcp_interrupt(self, bit, pc):
        self.mi[2] |= bit
        self.check_interupt(pc)

    def clear_rcp_interrupt(self, bit, pc):
        self.mi[2] &= ~bit
        self.check_interupt(pc)

    def _redirect_if(self, old, new):
        if new != old:
            raise Redirect(new)

    # SP
    def write_sp_regs(self, reg, v, mask, pc):
        if reg == 4:
            self.update_sp_status(v & mask, pc)
            return
        if reg in (5, 6):
            return
        if reg >= 8:
            return
        self.sp[reg] = (self.sp[reg] & ~mask & M32) | (v & mask)
        if reg == 2:
            self.dma_sp_write()
        elif reg == 3:
            self.dma_sp_read()
        elif reg == 7:
            self.sp[7] = 0

    def dma_sp_write(self):
        """DRAM -> SP mem (RD_LEN)."""
        sp = self.sp
        l = sp[2]
        length = ((l & 0xFFF) | 7) + 1
        count = ((l >> 12) & 0xFF) + 1
        skip = (l >> 20) & 0xFFF
        memaddr = sp[0] & 0xFFF
        dramaddr = sp[1] & 0xFFFFFF
        base = sp[0] & 0x1000
        for _ in range(count):
            data = self.dram_read_bytes(dramaddr, length)
            self.spmem_write_bytes(base + memaddr, data)
            memaddr += length
            dramaddr += length + skip

    def dma_sp_read(self):
        """SP mem -> DRAM (WR_LEN)."""
        sp = self.sp
        l = sp[3]
        length = ((l & 0xFFF) | 7) + 1
        count = ((l >> 12) & 0xFF) + 1
        skip = (l >> 20) & 0xFFF
        memaddr = sp[0] & 0xFFF
        dramaddr = sp[1] & 0xFFFFFF
        base = sp[0] & 0x1000
        for _ in range(count):
            data = self.spmem_read_bytes(base + memaddr, length)
            self.dram_write_bytes(dramaddr, data)
            memaddr += length
            dramaddr += length + skip

    def dram_read_bytes(self, addr, n):
        if n <= 0:
            return b''
        a0 = addr & ~3
        a1 = (addr + n + 3) & ~3
        w = self.rd[a0 >> 2:a1 >> 2]
        if len(w) * 4 != a1 - a0:
            w = w + array.array('I', [0]) * ((a1 - a0) // 4 - len(w))
        w.byteswap()
        b = w.tobytes()
        return b[addr - a0:addr - a0 + n]

    def dram_write_bytes(self, addr, data):
        n = len(data)
        if n == 0:
            return
        addr &= 0xFFFFFF
        if addr >= self.rdram_size:
            return
        n = min(n, self.rdram_size - addr)
        data = data[:n]
        a0 = addr & ~3
        a1 = (addr + n + 3) & ~3
        first = self.rd[a0 >> 2]
        last = self.rd[(a1 >> 2) - 1]
        buf = bytearray(a1 - a0)
        buf[0:4] = first.to_bytes(4, 'big')
        buf[-4:] = last.to_bytes(4, 'big')
        buf[addr - a0:addr - a0 + n] = data
        w = array.array('I', bytes(buf))
        w.byteswap()
        self.rd[a0 >> 2:a1 >> 2] = w
        if self.code_pages[addr >> 12] or self.code_pages[(addr + n - 1) >> 12]:
            self.flush_icache()

    def spmem_read_bytes(self, off, n):
        w = self.spmem[(off & ~3) >> 2:((off + n + 3) & ~3) >> 2]
        w = array.array('I', w)
        w.byteswap()
        b = w.tobytes()
        o = off & 3
        return b[o:o + n]

    def spmem_write_bytes(self, off, data):
        n = len(data)
        a0 = off & ~3
        a1 = (off + n + 3) & ~3
        cur = array.array('I', self.spmem[a0 >> 2:a1 >> 2])
        cur.byteswap()
        buf = bytearray(cur.tobytes())
        o = off - a0
        buf[o:o + n] = data
        w = array.array('I', bytes(buf))
        w.byteswap()
        self.spmem[a0 >> 2:a1 >> 2] = w

    def update_sp_status(self, w, pc):
        sp = self.sp
        if w & 0x1:
            sp[4] &= ~0x1
        if w & 0x2:
            sp[4] |= 0x1
        if w & 0x4:
            sp[4] &= ~0x2
        if w & 0x8:
            self.clear_rcp_interrupt(MI_INTR_SP, pc)
        if w & 0x10:
            self.signal_rcp_interrupt(MI_INTR_SP, pc)
        for bit, (clr, st) in enumerate(((0x20, 0x40), (0x80, 0x100), (0x200, 0x400), (0x800, 0x1000),
                                         (0x2000, 0x4000), (0x8000, 0x10000), (0x20000, 0x40000),
                                         (0x80000, 0x100000), (0x200000, 0x400000), (0x800000, 0x1000000))):
            sbit = 0x20 << bit
            if w & clr:
                sp[4] &= ~sbit
            if w & st:
                sp[4] |= sbit
        if not (w & 0x1) and not (w & 0x4):
            return
        if not (sp[4] & 0x3):
            self.do_sp_task(pc)

    def do_sp_task(self, pc):
        sp = self.sp
        save_pc = self.sp2[0] & ~0xFFF
        task_type = self.spmem[0xFC0 >> 2]
        if task_type == 1 and (self.dpc[3] & 0x2):
            return                                   # DP заморожен - задача выполнится при разморозке
        self.sp2[0] &= 0xFFF
        self.run_rsp_task(task_type)
        self.sp2[0] |= save_pc
        self.update_count(pc)
        if task_type == 1:
            if self.mi[2] & MI_INTR_SP:
                self.add_event(SP_INT, 1000 if self.g_delay_sp else 0)
            if self.mi[2] & MI_INTR_DP:
                self.add_event(DP_INT, 1000 if self.g_delay_dp else 0)
            self.mi[2] &= ~(MI_INTR_SP | MI_INTR_DP)
            sp[4] &= ~0x303
        elif task_type == 2:
            if self.mi[2] & MI_INTR_SP:
                self.add_event(SP_INT, 4000 if self.g_delay_sp else 0)
            self.mi[2] &= ~MI_INTR_SP
            sp[4] &= ~0x303
        else:
            if self.mi[2] & MI_INTR_SP:
                self.add_event(SP_INT, 0)
            self.mi[2] &= ~MI_INTR_SP
            sp[4] &= ~0x203

    def rsp_break(self, setbits):
        sp = self.sp
        sp[4] |= setbits | 0x2 | 0x1
        if sp[4] & 0x40:
            self.mi[2] |= MI_INTR_SP

    def rsp_interrupt_event(self, pc):
        sp = self.sp
        sp[4] |= 0x203
        if sp[4] & 0x40:
            return self.raise_rcp_interrupt(MI_INTR_SP, pc)
        return pc

    # DP
    def write_dpc(self, reg, v, mask, pc):
        w = v & mask
        if reg == 3:
            dp = self.dpc
            run = 0
            if w & 0x1:
                dp[3] &= ~0x1
            if w & 0x2:
                dp[3] |= 0x1
            if w & 0x4:
                dp[3] &= ~0x2
                if not (self.sp[4] & 0x3):
                    run = 1
            if w & 0x8:
                dp[3] |= 0x2
            if w & 0x10:
                dp[3] &= ~0x4
            if w & 0x20:
                dp[3] |= 0x4
            if run:
                self.do_sp_task(pc)
            return
        if reg in (2, 4, 5, 6, 7):
            return
        if reg < 8:
            self.dpc[reg] = (self.dpc[reg] & ~mask & M32) | (v & mask)
            if reg == 0:
                self.dpc[2] = self.dpc[0]
            elif reg == 1:
                self.signal_rcp_interrupt(MI_INTR_DP, pc)

    def rdp_interrupt_event(self, pc):
        self.dpc[3] &= ~2
        self.dpc[3] |= 0x81
        return self.raise_rcp_interrupt(MI_INTR_DP, pc)

    # MI
    def write_mi(self, reg, v, mask, pc):
        w = v & mask
        mi = self.mi
        if reg == 0:
            mi[0] |= w & 0x7F
            if w & 0x80:
                mi[0] &= ~0x80
            if w & 0x100:
                mi[0] |= 0x80
            if w & 0x200:
                mi[0] &= ~0x100
            if w & 0x400:
                mi[0] |= 0x100
            if w & 0x1000:
                mi[0] &= ~0x200
            if w & 0x2000:
                mi[0] |= 0x200
            if w & 0x800:
                self.clear_rcp_interrupt(MI_INTR_DP, pc)
        elif reg == 3:
            m = mi[3]
            for clr, st, b in ((0x1, 0x2, 0x1), (0x4, 0x8, 0x2), (0x10, 0x20, 0x4), (0x40, 0x80, 0x8),
                               (0x100, 0x200, 0x10), (0x400, 0x800, 0x20)):
                if w & clr:
                    m &= ~b
                if w & st:
                    m |= b
            mi[3] = m
            self.check_interupt(pc)
            self.update_count(pc)
            if self.cycle_count >= 0:
                npc = self.gen_interupt(pc)
                if npc != pc:
                    raise Redirect(npc)

    # VI
    def write_vi(self, reg, v, mask, pc):
        vi = self.vi
        if reg == 4:
            self.clear_rcp_interrupt(MI_INTR_VI, pc)
            return
        if reg >= 14:
            return
        vi[reg] = (vi[reg] & ~mask & M32) | (v & mask)
        if reg == 6:
            cps = int(f32_round(f32_round(self.aidacrate) / f32_round(self.vilimit)) / (vi[6] + 1))
            self.vi_delay = ((vi[6] + 1) * cps) & M32
            if vi[6] != 0 and self.vi_next == 0:
                self.update_count(pc)
                self.vi_next = (self.cp0[CP0_COUNT] + self.vi_delay) & M32
                self.add_event_count(VI_INT, self.vi_next)

    def vi_vertical_interrupt_event(self, pc):
        self.update_count(pc)
        self.vi_field ^= (self.vi[0] >> 6) & 1
        self.vi_next = (self.cp0[CP0_COUNT] + self.vi_delay) & M32
        self.add_event_count(VI_INT, self.vi_next)
        # игра перестала выдавать звук (SFX закончился, пауза): без тишины цикл ждал бы вечно
        # (как silence.patch в lazyusf2): через ~1 с без AI-данных досыпаем нули на каждый кадр VI
        self.vi_without_audio += 1
        if self.vi_without_audio > 60 and self.audio_rate > 0:
            n = min(4096, self.audio_rate * self.vi_delay // 93750000)
            if n:
                self.audio_out.append(bytes(n * 4))
                self.audio_samples += n
                if self.target_samples and self.audio_samples >= self.target_samples:
                    self.stop_request = True
        return self.raise_rcp_interrupt(MI_INTR_VI, pc)

    # AI
    def ai_dma_duration(self):
        sps = self.aidacrate // (1 + self.ai[4])
        return ((self.ai[1] * self.vi_delay * self.vilimit) // (4 * sps)) & M32

    def ai_remaining_len(self):
        f0 = self.ai_fifo[0]
        if f0['duration'] == 0:
            return 0
        self.update_count_now()
        nxt = self.get_event(AI_INT)
        if nxt == 0:
            return 0
        c = self.cp0[CP0_COUNT]
        if ((c - nxt) & M32) < 0x80000000:
            return 0
        rem = (nxt - c) & M32
        if rem >= 0x80000000:
            return 0
        return ((rem * f0['length'] // f0['duration']) & M32) & ~7

    def ai_do_dma(self, dma):
        if self.ai_format_changed:
            freq = 44100 if self.ai[4] == 0 else self.aidacrate // (1 + self.ai[4])
            self.audio_rate = freq
            self.ai_format_changed = 0
        self.push_audio(dma['address'], dma['length'])
        self.update_count_now()
        if not (self.ai[3] & AI_STATUS_FULL):
            self.remove_event(AI_INT)
            self.add_event(AI_INT, dma['duration'] if self.g_delay_ai else 0)

    def push_audio(self, address, length):
        self.vi_without_audio = 0
        data = self.dram_read_bytes(address & 0xFFFFFF, length)
        self.audio_out.append(data)
        self.audio_samples += length // 4

    def ai_fifo_push(self):
        duration = self.ai_dma_duration()
        ai = self.ai
        if ai[3] & AI_STATUS_BUSY:
            f = self.ai_fifo[1]
            f['address'] = ai[0]
            f['length'] = ai[1]
            f['duration'] = duration
            if self.enable_fifo_full:
                ai[3] |= AI_STATUS_FULL
            else:
                self.ai_do_dma(f)
        else:
            f = self.ai_fifo[0]
            f['address'] = ai[0]
            f['length'] = ai[1]
            f['duration'] = duration
            ai[3] |= AI_STATUS_BUSY
            self.ai_do_dma(f)

    def ai_fifo_pop(self):
        ai = self.ai
        if ai[3] & AI_STATUS_FULL:
            f0, f1 = self.ai_fifo
            f0.update(f1)
            ai[3] &= ~AI_STATUS_FULL
            self.ai_do_dma(f0)
        else:
            ai[3] &= ~AI_STATUS_BUSY

    def write_ai(self, reg, v, mask, pc):
        ai = self.ai
        if reg == 1:
            ai[1] = (ai[1] & ~mask & M32) | (v & mask)
            self.cur_ppc = pc
            self.ai_fifo_push()
            return
        if reg == 3:
            self.clear_rcp_interrupt(MI_INTR_AI, pc)
            self.audio_intr_reg &= ~MI_INTR_AI
            return
        if reg in (4, 5):
            if ai[reg] != (v & mask):
                self.ai_format_changed = 1
        if reg < 6:
            ai[reg] = (ai[reg] & ~mask & M32) | (v & mask)

    def ai_end_of_dma_event(self, pc):
        self.ai_fifo_pop()
        self.audio_intr_reg |= MI_INTR_AI
        return self.raise_rcp_interrupt(MI_INTR_AI, pc)

    # PI
    def write_pi(self, reg, v, mask, pc):
        pi = self.pi
        if reg == 2:
            pi[2] = (pi[2] & ~mask & M32) | (v & mask)
            pi[4] |= 1
            self.update_count(pc)
            self.add_event(PI_INT, 0x1000 if self.g_delay_pi else 0)
            return
        if reg == 3:
            pi[3] = (pi[3] & ~mask & M32) | (v & mask)
            self.dma_pi_write(pc)
            return
        if reg == 4:
            if (v & mask) & 2:
                self.clear_rcp_interrupt(MI_INTR_PI, pc)
            return
        if reg < 13:
            if 5 <= reg <= 12:
                v &= 0xFF
            pi[reg] = (pi[reg] & ~mask & M32) | (v & mask)

    def dma_pi_write(self, pc):
        pi = self.pi
        cart = pi[1]
        if cart < 0x10000000 or cart >= 0x1FC00000:
            pi[4] |= 1
            self.update_count(pc)
            self.add_event(PI_INT, 0x1000 if self.g_delay_pi else 0)
            return
        longueur = (pi[3] & 0xFFFFFF) + 1
        i = (cart - 0x10000000) & 0x3FFFFFF
        if i + longueur > self.rom_size:
            longueur = self.rom_size - i
        if pi[0] + longueur > 0x7FFFFF:
            longueur = 0x7FFFFF - pi[0]
        if i > self.rom_size or pi[0] > 0x7FFFFF:
            pi[4] |= 3
            self.update_count(pc)
            self.add_event(PI_INT, longueur // 8 if self.g_delay_pi else 0)
            return
        data = self.rom_be(i, longueur)
        self.dram_write_bytes(pi[0], data)
        pi[4] |= 3
        self.update_count(pc)
        self.add_event(PI_INT, longueur // 8 if self.g_delay_pi else 0)

    def rom_be(self, off, n):
        """Байты ROM в порядке N64 (big-endian)."""
        a0 = off & ~3
        a1 = (off + n + 3) & ~3
        w = array.array('I', bytes(self.rom[a0:a1]))
        w.byteswap()
        b = w.tobytes()
        return b[off - a0:off - a0 + n]

    def pi_end_of_dma_event(self, pc):
        self.pi[4] &= ~3
        return self.raise_rcp_interrupt(MI_INTR_PI, pc)

    # SI
    def write_si(self, reg, v, mask, pc):
        si = self.si
        if reg == 0:
            si[0] = (si[0] & ~mask & M32) | (v & mask)
        elif reg == 1:
            si[1] = (si[1] & ~mask & M32) | (v & mask)
            self.dma_si_read(pc)
        elif reg == 4:
            si[4] = (si[4] & ~mask & M32) | (v & mask)
            self.dma_si_write(pc)
        elif reg == 6:
            si[6] &= ~0x1000
            self.clear_rcp_interrupt(MI_INTR_SI, pc)

    def dma_si_write(self, pc):
        si = self.si
        self.pif_ram[:] = self.dram_read_bytes(si[0] & 0xFFFFFF, 64)
        self.update_pif_write()
        self.update_count(pc)
        if self.g_delay_si:
            self.add_event(SI_INT, 0x900)
        else:
            si[6] |= 0x1000
            self.signal_rcp_interrupt(MI_INTR_SI, pc)

    def dma_si_read(self, pc):
        si = self.si
        self.update_pif_read()
        self.dram_write_bytes(si[0] & 0xFFFFFF, bytes(self.pif_ram))
        self.update_count(pc)
        if self.g_delay_si:
            self.add_event(SI_INT, 0x900)
        else:
            si[6] |= 0x1000
            self.signal_rcp_interrupt(MI_INTR_SI, pc)

    def update_pif_write(self):
        ram = self.pif_ram
        if ram[0x3F] > 1:
            if ram[0x3F] == 0x08:
                ram[0x3F] = 0
            elif ram[0x3F] == 0x02:
                ram[46] = 0
                ram[47] = 0
                ram[63] = 0
            return
        i = 0
        channel = 0
        while i < 0x40:
            b = ram[i]
            if b == 0x00:
                channel += 1
                if channel > 6:
                    i = 0x40
            elif b == 0xFF:
                pass
            else:
                if not (b & 0xC0):
                    if channel < 4:
                        self._pif_cmd(i)
                    i += b + (ram[i + 1] & 0x3F) + 1
                    channel += 1
                else:
                    i = 0x40
            i += 1

    def _pif_cmd(self, i):
        # контроллеров нет: бит 0x80 в ответе (нет устройства)
        self.pif_ram[i + 1] |= 0x80

    def update_pif_read(self):
        ram = self.pif_ram
        i = 0
        channel = 0
        while i < 0x40:
            b = ram[i]
            if b == 0x00:
                channel += 1
                if channel > 6:
                    i = 0x40
            elif b == 0xFE:
                i = 0x40
            elif b in (0xFF, 0xB4, 0x56, 0xB8):
                pass
            else:
                if not (b & 0xC0):
                    i += b + (ram[i + 1] & 0x3F) + 1
                    channel += 1
                else:
                    i = 0x40
            i += 1

    def si_end_of_dma_event(self, pc):
        self.pif_ram[0x3F] = 0
        self.si[6] |= 0x1000
        return self.raise_rcp_interrupt(MI_INTR_SI, pc)

    # ── события ──
    def gen_interupt(self, pc):
        """gen_interupt: обрабатывает первое событие очереди, возвращает PC для продолжения."""
        if self.skip_jump:
            dest = self.skip_jump
            self.skip_jump = 0
            q = self.queue
            self.next_interupt = q[0][1] if q else 0
            self.cycle_count = s32(self.cp0[CP0_COUNT] - q[0][1]) if q else 0
            self.last_addr = dest
            return dest
        typ = self.queue[0][0]
        if typ == SPECIAL_INT:
            self._remove_first()
            self.add_event_count(SPECIAL_INT, (self.cp0[CP0_COUNT] & 0x80000000) ^ 0x80000000)
        elif typ == VI_INT:
            self._remove_first()
            pc = self.vi_vertical_interrupt_event(pc)
        elif typ == COMPARE_INT:
            self._remove_first()
            c = self.cp0
            c[CP0_COUNT] = (c[CP0_COUNT] + self.count_per_op) & M32
            self.cycle_count += self.count_per_op
            self.add_event_count(COMPARE_INT, c[CP0_COMPARE])
            c[CP0_COUNT] = (c[CP0_COUNT] - self.count_per_op) & M32
            self.cycle_count = s32(c[CP0_COUNT] - self.queue[0][1])
            if self.enablecompare:
                pc = self.raise_maskable_interrupt(0x8000, pc)
        elif typ == CHECK_INT:
            self._remove_first()
            pc = self.exception_general(pc)
        elif typ == SI_INT:
            self._remove_first()
            pc = self.si_end_of_dma_event(pc)
        elif typ == PI_INT:
            self._remove_first()
            pc = self.pi_end_of_dma_event(pc)
        elif typ == AI_INT:
            self._remove_first()
            pc = self.ai_end_of_dma_event(pc)
        elif typ == SP_INT:
            self._remove_first()
            pc = self.rsp_interrupt_event(pc)
        elif typ == DP_INT:
            self._remove_first()
            pc = self.rdp_interrupt_event(pc)
        else:
            self._remove_first()
            pc = self.exception_general(pc)
        return pc

    def flush_icache(self):
        self.icache.clear()
        self.code_pages = bytearray(0x800)

    # ═══════════ декодер ═══════════
    def fetch_word(self, pc):
        a = pc & M32
        if (a & 0xC0000000) == 0x80000000:
            p = a & 0x1FFFFFFF
        else:
            p = self.virt_to_phys(a, 2, a)
        if p < self.rdram_size:
            return self.rd[p >> 2], p
        if p >= 0x10000000:
            off = p - 0x10000000
            if off + 4 <= self.rom_size:
                return int.from_bytes(self.rom[off & ~3:(off & ~3) + 4], 'little'), p
            return ((off & 0xFFFC) << 16) | (off & 0xFFFC), p
        if (p & 0xFFFFE000) == 0x04000000:
            return self.spmem[(p & 0x1FFC) >> 2], p
        return 0, p

    def decode_at(self, pc):
        op, p = self.fetch_word(pc)
        f = self.make_insn(op, pc)
        self.icache[pc] = f
        if p < 0x800000:
            self.code_pages[p >> 12] = 1
        return f

    def handle_exception(self, e, ipc):
        """CpuException внутри команды по адресу ipc -> новый PC."""
        if e.kind == 'tlb':
            if e.w == 2:
                return self.tlb_refill_exception(e.addr, 2, e.addr)
            return self.tlb_refill_exception(e.addr, e.w, (ipc + 4) & M32)
        self.cp0[CP0_CAUSE] = e.cause
        return self.exception_general(ipc)

    def do_jump(self, pc, take, target, likely):
        """Общая часть DECLARE_JUMP: delay slot, учёт Count, события."""
        if not likely or take:
            dpc = (pc + 4) & M32
            self.delay_slot = 1
            f = self.icache.get(dpc)
            if f is None:
                try:
                    f = self.decode_at(dpc)
                except CpuException as e:
                    npc = self.handle_exception(e, dpc)
                    f = None
            if f is not None:
                if self.debug_count:
                    self.icount += 1
                    self._debug_dump(dpc)
                try:
                    npc = f(dpc)
                except CpuException as e:
                    npc = self.handle_exception(e, dpc)
                except Redirect as e:
                    npc = e.pc
            self.update_count(npc)
            self.delay_slot = 0
            if take and not self.skip_jump:
                npc = target & M32
        else:
            npc = (pc + 8) & M32
            self.update_count(npc)
        self.last_addr = npc
        if self.cycle_count >= 0:
            npc = self.gen_interupt(npc)
        if self.stop_request:
            self.stop_request = False
            self.resume_pc = npc
            raise Enough()
        return npc

    def _debug_dump(self, pc):
        pass

    def check_cop1(self):
        if not (self.cp0[CP0_STATUS] & 0x20000000):
            raise CpuException('general', 0, 0, (11 << 2) | 0x10000000)

    # ── FPU: доступ к регистрам ──
    def f_get_s(self, i):
        if self.cp0[CP0_STATUS] & 0x04000000:
            return bits_to_f(self.fgr[i])
        return bits_to_f(self.fgr[i >> 1] >> (32 * (i & 1)))

    def f_get_sbits(self, i):
        if self.cp0[CP0_STATUS] & 0x04000000:
            return self.fgr[i] & M32
        return (self.fgr[i >> 1] >> (32 * (i & 1))) & M32

    def f_set_sbits(self, i, bits):
        bits &= M32
        if self.cp0[CP0_STATUS] & 0x04000000:
            self.fgr[i] = (self.fgr[i] & ~M32 & M64) | bits
        else:
            sh = 32 * (i & 1)
            j = i >> 1
            self.fgr[j] = (self.fgr[j] & ~(M32 << sh) & M64) | (bits << sh)

    def f_get_dbits(self, i):
        if self.cp0[CP0_STATUS] & 0x04000000:
            return self.fgr[i]
        return self.fgr[i >> 1]

    def f_set_dbits(self, i, bits):
        if self.cp0[CP0_STATUS] & 0x04000000:
            self.fgr[i] = bits & M64
        else:
            self.fgr[i >> 1] = bits & M64

    def shuffle_fpr(self, old, new):
        if (new & 0x04000000) == (old & 0x04000000):
            return
        fg = self.fgr
        if new & 0x04000000:
            tmp = [(fg[i >> 1] >> (32 * (i & 1))) & M32 for i in range(32)]
            hi = [(fg[(i >> 1) + 16] >> (32 * (i & 1))) & M32 for i in range(32)]
            for i in range(32):
                fg[i] = (hi[i] << 32) | tmp[i]
        else:
            hi = [(fg[i] >> 32) & M32 for i in range(32)]
            lo = [fg[i] & M32 for i in range(32)]
            for i in range(16):
                fg[i] = (lo[i * 2 + 1] << 32) | lo[i * 2]
            for i in range(32):
                j = (i >> 1) + 16
                sh = 32 * (i & 1)
                fg[j] = (fg[j] & ~(M32 << sh) & M64) | (hi[i] << sh)

    def _build_decoder(self):
        m = self
        R = self.R
        RD = self.rd
        cp0 = self.cp0
        fgr = self.fgr
        rdsize = self.rdram_size
        CE = CpuException

        def NI(msg):
            def f(pc):
                raise Stop('нереализованный опкод %s @ %08x' % (msg, pc))
            return f

        def NOP_():
            def f(pc):
                return pc + 4
            return f

        # ─── память ───
        def vp(a, w, pc):
            if (a & 0xC0000000) == 0x80000000:
                return a & 0x1FFFFFFF
            return m.virt_to_phys(a, w, pc)

        def read_word(a, ppc):
            p = a & 0x1FFFFFFF if (a & 0xC0000000) == 0x80000000 else m.virt_to_phys(a, 0, ppc)
            if p < rdsize:
                return RD[p >> 2]
            m.cur_ppc = ppc
            return m.read_phys(p & ~3)

        def write_word(a, v, mask, ppc):
            p = a & 0x1FFFFFFF if (a & 0xC0000000) == 0x80000000 else m.virt_to_phys(a, 1, ppc)
            if p < rdsize:
                i = p >> 2
                if mask == M32:
                    RD[i] = v & M32
                else:
                    RD[i] = (RD[i] & ~mask & M32) | (v & mask)
                if m.code_pages[p >> 12]:
                    m.flush_icache()
                return
            m.write_phys(p & ~3, v, mask, ppc)

        self.read_word = read_word
        self.write_word = write_word

        def read_dword(a, ppc):
            return (read_word(a, ppc) << 32) | read_word((a + 4) & M32, ppc)

        def write_dword(a, v, ppc):
            write_word(a, (v >> 32) & M32, M32, ppc)
            write_word((a + 4) & M32, v & M32, M32, ppc)

        # ─── загрузки/сохранения ───
        def mk_lw(rs, rt, imm, signed):
            def f(pc):
                a = (R[rs] + imm) & M32
                if (a & 0xC0000000) == 0x80000000:
                    p = a & 0x1FFFFFFF
                    if p < rdsize:
                        v = RD[p >> 2]
                        R[rt] = (v - 0x100000000 if v & 0x80000000 else v) if signed else v
                        return pc + 4
                v = read_word(a, pc + 4)
                R[rt] = (v - 0x100000000 if v & 0x80000000 else v) if signed else v
                return pc + 4
            return f

        def mk_lb(rs, rt, imm, signed):
            def f(pc):
                a = (R[rs] + imm) & M32
                v = read_word(a & ~3, pc + 4) >> (24 - 8 * (a & 3)) & 0xFF
                R[rt] = v - 256 if (signed and v & 0x80) else v
                return pc + 4
            return f

        def mk_lh(rs, rt, imm, signed):
            def f(pc):
                a = (R[rs] + imm) & M32
                v = read_word(a & ~3, pc + 4) >> (16 - 8 * (a & 2)) & 0xFFFF
                R[rt] = v - 65536 if (signed and v & 0x8000) else v
                return pc + 4
            return f

        def mk_sw(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                if (a & 0xC0000000) == 0x80000000:
                    p = a & 0x1FFFFFFF
                    if p < rdsize:
                        RD[p >> 2] = R[rt] & M32
                        if m.code_pages[p >> 12]:
                            m.flush_icache()
                        return pc + 4
                write_word(a, R[rt] & M32, M32, pc + 4)
                return pc + 4
            return f

        def mk_sb(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                sh = 24 - 8 * (a & 3)
                write_word(a & ~3, (R[rt] & 0xFF) << sh, 0xFF << sh, pc + 4)
                return pc + 4
            return f

        def mk_sh(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                sh = 16 - 8 * (a & 2)
                write_word(a & ~3, (R[rt] & 0xFFFF) << sh, 0xFFFF << sh, pc + 4)
                return pc + 4
            return f

        def mk_lwl(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                w = read_word(a & ~3, pc + 4)
                k = (a & 3) * 8
                if k == 0:
                    R[rt] = s32(w)
                else:
                    R[rt] = s32((R[rt] & ((1 << k) - 1)) | (w << k))
                return pc + 4
            return f

        def mk_lwr(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                w = read_word(a & ~3, pc + 4)
                k = a & 3
                if k == 3:
                    R[rt] = s32(w)
                else:
                    n = 8 * (k + 1)
                    R[rt] = (R[rt] & ~((1 << n) - 1)) | (w >> (24 - 8 * k))
                return pc + 4
            return f

        def mk_swl(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                k = (a & 3) * 8
                write_word(a & ~3, (R[rt] & M32) >> k, M32 >> k, pc + 4)
                return pc + 4
            return f

        def mk_swr(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                k = a & 3
                sh = 24 - 8 * k
                write_word(a & ~3, ((R[rt] & M32) << sh) & M32, (M32 << sh) & M32, pc + 4)
                return pc + 4
            return f

        def mk_ld(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                R[rt] = s64(read_dword(a & ~7, pc + 4))
                return pc + 4
            return f

        def mk_sd(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                write_dword(a & ~7, R[rt] & M64, pc + 4)
                return pc + 4
            return f

        def mk_ldl(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                d = read_dword(a & ~7, pc + 4)
                k = (a & 7) * 8
                if k == 0:
                    R[rt] = s64(d)
                else:
                    R[rt] = s64((R[rt] & ((1 << k) - 1)) | (d << k))
                return pc + 4
            return f

        def mk_ldr(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                d = read_dword(a & ~7, pc + 4)
                k = a & 7
                if k == 7:
                    R[rt] = s64(d)
                else:
                    n = 8 * (k + 1)
                    R[rt] = s64((R[rt] & ~((1 << n) - 1)) | (d >> (64 - n)))
                return pc + 4
            return f

        def mk_sdl(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                k = (a & 7) * 8
                old = read_dword(a & ~7, pc + 4)
                keep = old & ~(M64 >> k) & M64
                write_dword(a & ~7, (((R[rt] & M64) >> k) | keep) & M64, pc + 4)
                return pc + 4
            return f

        def mk_sdr(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                k = a & 7
                sh = 56 - 8 * k
                old = read_dword(a & ~7, pc + 4)
                new = (((R[rt] & M64) << sh) & M64) | (old & ((1 << sh) - 1))
                write_dword(a & ~7, new, pc + 4)
                return pc + 4
            return f

        def mk_ll(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                R[rt] = s32(read_word(a, pc + 4))
                m.llbit = 1
                return pc + 4
            return f

        def mk_sc(rs, rt, imm):
            def f(pc):
                a = (R[rs] + imm) & M32
                if m.llbit:
                    m.llbit = 0
                    write_word(a, R[rt] & M32, M32, pc + 4)
                    R[rt] = 1
                else:
                    R[rt] = 0
                return pc + 4
            return f

        # ─── переходы ───
        do_jump = self.do_jump

        def idle_ok(pc, imm):
            if imm != -1:
                return False
            try:
                w, _ = m.fetch_word(pc + 4)
            except CE:
                return False
            return w == 0

        def with_idle(fn, pc, imm, cond_fn_for_idle):
            return fn

        def mk_branch(kind, rs, rt, imm, pc0, link, likely):
            tgt = (pc0 + (imm + 1) * 4) & M32
            idle = idle_ok(pc0, imm)

            if kind == 'beq':
                cond = lambda: R[rs] == R[rt]
            elif kind == 'bne':
                cond = lambda: R[rs] != R[rt]
            elif kind == 'blez':
                cond = lambda: R[rs] <= 0
            elif kind == 'bgtz':
                cond = lambda: R[rs] > 0
            elif kind == 'bltz':
                cond = lambda: R[rs] < 0
            elif kind == 'bgez':
                cond = lambda: R[rs] >= 0
            elif kind == 'bc1f':
                cond = lambda: (m.FCR31 & 0x800000) == 0
            else:
                cond = lambda: (m.FCR31 & 0x800000) != 0
            cop1 = kind in ('bc1f', 'bc1t')

            def f(pc):
                if cop1:
                    m.check_cop1()
                take = cond()
                if idle and take:
                    m.update_count(pc)
                    if m.cycle_count < 0:
                        c = cp0[CP0_COUNT]
                        cp0[CP0_COUNT] = (c - m.cycle_count) & M32
                        m.cycle_count = 0
                if link:
                    R[31] = s32(pc + 8)
                return do_jump(pc, take, tgt, likely)
            return f

        def mk_j(pc0, index, link):
            idle = (index == ((pc0 & 0x0FFFFFFF) >> 2) and (pc0 & 0x0FFFFFFF) != 0x0FFFFFFC and idle_ok(pc0, -1))

            def f(pc):
                tgt = ((index << 2) | ((pc + 4) & 0xF0000000)) & M32
                if idle:
                    m.update_count(pc)
                    if m.cycle_count < 0:
                        c = cp0[CP0_COUNT]
                        cp0[CP0_COUNT] = (c - m.cycle_count) & M32
                        m.cycle_count = 0
                if link:
                    R[31] = s32(pc + 8)
                return do_jump(pc, True, tgt, False)
            return f

        def mk_jr(rs, rd, link):
            def f(pc):
                tgt = R[rs] & M32
                if link and rd != 0:
                    R[rd] = s32(pc + 8)
                return do_jump(pc, True, tgt, False)
            return f

        # ─── ALU ───
        def mk_rtype(fn, rd, rs, rt, sa):
            if rd == 0:
                return NOP_()
            return fn(rd, rs, rt, sa)

        def A_sll(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32(R[rt] << sa)
                return pc + 4
            return f

        def A_srl(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32((R[rt] & M32) >> sa)
                return pc + 4
            return f

        def A_sra(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32(R[rt]) >> sa
                return pc + 4
            return f

        def A_sllv(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32(R[rt] << (R[rs] & 31))
                return pc + 4
            return f

        def A_srlv(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32((R[rt] & M32) >> (R[rs] & 31))
                return pc + 4
            return f

        def A_srav(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s32(R[rt]) >> (R[rs] & 31)
                return pc + 4
            return f

        def A_add(rd, rs, rt, sa):
            def f(pc):
                v = (R[rs] + R[rt]) & M32
                R[rd] = v - 0x100000000 if v & 0x80000000 else v
                return pc + 4
            return f

        def A_sub(rd, rs, rt, sa):
            def f(pc):
                v = (R[rs] - R[rt]) & M32
                R[rd] = v - 0x100000000 if v & 0x80000000 else v
                return pc + 4
            return f

        def A_and(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rs] & R[rt]
                return pc + 4
            return f

        def A_or(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rs] | R[rt]
                return pc + 4
            return f

        def A_xor(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rs] ^ R[rt]
                return pc + 4
            return f

        def A_nor(rd, rs, rt, sa):
            def f(pc):
                R[rd] = ~(R[rs] | R[rt])
                return pc + 4
            return f

        def A_slt(rd, rs, rt, sa):
            def f(pc):
                R[rd] = 1 if R[rs] < R[rt] else 0
                return pc + 4
            return f

        def A_sltu(rd, rs, rt, sa):
            def f(pc):
                R[rd] = 1 if (R[rs] & M64) < (R[rt] & M64) else 0
                return pc + 4
            return f

        def A_dadd(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64(R[rs] + R[rt])
                return pc + 4
            return f

        def A_dsub(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64(R[rs] - R[rt])
                return pc + 4
            return f

        def A_dsllv(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64(R[rt] << (R[rs] & 63))
                return pc + 4
            return f

        def A_dsrlv(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64((R[rt] & M64) >> (R[rs] & 63))
                return pc + 4
            return f

        def A_dsrav(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rt] >> (R[rs] & 63)
                return pc + 4
            return f

        def A_dsll(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64(R[rt] << sa)
                return pc + 4
            return f

        def A_dsrl(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64((R[rt] & M64) >> sa)
                return pc + 4
            return f

        def A_dsra(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rt] >> sa
                return pc + 4
            return f

        def A_dsll32(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64(R[rt] << (32 + sa))
                return pc + 4
            return f

        def A_dsrl32(rd, rs, rt, sa):
            def f(pc):
                R[rd] = s64((R[rt] & M64) >> (32 + sa))
                return pc + 4
            return f

        def A_dsra32(rd, rs, rt, sa):
            def f(pc):
                R[rd] = R[rt] >> (32 + sa)
                return pc + 4
            return f

        def A_mfhi(rd, rs, rt, sa):
            def f(pc):
                R[rd] = m.hi
                return pc + 4
            return f

        def A_mflo(rd, rs, rt, sa):
            def f(pc):
                R[rd] = m.lo
                return pc + 4
            return f

        SPECIAL_RD = {0: A_sll, 2: A_srl, 3: A_sra, 4: A_sllv, 6: A_srlv, 7: A_srav,
                      16: A_mfhi, 18: A_mflo, 20: A_dsllv, 22: A_dsrlv, 23: A_dsrav,
                      32: A_add, 33: A_add, 34: A_sub, 35: A_sub, 36: A_and, 37: A_or, 38: A_xor, 39: A_nor,
                      42: A_slt, 43: A_sltu, 44: A_dadd, 45: A_dadd, 46: A_dsub, 47: A_dsub,
                      56: A_dsll, 58: A_dsrl, 59: A_dsra, 60: A_dsll32, 62: A_dsrl32, 63: A_dsra32}

        def mk_mult(kind, rs, rt):
            def f(pc):
                a, b = R[rs], R[rt]
                if kind == 24:
                    t = s64(a * b)
                    m.hi = t >> 32
                    m.lo = s32(t)
                elif kind == 25:
                    t = (a & M32) * (b & M32)
                    m.hi = s64(t) >> 32
                    m.lo = s32(t)
                elif kind == 26:
                    d = s32(b)
                    if d:
                        n = s32(a)
                        q = abs(n) // abs(d)
                        if (n < 0) != (d < 0):
                            q = -q
                        r = n - q * d
                        m.lo = s32(q)
                        m.hi = s32(r)
                elif kind == 27:
                    d = b & M32
                    if d:
                        n = a & M32
                        m.lo = s32(n // d)
                        m.hi = s32(n % d)
                elif kind == 28:
                    t = a * b
                    m.lo = s64(t)
                    m.hi = s64(t >> 64)
                elif kind == 29:
                    t = (a & M64) * (b & M64)
                    m.lo = s64(t)
                    m.hi = s64(t >> 64)
                elif kind == 30:
                    if b:
                        q = abs(a) // abs(b)
                        if (a < 0) != (b < 0):
                            q = -q
                        m.lo = s64(q)
                        m.hi = s64(a - q * b)
                else:
                    ub = b & M64
                    if ub:
                        ua = a & M64
                        m.lo = s64(ua // ub)
                        m.hi = s64(ua % ub)
                return pc + 4
            return f

        def mk_mthi(rs):
            def f(pc):
                m.hi = R[rs]
                return pc + 4
            return f

        def mk_mtlo(rs):
            def f(pc):
                m.lo = R[rs]
                return pc + 4
            return f

        def mk_syscall():
            def f(pc):
                raise CE('general', 0, 0, 8 << 2)
            return f

        def mk_break():
            def f(pc):
                cp0[CP0_COUNT] = m.next_interupt & M32
                m.cycle_count = 0
                return pc + 4
            return f

        def mk_teq(rs, rt):
            def f(pc):
                if R[rs] == R[rt]:
                    raise Stop('trap exception in TEQ @ %08x' % pc)
                return pc + 4
            return f

        # ─── непосредственные ───
        def mk_addiu(rs, rt, imm):
            def f(pc):
                v = (R[rs] + imm) & M32
                R[rt] = v - 0x100000000 if v & 0x80000000 else v
                return pc + 4
            return f

        def mk_slti(rs, rt, imm):
            def f(pc):
                R[rt] = 1 if R[rs] < imm else 0
                return pc + 4
            return f

        def mk_sltiu(rs, rt, imm):
            uimm = imm & M64

            def f(pc):
                R[rt] = 1 if (R[rs] & M64) < uimm else 0
                return pc + 4
            return f

        def mk_andi(rs, rt, imm):
            u = imm & 0xFFFF

            def f(pc):
                R[rt] = R[rs] & u
                return pc + 4
            return f

        def mk_ori(rs, rt, imm):
            u = imm & 0xFFFF

            def f(pc):
                R[rt] = R[rs] | u
                return pc + 4
            return f

        def mk_xori(rs, rt, imm):
            u = imm & 0xFFFF

            def f(pc):
                R[rt] = R[rs] ^ u
                return pc + 4
            return f

        def mk_lui(rt, imm):
            val = s32(imm << 16)

            def f(pc):
                R[rt] = val
                return pc + 4
            return f

        def mk_daddiu(rs, rt, imm):
            def f(pc):
                R[rt] = s64(R[rs] + imm)
                return pc + 4
            return f

        self.mk = dict(
            NI=NI, NOP=NOP_, lw=mk_lw, lb=mk_lb, lh=mk_lh, sw=mk_sw, sb=mk_sb, sh=mk_sh, lwl=mk_lwl, lwr=mk_lwr,
            swl=mk_swl, swr=mk_swr, ld=mk_ld, sd=mk_sd, ldl=mk_ldl, ldr=mk_ldr, sdl=mk_sdl, sdr=mk_sdr,
            ll=mk_ll, sc=mk_sc, branch=mk_branch, j=mk_j, jr=mk_jr, rtype=mk_rtype, special_rd=SPECIAL_RD,
            mult=mk_mult, mthi=mk_mthi, mtlo=mk_mtlo, syscall=mk_syscall, brk=mk_break, teq=mk_teq,
            addiu=mk_addiu, slti=mk_slti, sltiu=mk_sltiu, andi=mk_andi, ori=mk_ori, xori=mk_xori, lui=mk_lui,
            daddiu=mk_daddiu, read_word=read_word, write_word=write_word, read_dword=read_dword,
            write_dword=write_dword)
        self._build_cop()

    def make_insn(self, op, pc):
        k = self.mk
        opc = op >> 26
        rs = (op >> 21) & 31
        rt = (op >> 16) & 31
        rd = (op >> 11) & 31
        sa = (op >> 6) & 31
        imm = op & 0xFFFF
        if imm & 0x8000:
            imm -= 0x10000
        if opc == 0:
            fn = op & 63
            if fn in k['special_rd']:
                return k['rtype'](k['special_rd'][fn], rd, rs, rt, sa)
            if fn == 8:
                return k['jr'](rs, 0, False)
            if fn == 9:
                return k['jr'](rs, rd, True)
            if fn == 12:
                return k['syscall']()
            if fn == 13:
                return k['brk']()
            if fn == 15:
                return k['NOP']()
            if fn == 17:
                return k['mthi'](rs)
            if fn == 19:
                return k['mtlo'](rs)
            if 24 <= fn <= 31:
                return k['mult'](fn, rs, rt)
            if fn == 52:
                return k['teq'](rs, rt)
            if fn in (48, 49, 50, 51, 54):
                return k['NI']('SPECIAL %d' % fn)
            return k['NI']('RESERVED SPECIAL %d' % fn)
        if opc == 1:
            r = rt
            if r in (0, 1, 2, 3, 16, 17, 18, 19):
                kind = 'bltz' if r in (0, 2, 16, 18) else 'bgez'
                likely = r in (2, 3, 18, 19)
                link = r >= 16
                return k['branch'](kind, rs, 0, imm, pc, link, likely)
            return k['NI']('REGIMM %d' % r)
        if opc == 2:
            return k['j'](pc, op & 0x3FFFFFF, False)
        if opc == 3:
            return k['j'](pc, op & 0x3FFFFFF, True)
        if opc in (4, 5, 6, 7, 20, 21, 22, 23):
            kind = {4: 'beq', 5: 'bne', 6: 'blez', 7: 'bgtz', 20: 'beq', 21: 'bne', 22: 'blez', 23: 'bgtz'}[opc]
            return k['branch'](kind, rs, rt, imm, pc, False, opc >= 20)
        if opc in (8, 9):
            return k['NOP']() if rt == 0 else k['addiu'](rs, rt, imm)
        if opc == 10:
            return k['NOP']() if rt == 0 else k['slti'](rs, rt, imm)
        if opc == 11:
            return k['NOP']() if rt == 0 else k['sltiu'](rs, rt, imm)
        if opc == 12:
            return k['NOP']() if rt == 0 else k['andi'](rs, rt, imm)
        if opc == 13:
            return k['NOP']() if rt == 0 else k['ori'](rs, rt, imm)
        if opc == 14:
            return k['NOP']() if rt == 0 else k['xori'](rs, rt, imm)
        if opc == 15:
            return k['NOP']() if rt == 0 else k['lui'](rt, imm)
        if opc == 16:
            return self.make_cop0(op, rs, rt, rd, pc)
        if opc == 17:
            return self.make_cop1(op, rs, rt, rd, sa, imm, pc)
        if opc in (24, 25):
            return k['NOP']() if rt == 0 else k['daddiu'](rs, rt, imm)
        # загрузки
        loads = {26: 'ldl', 27: 'ldr', 34: 'lwl', 38: 'lwr', 55: 'ld', 48: 'll'}
        if opc in (32, 33, 35, 36, 37, 39):
            if rt == 0:
                return k['NOP']()
            if opc == 35:
                return k['lw'](rs, rt, imm, True)
            if opc == 39:
                return k['lw'](rs, rt, imm, False)
            if opc == 32:
                return k['lb'](rs, rt, imm, True)
            if opc == 36:
                return k['lb'](rs, rt, imm, False)
            if opc == 33:
                return k['lh'](rs, rt, imm, True)
            return k['lh'](rs, rt, imm, False)
        if opc in loads:
            return k['NOP']() if rt == 0 else k[loads[opc]](rs, rt, imm)
        stores = {40: 'sb', 41: 'sh', 43: 'sw', 42: 'swl', 46: 'swr', 44: 'sdl', 45: 'sdr', 63: 'sd'}
        if opc in stores:
            return k[stores[opc]](rs, rt, imm)
        if opc == 56:
            return k['NOP']() if rt == 0 else k['sc'](rs, rt, imm)
        if opc == 47:
            return k['NOP']()
        if opc in (49, 53, 57, 61):
            return self.make_cop1_mem(opc, rs, rt, imm)
        return k['NI']('opcode %d' % opc)

    # ═══════════ COP0 / TLB ═══════════
    def translate_event_queue(self, base):
        self.remove_event(COMPARE_INT)
        self.remove_event(SPECIAL_INT)
        c = self.cp0
        for e in self.queue:
            e[1] = ((e[1] - c[CP0_COUNT]) + base) & M32
        c[CP0_COUNT] = base & M32
        self.add_event_count(SPECIAL_INT, (c[CP0_COUNT] & 0x80000000) ^ 0x80000000)
        c[CP0_COUNT] = (c[CP0_COUNT] + self.count_per_op) & M32
        self.cycle_count += self.count_per_op
        self.add_event_count(COMPARE_INT, c[CP0_COMPARE])
        c[CP0_COUNT] = (c[CP0_COUNT] - self.count_per_op) & M32
        self.cycle_count = s32(c[CP0_COUNT] - self.queue[0][1])

    def tlb_write(self, idx):
        e = self.tlb_e[idx]
        self._tlb_unmap(e)
        c = self.cp0
        lo0, lo1, hi = c[CP0_ENTRYLO0], c[CP0_ENTRYLO1], c[CP0_ENTRYHI]
        e['g'] = lo0 & lo1 & 1
        e['pfn_even'] = (lo0 & 0x3FFFFFC0) >> 6
        e['pfn_odd'] = (lo1 & 0x3FFFFFC0) >> 6
        e['c_even'] = (lo0 & 0x38) >> 3
        e['c_odd'] = (lo1 & 0x38) >> 3
        e['d_even'] = (lo0 & 4) >> 2
        e['d_odd'] = (lo1 & 4) >> 2
        e['v_even'] = (lo0 & 2) >> 1
        e['v_odd'] = (lo1 & 2) >> 1
        e['asid'] = hi & 0xFF
        e['vpn2'] = (hi & 0xFFFFE000) >> 13
        e['mask'] = (c[CP0_PAGEMASK] & 0x1FFE000) >> 13
        self._tlb_fill(e)
        self._tlb_map(e)
        self.flush_icache()

    def make_cop0(self, op, rs, rt, rd, pc0):
        m = self
        R = self.R
        c = self.cp0
        k = self.mk
        if rs == 0:                                   # MFC0
            if rt == 0:
                return k['NOP']()

            def f(pc):
                if rd == CP0_COUNT:
                    m.update_count(pc)
                R[rt] = s32(c[rd])
                return pc + 4
            return f
        if rs == 4:                                   # MTC0
            return self._mk_mtc0(rd, rt)
        if rs == 16:
            fn = op & 63
            if fn == 1:                               # TLBR
                def f(pc):
                    i = c[CP0_INDEX] & 0x1F
                    e = m.tlb_e[i]
                    c[CP0_PAGEMASK] = e['mask'] << 13
                    c[CP0_ENTRYHI] = (e['vpn2'] << 13) | e['asid']
                    c[CP0_ENTRYLO0] = (e['pfn_even'] << 6) | (e['c_even'] << 3) | (e['d_even'] << 2) | (e['v_even'] << 1) | e['g']
                    c[CP0_ENTRYLO1] = (e['pfn_odd'] << 6) | (e['c_odd'] << 3) | (e['d_odd'] << 2) | (e['v_odd'] << 1) | e['g']
                    return pc + 4
                return f
            if fn == 2:
                def f(pc):
                    m.tlb_write(c[CP0_INDEX] & 0x3F)
                    return pc + 4
                return f
            if fn == 6:
                def f(pc):
                    m.update_count(pc)
                    c[CP0_RANDOM] = (c[CP0_COUNT] // 2 % (32 - c[CP0_WIRED])) + c[CP0_WIRED]
                    m.tlb_write(c[CP0_RANDOM])
                    return pc + 4
                return f
            if fn == 8:
                def f(pc):
                    c[CP0_INDEX] |= 0x80000000
                    for i, e in enumerate(m.tlb_e):
                        if ((e['vpn2'] & ~e['mask']) == ((((c[CP0_ENTRYHI] & 0xFFFFE000) >> 13) & ~e['mask']))) and \
                                (e['g'] or e['asid'] == (c[CP0_ENTRYHI] & 0xFF)):
                            c[CP0_INDEX] = i
                            break
                    return pc + 4
                return f
            if fn == 24:                              # ERET
                def f(pc):
                    m.update_count(pc)
                    if c[CP0_STATUS] & 4:
                        raise Stop('error in ERET')
                    c[CP0_STATUS] &= ~2
                    npc = c[CP0_EPC]
                    m.llbit = 0
                    m.check_interupt(npc)
                    m.last_addr = npc
                    if m.cycle_count >= 0:
                        npc = m.gen_interupt(npc)
                    return npc
                return f
        return k['NI']('COP0 %08x' % op)

    def _mk_mtc0(self, rd, rt):
        m = self
        R = self.R
        c = self.cp0

        def f(pc):
            v = R[rt] & M32
            if rd == CP0_INDEX:
                c[CP0_INDEX] = v & 0x8000003F
            elif rd == CP0_RANDOM:
                pass
            elif rd == CP0_ENTRYLO0:
                c[CP0_ENTRYLO0] = v & 0x3FFFFFFF
            elif rd == CP0_ENTRYLO1:
                c[CP0_ENTRYLO1] = v & 0x3FFFFFFF
            elif rd == CP0_CONTEXT:
                c[CP0_CONTEXT] = (v & 0xFF800000) | (c[CP0_CONTEXT] & 0x007FFFF0)
            elif rd == CP0_PAGEMASK:
                c[CP0_PAGEMASK] = v & 0x01FFE000
            elif rd == CP0_WIRED:
                c[CP0_WIRED] = v
                c[CP0_RANDOM] = 31
            elif rd == CP0_BADVADDR:
                pass
            elif rd == CP0_COUNT:
                m.update_count(pc)
                m.interupt_unsafe_state = 1
                if m.cycle_count >= 0:
                    m.gen_interupt(pc)
                m.interupt_unsafe_state = 0
                m.translate_event_queue(v)
                c[CP0_COUNT] = v
            elif rd == CP0_ENTRYHI:
                c[CP0_ENTRYHI] = v & 0xFFFFE0FF
            elif rd == CP0_COMPARE:
                m.update_count(pc)
                m.remove_event(COMPARE_INT)
                c[CP0_COUNT] = (c[CP0_COUNT] + m.count_per_op) & M32
                m.cycle_count += m.count_per_op
                m.add_event_count(COMPARE_INT, v)
                c[CP0_COUNT] = (c[CP0_COUNT] - m.count_per_op) & M32
                m.cycle_count = s32(c[CP0_COUNT] - m.queue[0][1])
                c[CP0_COMPARE] = v
                c[CP0_CAUSE] &= 0xFFFF7FFF
            elif rd == CP0_STATUS:
                old = c[CP0_STATUS]
                if (v & 0x04000000) != (old & 0x04000000):
                    m.shuffle_fpr(old, v)
                c[CP0_STATUS] = v
                m.update_count(pc)
                m.check_interupt(pc + 4)
                m.interupt_unsafe_state = 1
                npc = pc + 4
                if m.cycle_count >= 0:
                    npc = m.gen_interupt(npc)
                m.interupt_unsafe_state = 0
                return npc
            elif rd == CP0_CAUSE:
                if v != 0:
                    raise Stop('MTC0 Cause != 0')
                c[CP0_CAUSE] = 0
            elif rd == CP0_EPC:
                c[CP0_EPC] = v
            elif rd == CP0_PREVID:
                pass
            elif rd == CP0_CONFIG:
                c[CP0_CONFIG] = v
            elif rd == CP0_WATCHLO:
                c[CP0_WATCHLO] = v
            elif rd == CP0_WATCHHI:
                c[CP0_WATCHHI] = v
            elif rd == CP0_TAGLO:
                c[CP0_TAGLO] = v & 0x0FFFFFC0
            elif rd == CP0_TAGHI:
                c[CP0_TAGHI] = 0
            else:
                raise Stop('Unknown MTC0 write %d' % rd)
            return pc + 4
        return f

    # ═══════════ COP1 ═══════════
    def _build_cop(self):
        pass

    def make_cop1_mem(self, opc, rs, rt, imm):
        m = self
        R = self.R

        if opc == 49:                              # LWC1
            def f(pc):
                m.check_cop1()
                a = (R[rs] + imm) & M32
                m.f_set_sbits(rt, m.read_word(a, pc + 4))
                return pc + 4
        elif opc == 57:                            # SWC1
            def f(pc):
                m.check_cop1()
                a = (R[rs] + imm) & M32
                m.write_word(a, m.f_get_sbits(rt), M32, pc + 4)
                return pc + 4
        elif opc == 53:                            # LDC1
            def f(pc):
                m.check_cop1()
                a = (R[rs] + imm) & M32
                m.f_set_dbits(rt, m.mk['read_dword'](a & ~7, pc + 4))
                return pc + 4
        else:                                      # SDC1
            def f(pc):
                m.check_cop1()
                a = (R[rs] + imm) & M32
                m.mk['write_dword'](a & ~7, m.f_get_dbits(rt), pc + 4)
                return pc + 4
        return f

    def make_cop1(self, op, rs, rt, rd, sa, imm, pc0):
        m = self
        R = self.R
        k = self.mk
        fs = rd
        ft = rt
        fd = sa
        sub = rs
        if sub == 0:                               # MFC1
            if rt == 0:
                return k['NOP']()

            def f(pc):
                m.check_cop1()
                R[rt] = s32(m.f_get_sbits(fs))
                return pc + 4
            return f
        if sub == 1:                               # DMFC1
            if rt == 0:
                return k['NOP']()

            def f(pc):
                m.check_cop1()
                R[rt] = s64(m.f_get_dbits(fs))
                return pc + 4
            return f
        if sub == 2:                               # CFC1
            if rt == 0:
                return k['NOP']()

            def f(pc):
                m.check_cop1()
                if fs == 31:
                    R[rt] = s32(m.FCR31)
                elif fs == 0:
                    R[rt] = s32(m.FCR0)
                return pc + 4
            return f
        if sub == 4:                               # MTC1
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fs, R[rt] & M32)
                return pc + 4
            return f
        if sub == 5:                               # DMTC1
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fs, R[rt] & M64)
                return pc + 4
            return f
        if sub == 6:                               # CTC1
            def f(pc):
                m.check_cop1()
                if fs == 31:
                    m.FCR31 = s32(R[rt])
                return pc + 4
            return f
        if sub == 8:                               # BC1x
            t = rt & 3
            kind = 'bc1f' if t in (0, 2) else 'bc1t'
            return k['branch'](kind, 0, 0, imm, pc0, False, t >= 2)
        fn = op & 63
        if sub == 16:
            return self._fp_s(fn, fs, ft, fd)
        if sub == 17:
            return self._fp_d(fn, fs, ft, fd)
        if sub == 20:
            return self._fp_w(fn, fs, fd)
        if sub == 21:
            return self._fp_l(fn, fs, fd)
        return k['NI']('COP1 %08x' % op)

    # ─── FPU: вспомогательное ───
    @staticmethod
    def _round_half_away(x):
        return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)

    def _to_int(self, x, bits, mode):
        """(int)round/trunc/ceil/floor(x) как на x86 (недопустимое значение -> INT_MIN)."""
        lim = 1 << (bits - 1)
        try:
            if x != x or math.isinf(x):
                return -lim
            if mode == 0:
                v = int(self._round_half_away(x))
            elif mode == 1:
                v = int(x)
            elif mode == 2:
                v = int(math.ceil(x))
            else:
                v = int(math.floor(x))
        except (OverflowError, ValueError):
            return -lim
        if v >= lim or v < -lim:
            return -lim
        return v

    def _fp_cmp(self, fn, a, b):
        """c.cond.fmt: обновляет FCR31 бит 23."""
        nan = (a != a) or (b != b)
        if fn in (56, 57, 58, 59, 60, 61, 62, 63) and nan:
            raise Stop('Invalid operation exception in C opcode')
        if fn == 48 or fn == 56 or fn == 57:
            r = False
        elif fn == 49:
            r = nan
        elif fn == 50:
            r = (not nan) and a == b
        elif fn == 51:
            r = nan or a == b
        elif fn == 52:
            r = (not nan) and a < b
        elif fn == 53:
            r = nan or a < b
        elif fn == 54:
            r = (not nan) and a <= b
        elif fn == 55:
            r = nan or a <= b
        elif fn in (58, 59):
            r = a == b
        elif fn in (60, 61):
            r = a < b
        else:
            r = a <= b
        if r:
            self.FCR31 |= 0x800000
        else:
            self.FCR31 &= ~0x800000

    @staticmethod
    def _fdiv(a, b):
        try:
            return a / b
        except ZeroDivisionError:
            if a != a or a == 0:
                return math.nan
            neg = (math.copysign(1.0, a) < 0) != (math.copysign(1.0, b) < 0)
            return -math.inf if neg else math.inf

    @staticmethod
    def _fsqrt(a):
        return math.sqrt(a) if a >= 0 else math.nan

    def _fp_s(self, fn, fs, ft, fd):
        m = self
        k = self.mk
        if fn in (0, 1, 2, 3):
            def f(pc):
                m.check_cop1()
                a = m.f_get_s(fs)
                b = m.f_get_s(ft)
                if fn == 0:
                    r = a + b
                elif fn == 1:
                    r = a - b
                elif fn == 2:
                    r = a * b
                else:
                    r = m._fdiv(a, b)
                m.f_set_sbits(fd, f_to_bits(r))
                return pc + 4
            return f
        if fn == 4:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, f_to_bits(m._fsqrt(m.f_get_s(fs))))
                return pc + 4
            return f
        if fn == 5:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, m.f_get_sbits(fs) & 0x7FFFFFFF)
                return pc + 4
            return f
        if fn == 6:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, m.f_get_sbits(fs))
                return pc + 4
            return f
        if fn == 7:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, m.f_get_sbits(fs) ^ 0x80000000)
                return pc + 4
            return f
        if 8 <= fn <= 15:
            mode = (fn - 8) & 3
            wide = fn < 12

            def f(pc):
                m.check_cop1()
                x = m.f_get_s(fs)
                if wide:
                    m.f_set_dbits(fd, m._to_int(x, 64, mode) & M64)
                else:
                    m.f_set_sbits(fd, m._to_int(x, 32, mode) & M32)
                return pc + 4
            return f
        if fn == 33:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, d_to_bits(m.f_get_s(fs)))
                return pc + 4
            return f
        if fn in (36, 37):
            def f(pc):
                m.check_cop1()
                x = m.f_get_s(fs)
                mode = m.FCR31 & 3
                if fn == 36:
                    m.f_set_sbits(fd, m._to_int(x, 32, mode) & M32)
                else:
                    m.f_set_dbits(fd, m._to_int(x, 64, mode) & M64)
                return pc + 4
            return f
        if 48 <= fn <= 63:
            def f(pc):
                m.check_cop1()
                m._fp_cmp(fn, m.f_get_s(fs), m.f_get_s(ft))
                return pc + 4
            return f
        return k['NI']('COP1.S %d' % fn)

    def _fp_d(self, fn, fs, ft, fd):
        m = self
        k = self.mk
        if fn in (0, 1, 2, 3):
            def f(pc):
                m.check_cop1()
                a = bits_to_d(m.f_get_dbits(fs))
                b = bits_to_d(m.f_get_dbits(ft))
                if fn == 0:
                    r = a + b
                elif fn == 1:
                    r = a - b
                elif fn == 2:
                    r = a * b
                else:
                    r = m._fdiv(a, b)
                m.f_set_dbits(fd, d_to_bits(r))
                return pc + 4
            return f
        if fn == 4:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, d_to_bits(m._fsqrt(bits_to_d(m.f_get_dbits(fs)))))
                return pc + 4
            return f
        if fn == 5:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, m.f_get_dbits(fs) & 0x7FFFFFFFFFFFFFFF)
                return pc + 4
            return f
        if fn == 6:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, m.f_get_dbits(fs))
                return pc + 4
            return f
        if fn == 7:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, m.f_get_dbits(fs) ^ 0x8000000000000000)
                return pc + 4
            return f
        if 8 <= fn <= 15:
            mode = (fn - 8) & 3
            wide = fn < 12

            def f(pc):
                m.check_cop1()
                x = bits_to_d(m.f_get_dbits(fs))
                if wide:
                    m.f_set_dbits(fd, m._to_int(x, 64, mode) & M64)
                else:
                    m.f_set_sbits(fd, m._to_int(x, 32, mode) & M32)
                return pc + 4
            return f
        if fn == 32:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, f_to_bits(bits_to_d(m.f_get_dbits(fs))))
                return pc + 4
            return f
        if fn in (36, 37):
            def f(pc):
                m.check_cop1()
                x = bits_to_d(m.f_get_dbits(fs))
                mode = m.FCR31 & 3
                if fn == 36:
                    m.f_set_sbits(fd, m._to_int(x, 32, mode) & M32)
                else:
                    m.f_set_dbits(fd, m._to_int(x, 64, mode) & M64)
                return pc + 4
            return f
        if 48 <= fn <= 63:
            def f(pc):
                m.check_cop1()
                m._fp_cmp(fn, bits_to_d(m.f_get_dbits(fs)), bits_to_d(m.f_get_dbits(ft)))
                return pc + 4
            return f
        return k['NI']('COP1.D %d' % fn)

    def _fp_w(self, fn, fs, fd):
        m = self
        if fn == 32:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, f_to_bits(float(s32(m.f_get_sbits(fs)))))
                return pc + 4
            return f
        if fn == 33:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, d_to_bits(float(s32(m.f_get_sbits(fs)))))
                return pc + 4
            return f
        return self.mk['NI']('COP1.W %d' % fn)

    def _fp_l(self, fn, fs, fd):
        m = self
        if fn == 32:
            def f(pc):
                m.check_cop1()
                m.f_set_sbits(fd, f_to_bits(float(s64(m.f_get_dbits(fs)))))
                return pc + 4
            return f
        if fn == 33:
            def f(pc):
                m.check_cop1()
                m.f_set_dbits(fd, d_to_bits(float(s64(m.f_get_dbits(fs)))))
                return pc + 4
            return f
        return self.mk['NI']('COP1.L %d' % fn)


# ═══════════════════════════════════════════════════════════════
# HLE аудио (ABI1) с трассировкой, как rsp_hle lazyusf2 + usftrace.patch
# ═══════════════════════════════════════════════════════════════

try:
    import numpy as np
except ImportError:                                  # pragma: no cover
    np = None

A_INIT, A_LOOP, A_LEFT, A_VOL, A_AUX = 0x01, 0x02, 0x02, 0x04, 0x08

RESAMPLE_LUT = [
    0x0c39, 0x66ad, 0x0d46, 0xffdf, 0x0b39, 0x6696, 0x0e5f, 0xffd8, 0x0a44, 0x6669, 0x0f83, 0xffd0, 0x095a, 0x6626, 0x10b4, 0xffc8,
    0x087d, 0x65cd, 0x11f0, 0xffbf, 0x07ab, 0x655e, 0x1338, 0xffb6, 0x06e4, 0x64d9, 0x148c, 0xffac, 0x0628, 0x643f, 0x15eb, 0xffa1,
    0x0577, 0x638f, 0x1756, 0xff96, 0x04d1, 0x62cb, 0x18cb, 0xff8a, 0x0435, 0x61f3, 0x1a4c, 0xff7e, 0x03a4, 0x6106, 0x1bd7, 0xff71,
    0x031c, 0x6007, 0x1d6c, 0xff64, 0x029f, 0x5ef5, 0x1f0b, 0xff56, 0x022a, 0x5dd0, 0x20b3, 0xff48, 0x01be, 0x5c9a, 0x2264, 0xff3a,
    0x015b, 0x5b53, 0x241e, 0xff2c, 0x0101, 0x59fc, 0x25e0, 0xff1e, 0x00ae, 0x5896, 0x27a9, 0xff10, 0x0063, 0x5720, 0x297a, 0xff02,
    0x001f, 0x559d, 0x2b50, 0xfef4, 0xffe2, 0x540d, 0x2d2c, 0xfee8, 0xffac, 0x5270, 0x2f0d, 0xfedb, 0xff7c, 0x50c7, 0x30f3, 0xfed0,
    0xff53, 0x4f14, 0x32dc, 0xfec6, 0xff2e, 0x4d57, 0x34c8, 0xfebd, 0xff0f, 0x4b91, 0x36b6, 0xfeb6, 0xfef5, 0x49c2, 0x38a5, 0xfeb0,
    0xfedf, 0x47ed, 0x3a95, 0xfeac, 0xfece, 0x4611, 0x3c85, 0xfeab, 0xfec0, 0x4430, 0x3e74, 0xfeac, 0xfeb6, 0x424a, 0x4060, 0xfeaf,
    0xfeaf, 0x4060, 0x424a, 0xfeb6, 0xfeac, 0x3e74, 0x4430, 0xfec0, 0xfeab, 0x3c85, 0x4611, 0xfece, 0xfeac, 0x3a95, 0x47ed, 0xfedf,
    0xfeb0, 0x38a5, 0x49c2, 0xfef5, 0xfeb6, 0x36b6, 0x4b91, 0xff0f, 0xfebd, 0x34c8, 0x4d57, 0xff2e, 0xfec6, 0x32dc, 0x4f14, 0xff53,
    0xfed0, 0x30f3, 0x50c7, 0xff7c, 0xfedb, 0x2f0d, 0x5270, 0xffac, 0xfee8, 0x2d2c, 0x540d, 0xffe2, 0xfef4, 0x2b50, 0x559d, 0x001f,
    0xff02, 0x297a, 0x5720, 0x0063, 0xff10, 0x27a9, 0x5896, 0x00ae, 0xff1e, 0x25e0, 0x59fc, 0x0101, 0xff2c, 0x241e, 0x5b53, 0x015b,
    0xff3a, 0x2264, 0x5c9a, 0x01be, 0xff48, 0x20b3, 0x5dd0, 0x022a, 0xff56, 0x1f0b, 0x5ef5, 0x029f, 0xff64, 0x1d6c, 0x6007, 0x031c,
    0xff71, 0x1bd7, 0x6106, 0x03a4, 0xff7e, 0x1a4c, 0x61f3, 0x0435, 0xff8a, 0x18cb, 0x62cb, 0x04d1, 0xff96, 0x1756, 0x638f, 0x0577,
    0xffa1, 0x15eb, 0x643f, 0x0628, 0xffac, 0x148c, 0x64d9, 0x06e4, 0xffb6, 0x1338, 0x655e, 0x07ab, 0xffbf, 0x11f0, 0x65cd, 0x087d,
    0xffc8, 0x10b4, 0x6626, 0x095a, 0xffd0, 0x0f83, 0x6669, 0x0a44, 0xffd8, 0x0e5f, 0x6696, 0x0b39, 0xffdf, 0x0d46, 0x66ad, 0x0c39,
]


def _s16(x):
    return ((x & 0xFFFF) ^ 0x8000) - 0x8000


def _clamp16(x):
    return -32768 if x < -32768 else (32767 if x > 32767 else x)


def _align(x, a):
    return (x + a - 1) & ~(a - 1)


class AlistExtra:
    """Команды аудио-микрокодов ABI2 ("nead") и ABI3 ("naudio") + общие примитивы alist.c
    (порт rsp_hle lazyusf2: alist.c, alist_naudio.c, alist_nead.c).  Адресация DMEM - логическая
    (big-endian), как в остальном Hle."""

    # ---------- общие примитивы ----------
    def a_clear(self, dmem, count):
        buf = self.buf
        for i in range(count):
            buf[(dmem + i) & 0xFFF] = 0

    def a_load(self, dmem, address, count):
        dmem &= ~3 & 0xFFF
        address &= ~7
        n = min(_align(count, 8), 0x1000 - dmem)
        if n > 0:
            self.buf[dmem:dmem + n] = self.m.dram_read_bytes(address & 0xFFFFFF, n)

    def a_save(self, dmem, address, count):
        dmem &= ~3 & 0xFFF
        address &= ~7
        n = min(_align(count, 8), 0x1000 - dmem)
        if n > 0:
            self.m.dram_write_bytes(address & 0xFFFFFF, bytes(self.buf[dmem:dmem + n]))

    def a_move(self, dmemo, dmemi, count):
        buf = self.buf
        for i in range(count):
            buf[(dmemo + i) & 0xFFF] = buf[(dmemi + i) & 0xFFF]

    def a_copy_every_other(self, dmemo, dmemi, count):
        s = self.s
        for _ in range(count):
            s[(dmemo >> 1) & 0x7FF] = s[(dmemi >> 1) & 0x7FF]
            dmemo += 2
            dmemi += 4

    def a_repeat64(self, dmemo, dmemi, count):
        chunk = bytes(self.buf[(dmemi + i) & 0xFFF] for i in range(128))
        for _ in range(count):
            for i in range(128):
                self.buf[(dmemo + i) & 0xFFF] = chunk[i]
            dmemo += 128

    def a_copy_blocks(self, dmemo, dmemi, block_size, count):
        buf = self.buf
        block_left = count
        while True:
            bytes_left = block_size
            while True:
                chunk = bytes(buf[(dmemi + i) & 0xFFF] for i in range(0x20))
                for i in range(0x20):
                    buf[(dmemo + i) & 0xFFF] = chunk[i]
                bytes_left -= 0x20
                dmemi += 0x20
                dmemo += 0x20
                if bytes_left <= 0:
                    break
            block_left -= 1
            if block_left <= 0:
                break

    def a_interleave(self, dmemo, left, right, count):
        n = count >> 2
        if n <= 0:
            return
        s = self.s
        L = s[((left >> 1) + np.arange(n * 2)) & 0x7FF].copy()
        Rr = s[((right >> 1) + np.arange(n * 2)) & 0x7FF].copy()
        out = np.empty(n * 4, dtype=np.int16)
        out[0::2] = L
        out[1::2] = Rr
        s[((dmemo >> 1) + np.arange(n * 4)) & 0x7FF] = out

    def a_mix(self, dmemo, dmemi, count, gain):
        n = count >> 1
        if n <= 0:
            return
        s = self.s
        src = s[((dmemi >> 1) + np.arange(n)) & 0x7FF].astype(np.int64)
        idx = ((dmemo >> 1) + np.arange(n)) & 0x7FF
        cur = s[idx].astype(np.int64)
        s[idx] = np.clip(cur + ((src * gain) >> 15), -32768, 32767)

    def a_add(self, dmemo, dmemi, count):
        n = count >> 1
        if n <= 0:
            return
        s = self.s
        src = s[((dmemi >> 1) + np.arange(n)) & 0x7FF].astype(np.int64)
        idx = ((dmemo >> 1) + np.arange(n)) & 0x7FF
        cur = s[idx].astype(np.int64)
        s[idx] = np.clip(cur + src, -32768, 32767)

    def a_multq44(self, dmem, count, gain):
        n = count >> 1
        if n <= 0:
            return
        s = self.s
        idx = ((dmem >> 1) + np.arange(n)) & 0x7FF
        cur = s[idx].astype(np.int64)
        s[idx] = np.clip((cur * gain) >> 4, -32768, 32767)

    def a_resample_zoh(self, dmemo, dmemi, count, pitch, pitch_accu):
        ipos = dmemi >> 1
        opos = dmemo >> 1
        s = self.s
        for _ in range(count >> 1):
            s[opos & 0x7FF] = s[ipos & 0x7FF]
            opos += 1
            pitch_accu += pitch
            ipos += pitch_accu >> 16
            pitch_accu &= 0xFFFF

    def a_overload(self, dmem, count, gain, attenuation):
        s = self.s
        pos = dmem >> 1
        for _ in range(count):
            accu = _clamp16(int(s[pos & 0x7FF]) * gain)
            s[pos & 0x7FF] = _s16((accu * attenuation) >> 16)
            pos += 1

    def a_envmix_nead(self, swap_wet_lr, dmem_dl, dmem_dr, dmem_wl, dmem_wr, dmemi, count, xors):
        s = self.s
        ev, es = self.env_values, self.env_steps
        count = _align(count, 8)
        if swap_wet_lr:
            dmem_wl, dmem_wr = dmem_wr, dmem_wl
        ib, dl, dr, wl, wr = dmemi >> 1, dmem_dl >> 1, dmem_dr >> 1, dmem_wl >> 1, dmem_wr >> 1
        x0, x1, x2, x3 = xors
        while count != 0:
            e0, e1, e2 = ev
            for i in range(8):
                v = int(s[(ib + i) & 0x7FF])
                l = _s16(((v * e0) >> 16) ^ x0)
                r = _s16(((v * e1) >> 16) ^ x1)
                l2 = _s16(((l * e2) >> 16) ^ x2)
                r2 = _s16(((r * e2) >> 16) ^ x3)
                j = (dl + i) & 0x7FF
                s[j] = _clamp16(int(s[j]) + l)
                j = (dr + i) & 0x7FF
                s[j] = _clamp16(int(s[j]) + r)
                j = (wl + i) & 0x7FF
                s[j] = _clamp16(int(s[j]) + l2)
                j = (wr + i) & 0x7FF
                s[j] = _clamp16(int(s[j]) + r2)
            ev[0] = (e0 + es[0]) & 0xFFFF
            ev[1] = (e1 + es[1]) & 0xFFFF
            ev[2] = (e2 + es[2]) & 0xFFFF
            dl += 8
            dr += 8
            wl += 8
            wr += 8
            ib += 8
            count -= 8

    @staticmethod
    def _ramp(value, step, target, n):
        """n шагов alist ramp_step: возвращает (массив int16 (value>>16), value, step)."""
        k = np.arange(1, n + 1, dtype=np.int64)
        if step == 0:
            if value <= target:
                value = target
            vals = np.full(n, value, dtype=np.int64)
            return (vals >> 16).astype(np.int16), value, 0
        vals = value + step * k
        if step < 0:
            reached = vals <= target
        else:
            reached = vals >= target
        if reached.any():
            first = int(np.argmax(reached))
            vals[first:] = target
            step = 0
        if n:
            value = int(vals[-1])
        return (vals >> 16).astype(np.int16), value, step

    def a_envmix_lin(self, init, dmem_dl, dmem_dr, dmem_wl, dmem_wr, dmemi, count, dry, wet,
                     vol, target, rate, address):
        """envmix 'lin' (ABI3).  Возвращает (value0, value1, target0, target1, dry) для трассы."""
        raw = self.m.dram_read_bytes(address & 0xFFFFFF, 80)
        wd = list(struct.unpack('>20I', raw))
        sw = lambda x: x - (1 << 32) if x & 0x80000000 else x
        div8 = lambda a: -((-a) // 8) if a < 0 else a // 8
        if init:
            st = [div8(rate[0]), div8(rate[1])]
            vl = [vol[0] << 16, vol[1] << 16]
            tg = [target[0] << 16, target[1] << 16]
        else:
            wet = _s16(wd[0])
            dry = _s16(wd[1])
            tg = [_s16(wd[2]) << 16, _s16(wd[3]) << 16]
            st = [sw(wd[4]), sw(wd[5])]
            vl = [sw(wd[8]), sw(wd[9])]
        n = count >> 1
        s = self.s
        if n > 0:
            lv, vl[0], st[0] = self._ramp(vl[0], st[0], tg[0], n)
            rv, vl[1], st[1] = self._ramp(vl[1], st[1], tg[1], n)
            L = lv.astype(np.int64)
            Rr = rv.astype(np.int64)
            ib = dmemi >> 1
            src = s[(ib + np.arange(n)) & 0x7FF].astype(np.int64)
            gains = [np.clip((L * dry + 0x4000) >> 15, -32768, 32767),
                     np.clip((Rr * dry + 0x4000) >> 15, -32768, 32767),
                     np.clip((L * wet + 0x4000) >> 15, -32768, 32767),
                     np.clip((Rr * wet + 0x4000) >> 15, -32768, 32767)]
            dst = [dmem_dl, dmem_dr, dmem_wl, dmem_wr]
            for k in range(4):
                idx = ((dst[k] >> 1) + np.arange(n)) & 0x7FF
                cur = s[idx].astype(np.int64)
                s[idx] = np.clip(cur + ((src * gains[k]) >> 15), -32768, 32767)
        wd[0] = (wd[0] & 0xFFFF0000) | (wet & 0xFFFF)
        wd[1] = (wd[1] & 0xFFFF0000) | (dry & 0xFFFF)
        wd[2] = (wd[2] & 0xFFFF0000) | ((tg[0] >> 16) & 0xFFFF)
        wd[3] = (wd[3] & 0xFFFF0000) | ((tg[1] >> 16) & 0xFFFF)
        wd[4] = st[0] & M32
        wd[5] = st[1] & M32
        wd[8] = vl[0] & M32
        wd[9] = vl[1] & M32
        self.m.dram_write_bytes(address & 0xFFFFFF, struct.pack('>20I', *wd))
        return vl[0] >> 16, vl[1] >> 16, tg[0] >> 16, tg[1] >> 16, dry

    def a_polef(self, init, dmemo, dmemi, count, gain, address):
        count = _align(count, 16)
        if count == 0:
            return
        s = self.s
        h1 = self.table[0:8]
        h2 = self.table[8:16]
        if init:
            l1 = l2 = 0
        else:
            l1 = _s16(self.dram_u16(address + 4))
            l2 = _s16(self.dram_u16(address + 6))
        h2_before = list(h2)
        for i in range(8):
            self.table[8 + i] = _s16((h2[i] * gain) >> 14)
        h2 = self.table[8:16]
        di = dmemi >> 1
        dst = dmemo >> 1
        last = None
        while count > 0:
            frame = [int(s[(di + i) & 0x7FF]) for i in range(8)]
            di += 8
            outs = [0] * 8
            for i in range(8):
                accu = frame[i] * gain
                accu += h1[i] * l1 + h2_before[i] * l2
                for j in range(i):
                    accu += h2[j] * frame[i - 1 - j]
                outs[i] = _clamp16(accu >> 14)
            for i in range(8):
                s[(dst + i) & 0x7FF] = outs[i]
            l1 = outs[6]
            l2 = outs[7]
            last = outs
            dst += 8
            count -= 16
        for i in range(4):
            self.dram_set_u16(address + 2 * i, last[4 + i])

    def a_iirf(self, init, dmemo, dmemi, count, address):
        t = self.table
        vmulf = lambda a, b: (a * b + 0x4000) >> 15
        count = _align(count, 16)
        s = self.s
        frame = [0] * 8
        ibuf = [0] * 4
        if not init:
            frame[6] = _s16(self.dram_u16(address + 4))
            frame[7] = _s16(self.dram_u16(address + 6))
            ibuf[1] = _s16(self.dram_u16(address + 8))
            ibuf[2] = _s16(self.dram_u16(address + 10))
        index = 7
        dst = dmemo >> 1
        di = dmemi >> 1
        prev = vmulf(t[9], frame[6]) * 2
        while True:
            for i in range(8):
                ibuf[index & 3] = int(s[di & 0x7FF])
                accu = prev + vmulf(t[0], ibuf[index & 3]) + vmulf(t[1], ibuf[(index - 1) & 3]) \
                    + vmulf(t[0], ibuf[(index - 2) & 3])
                accu += vmulf(t[8], frame[index]) * 2
                prev = vmulf(t[9], frame[index]) * 2
                frame[i] = _s16(accu)
                s[(dst + i) & 0x7FF] = frame[i]
                index = (index + 1) & 7
                di += 1
            dst += 8
            count -= 0x10
            if count <= 0:
                break
        self.dram_set_u16(address + 4, frame[6])
        self.dram_set_u16(address + 6, frame[7])
        self.dram_set_u16(address + 8, ibuf[(index - 2) & 3])
        self.dram_set_u16(address + 10, ibuf[(index - 1) & 3])

    def a_filter(self, dmem, count, address, lut):
        """alist_filter (FILTER в Zelda/Yoshi): в C индексы int16* идут в порядке памяти хоста
        (порядок полуслов внутри слова обратный), поэтому индекс k <-> логический k^1."""
        def dram_hw(base, k):
            return _s16(self.dram_u16(base + 2 * (k ^ 1)))

        t6 = [dram_hw(lut[0], k) for k in range(8)]
        t5 = [dram_hw(lut[1], k) for k in range(8)]
        for x in range(8):
            v = (t5[x] + t6[x]) >> 1
            t5[x] = t6[x] = v
        for x in range(8):
            self.dram_set_u16(lut[1] + 2 * (x ^ 1), t5[x])
            self.dram_set_u16(lut[0] + 2 * (x ^ 1), t6[x])
        s = self.s
        dm = dmem >> 1
        in1 = [dram_hw(address, k) for k in range(8)]
        pos = 0                                    # in2 = dm + pos (в полусловах, порядок хоста)
        outbuff = []

        # in2[k] (хост-порядок) = логический полуслово (pos + k) ^ 1
        def in2(p, k):
            return int(s[(dm + ((p + k) ^ 1)) & 0x7FF])
        t = t6
        for x in range(0, count, 16):
            i2 = [in2(pos, k) for k in range(8)]
            a = in1
            v = [0] * 8
            v[1] = a[0] * t[6] + a[3] * t[7] + a[2] * t[4] + a[5] * t[5] + a[4] * t[2] + a[7] * t[3] + a[6] * t[0] + i2[1] * t[1]
            v[0] = a[3] * t[6] + a[2] * t[7] + a[5] * t[4] + a[4] * t[5] + a[7] * t[2] + a[6] * t[3] + i2[1] * t[0] + i2[0] * t[1]
            v[3] = a[2] * t[6] + a[5] * t[7] + a[4] * t[4] + a[7] * t[5] + a[6] * t[2] + i2[1] * t[3] + i2[0] * t[0] + i2[3] * t[1]
            v[2] = a[5] * t[6] + a[4] * t[7] + a[7] * t[4] + a[6] * t[5] + i2[1] * t[2] + i2[0] * t[3] + i2[3] * t[0] + i2[2] * t[1]
            v[5] = a[4] * t[6] + a[7] * t[7] + a[6] * t[4] + i2[1] * t[5] + i2[0] * t[2] + i2[3] * t[3] + i2[2] * t[0] + i2[5] * t[1]
            v[4] = a[7] * t[6] + a[6] * t[7] + i2[1] * t[4] + i2[0] * t[5] + i2[3] * t[2] + i2[2] * t[3] + i2[5] * t[0] + i2[4] * t[1]
            v[7] = a[6] * t[6] + i2[1] * t[7] + i2[0] * t[4] + i2[3] * t[5] + i2[2] * t[2] + i2[5] * t[3] + i2[4] * t[0] + i2[7] * t[1]
            v[6] = i2[1] * t[6] + i2[0] * t[7] + i2[3] * t[4] + i2[2] * t[5] + i2[5] * t[2] + i2[4] * t[3] + i2[7] * t[0] + i2[6] * t[1]
            outbuff.extend(_s16((v[k] + 0x4000) >> 15) for k in range(8))
            in1 = i2
            pos += 8
        # последние 8 входных полуслов (in2 - 8) -> DRAM (16 байт, сырая копия)
        last_bytes = bytes(self.buf[((dm + pos - 8) * 2 + i) & 0xFFF] for i in range(16)) if pos >= 8 else bytes(16)
        self.m.dram_write_bytes(address & 0xFFFFFF, last_bytes)
        # outbuff (хост-порядок) -> DMEM
        for k, v in enumerate(outbuff):
            s[(dm + (k ^ 1)) & 0x7FF] = v

    # ---------- общая часть трассы (A/R/E) ----------
    def tr_adpcm(self, flags, inn, out, cnt, address):
        self.adpcm_seen = 1
        self.adpcm_init = flags & 1
        if self.t is None:
            return
        ob = (out + 32) & 0xFFFF
        off = -1
        v = self.voice(address)
        if flags & 1:
            v[0] = v[1] = v[2] = 0
        cum = v[0]
        if not v[2]:
            h = (2166136261 ^ self.cur_table) & M32
            h = (h * 16777619) & M32
            for i in range(16):
                if i * 2 < cnt:
                    h ^= self.sample(((ob & 0xFFF) >> 1) + i) & 0xFFFF
                    h = (h * 16777619) & M32
            v[1] = h
            v[2] = 1
        h = v[1]
        if cum + cnt > self.cov_len(h) and self.cov_len(h) < 262144:
            off = self.pcm_write_samples(ob, cnt // 2)
            self.cov[h] = cum + cnt
        self.t.append('A %u %u %u %u %x %x %x %d %d %u\n' % (
            flags, inn, out, cnt, address, self.loop, h, cum, off, 1 if (cum + cnt) < 0x7FFFFFFF else 0))
        v[0] += cnt

    def tr_resample(self, flags, pitch, address, inn, out, count):
        if self.t is None:
            return
        if (flags & A_INIT) and not self.adpcm_init:
            v0 = self.voice((address - 0x40) & M32)
            if v0[0] > 0:
                v0[0] = 0
                v0[2] = 0
        if not self.adpcm_seen and self.last_load_cnt > 0:
            cnt = self.last_load_cnt
            ib = self.last_load_in
            vaddr = (address - 0x40) & M32
            v = self.voice(vaddr)
            off = -1
            if flags & A_INIT:
                v[0] = v[1] = v[2] = 0
            cum = v[0]
            if not v[2]:
                h = (2166136261 ^ 0x50434D00) & M32
                h = (h * 16777619) & M32
                for i in range(16):
                    if i * 2 < cnt:
                        h ^= self.sample(((ib & 0xFFF) >> 1) + i) & 0xFFFF
                        h = (h * 16777619) & M32
                v[1] = h
                v[2] = 1
            h = v[1]
            if cum + cnt > self.cov_len(h) and self.cov_len(h) < 262144:
                off = self.pcm_write_samples(ib, cnt // 2)
                self.cov[h] = cum + cnt
            self.t.append('A %u %u %u %u %x %x %x %d %d %u\n' % (flags & A_INIT, ib, 0, cnt, vaddr, 0, h, cum, off, 1))
            v[0] += cnt
        self.t.append('R %u %u %x %u %u %u\n' % (flags, pitch, address, inn, out, count))

    def tr_envmix(self, flags, inn, out, count, v0, v1, t0, t1, dry, address):
        self.adpcm_seen = 0
        self.adpcm_init = 0
        self.last_load_cnt = 0
        if self.t is not None:
            self.t.append('E %u %u %u %u %d %d %d %d %d %d %d %d %x\n' % (
                flags, inn, out, count, v0, v1, t0, t1, 0, 0, dry, 0, address))

    def tr_load(self, dmem, count, address):
        self.tr('B %u %u %x\n' % (dmem, count, address))
        self.last_load_in = dmem
        self.last_load_cnt = count

    # ---------- ABI3 (naudio) ----------
    N3_COUNT = 0x170
    N3_MAIN, N3_MAIN2 = 0x4F0, 0x660
    N3_DRY_L, N3_DRY_R, N3_WET_L, N3_WET_R = 0x9D0, 0xB40, 0xCB0, 0xE20

    def n3_noop(self, w1, w2):
        pass

    def n3_02b0(self, w1, w2):
        self.rate[1] = _to_i32((self.rate[1] & 0xFFFF0000) | (w2 & 0xFFFF))

    def n3_14(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        gain = w1 & 0xFFFF
        address = w2 & 0xFFFFFF
        dmem = self.N3_MAIN if (w2 >> 24) == 0 else self.N3_MAIN2
        if self.table[0] == 0 and self.table[1] == 0:
            self.a_polef(flags & A_INIT, dmem, dmem, self.N3_COUNT, gain, address)
        else:
            self.a_iirf(flags & A_INIT, dmem, dmem, self.N3_COUNT, address)

    def n3_setvol(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        self.tr('V %08x %08x\n' % (w1, w2))
        if flags & A_VOL:
            if flags & A_LEFT:
                self.vol[0] = _s16(w1)
                self.dry = _s16(w2 >> 16)
                self.wet = _s16(w2)
            else:
                self.target[1] = _s16(w1)
                self.rate[1] = _to_i32(w2)
        else:
            self.target[0] = _s16(w1)
            self.rate[0] = _to_i32(w2)

    def n3_envmixer(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        address = w2 & 0xFFFFFF
        self.vol[1] = _s16(w1)
        v0, v1, t0, t1, dry = self.a_envmix_lin(
            flags & A_INIT, self.N3_DRY_L, self.N3_DRY_R, self.N3_WET_L, self.N3_WET_R,
            self.N3_MAIN, self.N3_COUNT, self.dry, self.wet, self.vol, self.target, self.rate, address)
        self.tr_envmix(flags, self.N3_MAIN, self.N3_MAIN, self.N3_COUNT, v0, v1, t0, t1, dry, address)

    def n3_clearbuff(self, w1, w2):
        self.a_clear((w1 + self.N3_MAIN) & 0xFFFF, w2 & 0xFFF)

    def n3_mixer(self, w1, w2):
        self.a_mix((w2 + self.N3_MAIN) & 0xFFFF, ((w2 >> 16) + self.N3_MAIN) & 0xFFFF, self.N3_COUNT, _s16(w1))

    def n3_loadbuff(self, w1, w2):
        count = (w1 >> 12) & 0xFFF
        dmem = ((w1 & 0xFFF) + self.N3_MAIN) & 0xFFFF
        address = w2 & 0xFFFFFF
        self.tr_load(dmem, count, address)
        self.a_load(dmem, address, count)

    def n3_savebuff(self, w1, w2):
        count = (w1 >> 12) & 0xFFF
        dmem = ((w1 & 0xFFF) + self.N3_MAIN) & 0xFFFF
        address = w2 & 0xFFFFFF
        self.tr('S %u %u %x\n' % (dmem, count, address))
        self.a_save(dmem, address, count)

    def n3_loadadpcm(self, w1, w2):
        count = w1 & 0xFFFF
        address = w2 & 0xFFFFFF
        self.tr('T %x %u\n' % (address, count))
        self.cur_table = address
        for i in range(min(count >> 1, 128)):
            self.table[i] = _s16(self.dram_u16(address + 2 * i))

    def n3_dmemmove(self, w1, w2):
        self.a_move(((w2 >> 16) + self.N3_MAIN) & 0xFFFF, (w1 + self.N3_MAIN) & 0xFFFF, ((w2 & 0xFFFF) + 3) & ~3)

    def n3_setloop(self, w1, w2):
        self.loop = w2 & 0xFFFFFF
        self.tr('L %x\n' % self.loop)

    def n3_adpcm(self, w1, w2):
        address = w1 & 0xFFFFFF
        flags = (w2 >> 28) & 0xF
        count = (w2 >> 16) & 0xFFF
        dmemi = (((w2 >> 12) & 0xF) + self.N3_MAIN) & 0xFFFF
        dmemo = ((w2 & 0xFFF) + self.N3_MAIN) & 0xFFFF
        cnt = (count + 0x1F) & ~0x1F
        self.adpcm(flags & A_INIT, flags & A_LOOP, dmemo, dmemi, cnt, address)
        self.tr_adpcm(flags, dmemi, dmemo, cnt, address)

    def n3_resample(self, w1, w2):
        address = w1 & 0xFFFFFF
        flags = (w2 >> 30) & 3
        pitch = (w2 >> 14) & 0xFFFF
        dmemi = (((w2 >> 2) & 0xFFF) + self.N3_MAIN) & 0xFFFF
        dmemo = self.N3_MAIN2 if (w2 & 3) else self.N3_MAIN
        self.tr_resample(flags, pitch, address, dmemi, dmemo, self.N3_COUNT)
        self.resample(flags & A_INIT, dmemo, dmemi, self.N3_COUNT, pitch << 1, address)

    def n3_interleave(self, w1, w2):
        self.a_interleave(self.N3_MAIN, self.N3_DRY_L, self.N3_DRY_R, self.N3_COUNT)

    def n3_overload(self, w1, w2):
        self.a_overload((w1 & 0xFFF) + self.N3_MAIN, self.N3_COUNT, _s16(w2), (w2 >> 16) & 0xFFFF)

    def n3_table(self, variant):
        n, ad, cl, ev = self.n3_noop, self.n3_adpcm, self.n3_clearbuff, self.n3_envmixer
        lb, rs, sb = self.n3_loadbuff, self.n3_resample, self.n3_savebuff
        sv, dm, la, mx = self.n3_setvol, self.n3_dmemmove, self.n3_loadadpcm, self.n3_mixer
        il, sl = self.n3_interleave, self.n3_setloop
        if variant in ('mp3', 'cbfd'):
            return [self.n3_overload, ad, cl, ev, lb, rs, sb, n, n, sv, dm, la, mx, il, self.n3_14, sl]
        f7 = mx if variant == 'dk' else n
        return [n, ad, cl, ev, lb, rs, sb, f7, f7, sv, dm, la, mx, il, self.n3_02b0, sl]

    # ---------- ABI2 (nead) ----------
    def n2_setbuff(self, w1, w2):
        self.inn = w1 & 0xFFFF
        self.out = (w2 >> 16) & 0xFFFF
        self.count = w2 & 0xFFFF

    def n2_adpcm(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        address = w2 & 0xFFFFFF
        cnt = (self.count + 0x1F) & ~0x1F
        self.adpcm(flags & 1, flags & 2, self.out, self.inn, cnt, address, bool(flags & 4))
        self.tr_adpcm(flags, self.inn, self.out, cnt, address)

    def n2_clearbuff(self, w1, w2):
        count = w2 & 0xFFF
        if count:
            self.a_clear(w1 & 0xFFFF, count)

    def n2_loadbuff(self, w1, w2):
        count = (w1 >> 12) & 0xFFF
        dmem = w1 & 0xFFF
        address = w2 & 0xFFFFFF
        self.tr_load(dmem, count, address)
        self.a_load(dmem, address, count)

    def n2_savebuff(self, w1, w2):
        count = (w1 >> 12) & 0xFFF
        dmem = w1 & 0xFFF
        address = w2 & 0xFFFFFF
        self.tr('S %u %u %x\n' % (dmem, count, address))
        self.a_save(dmem, address, count)

    def n2_mixer(self, w1, w2):
        self.a_mix(w2 & 0xFFFF, (w2 >> 16) & 0xFFFF, (w1 >> 12) & 0xFF0, _s16(w1))

    def n2_resample(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        pitch = w1 & 0xFFFF
        address = w2 & 0xFFFFFF
        cnt = (self.count + 0xF) & ~0xF
        self.tr_resample(flags, pitch, address, self.inn, self.out, self.count)
        self.resample(flags & 1, self.out, self.inn, cnt, pitch << 1, address)

    def n2_resample_zoh(self, w1, w2):
        self.a_resample_zoh(self.out, self.inn, self.count, (w1 & 0xFFFF) << 1, w2 & 0xFFFF)

    def n2_dmemmove(self, w1, w2):
        count = w2 & 0xFFFF
        if count:
            self.a_move((w2 >> 16) & 0xFFFF, w1 & 0xFFFF, (count + 3) & ~3)

    def n2_envsetup1_mk(self, w1, w2):
        self.env_values[2] = (w1 >> 8) & 0xFF00
        self.env_steps[2] = 0
        self.env_steps[0] = (w2 >> 16) & 0xFFFF
        self.env_steps[1] = w2 & 0xFFFF

    def n2_envsetup1(self, w1, w2):
        self.env_values[2] = (w1 >> 8) & 0xFF00
        self.env_steps[2] = w1 & 0xFFFF
        self.env_steps[0] = (w2 >> 16) & 0xFFFF
        self.env_steps[1] = w2 & 0xFFFF

    def n2_envsetup2(self, w1, w2):
        self.env_values[0] = (w2 >> 16) & 0xFFFF
        self.env_values[1] = w2 & 0xFFFF

    def _n2_env(self, w1, w2, mk):
        dmemi = (w1 >> 12) & 0xFF0
        count = (w1 >> 8) & 0xFF
        dl, dr = (w2 >> 20) & 0xFF0, (w2 >> 12) & 0xFF0
        wl, wr = (w2 >> 4) & 0xFF0, (w2 << 4) & 0xFF0
        if mk:
            swap = False
            x2 = x3 = 0
        else:
            swap = bool((w1 >> 4) & 1)
            x2 = (-((w1 & 0x8) >> 1)) & 0xFFFF
            x3 = (-((w1 & 0x4) >> 1)) & 0xFFFF
        x0 = (-((w1 & 0x2) >> 1)) & 0xFFFF
        x1 = (-(w1 & 0x1)) & 0xFFFF
        # трасса: коэффициенты до применения (затем они сдвигаются шагами)
        ev0, ev1 = self.env_values[0], self.env_values[1]
        steps = _align(count, 8) // 8
        t0 = (ev0 + self.env_steps[0] * steps) & 0xFFFF
        t1 = (ev1 + self.env_steps[1] * steps) & 0xFFFF
        self.a_envmix_nead(swap, dl, dr, wl, wr, dmemi, count, (x0, x1, x2, x3))
        # 16-битная огибающая 0..65535 -> шкала ABI1 (32768 = 1.0)
        self.tr_envmix(0, dmemi, dl, count, ev0 >> 1, ev1 >> 1, t0 >> 1, t1 >> 1, 32768, 0)

    def n2_envmixer_mk(self, w1, w2):
        self._n2_env(w1, w2, True)

    def n2_envmixer(self, w1, w2):
        self._n2_env(w1, w2, False)

    def n2_duplicate(self, w1, w2):
        self.a_repeat64(w2 >> 16 & 0xFFFF, w1 & 0xFFFF, (w1 >> 16) & 0xFF)

    def n2_interl(self, w1, w2):
        self.a_copy_every_other(w2 & 0xFFFF, (w2 >> 16) & 0xFFFF, w1 & 0xFFFF)

    def n2_interleave_mk(self, w1, w2):
        if self.count == 0:
            return
        self.a_interleave(self.out, (w2 >> 16) & 0xFFFF, w2 & 0xFFFF, self.count)

    def n2_interleave(self, w1, w2):
        self.a_interleave(w1 & 0xFFFF, (w2 >> 16) & 0xFFFF, w2 & 0xFFFF, (w1 >> 12) & 0xFF0)

    def n2_addmixer(self, w1, w2):
        self.a_add(w2 & 0xFFFF, (w2 >> 16) & 0xFFFF, (w1 >> 12) & 0xFF0)

    def n2_hilogain(self, w1, w2):
        g = (w1 >> 16) & 0xFF
        self.a_multq44((w2 >> 16) & 0xFFFF, w1 & 0xFFF, g - 256 if g & 0x80 else g)

    def n2_filter(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        address = w2 & 0xFFFFFF
        if flags > 1:
            self.filter_count = w1 & 0xFFFF
            self.filter_lut[0] = address
        else:
            self.filter_lut[1] = address + 0x10
            self.a_filter(w1 & 0xFFFF, self.filter_count, address, self.filter_lut)

    def n2_nead16(self, w1, w2):
        self.a_copy_blocks((w2 >> 16) & 0xFFFF, w1 & 0xFFFF, w2 & 0xFFFF, (w1 >> 16) & 0xFF)

    def n2_polef(self, w1, w2):
        if self.count == 0:
            return
        self.a_polef((w1 >> 16) & 1, self.out, self.inn, self.count, w1 & 0xFFFF, w2 & 0xFFFFFF)

    def n2_table(self, variant):
        n = self.n3_noop
        U = n                                    # UNKNOWN: в C только предупреждение
        ad, cl = self.n2_adpcm, self.n2_clearbuff
        rs, zoh = self.n2_resample, self.n2_resample_zoh
        sb = self.n2_setbuff
        dm, la, mx = self.n2_dmemmove, self.n3_loadadpcm_nead, self.n2_mixer
        sl, nd16, il = self.n2_setloop, self.n2_nead16, self.n2_interl
        lb, sv = self.n2_loadbuff, self.n2_savebuff
        e2 = self.n2_envsetup2
        pol, ilm, ili = self.n2_polef, self.n2_interleave_mk, self.n2_interleave
        am, hl, dup, flt = self.n2_addmixer, self.n2_hilogain, self.n2_duplicate, self.n2_filter
        seg = n
        if variant == 'mk':
            return [n, ad, cl, n, n, rs, n, seg, sb, n, dm, la, mx, ilm, pol, sl,
                    nd16, il, self.n2_envsetup1_mk, self.n2_envmixer_mk, lb, sv, e2, n,
                    n, n, n, n, n, n, n, n]
        e1, em = self.n2_envsetup1, self.n2_envmixer
        if variant == 'sf':
            return [n, ad, cl, n, am, rs, zoh, n, sb, n, dm, la, mx, ilm, pol, sl,
                    nd16, il, e1, em, lb, sv, e2, n, hl, U, dup, n, n, n, n, n]
        if variant == 'sfj':
            return [n, ad, cl, n, am, rs, zoh, n, sb, n, dm, la, mx, ilm, pol, sl,
                    nd16, il, e1, em, lb, sv, e2, U, hl, U, dup, n, n, n, n, n]
        if variant == 'fz':
            return [U, ad, cl, n, am, rs, n, n, sb, n, dm, la, mx, ili, n, sl,
                    nd16, il, e1, em, lb, sv, e2, U, n, U, dup, n, n, n, n, n]
        if variant == 'wrjb':
            return [n, ad, cl, U, am, rs, zoh, n, sb, n, dm, la, mx, ili, n, sl,
                    nd16, il, e1, em, lb, sv, e2, U, hl, U, dup, flt, n, n, n, n]
        # семейство Zelda/Yoshi (0x18 команд)
        zero = {'ys': U, '1080': U, 'oot': U, 'mm': U, 'mmb': n, 'ac': U}[variant]
        three = {'ys': U, '1080': U, 'oot': U, 'mm': n, 'mmb': n, 'ac': n}[variant]
        return [zero, ad, cl, three, am, rs, zoh, flt, sb, dup, dm, la, mx, ili, hl, sl,
                nd16, il, e1, em, lb, sv, e2, U]

    def n3_loadadpcm_nead(self, w1, w2):
        count = w1 & 0xFFFF
        address = w2 & 0xFFFFFF
        self.tr('T %x %u\n' % (address, count))
        self.cur_table = address
        for i in range(min(count >> 1, 128)):
            self.table[i] = _s16(self.dram_u16(address + 2 * i))

    def n2_setloop(self, w1, w2):
        self.loop = w2 & 0xFFFFFF
        self.tr('L %x\n' % self.loop)

    # ---------- запуск alist ----------
    def run_alist(self, table):
        m = self.m
        if self.t is not None:
            self.t.append('F\n')
        sp = m.spmem
        data_ptr = sp[0xFF0 >> 2]
        data_size = sp[0xFF4 >> 2]
        raw = m.dram_read_bytes(data_ptr & 0xFFFFFF, data_size & ~7)
        words = struct.unpack('>%dI' % (len(raw) // 4), raw)
        size = len(table)
        for i in range(0, len(words), 2):
            w1, w2 = words[i], words[i + 1]
            acmd = (w1 >> 24) & 0x7F
            if acmd < size:
                table[acmd](w1, w2)
        m.rsp_break(0x200)


class MusyxMixin:
    """MusyX v1/v2 (Factor 5; 007 TWINE, Rogue Squadron, Indiana Jones, ...): порт rsp_hle/musyx.c
    + трасса голосов в том же формате A/R/E, что и у ABI1/2/3."""
    MX_SUB = 192
    MX_VOICES = 32
    MX_VSIZE = 0x50

    # ---- чтение DRAM ----
    def mx_u8(self, a):
        return self.m.dram_read_bytes(a & 0xFFFFFF, 1)[0]

    def mx_u16s(self, a, n):
        if n <= 0:
            return []
        return list(struct.unpack('>%dH' % n, self.m.dram_read_bytes(a & 0xFFFFFF, 2 * n)))

    def mx_s16s(self, a, n):
        if n <= 0:
            return []
        return list(struct.unpack('>%dh' % n, self.m.dram_read_bytes(a & 0xFFFFFF, 2 * n)))

    def mx_u32s(self, a, n):
        if n <= 0:
            return []
        return list(struct.unpack('>%dI' % n, self.m.dram_read_bytes(a & 0xFFFFFF, 4 * n)))

    def mx_store_u16(self, a, vals):
        self.m.dram_write_bytes(a & 0xFFFFFF, struct.pack('>%dH' % len(vals), *[v & 0xFFFF for v in vals]))

    # ---- состояние ----
    def mx_load_base_vol(self, address):
        hi = self.mx_u16s(address, 4)
        lo = self.mx_u16s(address + 8, 4)
        return [_to_i32((hi[k] << 16) | lo[k]) for k in range(4)]

    def mx_save_base_vol(self, bv, address):
        self.mx_store_u16(address, [(v >> 16) & 0xFFFF for v in bv] + [v & 0xFFFF for v in bv])

    def mx_update_base_vol(self, bv, voice_mask, last_sample_ptr, mask_15, ptr_24):
        if voice_mask:
            for i in range(self.MX_VOICES):
                if (voice_mask >> i) & 1:
                    vals = self.mx_s16s(last_sample_ptr + 8 * i, 4)
                    for k in range(4):
                        bv[k] = _to_i32(bv[k] + vals[k])
        if mask_15:
            for i in range(4):
                if (mask_15 >> i) & 1:
                    vals = self.mx_s16s(ptr_24 + 8 * i, 4)
                    for k in range(4):
                        bv[k] = _to_i32(bv[k] + vals[k])
        for k in range(4):
            bv[k] = _to_i32(bv[k] * 0x0000F850) >> 16

    # ---- голоса ----
    def mx_cat(self, catsrc, nbytes_unit):
        ptr1, ptr2, size1, size2 = struct.unpack('>IIHH', self.m.dram_read_bytes(catsrc & 0xFFFFFF, 12))
        data = self.m.dram_read_bytes(ptr1 & 0xFFFFFF, size1)
        if size2:
            data += self.m.dram_read_bytes(ptr2 & 0xFFFFFF, size2)
        return data, size1

    def mx_adpcm_decode(self, dst, doff, src, table, count, skip):
        """adpcm_decode_frames: dst - список int, doff - смещение, src - bytes."""
        sp = 0
        nb = 8
        jump_gap = False
        if skip >= 32:
            jump_gap = True
            nb += 16
            sp += 4
        n = len(src)
        for _ in range(count):
            if nb >= n:
                break
            c2 = src[nb]
            book = table[(c2 & 0xF0):(c2 & 0xF0) + 16]
            if len(book) < 16:
                book = book + [0] * (16 - len(book))
            rshift = c2 & 0x0F
            frame = [0] * 32
            frame[0] = _s16((src[sp] << 8) | src[sp + 1]) if sp + 3 < n else 0
            frame[1] = _s16((src[sp + 2] << 8) | src[sp + 3]) if sp + 3 < n else 0
            for i in range(1, 16):
                byte = src[nb + i] if nb + i < n else 0
                frame[2 * i] = _s16((byte & 0xF0) << 8) >> rshift
                frame[2 * i + 1] = _s16((byte & 0x0F) << 12) >> rshift
            out = [0] * 32
            out[0], out[1] = frame[0], frame[1]
            b1, b2 = book[0:8], book[8:16]

            def resid(d0, cnt, last0):
                l1, l2 = out[last0], out[last0 + 1]
                for i in range(cnt):
                    accu = (frame[d0 + i] << 11) + b1[i] * l1 + b2[i] * l2
                    for j in range(i):
                        accu += b2[j] * frame[d0 + i - 1 - j]
                    out[d0 + i] = _clamp16(accu >> 11)
            resid(2, 6, 0)
            resid(8, 8, 6)
            resid(16, 8, 14)
            resid(24, 8, 22)
            if jump_gap:
                nb += 8
                sp += 32
            jump_gap = not jump_gap
            nb += 16
            sp += 4
            dst[doff:doff + 32] = out
            doff += 32

    def mx_load_samples(self, vp):
        samples = [0] * (0x200 + 16)
        ad3c = self.mx_u8(vp + 0x3C)
        if ad3c == 0:                                          # PCM16
            u8_3e = self.mx_u8(vp + 0x3E)
            u16_40, u16_42 = self.mx_u16s(vp + 0x40, 2)
            count = _align(u16_40 + u8_3e, 4)
            segbase = 0x200 - count
            offset = u8_3e
            data, size1 = self.mx_cat(vp + 0x24, 2)
            vals = list(struct.unpack('>%dh' % (len(data) // 2), data[:len(data) & ~1]))
            samples[segbase:segbase + len(vals)] = vals[:max(0, 0x200 - segbase)]
            if u16_42:
                data, size1 = self.mx_cat(vp + 0x30, 2)
                vals = list(struct.unpack('>%dh' % (len(data) // 2), data[:len(data) & ~1]))
                samples[0:len(vals)] = vals[:0x200]
        else:
            u8_3d = self.mx_u8(vp + 0x3D)
            u8_3e = self.mx_u8(vp + 0x3E)
            u8_3f = self.mx_u8(vp + 0x3F)
            tptr = struct.unpack('>I', self.m.dram_read_bytes((vp + 0x40) & 0xFFFFFF, 4))[0]
            table = self.mx_s16s(tptr, 128)
            count = ad3c << 5
            segbase = 0x200 - count
            offset = u8_3e & 0x1F
            data, _ = self.mx_cat(vp + 0x24, 1)
            self.mx_adpcm_decode(samples, segbase, data, table, ad3c, u8_3e)
            if u8_3d:
                data, _ = self.mx_cat(vp + 0x30, 1)
                self.mx_adpcm_decode(samples, 0, data, table, u8_3d, u8_3f)
        return samples, segbase, offset

    def mx_mix_voice(self, mixer, vp, samples, segbase, offset, last_sample_ptr):
        S = self.MX_SUB
        pitch_q16, pitch_shift = self.mx_u16s(vp + 0x20, 2)
        end_point, restart_point = self.mx_u16s(vp + 0x48, 2)
        u16_4e = self.mx_u16s(vp + 0x4E, 1)[0]
        start = segbase + offset + u16_4e
        s_end = segbase + end_point
        s_restart = (restart_point & 0x7FFF) + (0 if (restart_point & 0x8000) else segbase)
        step = pitch_shift << 4
        env = [_to_i32(x) for x in self.mx_u32s(vp, 4)]
        env_step = [_to_i32(x) for x in self.mx_u32s(vp + 0x10, 4)]
        i = np.arange(S, dtype=np.int64)
        tot = pitch_q16 + step * i
        lut_i = (tot & 0xFC00) >> 8
        smp = start + (tot >> 16)
        i0 = 0
        while i0 < S:
            hit = np.nonzero(smp[i0:] >= s_end)[0]
            if len(hit) == 0:
                break
            j = i0 + int(hit[0])
            smp[j:] += s_restart - s_end
            i0 = j + 1
        arr = np.array(samples, dtype=np.int64)
        L = np.array(RESAMPLE_LUT, dtype=np.int64)
        L = np.where(L >= 0x8000, L - 0x10000, L)
        idx = np.clip(smp, 0, len(samples) - 4)
        accu = np.zeros(S, dtype=np.int64)
        for k in range(4):
            accu = np.clip(accu + ((arr[idx + k] * L[lut_i + k]) >> 15), -32768, 32767)
        v = accu
        v4 = []
        for k in range(4):
            e = ((env[k] + env_step[k] * i + 0x80000000) & 0xFFFFFFFF) - 0x80000000
            a = (v * (e >> 16)) >> 15
            a = np.clip(a, -32768, 32767)
            dst = mixer[k]
            mixer[k] = np.clip(a + dst, -32768, 32767)
            v4.append(int(a[-1]))
        self.mx_store_u16(last_sample_ptr, v4)
        self.mx_trace_voice(vp, last_sample_ptr, samples, smp, start, pitch_shift, env, env_step, S)

    def mx_trace_voice(self, vp, key, samples, smp, start, pitch_shift, env, env_step, S):
        if self.t is None:
            return
        ptr1, ptr2, z1, z2 = struct.unpack('>IIHH', self.m.dram_read_bytes((vp + 0x24) & 0xFFFFFF, 12))
        tv = self.voice(key)
        if z1 == 0 and z2 == 0:
            tv[0] = tv[1] = tv[2] = 0
            self.mx_prev.pop(key, None)
            return
        prev = self.mx_prev.get(key)
        init = prev is None
        if not init:
            pp1, pp2, pz1, pz2 = prev
            lo = pp2 if pz2 else pp1
            hi = lo + (pz2 if pz2 else pz1) + 0x80
            lo2 = pp1
            if not ((lo <= ptr1 < hi) or (lo2 <= ptr1 < lo2 + pz1 + 0x80)):
                init = True
        if init:
            tv[0] = 0
        # поток потреблённых выборок (как в C-трассе)
        run = [samples[start]] if 0 <= start < 0x200 else []
        prv = start
        sm = [int(x) for x in smp]
        for idx in sm:
            if 0 <= idx < 0x200 and run is not None:
                if idx > prv:
                    run.extend(samples[prv + 1:idx + 1])
                elif idx < prv:
                    run.append(samples[idx])
                prv = idx
            if len(run) >= 4096:
                break
        run = run[:4096]
        if run:
            cnt = len(run) * 2
            off = -1
            if init or tv[1] == 0:
                h = (2166136261 ^ 0x4D555358) & M32
                h = (h * 16777619) & M32
                for k in range(min(16, len(run))):
                    h ^= run[k] & 0xFFFF
                    h = (h * 16777619) & M32
                tv[1] = h
            h = tv[1]
            if tv[0] + cnt > self.cov_len(h) and self.cov_len(h) < 262144:
                off = len(self.pcm)
                self.pcm += np.array(run, dtype='<i2').tobytes()
                self.cov[h] = tv[0] + cnt
            self.t.append('A %u %u %u %u %x %x %x %d %d %u\n' % (1 if init else 0, 0, 0, cnt, key, ptr2, h, tv[0], off, 1))
            tv[0] += cnt
        self.t.append('R %u %u %x %u %u %u\n' % (0, pitch_shift * 8, key, 0, 0, S))
        self.t.append('E %u %u %u %u %d %d %d %d %d %d %d %d %x\n' % (
            0, 0, 0, S, env[0] >> 16, env[1] >> 16,
            _to_i32(env[0] + env_step[0] * S) >> 16, _to_i32(env[1] + env_step[1] * S) >> 16, 0, 0, 32768, 0, key))
        self.mx_prev[key] = (ptr1, ptr2, z1, z2)

    def mx_voice_stage(self, mixer, vp, last_sample_ptr):
        sz1 = self.mx_u16s(vp + 0x24 + 8, 1)[0]
        if sz1 == 0:
            return struct.unpack('>I', self.m.dram_read_bytes((vp + 0x44) & 0xFFFFFF, 4))[0]
        i = 0
        while True:
            samples, segbase, offset = self.mx_load_samples(vp)
            if not ((self.mute_mask >> i) & 1 if i < 32 else 0):
                self.mx_mix_voice(mixer, vp, samples, segbase, offset, last_sample_ptr + i * 8)
            out = struct.unpack('>I', self.m.dram_read_bytes((vp + 0x44) & 0xFFFFFF, 4))[0]
            if out != 0 or i > 64:
                return out
            i += 1
            vp += self.MX_VSIZE

    # ---- SFX ----
    def mx_sfx_stage(self, mixer, state, sfx_ptr, idx, v2):
        S = self.MX_SUB
        if sfx_ptr == 0:
            return
        cbuf_ptr, cbuf_len = struct.unpack('>II', self.m.dram_read_bytes(sfx_ptr & 0xFFFFFF, 8))
        tap_count = self.mx_u16s(sfx_ptr + 8, 1)[0]
        tap_delays = self.mx_u32s(sfx_ptr + 0x0C, 8)
        tap_gains = self.mx_s16s(sfx_ptr + 0x2C, 8)
        hgain = self.mx_s16s(sfx_ptr + 0x0A, 1)[0]
        hco = self.mx_s16s(sfx_ptr + 0x40, 4)
        g0, g1 = self.mx_u16s(sfx_ptr + 0x3C, 2)
        pos = idx * S
        sub = np.zeros(S, dtype=np.int64)
        for i in range(min(tap_count, 8)):
            dpos = pos - tap_delays[i]
            if dpos <= 0:
                dpos += cbuf_len
            dlen = S
            delayed = [0] * S
            if dpos + S > cbuf_len:
                dlen = cbuf_len - dpos
                if dlen < 0:
                    dlen = 0
                delayed[dlen:] = self.mx_s16s(cbuf_ptr, S - dlen)
            delayed[:dlen] = self.mx_s16s(cbuf_ptr + dpos * 2, dlen)
            sub = np.clip(sub + ((np.array(delayed, dtype=np.int64) * tap_gains[i] + 0x4000) >> 15), -32768, 32767)
        L, R, C = mixer[0], mixer[1], mixer[2]
        if not v2:
            mixer[0] = np.clip(L + sub, -32768, 32767)
            mixer[1] = np.clip(R + sub, -32768, 32767)
        else:
            v1 = (sub * g0) >> 16
            v2_ = (sub * g1) >> 16
            mixer[0] = np.clip(L + v1, -32768, 32767)
            mixer[1] = np.clip(R + v1, -32768, 32767)
            mixer[2] = np.clip(C + v2_, -32768, 32767)
        buf = np.concatenate([np.array(state['last4'], dtype=np.int64), sub])        # buffer[0..3] = last4, subframe
        state['last4'] = [int(x) for x in sub[S - 4:]]
        x = buf[1:]                                                                    # buffer + 1
        h = [(hgain * hco[k]) >> 15 for k in range(4)]
        acc = (h[0] * x[0:S] + h[1] * x[1:S + 1] + h[2] * x[2:S + 2] + h[3] * x[3:S + 3]) >> 15
        mixer[3] = np.clip(mixer[3] + acc, -32768, 32767)
        self.mx_store_u16(cbuf_ptr + pos * 2, [int(v) for v in mixer[3]])

    def mx_write_lr(self, mixer, bl, br, output_ptr):
        S = self.MX_SUB
        l = np.clip(mixer[0] + bl, -32768, 32767) & 0xFFFF
        r = np.clip(mixer[1] + br, -32768, 32767) & 0xFFFF
        out = np.empty(S * 2, dtype='>u2')
        out[0::2] = l
        out[1::2] = r
        self.m.dram_write_bytes(output_ptr & 0xFFFFFF, out.tobytes())

    def mx_init_v1(self, bv):
        S = self.MX_SUB
        base_cc0 = _clamp16(bv[2])
        base_e50 = _clamp16(bv[3])
        cc0 = self.mx_cc0
        left = np.clip(cc0 + base_cc0, -32768, 32767)
        right = np.clip(-cc0 - base_cc0, -32768, 32767)
        mixer = [left, right, np.zeros(S, dtype=np.int64), np.full(S, base_e50, dtype=np.int64)]
        return mixer

    def mx_task_v1(self):
        S = self.MX_SUB
        sp = self.m.spmem
        sfd_ptr = sp[0xFF0 >> 2]
        sfd_count = sp[0xFF4 >> 2]
        state_ptr = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 8) & 0xFFFFFF, 4))[0]
        bv = self.mx_load_base_vol(state_ptr + 0x100)
        self.mx_cc0 = np.array(self.mx_s16s(state_ptr + 0x110, S), dtype=np.int64)
        state = {'last4': self.mx_s16s(state_ptr + 0x290, 4)}
        while True:
            sfx_index = self.mx_u16s(sfd_ptr + 2, 1)[0]
            voice_mask = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 4) & 0xFFFFFF, 4))[0]
            sfx_ptr = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 0xC) & 0xFFFFFF, 4))[0]
            vp = sfd_ptr + 0x10
            self.mx_update_base_vol(bv, voice_mask, state_ptr, 0, 0)
            mixer = self.mx_init_v1(bv)
            output_ptr = self.mx_voice_stage(mixer, vp, state_ptr)
            self.mx_sfx_stage(mixer, state, sfx_ptr, sfx_index, False)
            self.mx_write_lr(mixer, _clamp16(bv[0]), _clamp16(bv[1]), output_ptr)
            self.mx_cc0 = mixer[2]
            self.tr('S 0 %u 0\n' % (S * 4))
            sfd_count -= 1
            if sfd_count == 0:
                break
            sfd_ptr += 0x10 + self.MX_VOICES * self.MX_VSIZE
            state_ptr = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 8) & 0xFFFFFF, 4))[0]
        self.mx_save_base_vol(bv, state_ptr + 0x100)
        self.mx_store_u16(state_ptr + 0x110, [int(v) for v in self.mx_cc0])
        self.mx_store_u16(state_ptr + 0x290, state['last4'])
        self.m.rsp_break(0x200)

    def mx_task_v2(self):
        S = self.MX_SUB
        sp = self.m.spmem
        sfd_ptr = sp[0xFF0 >> 2]
        sfd_count = sp[0xFF4 >> 2]
        while True:
            sfx_index = self.mx_u16s(sfd_ptr + 2, 1)[0]
            voice_mask = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 4) & 0xFFFFFF, 4))[0]
            state_ptr = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 8) & 0xFFFFFF, 4))[0]
            sfx_ptr = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 0xC) & 0xFFFFFF, 4))[0]
            vp = sfd_ptr + 0x28
            ptr_10 = struct.unpack('>I', self.m.dram_read_bytes((sfd_ptr + 0x10) & 0xFFFFFF, 4))[0]
            mask_15 = self.mx_u8(sfd_ptr + 0x15)
            mask_16 = self.mx_u16s(sfd_ptr + 0x16, 1)[0]
            ptr_18, ptr_1c, ptr_20, ptr_24 = self.mx_u32s(sfd_ptr + 0x18, 4)[0], self.mx_u32s(sfd_ptr + 0x1C, 1)[0], \
                self.mx_u32s(sfd_ptr + 0x20, 1)[0], self.mx_u32s(sfd_ptr + 0x24, 1)[0]
            bv = self.mx_load_base_vol(state_ptr + 0x100)
            state = {'last4': self.mx_s16s(state_ptr + 0x110, 4)}
            self.mx_update_base_vol(bv, voice_mask, state_ptr, mask_15, ptr_24)
            mixer = [np.full(S, _clamp16(bv[k]), dtype=np.int64) for k in range(4)]
            output_ptr = self.mx_voice_stage(mixer, vp, state_ptr)
            self.mx_sfx_stage(mixer, state, sfx_ptr, sfx_index, True)
            for k in range(3):
                self.m.dram_write_bytes((output_ptr + k * 2 * S) & 0xFFFFFF,
                                        np.array(mixer[k], dtype='>i2').tobytes())
            self.mx_save_base_vol(bv, state_ptr + 0x100)
            self.mx_store_u16(state_ptr + 0x110, state['last4'])
            if mask_16:
                self.mx_interleave_v2(mask_16, ptr_18, ptr_1c, ptr_20)
            self.tr('S 0 %u 0\n' % (S * 4))
            sfd_count -= 1
            if sfd_count == 0:
                break
            sfd_ptr += 0x28 + self.MX_VOICES * self.MX_VSIZE
        self.m.rsp_break(0x200)

    def mx_interleave_v2(self, mask_16, ptr_18, ptr_1c, output_ptr):
        S = self.MX_SUB
        sub = np.zeros(S, dtype=np.int64)
        v = np.array(self.mx_s16s(ptr_1c, S), dtype=np.int64)
        left = v.copy()
        right = np.clip(-v, -32768, 32767)
        for k in range(8):
            if not (mask_16 >> k) & 1:
                continue
            address = struct.unpack('>I', self.m.dram_read_bytes((ptr_18 + 8 * k) & 0xFFFFFF, 4))[0]
            hgain = self.mx_s16s(ptr_18 + 8 * k + 4, 1)[0]
            a = np.array(self.mx_s16s(address, S), dtype=np.int64)
            b = np.array(self.mx_s16s(address + 2 * S, S), dtype=np.int64)
            c = np.array(self.mx_s16s(address + 4 * S, S), dtype=np.int64)
            left = np.clip(left + ((a * hgain + 0x4000) >> 15), -32768, 32767)
            right = np.clip(right + ((b * hgain + 0x4000) >> 15), -32768, 32767)
            sub = np.clip(sub + ((c * hgain + 0x4000) >> 15), -32768, 32767)
        out = np.empty(S * 2, dtype='>u2')
        out[0::2] = left & 0xFFFF
        out[1::2] = right & 0xFFFF
        self.m.dram_write_bytes(output_ptr & 0xFFFFFF, out.tobytes())
        self.mx_store_u16(ptr_1c, [int(x) for x in sub])


class Hle(AlistExtra, MusyxMixin):
    DMEM_BASE = 0x5C0

    def __init__(self, m):
        self.m = m
        self.buf = bytearray(0x1000)
        self.s = np.frombuffer(self.buf, dtype='>i2')
        self.segments = [0] * 16
        self.inn = self.out = self.count = 0
        self.dry_right = self.wet_left = self.wet_right = 0
        self.dry = self.wet = 0
        self.vol = [0, 0]
        self.target = [0, 0]
        self.rate = [0, 0]
        self.loop = 0
        self.table = [0] * 384      # 128 + нули: как в C чтение книги за концом таблицы
        self.cached = {}
        self.ucode_printed = False
        # трассировка
        self.t = None                   # список строк
        self.pcm = None                 # bytearray
        self.voices = {}
        self.cov = {}
        self.cur_table = 0
        self.adpcm_seen = 0
        self.adpcm_init = 0
        self.last_load_in = 0
        self.last_load_cnt = 0
        self.unsupported = False
        self.env_values = [0, 0, 0]
        self.env_steps = [0, 0, 0]
        self.filter_count = 0
        self.filter_lut = [0, 0]
        self.mute_mask = 0
        self.mx_prev = {}
        self.mx_cc0 = None

    # ── доступ к DRAM как к логическому BE ──
    def dram_u16(self, a):
        w = self.m.rd[((a & 0xFFFFFF) & ~3) >> 2]
        return (w >> 16) & 0xFFFF if not (a & 2) else w & 0xFFFF

    def dram_u32(self, a):
        return self.m.rd[(a & 0xFFFFFF) >> 2]

    def dram_set_u16(self, a, v):
        a &= 0xFFFFFF
        i = (a & ~3) >> 2
        w = self.m.rd[i]
        if a & 2:
            w = (w & 0xFFFF0000) | (v & 0xFFFF)
        else:
            w = (w & 0x0000FFFF) | ((v & 0xFFFF) << 16)
        self.m.rd[i] = w

    def get_address(self, so):
        seg = (so >> 24) & 0x3F
        off = so & 0xFFFFFF
        if seg >= 16:
            return off
        return (self.segments[seg] + off) & M32

    def sample(self, pos):
        return int(self.s[pos & 0x7FF])

    def set_sample(self, pos, v):
        self.s[pos & 0x7FF] = v

    # ── выполнение задачи ──
    def execute(self):
        m = self.m
        sp = m.spmem
        uc_start = sp[0xFD0 >> 2]
        uc_dstart = sp[0xFD8 >> 2]
        uc_dsize = sp[0xFDC >> 2]
        key = (uc_start, uc_dstart, uc_dsize)
        fn = self.cached.get(key, 0)
        if fn == 0:
            fn = self.detect()
            self.cached[key] = fn
        fn()

    def is_task(self):
        return self.m.spmem[0xFCC >> 2] <= 0x1000

    def detect(self):
        m = self.m
        sp = m.spmem
        if self.is_task():
            typ = sp[0xFC0 >> 2]
            if typ == 2:
                fn = self.detect_audio()
                if fn:
                    return fn
            # распознавание "обычных" задач (JPEG и т.п.) не поддерживается
            return self.unknown_task
        return self.unknown_ucode

    def detect_audio(self):
        m = self.m
        ud = m.spmem[0xFD8 >> 2]
        d = self.dram_u32
        if not self.ucode_printed:
            self.ucode_printed = True
            if self.t is not None:
                self.t.append('# ucode %08x %08x %08x %08x\n' % (d(ud), d(ud + 0x10), d(ud + 0x28), d(ud + 0x30)))
        if d(ud) == 1:
            if d(ud + 0x30) == 0xF0000F00:
                v = d(ud + 0x28)
                if v == 0x1E24138C:
                    return lambda: self.process_audio(False)
                if v == 0x1DC8138C or v == 0x1E3C1390:
                    return lambda: self.process_audio(True)       # GE/BC: ENVMIXER_GE не реализован - как ABI1
            else:
                v = d(ud + 0x10)
                nead = {0x11181350: 'mk', 0x111812E0: 'sfj', 0x110412AC: 'wrjb', 0x110412CC: 'sf',
                        0x1CD01250: 'fz', 0x1F08122C: 'ys', 0x1F38122C: '1080', 0x1F681230: 'oot',
                        0x1F801250: 'mm', 0x109411F8: 'mmb', 0x1EAC11B8: 'ac', 0x1F4C1230: 'fz'}
                if v == 0x00010010:
                    return self.mx_task_v2           # MusyX v2 (Indiana Jones, Battle for Naboo)
                if v in nead:
                    tbl = self.n2_table(nead[v])
                    return lambda: self.run_alist(tbl)
        else:
            v = d(ud + 0x10)
            if v == 0x00000001:
                return self.mx_task_v1               # MusyX v1 (Rogue Squadron, 007 TWINE, ...)
            naudio = {0x0000127C: 'naudio', 0x00001280: 'bk', 0x1C58126C: 'dk',
                      0x1AE8143C: 'mp3', 0x1AB0140C: 'cbfd'}
            if v in naudio:
                tbl = self.n3_table(naudio[v])
                return lambda: self.run_alist(tbl)
        self.unsupported = True
        return self.unknown_task

    def unknown_task(self):
        # графическую задачу не исполняем, но игра ждёт прерывание DP (full sync) от RDP:
        # без него её планировщик встаёт и аудио-задачи перестают приходить (gfxdone.patch)
        if self.m.spmem[0xFC0 >> 2] == 1:
            self.m.mi[2] |= MI_INTR_DP
        self.m.rsp_break(0x200)

    def unknown_ucode(self):
        pass

    def process_audio(self, ge):
        m = self.m
        self.segments = [0] * 16
        if self.t is not None:
            self.t.append('F\n')
        sp = m.spmem
        data_ptr = sp[0xFF0 >> 2]
        data_size = sp[0xFF4 >> 2]
        raw = m.dram_read_bytes(data_ptr & 0xFFFFFF, data_size & ~7)
        words = struct.unpack('>%dI' % (len(raw) // 4), raw)
        for i in range(0, len(words), 2):
            w1, w2 = words[i], words[i + 1]
            acmd = (w1 >> 24) & 0x7F
            if acmd < 16:
                self.abi[acmd](w1, w2)
        m.rsp_break(0x200)

    @property
    def abi(self):
        a = getattr(self, '_abi', None)
        if a is None:
            a = self._abi = [self.c_spnoop, self.c_adpcm, self.c_clearbuff, self.c_envmixer,
                             self.c_loadbuff, self.c_resample, self.c_savebuff, self.c_segment,
                             self.c_setbuff, self.c_setvol, self.c_dmemmove, self.c_loadadpcm,
                             self.c_mixer, self.c_interleave, self.c_polef, self.c_setloop]
        return a

    # ── вспомогательные ──
    def tr(self, s):
        if self.t is not None:
            self.t.append(s)

    def voice(self, addr):
        addr &= M32
        v = self.voices.get(addr)
        if v is None:
            v = self.voices[addr] = [0, 0, 0]       # cum, hash, has
        return v

    def cov_len(self, h):
        return self.cov.get(h, 0)

    def pcm_write_samples(self, dmem_addr, nsamp):
        off = len(self.pcm)
        i0 = (dmem_addr & 0xFFF) >> 1
        idx = (i0 + np.arange(nsamp)) & 0x7FF
        self.pcm += self.s[idx].astype('<i2').tobytes()
        return off

    # ── команды ──
    def c_spnoop(self, w1, w2):
        pass

    def c_clearbuff(self, w1, w2):
        dmem = (w1 + self.DMEM_BASE) & 0xFFFF
        count = w2 & 0xFFF
        if count == 0:
            return
        count = _align(count, 16)
        for i in range(count):
            self.buf[(dmem + i) & 0xFFF] = 0

    def c_envmixer(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        address = self.get_address(w2)
        self.envmix_exp(flags & A_INIT, flags & A_AUX, address)
        self.adpcm_seen = 0
        self.adpcm_init = 0
        self.last_load_cnt = 0
        if self.t is not None:
            raw = self.m.dram_read_bytes(address & 0xFFFFFF, 80)
            wd = struct.unpack('>20I', raw)
            sw = lambda x: x - (1 << 32) if x & 0x80000000 else x
            self.t.append('E %u %u %u %u %d %d %d %d %d %d %d %d %x\n' % (
                flags, self.inn, self.out, self.count,
                sw(wd[8]) >> 16, sw(wd[9]) >> 16, sw(wd[2]) >> 16, sw(wd[3]) >> 16,
                sw(wd[4]), sw(wd[5]), _s16(wd[1]), _s16(wd[0]), address))

    def envmix_exp(self, init, aux, address):
        raw = self.m.dram_read_bytes(address & 0xFFFFFF, 80)
        wd = list(struct.unpack('>20I', raw))
        sw = lambda x: x - (1 << 32) if x & 0x80000000 else x
        dry, wet = self.dry, self.wet
        vol, target, rate = self.vol, self.target, self.rate
        if init:
            rv = [vol[0] << 16, vol[1] << 16]
            rt = [target[0] << 16, target[1] << 16]
            er = [rate[0], rate[1]]
            es = [_to_i32(vol[0] * rate[0]), _to_i32(vol[1] * rate[1])]
        else:
            wet = _s16(wd[0])
            dry = _s16(wd[1])
            rt = [sw(wd[2]), sw(wd[3])]
            er = [sw(wd[4]), sw(wd[5])]
            es = [sw(wd[6]), sw(wd[7])]
            rv = [sw(wd[8]), sw(wd[9])]
        step = [rt[0] - rv[0], rt[1] - rv[1]]
        count = self.count
        n_out = 4 if aux else 2
        nsamp = (count + 15) // 16 * 8
        lv = [0] * nsamp
        rvv = [0] * nsamp
        ptr = 0
        for y in range(0, count, 16):
            for i in (0, 1):
                if step[i] != 0:
                    es[i] = _to_i32((es[i] * er[i]) >> 16)
                    step[i] = (es[i] - rv[i]) >> 3
            for x in range(8):
                for i in (0, 1):
                    rv[i] += step[i]
                    reached = (rv[i] <= rt[i]) if step[i] <= 0 else (rv[i] >= rt[i])
                    if reached:
                        rv[i] = rt[i]
                        step[i] = 0
                lv[ptr] = rv[0] >> 16
                rvv[ptr] = rv[1] >> 16
                ptr += 1
        s = self.s
        nn = ptr
        if nn:
            L = np.array(lv[:nn], dtype=np.int64)
            Rr = np.array(rvv[:nn], dtype=np.int64)
            ib = ((self.inn & 0xFFF) >> 1)
            src = s[(ib + np.arange(nn)) & 0x7FF].astype(np.int64)
            gains = [np.clip((L * dry + 0x4000) >> 15, -32768, 32767),
                     np.clip((Rr * dry + 0x4000) >> 15, -32768, 32767),
                     np.clip((L * wet + 0x4000) >> 15, -32768, 32767),
                     np.clip((Rr * wet + 0x4000) >> 15, -32768, 32767)]
            dst = [self.out, self.dry_right, self.wet_left, self.wet_right]
            for k in range(n_out):
                o = ((dst[k] & 0xFFF) >> 1)
                idx = (o + np.arange(nn)) & 0x7FF
                cur = s[idx].astype(np.int64)
                s[idx] = np.clip(cur + ((src * gains[k]) >> 15), -32768, 32767)
        wd[0] = (wd[0] & 0xFFFF0000) | (wet & 0xFFFF)
        wd[1] = (wd[1] & 0xFFFF0000) | (dry & 0xFFFF)
        wd[2] = rt[0] & M32
        wd[3] = rt[1] & M32
        wd[4] = er[0] & M32
        wd[5] = er[1] & M32
        wd[6] = es[0] & M32
        wd[7] = es[1] & M32
        wd[8] = rv[0] & M32
        wd[9] = rv[1] & M32
        self.m.dram_write_bytes(address & 0xFFFFFF, struct.pack('>20I', *wd))

    def c_resample(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        pitch = w1 & 0xFFFF
        address = self.get_address(w2)
        if self.t is not None:
            if (flags & A_INIT) and not self.adpcm_init:
                v0 = self.voice((address - 0x40) & M32)
                if v0[0] > 0:
                    v0[0] = 0
                    v0[2] = 0
            if not self.adpcm_seen and self.last_load_cnt > 0:
                cnt = self.last_load_cnt
                ib = self.last_load_in
                vaddr = (address - 0x40) & M32
                v = self.voice(vaddr)
                off = -1
                if flags & A_INIT:
                    v[0] = 0
                    v[1] = 0
                    v[2] = 0
                cum = v[0]
                if not v[2]:
                    h = (2166136261 ^ 0x50434D00) & M32
                    h = (h * 16777619) & M32
                    for i in range(16):
                        if i * 2 < cnt:
                            h ^= self.sample(((ib & 0xFFF) >> 1) + i) & 0xFFFF
                            h = (h * 16777619) & M32
                    v[1] = h
                    v[2] = 1
                h = v[1]
                if cum + cnt > self.cov_len(h) and self.cov_len(h) < 262144:
                    off = self.pcm_write_samples(ib, cnt // 2)
                    self.cov[h] = cum + cnt
                self.t.append('A %u %u %u %u %x %x %x %d %d %u\n' % (flags & A_INIT, ib, 0, cnt, vaddr, 0, h, cum, off, 1))
                v[0] += cnt
            self.t.append('R %u %u %x %u %u %u\n' % (flags, pitch, address, self.inn, self.out, self.count))
        self.resample(flags & A_INIT, self.out, self.inn, _align(self.count, 16), pitch << 1, address)

    def resample(self, init, dmemo, dmemi, count, pitch, address):
        ipos = ((dmemi >> 1) - 4) & 0xFFFF
        opos = dmemo >> 1
        count >>= 1
        if init:
            for k in range(4):
                self.set_sample(ipos + k, 0)
            accu = 0
        else:
            for k in range(4):
                self.set_sample(ipos + k, _s16(self.dram_u16(address + 2 * k)))
            accu = self.dram_u16(address + 8)
        if count > 0:
            s = self.s
            k = np.arange(count, dtype=np.int64)
            tot = accu + pitch * k
            lut_i = ((tot & 0xFC00) >> 8)
            ip = (ipos + (tot >> 16)) & 0xFFFF
            L = np.array(RESAMPLE_LUT, dtype=np.int64)
            L = np.where(L >= 0x8000, L - 0x10000, L)
            li = lut_i
            ssum = np.zeros(count, dtype=np.int64)
            for j in range(4):
                ssum += s[(ip + j) & 0x7FF].astype(np.int64) * L[li + j]
            res = np.clip(ssum >> 15, -32768, 32767)
            oi = (opos + k) & 0x7FF
            end_tot = accu + pitch * count
            ipos_end = (ipos + (end_tot >> 16)) & 0xFFFF
            accu_end = end_tot & 0xFFFF
            s[oi] = res
        else:
            ipos_end = ipos
            accu_end = accu
        for kk in range(4):
            self.dram_set_u16(address + 2 * kk, self.sample(ipos_end + kk))
        self.dram_set_u16(address + 8, accu_end)

    def c_setvol(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        self.tr('V %08x %08x\n' % (w1, w2))
        if flags & A_AUX:
            self.dry = _s16(w1)
            self.wet = _s16(w2)
        else:
            lr = 0 if (flags & A_LEFT) else 1
            if flags & A_VOL:
                self.vol[lr] = _s16(w1)
            else:
                self.target[lr] = _s16(w1)
                self.rate[lr] = _to_i32(w2)

    def c_setloop(self, w1, w2):
        self.loop = self.get_address(w2)
        self.tr('L %x\n' % self.loop)

    def c_adpcm(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        address = self.get_address(w2)
        cnt32 = _align(self.count, 32)
        self.adpcm(flags & A_INIT, flags & A_LOOP, self.out, self.inn, cnt32, address)
        self.adpcm_seen = 1
        self.adpcm_init = flags & A_INIT
        if self.t is not None:
            cnt = cnt32
            ob = (self.out + 32) & 0xFFFF
            off = -1
            v = self.voice(address)
            if flags & A_INIT:
                v[0] = 0
                v[1] = 0
                v[2] = 0
            cum = v[0]
            if not v[2]:
                h = (2166136261 ^ self.cur_table) & M32
                h = (h * 16777619) & M32
                for i in range(16):
                    if i * 2 < cnt:
                        h ^= self.sample(((ob & 0xFFF) >> 1) + i) & 0xFFFF
                        h = (h * 16777619) & M32
                v[1] = h
                v[2] = 1
            h = v[1]
            if cum + cnt > self.cov_len(h) and self.cov_len(h) < 262144:
                off = self.pcm_write_samples(ob, cnt // 2)
                self.cov[h] = cum + cnt
            self.t.append('A %u %u %u %u %x %x %x %d %d %u\n' % (
                flags, self.inn, self.out, cnt, address, self.loop, h, cum, off, 1 if (cum + cnt) < 0x7FFFFFFF else 0))
            v[0] += cnt

    def adpcm(self, init, loop, dmemo, dmemi, count, last_frame_address, two_bit=False):
        tbl = self.table
        if init:
            last = [0] * 16
        else:
            a = self.loop if loop else last_frame_address
            last = [_s16(self.dram_u16(a + 2 * i)) for i in range(16)]
        buf = self.buf
        s = self.s
        o = (dmemo & 0xFFF) >> 1
        for i in range(16):
            s[(o + i) & 0x7FF] = last[i]
        o += 16
        di = dmemi & 0xFFF
        count_left = count
        while count_left != 0:
            code = buf[di & 0xFFF]
            di += 1
            scale = (code & 0xF0) >> 4
            cb = (code & 0xF) << 4
            frame = [0] * 16
            if two_bit:
                rshift = 14 - scale if scale < 14 else 0
                for i in range(4):
                    byte = buf[(di + i) & 0xFFF]
                    frame[4 * i] = _s16((byte & 0xC0) << 8) >> rshift
                    frame[4 * i + 1] = _s16((byte & 0x30) << 10) >> rshift
                    frame[4 * i + 2] = _s16((byte & 0x0C) << 12) >> rshift
                    frame[4 * i + 3] = _s16((byte & 0x03) << 14) >> rshift
                di += 4
            else:
                rshift = 12 - scale if scale < 12 else 0
                for i in range(8):
                    byte = buf[(di + i) & 0xFFF]
                    frame[2 * i] = _s16((byte & 0xF0) << 8) >> rshift
                    frame[2 * i + 1] = _s16((byte & 0x0F) << 12) >> rshift
                di += 8
            book1 = tbl[cb:cb + 8]
            book2 = tbl[cb + 8:cb + 16]
            l1 = last[14]
            l2 = last[15]
            for i in range(8):
                accu = (frame[i] << 11) + book1[i] * l1 + book2[i] * l2
                for j in range(i):
                    accu += book2[j] * frame[i - 1 - j]
                last[i] = _clamp16(accu >> 11)
            l1 = last[6]
            l2 = last[7]
            for i in range(8):
                accu = (frame[8 + i] << 11) + book1[i] * l1 + book2[i] * l2
                for j in range(i):
                    accu += book2[j] * frame[8 + i - 1 - j]
                last[8 + i] = _clamp16(accu >> 11)
            for i in range(16):
                s[(o + i) & 0x7FF] = last[i]
            o += 16
            count_left -= 32
        for i in range(16):
            self.dram_set_u16(last_frame_address + 2 * i, last[i])

    def c_loadbuff(self, w1, w2):
        address = self.get_address(w2)
        self.tr('B %u %u %x\n' % (self.inn, self.count, address))
        self.last_load_in = self.inn
        self.last_load_cnt = self.count
        if self.count == 0:
            return
        dmem = self.inn & ~3 & 0xFFF
        addr = address & ~7
        n = _align(self.count, 8)
        n = min(n, 0x1000 - dmem)
        self.buf[dmem:dmem + n] = self.m.dram_read_bytes(addr & 0xFFFFFF, n)

    def c_savebuff(self, w1, w2):
        address = self.get_address(w2)
        self.tr('S %u %u %x\n' % (self.out, self.count, address))
        if self.count == 0:
            return
        dmem = self.out & ~3 & 0xFFF
        addr = address & ~7
        n = _align(self.count, 8)
        n = min(n, 0x1000 - dmem)
        self.m.dram_write_bytes(addr & 0xFFFFFF, bytes(self.buf[dmem:dmem + n]))

    def c_segment(self, w1, w2):
        seg = (w2 >> 24) & 0x3F
        if seg < 16:
            self.segments[seg] = w2 & 0xFFFFFF

    def c_setbuff(self, w1, w2):
        flags = (w1 >> 16) & 0xFF
        B = self.DMEM_BASE
        if flags & A_AUX:
            self.dry_right = (w1 + B) & 0xFFFF
            self.wet_left = ((w2 >> 16) + B) & 0xFFFF
            self.wet_right = (w2 + B) & 0xFFFF
        else:
            self.inn = (w1 + B) & 0xFFFF
            self.out = ((w2 >> 16) + B) & 0xFFFF
            self.count = w2 & 0xFFFF

    def c_dmemmove(self, w1, w2):
        B = self.DMEM_BASE
        dmemi = (w1 + B) & 0xFFFF
        dmemo = ((w2 >> 16) + B) & 0xFFFF
        count = w2 & 0xFFFF
        if count == 0:
            return
        count = _align(count, 16)
        for i in range(count):
            self.buf[(dmemo + i) & 0xFFF] = self.buf[(dmemi + i) & 0xFFF]

    def c_loadadpcm(self, w1, w2):
        count = w1 & 0xFFFF
        address = self.get_address(w2)
        self.tr('T %x %u\n' % (address, count))
        self.cur_table = address
        n = _align(count, 8) >> 1
        for i in range(min(n, 128)):
            self.table[i] = _s16(self.dram_u16(address + 2 * i))

    def c_interleave(self, w1, w2):
        B = self.DMEM_BASE
        left = ((w2 >> 16) + B) & 0xFFFF
        right = (w2 + B) & 0xFFFF
        if self.count == 0:
            return
        n = _align(self.count, 16) >> 2          # групп по 2 выборки
        s = self.s
        L = s[((left >> 1) + np.arange(n * 2)) & 0x7FF].copy()
        Rr = s[((right >> 1) + np.arange(n * 2)) & 0x7FF].copy()
        out = np.empty(n * 4, dtype=np.int16)
        out[0::2] = L
        out[1::2] = Rr
        s[((self.out >> 1) + np.arange(n * 4)) & 0x7FF] = out

    def c_mixer(self, w1, w2):
        B = self.DMEM_BASE
        gain = _s16(w1)
        dmemi = ((w2 >> 16) + B) & 0xFFFF
        dmemo = (w2 + B) & 0xFFFF
        if self.count == 0:
            return
        n = _align(self.count, 32) >> 1
        s = self.s
        src = s[((dmemi >> 1) + np.arange(n)) & 0x7FF].astype(np.int64)
        idx = ((dmemo >> 1) + np.arange(n)) & 0x7FF
        cur = s[idx].astype(np.int64)
        s[idx] = np.clip(cur + ((src * gain) >> 15), -32768, 32767)

    def c_polef(self, w1, w2):
        if self.count == 0:
            return
        self.a_polef((w1 >> 16) & A_INIT, self.out, self.inn, self.count, w1 & 0xFFFF, self.get_address(w2))


def _to_i32(x):
    return ((x & M32) ^ 0x80000000) - 0x80000000


# ═══════════════════════════════════════════════════════════════
# Запуск
# ═══════════════════════════════════════════════════════════════

class Enough(Exception):
    pass


def _attach(m):
    hle = Hle(m)
    m.hle = hle

    def run_rsp_task(task_type):
        if task_type in (1, 2):
            hle.execute()
        else:
            pass

    m.run_rsp_task = run_rsp_task

    def push_audio(address, length):
        m.vi_without_audio = 0
        data = m.dram_read_bytes(address & 0xFFFFFF, length)
        m.audio_out.append(data)
        m.audio_samples += length // 4
        if m.target_samples and m.audio_samples >= m.target_samples:
            m.stop_request = True

    m.push_audio = push_audio


def _debug_dump_fn(m):
    def dump(pc):
        if m.icount % 200000 == 0 and m.dump is not None:
            h = 0
            for r in m.R:
                h = (h * 1000003 + (r & M64)) & M64
            c = m.cp0
            m.dump.write('%d pc=%08x cnt=%d h=%016x hi=%x lo=%x st=%08x cause=%08x\n' % (
                m.icount, pc, c[CP0_COUNT], h, m.hi & M64, m.lo & M64, c[CP0_STATUS], c[CP0_CAUSE]))
    m._debug_dump = dump


def run(m, target_samples=0, max_instr=0):
    """Основной цикл. Выходит по исключению Enough (набрано target_samples отсчётов звука)."""
    m.target_samples = target_samples
    icache = m.icache
    pc = m.pc
    debug = m.debug_count
    try:
        while True:
            try:
                if debug:
                    while True:
                        f = icache.get(pc)
                        if f is None:
                            f = m.decode_at(pc)
                        m.icount += 1
                        m._debug_dump(pc)
                        if max_instr and m.icount >= max_instr:
                            raise Enough()
                        pc = f(pc)
                else:
                    while True:
                        f = icache.get(pc)
                        if f is None:
                            f = m.decode_at(pc)
                        pc = f(pc)
            except CpuException as e:
                pc = m.handle_exception(e, pc)
            except Redirect as e:
                pc = e.pc
    except Enough:
        if m.resume_pc is not None:
            pc = m.resume_pc
            m.resume_pc = None
    m.pc = pc


def load(path):
    u = Usf()
    u.load(path)
    m = N64(u)
    _attach(m)
    return m


def resample_audio(x, rate_in, rate_out=44100, half=16):
    """Стерео float (N, 2) -> другая частота; оконный sinc (Блэкман), только numpy."""
    if rate_in == rate_out:
        return x
    n_out = int(len(x) * rate_out / rate_in)
    ratio = rate_in / float(rate_out)
    cutoff = min(1.0, rate_out / float(rate_in))
    taps = 2 * half
    pos = np.arange(n_out, dtype=np.float64) * ratio
    base = np.floor(pos).astype(np.int64)
    frac = pos - base
    k = np.arange(-half + 1, half + 1)
    xp = np.concatenate((np.zeros((half, x.shape[1])), x, np.zeros((half + 1, x.shape[1]))))
    out = np.empty((n_out, x.shape[1]))
    for s0 in range(0, n_out, 20000):
        s1 = min(n_out, s0 + 20000)
        d = k[None, :] - frac[s0:s1, None]
        w = np.sinc(d * cutoff) * cutoff * (0.42 + 0.5 * np.cos(np.pi * d / half) + 0.08 * np.cos(2 * np.pi * d / half))
        idx = base[s0:s1, None] + k[None, :] + half
        out[s0:s1] = np.einsum('jk,jkc->jc', w, xp[idx])
    return out


def trace(path, prefix, seconds, raw_path=None, progress=None):
    m = load(path)
    m.hle.t = []
    m.hle.pcm = bytearray()
    # как usftrace: сначала запускаем эмуляцию до первого куска звука, затем выводим "# rate"
    m.target_samples = 1
    run(m, target_samples=1)
    m.hle.t.append('# rate %d\n' % m.audio_rate)
    rate = m.audio_rate or 32000
    total = int(seconds * rate)
    m.audio_samples = 0
    m.audio_out = [m.audio_out[0]] if m.audio_out else []
    if m.audio_out:
        m.audio_samples = len(m.audio_out[0]) // 4
    done = False
    chunk = max(4096, rate // 2)
    t = m.target_samples
    while m.audio_samples < total:
        run(m, target_samples=min(total, m.audio_samples + chunk))
        if progress:
            progress(min(1.0, m.audio_samples / float(total)))
    with open(prefix + '.trace', 'w') as f:
        f.write(''.join(m.hle.t))
    with open(prefix + '.pcm', 'wb') as f:
        f.write(bytes(m.hle.pcm))
    if raw_path:
        data = b''.join(m.audio_out)
        a = np.frombuffer(data, dtype='>i2').reshape(-1, 2).astype(np.float64)
        a = a[:total]
        y = resample_audio(a, rate, 44100)
        np.clip(np.round(y), -32768, 32767).astype('<i2').tofile(raw_path)
    if m.hle.unsupported:
        print("usf_pyemu: аудио-микрокод этой игры не поддерживается (поддержаны ABI1, ABI2, ABI3)", file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    trace(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
