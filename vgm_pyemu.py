#!/usr/bin/env python3
"""
vgm_pyemu - эмуляторы YM2612 (OPN2) и SN76489 (PSG) на чистом Python + numpy.

Зачем: vgm2xm-midi-wav.py рендерит звук через vgm2wav (libvgm, C++). Этот модуль
делает то же самое без компилятора, cmake и git - нужен только numpy.

Модель повторяет ядра libvgm по умолчанию:
  * YM2612 - MAME/GPGX (fmopn.c): те же таблицы синуса и экспоненты, огибающая
    с теми же шагами и счётчиком, LFO, детюн, KSR, feedback, задержка MEM между
    операторами, 3-slot режим, SSG-EG, DAC (канал 6), обрезка канала по 14 бит;
  * SN76489 - MAME (sn76496.c): 3 тона + шум (LFSR с отводами из заголовка VGM),
    ограничение высоких частот, объём 2 дБ на ступень.

Отличия от C-версии (по скорости):
  * между записями в регистры параметры постоянны, поэтому участок считается
    векторно (numpy), а не по одному отсчёту; по отсчётам считается только
    канал с feedback у первого оператора и SSG-EG;
  * тон PSG интегрируется аналитически на интервале выходного отсчёта
    (усреднение, без алиасинга), в libvgm там линейная передискретизация;
  * из чипа (clk/144 для YM2612) в 44100 Гц - линейная интерполяция.

Использование из кода:
    import vgm_pyemu
    wav = vgm_pyemu.render(events, end_t, ym_clk, sn_clk, sn_flags)   # (N, 2) float64, шкала int16, обрезано по 16 бит
events - список (t, kind, a, b) из load_vgm: 'Y' (a = порт<<8 | регистр, b = значение),
'S' (a = байт PSG), t - в отсчётах 44100 Гц.
"""
import math

import numpy as np

# ═══════════════════════════════════════════════════════════════
# Таблицы YM2612 (как в fmopn.c)
# ═══════════════════════════════════════════════════════════════

ENV_BITS = 10
ENV_STEP = 128.0 / (1 << ENV_BITS)
MAX_ATT = (1 << ENV_BITS) - 1
EG_OFF, EG_REL, EG_SUS, EG_DEC, EG_ATT = 0, 1, 2, 3, 4
SIN_LEN = 1 << 10
TL_RES = 256
TL_TAB_LEN = 13 * 2 * TL_RES
ENV_QUIET = TL_TAB_LEN >> 3
FREQ_SH = 16
FN_MAX = 0x20000 << (FREQ_SH - 10)
PH_MASK = (1 << 26) - 1

SL_TABLE = [int(db * (4.0 / ENV_STEP)) for db in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 31)]

_O = lambda a: a * 8
EG_INC = [
    0, 1, 0, 1, 0, 1, 0, 1,
    0, 1, 0, 1, 1, 1, 0, 1,
    0, 1, 1, 1, 0, 1, 1, 1,
    0, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 2, 1, 1, 1, 2,
    1, 2, 1, 2, 1, 2, 1, 2,
    1, 2, 2, 2, 1, 2, 2, 2,
    2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 4, 2, 2, 2, 4,
    2, 4, 2, 4, 2, 4, 2, 4,
    2, 4, 4, 4, 2, 4, 4, 4,
    4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 8, 4, 4, 4, 8,
    4, 8, 4, 8, 4, 8, 4, 8,
    4, 8, 8, 8, 4, 8, 8, 8,
    8, 8, 8, 8, 8, 8, 8, 8,
    16, 16, 16, 16, 16, 16, 16, 16,
    0, 0, 0, 0, 0, 0, 0, 0,
]


def _rate_tables():
    sel = [_O(18)] * 32
    sh = [11] * 32
    sel += [_O(18), _O(18), _O(2), _O(3)]           # rate 0,1: бесконечные (по тестам Nemesis)
    sh += [11] * 4
    for r in range(1, 12):
        sel += [_O(0), _O(1), _O(2), _O(3)]
        sh += [11 - r] * 4
    for base in (4, 8, 12):                         # rates 12..14
        sel += [_O(base), _O(base + 1), _O(base + 2), _O(base + 3)]
        sh += [0] * 4
    sel += [_O(16)] * 4                             # rate 15
    sh += [0] * 4
    sel += [_O(16)] * 32
    sh += [0] * 32
    return sel, sh


RATE_SEL, RATE_SH = _rate_tables()
EG_INC_NP = np.array(EG_INC, dtype=np.int64)

DT_TAB_INT = [
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 7, 8, 8, 8, 8,
    1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 7, 8, 8, 9, 10, 11, 12, 13, 14, 16, 16, 16, 16,
    2, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6, 7, 8, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 20, 22, 22, 22, 22,
]
DT_TAB = [[0] * 32 for _ in range(8)]
for _d in range(4):
    for _i in range(32):
        DT_TAB[_d][_i] = DT_TAB_INT[_d * 32 + _i] << (FREQ_SH - 10)
        DT_TAB[_d + 4][_i] = -DT_TAB[_d][_i]

FKTABLE = (0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 3, 3, 3, 3, 3, 3)
LFO_SAMPLES = (108, 77, 71, 67, 62, 44, 8, 5)
LFO_AMS_SHIFT = (8, 3, 1, 0)

LFO_PM_OUTPUT = (
    # fnum bit 0
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 1, 1, 1, 1),
    # fnum bit 1
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 1, 1, 1, 1),
    (0, 0, 1, 1, 2, 2, 2, 3),
    # fnum bit 2
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 1),
    (0, 0, 0, 0, 1, 1, 1, 1),
    (0, 0, 1, 1, 2, 2, 2, 3),
    (0, 0, 2, 3, 4, 4, 5, 6),
    # fnum bit 3
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 1, 1),
    (0, 0, 0, 0, 1, 1, 1, 1),
    (0, 0, 0, 1, 1, 1, 1, 2),
    (0, 0, 1, 1, 2, 2, 2, 3),
    (0, 0, 2, 3, 4, 4, 5, 6),
    (0, 0, 4, 6, 8, 8, 10, 12),
    # fnum bit 4
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 1, 1, 1, 1),
    (0, 0, 0, 1, 1, 1, 2, 2),
    (0, 0, 1, 1, 2, 2, 3, 3),
    (0, 0, 1, 2, 2, 2, 3, 4),
    (0, 0, 2, 3, 4, 4, 5, 6),
    (0, 0, 4, 6, 8, 8, 10, 12),
    (0, 0, 8, 12, 16, 16, 20, 24),
    # fnum bit 5
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 2, 2, 2, 2),
    (0, 0, 0, 2, 2, 2, 4, 4),
    (0, 0, 2, 2, 4, 4, 6, 6),
    (0, 0, 2, 4, 4, 4, 6, 8),
    (0, 0, 4, 6, 8, 8, 10, 12),
    (0, 0, 8, 12, 16, 16, 20, 24),
    (0, 0, 16, 24, 32, 32, 40, 48),
    # fnum bit 6
    (0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 4, 4, 4, 4),
    (0, 0, 0, 4, 4, 4, 8, 8),
    (0, 0, 4, 4, 8, 8, 12, 12),
    (0, 0, 4, 8, 8, 8, 12, 16),
    (0, 0, 8, 12, 16, 16, 20, 24),
    (0, 0, 16, 24, 32, 32, 40, 48),
    (0, 0, 32, 48, 64, 64, 80, 96),
)


def _build_tables():
    tl = [0] * TL_TAB_LEN
    for x in range(TL_RES):
        m = (1 << 16) / math.pow(2, (x + 1) * (ENV_STEP / 4.0) / 8.0)
        m = math.floor(m)
        n = int(m) >> 4
        n = (n >> 1) + 1 if n & 1 else n >> 1
        n <<= 2
        tl[x * 2] = n
        tl[x * 2 + 1] = -n
        for i in range(1, 13):
            tl[x * 2 + i * 2 * TL_RES] = tl[x * 2] >> i
            tl[x * 2 + 1 + i * 2 * TL_RES] = -tl[x * 2 + i * 2 * TL_RES]
    sn = [0] * SIN_LEN
    for i in range(SIN_LEN):
        m = math.sin(((i * 2) + 1) * math.pi / SIN_LEN)
        o = 8 * math.log(1.0 / m) / math.log(2.0) if m > 0.0 else 8 * math.log(-1.0 / m) / math.log(2.0)
        o = o / (ENV_STEP / 4)
        n = int(2.0 * o)
        n = (n >> 1) + 1 if n & 1 else n >> 1
        sn[i] = n * 2 + (0 if m >= 0.0 else 1)
    pm = np.zeros(128 * 8 * 32, dtype=np.int64)
    for depth in range(8):
        for fnum in range(128):
            for step in range(8):
                value = 0
                for bit in range(7):
                    if fnum & (1 << bit):
                        value += LFO_PM_OUTPUT[bit * 8 + depth][step]
                base = fnum * 32 * 8 + depth * 32
                pm[base + step] = value
                pm[base + (step ^ 7) + 8] = value
                pm[base + step + 16] = -value
                pm[base + (step ^ 7) + 24] = -value
    return tl, sn, pm


TL_LIST, SIN_LIST, LFO_PM_TABLE = _build_tables()
TLX_LIST = TL_LIST + [0] * (24000 - TL_TAB_LEN)         # за пределами таблицы - тишина
TLX = np.array(TLX_LIST, dtype=np.int64)
SIN_NP = np.array(SIN_LIST, dtype=np.int64)


# ═══════════════════════════════════════════════════════════════
# YM2612
# ═══════════════════════════════════════════════════════════════

class _Slot:
    __slots__ = ('dt', 'mul', 'tl', 'KSR', 'ar', 'd1r', 'd2r', 'rr', 'sl', 'amask', 'ssg', 'ssgn',
                 'state', 'volume', 'key', 'phase', 'ksr', 'inc', 'r_ar', 'r_d1', 'r_d2', 'r_rr',
                 'bf', 'kc')

    def __init__(self):
        self.dt = 0
        self.mul = 1
        self.tl = 127 << 3
        self.KSR = 3
        self.ar = self.d1r = self.d2r = 0
        self.rr = 34
        self.sl = 0
        self.amask = 0
        self.ssg = 0
        self.ssgn = 0
        self.state = EG_OFF
        self.volume = MAX_ATT
        self.key = 0
        self.phase = 0
        self.ksr = 0
        self.inc = 0
        self.bf = 0
        self.kc = 0
        self.r_ar = self.r_d1 = self.r_d2 = self.r_rr = (0, _O(18))

    def calc_rates(self):
        k = self.ksr
        if self.ar + k < 94:
            self.r_ar = (RATE_SH[self.ar + k], RATE_SEL[self.ar + k])
        else:
            self.r_ar = (0, _O(18))
        self.r_d1 = (RATE_SH[self.d1r + k], RATE_SEL[self.d1r + k])
        self.r_d2 = (RATE_SH[self.d2r + k], RATE_SEL[self.d2r + k])
        self.r_rr = (RATE_SH[self.rr + k], RATE_SEL[self.rr + k])


class _Chan:
    def __init__(self):
        self.slots = [_Slot() for _ in range(4)]
        self.algo = 0
        self.FB = 10
        self.pms = 0
        self.ams = 8
        self.panL = 1
        self.panR = 1
        self.bf = 0                 # block_fnum
        self.fc = 0
        self.kcode = 0
        self.mem = 0
        self.op1 = [0, 0]
        self.dirty = True


def _eg_scalar_tick(sl, cnt):
    """Один шаг огибающей (advance_eg_channel) для одного слота - для SSG-EG."""
    st = sl.state
    if st == EG_ATT:
        sh, sel = sl.r_ar
        if not (cnt & ((1 << sh) - 1)):
            sl.volume += (~sl.volume * EG_INC[sel + ((cnt >> sh) & 7)]) >> 4
            if sl.volume <= 0:
                sl.volume = 0
                sl.state = EG_SUS if sl.sl == 0 else EG_DEC
    elif st == EG_DEC:
        sh, sel = sl.r_d1
        if not (cnt & ((1 << sh) - 1)):
            if sl.ssg & 8:
                if sl.volume < 0x200:
                    sl.volume += 4 * EG_INC[sel + ((cnt >> sh) & 7)]
            else:
                sl.volume += EG_INC[sel + ((cnt >> sh) & 7)]
            if sl.volume >= sl.sl:
                sl.state = EG_SUS
    elif st == EG_SUS:
        sh, sel = sl.r_d2
        if not (cnt & ((1 << sh) - 1)):
            if sl.ssg & 8:
                if sl.volume < 0x200:
                    sl.volume += 4 * EG_INC[sel + ((cnt >> sh) & 7)]
            else:
                sl.volume += EG_INC[sel + ((cnt >> sh) & 7)]
                if sl.volume >= MAX_ATT:
                    sl.volume = MAX_ATT
    elif st == EG_REL:
        sh, sel = sl.r_rr
        if not (cnt & ((1 << sh) - 1)):
            if sl.ssg & 8:
                if sl.volume < 0x200:
                    sl.volume += 4 * EG_INC[sel + ((cnt >> sh) & 7)]
                if sl.volume >= 0x200:
                    sl.volume = MAX_ATT
                    sl.state = EG_OFF
            else:
                sl.volume += EG_INC[sel + ((cnt >> sh) & 7)]
                if sl.volume >= MAX_ATT:
                    sl.volume = MAX_ATT
                    sl.state = EG_OFF


def _ssg_update(sl):
    """update_ssg_eg_channel для одного слота; True - фаза сброшена."""
    reset = False
    if (sl.ssg & 8) and sl.volume >= 0x200 and sl.state > EG_REL:
        if sl.ssg & 1:
            if sl.ssg & 2:
                sl.ssgn = 4
            if sl.state != EG_ATT and not (sl.ssgn ^ (sl.ssg & 4)):
                sl.volume = MAX_ATT
        else:
            if sl.ssg & 2:
                sl.ssgn ^= 4
            else:
                sl.phase = 0
                reset = True
            if sl.state != EG_ATT:
                if sl.ar + sl.ksr < 94:
                    sl.state = (EG_SUS if sl.sl == 0 else EG_DEC) if sl.volume <= 0 else EG_ATT
                else:
                    sl.volume = 0
                    sl.state = EG_SUS if sl.sl == 0 else EG_DEC
    return reset


def _eg_run(sl, c0, m):
    """Огибающая слота на m шагах (счётчик c0..c0+m-1) без SSG-EG. Возвращает массив громкости после каждого шага."""
    out = np.empty(m, dtype=np.int64)
    pos = 0
    vol = sl.volume
    while pos < m:
        st = sl.state
        if st == EG_OFF:
            out[pos:] = vol
            break
        if st == EG_ATT:
            sh, sel = sl.r_ar
        elif st == EG_DEC:
            sh, sel = sl.r_d1
        elif st == EG_SUS:
            sh, sel = sl.r_d2
        else:
            sh, sel = sl.r_rr
        if sh:                                   # медленная скорость: изменение может вообще не попасть в участок
            msk = (1 << sh) - 1
            if ((c0 + pos + msk) & ~msk) >= c0 + m:
                out[pos:] = vol
                break
        cnt = np.arange(c0 + pos, c0 + m, dtype=np.int64)
        mask = (cnt & ((1 << sh) - 1)) == 0
        inc = np.where(mask, EG_INC_NP[sel + ((cnt >> sh) & 7)], 0)
        if st == EG_ATT:
            nz = np.nonzero(inc)[0]
            last = 0
            done = None
            for k in nz:
                k = int(k)
                out[pos + last:pos + k] = vol
                vol += (~vol * int(inc[k])) >> 4
                last = k
                if vol <= 0:
                    vol = 0
                    sl.state = EG_SUS if sl.sl == 0 else EG_DEC
                    done = k
                    break
            if done is None:
                out[pos + last:] = vol
                break
            out[pos + done] = vol
            pos += done + 1
        elif st == EG_DEC:
            cs = vol + np.cumsum(inc)
            hit = mask & (cs >= sl.sl)
            if hit.any():
                k = int(hit.argmax())
                out[pos:pos + k + 1] = cs[:k + 1]
                vol = int(cs[k])
                sl.state = EG_SUS
                pos += k + 1
            else:
                out[pos:] = cs
                vol = int(cs[-1])
                break
        elif st == EG_SUS:
            cs = np.minimum(vol + np.cumsum(inc), MAX_ATT)
            out[pos:] = cs
            vol = int(cs[-1])
            break
        else:
            cs = vol + np.cumsum(inc)
            hit = mask & (cs >= MAX_ATT)
            if hit.any():
                k = int(hit.argmax())
                out[pos:pos + k] = cs[:k]
                out[pos + k:] = MAX_ATT
                vol = MAX_ATT
                sl.state = EG_OFF
            else:
                out[pos:] = cs
                vol = int(cs[-1])
            break
    sl.volume = vol
    return out


class YM2612:
    def __init__(self, clock):
        self.clock = clock
        self.ch = [_Chan() for _ in range(6)]
        self.sl3_bf = [0, 0, 0]
        self.sl3_fc = [0, 0, 0]
        self.sl3_kc = [0, 0, 0]
        self.fn_h = 0
        self.sl3_fn_h = 0
        self.mode = 0
        self.lfo_en = False
        self.lfo_n = 108
        self.lfo_timer = 0
        self.lfo_cnt = 0
        self.dacen = 0
        self.dac_t = np.zeros(0, dtype=np.int64)
        self.dac_v = np.zeros(0, dtype=np.int64)

    # ── запись регистров ──
    def write(self, addr, v):
        port = addr >> 8
        r = addr & 0xFF
        for ci in range(6):                 # как update_req в libvgm: пересчёт перед каждой записью
            ch = self.ch[ci]
            if ch.dirty:
                self._refresh(ci)
            for sl in ch.slots:             # ...и внеочередной шаг SSG-EG (обновление нулевой длины)
                if sl.ssg & 8:
                    _ssg_update(sl)
        if r < 0x30:
            if not port:
                self._write_mode(r, v)
            return
        c = r & 3
        if c == 3:
            return
        ci = c + 3 * port
        ch = self.ch[ci]
        sl = ch.slots[(r >> 2) & 3]
        top = r & 0xF0
        if top == 0x30:
            sl.mul = (v & 0x0F) * 2 if (v & 0x0F) else 1
            sl.dt = (v >> 4) & 7
            ch.dirty = True
        elif top == 0x40:
            sl.tl = (v & 0x7F) << 3
        elif top == 0x50:
            sl.ar = 32 + ((v & 0x1F) << 1) if (v & 0x1F) else 0
            sl.KSR = 3 - (v >> 6)
            ch.dirty = True
        elif top == 0x60:
            sl.d1r = 32 + ((v & 0x1F) << 1) if (v & 0x1F) else 0
            sl.amask = 1 if (v & 0x80) else 0
            ch.dirty = True
        elif top == 0x70:
            sl.d2r = 32 + ((v & 0x1F) << 1) if (v & 0x1F) else 0
            ch.dirty = True
        elif top == 0x80:
            sl.sl = SL_TABLE[v >> 4]
            if sl.state == EG_DEC and sl.volume >= sl.sl:
                sl.state = EG_SUS
            sl.rr = 34 + ((v & 0x0F) << 2)
            ch.dirty = True
        elif top == 0x90:
            sl.ssg = v & 0x0F
        elif top == 0xA0:
            s = (r >> 2) & 3
            if s == 0:
                fn = ((self.fn_h & 7) << 8) + v
                blk = self.fn_h >> 3
                ch.kcode = (blk << 2) | FKTABLE[fn >> 7]
                ch.fc = (fn * 2 * 2048) >> (7 - blk)
                ch.bf = (blk << 11) | fn
                ch.dirty = True
            elif s == 1:
                self.fn_h = v & 0x3F
            elif s == 2 and port == 0:
                fn = ((self.sl3_fn_h & 7) << 8) + v
                blk = self.sl3_fn_h >> 3
                self.sl3_kc[c] = (blk << 2) | FKTABLE[fn >> 7]
                self.sl3_fc[c] = (fn * 2 * 2048) >> (7 - blk)
                self.sl3_bf[c] = (blk << 11) | fn
                self.ch[2].dirty = True
            elif s == 3 and port == 0:
                self.sl3_fn_h = v & 0x3F
        elif top == 0xB0:
            s = (r >> 2) & 3
            if s == 0:
                ch.algo = v & 7
                ch.FB = 10 - ((v >> 3) & 7)
            elif s == 1:
                ch.pms = (v & 7) * 32
                ch.ams = LFO_AMS_SHIFT[(v >> 4) & 3]
                ch.panL = 1 if (v & 0x80) else 0
                ch.panR = 1 if (v & 0x40) else 0

    def _write_mode(self, r, v):
        if r == 0x22:
            if v & 8:
                self.lfo_en = True
                self.lfo_n = LFO_SAMPLES[v & 7]
            else:
                self.lfo_en = False
                self.lfo_timer = 0
                self.lfo_cnt = 0
        elif r == 0x27:
            if (self.mode ^ v) & 0xC0:
                self.ch[2].dirty = True
            self.mode = v
        elif r == 0x28:
            c = v & 3
            if c == 3:
                return
            if v & 4:
                c += 3
            ch = self.ch[c]
            for bit, idx in ((0x10, 0), (0x20, 2), (0x40, 1), (0x80, 3)):
                if v & bit:
                    self._keyon(ch.slots[idx])
                else:
                    self._keyoff(ch.slots[idx])
        elif r == 0x2B:
            self.dacen = v & 0x80

    @staticmethod
    def _keyon(sl):
        if not sl.key:
            sl.phase = 0
            sl.ssgn = 0
            if sl.ar + sl.ksr < 94:
                sl.state = (EG_SUS if sl.sl == 0 else EG_DEC) if sl.volume <= 0 else EG_ATT
            else:
                sl.volume = 0
                sl.state = EG_SUS if sl.sl == 0 else EG_DEC
        sl.key = 1

    @staticmethod
    def _keyoff(sl):
        if sl.key:
            if sl.state > EG_REL:
                sl.state = EG_REL
                if sl.ssg & 8:
                    if sl.ssgn ^ (sl.ssg & 4):
                        sl.volume = (0x200 - sl.volume) & MAX_ATT
                    if sl.volume >= 0x200:
                        sl.volume = MAX_ATT
                        sl.state = EG_OFF
        sl.key = 0

    # ── пересчёт приращений фазы и скоростей огибающей ──
    def _refresh(self, ci):
        ch = self.ch[ci]
        S = ch.slots
        if ci == 2 and (self.mode & 0xC0):
            src = ((self.sl3_fc[1], self.sl3_kc[1], self.sl3_bf[1]),
                   (self.sl3_fc[0], self.sl3_kc[0], self.sl3_bf[0]),
                   (self.sl3_fc[2], self.sl3_kc[2], self.sl3_bf[2]),
                   (ch.fc, ch.kcode, ch.bf))
        else:
            one = (ch.fc, ch.kcode, ch.bf)
            src = (one, one, one, one)
        for sl, (fc, kc, bf) in zip(S, src):
            fc += DT_TAB[sl.dt][kc]
            if fc < 0:
                fc += FN_MAX
            sl.inc = (fc * sl.mul) >> 1
            sl.bf = bf
            sl.kc = kc
            sl.ksr = kc >> sl.KSR
            sl.calc_rates()
        ch.dirty = False

    # ── рендер ──
    def _lfo(self, n):
        """AM и шаг PM для каждого из n отсчётов, затем продвижение LFO."""
        if not self.lfo_en:
            return np.full(n, 126, dtype=np.int64), np.zeros(n, dtype=np.int64)
        N = self.lfo_n
        i = np.arange(n, dtype=np.int64)
        cnt = (self.lfo_cnt + (self.lfo_timer + i) // N) & 127
        am = np.where(cnt < 64, (cnt ^ 63) << 1, (cnt & 63) << 1)
        pm = cnt >> 2
        tot = self.lfo_timer + n
        self.lfo_cnt = (self.lfo_cnt + tot // N) & 127
        self.lfo_timer = tot % N
        return am, pm

    def _advance_lfo_only(self, n):
        if self.lfo_en:
            tot = self.lfo_timer + n
            self.lfo_cnt = (self.lfo_cnt + tot // self.lfo_n) & 127
            self.lfo_timer = tot % self.lfo_n

    def _phase(self, sl, ch, n, pm_step):
        ph0 = sl.phase
        if ch.pms and pm_step is not None:
            off = LFO_PM_TABLE[((sl.bf & 0x7F0) << 4) + ch.pms + pm_step]
            if off.any():
                blk = sl.bf >> 11
                fn = ((sl.bf << 1) + off) & 0xFFF
                finc = ((fn * 2048) >> (7 - blk)) + DT_TAB[sl.dt][sl.kc]
                finc = np.where(finc < 0, finc + FN_MAX, finc)
                inc = np.where(off != 0, (finc * sl.mul) >> 1, sl.inc)
                cs = np.cumsum(inc)
                sl.phase = (ph0 + int(cs[-1])) & PH_MASK
                return ph0 + cs - inc
        ph = ph0 + sl.inc * np.arange(n, dtype=np.int64)
        sl.phase = (ph0 + sl.inc * n) & PH_MASK
        return ph

    def _skip_phase(self, ch, n, done=()):
        for sl in ch.slots:
            if id(sl) not in done:
                sl.phase = (sl.phase + sl.inc * n) & PH_MASK

    @staticmethod
    def _op(idx, env, pm):
        if pm is not None:
            idx = idx + (pm >> 1)
        return TLX[(env << 3) + SIN_NP[idx & 1023]]

    @staticmethod
    def _op1_fb(idx, env, fb, y0, y1):
        out = [0] * len(idx)
        il = idx.tolist()
        e8 = (env << 3).tolist()
        sn = SIN_LIST
        tl = TLX_LIST
        for i in range(len(il)):
            o = tl[e8[i] + sn[(il[i] + ((y0 + y1) >> fb)) & 1023]]
            out[i] = o
            y0 = y1
            y1 = o
        return np.array(out, dtype=np.int64), y0, y1

    def _slot_env(self, sl, S0, n, m, j0, sidx, ssg_info):
        """Массив vol_out (без TL и AM) по отсчётам."""
        if sl.state == EG_OFF and not (sl.ssg & 8):
            return np.full(n, sl.volume, dtype=np.int64)
        if sl.ssg & 8:
            vo = np.empty(n, dtype=np.int64)
            resets = []
            ph_keep = sl.phase
            for i in range(n):
                if _ssg_update(sl):
                    resets.append(i)
                v = sl.volume
                vo[i] = ((0x200 - v) & MAX_ATT) if (sl.state > EG_REL and (sl.ssgn ^ (sl.ssg & 4))) else v
                if (S0 + i) % 3 == 2:
                    _eg_scalar_tick(sl, (S0 + i) // 3 + 1)
            sl.phase = ph_keep
            ssg_info[id(sl)] = resets
            return vo
        if sl.state == EG_OFF and m == 0:
            return np.full(n, sl.volume, dtype=np.int64)
        prev = sl.volume
        ticks = _eg_run(sl, j0 + 1, m)
        tv = np.concatenate((np.array([prev], dtype=np.int64), ticks))
        return tv[sidx]

    def render_chunk(self, S0, n):
        """n отсчётов чипа начиная с S0 -> (L, R) int64."""
        L = np.zeros(n, dtype=np.int64)
        R = np.zeros(n, dtype=np.int64)
        for ci in range(6):
            if self.ch[ci].dirty:
                self._refresh(ci)
        j0 = S0 // 3
        j1 = (S0 + n - 3) // 3
        m = max(0, j1 - j0 + 1)
        sidx = (np.arange(S0, S0 + n, dtype=np.int64) // 3) - j0
        any_active = self.dacen != 0
        for ch in self.ch:
            for sl in ch.slots:
                if sl.state != EG_OFF or sl.volume < MAX_ATT:
                    any_active = True
        if not any_active:
            self._advance_lfo_only(n)
            for ch in self.ch:
                self._skip_phase(ch, n)
            return L, R
        am, pm_step = self._lfo(n)
        dac = None
        if self.dacen and len(self.dac_t):
            k = np.searchsorted(self.dac_t, np.arange(S0, S0 + n, dtype=np.int64), side='right') - 1
            dac = np.where(k >= 0, self.dac_v[np.maximum(k, 0)], 0)
        ssg_info = {}
        for ci in range(6):
            ch = self.ch[ci]
            vo = [self._slot_env(sl, S0, n, m, j0, sidx, ssg_info) for sl in ch.slots]
            if ci == 5 and self.dacen:
                out = dac if dac is not None else np.zeros(n, dtype=np.int64)
                out = np.clip(out, -8192, 8191)
                if ch.panL:
                    L += out
                if ch.panR:
                    R += out
                continue
            alg = ch.algo
            carriers = {0: (3,), 1: (3,), 2: (3,), 3: (3,), 4: (2, 3), 5: (2, 1, 3), 6: (2, 1, 3), 7: (0, 2, 1, 3)}[alg]
            audible = False
            for k in carriers:
                if int(vo[k].min()) + ch.slots[k].tl < ENV_QUIET:
                    audible = True
                    break
            if not audible:
                done = set()
                for sl in ch.slots:              # SSG-EG: сброс фазы мог случиться на тихом участке
                    rs = ssg_info.get(id(sl))
                    if rs:
                        sl.phase = (sl.inc * (n - rs[-1])) & PH_MASK
                        done.add(id(sl))
                # каналу не слышно, но оператор 1 с feedback может ещё звучать: его история
                # (два последних выхода) влияет на следующую ноту, поэтому досчитываем её
                s0 = ch.slots[0]
                if ch.FB < 10 and int(vo[0].min()) + s0.tl < ENV_QUIET:
                    e0 = vo[0] + s0.tl
                    if s0.amask:
                        e0 = e0 + (am >> ch.ams)
                    keep = s0.phase
                    ph0 = self._phase(s0, ch, n, pm_step)
                    if id(s0) in done:
                        s0.phase = keep
                    _, y0, y1 = self._op1_fb(ph0 >> FREQ_SH, e0, ch.FB, ch.op1[0], ch.op1[1])
                    ch.op1 = [y0, y1]
                    done.add(id(s0))
                    if ssg_info.get(id(s0)):
                        s0.phase = (s0.inc * (n - ssg_info[id(s0)][-1])) & PH_MASK
                else:
                    ch.op1 = [0, 0]
                self._skip_phase(ch, n, done)
                ch.mem = 0
                continue
            amc = am >> ch.ams
            env = []
            for k in range(4):
                sl = ch.slots[k]
                e = vo[k] + sl.tl
                if sl.amask:
                    e = e + amc
                env.append(e)
            idx = []
            for k in range(4):
                sl = ch.slots[k]
                ph = self._phase(sl, ch, n, pm_step)
                resets = ssg_info.get(id(sl))
                if resets:                       # SSG-EG: сброс фазы внутри участка
                    ph = ph.copy()
                    for rpos in resets:
                        ph[rpos:] = ph[rpos:] - ph[rpos]
                    sl.phase = int(ph[-1] + sl.inc) & PH_MASK
                idx.append(ph >> FREQ_SH)
            # M1 = 0, M2 = 1, C1 = 2, C2 = 3
            if ch.FB < 10:
                o_m1, y0, y1 = self._op1_fb(idx[0], env[0], ch.FB, ch.op1[0], ch.op1[1])
                ch.op1 = [y0, y1]
            else:
                o_m1 = self._op(idx[0], env[0], None)
                ch.op1 = [int(o_m1[-2]) if n > 1 else ch.op1[1], int(o_m1[-1])]

            def delay(a):
                d = np.empty_like(a)
                d[0] = ch.mem
                d[1:] = a[:-1]
                ch.mem = int(a[-1])
                return d

            op = self._op
            if alg == 0:
                o_c1 = op(idx[2], env[2], o_m1)
                o_m2 = op(idx[1], env[1], delay(o_c1))
                out = op(idx[3], env[3], o_m2)
            elif alg == 1:
                o_c1 = op(idx[2], env[2], None)
                o_m2 = op(idx[1], env[1], delay(o_m1 + o_c1))
                out = op(idx[3], env[3], o_m2)
            elif alg == 2:
                o_c1 = op(idx[2], env[2], None)
                o_m2 = op(idx[1], env[1], delay(o_c1))
                out = op(idx[3], env[3], o_m1 + o_m2)
            elif alg == 3:
                o_c1 = op(idx[2], env[2], o_m1)
                o_m2 = op(idx[1], env[1], None)
                out = op(idx[3], env[3], delay(o_c1) + o_m2)
            elif alg == 4:
                o_c1 = op(idx[2], env[2], o_m1)
                o_m2 = op(idx[1], env[1], None)
                o_c2 = op(idx[3], env[3], o_m2)
                out = o_c1 + o_c2
            elif alg == 5:
                o_c1 = op(idx[2], env[2], o_m1)
                o_m2 = op(idx[1], env[1], delay(o_m1))
                o_c2 = op(idx[3], env[3], o_m1)
                out = o_c1 + o_m2 + o_c2
            elif alg == 6:
                o_c1 = op(idx[2], env[2], o_m1)
                o_m2 = op(idx[1], env[1], None)
                o_c2 = op(idx[3], env[3], None)
                out = o_c1 + o_m2 + o_c2
            else:
                out = o_m1 + op(idx[2], env[2], None) + op(idx[1], env[1], None) + op(idx[3], env[3], None)
            out = np.clip(out, -8192, 8191)
            if ch.panL:
                L += out
            if ch.panR:
                R += out
        return L, R

    def render(self, events, total):
        """events: [(sample, addr, val)] по возрастанию; total - число отсчётов чипа."""
        dt, dv = [], []
        ev = []
        for s, a, v in events:
            if a == 0x2A:
                dt.append(s)
                dv.append((v - 0x80) * 64)
            else:
                ev.append((s, a, v))
        self.dac_t = np.array(dt, dtype=np.int64)
        self.dac_v = np.array(dv, dtype=np.int64)
        L = np.zeros(total, dtype=np.int32)
        R = np.zeros(total, dtype=np.int32)
        pos = 0
        CH = 1 << 16

        def run(upto):
            nonlocal pos
            while pos < upto:
                n = min(CH, upto - pos)
                l, r = self.render_chunk(pos, n)
                L[pos:pos + n] = l
                R[pos:pos + n] = r
                pos += n

        for s, a, v in ev:
            if s > total:
                break
            if s > pos:
                run(s)
            self.write(a, v)
        run(total)
        return L, R


# ═══════════════════════════════════════════════════════════════
# SN76489 (MAME sn76496.c)
# ═══════════════════════════════════════════════════════════════

class _NoiseSeq:
    """Последовательность выходных битов LFSR от состояния после сброса."""

    def __init__(self, taps, width, white):
        tap = [0, 0]
        k = 0
        for i in range(width):
            if taps & (1 << i):
                tap[k] = 1 << i
                k += 1
                if k >= 2:
                    break
        fbm = 1 << (width - 1)
        rng = fbm
        bits = [rng & 1]
        start = rng
        L = None
        for step in range(1, (1 << min(width, 17)) + 2):
            a = (rng & tap[0]) != 0
            b = (rng & tap[1]) != 0 and white
            if a != b:
                rng = (rng >> 1) | fbm
            else:
                rng >>= 1
            if rng == start:
                L = step
                break
            bits.append(rng & 1)
        if L is None:
            L = len(bits)
        self.L = L
        b = np.array(bits[:L], dtype=np.int64) * 2 - 1           # +-1
        self.B = b
        self.pref = np.concatenate(([0], np.cumsum(b)))
        self.tot = int(self.pref[-1])

    def cum(self, k):
        k = np.asarray(k, dtype=np.int64)
        q, r = np.divmod(k, self.L)
        return q * self.tot + self.pref[r]

    def val(self, m):
        return self.B[np.asarray(m, dtype=np.int64) % self.L]


def _square_integral(x, sign0, c, P):
    """Интеграл меандра: sign0 до момента c, затем знак меняется каждые P. x - массив моментов."""
    y = x - c
    q = np.floor(y / P)
    r = y - q * P
    qi = q.astype(np.int64)
    tail = np.where((qi & 1) == 0, -r, -P + r)
    g = np.where(y < 0, x, c + tail)
    return sign0 * g


class PSG:
    """SN76496 (MAME) с целочисленным счётом тактов clk/16, как в libvgm."""

    def __init__(self, clock, taps=9, width=16, out_rate=44100):
        self.clock = clock
        self.rate = clock // 16                              # частота чипа (тактов в секунду)
        self.fnum_limit = int(self.rate // out_rate)
        self.vol_table = [int(0x8000 / 4 * 2.0 ** (i / -3.0) + 0.5) for i in range(15)] + [0]
        self.reg = [0] * 8
        self.last = 3
        self.period = [0, 0, 0, 0]
        self.volume = [0, 0, 0, 0]
        self.cnt = [0, 0, 0, 0]
        self.o = [0, 0, 0]
        self.seq = {True: _NoiseSeq(taps, width, True), False: _NoiseSeq(taps, width, False)}
        self.m0 = 0
        self.out3 = -1
        self.white = False

    def write(self, data):
        reg = self.reg
        if data & 0x80:
            r = (data & 0x70) >> 4
            self.last = r
            reg[r] = (reg[r] & 0x3F0) | (data & 0x0F)
        else:
            r = self.last
        c = r >> 1
        if r in (0, 2, 4):
            if (data & 0x80) == 0:
                reg[r] = (reg[r] & 0x0F) | ((data & 0x3F) << 4)
            self.period[c] = reg[r]
            if r == 4 and (reg[6] & 3) == 3:
                self.period[3] = self.period[2] << 1
        elif r in (1, 3, 5, 7):
            self.volume[c] = self.vol_table[data & 0x0F]
            if (data & 0x80) == 0:
                reg[r] = (reg[r] & 0x3F0) | (data & 0x0F)
        else:
            if (data & 0x80) == 0:
                reg[r] = (reg[r] & 0x3F0) | (data & 0x0F)
            n = reg[6] & 3
            self.period[3] = (self.period[2] << 1) if n == 3 else (1 << (5 + n))
            self.m0 = 0
        self.white = bool(reg[6] & 4)

    def advance(self, K, xs):
        """K тактов чипа. xs - моменты (в тактах от начала участка), где нужен интеграл выхода.
        Возвращает (интеграл на xs, интеграл за весь участок); выход чипа - сумма каналов, >>1."""
        res = np.zeros(len(xs), dtype=np.float64)
        tot = 0.0
        xe = np.array([float(K)])
        for i in range(3):
            P = self.period[i]
            Pe = max(P, 1)
            vol = self.volume[i]
            cnt = self.cnt[i]
            c = max(cnt, 1)
            o = self.o[i]
            if vol:
                if P <= 1:
                    res += vol * xs
                    tot += vol * K
                elif P > self.fnum_limit:
                    s0 = 1.0 if o else -1.0
                    res += vol * _square_integral(xs, s0, float(c - 1), float(Pe))
                    tot += vol * float(_square_integral(xe, s0, float(c - 1), float(Pe))[0])
            if K < c:
                self.cnt[i] = c - K
            else:
                n = (K - c) // Pe + 1
                self.o[i] = o ^ (n & 1)
                self.cnt[i] = P - (K - (c + (n - 1) * Pe))
        # шум
        P = self.period[3]
        Pe = max(P, 1)
        cnt = self.cnt[3]
        c = max(cnt, 1)
        vol = self.volume[3]
        sq = self.seq[self.white]
        m0 = self.m0
        if vol:
            c0 = float(c - 1)
            for arr, dst in ((xs, 0), (xe, 1)):
                y = arr - c0
                q = np.floor(y / Pe)
                r = y - q * Pe
                qi = q.astype(np.int64)
                part = sq.cum(m0 + qi + 1) - sq.cum(m0 + 1)
                F = np.where(y < 0, self.out3 * arr, self.out3 * c0 + Pe * part + sq.val(m0 + qi + 1) * r)
                if dst == 0:
                    res += vol * F
                else:
                    tot += vol * float(F[0])
        if K < c:
            self.cnt[3] = c - K
        else:
            n = (K - c) // Pe + 1
            self.m0 = m0 + n
            self.out3 = int(sq.val(self.m0))
            self.cnt[3] = P - (K - (c + (n - 1) * Pe))
        return res * 0.5, tot * 0.5


def render_psg(events, nsamp, clock, taps=9, width=16, sr=44100):
    chip = PSG(clock, taps, width, sr)
    rate = chip.rate
    xb = np.arange(nsamp + 1, dtype=np.float64) * rate / sr
    G = np.zeros(nsamp + 1, dtype=np.float64)
    end_tick = -(-(nsamp + 1) * rate // sr) + 1
    pos = 0
    jb = 0
    base = 0.0
    for t, data in events + [(None, None)]:
        e = end_tick if t is None else -(-int(t) * rate // sr)
        if e > end_tick:
            e = end_tick
        if e > pos:
            K = e - pos
            jh = int(np.searchsorted(xb, e, side='left'))
            xs = xb[jb:jh] - pos
            loc, tot = chip.advance(K, xs)
            G[jb:jh] = base + loc
            base += tot
            jb = jh
            pos = e
        if data is not None:
            chip.write(data)
    return np.diff(G) * (sr / rate)


# ═══════════════════════════════════════════════════════════════
# Общий рендер
# ═══════════════════════════════════════════════════════════════



def chip_gains(ym_clk, sn_clk):
    """Громкость чипов как в libvgm: базовые 0x100 (YM2612) и 0x80 (SN76489), затем общая нормализация."""
    ov = (0x100 if ym_clk else 0) + (0x80 if sn_clk else 0)
    fac = 1
    while 0 < ov <= 0x180:
        fac *= 2
        ov *= 2
    return float(fac), fac * 0.5


def render(events, end_t, ym_clk, sn_clk, sn_flags=(9, 16), sr=44100):
    """events: [(t, kind, a, b)] -> ndarray (N, 2) float64 (шкала int16, обрезано по 16 бит)."""
    N = int(end_t)
    out = np.zeros((N, 2), dtype=np.float64)
    YM_GAIN, PSG_GAIN = chip_gains(ym_clk, sn_clk)
    if ym_clk:
        fs = ym_clk // 144                              # libvgm: целая частота чипа
        # libvgm делает запись после того, как чип уже сгенерировал ceil(t * fs / sr) отсчётов
        ev = [(-(-int(t) * fs // sr), a, b) for t, k, a, b in events if k == 'Y']
        total = -(-N * fs // sr) + 4
        chip = YM2612(ym_clk)
        L, R = chip.render(ev, total)
        # выходной отсчёт - среднее чипа на интервале [j, j+1) * fs / sr (как RSMODE_LINEAR вниз)
        for ci, a in enumerate((L, R)):
            cs = np.concatenate(([0], np.cumsum(a, dtype=np.int64)))
            blk = 1 << 22
            for j0 in range(0, N, blk):
                j1 = min(N, j0 + blk)
                x = np.arange(j0, j1 + 1, dtype=np.float64) * fs / sr
                k = np.floor(x).astype(np.int64)
                integ = cs[k] + a[k] * (x - k)
                out[j0:j1, ci] += np.diff(integ) * (sr / fs) * YM_GAIN
    if sn_clk:
        pev = [(int(t), a) for t, k, a, b in events if k == 'S']
        p = render_psg(pev, N, sn_clk, sn_flags[0], sn_flags[1]) * PSG_GAIN
        out[:, 0] += p
        out[:, 1] += p
    np.clip(out, -32768, 32767, out=out)         # как 16-битный WAV у vgm2wav
    return out
