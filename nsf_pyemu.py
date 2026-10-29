#!/usr/bin/env python3
"""
Встроенный эмулятор NSF (NES) на чистом Python + numpy: ничего собирать не нужно.

  * MOS 6502 (с неофициальными опкодами, такты считаются по таблице) - интерпретатор
    генерируется из шаблонов один раз и работает на локальных переменных;
  * APU 2A03: два прямоугольных канала (огибающая, sweep, счётчик длины), треугольник
    (линейный счётчик), шум (LFSR 15 бит, короткий режим), DMC (дельта-модуляция и
    прямая запись $4011); кадровый счётчик 4/5 шагов;
  * банки 4 КБ ($5FF8-$5FFF), PRG-RAM, зеркала ОЗУ;
  * рендер в WAV по журналу событий: каждый канал интегрируется точно (усреднение по
    отсчёту), потом нелинейный микшер NES.

Эмулятор пишет не звук, а ЖУРНАЛ СОБЫТИЙ по каналам: (такт, trig, a, b, уровень)
при каждом изменении того, что слышно (высота, скважность, громкость), и trig=True при
перезапуске ноты записью в $4003/$4007/$400B/$400F. Из него конвертер делает ноты,
а render_audio() - звук.

Расширения (VRC6/VRC7/FDS/MMC5/N163/5B) не эмулируются: пишутся только в память и
игнорируются, звук будет только от 2A03.
"""

import struct

import numpy as np

NTSC_CLOCK = 1789773
PAL_CLOCK = 1662607
DENDY_CLOCK = 1773448

LENGTH_TABLE = [10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
                12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30]
NOISE_PERIOD = {False: [4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068],
                True: [4, 8, 14, 30, 60, 88, 118, 148, 188, 236, 354, 472, 708, 944, 1890, 3778]}
DMC_PERIOD = {False: [428, 380, 340, 320, 286, 254, 226, 214, 190, 160, 142, 128, 106, 84, 72, 54],
              True: [398, 354, 316, 298, 276, 236, 210, 198, 176, 148, 132, 118, 98, 78, 66, 50]}
DUTY_SEQ = [[0, 1, 0, 0, 0, 0, 0, 0], [0, 1, 1, 0, 0, 0, 0, 0],
            [0, 1, 1, 1, 1, 0, 0, 0], [1, 0, 0, 1, 1, 1, 1, 1]]
TRI_SEQ = list(range(15, -1, -1)) + list(range(16))

HALT_ADDR = 0x4800            # здесь лежит "останов": опкод 0x02, возврат из подпрограммы

# Линейный микшер (как у libgme, по нему сверяются WAV других конвертеров): вклад канала
# на единицу его уровня (pulse/noise/треугольник 0..15), полная шкала = 1.0
MIX_PULSE = 0.0210
MIX_TRI = 0.02375
MIX_NOISE = 0.0127
DMC_GAIN = 1.385            # DMC у libgme: стандартная нелинейная кривая NES, умноженная на это


def dmc_curve(d):
    """Уровень ЦАП DMC (0..127) -> вклад в смесь."""
    d = np.asarray(d, dtype=np.float64)
    return DMC_GAIN * np.where(d > 0, 159.79 / (22638.0 / np.maximum(d, 1e-9) + 100.0), 0.0)


# ═══════════════════════════════════════════════════════════════
# NSF-файл
# ═══════════════════════════════════════════════════════════════

class NSF:
    pass


def _str(b):
    return b.split(b'\x00')[0].decode('latin-1', 'replace').strip()


def load_nsf(path):
    with open(path, 'rb') as f:
        d = f.read()
    if d[:5] != b'NESM\x1a':
        raise SystemExit(f"{path}: это не NSF")
    n = NSF()
    n.version = d[5]
    n.total_songs = d[6] or 1
    n.start_song = d[7] or 1
    n.load_addr, n.init_addr, n.play_addr = struct.unpack_from('<HHH', d, 8)
    n.title, n.artist, n.copyright = _str(d[0x0E:0x2E]), _str(d[0x2E:0x4E]), _str(d[0x4E:0x6E])
    n.ntsc_us = struct.unpack_from('<H', d, 0x6E)[0]
    n.banks = list(d[0x70:0x78])
    n.pal_us = struct.unpack_from('<H', d, 0x78)[0]
    n.pal = bool(d[0x7A] & 1)
    n.dual = bool(d[0x7A] & 2)
    n.chips = d[0x7B]
    n.data = d[0x80:]
    n.banked = any(n.banks)
    return n


CHIP_NAMES = [(1, 'VRC6'), (2, 'VRC7'), (4, 'FDS'), (8, 'MMC5'), (16, 'N163'), (32, 'Sunsoft 5B')]


def chips_of(n):
    return [name for bit, name in CHIP_NAMES if n.chips & bit]


# ═══════════════════════════════════════════════════════════════
# 6502: генератор интерпретатора
# ═══════════════════════════════════════════════════════════════
# Регистры - локальные переменные: a, x, y, sp, pc; флаги: c, ov (0/1), fn (0/128),
# fz (bool), fi, fd (0/1). Память - bytearray mem[65536]; ОЗУ ($0000-$07FF) зеркалится
# масками, ввод-вывод ($2000-$5FFF) идёт через rd_io/wr.

RD = "(mem[ea & 2047] if ea < 8192 else mem[ea] if ea >= 24576 else rd_io(ea, cyc))"

# режим -> (код вычисления ea, такты чтения, штраф за переход страницы, прямой доступ к mem)
MODES = {
    'imm': ("ea = pc; pc = (pc + 1) & 65535", 2, False, True),
    'zp': ("ea = mem[pc]; pc = (pc + 1) & 65535", 3, False, True),
    'zpx': ("ea = (mem[pc] + x) & 255; pc = (pc + 1) & 65535", 4, False, True),
    'zpy': ("ea = (mem[pc] + y) & 255; pc = (pc + 1) & 65535", 4, False, True),
    'abs': ("ea = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535", 4, False, False),
    'abx': ("t = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535; ea = (t + x) & 65535",
            4, True, False),
    'aby': ("t = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535; ea = (t + y) & 65535",
            4, True, False),
    'inx': ("t = (mem[pc] + x) & 255; pc = (pc + 1) & 65535; "
            "ea = mem[t] | (mem[(t + 1) & 255] << 8)", 6, False, False),
    'iny': ("t = mem[pc]; pc = (pc + 1) & 65535; t = mem[t] | (mem[(t + 1) & 255] << 8); "
            "ea = (t + y) & 65535", 5, True, False),
}
STORE_CYC = {'zp': 3, 'zpx': 4, 'zpy': 4, 'abs': 4, 'abx': 5, 'aby': 5, 'inx': 6, 'iny': 6}
RMW_CYC = {'zp': 5, 'zpx': 6, 'abs': 6, 'abx': 7, 'aby': 7, 'inx': 8, 'iny': 8}

NZ = "fn = {r} & 128; fz = {r} == 0"
ADC = ("t = a + v + c; ov = (~(a ^ v) & (a ^ t) & 128) >> 7; c = t >> 8; a = t & 255; "
       + NZ.format(r='a'))
CMP = "t = {R} - v; c = 1 if t >= 0 else 0; t &= 255; fn = t & 128; fz = t == 0"

# операции, читающие операнд v
READ_OPS = {
    'LDA': "a = v; " + NZ.format(r='a'),
    'LDX': "x = v; " + NZ.format(r='x'),
    'LDY': "y = v; " + NZ.format(r='y'),
    'AND': "a &= v; " + NZ.format(r='a'),
    'ORA': "a |= v; " + NZ.format(r='a'),
    'EOR': "a ^= v; " + NZ.format(r='a'),
    'ADC': ADC,
    'SBC': "v ^= 255; " + ADC,
    'CMP': CMP.format(R='a'),
    'CPX': CMP.format(R='x'),
    'CPY': CMP.format(R='y'),
    'BIT': "fn = v & 128; ov = (v >> 6) & 1; fz = (a & v) == 0",
    'LAX': "a = x = v; " + NZ.format(r='a'),
    'NOP': "pass",
    'ANC': "a &= v; " + NZ.format(r='a') + "; c = fn >> 7",
    'ALR': "a &= v; c = a & 1; a >>= 1; " + NZ.format(r='a'),
    'ARR': ("a &= v; a = (a >> 1) | (c << 7); " + NZ.format(r='a') +
            "; c = (a >> 6) & 1; ov = ((a >> 6) ^ (a >> 5)) & 1"),
    'AXS': "t = (a & x) - v; c = 1 if t >= 0 else 0; x = t & 255; " + NZ.format(r='x'),
}
# операции чтения-изменения-записи: меняют v, потом пишут
RMW_OPS = {
    'ASL': "c = v >> 7; v = (v << 1) & 255; " + NZ.format(r='v'),
    'LSR': "c = v & 1; v >>= 1; " + NZ.format(r='v'),
    'ROL': "t = (v << 1) | c; c = v >> 7; v = t & 255; " + NZ.format(r='v'),
    'ROR': "t = (v >> 1) | (c << 7); c = v & 1; v = t; " + NZ.format(r='v'),
    'INC': "v = (v + 1) & 255; " + NZ.format(r='v'),
    'DEC': "v = (v - 1) & 255; " + NZ.format(r='v'),
    'DCP': "v = (v - 1) & 255; WRITE; " + CMP.format(R='a'),
    'ISB': "v = (v + 1) & 255; WRITE; v ^= 255; " + ADC,
    'SLO': "c = v >> 7; v = (v << 1) & 255; WRITE; a |= v; " + NZ.format(r='a'),
    'RLA': "t = (v << 1) | c; c = v >> 7; v = t & 255; WRITE; a &= v; " + NZ.format(r='a'),
    'SRE': "c = v & 1; v >>= 1; WRITE; a ^= v; " + NZ.format(r='a'),
    'RRA': "t = (v >> 1) | (c << 7); c = v & 1; v = t; WRITE; " + ADC,
}
STORE_OPS = {'STA': 'a', 'STX': 'x', 'STY': 'y', 'SAX': 'a & x'}

BRANCH = {0x10: "not fn", 0x30: "fn", 0x50: "not ov", 0x70: "ov", 0x90: "not c", 0xB0: "c",
          0xD0: "not fz", 0xF0: "fz"}

IMPLIED = {
    0x18: "c = 0", 0x38: "c = 1", 0x58: "fi = 0", 0x78: "fi = 1", 0xB8: "ov = 0",
    0xD8: "fd = 0", 0xF8: "fd = 1",
    0xAA: "x = a; " + NZ.format(r='x'), 0xA8: "y = a; " + NZ.format(r='y'),
    0x8A: "a = x; " + NZ.format(r='a'), 0x98: "a = y; " + NZ.format(r='a'),
    0xBA: "x = sp; " + NZ.format(r='x'), 0x9A: "sp = x",
    0xE8: "x = (x + 1) & 255; " + NZ.format(r='x'), 0xC8: "y = (y + 1) & 255; " + NZ.format(r='y'),
    0xCA: "x = (x - 1) & 255; " + NZ.format(r='x'), 0x88: "y = (y - 1) & 255; " + NZ.format(r='y'),
    0xEA: "pass",
    0x0A: "c = a >> 7; a = (a << 1) & 255; " + NZ.format(r='a'),
    0x4A: "c = a & 1; a >>= 1; " + NZ.format(r='a'),
    0x2A: "t = (a << 1) | c; c = a >> 7; a = t & 255; " + NZ.format(r='a'),
    0x6A: "t = (a >> 1) | (c << 7); c = a & 1; a = t; " + NZ.format(r='a'),
}
PUSH = "mem[256 + sp] = {v}; sp = (sp - 1) & 255"
PULL = "sp = (sp + 1) & 255; {v} = mem[256 + sp]"
PSTATUS = "(c | (fz << 1) | (fi << 2) | (fd << 3) | 48 | (ov << 6) | fn)"
SETP = ("c = p & 1; fz = bool(p & 2); fi = (p >> 2) & 1; fd = (p >> 3) & 1; ov = (p >> 6) & 1; "
        "fn = p & 128")


def _build_ops():
    """-> {опкод: (строки кода, такты)}; такты вынесены в код как cyc += N."""
    ops = {}

    def add(op, lines, base=0):
        if isinstance(lines, str):
            lines = [lines]
        pre = [f"cyc += {base}"] if base else []
        ops[op] = pre + lines

    def with_mode(mode, kind, body, op, base=None, extra=()):
        code, rc, penalty, direct = MODES[mode]
        lines = [s for s in code.split('; ')]
        if kind == 'read':
            cycles = rc
            rd = "mem[ea]" if direct else RD
            lines.append(f"v = {rd}")
            if penalty:
                lines.append("if (t ^ ea) & 65280: cyc += 1")
        elif kind == 'store':
            cycles = STORE_CYC[mode]
        else:
            cycles = RMW_CYC[mode]
            rd = "mem[ea]" if direct else RD
            lines.append(f"v = {rd}")
        wr = "mem[ea] = v" if direct else "wr(ea, v, cyc)"
        for part in body.split('; '):
            lines.append(wr if part == 'WRITE' else part)
        if kind == 'rmw' and 'WRITE' not in body:
            lines.append(wr)
        lines = list(extra) + lines
        add(op, ["cyc += %d" % cycles] + lines)

    # блоки cc=01
    alu = ['ORA', 'AND', 'EOR', 'ADC', 'STA', 'LDA', 'CMP', 'SBC']
    modes01 = ['inx', 'zp', 'imm', 'abs', 'iny', 'zpx', 'aby', 'abx']
    for aaa, name in enumerate(alu):
        for bbb, mode in enumerate(modes01):
            op = (aaa << 5) | (bbb << 2) | 1
            if name == 'STA':
                if mode == 'imm':
                    continue
                code, _, _, direct = MODES[mode]
                lines = [s for s in code.split('; ')]
                lines.append("mem[ea] = a" if direct else "wr(ea, a, cyc)")
                add(op, ["cyc += %d" % STORE_CYC[mode]] + lines)
            else:
                with_mode(mode, 'read', READ_OPS[name], op)
    # cc=10: сдвиги, LDX/STX, INC/DEC
    for bbb, mode in ((1, 'zp'), (3, 'abs'), (5, 'zpx'), (7, 'abx')):
        for aaa, name in ((0, 'ASL'), (1, 'ROL'), (2, 'LSR'), (3, 'ROR'), (6, 'DEC'), (7, 'INC')):
            with_mode(mode, 'rmw', RMW_OPS[name], (aaa << 5) | (bbb << 2) | 2)
    for op, mode in ((0xA2, 'imm'), (0xA6, 'zp'), (0xB6, 'zpy'), (0xAE, 'abs'), (0xBE, 'aby')):
        with_mode(mode, 'read', READ_OPS['LDX'], op)
    for op, mode in ((0x86, 'zp'), (0x96, 'zpy'), (0x8E, 'abs')):
        code, _, _, direct = MODES[mode]
        lines = code.split('; ') + ["mem[ea] = x" if direct else "wr(ea, x, cyc)"]
        add(op, ["cyc += %d" % STORE_CYC[mode]] + lines)
    # cc=00: LDY/CPY/CPX/BIT/STY
    for op, mode in ((0xA0, 'imm'), (0xA4, 'zp'), (0xB4, 'zpx'), (0xAC, 'abs'), (0xBC, 'abx')):
        with_mode(mode, 'read', READ_OPS['LDY'], op)
    for op, mode in ((0xC0, 'imm'), (0xC4, 'zp'), (0xCC, 'abs')):
        with_mode(mode, 'read', READ_OPS['CPY'], op)
    for op, mode in ((0xE0, 'imm'), (0xE4, 'zp'), (0xEC, 'abs')):
        with_mode(mode, 'read', READ_OPS['CPX'], op)
    for op, mode in ((0x24, 'zp'), (0x2C, 'abs')):
        with_mode(mode, 'read', READ_OPS['BIT'], op)
    for op, mode in ((0x84, 'zp'), (0x94, 'zpx'), (0x8C, 'abs')):
        code, _, _, direct = MODES[mode]
        lines = code.split('; ') + ["mem[ea] = y" if direct else "wr(ea, y, cyc)"]
        add(op, ["cyc += %d" % STORE_CYC[mode]] + lines)
    # переходы, стек, вызовы
    for op, cond in BRANCH.items():
        add(op, ["cyc += 2", "t = mem[pc]; pc = (pc + 1) & 65535",
                 f"if {cond}:", "    cyc += 1",
                 "    t = (pc + t - 256) & 65535 if t & 128 else (pc + t) & 65535",
                 "    if (t ^ pc) & 65280: cyc += 1",
                 "    pc = t"])
    for op, body in IMPLIED.items():
        add(op, ["cyc += 2", body])
    add(0x48, ["cyc += 3", PUSH.format(v='a')])
    add(0x08, ["cyc += 3", PUSH.format(v=PSTATUS)])
    add(0x68, ["cyc += 4", PULL.format(v='a'), NZ.format(r='a')])
    add(0x28, ["cyc += 4", PULL.format(v='p'), SETP])
    add(0x4C, ["cyc += 3", "pc = mem[pc] | (mem[(pc + 1) & 65535] << 8)"])
    add(0x6C, ["cyc += 5", "ea = mem[pc] | (mem[(pc + 1) & 65535] << 8)",
               f"pc = {RD} | ({RD.replace('ea', '((ea & 65280) | ((ea + 1) & 255))')} << 8)"])
    add(0x20, ["cyc += 6", "ea = mem[pc] | (mem[(pc + 1) & 65535] << 8)", "t = (pc + 1) & 65535",
               "mem[256 + sp] = t >> 8; mem[256 + ((sp - 1) & 255)] = t & 255; sp = (sp - 2) & 255",
               "pc = ea"])
    add(0x60, ["cyc += 6", "t = mem[256 + ((sp + 1) & 255)] | (mem[256 + ((sp + 2) & 255)] << 8)",
               "sp = (sp + 2) & 255; pc = (t + 1) & 65535"])
    add(0x40, ["cyc += 6", PULL.format(v='p'), SETP,
               "pc = mem[256 + ((sp + 1) & 255)] | (mem[256 + ((sp + 2) & 255)] << 8); sp = (sp + 2) & 255"])
    # неофициальные
    for op, mode in ((0xA3, 'inx'), (0xA7, 'zp'), (0xAF, 'abs'), (0xB3, 'iny'), (0xB7, 'zpy'), (0xBF, 'aby')):
        with_mode(mode, 'read', READ_OPS['LAX'], op)
    for op, mode in ((0x83, 'inx'), (0x87, 'zp'), (0x8F, 'abs'), (0x97, 'zpy')):
        code, _, _, direct = MODES[mode]
        lines = code.split('; ') + ["mem[ea] = a & x" if direct else "wr(ea, a & x, cyc)"]
        add(op, ["cyc += %d" % STORE_CYC[mode]] + lines)
    rmw_modes = {0x03: 'inx', 0x07: 'zp', 0x0F: 'abs', 0x13: 'iny', 0x17: 'zpx', 0x1B: 'aby', 0x1F: 'abx'}
    for base, name in ((0x00, 'SLO'), (0x20, 'RLA'), (0x40, 'SRE'), (0x60, 'RRA'),
                       (0xC0, 'DCP'), (0xE0, 'ISB')):
        for lo, mode in rmw_modes.items():
            with_mode(mode, 'rmw', RMW_OPS[name], base | lo)
    add(0xEB, ["cyc += 2", "ea = pc; pc = (pc + 1) & 65535", "v = mem[ea] ^ 255", ADC])
    for op, name in ((0x0B, 'ANC'), (0x2B, 'ANC'), (0x4B, 'ALR'), (0x6B, 'ARR'), (0xCB, 'AXS')):
        with_mode('imm', 'read', READ_OPS[name], op)
    for op in (0x1A, 0x3A, 0x5A, 0x7A, 0xDA, 0xFA):
        add(op, ["cyc += 2"])
    for op in (0x80, 0x82, 0x89, 0xC2, 0xE2):
        with_mode('imm', 'read', 'pass', op)
    for op in (0x04, 0x44, 0x64):
        with_mode('zp', 'read', 'pass', op)
    for op in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
        with_mode('zpx', 'read', 'pass', op)
    with_mode('abs', 'read', 'pass', 0x0C)
    for op in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
        with_mode('abx', 'read', 'pass', op)
    return ops


def _build_interpreter():
    ops = _build_ops()
    src = ["def run(self, limit):",
           "    mem = self.mem; rd_io = self.rd_io; wr = self.wr",
           "    a, x, y, sp, pc = self.a, self.x, self.y, self.sp, self.pc",
           "    c, ov, fn, fz, fi, fd = self.c, self.ov, self.fn, self.fz, self.fi, self.fd",
           "    cyc = self.cyc",
           "    res = 1",
           "    while cyc < limit:",
           "        op = mem[pc]; pc = (pc + 1) & 65535"]

    def emit(lo, hi, ind):
        pad = '    ' * ind
        if hi - lo == 1:
            body = ops.get(lo)
            if body is None:
                if lo == 0x02:
                    src.append(pad + "res = 0; break")
                else:
                    src.append(pad + "self.bad = op; res = 2; break")
                return
            for ln in body:
                for part in ln.split('; ') if not ln.startswith(('if ', '    ')) else [ln]:
                    src.append(pad + part)
            return
        mid = (lo + hi) // 2
        src.append(pad + f"if op < {mid}:")
        emit(lo, mid, ind + 1)
        src.append(pad + "else:")
        emit(mid, hi, ind + 1)

    emit(0, 256, 2)
    src += ["    self.a, self.x, self.y, self.sp, self.pc = a, x, y, sp, pc",
            "    self.c, self.ov, self.fn, self.fz, self.fi, self.fd = c, ov, fn, fz, fi, fd",
            "    self.cyc = cyc",
            "    return res"]
    ns = {}
    exec(compile('\n'.join(src), '<nsf6502>', 'exec'), ns)
    return ns['run']


_RUN = None


# ═══════════════════════════════════════════════════════════════
# APU 2A03
# ═══════════════════════════════════════════════════════════════

FC4 = [(7457, 1, 0), (14913, 1, 1), (22371, 1, 0), (29829, 1, 1)]
FC4_LEN = 29830
FC5 = [(7457, 1, 0), (14913, 1, 1), (22371, 1, 0), (29829, 0, 0), (37281, 1, 1)]
FC5_LEN = 37282


class Env:
    """Генератор огибающей (pulse/noise)."""
    __slots__ = ('start', 'div', 'decay', 'loop', 'const', 'vol')

    def __init__(self):
        self.start = False
        self.div = self.decay = 0
        self.loop = self.const = False
        self.vol = 0

    def clock(self):
        if self.start:
            self.start = False
            self.decay = 15
            self.div = self.vol
        elif self.div == 0:
            self.div = self.vol
            if self.decay > 0:
                self.decay -= 1
            elif self.loop:
                self.decay = 15
        else:
            self.div -= 1

    def level(self):
        return self.vol if self.const else self.decay


class Pulse:
    def __init__(self, ones):
        self.ones = ones            # pulse 1: sweep вычитает на 1 больше
        self.env = Env()
        self.duty = 0
        self.timer = 0
        self.length = 0
        self.halt = False
        self.sw_en = False
        self.sw_period = 0
        self.sw_neg = False
        self.sw_shift = 0
        self.sw_reload = False
        self.sw_div = 0

    def target(self):
        d = self.timer >> self.sw_shift
        if self.sw_neg:
            return self.timer - d - (1 if self.ones else 0)
        return self.timer + d

    def clock_sweep(self):
        t = self.target()
        if self.sw_div == 0 and self.sw_en and self.sw_shift and self.timer >= 8 and t <= 0x7FF:
            self.timer = max(0, t)
        if self.sw_div == 0 or self.sw_reload:
            self.sw_div = self.sw_period
            self.sw_reload = False
        else:
            self.sw_div -= 1

    def level(self):
        if self.length == 0 or self.timer < 8 or self.target() > 0x7FF:
            return 0
        return self.env.level()


class APU:
    def __init__(self, clock, pal=False):
        self.clock = clock
        self.pal = pal
        self.noise_tab = NOISE_PERIOD[pal]
        self.dmc_tab = DMC_PERIOD[pal]
        self.en = [False] * 5
        self.pl = [Pulse(True), Pulse(False)]
        # треугольник
        self.tri_timer = 0
        self.tri_length = 0
        self.tri_ctrl = False
        self.tri_reload_val = 0
        self.tri_counter = 0
        self.tri_reload = False
        # шум
        self.nz_env = Env()
        self.nz_length = 0
        self.nz_halt = False
        self.nz_mode = 0
        self.nz_idx = 0
        # DMC
        self.dmc_idx = 0
        self.dmc_loop = False
        self.dmc_irq = False
        self.dmc_addr = 0xC000
        self.dmc_len = 1
        self.dmc_out = 0
        self.dmc_active = False
        self.dmc_end = 0
        self.mem_read = None          # колбэк: адрес -> байт
        self.dmc_events = []          # ('start', такт, dict) / ('stop', такт)
        self.dac_writes = []          # (такт, значение $4011)
        # кадровый счётчик
        self.fc5 = False
        self.fc_inhibit = False
        self.fc_start = 0
        self.fc_step = 0
        self.fc_irq = False
        self.next_tick = FC4[0][0]
        # журнал
        self.ev = [[] for _ in range(4)]
        self.last = [None] * 4
        self.sound_cycle = 0          # последний такт, когда было слышно хоть что-то
        self.cyc = 0

    # --- наблюдаемое состояние канала ---
    def _obs(self, ch):
        if ch < 2:
            p = self.pl[ch]
            return (p.timer, p.duty, p.level())
        if ch == 2:
            on = (self.en[2] and self.tri_length > 0 and self.tri_counter > 0 and self.tri_timer >= 2)
            return (self.tri_timer, 0, 15 if on else 0)
        lv = self.nz_env.level() if (self.en[3] and self.nz_length > 0) else 0
        return (self.nz_idx, self.nz_mode, lv)

    def _log(self, ch, cyc, trig=False):
        s = self._obs(ch)
        if trig or s != self.last[ch]:
            self.ev[ch].append((cyc, trig) + s)
            self.last[ch] = s
        if s[2]:
            self.sound_cycle = cyc

    def _log_all(self, cyc):
        for ch in range(4):
            self._log(ch, cyc)

    # --- кадровый счётчик ---
    def _tick(self, cyc, q, h):
        if q:
            for p in self.pl:
                p.env.clock()
            self.nz_env.clock()
            if self.tri_reload:
                self.tri_counter = self.tri_reload_val
            elif self.tri_counter > 0:
                self.tri_counter -= 1
            if not self.tri_ctrl:
                self.tri_reload = False
        if h:
            for p in self.pl:
                if not p.halt and p.length > 0:
                    p.length -= 1
                p.clock_sweep()
            if not self.tri_ctrl and self.tri_length > 0:
                self.tri_length -= 1
            if not self.nz_halt and self.nz_length > 0:
                self.nz_length -= 1
        self._log_all(cyc)

    def advance(self, cyc):
        while self.next_tick <= cyc:
            t = self.next_tick
            seq = FC5 if self.fc5 else FC4
            _, q, h = seq[self.fc_step]
            self._tick(t, q, h)
            self.fc_step += 1
            if self.fc_step >= len(seq):
                self.fc_step = 0
                self.fc_start += FC5_LEN if self.fc5 else FC4_LEN
            self.next_tick = self.fc_start + seq[self.fc_step][0]
        if self.dmc_active and not self.dmc_loop and cyc >= self.dmc_end:
            self.dmc_active = False
        self.cyc = cyc

    # --- регистры ---
    def write(self, addr, v, cyc):
        self.advance(cyc)
        if addr <= 0x4007:
            ch = (addr - 0x4000) >> 2
            p = self.pl[ch]
            r = addr & 3
            trig = False
            if r == 0:
                p.duty = v >> 6
                p.halt = p.env.loop = bool(v & 0x20)
                p.env.const = bool(v & 0x10)
                p.env.vol = v & 15
            elif r == 1:
                p.sw_en = bool(v & 0x80)
                p.sw_period = (v >> 4) & 7
                p.sw_neg = bool(v & 8)
                p.sw_shift = v & 7
                p.sw_reload = True
            elif r == 2:
                p.timer = (p.timer & 0x700) | v
            else:
                p.timer = (p.timer & 0xFF) | ((v & 7) << 8)
                if self.en[ch]:
                    p.length = LENGTH_TABLE[v >> 3]
                p.env.start = True
                trig = True
            self._log(ch, cyc, trig)
        elif addr <= 0x400B:
            r = addr & 3
            trig = False
            if r == 0:
                self.tri_ctrl = bool(v & 0x80)
                self.tri_reload_val = v & 0x7F
            elif r == 2:
                self.tri_timer = (self.tri_timer & 0x700) | v
            elif r == 3:
                self.tri_timer = (self.tri_timer & 0xFF) | ((v & 7) << 8)
                if self.en[2]:
                    self.tri_length = LENGTH_TABLE[v >> 3]
                self.tri_reload = True
                trig = True
            self._log(2, cyc, trig)
        elif addr <= 0x400F:
            r = addr & 3
            trig = False
            if r == 0:
                self.nz_halt = self.nz_env.loop = bool(v & 0x20)
                self.nz_env.const = bool(v & 0x10)
                self.nz_env.vol = v & 15
            elif r == 2:
                self.nz_mode = v >> 7
                self.nz_idx = v & 15
            elif r == 3:
                if self.en[3]:
                    self.nz_length = LENGTH_TABLE[v >> 3]
                self.nz_env.start = True
                trig = True
            self._log(3, cyc, trig)
        elif addr == 0x4010:
            self.dmc_idx = v & 15
            self.dmc_loop = bool(v & 0x40)
            self.dmc_irq = bool(v & 0x80)
        elif addr == 0x4011:
            self.dmc_out = v & 0x7F
            self.dac_writes.append((cyc, self.dmc_out))
            self.sound_cycle = cyc
        elif addr == 0x4012:
            self.dmc_addr = 0xC000 + v * 64
        elif addr == 0x4013:
            self.dmc_len = v * 16 + 1
        elif addr == 0x4015:
            for ch in range(4):
                self.en[ch] = bool(v & (1 << ch))
            if not self.en[0]:
                self.pl[0].length = 0
            if not self.en[1]:
                self.pl[1].length = 0
            if not self.en[2]:
                self.tri_length = 0
            if not self.en[3]:
                self.nz_length = 0
            self.en[4] = bool(v & 16)
            if self.en[4]:
                if not self.dmc_active:
                    self._dmc_start(cyc)
            elif self.dmc_active:
                self.dmc_active = False
                self.dmc_events.append(('stop', cyc))
            self._log_all(cyc)
        elif addr == 0x4017:
            self.fc5 = bool(v & 0x80)
            self.fc_inhibit = bool(v & 0x40)
            self.fc_start = cyc
            self.fc_step = 0
            seq = FC5 if self.fc5 else FC4
            self.next_tick = cyc + seq[0][0]
            if self.fc5:
                self._tick(cyc, 1, 1)

    def _dmc_start(self, cyc):
        n = self.dmc_len
        data = bytes(self.mem_read(0x8000 | ((self.dmc_addr + i) & 0x7FFF)) for i in range(n)) \
            if self.mem_read else b''
        per = self.dmc_tab[self.dmc_idx]
        self.dmc_active = True
        self.dmc_end = cyc + n * 8 * per
        self.dmc_events.append(('start', cyc, {'idx': self.dmc_idx, 'period': per, 'loop': self.dmc_loop,
                                               'level': self.dmc_out, 'data': data}))
        self.sound_cycle = cyc

    def read_status(self, cyc):
        self.advance(cyc)
        v = 0
        if self.pl[0].length:
            v |= 1
        if self.pl[1].length:
            v |= 2
        if self.tri_length:
            v |= 4
        if self.nz_length:
            v |= 8
        if self.dmc_active:
            v |= 16
        if self.fc_irq:
            v |= 0x40
        self.fc_irq = False
        return v


# ═══════════════════════════════════════════════════════════════
# Плеер NSF
# ═══════════════════════════════════════════════════════════════

class Trace:
    pass


class NSFPlayer:
    def __init__(self, nsf, song):
        global _RUN
        if _RUN is None:
            _RUN = _build_interpreter()
        self.nsf = nsf
        self.song = song
        self.pal = nsf.pal
        self.clock = PAL_CLOCK if nsf.pal else NTSC_CLOCK
        us = nsf.pal_us if nsf.pal else nsf.ntsc_us
        if not us:
            us = 20000 if nsf.pal else 16666
        self.play_us = us
        # стандартная скорость (0x411A = 16666 / 0x4E20 = 20000) означает точный кадр консоли:
        # 29780.5 / 33247.5 такта (60.0988 / 50.007 Гц), как у других плееров (libgme)
        if us == (20000 if nsf.pal else 16666):
            self.cpp = 33247.5 if nsf.pal else 29780.5
        else:
            self.cpp = us * self.clock / 1e6      # тактов между вызовами PLAY
        self.mem = bytearray(65536)
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.c = self.ov = self.fn = 0
        self.fz = False
        self.fi = 1
        self.fd = 0
        self.cyc = 0
        self.bad = None
        self.vblank = 0
        self.apu = APU(self.clock, nsf.pal)
        self.apu.mem_read = lambda a: self.mem[a]
        self.run_core = _RUN.__get__(self)
        # образ ПЗУ для банков: перед данными - (load & 0xFFF) нулей
        self.image = bytes(nsf.load_addr & 0xFFF) + nsf.data if nsf.banked else None
        self._load()

    def _set_bank(self, slot, bank):
        src = bank * 0x1000
        chunk = self.image[src:src + 0x1000]
        chunk = chunk + bytes(0x1000 - len(chunk))
        self.mem[0x8000 + slot * 0x1000:0x9000 + slot * 0x1000] = chunk

    def _load(self):
        n = self.nsf
        mem = self.mem
        if n.banked:
            for s in range(8):
                self._set_bank(s, n.banks[s])
        else:
            la = n.load_addr
            data = n.data[:0x10000 - la]
            mem[la:la + len(data)] = data
        mem[HALT_ADDR] = 0x02
        self.banked = n.banked
        self.rom_start = 0x8000 if n.banked else min(0x8000, n.load_addr)

    # --- шина ---
    def rd_io(self, a, cyc):
        if a == 0x4015:
            return self.apu.read_status(cyc)
        if a == 0x2002:
            self.vblank ^= 0x80
            return self.vblank
        if a in (0x4016, 0x4017):
            return 0
        return self.mem[a]

    def wr(self, a, v, cyc):
        if a < 0x2000:
            self.mem[a & 0x7FF] = v
        elif 0x4000 <= a <= 0x4017:
            if a != 0x4014 and a != 0x4016:
                self.apu.write(a, v, cyc)
        elif 0x5FF8 <= a <= 0x5FFF:
            if self.banked:
                self._set_bank(a - 0x5FF8, v)
        elif a < 0x8000:
            self.mem[a] = v
        elif not self.banked and a < self.rom_start:
            self.mem[a] = v

    # --- вызов подпрограммы ---
    def call(self, addr, a=0, x=0, budget=2000000):
        """Выполняет подпрограмму до RTS. -> 0 вернулась, 1 не уложилась в budget, 2 плохой опкод."""
        self.a, self.x, self.y = a, x, 0
        self.sp = 0xFD
        t = (HALT_ADDR - 1) & 0xFFFF
        self.mem[0x100 + self.sp] = t >> 8
        self.mem[0x100 + ((self.sp - 1) & 255)] = t & 255
        self.sp = (self.sp - 2) & 255
        self.pc = addr
        self.fi = 1
        return self.run_core(self.cyc + budget)

    def init_song(self):
        n = self.nsf
        mem = self.mem
        for i in range(0x800):
            mem[i] = 0
        if n.banked:
            for s in range(8):
                self._set_bank(s, n.banks[s])
        apu = self.apu
        for r in range(0x4000, 0x4014):
            apu.write(r, 0, 0)
        apu.write(0x4015, 0x0F, 0)
        apu.write(0x4017, 0x40, 0)
        return self.call(n.init_addr, self.song, 1 if n.pal else 0, budget=int(self.clock * 3))

    def run(self, seconds, stop_silence=0.0, progress=None):
        """Эмулирует seconds секунд. stop_silence>0 - остановиться после стольких секунд тишины
        (после того, как что-то прозвучало). -> Trace."""
        apu = self.apu
        self.init_song()
        apu.advance(self.cyc)
        base = self.cyc                      # дальше PLAY вызывается раз в cpp тактов
        total = int(seconds * self.clock)
        k = 0
        timeouts = 0
        bad = 0
        heard = False
        silence = int(stop_silence * self.clock)
        while True:
            target = base + int(k * self.cpp)
            if target - base >= total:
                break
            if self.cyc < target:
                self.cyc = target
            r = self.call(self.nsf.play_addr, budget=int(self.cpp * 4))
            if r == 1:
                timeouts += 1
            elif r == 2:
                bad += 1
            apu.advance(self.cyc)
            k += 1
            if apu.sound_cycle:
                heard = True
            if silence and heard and self.cyc - max(apu.sound_cycle, base) > silence:
                break
            if progress and k % 120 == 0:
                progress(min(1.0, (self.cyc - base) / total))
        end = max(self.cyc, base + int(k * self.cpp))
        apu.advance(end)
        tr = Trace()
        tr.start = base
        tr.clock = self.clock
        tr.pal = self.pal
        tr.cpp = self.cpp
        tr.frames = k
        tr.end = end
        tr.ev = apu.ev
        tr.dmc_events = apu.dmc_events
        tr.dac_writes = apu.dac_writes
        tr.sound_end = apu.sound_cycle
        tr.timeouts, tr.bad_ops = timeouts, bad
        tr.noise_tab = apu.noise_tab
        tr.dmc_tab = apu.dmc_tab
        return tr


# ═══════════════════════════════════════════════════════════════
# Рендер звука по журналу
# ═══════════════════════════════════════════════════════════════

def _lfsr_seq(mode):
    """Выход шумового канала за период LFSR (1 - звучит)."""
    r = 1
    out = []
    seen = {}
    while r not in seen:
        seen[r] = len(out)
        out.append(0 if r & 1 else 1)
        fb = (r & 1) ^ ((r >> (6 if mode else 1)) & 1)
        r = (r >> 1) | (fb << 14)
    start = seen[r]
    return np.array(out[start:], dtype=np.float64) if start else np.array(out, dtype=np.float64)


_WAVES = {}


def _wave(kind, sub=0):
    key = (kind, sub)
    if key not in _WAVES:
        if kind == 'pulse':
            w = np.array(DUTY_SEQ[sub], dtype=np.float64)
        elif kind == 'tri':
            w = np.array(TRI_SEQ, dtype=np.float64) / 15.0
        else:
            w = _lfsr_seq(sub)
        cum = np.concatenate(([0.0], np.cumsum(w)))
        _WAVES[key] = (w, cum)
    return _WAVES[key]


def render_audio(tr, sr=44100, over=4, masks=None, mono_gain=1.0, channels=(0, 1, 2, 3, 4)):
    """Рендер WAV по журналу. masks: {канал: [(t0, t1) сек]} - играть только эти отрезки.
    -> float32 моно (-1..1)."""
    clock = tr.clock
    nfine = int((tr.end - tr.start) / clock * sr * over)
    # границы отсчётов в тактах; t=0 - начало музыки (первый вызов PLAY)
    tb = tr.start + np.arange(nfine + 1, dtype=np.float64) * (clock / float(sr * over))
    avg = {}
    for ch in channels:
        if ch == 4:
            avg[4] = _render_dmc(tr, tb, masks.get(4) if masks else None)
            continue
        events = tr.ev[ch]
        if masks is not None:
            events = _masked(events, masks.get(ch, []), clock, tr.start)
        avg[ch] = _render_wavetable(ch, events, tb, tr)
    z = np.zeros(nfine)
    y = (MIX_PULSE * (avg.get(0, z) + avg.get(1, z)) + MIX_TRI * avg.get(2, z) +
         MIX_NOISE * avg.get(3, z) + avg.get(4, z))
    if over > 1:
        m = len(y) // over * over
        y = y[:m].reshape(-1, over).mean(axis=1)
    y = _highpass(y, sr)
    return (y * mono_gain).astype(np.float32)


HP_TAU = 0.0028               # постоянная времени ФВЧ (~57 Гц): как у libgme для NES


def _highpass(y, sr, tau=HP_TAU):
    """Каузальный однополюсный ФВЧ (убирает постоянную составляющую, как в плеерах)."""
    dt = 1.0 / sr
    alpha = dt / (tau + dt)
    try:
        from scipy.signal import lfilter
        lp = lfilter([alpha], [1.0, alpha - 1.0], y)
    except ImportError:
        k = int(12 * tau * sr) + 1
        h = alpha * (1.0 - alpha) ** np.arange(k)
        n = 1 << int(np.ceil(np.log2(len(y) + k)))
        lp = np.fft.irfft(np.fft.rfft(y, n) * np.fft.rfft(h, n), n)[:len(y)]
    return y - lp


def _masked(events, ranges, clock, start):
    """Оставить звучание канала только в отрезках ranges (секунды от начала музыки): вне них
    уровень 0. Добавляет события на границах отрезков."""
    if not events:
        return events
    rs = [(start + int(a * clock), start + int(b * clock)) for a, b in ranges]
    cuts = sorted(set([c for r in rs for c in r] + [e[0] for e in events]))
    out = []
    ei = 0
    cur = None
    for c in cuts:
        trig = False
        while ei < len(events) and events[ei][0] <= c:
            cur = events[ei]
            trig = trig or (events[ei][0] == c and events[ei][1])
            ei += 1
        if cur is None:
            continue
        inside = any(a <= c < b for a, b in rs)
        out.append((c, trig, cur[2], cur[3], cur[4] if inside else 0))
    return out


def _render_wavetable(ch, events, tb, tr):
    """Средние по интервалам [tb[j], tb[j+1]) для pulse/triangle/noise."""
    n = len(tb) - 1
    res = np.zeros(n)
    if not events:
        return res
    clock_end = tb[-1]
    nseg = len(events)
    cs = np.empty(nseg)
    ls = np.empty(nseg)
    lvl = np.empty(nseg)
    ph0 = np.empty(nseg)
    g0 = np.empty(nseg)
    kind = 'pulse' if ch < 2 else ('tri' if ch == 2 else 'noise')
    wid = []
    phase = 0.0
    acc = 0.0
    prev_c = None
    prev = None
    for i, e in enumerate(events):
        c0 = float(e[0])
        c1 = float(events[i + 1][0]) if i + 1 < nseg else clock_end
        # завершить предыдущий сегмент: фаза и накопленный интеграл
        if prev is not None:
            dur = c0 - prev_c
            if dur > 0 and prev['lvl'] > 0:
                w, cum = prev['wave']
                M = len(w)
                u0 = phase
                u1 = u0 + dur / prev['ls']
                acc += (_F(cum, w, M, u1) - _F(cum, w, M, u0)) * prev['ls'] * prev['lvl']
                phase = u1 % M
            elif dur > 0:
                w, cum = prev['wave']
                phase = (phase + dur / prev['ls']) % len(w)
        # параметры нового сегмента
        trig = e[1]
        if kind == 'pulse':
            ls_i = 2.0 * (e[2] + 1)
            wave = _wave('pulse', e[3])
        elif kind == 'tri':
            ls_i = float(e[2] + 1)
            wave = _wave('tri')
        else:
            ls_i = float(tr.noise_tab[e[2]])
            wave = _wave('noise', e[3])
        if trig and kind == 'pulse':
            phase = 0.0
        elif prev is not None and prev['wave'] is not wave:
            phase = 0.0 if kind != 'noise' else phase % len(wave[0])
        if kind == 'noise' and prev is not None and prev['ls'] != ls_i:
            pass
        cs[i], ls[i], lvl[i], ph0[i], g0[i] = c0, ls_i, float(e[4]), phase, acc
        wid.append(wave)
        prev = {'lvl': float(e[4]), 'ls': ls_i, 'wave': wave}
        prev_c = c0
    # векторно по границам
    seg = np.searchsorted(cs, tb, side='right') - 1
    valid = seg >= 0
    seg = np.clip(seg, 0, nseg - 1)
    G = np.zeros(len(tb))
    # группируем по форме волны
    groups = {}
    for i, wv in enumerate(wid):
        groups.setdefault(id(wv), (wv, []))[1].append(i)
    for _, (wv, idxs) in groups.items():
        w, cum = wv
        M = len(w)
        sel = np.isin(seg, idxs) & valid
        if not sel.any():
            continue
        s = seg[sel]
        u0 = ph0[s]
        u1 = u0 + (tb[sel] - cs[s]) / ls[s]
        G[sel] = g0[s] + (_Fv(cum, w, M, u1) - _Fv(cum, w, M, u0)) * ls[s] * lvl[s]
    dt = tb[1:] - tb[:-1]
    return (G[1:] - G[:-1]) / dt


def _F(cum, w, M, u):
    nf = int(u // M)
    rem = u - nf * M
    k = int(rem)
    if k >= M:
        k = M - 1
    return nf * cum[M] + cum[k] + (rem - k) * w[k]


def _Fv(cum, w, M, u):
    nf = np.floor(u / M)
    rem = u - nf * M
    k = np.minimum(rem.astype(np.int64), M - 1)
    return nf * cum[M] + cum[k] + (rem - k) * w[k]


DMC_STEP_GAIN = DMC_GAIN * 159.79 / 22638.0     # дельта-шаги DMC идут линейно, с наклоном кривой в нуле


def dmc_decode(data, level0, loop=False, nbits=None):
    """Поток DMC -> уровень счётчика (0..127) после каждого бита. loop - повторять данные до nbits."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder='little')
    if loop and nbits and nbits > len(bits):
        bits = np.tile(bits, int(np.ceil(nbits / float(len(bits)))))
    if nbits is not None:
        bits = bits[:nbits]
    out = np.empty(len(bits))
    cur = level0
    for i, b in enumerate(bits.tolist()):
        if b:
            if cur <= 125:
                cur += 2
        elif cur >= 2:
            cur -= 2
        out[i] = cur
    return out


def dmc_levels(tr):
    """Журнал ЦАП DMC для рендера: отсортированные (такт, вклад в смесь) - прямые записи
    ($4011, кривая) и воспроизведение сэмплов (линейные шаги от текущего уровня)."""
    pts = [(c, float(dmc_curve(v))) for c, v in tr.dac_writes]
    for ev in tr.dmc_events:
        if ev[0] != 'start':
            continue
        c0, info = ev[1], ev[2]
        data = info['data']
        if not data:
            continue
        per = info['period']
        level = info['level']
        nxt = [e[1] for e in tr.dmc_events if e[1] > c0 and e[0] in ('stop', 'start')]
        t_stop = min(nxt + [tr.end])
        total = len(data) * 8
        nbits = total if not info['loop'] else int((t_stop - c0) // per) + 1
        lv = dmc_decode(data, level, info['loop'], nbits)
        times = c0 + (np.arange(len(lv)) + 1) * per
        keep = times <= t_stop
        base = float(dmc_curve(level))
        for t, l in zip(times[keep].tolist(), lv[keep].tolist()):
            pts.append((t, base + DMC_STEP_GAIN * (l - level)))
    pts.sort()
    return pts


def _render_dmc(tr, tb, ranges):
    n = len(tb) - 1
    pts = dmc_levels(tr)
    if not pts:
        return np.zeros(n)
    t = np.array([p[0] for p in pts], dtype=np.float64)
    v = np.array([p[1] for p in pts], dtype=np.float64)
    if ranges is not None:
        # играть только внутри отрезков: вне - держать 0 (вместо удержания)
        keep = np.zeros(len(t), dtype=bool)
        for a, b in ranges:
            keep |= (t >= tr.start + a * tr.clock) & (t < tr.start + b * tr.clock)
        v = np.where(keep, v, 0.0)
    # ступенчатая функция: интеграл на границах
    seg_dur = np.diff(np.concatenate((t, [tb[-1]])))
    cum = np.concatenate(([0.0], np.cumsum(v * seg_dur)))
    idx = np.searchsorted(t, tb, side='right') - 1
    G = np.where(idx >= 0, cum[np.clip(idx, 0, None)] + v[np.clip(idx, 0, None)] * (tb - t[np.clip(idx, 0, None)]), 0.0)
    return (G[1:] - G[:-1]) / (tb[1:] - tb[:-1])
