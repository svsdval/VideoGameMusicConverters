#!/usr/bin/env python3
"""
VGM/VGZ (Sega Mega Drive: YM2612 + SN76489) -> XM / MIDI / WAV

Отличие от vgm2xm-midi*.py: тембры не синтезируются "на глаз", а снимаются с
настоящего эмулятора чипа: C++ libvgm (vgm_render/vgm2wav, если собран) или встроенный
эмулятор на чистом Python + numpy (vgm_pyemu.py, ничего собирать не нужно).

  1. VGM разбирается в ноты: для YM2612 - key on/off, F-num/block, TL носителей,
     панорама, снимок патча (все регистры операторов); для SN76489 - тон/шум,
     громкость, высота; DAC-поток YM2612 режется на сэмплы.
  2. Для каждого уникального FM-патча собирается служебный VGM, в котором патч
     играется на нужных высотах. Эмулятор его рендерит, из записи вырезается
     сэмпл (атака + затухание + бесшовный луп на устоявшейся части) и измеряется
     время релиза. Это тот же самый звук, что и в WAV эмулятора.
  3. Инструмент XM = патч, в нём по сэмплу на каждую октаву (keymap).
     PSG: меандр/шум (LFSR как в чипе). Громкость/панорама/глиссандо - из логов.
  4. Темп подбирается по началам нот, ноты раскладываются по каналам.

Использование:
    python3 vgm2xm-midi-wav.py music.vgm
    python3 vgm2xm-midi-wav.py music.vgm --all
    python3 vgm2xm-midi-wav.py music.vgz --midi --compact 2
"""

import argparse
import bisect
import collections
import gzip
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
RENDER_DIR = os.path.join(HERE, 'vgm_render')

SR = 44100                    # частота отсчётов VGM и рендера
XM_C4_RATE = 8363.0
MAX_XM_CHANNELS = 32
MAX_XM_INSTRUMENTS = 128
SETTLE = 300                  # отсчётов: регистры, записанные сразу после key on, ещё относятся к старту

# ═══════════════════════════════════════════════════════════════
# Разбор VGM
# ═══════════════════════════════════════════════════════════════

class VGM:
    pass


OTHER_CHIPS = [(0x10, 'YM2413'), (0x30, 'YM2151'), (0x38, 'SegaPCM'), (0x44, 'RF5C68'),
               (0x48, 'YM2203'), (0x4C, 'YM2608'), (0x50, 'YM2610'), (0x54, 'YM3812'),
               (0x58, 'YM3526'), (0x5C, 'Y8950'), (0x60, 'YMF262'), (0x64, 'YMF278B'),
               (0x68, 'YMF271'), (0x6C, 'YMZ280B'), (0x70, 'RF5C164'), (0x74, 'AY8910'),
               (0x80, 'GameBoy'), (0x84, 'NES APU'), (0x88, 'MultiPCM'), (0x8C, 'uPD7759')]


def load_vgm(path):
    with open(path, 'rb') as f:
        d = f.read()
    return parse_vgm(d, path)


def parse_vgm(d, path='VGM'):
    if d[:2] == b'\x1f\x8b':
        d = gzip.decompress(d)
    if d[:4] != b'Vgm ':
        raise SystemExit(f"{path}: это не VGM")

    def u32(o):
        return struct.unpack_from('<I', d, o)[0] if o + 4 <= len(d) else 0

    v = VGM()
    v.raw = d
    v.version = u32(8)
    v.sn_clk = u32(0x0C) & 0x3FFFFFFF
    v.ym_clk = (u32(0x2C) & 0x3FFFFFFF) if v.version >= 0x110 else 0
    v.sn_flags = (struct.unpack_from('<H', d, 0x28)[0], d[0x2A]) if v.version >= 0x110 and len(d) > 0x2A else (9, 16)
    if v.sn_flags[0] == 0:
        v.sn_flags = (9, 16)
    v.total = u32(0x18)
    data_off = (0x34 + u32(0x34)) if (v.version >= 0x150 and u32(0x34)) else 0x40
    v.other = []
    if v.version >= 0x110:
        for off, nm in OTHER_CHIPS:
            if off + 4 <= data_off and (u32(off) & 0x3FFFFFFF):
                v.other.append(nm)
    loop_abs = (0x1C + u32(0x1C)) if u32(0x1C) else None

    ev = []
    t = 0
    p = data_off
    bank = bytearray()
    bank_off = 0
    loop_idx = None
    loop_t = 0
    fixed = {0x90: 5, 0x91: 5, 0x92: 6, 0x93: 11, 0x94: 2, 0x95: 5}
    while p < len(d):
        if loop_abs is not None and p == loop_abs:
            loop_idx = len(ev)
            loop_t = t
        o = d[p]
        if o == 0x4F:
            ev.append((t, 'G', d[p + 1], 0)); p += 2
        elif o == 0x50:
            ev.append((t, 'S', d[p + 1], 0)); p += 2
        elif o in (0x52, 0x53):
            ev.append((t, 'Y', ((o - 0x52) << 8) | d[p + 1], d[p + 2])); p += 3
        elif o == 0x61:
            t += struct.unpack_from('<H', d, p + 1)[0]; p += 3
        elif o == 0x62:
            t += 735; p += 1
        elif o == 0x63:
            t += 882; p += 1
        elif o == 0x66:
            break
        elif o == 0x67:
            typ = d[p + 2]
            size = u32(p + 3) & 0x7FFFFFFF
            if typ == 0x00:
                bank += d[p + 7:p + 7 + size]
            p += 7 + size
        elif o == 0x68:
            p += 12
        elif 0x70 <= o <= 0x7F:
            t += (o & 15) + 1; p += 1
        elif 0x80 <= o <= 0x8F:
            val = bank[bank_off] if bank_off < len(bank) else 0x80
            bank_off += 1
            ev.append((t, 'Y', 0x2A, val))
            t += o & 15
            p += 1
        elif o == 0xE0:
            bank_off = u32(p + 1); p += 5
        elif o in fixed:
            p += fixed[o]
        elif 0x30 <= o <= 0x3F:
            p += 2
        elif 0x40 <= o <= 0x4E:
            p += 3
        elif 0x51 <= o <= 0x5F:
            p += 3
        elif 0xA0 <= o <= 0xBF:
            p += 3
        elif 0xC0 <= o <= 0xDF:
            p += 4
        elif 0xE1 <= o <= 0xFF:
            p += 5
        else:
            break
    v.end_t = t
    v.events = ev
    v.loop_idx = loop_idx
    v.loop_t = loop_t
    return v


def repeat_loops(v, loops):
    """Событие списка с повторами петли (loops=1 - как записано в файле)."""
    ev = list(v.events)
    if loops > 1 and v.loop_idx is not None:
        ln = v.end_t - v.loop_t
        base = v.events[v.loop_idx:]
        for k in range(1, loops):
            ev += [(e[0] + k * ln, e[1], e[2], e[3]) for e in base]
        return ev, v.end_t + (loops - 1) * ln
    return ev, v.end_t


# ═══════════════════════════════════════════════════════════════
# Трекинг чипов -> ноты
# ═══════════════════════════════════════════════════════════════

ALG_CARRIERS = {0: (4,), 1: (4,), 2: (4,), 3: (4,), 4: (2, 4), 5: (2, 3, 4), 6: (2, 3, 4), 7: (1, 2, 3, 4)}
OP_OFF = {1: 0, 3: 4, 2: 8, 4: 12}              # номер оператора -> смещение регистра
SLOT_OP = {0: 1, 1: 3, 2: 2, 3: 4}              # (reg >> 2) & 3 -> номер оператора


class Note:
    def __init__(self, kind, voice, t):
        self.kind = kind            # 'fm' | 'psg' | 'noise' | 'dac'
        self.voice = voice
        self.start = t              # в отсчётах VGM; потом секунды
        self.end = None
        self.cur = []               # [(t, freq, level, pan)]
        self.patch = None           # FM: ключ патча; noise: (fb, nf)
        self.dac = None             # DAC: (bytes, rate)
        self.pending = False
        self.inst = None            # номер XM-инструмента (1..)
        self.start_row = self.end_row = 0
        self.channel = 0
        self.tail_rows = 0


class Tracker:
    def __init__(self, v):
        self.v = v
        self.fs = v.ym_clk / 144.0 if v.ym_clk else 0.0
        self.R = [bytearray(256), bytearray(256)]
        self.a4 = [0] * 6
        self.fm_note = [None] * 6
        self.dac_en = False
        self.dac_w = []
        self.notes = []
        self.pending = []
        # PSG
        self.sn_clk = v.sn_clk
        self.div = [0, 0, 0]
        self.att = [15, 15, 15, 15]
        self.noise = 0
        self.latch = (0, 0)
        self.gg = 0xFF
        self.psg_note = [None] * 4
        self.dirty = set()
        self.cur_t = 0

    # ---------- общее ----------
    def feed(self, t, op, a, b):
        if t != self.cur_t:
            self._commit(self.cur_t)
            self.cur_t = t
        self._settle(t)
        if op == 'Y':
            self._ym(t, a >> 8, a & 0xFF, b)
        elif op == 'S':
            self._sn(t, a)
        elif op == 'G':
            self.gg = a
            self.dirty.update((0, 1, 2, 3))

    def finish(self, t_end):
        self._commit(self.cur_t)
        for n, c in list(self.pending):
            self._finalize(n)
        for c in range(6):
            if self.fm_note[c] is not None:
                self._fm_off(t_end, c)
        for ch in range(4):
            if self.psg_note[ch] is not None:
                self.psg_note[ch].end = t_end
                self.psg_note[ch] = None
        self._make_dac()
        out = []
        for n in self.notes:
            if n.end is None:
                n.end = t_end
            if n.kind == 'dac':
                out.append(n)
                continue
            if not n.cur:
                continue
            if max(c[2] for c in n.cur) < 0.002:
                continue
            out.append(n)
        out.sort(key=lambda n: (n.start, n.voice))
        self.notes = out

    # ---------- YM2612 ----------
    def _ch_of_reg(self, port, reg):
        i = reg & 3
        return None if i == 3 else port * 3 + i

    def _ym(self, t, port, reg, val):
        R = self.R[port]
        R[reg] = val
        if port == 0 and reg == 0x28:
            c = val & 3
            if c == 3:
                return
            if val & 4:
                c += 3
            if val >> 4:
                if self.fm_note[c] is None and not (c == 5 and self.dac_en) and self.fs:
                    self._fm_on(t, c)
            elif self.fm_note[c] is not None:
                self._fm_off(t, c)
        elif port == 0 and reg == 0x2A:
            self.dac_w.append((t, val))
        elif port == 0 and reg == 0x2B:
            self.dac_en = bool(val & 0x80)
        elif 0xA0 <= reg <= 0xA2 or 0xA4 <= reg <= 0xA6:
            c = self._ch_of_reg(port, reg)
            if c is None:
                return
            if reg >= 0xA4:
                self.a4[c] = val
            else:
                self._fm_update(t, c)
        elif 0x40 <= reg <= 0x4F:
            c = self._ch_of_reg(port, reg)
            if c is not None:
                self._fm_update(t, c)
        elif 0xB4 <= reg <= 0xB6:
            self._fm_update(t, port * 3 + (reg & 3))

    def _fm_freq(self, c):
        port, i = divmod(c, 3)
        fnum = ((self.a4[c] & 7) << 8) | self.R[port][0xA0 + i]
        blk = (self.a4[c] >> 3) & 7
        return fnum * self.fs / float(1 << (21 - blk))

    def _fm_level_pan(self, c):
        port, i = divmod(c, 3)
        R = self.R[port]
        alg = R[0xB0 + i] & 7
        mintl = min(R[0x40 + i + OP_OFF[o]] & 0x7F for o in ALG_CARRIERS[alg])
        lv = 10.0 ** (-0.75 * mintl / 20.0)
        b4 = R[0xB4 + i]
        left, right = b4 & 0x80, b4 & 0x40
        if left and right:
            pan = 128
        elif left:
            pan = 0
        elif right:
            pan = 255
        else:
            pan = 128
            lv = 0.0
        return lv, pan

    def _fm_on(self, t, c):
        n = Note('fm', c, t)
        n.pending = True
        n.snap_at = t + SETTLE
        self.fm_note[c] = n
        self.notes.append(n)
        self.pending.append((n, c))
        self._fm_update(t, c)

    def _fm_update(self, t, c):
        n = self.fm_note[c]
        if n is None:
            return
        f = self._fm_freq(c)
        lv, pan = self._fm_level_pan(c)
        if not n.cur or n.cur[-1][1:] != (f, lv, pan):
            n.cur.append((t, f, lv, pan))

    def _fm_off(self, t, c):
        n = self.fm_note[c]
        if n is None:
            return
        if n.pending:
            self._finalize(n)
        n.end = t
        self.fm_note[c] = None

    def _settle(self, t):
        if not self.pending:
            return
        for item in list(self.pending):
            n, c = item
            if t >= n.snap_at:
                self._finalize(n)

    def _finalize(self, n):
        """Снимок патча и стартовых значений (после "оседания" регистров)."""
        if not n.pending:
            return
        n.pending = False
        self.pending = [(m, c) for (m, c) in self.pending if m is not n]
        c = n.voice
        port, i = divmod(c, 3)
        R = self.R[port]
        b0 = R[0xB0 + i]
        alg = b0 & 7
        car = ALG_CARRIERS[alg]
        mintl = min(R[0x40 + i + OP_OFF[o]] & 0x7F for o in car)
        ops = []
        for o in (1, 2, 3, 4):
            off = i + OP_OFF[o]
            tl = R[0x40 + off] & 0x7F
            if o in car:
                tl -= mintl
            ops.append((R[0x30 + off], tl, R[0x50 + off], R[0x60 + off], R[0x70 + off],
                        R[0x80 + off], R[0x90 + off] & 0x0F))
        lfo = self.R[0][0x22] if (R[0xB4 + i] & 0x37) else 0
        n.patch = (tuple(ops), b0 & 0x3F, R[0xB4 + i] & 0x37, lfo)
        f = self._fm_freq(c)
        lv, pan = self._fm_level_pan(c)
        later = [e for e in n.cur if e[0] >= n.snap_at]
        n.cur = [(n.start, f, lv, pan)] + later
        # убрать повторы первого значения
        if later and later[0][1:] == (f, lv, pan):
            n.cur = later if later[0][0] == n.start else n.cur[:1] + later[1:]

    # ---------- SN76489 ----------
    def _sn(self, t, val):
        if val & 0x80:
            ch = (val >> 5) & 3
            typ = (val >> 4) & 1
            self.latch = (ch, typ)
            data = val & 15
            if typ:
                self.att[ch] = data
                self.dirty.add(ch)
            elif ch < 3:
                self.div[ch] = (self.div[ch] & 0x3F0) | data
                self.dirty.add(ch)
                if ch == 2:
                    self.dirty.add(3)
            else:
                self.noise = data & 7
                self.dirty.add(3)
        else:
            ch, typ = self.latch
            if typ:
                self.att[ch] = val & 15
                self.dirty.add(ch)
            elif ch < 3:
                self.div[ch] = (self.div[ch] & 0x0F) | ((val & 0x3F) << 4)
                self.dirty.add(ch)
                if ch == 2:
                    self.dirty.add(3)
            else:
                self.noise = val & 7
                self.dirty.add(3)

    def _pan_of(self, ch):
        left = bool(self.gg & (0x10 << ch))
        right = bool(self.gg & (1 << ch))
        if left and right:
            return 128, 1.0
        if left:
            return 0, 1.0
        if right:
            return 255, 1.0
        return 128, 0.0

    def _commit(self, t):
        if not self.dirty:
            return
        dirty, self.dirty = self.dirty, set()
        for ch in sorted(dirty):
            att = self.att[ch]
            pan, pg = self._pan_of(ch)
            lv = 10.0 ** (-2.0 * att / 20.0) * pg if att < 15 else 0.0
            if ch < 3:
                div = self.div[ch] or 1024
                f = self.sn_clk / (32.0 * div)
                aud = att < 15 and pg > 0 and 20 <= f <= 12000
                key = None
            else:
                nf = self.noise & 3
                n_ = (16, 32, 64)[nf] if nf < 3 else (self.div[2] or 1024)
                f = self.sn_clk / (32.0 * n_)
                aud = att < 15 and pg > 0
                key = (1 if self.noise & 4 else 0, nf)
            n = self.psg_note[ch]
            if not aud:
                if n is not None:
                    n.end = t
                    self.psg_note[ch] = None
                continue
            if n is not None:
                pt, pf, plv, ppan = n.cur[-1]
                retrig = False
                if ch < 3:
                    st = abs(12 * math.log2(f / pf)) if pf > 0 else 0
                    if lv > plv * 1.9 and st >= 0.5:
                        retrig = True
                    elif lv > plv * 3.5:
                        retrig = True
                else:
                    if key != n.patch or lv > plv * 3.5:
                        retrig = True
                if retrig:
                    n.end = t
                    n = None
                    self.psg_note[ch] = None
                else:
                    if (f, lv, pan) != (pf, plv, ppan):
                        n.cur.append((t, f, lv, pan))
                    continue
            n = Note('psg' if ch < 3 else 'noise', 6 + ch if ch < 3 else 9, t)
            n.patch = key
            n.cur.append((t, f, lv, pan))
            self.psg_note[ch] = n
            self.notes.append(n)

    # ---------- DAC ----------
    def _make_dac(self):
        w = self.dac_w
        if len(w) < 16:
            return
        segs = []
        cur = [w[0]]
        for a, b in zip(w, w[1:]):
            if b[0] - a[0] > 530:
                segs.append(cur)
                cur = []
            cur.append(b)
        segs.append(cur)
        for sg in segs:
            if len(sg) < 24:
                continue
            ts = np.array([x[0] for x in sg], dtype=np.float64)
            dt = float(np.median(np.diff(ts)))
            if dt <= 0:
                continue
            data = bytes(x[1] for x in sg)
            if len(set(data)) < 3:
                continue
            n = Note('dac', 10, int(ts[0]))
            n.end = int(ts[-1] + dt)
            n.dac = (data, SR / dt)
            n.cur.append((n.start, 0.0, 1.0, 128))
            self.notes.append(n)


# ═══════════════════════════════════════════════════════════════
# Рендер через vgm2wav (эмулятор libvgm)
# ═══════════════════════════════════════════════════════════════

def find_renderer():
    for p in (os.path.join(RENDER_DIR, 'vgm2wav'), shutil.which('vgm2wav')):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def build_renderer():
    sh = os.path.join(RENDER_DIR, 'build.sh')
    print("[vgm] сборка vgm2wav (libvgm) ...")
    r = subprocess.run(['sh', sh])
    if r.returncode != 0:
        raise SystemExit("не удалось собрать vgm2wav (нужны git, cmake, gcc, make)")


def pick_renderer(mode):
    """Возвращает путь к vgm2wav или None (= встроенный эмулятор на Python)."""
    exe = find_renderer() if mode in ('auto', 'native') else None
    if mode == 'native' and exe is None:
        build_renderer()
        exe = find_renderer()
        if exe is None:
            raise SystemExit("vgm2wav не найден")
    if exe is None:
        print("[vgm] эмулятор: встроенный на Python (vgm_pyemu.py); "
              "для скорости можно собрать C++ libvgm: --build-renderer")
    return exe


def read_wav(path):
    """Читает PCM16 WAV (в т.ч. WAVE_FORMAT_EXTENSIBLE) -> float (N, ch)."""
    with open(path, 'rb') as f:
        d = f.read()
    p = 12
    ch = 2
    bits = 16
    data = b''
    while p + 8 <= len(d):
        cid = d[p:p + 4]
        sz = struct.unpack_from('<I', d, p + 4)[0]
        if cid == b'fmt ':
            ch = struct.unpack_from('<H', d, p + 10)[0]
            bits = struct.unpack_from('<H', d, p + 22)[0]
        elif cid == b'data':
            data = d[p + 8:p + 8 + sz]
            break
        p += 8 + sz + (sz & 1)
    if bits != 16:
        raise SystemExit("ожидался WAV 16 бит")
    n = len(data) // (2 * ch)
    return np.frombuffer(data[:n * 2 * ch], dtype='<i2').reshape(n, ch).astype(np.float64)


def run_renderer(exe, vgm_bytes, loops=1, fade=0.0):
    """VGM (байты) -> стерео float (N, 2) в шкале int16. exe=None - эмулятор на Python."""
    if exe is None:
        import vgm_pyemu
        v = parse_vgm(vgm_bytes)
        events, end_t = repeat_loops(v, loops)
        return vgm_pyemu.render(events, end_t, v.ym_clk, v.sn_clk, v.sn_flags, SR)
    fd, src = tempfile.mkstemp(suffix='.vgm')
    os.close(fd)
    out = src[:-4] + '.wav'
    try:
        with open(src, 'wb') as f:
            f.write(vgm_bytes)
        r = subprocess.run([exe, f'--loops={loops}', f'--fade={fade}', '--bps=16', src, out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.isfile(out):
            raise SystemExit("vgm2wav завершился с ошибкой")
        return read_wav(out)
    finally:
        os.unlink(src)
        if os.path.isfile(out):
            os.unlink(out)


class VgmBuilder:
    """Минимальный генератор VGM 1.51 (YM2612 + SN76489)."""

    def __init__(self, ym_clk, sn_clk, sn_flags):
        self.ym_clk, self.sn_clk, self.sn_flags = ym_clk, sn_clk, sn_flags
        self.b = bytearray()
        self.t = 0

    def ym(self, port, reg, val):
        self.b += bytes([0x52 + port, reg, val & 0xFF])

    def sn(self, val):
        self.b += bytes([0x50, val & 0xFF])

    def wait(self, n):
        n = int(round(n))
        while n > 0:
            k = min(n, 65535)
            self.b += b'\x61' + struct.pack('<H', k)
            n -= k
            self.t += k

    def build(self):
        self.b += b'\x66'
        hdr = bytearray(0x80)
        hdr[0:4] = b'Vgm '
        struct.pack_into('<I', hdr, 4, len(hdr) + len(self.b) - 4)
        struct.pack_into('<I', hdr, 8, 0x151)
        struct.pack_into('<I', hdr, 0x0C, self.sn_clk)
        struct.pack_into('<I', hdr, 0x18, self.t)
        struct.pack_into('<H', hdr, 0x28, self.sn_flags[0])
        hdr[0x2A] = self.sn_flags[1]
        struct.pack_into('<I', hdr, 0x2C, self.ym_clk)
        struct.pack_into('<I', hdr, 0x34, 0x80 - 0x34)
        return bytes(hdr) + bytes(self.b)


def fm_regs_for(freq, fs):
    """F-num/block для частоты (fnum в 600..1200, как в таблицах драйверов)."""
    for blk in range(8):
        fnum = freq * (1 << (21 - blk)) / fs
        if fnum < 1200 or blk == 7:
            fnum = int(round(max(1, min(2047, fnum))))
            return blk, fnum
    return 7, 2047


def fm_actual_freq(blk, fnum, fs):
    return fnum * fs / float(1 << (21 - blk))


RENDER_TL = 24          # TL носителей при рендере патчей (-18 дБ): иначе канал YM2612 клиппирует
RENDER_GAIN = 10.0 ** (-0.75 * RENDER_TL / 20.0)


def write_patch(b, patch, blk, fnum, ch=0, base_tl=0):
    ops, b0, b4, lfo = patch
    car = ALG_CARRIERS[b0 & 7]
    port, i = divmod(ch, 3)
    for o, vals in zip((1, 2, 3, 4), ops):
        off = i + OP_OFF[o]
        r30, tl, r50, r60, r70, r80, r90 = vals
        if o in car:
            tl = min(127, tl + base_tl)
        b.ym(port, 0x30 + off, r30)
        b.ym(port, 0x40 + off, tl)
        b.ym(port, 0x50 + off, r50)
        b.ym(port, 0x60 + off, r60)
        b.ym(port, 0x70 + off, r70)
        b.ym(port, 0x80 + off, r80)
        b.ym(port, 0x90 + off, r90)
    b.ym(port, 0xB0 + i, b0)
    b.ym(port, 0xB4 + i, 0xC0 | b4)
    b.ym(0, 0x22, lfo)
    b.ym(port, 0xA4 + i, (blk << 3) | (fnum >> 8))
    b.ym(port, 0xA0 + i, fnum & 0xFF)


T_ON = SR            # 1 с звучания
T_REL = int(0.4 * SR)
T_GAP = int(0.12 * SR)


def keyon_code(ch):
    return (ch % 3) | (4 if ch >= 3 else 0)


class RenderJobs:
    """Пачка рендеров: калибровка PSG/DAC + FM-патчи на заданных высотах."""

    def __init__(self, v):
        self.v = v
        self.b = VgmBuilder(v.ym_clk or 7670453, v.sn_clk or 3579545, v.sn_flags)
        self.segs = {}
        self.fs = (v.ym_clk or 7670453) / 144.0
        self._calib()

    def _calib(self):
        b = self.b
        # PSG: тон ch0 att 0, делитель 254 (~440 Гц)
        b.sn(0x80 | 0x0E)          # ch0 tone low 4 bits (254 = 0x0FE -> low E)
        b.sn(0x0F)                 # high 6 bits = 0x0F
        b.sn(0x90 | 0)             # att 0
        b.sn(0xFF)                 # noise att off, etc.
        t0 = b.t
        b.wait(int(0.25 * SR))
        self.segs['psg'] = (t0 + int(0.05 * SR), t0 + int(0.25 * SR))
        b.sn(0x90 | 15)
        b.wait(int(0.1 * SR))
        # DAC: чередование 0x00 / 0xFF
        b.ym(0, 0x2B, 0x80)
        t0 = b.t
        for k in range(120):
            b.ym(0, 0x2A, 0xFF if k % 2 == 0 else 0x00)
            b.wait(60)
        self.segs['dac'] = (t0, b.t)
        b.ym(0, 0x2A, 0x80)
        b.ym(0, 0x2B, 0x00)
        b.wait(int(0.1 * SR))

    def add_patch(self, key, patch, freq):
        b = self.b
        blk, fnum = fm_regs_for(freq, self.fs)
        f_s = fm_actual_freq(blk, fnum, self.fs)
        # заглушить канал 0: key off, RR = 15
        b.ym(0, 0x28, 0x00)
        for o in (1, 2, 3, 4):
            b.ym(0, 0x80 + OP_OFF[o], 0x0F)
        b.wait(T_GAP)
        write_patch(b, patch, blk, fnum, 0, RENDER_TL)
        b.wait(8)
        b.ym(0, 0x28, 0xF0)
        t_on = b.t
        b.wait(T_ON)
        b.ym(0, 0x28, 0x00)
        b.wait(T_REL)
        self.segs[key] = (t_on, f_s)

    def render(self, exe):
        data = self.b.build()
        return run_renderer(exe, data, loops=1, fade=0.0)


# ═══════════════════════════════════════════════════════════════
# Сэмплы: луп, релиз
# ═══════════════════════════════════════════════════════════════

def rms_windows(x, w):
    n = len(x) // w
    if n == 0:
        return np.array([float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0])
    return np.sqrt((x[:n * w].reshape(n, w) ** 2).mean(axis=1))


def find_loop(x, P, s0, rms_ref, debug=False):
    """Лучший (start, len) для бесшовного лупа. P - ожидаемый период в сэмплах."""
    N = len(x)
    Lmax = min(int(0.5 * SR), (N - s0) // 2)
    if Lmax < 48:
        return None
    best = None
    L_list = []
    m = 1
    while True:
        L0 = m * P
        if L0 > Lmax or m > 400:
            break
        for L in (int(math.floor(L0)), int(math.ceil(L0))):
            if 32 <= L <= Lmax and (not L_list or L_list[-1] != L) and L not in L_list:
                L_list.append(L)
        m += 1
    if not L_list:
        L_list = [min(Lmax, max(32, int(round(P))))]
    smax = min(N - 2 * 32, s0 + int(0.5 * SR))
    for L in L_list:
        W = min(L, 1024)
        if N - L - W <= s0:
            continue
        e = np.abs(x[L:] - x[:-L])
        cs = np.concatenate(([0.0], np.cumsum(e)))
        ma = (cs[W:] - cs[:-W]) / W                     # ma[s] = mean e[s:s+W]
        hi = min(len(ma), smax + 1, N - L - W + 1)
        if hi <= s0:
            continue
        seg = ma[s0:hi]
        k = int(seg.argmin())
        err = float(seg[k]) / (rms_ref + 1e-9)
        if best is None or err < best[0]:
            best = (err, s0 + k, L)
        if err < 0.02:
            break
    if best is None:
        return None
    return best[1], best[2], best[0]


def make_fm_sample(xs, f_s, tail):
    """xs - моно-запись звучания (T_ON отсчётов), f_s - частота в регистрах.
    Возвращает (данные float, loop|None, peak, release_seconds)."""
    peak = float(np.abs(xs).max()) or 1.0
    r = rms_windows(xs, 441)
    mx = float(r.max()) or 1.0
    final = float(np.median(r[-8:]))
    P = SR / f_s
    # релиз
    rel = 0.4
    if tail is not None and len(tail):
        lvl0 = float(np.sqrt(np.mean(xs[-441:] ** 2))) or 1e-9
        rt = rms_windows(tail, 220)
        idx = np.nonzero(rt < 0.25 * lvl0)[0]
        rel = (idx[0] * 220 / SR) / 0.75 if len(idx) else 0.4 / 0.75
    if final < 0.03 * mx:
        # затухающий (перкуссионный) звук
        thr = 0.01 * mx
        idx = np.nonzero(rms_windows(xs, 220) > thr)[0]
        end = int((idx[-1] + 1) * 220) if len(idx) else len(xs)
        end = max(end, 1024)
        data = xs[:end].copy()
        k = min(128, len(data) // 4)
        data[-k:] *= np.linspace(1, 0, k)
        return data, None, peak, rel, mx
    ok = (r > 0.8 * final) & (r < 1.25 * final)
    s0 = len(xs) // 2
    bad = np.nonzero(~ok)[0]
    s0 = int((bad[-1] + 1) * 441) if len(bad) else 0
    if s0 >= len(xs) // 2:
        s0 = len(xs) // 2
    res = find_loop(xs, P, s0, final)
    if res is None:
        return xs, None, peak, rel, mx
    s, L, err = res
    return xs[:s + L].copy(), (s, L), peak, rel, mx


def square_cycle(c):
    return np.where(np.arange(c) < c // 2, 1.0, -1.0)


def noise_sample(fb, nf_n, rate_shift, seconds=2.0):
    """LFSR SN76489: 16 бит, белый шум (tap 0x0009) или периодический."""
    nshift = int(seconds * rate_shift) + 2
    reg = 0x8000
    bits = np.empty(nshift, dtype=np.float64)
    for i in range(nshift):
        bits[i] = 1.0 if (reg & 1) else -1.0
        if fb:
            fbk = bin(reg & 0x0009).count('1') & 1
        else:
            fbk = reg & 1
        reg = (reg >> 1) | (fbk << 15)
    n = int(seconds * SR)
    idx = np.minimum((np.arange(n) * rate_shift / SR).astype(np.int64), nshift - 1)
    return bits[idx]


# ═══════════════════════════════════════════════════════════════
# Темп, каналы
# ═══════════════════════════════════════════════════════════════

def detect_row_interval(times, max_row=0.06):
    t = np.array(sorted(times), dtype=np.float64)
    if len(t) < 4:
        return 0.05, 0.0, 0.0
    grid = np.arange(0.02, 0.45, 0.0002)
    sc = np.array([abs(np.exp(2j * np.pi * t / d).mean()) for d in grid])
    best = sc.max()
    if best < 0.25:
        return 0.025, 0.0, float(best)
    peaks = [i for i in range(1, len(grid) - 1)
             if sc[i] >= sc[i - 1] and sc[i] >= sc[i + 1] and sc[i] >= 0.9 * best]
    d0 = grid[max(peaks, key=lambda i: grid[i])]
    fine = np.arange(d0 - 0.001, d0 + 0.001, 0.000002)
    fs = np.array([abs(np.exp(2j * np.pi * t / d).mean()) for d in fine])
    d0 = float(fine[int(fs.argmax())])
    m = max(1, int(math.ceil(d0 / max_row - 1e-9)))
    delta = d0 / m
    z = np.exp(2j * np.pi * t / delta).mean()
    t0 = (math.atan2(z.imag, z.real) / (2 * math.pi)) * delta
    return delta, t0, float(abs(np.exp(2j * np.pi * t / d0).mean()))


def tempo_schedule(bpm, speed, true_delta, nrows, block=16, rate=48000):
    """BPM в XM целое, а шаг сетки нот - нет. Ноты стоят на сетке true_delta (ритм точный),
    а BPM по ходу меняется на близкие значения (Fxx), чтобы время строк не уходило от настоящего.
    Плееры (OpenMPT, FT2) округляют длину тика вниз до целого числа отсчётов, это учтено (rate).
    -> (стартовый BPM, {строка: новый BPM})."""
    def row_dur(b):
        return speed * int(rate * 2.5 / b) / float(rate)
    ideal = 2.5 * speed / true_delta
    if not 32 <= ideal <= 255:
        return bpm, {}
    cands = range(max(32, int(ideal) - 3), min(255, int(ideal) + 4) + 1)
    t_xm = 0.0
    cur = start = None
    sched = {}
    for r in range(0, nrows, block):
        n = min(block, nrows - r)
        target = (r + n) * true_delta
        best = min(cands, key=lambda b: abs(t_xm + n * row_dur(b) - target))
        if cur is not None and best != cur and \
                abs(t_xm + n * row_dur(cur) - target) - abs(t_xm + n * row_dur(best) - target) < 0.0005:
            best = cur                      # не дёргаем темп ради долей миллисекунды
        if cur is None:
            start = best
        elif best != cur:
            sched[r] = best
        cur = best
        t_xm += n * row_dur(best)
    return start, sched


def interval_to_bpm_speed(delta):
    best = None
    for speed in range(1, 13):
        bpm = int(round(2.5 * speed / delta))
        if not 32 <= bpm <= 255:
            continue
        real = 2.5 * speed / bpm
        err = abs(real - delta) / delta
        score = (round(err, 4), abs(speed - 6))
        if best is None or score < best[0]:
            best = (score, bpm, speed, real)
    if best is None:
        speed = 1 if delta < 0.05 else 12
        bpm = max(32, min(255, int(round(2.5 * speed / delta))))
        return bpm, speed, 2.5 * speed / bpm
    return best[1], best[2], best[3]


class Cell:
    __slots__ = ('note', 'inst', 'vol', 'eff', 'par')

    def __init__(self):
        self.note = self.inst = self.vol = self.eff = self.par = 0


def allocate_channels(notes, max_channels):
    busy_until = []
    last_inst = []
    started = []
    owner = []
    for n in notes:
        free = [c for c in range(len(busy_until)) if busy_until[c] <= n.start_row]
        same = [c for c in free if last_inst[c] == n.inst]
        if same:
            c = max(same, key=lambda c: busy_until[c])
        elif free:
            c = min(free)
        elif len(busy_until) < max_channels:
            busy_until.append(0)
            last_inst.append(-1)
            started.append(0)
            owner.append(None)
            c = len(busy_until) - 1
        else:
            c = min(range(len(busy_until)), key=lambda c: started[c])
            prev = owner[c]
            if prev is not None:
                prev.end_row = max(prev.start_row + 1, min(prev.end_row, n.start_row))
        n.channel = c
        busy_until[c] = n.end_row + n.tail_rows
        last_inst[c] = n.inst
        started[c] = n.start_row
        owner[c] = n
    return len(busy_until)


# ═══════════════════════════════════════════════════════════════
# XM / MIDI
# ═══════════════════════════════════════════════════════════════

class XMWriter:
    def __init__(self, title, nch, bpm, speed):
        self.title, self.nch, self.bpm, self.speed = title, nch, bpm, speed
        self.patterns = []
        self.instruments = []

    def set_rows(self, grid, nrows):
        rpp = 64
        npat = max(1, (nrows + rpp - 1) // rpp)
        if npat > 256:
            print(f"[xm] ВНИМАНИЕ: {npat} паттернов > 256, хвост обрезан (используйте --compact)",
                  file=sys.stderr)
            npat = 256
        self.patterns = []
        for p in range(npat):
            self.patterns.append([[grid.get((p * rpp + r, c)) for c in range(self.nch)]
                                  for r in range(rpp)])

    def write(self, path):
        with open(path, 'wb') as f:
            f.write(b'Extended Module: ')
            f.write(self.title.encode('ascii', 'replace')[:20].ljust(20, b' '))
            f.write(b'\x1a')
            f.write(b'VGM2XM Converter    ')
            f.write(struct.pack('<H', 0x0104))
            f.write(struct.pack('<I', 276))
            f.write(struct.pack('<HHHHHHHH', len(self.patterns), 0, self.nch,
                                len(self.patterns), len(self.instruments), 1,
                                self.speed, self.bpm))
            order = bytearray(256)
            for i in range(len(self.patterns)):
                order[i] = i
            f.write(order)
            for pat in self.patterns:
                self._write_pattern(f, pat)
            for inst in self.instruments:
                self._write_instrument(f, inst)

    def _write_pattern(self, f, pat):
        packed = bytearray()
        for row in pat:
            for c in row:
                if c is None or not (c.note or c.inst or c.vol or c.eff or c.par):
                    packed.append(0x80)
                    continue
                flag = 0x80
                if c.note: flag |= 1
                if c.inst: flag |= 2
                if c.vol: flag |= 4
                if c.eff: flag |= 8
                if c.par: flag |= 16
                packed.append(flag)
                if c.note: packed.append(c.note)
                if c.inst: packed.append(c.inst)
                if c.vol: packed.append(c.vol)
                if c.eff: packed.append(c.eff)
                if c.par: packed.append(c.par)
        f.write(struct.pack('<IBHH', 9, 0, len(pat), len(packed)))
        f.write(packed)

    def _write_instrument(self, f, inst):
        name = inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00')
        smps = inst['samples']
        ihs = 263
        f.write(struct.pack('<I', ihs))
        f.write(name)
        f.write(struct.pack('<BH', 0, len(smps)))
        if not smps:
            return
        f.write(struct.pack('<I', 40))
        km = bytearray(96)
        for i, s in enumerate(inst['keymap'][:96]):
            km[i] = s
        f.write(km)
        ve = bytearray(48)
        struct.pack_into('<HHHH', ve, 0, 0, 64, 1000, 64)
        f.write(ve)
        pe = bytearray(48)
        struct.pack_into('<HHHH', pe, 0, 0, 32, 1000, 32)
        f.write(pe)
        # 2 точки огибающей громкости (плоская, нужна, чтобы работал fadeout)
        f.write(bytes([2, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0]))
        f.write(struct.pack('<H', int(inst.get('fadeout', 0))))
        f.write(b'\x00' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
        for s in smps:
            data = s['data']
            loop = s['loop']
            ls, ll = loop if loop else (0, 0)
            f.write(struct.pack('<III', len(data) * 2, ls * 2, ll * 2))
            f.write(struct.pack('<BbBBb', 64, s['finetune'], (1 if loop else 0) | 0x10, 128, s['rel']))
            f.write(b'\x00')
            f.write(name)
        for s in smps:
            d = np.diff(s['data'].astype(np.int32), prepend=0)
            d = ((d + 32768) % 65536 - 32768).astype('<i2')
            f.write(d.tobytes())


def vlq(v):
    v = max(0, int(v))
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


DRUM_NOTES = [36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56]


def write_midi(path, title, midi_events):
    tpqn = 480
    tracks = collections.defaultdict(list)
    for tick, order, ch, data in midi_events:
        tracks[ch].append((tick, order, data))
    out = bytearray()
    tempo = [(0, 0, bytes([0xFF, 0x51, 0x03]) + (500000).to_bytes(3, 'big'))]
    if title:
        t = title.encode('ascii', 'replace')[:100]
        tempo.append((0, 1, bytes([0xFF, 0x03, len(t)]) + t))
    all_tracks = [tempo] + [tracks[c] for c in sorted(tracks)]
    out += b'MThd' + struct.pack('>IHHH', 6, 1, len(all_tracks), tpqn)
    for tr in all_tracks:
        tr = sorted(tr, key=lambda e: (e[0], e[1]))
        body = bytearray()
        prev = 0
        for tick, _, data in tr:
            body += vlq(tick - prev) + data
            prev = tick
        body += vlq(0) + b'\xFF\x2F\x00'
        out += b'MTrk' + struct.pack('>I', len(body)) + body
    with open(path, 'wb') as f:
        f.write(out)


# ═══════════════════════════════════════════════════════════════
# Инструменты (патч -> зоны -> сэмплы)
# ═══════════════════════════════════════════════════════════════

class Zone:
    def __init__(self, lo, hi, n0):
        self.lo, self.hi, self.n0 = lo, hi, n0     # диапазон нот и опорная нота (реальные номера XM)
        self.data = None                           # float, нормирован на пик 1.0
        self.loop = None
        self.f_s = 0.0                             # частота, которой соответствует запись при 44.1 кГц
        self.peak = 1.0                            # реальный пик (в единицах 16-бит рендера)
        self.rate = SR                             # частота дискретизации записи


class Instrument:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name
        self.zones = []
        self.rel_s = 0.0
        self.notes = []
        self.perc = False
        self.bright = 0.0
        self.centroid = 0.0
        self.fold = 0

    def zone_for(self, n):
        for z in self.zones:
            if z.lo <= n <= z.hi:
                return z
        return min(self.zones, key=lambda z: min(abs(n - z.lo), abs(n - z.hi)))


def note_of(freq, off):
    """Вещественный номер ноты XM (49 = C-4) по частоте."""
    return 69.0 + 12.0 * math.log2(freq / 440.0) - off - 11.0


def freq_of(n, off):
    return 440.0 * 2.0 ** ((n + 11.0 - 69.0 + off) / 12.0)


def to_mono(w):
    return w.mean(axis=1) if w.ndim == 2 else w


# ═══════════════════════════════════════════════════════════════
# Конвертация
# ═══════════════════════════════════════════════════════════════

def convert(args):
    in_path = args.input
    base = os.path.splitext(args.output or in_path)[0]
    only = args.midi_only or args.wav_only
    want_xm = args.xm or args.all or not only
    want_midi = args.midi or args.all or args.midi_only
    want_wav = args.wav or args.all or args.wav_only

    v = load_vgm(in_path)
    title = os.path.basename(base)
    if v.other:
        print(f"[vgm] ВНИМАНИЕ: чипы {', '.join(v.other)} не поддерживаются, будут пропущены",
              file=sys.stderr)
    exe = pick_renderer(args.renderer)

    events, end_t = repeat_loops(v, args.loops)
    print(f"[vgm] {os.path.basename(in_path)}: {end_t / SR:.1f} с, "
          f"YM2612 {v.ym_clk or '-'} Гц, SN76489 {v.sn_clk or '-'} Гц, событий: {len(events)}")

    if want_wav:
        wav_path = args.wav_file or base + '.wav'
        if exe is None:
            print("[wav] рендер WAV на Python: примерно 0.1-0.3x от реального времени ...")
        w = run_renderer(exe, v.raw, loops=args.loops, fade=0.0)
        with wave.open(wav_path, 'wb') as f:
            f.setnchannels(2)
            f.setsampwidth(2)
            f.setframerate(SR)
            f.writeframes(np.clip(w, -32768, 32767).astype('<i2').tobytes())
        print(f"[wav] {wav_path}")
    if not (want_xm or want_midi):
        return

    tr = Tracker(v)
    for t, op, a, b in events:
        tr.feed(t, op, a, b)
    tr.finish(end_t)
    notes = tr.notes
    if not notes:
        raise SystemExit("нот не найдено")
    cnt = collections.Counter(n.kind for n in notes)
    print(f"[vgm] нот: FM {cnt['fm']}, PSG {cnt['psg']}, шум {cnt['noise']}, DAC {cnt['dac']}")

    for n in notes:
        n.t0 = n.start / SR
        n.t1 = max(n.end, n.start + 1) / SR
        n.times = [c[0] / SR for c in n.cur]

    # ── подстройка: сдвиг сетки полутонов (чип не всегда настроен на A=440) ──
    ps, wt = [], []
    for n in notes:
        if n.kind in ('fm', 'psg') and n.cur and n.cur[0][1] > 0:
            ps.append(12.0 * math.log2(n.cur[0][1] / 440.0))
            wt.append(math.sqrt(max(0.05, n.t1 - n.t0)))
    off = 0.0
    if ps:
        ang = np.array(ps) * 2 * np.pi
        wt = np.array(wt)
        z = (wt * np.exp(1j * ang)).sum() / wt.sum()
        if abs(z) > 0.3:
            off = math.atan2(z.imag, z.real) / (2 * math.pi)
        print(f"[xm] сетка полутонов: сдвиг {off * 100:+.0f} центов (концентрация {abs(z):.2f})")
    shift = args.transpose + 12 * args.octave

    # ── инструменты ──
    insts = []                     # Instrument
    inst_of = {}                   # ключ -> Instrument
    patch_use = collections.Counter(n.patch for n in notes if n.kind == 'fm')
    patches = [p for p, _ in patch_use.most_common()]
    if len(patches) > MAX_XM_INSTRUMENTS - 6:
        keep = set(patches[:MAX_XM_INSTRUMENTS - 6])
        flat = {p: p for p in keep}
        def dist(a, b):
            return sum(abs(x - y) for oa, ob in zip(a[0], b[0]) for x, y in zip(oa, ob)) + \
                   abs(a[1] - b[1])
        for p in patches:
            if p not in keep:
                flat[p] = min(keep, key=lambda q: dist(p, q))
        print(f"[xm] ВНИМАНИЕ: патчей {len(patches)} > {MAX_XM_INSTRUMENTS - 6}, редкие слиты с похожими",
              file=sys.stderr)
        for n in notes:
            if n.kind == 'fm':
                n.patch = flat[n.patch]
        patches = [p for p in patches if p in keep]

    first_t = {}
    for n in notes:
        key = (n.kind, n.patch if n.kind in ('fm', 'noise') else (n.dac[0] if n.kind == 'dac' else None))
        if n.kind == 'dac':
            key = ('dac', hash(n.dac[0]) & 0xFFFFFFFF, int(round(n.dac[1])))
        first_t.setdefault(key, n.t0)
        n.key = key
    order = sorted(first_t, key=lambda k: first_t[k])

    fm_zone_w = max(1, args.zone_width)
    # зоны FM
    by_key = collections.defaultdict(list)
    for n in notes:
        by_key[n.key].append(n)
    names = {'fm': 'FM', 'psg': 'PSG square', 'noise': 'PSG noise', 'dac': 'DAC'}
    for key in order:
        kind = key[0]
        lst = by_key[key]
        ins = Instrument(kind, '')
        ins.notes = lst
        ins.key = key
        insts.append(ins)
        inst_of[key] = ins
        if kind == 'fm':
            ns = []
            for n in lst:
                for c in n.cur:
                    if c[1] > 0:
                        ns.append(note_of(c[1], off))
            ns = sorted(int(round(x)) for x in ns)
            ns = [min(96, max(1, x)) for x in ns]
            if not ns:
                ns = [49]
            zones = []
            lo = ns[0]
            while lo <= ns[-1]:
                hi = lo + fm_zone_w - 1
                inside = [x for x in ns if lo <= x <= hi]
                if inside:
                    zones.append(Zone(lo, hi, int(round((inside[0] + inside[-1]) / 2))))
                lo = hi + 1
                if len(zones) >= 16:
                    break
            if len(zones) >= 16 and zones[-1].hi < ns[-1]:
                zones[-1].hi = 96
            zones[0].lo = 1
            zones[-1].hi = 96
            ins.zones = zones

    # ── рендер FM-патчей ──
    jobs = RenderJobs(v)
    zone_list = []
    for ins in insts:
        if ins.kind != 'fm':
            continue
        for zi, z in enumerate(ins.zones):
            key = (id(ins), zi)
            f_ref = freq_of(z.n0, off)
            jobs.add_patch(key, ins.key[1], f_ref)
            zone_list.append((ins, z, key))
    print(f"[vgm] рендер сэмплов: патчей {len([i for i in insts if i.kind == 'fm'])}, "
          f"сэмплов {len(zone_list)}, ~{jobs.b.t / SR:.0f} с звука ...")
    wav = jobs.render(exe)
    mono = to_mono(wav)

    a, b_ = jobs.segs['psg']
    psg_peak = float(np.abs(mono[a:b_]).max()) or 8000.0
    a, b_ = jobs.segs['dac']
    dac_peak = float(np.abs(mono[a:b_]).max()) or 8000.0
    dac_scale = dac_peak / 32512.0
    if args.debug:
        print(f'    калибровка: PSG пик {psg_peak:.0f}, DAC пик {dac_peak:.0f}', file=sys.stderr)

    for ins, z, key in zone_list:
        t_on, f_s = jobs.segs[key]
        xs = mono[t_on:t_on + T_ON].copy()
        tail = mono[t_on + T_ON:t_on + T_ON + T_REL]
        data, loop, peak, rel, mx = make_fm_sample(xs, f_s, tail)
        z.data, z.loop, z.f_s = data, loop, f_s
        z.peak = peak / RENDER_GAIN          # пик при TL = 0 (без клиппинга)
        z.clipped = peak >= 16300
        if args.debug:
            ls = f"loop {loop[0]}+{loop[1]}" if loop else "one-shot"
            print(f"    инстр. FM z{z.lo}-{z.hi} n0={z.n0}: {len(data)} отсч., {ls}, "
                  f"пик {peak:.0f}, релиз {rel * 1000:.0f} мс", file=sys.stderr)
        if z is ins.zones[0]:
            ins.rel_s = rel
            ins.perc = loop is None
            sp = np.abs(np.fft.rfft(data[:8192] * np.hanning(len(data[:8192]))))
            fr = np.fft.rfftfreq(len(data[:8192]), 1.0 / SR)
            ins.centroid = float((sp * fr).sum() / (sp.sum() + 1e-9))
            ins.bright = ins.centroid / f_s

    # ── PSG / шум / DAC ──
    for ins in insts:
        if ins.kind == 'psg':
            zs = [(1, 47, 36, 128), (48, 71, 60, 64), (72, 96, 84, 32)]
            for lo, hi, n0, c in zs:
                z = Zone(lo, hi, n0)
                z.data = square_cycle(c)
                z.loop = (0, c)
                z.f_s = SR / c
                z.peak = psg_peak
                ins.zones.append(z)
        elif ins.kind == 'noise':
            fb, nf = ins.key[1]
            n_ = (16, 32, 64, 32)[nf]
            shift_rate = v.sn_clk / (32.0 * n_)
            z = Zone(1, 96, 0)
            z.data = noise_sample(fb, nf, shift_rate, seconds=1.0 if fb == 0 else 2.0)
            z.loop = (0, len(z.data))
            z.f_s = shift_rate
            z.peak = psg_peak
            z.n0 = int(round(min(96, max(1, note_of(shift_rate, off)))))
            ins.zones.append(z)
        elif ins.kind == 'dac':
            data, rate = ins.notes[0].dac
            pcm = (np.frombuffer(data, dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
            z = Zone(1, 96, 49)
            z.data = pcm
            z.loop = None
            z.rate = rate
            z.f_s = rate
            z.peak = float(np.abs(pcm).max() or 1.0) * dac_scale
            ins.zones.append(z)
            ins.perc = True
            ins.centroid = 0.0
            sp = np.abs(np.fft.rfft(pcm[:8192] * np.hanning(len(pcm[:8192]))))
            fr = np.fft.rfftfreq(len(pcm[:8192]), 1.0 / rate)
            ins.centroid = float((sp * fr).sum() / (sp.sum() + 1e-9))

    # ── уровни ──
    peaks = []
    for n in notes:
        ins = inst_of[n.key]
        z = ins.zone_for(int(round(note_of(n.cur[0][1], off))) if n.cur[0][1] > 0 else 49)
        peaks.append(max(c[2] for c in n.cur) * z.peak)
    norm = (float(np.percentile(peaks, 99)) or 1.0) * args.norm_scale
    if args.debug:
        print(f'    норма громкости: {norm:.0f}', file=sys.stderr)

    # ── сетка строк ──
    onsets = [n.t0 for n in notes]
    t0g = 0.0
    if args.bpm:
        speed = args.speed or 6
        bpm = args.bpm
        true_delta = 2.5 * speed / bpm
        delta = true_delta
    else:
        # драйверы Mega Drive играют по кадрам (60/50 Гц): если ноты ложатся на кадры -
        # строка = кадр, это точнее любой "музыкальной" сетки
        s_on = np.array(sorted(n.start for n in notes), dtype=np.float64)
        frame = None
        for P in (735, 882):
            z = np.exp(2j * np.pi * s_on / P).mean()
            if abs(z) >= 0.6 and (frame is None or abs(z) > frame[1]):
                frame = (P, abs(z), math.atan2(z.imag, z.real) / (2 * math.pi) * P)
        if frame is not None and not args.no_frame_grid:
            true_delta = frame[0] / SR
            t0g = frame[2] / SR
            coh = frame[1]
            print(f"[xm] ноты привязаны к кадрам {SR / frame[0]:.0f} Гц (когерентность {coh:.2f})")
        else:
            true_delta, t0g, coh = detect_row_interval(onsets)
        true_delta *= args.compact
        bpm, speed, delta = interval_to_bpm_speed(true_delta)
        if args.speed:
            speed = args.speed
            bpm = max(32, min(255, int(round(2.5 * speed / true_delta))))
            delta = 2.5 * speed / bpm
        print(f"[xm] сетка нот: {true_delta * 1000:.3f} мс/строка, когерентность {coh:.2f}")
    print(f"[xm] строка = {delta * 1000:.1f} мс  (BPM {bpm}, speed {speed})")
    tick = 2.5 / bpm

    for n in notes:
        n.start_row = int(round((n.t0 - t0g) / true_delta))
        n.end_row = max(n.start_row + 1, int(round((n.t1 - t0g) / true_delta)))
    sh = min(n.start_row for n in notes)
    for n in notes:
        n.start_row -= sh
        n.end_row -= sh
    row_t0 = t0g + sh * true_delta

    # ── fold и fadeout ──
    for ins in insts:
        if ins.kind in ('fm', 'psg'):
            vals = []
            for n in ins.notes:
                for c in n.cur:
                    if c[1] > 0:
                        vals.append(note_of(c[1], off) + shift)
            lo, hi = (min(vals), max(vals)) if vals else (49, 49)
            fold = 0
            if hi + 0.5 > 96:
                fold = -int(math.ceil((hi + 0.5 - 96) / 12.0))
            elif lo - 0.5 < 1:
                fold = int(math.ceil((1 - (lo - 0.5)) / 12.0))
            ins.fold = fold
        fade_s = ins.rel_s
        if ins.kind == 'fm' and fade_s > 6 * tick:
            ins.fadeout = int(min(4095, max(1, round(32768.0 * tick / fade_s))))
        else:
            ins.fadeout = 0
    for n in notes:
        ins = inst_of[n.key]
        n.tail_rows = 0
        if ins.fadeout:
            n.tail_rows = min(16, int(math.ceil(ins.rel_s / delta)))

    # ── инструменты XM ──
    for i, ins in enumerate(insts):
        ins.index = i + 1
    for n in notes:
        n.inst = inst_of[n.key].index
    if len(insts) > MAX_XM_INSTRUMENTS:
        raise SystemExit(f"инструментов {len(insts)} > {MAX_XM_INSTRUMENTS}; уменьшите число патчей")

    xm_insts = []
    for ins in insts:
        smps = []
        keymap = [1] * 96
        for zi, z in enumerate(ins.zones):
            data = z.data
            pk = float(np.abs(data).max()) or 1.0
            d16 = np.clip(np.round(data * (32767.0 / pk)), -32768, 32767).astype(np.int16)
            z.cell0 = z.n0 + shift + 12 * ins.fold
            nominal = freq_of(z.n0, off)
            if ins.kind == 'dac':
                tune = 12.0 * math.log2(z.rate / XM_C4_RATE)
                z.cell0 = 49
            else:
                # при ноте z.n0 запись должна звучать на nominal Гц
                play_rate = SR * nominal / z.f_s
                tune = 12.0 * math.log2(play_rate / XM_C4_RATE) - (z.cell0 - 49)
            rel_ = int(math.floor(tune))
            fine = int(round((tune - rel_) * 128))
            if fine > 127:
                rel_, fine = rel_ + 1, fine - 128
            rel_ = max(-96, min(95, rel_))
            smps.append({'data': d16, 'loop': z.loop, 'rel': rel_, 'finetune': max(-128, min(127, fine))})
            for cell in range(1, 97):
                n_real = cell - shift - 12 * ins.fold
                if z.lo <= n_real <= z.hi:
                    keymap[cell - 1] = zi
        # ячейки вне зон
        for cell in range(1, 97):
            n_real = cell - shift - 12 * ins.fold
            if not any(z.lo <= n_real <= z.hi for z in ins.zones):
                zz = ins.zone_for(n_real)
                keymap[cell - 1] = ins.zones.index(zz)
        nm = f"{ins.index:02d} {names[ins.kind]}"
        xm_insts.append({'name': nm, 'samples': smps, 'keymap': keymap, 'fadeout': ins.fadeout})
    n_samples = sum(len(x['samples']) for x in xm_insts)
    print(f"[xm] инструментов: {len(xm_insts)}, сэмплов: {n_samples}")

    nch = allocate_channels(notes, args.channels)
    nch = max(2, nch + (nch & 1))
    nch = min(nch, MAX_XM_CHANNELS)
    print(f"[xm] каналов: {nch}")

    def cur_idx(n, t):
        return max(0, bisect.bisect_right(n.times, t) - 1)

    def cell_note(n, ins, freq):
        if ins.kind == 'dac':
            return 49
        return note_of(freq, off) + shift + 12 * ins.fold

    # ── XM ──
    if want_xm:
        grid = {}
        last_row = 0
        by_chan = collections.defaultdict(list)
        for n in notes:
            by_chan[n.channel].append(n)

        def put(row, ch):
            c = grid.get((row, ch))
            if c is None:
                c = grid[(row, ch)] = Cell()
            return c

        for ch, lst in by_chan.items():
            lst.sort(key=lambda n: n.start_row)
            for k, n in enumerate(lst):
                ins = inst_of[n.key]
                nxt = lst[k + 1].start_row if k + 1 < len(lst) else None
                end_row = n.end_row if nxt is None else min(n.end_row, nxt)
                n.end_row = end_row
                last_row = max(last_row, end_row)
                z0 = ins.zone_for(int(round(note_of(n.cur[0][1], off))) if n.cur[0][1] > 0 else 49)

                def vol_of(lv, z):
                    return max(1, min(64, int(round(lv * z.peak / norm * 64.0)))) if lv > 0 else 0

                c0 = put(n.start_row, ch)
                f0 = n.cur[0][1]
                x0 = cell_note(n, ins, f0) if f0 > 0 else 49
                cur_cell = max(1, min(96, int(round(x0))))
                c0.note = cur_cell
                c0.inst = n.inst
                last_vol = vol_of(n.cur[0][2], z0)
                if ins.kind == 'dac':
                    last_vol = max(1, min(64, int(round(z0.peak / norm * 64.0))))
                c0.vol = 0x10 + last_vol
                last_pan = n.cur[0][3]
                if not args.no_pan and last_pan != 128:
                    c0.eff, c0.par = 0x08, max(1, min(255, last_pan))
                last_pitch = x0
                if ins.kind != 'dac':
                    for row in range(n.start_row + 1, end_row):
                        t = row_t0 + row * true_delta
                        i = cur_idx(n, t)
                        _, f, lv, pan = n.cur[i]
                        c = None
                        vv = vol_of(lv, z0)
                        if vv != last_vol and abs(vv - last_vol) >= max(1, int(0.06 * last_vol)):
                            c = put(row, ch)
                            c.vol = 0x10 + vv
                            last_vol = vv
                        if f > 0:
                            pf = cell_note(n, ins, f)
                            if abs(pf - last_pitch) >= args.bend_threshold:
                                nc = max(1, min(96, int(round(pf))))
                                if nc != cur_cell:
                                    c = c or put(row, ch)
                                    c.note = nc
                                    c.eff, c.par = 0x03, 0xFF
                                    cur_cell = nc
                                last_pitch = pf
                        if not args.no_pan and abs(pan - last_pan) >= 40:
                            c = c or put(row, ch)
                            if not c.eff:
                                c.eff, c.par = 0x08, max(1, min(255, pan))
                                last_pan = pan
                if nxt is None or end_row < nxt:
                    off_c = put(end_row, ch)
                    if not off_c.note:
                        if ins.fadeout:
                            off_c.note = 97
                        elif ins.kind != 'dac':
                            off_c.vol = 0x10
        if last_row + 1 > 256 * 64:
            last_row = 256 * 64 - 1
        start_bpm, sched = tempo_schedule(bpm, speed, true_delta, last_row + 2)
        for r, b in sched.items():
            for chn in range(nch):
                c = grid.get((r, chn))
                if c is None:
                    c = grid[(r, chn)] = Cell()
                if not c.eff:
                    c.eff, c.par = 0x0F, b
                    break
        xm = XMWriter(title, nch, start_bpm, speed)
        xm.set_rows(grid, last_row + 2)
        xm.instruments = xm_insts
        xm_path = base + '.xm'
        xm.write(xm_path)
        size = os.path.getsize(xm_path) / 1024.0
        print(f"[xm] {xm_path}  ({len(xm.patterns)} паттернов, {size:.0f} КБ)")

    # ── MIDI ──
    if want_midi:
        ev = []
        tps = 960.0
        fm_progs = {}
        for ins in insts:
            if ins.kind == 'fm':
                med = np.median([note_of(c[1], off) for n in ins.notes for c in n.cur if c[1] > 0] or [49])
                if ins.perc:
                    prog = 38 if med < 40 else (36 if med < 52 else (11 if ins.bright < 8 else 12))
                else:
                    if med < 40:
                        prog = 39
                    elif ins.bright < 2.5:
                        prog = 89
                    elif ins.bright < 6:
                        prog = 80
                    else:
                        prog = 81
                fm_progs[ins.index] = prog
        drums = {}
        dac_ins = sorted([i for i in insts if i.kind == 'dac'], key=lambda i: i.centroid)
        for k, i in enumerate(dac_ins):
            drums[i.index] = DRUM_NOTES[min(len(DRUM_NOTES) - 1, k * len(DRUM_NOTES) // max(1, len(dac_ins)))]
        noise_drum = {0: 42, 1: 38, 2: 36, 3: 40}
        midi_ch = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 9}
        init_done = set()
        ch_prog = {}
        for n in sorted(notes, key=lambda n: n.t0):
            ins = inst_of[n.key]
            mch = midi_ch.get(n.voice, 9)
            z0 = ins.zone_for(int(round(note_of(n.cur[0][1], off))) if n.cur[0][1] > 0 else 49)
            tick0 = int(round(n.t0 * tps))
            tick1 = max(tick0 + 20, int(round(n.t1 * tps)))
            if ins.kind in ('dac', 'noise'):
                mch = 9
                key = drums[ins.index] if ins.kind == 'dac' else noise_drum[ins.key[1][1]]
            else:
                key = int(round(note_of(n.cur[0][1], off) + 11 + shift))
                key = max(0, min(127, key))
            if mch not in init_done:
                init_done.add(mch)
                ev += [(0, 0, mch, bytes([0xB0 | mch, 101, 0])), (0, 0, mch, bytes([0xB0 | mch, 100, 0])),
                       (0, 0, mch, bytes([0xB0 | mch, 6, 12])), (0, 0, mch, bytes([0xB0 | mch, 38, 0]))]
            if mch != 9:
                prog = fm_progs.get(ins.index, 80)
                if ch_prog.get(mch) != prog:
                    ev.append((tick0, 1, mch, bytes([0xC0 | mch, prog])))
                    ch_prog[mch] = prog
            maxlv = max(c[2] for c in n.cur)
            vel = max(1, min(127, int(round(maxlv * z0.peak / norm * 110))))
            ev.append((tick0, 2, mch, bytes([0xB0 | mch, 10, max(0, min(127, n.cur[0][3] // 2))])))
            ev.append((tick0, 3, mch, bytes([0x90 | mch, key, vel])))
            if mch != 9:
                last_b = last_e = None
                for (tt, f, lv, pan) in n.cur:
                    tk = int(round(tt / SR * tps))
                    if tk >= tick1:
                        break
                    if f > 0:
                        bend = max(-12.0, min(12.0, note_of(f, off) + 11 + shift - key))
                        if last_b is None or abs(bend - last_b) >= 0.06:
                            val = int(8192 + bend / 12.0 * 8191)
                            ev.append((tk, 2, mch, bytes([0xE0 | mch, val & 0x7F, (val >> 7) & 0x7F])))
                            last_b = bend
                    e = max(1, min(127, int(round(lv / max(maxlv, 1e-6) * 127))))
                    if last_e is None or abs(e - last_e) >= 3:
                        ev.append((tk, 2, mch, bytes([0xB0 | mch, 11, e])))
                        last_e = e
            ev.append((tick1, 0, mch, bytes([0x80 | mch, key, 0])))
        midi_path = base + '.mid'
        write_midi(midi_path, title, ev)
        print(f"[mid] {midi_path}")


def main():
    ap = argparse.ArgumentParser(description="VGM/VGZ (Mega Drive: YM2612 + SN76489) -> XM / MIDI / WAV")
    ap.add_argument('input', nargs='?', help='файл .vgm / .vgz')
    ap.add_argument('-o', '--output', help='базовое имя выходных файлов')
    ap.add_argument('--xm', action='store_true', help='записать XM (по умолчанию)')
    ap.add_argument('--midi', action='store_true', help='XM + MIDI')
    ap.add_argument('--midi-only', action='store_true', help='только MIDI')
    ap.add_argument('--wav', action='store_true', help='+ WAV (рендер libvgm, 44.1 кГц стерео)')
    ap.add_argument('--wav-only', action='store_true', help='только WAV')
    ap.add_argument('--wav-file', help='имя WAV файла')
    ap.add_argument('--all', action='store_true', help='XM + MIDI + WAV')
    ap.add_argument('--loops', type=int, default=1, help='сколько раз проиграть петлю (1 = как в файле)')
    ap.add_argument('--octave', type=int, default=0, help='сдвиг имён нот в октавах (звук не меняется)')
    ap.add_argument('--transpose', type=int, default=0, help='сдвиг имён нот в полутонах (звук не меняется)')
    ap.add_argument('--compact', type=int, default=1, help='сделать строки в N раз крупнее (меньше строк)')
    ap.add_argument('--bpm', type=int, help='задать BPM вручную')
    ap.add_argument('--speed', type=int, help='задать speed вручную')
    ap.add_argument('--channels', type=int, default=MAX_XM_CHANNELS, help='максимум каналов XM (<=32)')
    ap.add_argument('--zone-width', type=int, default=12,
                    help='ширина зоны одного сэмпла FM в полутонах (меньше - точнее тембр, больше файл)')
    ap.add_argument('--bend-threshold', type=float, default=0.6,
                    help='порог (полутона) для глиссандо 3FF внутри ноты')
    ap.add_argument('--no-frame-grid', action='store_true', help='не привязывать строки к кадрам 60/50 Гц, искать темп по нотам')
    ap.add_argument('--norm-scale', type=float, default=0.7,
                    help='громкость XM: меньше - громче (0.7 примерно совпадает с уровнем эмулятора)')
    ap.add_argument('--debug', action='store_true', help='подробности про сэмплы')
    ap.add_argument('--no-pan', action='store_true', help='не писать панораму (8xx)')
    ap.add_argument('--renderer', choices=('auto', 'native', 'python'), default='auto',
                    help='эмулятор чипов: auto - C++ vgm2wav, если собран, иначе встроенный Python; '
                         'native - всегда vgm2wav (соберёт при необходимости); python - всегда встроенный')
    ap.add_argument('--build-renderer', action='store_true', help='собрать vgm_render/vgm2wav (C++ libvgm, быстрее) и выйти')
    args = ap.parse_args()
    args.channels = max(2, min(MAX_XM_CHANNELS, args.channels))
    args.compact = max(1, args.compact)
    args.loops = max(1, args.loops)
    if args.build_renderer:
        build_renderer()
        return
    if not args.input or not os.path.isfile(args.input):
        ap.error(f"нужен файл .vgm / .vgz: {args.input or ''}")
    convert(args)


if __name__ == '__main__':
    main()
