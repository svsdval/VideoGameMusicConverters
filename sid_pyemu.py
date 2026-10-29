#!/usr/bin/env python3
"""
sid_pyemu - SID (MOS 6581/8580) + 6510 + PSID-плеер на чистом Python + numpy.

Замена нативному sid_tracer/sidtrace (6510 + reSID) для sid2xm-midi-wav-v2.py, когда нет
компилятора. Выдаёт те же файлы/данные:
    trace(...)      -> prefix.trace (W - записи в регистры SID, E - счётчики огибающих каждые 5 мс)
                       и (по желанию) моно-рендер .raw 44.1 кГц
    waves(...)      -> однопериодные сэмплы голоса (с фильтром), как "sidtrace waves"

Что точно, что приблизительно:
  * 6510 (все официальные и «стабильные» недокументированные опкоды), PSID/RSID-загрузчик,
    тот же порядок INIT/PLAY, что у sidtrace - точно;
  * генератор огибающей (rate counter, exponential counter, ADSR-баг) - порт reSID, точно;
  * генератор волн: аккумулятор, шум (LFSR 23 бита), пульс, треугольник, пила, комбинированные
    волны по таблицам reSID, ЦАП 6581/8580 - порт reSID;
  * фильтр: 8580 - тот же SVF, что в reSID (там он тоже упрощён); 6581 - SVF с частотой среза и
    добротностью, подогнанными по АЧХ reSID (в reSID у 6581 модель на операционных усилителях,
    которую здесь не воспроизводим): ошибка по АЧХ порядка 1-2 дБ;
  * рендер WAV (.raw): без синхронизации и кольцевой модуляции осцилляторов.
"""
import base64
import math
import struct
import sys
import zlib

import numpy as np

_WAVE_TABLES_B64 = (
    "eNrtXYt5qzoM5pykgUAgGeGOkBE6QkfoCIzgETJCRmAEj8AIHsEjXMsP/ODhJrSHtpb0uUIYI2wjMO4fOcvSpavgbemymeVWc6r1T7X/mWauOUP/R/9H//+19ad/fGaaOfCO79D/0f/R/5N9//9B/0f/R///ufUnu2WmATPB8N7nuWL0f/R/9H/8/kf/R/9H/8fvf/R/9H/0f/z+R/9H/0f/x+9/JCSkX0l/SeLj/4RpL9OevyRQzyn9INOBHz75/CEdZnRlP2/zJ8vnwnKe5V0eXE2u+lVIcXaS90o/kBwSyZkoQwVzsYXvf0HFD7dfPJhfDFIyPyZYf133rMxKXn6hfWXFt1sOElLVVrP9Y4+fK19lVVf5/Sn2WVmRqtc6qbKSCJ1JSSsutrjkjfv/tKrti9nyRZDv6kpK5qen/WfafrFoH2wOtgXXbb3onyfnek05W77O6q4O8utBikTqvh6OPRGhMylpzWUCXvH8gPM2s+1u833dSEhN28z2z1z5k5aSu8btTXtew72jE8FMStpwmYAD/zkF5zs7GpzlPEhI5+vZr0+Y/3bW1s+K3+Xf13MrE7Bfn0wfT8RRRPBNn8fody07mYDdfCpY/e1lAgadecwX2D8SzqbOos5ozk61RbhadRXqimDfXUuTf3N1p4amJ1UrqBaxLZSZo5s3s33WZYxU5YvrXL7xlrOnmR4enszqLlB3BBElmbxP7DX2rmZ62Tlf1/j1Mdel7WQj3Uhtv238+kTLF4FPNkPNlj12htRTQD0RiHg6sJpISfSTowet1k8UsFP719NBbu20yGlU39PoTejOQ7qlT8Mz0h4XtNeoPrH8WP1P3t0xPl+oZzP2n38DbjoGVaMAGA1QwVnJxF81WpBjjKIHrQpGFk5/dpBb6REJ7CuD9iqX+6eF0uVC+SLSvutaPxMjwFLbnPavYsUI8Ad8gYgvgOOSf/72+m9uf2NSswAwGwDzApnYormaLYD5kSzrcyJ1OeOw1zMPDnWQq2Yj9oszFjPUQuk8mBl5oHx0BiZW/4Oq52Bn/7nn//b9/0Je9GxgEvVFCvp/R3cZ/hcACQkJKTG6YBOk/fb/g22QdP//Tbv+7cYj326l/T5Snu3C0b6VEgWII/+0/T/xr/12Y/xTt9J+HynPgnyD9wLJX4AT7//Dxv2/0n4fKc+CfIN3A8kPfD3+bSVdV+LP3iLl34N8g7cD2eZtHH8XoVuk/D3I7zQ+T0iRuhC/Z0k9l/c0d3RRTuP5QB76vFf4PpN/yA4sH6RIwBnM2APOT+P9SM493tgBt8O/yf/0XTe2f1lZ/sP5rl5Y5hvXP9u4/u1xW/vdcbZ8MXV8yP1xOZ8dfZ2DfsyO/EhEAk6m/ye5Lb/Avm9jsX+6cvn+iPZ/uZBfZiUrh22DdzVSJD7Gvz54/34of+H6rlWkfDnSvfq8Vcv575WnA95W4mZbmVqLv33OfnWrRnVy8LdZda98vRskEalTusvB8dTidwui8bxKQupdfG+h8b1aUpGY1KnE+1KN9zWSfw7+dw3+egm/my3ef9mo/Fz/nUZ64eJJ29PK+3+l/W5l+f4U3D9BPgt0bqVIfIx//sf1v9bR8ov1e6uX89/roL9riWAE3LVIrcJfLzxfI/brW2i/dqSwcQ/0rja4bSIS8HJ5avHcBdF4biUhAat84uG7QVKRmNSph/c2kk/jv+P973MTed43o+OtbLLm2kTaP1L+LZL/HuitxZ2L1Cr8+Qr7t/l8aece4N27QRKROoNfn8XHU08nEs9OpIQErPtf72eDpCIxiXenHt7dSMML973Bv4e6kpDOl0j+f4F+tVKkq8LPL5R/DfQ3K0UCHvIbjR43UqR3qb96eHsjDfv2yXmQDVHI9ULj8RuDx1cSErDqJ5N/HySk+6BbvL6RhnU/nyEpZD31OAvw/EYaVvlshPL/GN6fn3EOHAkJCQkJKTXC//8jISEhISH9Qgrxnfp9r2IBYfwfJCQkJCQk/P5HQkJCQkJCwu//307br/+wHX2P9T+w//814fp/6P/o/9+l/7dZ/487jP2P/o/9n+z7H/0f/R/7/we//x9b/4/t+Pf6/T/6P/o/9j9+/2P/o//j+/9ff/8nHv8P/R/9/7f7/+L7P+X5v0L0P8T/SpRbybxwVjTz1zE7eeve+SvsTeeH66T5e74hXyH+xHlLvpzJSqYRjkUDyJASphabIEmi4r1PMf532vdAxtKL/2xXOdtDnNzV8Y/3kVzXnrKpLUv24w/vA1bH7P0SgxRMIH6w3Z9bJpqpI2F9PyYTw/l/5ytgQ9vPxj+dir/5BLPiy+K/+vHjlq4iPP6fMi83+v5Sq86WbRU9bqIURB6VCeKHlt5evZ/IRCB+qJRqm8q4oFSu9stM/M8tv0JPo/iK8RL++uxBvMdHmZ9G1/PkmZ7jtl5RupbxJ8N9cj8wxIGkwxZsUxkXEv5ymWT8x+ds26/78f4HvvvblV/wJJJLhy0iNSajIarYiDr+4TzbVeif/r5/ndAhHiFEJtTxByUTmT7OEFnQxBOc+/JX8QEfiwbIg6Nt1MFpNtEM7zLi4cfZtohqBWiPV906yy34EPu9OMnqLmD6zlCRM9XdovJp9B5bdwc/5AFT82/Tc3YffwLKp4B6ItgnhHpiqAi0ZkuyesI4fDP7t3gCPs6F99aAEcDz5ykm30WnJ96Bp+3ewWoUoEYEdoRgxgyZ3lYjioGdMUdXDaMOdyQyN4IZz0Pb8xUJjQCzD43cTf7ySHvVNww/siN5tg7+9S99scS/gooEvwC/AdmZAKZnB6ieLfBnEMgwsyDlMBPRDTnBDMVhYhbD8jBz0uZBiYNzjJ0VeXYGJl7/w6ux/Cnn+3H9/8JeaOKrACbt/zu+g1VCKa4EjP8BREJKitLDfwVPf3z6/ej777Kq7m3iv//b3v+37H8ovbX/b/38aze+/7uV+Js+Up7twtE+4D7oHzHml+j/1PE/+P5P3P83tt+tLH+P5LMA32XwXvyFisQ3x39t3v8bz3xdV9p/i+DXWJAv8W77bC+kSHw9/m1t/dfZ31/C8u4sLszp5loznKsZ6TaH1Pr4u0kLgabbT84Yi/Pc8iwLMHseXo/kDnZP6Qczi37LO4vfG+P75HE0wPVRje0zeL7ewfpN4fzUbD51MH/M//1/4vj/5N//Rdr1bzeu/3Wd/aKPrE/Pgv/SciOPkIB/ef9H2md1/xdP25d8faT8xPrKl8j/6F9Hv/qzkhe85OW66w/+6188iuNd+/vLS/EQftbqQl7LtmpX4l8vsfLVhK7xu6S6AX539vrgOFJ5qF/QB3wv4HR6rWcB3pfKxARb3C/zcL+GJ/xiwk9m1/f+6v776l/BnrbFP19PH8I7z65vf5nPn0bbOZJLXo3gfiR/hK6+RuoXQ2NfVmABL6IVvhx9WM/oEklJ6i5in9TTOtGo7r72cd90wH0bvLfFfTMP983X4b8/ia/NE7hzB39+aWbz3V+iT/KluQL6dvH8E/qncrcC9w4Y5T7YTx3UssK5G+S7wbxTjXjmcfz7R/g8o58D5PUMQvsayY/x2yL2HnD2Lu4eMPcu9t7i7zMHh2+3LVJ9Dp9/i+L0fb3zsPsKv69+xR//Jb/C9LvYfoPMZwscw/tnSEhISEhISIkRR9QvEhISEhLSb6O/EkfwN4wL5CICsJGQkJCQkJBSIpLBbwCwHZCQkiEPbUGLjfF/K2PXjMo/Ew3TlvsShNPm8RVj8Q9/NzdBnLTRCgD/dMWBT1o14LlVAaYRAf8D7arDYw=="
)

MOS6581, MOS8580 = 0, 1


def _load_wave_tables():
    raw = zlib.decompress(base64.b64decode(_WAVE_TABLES_B64))
    arr = np.frombuffer(raw, dtype='<u2').reshape(2, 4, 4096)
    tabs = np.zeros((2, 8, 4096), dtype=np.int64)
    idx = np.arange(4096)
    for m in (0, 1):
        tabs[m][0][:] = 0xFFF
        acc = idx << 12
        msb = (acc & 0x800000) != 0
        tabs[m][1][:] = ((acc ^ np.where(msb, -1, 0)) >> 11) & 0xFFE
        tabs[m][2][:] = acc >> 12
        tabs[m][4][:] = 0xFFF
        tabs[m][3] = arr[m][0]
        tabs[m][5] = arr[m][1]
        tabs[m][6] = arr[m][2]
        tabs[m][7] = arr[m][3]
    return tabs


WAVE_TABLES = _load_wave_tables()


def build_dac(bits, r2_div_r, term):
    """ЦАП из лестницы R-2R (как dac.h reSID)."""
    vbit = []
    for set_bit in range(bits):
        bit = 0
        Vn = 1.0
        R = 1.0
        R2 = r2_div_r * R
        Rn = R2 if term else math.inf
        while bit < set_bit:
            if Rn == math.inf:
                Rn = R + R2
            else:
                Rn = R + R2 * Rn / (R2 + Rn)
            bit += 1
        if Rn == math.inf:
            Rn = R2
        else:
            Rn = R2 * Rn / (R2 + Rn)
            Vn = Vn * Rn / R2
        bit += 1
        while bit < bits:
            Rn += R
            I = Vn / Rn
            Rn = R2 * Rn / (R2 + Rn)
            Vn = Rn * I
            bit += 1
        vbit.append(Vn)
    tab = np.zeros(1 << bits, dtype=np.int64)
    for i in range(1 << bits):
        vo = 0.0
        for j in range(bits):
            if (i >> j) & 1:
                vo += vbit[j]
        tab[i] = int(((1 << bits) - 1) * vo + 0.5)
    return tab


WAVE_DAC = (build_dac(12, 2.20, False), build_dac(12, 2.00, True))
ENV_DAC = (build_dac(8, 2.20, False), build_dac(8, 2.00, True))
WAVE_ZERO = (0x380, 0x800)

RATE_PERIOD = (9, 32, 63, 95, 149, 220, 267, 313, 392, 977, 1954, 3126, 3907, 11720, 19532, 31251)
SUSTAIN_LEVEL = tuple(i * 0x11 for i in range(16))

ATTACK, DECAY_SUSTAIN, RELEASE = 0, 1, 2


class Envelope:
    """Порт EnvelopeGenerator::clock(delta_t) из reSID."""
    __slots__ = ('rate_counter', 'rate_period', 'exp_counter', 'exp_period', 'counter', 'hold_zero',
                 'attack', 'decay', 'sustain', 'release', 'gate', 'state')

    def __init__(self):
        self.reset()

    def reset(self):
        self.counter = 0
        self.attack = self.decay = self.sustain = self.release = 0
        self.gate = 0
        self.rate_counter = 0
        self.exp_counter = 0
        self.exp_period = 1
        self.state = RELEASE
        self.rate_period = RATE_PERIOD[0]
        self.hold_zero = True

    def write_control(self, control):
        gate_next = control & 1
        if not self.gate and gate_next:
            self.state = ATTACK
            self.rate_period = RATE_PERIOD[self.attack]
            self.hold_zero = False
        elif self.gate and not gate_next:
            self.state = RELEASE
            self.rate_period = RATE_PERIOD[self.release]
        self.gate = gate_next

    def write_ad(self, v):
        self.attack = (v >> 4) & 15
        self.decay = v & 15
        if self.state == ATTACK:
            self.rate_period = RATE_PERIOD[self.attack]
        elif self.state == DECAY_SUSTAIN:
            self.rate_period = RATE_PERIOD[self.decay]

    def write_sr(self, v):
        self.sustain = (v >> 4) & 15
        self.release = v & 15
        if self.state == RELEASE:
            self.rate_period = RATE_PERIOD[self.release]

    def _set_exp(self):
        c = self.counter
        if c == 0xFF:
            self.exp_period = 1
        elif c == 0x5D:
            self.exp_period = 2
        elif c == 0x36:
            self.exp_period = 4
        elif c == 0x1A:
            self.exp_period = 8
        elif c == 0x0E:
            self.exp_period = 16
        elif c == 0x06:
            self.exp_period = 30
        elif c == 0x00:
            self.exp_period = 1
            self.hold_zero = True

    def clock(self, delta_t):
        rate_step = self.rate_period - self.rate_counter
        if rate_step <= 0:
            rate_step += 0x7FFF
        while delta_t:
            if delta_t < rate_step:
                self.rate_counter += delta_t
                if self.rate_counter & 0x8000:
                    self.rate_counter = (self.rate_counter + 1) & 0x7FFF
                return
            self.rate_counter = 0
            delta_t -= rate_step
            if self.state == ATTACK:
                go = True
            else:
                self.exp_counter += 1
                go = self.exp_counter == self.exp_period
            if go:
                self.exp_counter = 0
                if self.hold_zero:
                    rate_step = self.rate_period
                    continue
                if self.state == ATTACK:
                    self.counter = (self.counter + 1) & 0xFF
                    if self.counter == 0xFF:
                        self.state = DECAY_SUSTAIN
                        self.rate_period = RATE_PERIOD[self.decay]
                elif self.state == DECAY_SUSTAIN:
                    if self.counter == SUSTAIN_LEVEL[self.sustain]:
                        return
                    self.counter -= 1
                else:
                    self.counter = (self.counter - 1) & 0xFF
                self._set_exp()
            rate_step = self.rate_period


def noise_output_bits(sr):
    """Выходные 8 бит шума (старшие) из сдвигового регистра -> 12-битное значение."""
    return (((sr & 0x100000) >> 9) | ((sr & 0x040000) >> 8) | ((sr & 0x004000) >> 5) |
            ((sr & 0x000800) >> 3) | ((sr & 0x000200) >> 2) | ((sr & 0x000020) << 1) |
            ((sr & 0x000004) << 3) | ((sr & 0x000001) << 4))


class NoiseSeq:
    """Последовательность состояний LFSR 23 бит от 0x7FFFFF, лениво наращиваемая."""

    def __init__(self):
        self.sr = 0x7FFFFF
        self.out = [noise_output_bits(self.sr)]

    def get(self, upto):
        out = self.out
        sr = self.sr
        while len(out) <= upto:
            bit0 = ((sr >> 22) ^ (sr >> 17)) & 1
            sr = ((sr << 1) | bit0) & 0x7FFFFF
            out.append(noise_output_bits(sr))
        self.sr = sr
        return out


_NOISE = NoiseSeq()


def noise_pulse6581(n):
    return np.where(n < 0xF00, 0, n & (n << 1) & (n << 2))


def noise_pulse8580(n):
    return np.where(n < 0xFC0, n & (n << 1), 0xFC0)


def waveform12(model, wf, acc, pw, noise12=None):
    """12-битный выход генератора волн для массива значений аккумулятора (без ring/sync).
    wf - биты формы (1 треуг., 2 пила, 4 пульс, 8 шум)."""
    ix = (acc >> 12) & 0xFFF
    wave = WAVE_TABLES[model][wf & 7][ix]
    pulse = np.where(ix >= pw, 0xFFF, 0)
    no_pulse = 0xFFF if not (wf & 4) else 0
    if wf & 8:
        nz = noise12
    else:
        nz = 0xFFF
    out = wave & (no_pulse | pulse) & nz
    if (wf & 0xC) == 0xC:
        out = noise_pulse6581(out) if model == MOS6581 else noise_pulse8580(out)
    return out


def wave_period(model, ctrl, pw, freq):
    """Однопериодный выход голоса 1 при envelope = 255, как sidtrace waves (до фильтра/микшера):
    целочисленное (wave_dac - wave_zero) * env_dac, массив int64."""
    wf = (ctrl >> 4) & 15
    noise = bool(wf & 8)
    count = 32768 if noise else int(1048576 / freq)
    t = 200000 + 16 * (np.arange(count, dtype=np.int64) + 1)
    total = t * freq
    acc = total & 0xFFFFFF
    nz = None
    if noise:
        n = (total + (1 << 19)) >> 20
        seq = np.array(_NOISE.get(int(n.max())), dtype=np.int64)
        nz = seq[n] | 0xF  # младшие 4 бита: no_noise_or_noise_output = no_noise | noise_output
        nz = seq[n]
    w12 = waveform12(model, wf, acc, pw, nz)
    env = ENV_DAC[model][255]
    return (WAVE_DAC[model][w12] - WAVE_ZERO[model]) * env


# ═══════════════════════════════════════════════════════════════
# Фильтры (по тактам), внешний фильтр и микшер
# ═══════════════════════════════════════════════════════════════

EXT_A = 1.0 - math.exp(-1.0e5 * 1e-6)           # ФНЧ ~16 кГц (R=10k, C=1nF) за такт
EXT_B = 1.0 - math.exp(-100.0 * 1e-6)           # ФВЧ ~16 Гц


def svf_response(z, w, q):
    """Передаточные функции SVF reSID за такт: z^-1 = exp(-jw) (массив), w - частота среза за такт, q = 1/Q.
    Возвращает (Hlp, Hbp, Hhp) от входа Vi.  Уравнения (как filter.h, 8580):
        Vbp[n] = Vbp[n-1] - w*Vhp[n-1];  Vlp[n] = Vlp[n-1] - w*Vbp[n-1];  Vhp[n] = q*Vbp[n] - Vlp[n] - Vi[n]
    """
    z = np.where(np.abs(1.0 - z) < 1e-12, np.exp(-1e-9j), z)      # постоянная составляющая всё равно срезается внешним ФВЧ
    d = 1.0 - z                                    # 1 - z^-1
    # Vbp = -w z Vhp / d ; Vlp = -w z Vbp / d = w^2 z^2 Vhp / d^2
    # Vhp = q Vbp - Vlp - Vi  ->  Vhp (1 + q w z/d + w^2 z^2/d^2) = -Vi ... знаки: Vhp = q*(-w z Vhp/d) - w^2 z^2 Vhp/d^2 - Vi
    den = 1.0 + q * w * z / d + (w * z / d) ** 2
    hhp = -1.0 / den
    hbp = -w * z / d * hhp
    hlp = -w * z / d * hbp
    return hlp, hbp, hhp


def ext_filter_response(z):
    """Внешний фильтр SID: ФНЧ (EXT_A) и ФВЧ (EXT_B), каскад по тактам."""
    hlp = EXT_A / (1.0 - (1.0 - EXT_A) * z)
    # vhp += B*(vlp - vhp); out = vlp - vhp  -> out = vlp*(1 - B z/(1-(1-B)z))
    hhp = 1.0 - EXT_B * z / (1.0 - (1.0 - EXT_B) * z)
    return hlp * hhp


def filter_params(model, fc, res):
    """(w за такт, 1/Q, усиление) фильтра. 8580 - как в reSID; 6581 - подгонка под АЧХ reSID."""
    if model == MOS8580:
        w0 = (82355 * (fc + 1)) >> 11
        q = (1448, 1328, 1218, 1117, 1024, 939, 861, 790, 724, 664, 609, 558, 512, 470, 431, 395)[res] / 1024.0
        return w0 / float(1 << 20), q, (1.0, 1.0, 1.0)
    return _interp_f6581(fc, res)


F6581_FC = (0, 32, 64, 128, 192, 256, 320, 384, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1024, 1152, 1280, 1408, 1536, 1792, 2047)
F6581_W = (
    (1.6939e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.1674e-03, 2.5545e-03, 2.7733e-03, 3.0108e-03, 3.8525e-03, 5.3516e-03, 7.4342e-03, 1.1212e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.5422e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.1191e-01),
    (1.6939e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.5545e-03, 2.7733e-03, 3.0108e-03, 3.8525e-03, 5.3516e-03, 7.4342e-03, 1.1212e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.5422e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.1191e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 3.8525e-03, 5.3516e-03, 7.4342e-03, 1.1212e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.5422e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.1191e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 3.8525e-03, 5.3516e-03, 7.4342e-03, 1.1212e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.5422e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.1191e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 3.8525e-03, 5.3516e-03, 7.4342e-03, 1.2172e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 3.8525e-03, 5.8100e-03, 7.4342e-03, 1.2172e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 2.1674e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 3.8525e-03, 5.8100e-03, 8.0708e-03, 1.2172e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 4.1824e-03, 5.8100e-03, 8.0708e-03, 1.2172e-02, 1.5574e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.8354e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 4.1824e-03, 5.8100e-03, 8.0708e-03, 1.2172e-02, 1.6908e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.2962e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.0108e-03, 4.1824e-03, 6.3075e-03, 8.0708e-03, 1.2172e-02, 1.6908e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.2962e-02, 8.7463e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.2686e-03, 4.1824e-03, 6.3075e-03, 8.7620e-03, 1.3214e-02, 1.6908e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.7995e-02, 6.2962e-02, 8.0564e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.2686e-03, 4.5406e-03, 6.3075e-03, 9.5124e-03, 1.3214e-02, 1.6908e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.3420e-02, 6.2962e-02, 8.0564e-02, 1.0309e-01),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.2686e-03, 4.5406e-03, 6.8477e-03, 9.5124e-03, 1.3214e-02, 1.6908e-02, 2.1635e-02, 2.1635e-02, 3.2628e-02, 4.5325e-02, 5.3420e-02, 6.2962e-02, 8.0564e-02, 9.4954e-02),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.2686e-03, 4.9295e-03, 7.4342e-03, 1.0327e-02, 1.4346e-02, 1.6908e-02, 2.1635e-02, 2.3488e-02, 3.2628e-02, 4.5325e-02, 5.3420e-02, 6.2962e-02, 8.0564e-02, 9.4954e-02),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.5545e-03, 3.5486e-03, 5.3516e-03, 8.0708e-03, 1.1212e-02, 1.5574e-02, 1.8356e-02, 2.1635e-02, 2.3488e-02, 3.2628e-02, 4.5325e-02, 5.3420e-02, 6.2962e-02, 8.0564e-02, 9.4954e-02),
    (1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.5602e-03, 1.6939e-03, 1.8389e-03, 1.9964e-03, 2.3530e-03, 2.7733e-03, 3.8525e-03, 5.8100e-03, 8.7620e-03, 1.2172e-02, 1.5574e-02, 1.8356e-02, 2.3488e-02, 2.3488e-02, 3.2628e-02, 4.5325e-02, 5.3420e-02, 6.2962e-02, 8.0564e-02, 9.4954e-02),
)
F6581_Q = (
    (1.156, 1.156, 1.156, 1.246, 1.246, 1.246, 1.344, 1.448, 1.682, 1.682, 1.954, 2.107, 2.447, 2.271, 2.271, 1.954, 1.813, 1.813, 1.682, 1.448, 1.344, 1.246, 1.156, 1.246),
    (1.156, 1.156, 1.156, 1.156, 1.246, 1.344, 1.448, 1.448, 1.561, 1.682, 1.954, 2.107, 2.271, 2.271, 2.107, 1.954, 1.813, 1.682, 1.561, 1.344, 1.246, 1.246, 1.156, 1.156),
    (1.246, 1.246, 1.246, 1.156, 1.156, 1.246, 1.344, 1.344, 1.561, 1.682, 1.813, 1.954, 2.271, 2.107, 2.107, 1.813, 1.682, 1.682, 1.561, 1.344, 1.246, 1.246, 1.156, 1.156),
    (1.156, 1.156, 1.156, 1.073, 1.156, 1.246, 1.344, 1.344, 1.561, 1.561, 1.813, 1.954, 2.107, 2.107, 1.954, 1.813, 1.682, 1.561, 1.561, 1.344, 1.246, 1.156, 1.073, 1.156),
    (1.156, 1.156, 1.156, 1.156, 1.073, 1.156, 1.246, 1.246, 1.448, 1.561, 1.682, 1.813, 2.107, 1.954, 1.954, 1.682, 1.561, 1.561, 1.344, 1.246, 1.156, 1.156, 1.073, 0.995),
    (1.073, 1.073, 1.073, 1.073, 1.073, 1.156, 1.246, 1.246, 1.448, 1.448, 1.682, 1.813, 1.954, 1.813, 1.954, 1.561, 1.561, 1.448, 1.246, 1.246, 1.156, 1.156, 0.995, 0.995),
    (0.995, 0.995, 0.995, 0.995, 0.995, 1.073, 1.156, 1.156, 1.344, 1.448, 1.561, 1.682, 1.813, 1.813, 1.813, 1.561, 1.448, 1.448, 1.246, 1.156, 1.073, 1.073, 0.995, 0.924),
    (0.924, 0.924, 0.995, 0.995, 0.924, 0.995, 1.073, 1.156, 1.246, 1.344, 1.448, 1.561, 1.813, 1.682, 1.682, 1.448, 1.448, 1.344, 1.156, 1.156, 1.073, 0.995, 0.924, 0.857),
    (0.924, 0.924, 0.924, 0.924, 0.857, 0.995, 0.995, 1.073, 1.156, 1.246, 1.344, 1.561, 1.682, 1.561, 1.561, 1.448, 1.344, 1.246, 1.156, 1.073, 0.995, 0.857, 0.924, 0.857),
    (0.857, 0.857, 0.857, 0.857, 0.924, 0.924, 0.924, 0.995, 1.073, 1.156, 1.246, 1.448, 1.561, 1.448, 1.448, 1.344, 1.246, 1.156, 1.073, 0.995, 0.995, 0.857, 0.857, 0.795),
    (0.795, 0.795, 0.795, 0.795, 0.857, 0.857, 0.857, 0.924, 0.995, 1.073, 1.156, 1.344, 1.448, 1.448, 1.344, 1.246, 1.156, 1.073, 0.995, 0.924, 0.924, 0.795, 0.738, 0.738),
    (0.738, 0.738, 0.738, 0.738, 0.795, 0.795, 0.795, 0.857, 0.924, 0.995, 1.073, 1.246, 1.246, 1.448, 1.246, 1.073, 1.073, 0.995, 0.924, 0.924, 0.738, 0.738, 0.684, 0.684),
    (0.635, 0.684, 0.684, 0.684, 0.738, 0.738, 0.738, 0.795, 0.857, 0.924, 0.995, 1.156, 1.156, 1.246, 1.073, 0.995, 0.924, 0.924, 0.795, 0.795, 0.684, 0.684, 0.589, 0.589),
    (0.589, 0.589, 0.589, 0.589, 0.635, 0.635, 0.684, 0.684, 0.738, 0.795, 0.924, 1.073, 0.995, 1.156, 0.995, 0.857, 0.795, 0.857, 0.738, 0.738, 0.635, 0.635, 0.546, 0.507),
    (0.546, 0.546, 0.546, 0.546, 0.589, 0.589, 0.589, 0.635, 0.635, 0.738, 0.857, 0.995, 0.924, 0.995, 0.924, 0.795, 0.684, 0.738, 0.635, 0.684, 0.546, 0.546, 0.507, 0.470),
    (0.470, 0.470, 0.470, 0.470, 0.507, 0.507, 0.507, 0.546, 0.589, 0.684, 0.738, 0.924, 0.857, 0.924, 0.738, 0.635, 0.635, 0.589, 0.546, 0.589, 0.470, 0.470, 0.436, 0.376),
)
F6581_G = (
    (0.702, 0.702, 0.702, 0.725, 0.730, 0.662, 0.632, 0.611, 0.588, 0.571, 0.637, 0.604, 0.673, 0.692, 0.761, 0.764, 0.758, 0.783, 0.773, 0.775, 0.763, 0.748, 0.743, 0.752),
    (0.712, 0.712, 0.712, 0.713, 0.742, 0.776, 0.740, 0.623, 0.579, 0.584, 0.653, 0.623, 0.672, 0.717, 0.757, 0.780, 0.771, 0.777, 0.767, 0.771, 0.759, 0.759, 0.754, 0.752),
    (0.837, 0.837, 0.837, 0.724, 0.730, 0.764, 0.728, 0.614, 0.652, 0.657, 0.645, 0.621, 0.699, 0.718, 0.778, 0.776, 0.766, 0.791, 0.779, 0.782, 0.771, 0.770, 0.765, 0.763),
    (0.824, 0.824, 0.824, 0.714, 0.742, 0.778, 0.742, 0.627, 0.667, 0.650, 0.664, 0.645, 0.703, 0.747, 0.779, 0.795, 0.781, 0.787, 0.791, 0.795, 0.783, 0.769, 0.765, 0.774),
    (0.838, 0.838, 0.838, 0.839, 0.733, 0.767, 0.734, 0.620, 0.661, 0.667, 0.659, 0.648, 0.738, 0.753, 0.750, 0.795, 0.779, 0.803, 0.808, 0.793, 0.782, 0.782, 0.777, 0.772),
    (0.828, 0.827, 0.827, 0.829, 0.746, 0.783, 0.750, 0.634, 0.679, 0.663, 0.681, 0.678, 0.690, 0.762, 0.777, 0.798, 0.796, 0.803, 0.807, 0.807, 0.796, 0.795, 0.779, 0.785),
    (0.819, 0.819, 0.819, 0.821, 0.739, 0.775, 0.743, 0.631, 0.675, 0.682, 0.679, 0.687, 0.704, 0.742, 0.785, 0.822, 0.797, 0.821, 0.822, 0.807, 0.797, 0.797, 0.793, 0.789),
    (0.813, 0.813, 0.835, 0.837, 0.734, 0.770, 0.738, 0.719, 0.674, 0.681, 0.682, 0.641, 0.746, 0.758, 0.798, 0.830, 0.818, 0.825, 0.824, 0.824, 0.813, 0.800, 0.797, 0.794),
    (0.829, 0.829, 0.829, 0.831, 0.731, 0.788, 0.736, 0.719, 0.675, 0.682, 0.686, 0.680, 0.773, 0.779, 0.816, 0.806, 0.824, 0.831, 0.842, 0.827, 0.817, 0.813, 0.814, 0.810),
    (0.825, 0.825, 0.825, 0.827, 0.860, 0.786, 0.736, 0.721, 0.678, 0.687, 0.698, 0.705, 0.746, 0.811, 0.841, 0.821, 0.833, 0.841, 0.848, 0.833, 0.836, 0.832, 0.820, 0.817),
    (0.823, 0.823, 0.823, 0.826, 0.858, 0.786, 0.738, 0.725, 0.685, 0.696, 0.656, 0.739, 0.786, 0.813, 0.814, 0.843, 0.846, 0.854, 0.856, 0.840, 0.843, 0.840, 0.832, 0.827),
    (0.823, 0.823, 0.823, 0.827, 0.859, 0.789, 0.743, 0.732, 0.695, 0.711, 0.682, 0.719, 0.815, 0.826, 0.853, 0.853, 0.864, 0.872, 0.866, 0.863, 0.856, 0.852, 0.843, 0.838),
    (0.810, 0.826, 0.827, 0.830, 0.863, 0.795, 0.752, 0.743, 0.707, 0.730, 0.725, 0.775, 0.815, 0.869, 0.885, 0.887, 0.874, 0.895, 0.868, 0.864, 0.870, 0.866, 0.847, 0.850),
    (0.816, 0.816, 0.817, 0.820, 0.852, 0.786, 0.764, 0.743, 0.713, 0.743, 0.788, 0.795, 0.813, 0.882, 0.884, 0.917, 0.893, 0.874, 0.888, 0.881, 0.888, 0.883, 0.865, 0.858),
    (0.826, 0.826, 0.827, 0.831, 0.864, 0.799, 0.766, 0.766, 0.729, 0.795, 0.806, 0.843, 0.852, 0.905, 0.898, 0.914, 0.924, 0.901, 0.903, 0.904, 0.900, 0.896, 0.887, 0.878),
    (0.827, 0.827, 0.828, 0.832, 0.865, 0.804, 0.776, 0.785, 0.785, 0.811, 0.834, 0.902, 0.957, 0.966, 0.968, 0.966, 0.925, 0.931, 0.928, 0.923, 0.921, 0.916, 0.910, 0.889),
)


F6581_C = {
    1: (
        (1.00, 1.01, 1.01, 1.03, 1.05, 1.01, 1.02, 1.09, 1.18, 1.27, 1.54, 1.82, 1.72, 1.37, 1.11, 1.03, 1.03, 0.99, 0.98, 0.97, 0.98, 1.00, 1.01, 1.00),
        (1.00, 1.00, 1.01, 1.03, 1.05, 1.07, 1.10, 1.11, 1.17, 1.32, 1.59, 1.86, 1.68, 1.37, 1.11, 1.03, 1.02, 1.00, 0.99, 0.98, 1.00, 1.00, 1.00, 1.01),
        (1.05, 1.05, 1.06, 1.02, 1.04, 1.07, 1.11, 1.09, 1.23, 1.37, 1.59, 1.85, 1.67, 1.35, 1.11, 1.04, 1.04, 1.00, 0.99, 0.98, 0.99, 0.99, 1.00, 1.00),
        (1.05, 1.05, 1.06, 1.02, 1.04, 1.07, 1.13, 1.11, 1.28, 1.39, 1.68, 1.88, 1.63, 1.33, 1.11, 1.03, 1.03, 1.01, 0.99, 0.97, 0.99, 1.00, 1.01, 1.00),
        (1.04, 1.05, 1.06, 1.07, 1.04, 1.07, 1.13, 1.11, 1.31, 1.47, 1.73, 1.82, 1.61, 1.30, 1.16, 1.04, 1.04, 1.01, 0.97, 0.98, 1.00, 0.99, 1.00, 1.01),
        (1.04, 1.05, 1.06, 1.07, 1.04, 1.08, 1.16, 1.18, 1.39, 1.49, 1.82, 1.84, 1.62, 1.28, 1.15, 1.05, 1.04, 1.02, 0.99, 0.98, 0.99, 0.99, 1.01, 1.00),
        (1.04, 1.05, 1.05, 1.07, 1.04, 1.10, 1.17, 1.22, 1.43, 1.55, 1.91, 1.78, 1.57, 1.32, 1.15, 1.04, 1.05, 1.02, 0.98, 0.99, 1.00, 1.00, 1.00, 1.00),
        (1.04, 1.04, 1.05, 1.07, 1.03, 1.13, 1.19, 1.36, 1.48, 1.60, 1.92, 1.79, 1.53, 1.30, 1.14, 1.04, 1.04, 1.03, 0.99, 0.98, 0.99, 1.00, 1.01, 1.01),
        (1.03, 1.04, 1.04, 1.06, 1.03, 1.15, 1.22, 1.41, 1.51, 1.74, 1.91, 1.77, 1.46, 1.26, 1.13, 1.09, 1.05, 1.03, 0.99, 0.99, 1.00, 1.00, 1.00, 1.00),
        (1.02, 1.03, 1.04, 1.06, 1.11, 1.19, 1.38, 1.45, 1.59, 1.88, 1.88, 1.69, 1.47, 1.22, 1.11, 1.09, 1.05, 1.03, 0.99, 1.00, 0.99, 0.99, 1.00, 1.00),
        (1.02, 1.03, 1.04, 1.06, 1.14, 1.25, 1.48, 1.54, 1.80, 2.03, 1.83, 1.59, 1.39, 1.25, 1.16, 1.08, 1.05, 1.03, 1.00, 1.01, 1.00, 0.99, 1.00, 1.00),
        (1.01, 1.02, 1.03, 1.05, 1.17, 1.48, 1.55, 1.63, 2.03, 2.07, 1.75, 1.57, 1.31, 1.27, 1.13, 1.08, 1.05, 1.03, 1.01, 1.00, 0.99, 0.99, 1.00, 1.00),
        (1.01, 1.01, 1.02, 1.04, 1.25, 1.64, 1.63, 1.77, 2.29, 2.07, 1.63, 1.45, 1.32, 1.21, 1.09, 1.05, 1.05, 1.02, 1.02, 1.01, 0.99, 0.99, 1.01, 1.00),
        (1.00, 1.01, 1.02, 1.04, 1.79, 1.75, 1.64, 2.02, 2.29, 1.96, 1.47, 1.40, 1.32, 1.21, 1.12, 1.03, 1.04, 1.07, 1.02, 1.01, 0.99, 0.99, 1.00, 1.01),
        (0.98, 0.99, 1.00, 1.03, 1.90, 1.77, 1.63, 2.19, 2.24, 1.84, 1.36, 1.34, 1.29, 1.20, 1.13, 1.06, 1.02, 1.06, 1.02, 1.01, 1.00, 1.00, 1.00, 1.00),
        (0.97, 0.98, 0.99, 1.02, 1.91, 1.73, 1.61, 3.39, 2.15, 1.53, 1.22, 1.28, 1.19, 1.16, 1.07, 1.03, 1.06, 1.04, 1.02, 1.02, 1.00, 1.00, 0.99, 1.01),
    ),
    2: (
        (0.89, 0.89, 0.90, 0.91, 0.93, 0.98, 1.07, 1.20, 1.44, 1.52, 1.64, 1.87, 1.88, 1.75, 1.68, 1.62, 1.65, 1.56, 1.73, 1.74, 1.91, 2.05, 2.27, 2.65),
        (0.89, 0.89, 0.90, 0.91, 0.93, 0.96, 1.06, 1.21, 1.41, 1.54, 1.65, 1.87, 1.81, 1.72, 1.65, 1.62, 1.65, 1.56, 1.73, 1.73, 1.91, 2.06, 2.27, 2.62),
        (0.86, 0.86, 0.87, 0.91, 0.93, 0.96, 1.06, 1.19, 1.34, 1.44, 1.61, 1.80, 1.77, 1.67, 1.64, 1.60, 1.65, 1.56, 1.74, 1.74, 1.91, 2.06, 2.27, 2.62),
        (0.86, 0.86, 0.87, 0.91, 0.93, 0.96, 1.07, 1.20, 1.36, 1.41, 1.62, 1.77, 1.70, 1.63, 1.60, 1.59, 1.65, 1.56, 1.74, 1.75, 1.91, 2.05, 2.25, 2.62),
        (0.86, 0.86, 0.87, 0.88, 0.93, 0.96, 1.07, 1.19, 1.33, 1.43, 1.58, 1.68, 1.65, 1.57, 1.78, 1.57, 1.64, 1.56, 1.56, 1.74, 1.90, 2.05, 2.25, 2.39),
        (0.86, 0.86, 0.87, 0.88, 0.93, 0.97, 1.08, 1.23, 1.35, 1.39, 1.59, 1.64, 1.75, 1.52, 1.76, 1.56, 1.64, 1.55, 1.55, 1.75, 1.91, 2.06, 2.23, 2.39),
        (0.86, 0.86, 0.87, 0.88, 0.93, 0.99, 1.08, 1.23, 1.33, 1.40, 1.56, 1.55, 1.67, 1.66, 1.72, 1.55, 1.62, 1.55, 1.56, 1.74, 1.90, 2.05, 2.24, 2.37),
        (0.86, 0.86, 0.86, 0.88, 0.93, 1.00, 1.09, 1.22, 1.31, 1.38, 1.50, 1.64, 1.61, 1.60, 1.67, 1.52, 1.62, 1.54, 1.56, 1.75, 1.91, 2.04, 2.23, 2.35),
        (0.85, 0.86, 0.87, 0.88, 0.93, 1.01, 1.12, 1.21, 1.28, 1.39, 1.43, 1.58, 1.52, 1.54, 1.62, 1.70, 1.61, 1.53, 1.56, 1.75, 1.90, 1.83, 2.23, 2.35),
        (0.85, 0.86, 0.86, 0.88, 0.92, 1.03, 1.16, 1.20, 1.27, 1.39, 1.35, 1.47, 1.61, 1.46, 1.57, 1.66, 1.59, 1.51, 1.56, 1.74, 1.91, 1.84, 2.22, 2.33),
        (0.85, 0.86, 0.86, 0.88, 0.94, 1.06, 1.17, 1.20, 1.32, 1.38, 1.38, 1.35, 1.50, 1.57, 1.69, 1.61, 1.58, 1.49, 1.55, 1.73, 1.91, 1.84, 2.01, 2.31),
        (0.85, 0.85, 0.86, 0.88, 0.95, 1.15, 1.17, 1.20, 1.36, 1.33, 1.28, 1.41, 1.37, 1.67, 1.61, 1.55, 1.56, 1.47, 1.55, 1.75, 1.69, 1.83, 1.99, 2.29),
        (0.85, 0.85, 0.86, 0.87, 1.00, 1.17, 1.17, 1.23, 1.39, 1.27, 1.15, 1.27, 1.44, 1.53, 1.50, 1.50, 1.52, 1.46, 1.51, 1.71, 1.68, 1.82, 1.95, 2.07),
        (0.84, 0.85, 0.86, 0.88, 1.17, 1.16, 1.15, 1.28, 1.31, 1.14, 1.01, 1.29, 1.47, 1.57, 1.59, 1.43, 1.47, 1.61, 1.50, 1.70, 1.67, 1.81, 1.93, 2.02),
        (0.83, 0.84, 0.85, 0.87, 1.16, 1.15, 1.14, 1.29, 1.22, 1.04, 0.98, 1.29, 1.47, 1.56, 1.68, 1.56, 1.41, 1.54, 1.47, 1.69, 1.63, 1.77, 1.92, 2.00),
        (0.83, 0.83, 0.84, 0.86, 1.15, 1.12, 1.13, 1.61, 1.12, 0.90, 0.93, 1.28, 1.38, 1.55, 1.50, 1.45, 1.54, 1.45, 1.43, 1.66, 1.60, 1.73, 1.87, 1.89),
    ),
    4: (
        (0.76, 0.75, 0.74, 0.72, 0.70, 0.75, 0.79, 0.83, 0.96, 0.99, 0.97, 1.09, 1.12, 1.12, 1.13, 1.11, 1.14, 1.09, 1.26, 1.27, 1.42, 1.54, 1.75, 2.43),
        (0.75, 0.75, 0.74, 0.72, 0.69, 0.67, 0.70, 0.83, 0.94, 0.99, 0.96, 1.08, 1.09, 1.11, 1.11, 1.12, 1.15, 1.08, 1.24, 1.23, 1.37, 1.55, 1.77, 2.32),
        (0.67, 0.66, 0.66, 0.72, 0.70, 0.66, 0.70, 0.81, 0.82, 0.86, 0.93, 1.05, 1.07, 1.08, 1.11, 1.11, 1.14, 1.09, 1.26, 1.25, 1.38, 1.57, 1.78, 2.34),
        (0.67, 0.67, 0.66, 0.72, 0.70, 0.66, 0.70, 0.81, 0.81, 0.84, 0.93, 1.03, 1.04, 1.06, 1.09, 1.11, 1.16, 1.08, 1.28, 1.26, 1.40, 1.51, 1.71, 2.36),
        (0.67, 0.66, 0.66, 0.65, 0.70, 0.67, 0.69, 0.80, 0.80, 0.84, 0.90, 0.98, 1.01, 1.03, 1.26, 1.09, 1.14, 1.09, 1.08, 1.23, 1.36, 1.53, 1.73, 1.86),
        (0.67, 0.67, 0.66, 0.65, 0.70, 0.66, 0.69, 0.80, 0.80, 0.83, 0.89, 0.96, 1.13, 1.01, 1.25, 1.08, 1.16, 1.07, 1.06, 1.25, 1.38, 1.54, 1.65, 1.88),
        (0.67, 0.67, 0.66, 0.65, 0.70, 0.67, 0.69, 0.78, 0.79, 0.83, 0.87, 0.92, 1.10, 1.15, 1.23, 1.08, 1.15, 1.09, 1.08, 1.23, 1.34, 1.50, 1.67, 1.80),
        (0.68, 0.67, 0.66, 0.65, 0.70, 0.67, 0.68, 0.69, 0.78, 0.81, 0.84, 1.03, 1.07, 1.12, 1.18, 1.06, 1.16, 1.08, 1.07, 1.25, 1.37, 1.46, 1.61, 1.72),
        (0.67, 0.67, 0.67, 0.65, 0.70, 0.67, 0.68, 0.69, 0.76, 0.79, 0.80, 0.99, 1.02, 1.08, 1.14, 1.21, 1.15, 1.07, 1.09, 1.23, 1.34, 1.22, 1.63, 1.74),
        (0.67, 0.67, 0.67, 0.66, 0.63, 0.67, 0.67, 0.68, 0.75, 0.77, 0.76, 0.94, 1.13, 1.03, 1.11, 1.18, 1.14, 1.05, 1.09, 1.22, 1.37, 1.24, 1.58, 1.67),
        (0.67, 0.67, 0.67, 0.66, 0.64, 0.67, 0.67, 0.68, 0.75, 0.75, 0.84, 0.88, 1.07, 1.14, 1.22, 1.15, 1.13, 1.04, 1.08, 1.21, 1.35, 1.23, 1.31, 1.61),
        (0.67, 0.67, 0.67, 0.66, 0.64, 0.67, 0.68, 0.68, 0.73, 0.71, 0.78, 0.97, 0.99, 1.25, 1.19, 1.09, 1.11, 1.03, 1.07, 1.24, 1.11, 1.21, 1.29, 1.56),
        (0.68, 0.67, 0.67, 0.66, 0.65, 0.67, 0.68, 0.69, 0.71, 0.67, 0.71, 0.90, 1.07, 1.14, 1.10, 1.07, 1.06, 1.03, 1.04, 1.19, 1.10, 1.21, 1.21, 1.28),
        (0.67, 0.67, 0.67, 0.67, 0.65, 0.68, 0.67, 0.69, 0.66, 0.61, 0.64, 0.96, 1.11, 1.20, 1.18, 1.02, 1.02, 1.15, 1.04, 1.18, 1.10, 1.20, 1.20, 1.19),
        (0.67, 0.67, 0.67, 0.66, 0.66, 0.68, 0.66, 0.68, 0.61, 0.55, 0.67, 0.99, 1.13, 1.20, 1.28, 1.14, 0.97, 1.09, 1.01, 1.18, 1.06, 1.16, 1.19, 1.17),
        (0.66, 0.66, 0.66, 0.66, 0.66, 0.66, 0.65, 0.70, 0.56, 0.53, 0.68, 1.01, 1.07, 1.22, 1.12, 1.04, 1.08, 1.01, 0.98, 1.13, 1.02, 1.12, 1.14, 1.04),
    ),
}


def _interp_f6581(fc, res):
    fcs = F6581_FC
    i = 0
    while i < len(fcs) - 2 and fc >= fcs[i + 1]:
        i += 1
    t = (fc - fcs[i]) / float(fcs[i + 1] - fcs[i])
    t = min(1.0, max(0.0, t))
    w0, w1 = F6581_W[res][i], F6581_W[res][i + 1]
    w = math.exp(math.log(w0) * (1 - t) + math.log(w1) * t)
    q = F6581_Q[res][i] * (1 - t) + F6581_Q[res][i + 1] * t
    g = F6581_G[res][i] * (1 - t) + F6581_G[res][i + 1] * t
    cs = tuple(F6581_C[m][res][i] * (1 - t) + F6581_C[m][res][i + 1] * t for m in (1, 2, 4))
    return w, q, (g * cs[0], g * cs[1], g * cs[2])


# усиление: (выход SID) / (цифровое значение голоса), подобрано по reSID
K_VOICE = (8.4e-3, 7.6e-3)


def cycle_wave(model, wf, pw, freq):
    """Значения (wave_dac - wave_zero) * env за каждый такт одного периода (период = 2^24/freq тактов)."""
    P = (1 << 24) // freq
    c = np.arange(P, dtype=np.int64)
    acc = (c * freq) & 0xFFFFFF
    w12 = waveform12(model, wf, acc, pw, None)
    return (WAVE_DAC[model][w12] - WAVE_ZERO[model]) * ENV_DAC[model][255]


def capture_period(model, ctrl, pw, fc=0, rr=0, mv=0x0F, freq=8192):
    """Как sidtrace waves: возвращает массив float одного периода (шум: 32768 выборок)."""
    wf = (ctrl >> 4) & 15
    if wf == 0 or (wf & 8 and wf & 7):
        return np.zeros(32768 if wf & 8 else max(1, int(1048576 / freq)))
    route = rr & 1
    res = (rr >> 4) & 15
    mode = (mv >> 4) & 7
    vol = mv & 15
    if wf & 8:
        return _capture_noise(model, fc, rr, mv, freq, wf)
    x = cycle_wave(model, wf, pw, freq).astype(np.float64)
    P = len(x)
    X = np.fft.rfft(x)
    k = np.arange(len(X))
    z = np.exp(-2j * np.pi * k / P)
    H = np.ones(len(X), dtype=np.complex128)
    if route:
        w, q, g = filter_params(model, fc, res)
        hlp, hbp, hhp = svf_response(z, w, q)
        hlp, hbp, hhp = hlp * g[0], hbp * g[1], hhp * g[2]
        H = np.zeros_like(H)
        if mode & 1:
            H = H + hlp
        if mode & 2:
            H = H + hbp
        if mode & 4:
            H = H + hhp
    H = H * ext_filter_response(z) * (vol / 15.0) * K_VOICE[model]
    y = np.fft.irfft(X * H, P)
    return y[::16]


def _capture_noise(model, fc, rr, mv, freq, wf):
    count = 32768
    t = 200000 + 16 * (np.arange(count, dtype=np.int64) + 1)
    total = t * freq
    n = (total + (1 << 19)) >> 20
    seq = np.array(_NOISE.get(int(n.max())), dtype=np.int64)
    w12 = seq[n]
    x = ((WAVE_DAC[model][w12] - WAVE_ZERO[model]) * ENV_DAC[model][255]).astype(np.float64)
    route = rr & 1
    res = (rr >> 4) & 15
    mode = (mv >> 4) & 7
    vol = mv & 15
    # входной сигнал постоянен на 16 тактов: передаточная функция на частоте выборок 16 тактов
    # получается возведением за такт в 16-ю степень состояния; проще - частотная область с
    # суммой по 16 тактам: H16(z) = H(z) * (1 - z^16)/(1 - z) на частоте выборок
    N = count
    X = np.fft.rfft(x)
    k = np.arange(len(X))
    om = 2 * np.pi * k / N / 16.0                       # рад/такт
    z = np.exp(-1j * om)
    hold = (1 - z ** 16) / (1 - z + 1e-30)
    hold[0] = 16.0
    H = np.ones(len(X), dtype=np.complex128)
    if route:
        w, q, g = filter_params(model, fc, res)
        hlp, hbp, hhp = svf_response(z, w, q)
        hlp, hbp, hhp = hlp * g[0], hbp * g[1], hhp * g[2]
        H = np.zeros_like(H)
        if mode & 1:
            H = H + hlp
        if mode & 2:
            H = H + hbp
        if mode & 4:
            H = H + hhp
    H = H * ext_filter_response(z) * (vol / 15.0) * K_VOICE[model] * hold / 16.0
    return np.fft.irfft(X * H, N)


# ═══════════════════════════════════════════════════════════════
# SID-чип для плеера: регистры, огибающие, лениво считаемые осцилляторы
# ═══════════════════════════════════════════════════════════════

class Chip:
    def __init__(self, model):
        self.model = model
        self.regs = [0] * 32
        self.env = [Envelope() for _ in range(3)]
        self.time = 0                       # такт, до которого досчитаны огибающие
        # осцилляторы: аккумулятор без обрезки (для счёта сдвигов шума), как функция такта
        self.acc_t = [0, 0, 0]              # такт последней фиксации
        self.acc = [0, 0, 0]                # накопленное значение (без модуля)
        self.noise_base = [0, 0, 0]         # число сдвигов на момент сброса шума
        self.noise_seq_off = [0, 0, 0]      # индекс в последовательности LFSR
        self.test = [0, 0, 0]
        self.wav_log = None                 # список (такт, голос, регистр, значение) для рендера

    def freq(self, v):
        return self.regs[v * 7] | (self.regs[v * 7 + 1] << 8)

    def pw(self, v):
        return self.regs[v * 7 + 2] | ((self.regs[v * 7 + 3] & 15) << 8)

    def ctrl(self, v):
        return self.regs[v * 7 + 4]

    def advance(self, cyc):
        d = cyc - self.time
        if d > 0:
            for e in self.env:
                e.clock(d)
            self.time = cyc

    def fix_acc(self, v, cyc):
        """Довести аккумулятор голоса до такта cyc (при test он стоит)."""
        if not self.test[v]:
            self.acc[v] += self.freq(v) * (cyc - self.acc_t[v])
        self.acc_t[v] = cyc

    def read(self, reg, cyc):
        self.advance(cyc)
        if reg == 0x1B:
            return self.osc3(cyc)
        if reg == 0x1C:
            return self.env[2].counter
        return 0

    def osc3(self, cyc):
        self.fix_acc(2, cyc)
        wf = (self.ctrl(2) >> 4) & 15
        if not wf:
            return 0
        acc = np.array([self.acc[2] & 0xFFFFFF], dtype=np.int64)
        nz = None
        if wf & 8:
            n = (self.acc[2] - self.noise_base[2] + (1 << 19)) >> 20
            nz = np.array([_NOISE.get(n + self.noise_seq_off[2])[n + self.noise_seq_off[2]]], dtype=np.int64)
        w = waveform12(self.model, wf, acc, self.pw(2), nz)
        return int(w[0]) >> 4

    def write(self, reg, val, cyc):
        self.advance(cyc)
        v, r = divmod(reg, 7)
        if reg < 21:
            if r in (0, 1) and v < 3:
                self.fix_acc(v, cyc)
            elif r == 4:
                self.fix_acc(v, cyc)
                old = self.regs[reg]
                if (val & 8) and not (old & 8):                  # test: сброс аккумулятора и шума
                    self.acc[v] = 0
                    self.noise_base[v] = 0
                    self.noise_seq_off[v] = 0
                self.test[v] = 1 if (val & 8) else 0
        self.regs[reg] = val
        if reg < 21:
            if r == 4:
                self.env[v].write_control(val)
            elif r == 5:
                self.env[v].write_ad(val)
            elif r == 6:
                self.env[v].write_sr(val)
        if self.wav_log is not None and reg < 25:
            self.wav_log.append((cyc, reg, val))


# ═══════════════════════════════════════════════════════════════
# 6510 и PSID-плеер (порт sidtrace.cpp)
# ═══════════════════════════════════════════════════════════════

_RD = (6, 3, 2, 4, 5, 4, 4, 4, 4)
_WR = (6, 3, 2, 4, 6, 4, 5, 5, 4)
_RM = (8, 5, 2, 6, 8, 6, 7, 7, 6)
STOP = 0xFFF0


class Machine:
    def __init__(self):
        self.ram = bytearray(65536)
        self.chips = []
        self.bases = []
        self.cyc = 0
        self.clock_hz = 985248
        self.ftrace = None
        self.tick_cycles = 0
        self.next_tick = 0
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.fc = self.fz = self.fd = self.fv = self.fn = 0
        self.fi = 1
        self.pc = 0
        self.jam = False
        self.pcross = False

    def sid_index(self, addr):
        for i, b in enumerate(self.bases):
            if b <= addr < b + 0x20:
                return i
        return -1

    def ticks_until(self, T):
        """Выдать строки E для всех тиков <= T."""
        if not self.tick_cycles:
            return
        while self.next_tick <= T:
            tk = self.next_tick
            for c, chip in enumerate(self.chips):
                chip.advance(tk)
                if self.ftrace:
                    self.ftrace.append('E %d %d %d %d %d\n' % (tk, c, chip.env[0].counter,
                                                              chip.env[1].counter, chip.env[2].counter))
            self.next_tick += self.tick_cycles

    def rd(self, addr):
        if 0xD000 <= addr < 0xE000:
            c = self.sid_index(addr)
            if c >= 0:
                self.ticks_until(self.cyc)
                return self.chips[c].read(addr - self.bases[c], self.cyc)
            if addr == 0xD012:
                return (self.cyc // 63) % 312 & 0xFF
            if addr == 0xD011:
                return ((((self.cyc // 63) % 312) >> 1) & 0x80)
        return self.ram[addr]

    def wr(self, addr, v):
        self.ram[addr] = v
        if 0xD400 <= addr < 0xDE00:
            c = self.sid_index(addr)
            if c >= 0:
                reg = addr - self.bases[c]
                self.ticks_until(self.cyc)
                if reg < 0x19 and self.ftrace is not None:
                    self.ftrace.append('W %d %d %d %d\n' % (self.cyc, c, reg, v))
                if reg < 0x19:
                    self.chips[c].write(reg, v, self.cyc)

    def fetch(self):
        v = self.rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        l = self.fetch()
        return l | (self.fetch() << 8)

    def push(self, v):
        self.wr(0x100 + self.sp, v & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def pop(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.rd(0x100 + self.sp)

    def getp(self, b):
        return (self.fc | (self.fz << 1) | (self.fi << 2) | (self.fd << 3) | (0x10 if b else 0) | 0x20 |
                (self.fv << 6) | (self.fn << 7))

    def setp(self, p):
        self.fc = p & 1
        self.fz = (p >> 1) & 1
        self.fi = (p >> 2) & 1
        self.fd = (p >> 3) & 1
        self.fv = (p >> 6) & 1
        self.fn = (p >> 7) & 1

    def nz(self, v):
        self.fz = 1 if v == 0 else 0
        self.fn = v >> 7

    def ea(self, mode):
        self.pcross = False
        if mode == 0:
            z = self.fetch()
            p = (z + self.x) & 0xFF
            return self.rd(p) | (self.rd((p + 1) & 0xFF) << 8)
        if mode == 1:
            return self.fetch()
        if mode == 2:
            a = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
            return a
        if mode == 3:
            return self.fetch16()
        if mode == 4:
            z = self.fetch()
            b = self.rd(z) | (self.rd((z + 1) & 0xFF) << 8)
            r = (b + self.y) & 0xFFFF
            self.pcross = bool((b ^ r) & 0xFF00)
            return r
        if mode == 5:
            return (self.fetch() + self.x) & 0xFF
        if mode == 6:
            b = self.fetch16()
            r = (b + self.y) & 0xFFFF
            self.pcross = bool((b ^ r) & 0xFF00)
            return r
        if mode == 7:
            b = self.fetch16()
            r = (b + self.x) & 0xFFFF
            self.pcross = bool((b ^ r) & 0xFF00)
            return r
        return (self.fetch() + self.y) & 0xFF

    def mc(self, mode, kind):
        c = (_RD, _WR, _RM)[kind][mode]
        if kind == 0 and self.pcross and mode in (4, 6, 7):
            c += 1
        return c

    def adc(self, v):
        a = self.a
        if self.fd:
            lo = (a & 15) + (v & 15) + self.fc
            hi = (a >> 4) + (v >> 4)
            self.fz = 1 if ((a + v + self.fc) & 0xFF) == 0 else 0
            if lo > 9:
                lo += 6
                hi += 1
            self.fn = 1 if (hi & 8) else 0
            self.fv = 1 if (~(a ^ v) & (a ^ (hi << 4)) & 0x80) else 0
            if hi > 9:
                hi += 6
            self.fc = 1 if hi > 15 else 0
            self.a = ((hi << 4) | (lo & 15)) & 0xFF
        else:
            r = a + v + self.fc
            self.fv = 1 if (~(a ^ v) & (a ^ r) & 0x80) else 0
            self.fc = 1 if r > 255 else 0
            self.a = r & 0xFF
            self.nz(self.a)

    def sbc(self, v):
        a = self.a
        if self.fd:
            r = a - v - (1 - self.fc)
            lo = (a & 15) - (v & 15) - (1 - self.fc)
            hi = (a >> 4) - (v >> 4)
            if lo & 16:
                lo -= 6
                hi -= 1
            if hi & 16:
                hi -= 6
            self.fv = 1 if ((a ^ v) & (a ^ r) & 0x80) else 0
            self.fc = 1 if r >= 0 else 0
            self.nz(r & 0xFF)
            self.a = ((hi << 4) | (lo & 15)) & 0xFF
        else:
            r = a - v - (1 - self.fc)
            self.fv = 1 if ((a ^ v) & (a ^ r) & 0x80) else 0
            self.fc = 1 if r >= 0 else 0
            self.a = r & 0xFF
            self.nz(self.a)

    def cmp(self, r, v):
        self.fc = 1 if r >= v else 0
        self.nz((r - v) & 0xFF)

    def asl(self, v):
        self.fc = v >> 7
        v = (v << 1) & 0xFF
        self.nz(v)
        return v

    def lsr(self, v):
        self.fc = v & 1
        v >>= 1
        self.nz(v)
        return v

    def rol(self, v):
        c = self.fc
        self.fc = v >> 7
        v = ((v << 1) | c) & 0xFF
        self.nz(v)
        return v

    def ror(self, v):
        c = self.fc
        self.fc = v & 1
        v = (v >> 1) | (c << 7)
        self.nz(v)
        return v

    def step(self):
        op = self.fetch()
        cc = op & 3
        bbb = (op >> 2) & 7
        aaa = op >> 5
        if (op & 0x1F) == 0x10:
            k = aaa >> 1
            cond = (self.fn, self.fv, self.fc, self.fz)[k]
            if not (aaa & 1):
                cond = 0 if cond else 1
            d = self.fetch()
            if d >= 128:
                d -= 256
            cy = 2
            if cond:
                t = (self.pc + d) & 0xFFFF
                cy += 1 + (1 if ((t ^ self.pc) & 0xFF00) else 0)
                self.pc = t
            return cy
        if op == 0x00:
            self.pc = (self.pc + 1) & 0xFFFF
            self.push(self.pc >> 8)
            self.push(self.pc & 0xFF)
            self.push(self.getp(True))
            self.fi = 1
            self.pc = self.rd(0xFFFE) | (self.rd(0xFFFF) << 8)
            return 7
        if op == 0x20:
            t = self.fetch16()
            self.pc = (self.pc - 1) & 0xFFFF
            self.push(self.pc >> 8)
            self.push(self.pc & 0xFF)
            self.pc = t
            return 6
        if op == 0x40:
            self.setp(self.pop())
            l = self.pop()
            self.pc = l | (self.pop() << 8)
            return 6
        if op == 0x60:
            l = self.pop()
            self.pc = ((l | (self.pop() << 8)) + 1) & 0xFFFF
            return 6
        if op == 0x08:
            self.push(self.getp(True))
            return 3
        if op == 0x28:
            self.setp(self.pop())
            return 4
        if op == 0x48:
            self.push(self.a)
            return 3
        if op == 0x68:
            self.a = self.pop()
            self.nz(self.a)
            return 4
        if op == 0x88:
            self.y = (self.y - 1) & 0xFF
            self.nz(self.y)
            return 2
        if op == 0xA8:
            self.y = self.a
            self.nz(self.y)
            return 2
        if op == 0xC8:
            self.y = (self.y + 1) & 0xFF
            self.nz(self.y)
            return 2
        if op == 0xE8:
            self.x = (self.x + 1) & 0xFF
            self.nz(self.x)
            return 2
        simple = {0x18: ('fc', 0), 0x38: ('fc', 1), 0x58: ('fi', 0), 0x78: ('fi', 1), 0xB8: ('fv', 0),
                  0xD8: ('fd', 0), 0xF8: ('fd', 1)}
        if op in simple:
            setattr(self, *simple[op])
            return 2
        if op == 0x8A:
            self.a = self.x
            self.nz(self.a)
            return 2
        if op == 0x98:
            self.a = self.y
            self.nz(self.a)
            return 2
        if op == 0x9A:
            self.sp = self.x
            return 2
        if op == 0xAA:
            self.x = self.a
            self.nz(self.x)
            return 2
        if op == 0xBA:
            self.x = self.sp
            self.nz(self.x)
            return 2
        if op == 0xCA:
            self.x = (self.x - 1) & 0xFF
            self.nz(self.x)
            return 2
        if op == 0xEA:
            return 2
        if op == 0x4C:
            self.pc = self.fetch16()
            return 3
        if op == 0x6C:
            p = self.fetch16()
            self.pc = self.rd(p) | (self.rd((p & 0xFF00) | ((p + 1) & 0xFF)) << 8)
            return 5
        if op in (0x24, 0x2C):
            ad = self.ea(1 if op == 0x24 else 3)
            v = self.rd(ad)
            self.fz = 1 if (self.a & v) == 0 else 0
            self.fn = v >> 7
            self.fv = (v >> 6) & 1
            return 3 if op == 0x24 else 4
        if op in (0x0B, 0x2B):
            self.a &= self.fetch()
            self.nz(self.a)
            self.fc = self.fn
            return 2
        if op == 0x4B:
            self.a &= self.fetch()
            self.fc = self.a & 1
            self.a >>= 1
            self.nz(self.a)
            return 2
        if op == 0x6B:
            self.a &= self.fetch()
            self.a = (self.a >> 1) | (self.fc << 7)
            self.nz(self.a)
            self.fc = (self.a >> 6) & 1
            self.fv = ((self.a >> 6) ^ (self.a >> 5)) & 1
            return 2
        if op == 0x8B:
            self.a = (self.a | 0xEE) & self.x & self.fetch()
            self.nz(self.a)
            return 2
        if op == 0xAB:
            self.a = self.x = (self.a | 0xEE) & self.fetch()
            self.nz(self.a)
            return 2
        if op == 0xCB:
            r = (self.a & self.x) - self.fetch()
            self.fc = 1 if r >= 0 else 0
            self.x = r & 0xFF
            self.nz(self.x)
            return 2
        if op == 0xEB:
            self.sbc(self.fetch())
            return 2
        if op in (0x9B, 0x9C, 0x9E, 0x9F, 0xBB):
            self.fetch16()
            return 5
        if op == 0x80 or op in (0x82, 0x89, 0xC2, 0xE2):
            self.fetch()
            return 2
        if op in (0x04, 0x44, 0x64):
            self.fetch()
            return 3
        if op == 0x0C:
            self.fetch16()
            return 4
        if op in (0x14, 0x34, 0x54, 0x74, 0xD4, 0xF4):
            self.fetch()
            return 4
        if op in (0x1C, 0x3C, 0x5C, 0x7C, 0xDC, 0xFC):
            self.ea(7)
            return 4 + (1 if self.pcross else 0)
        if cc == 2 and bbb == 6 and aaa not in (4, 5):
            return 2
        if cc == 1:
            ad = self.ea(bbb)
            if aaa == 0:
                self.a |= self.rd(ad)
                self.nz(self.a)
            elif aaa == 1:
                self.a &= self.rd(ad)
                self.nz(self.a)
            elif aaa == 2:
                self.a ^= self.rd(ad)
                self.nz(self.a)
            elif aaa == 3:
                self.adc(self.rd(ad))
            elif aaa == 4:
                self.wr(ad, self.a)
                return self.mc(bbb, 1)
            elif aaa == 5:
                self.a = self.rd(ad)
                self.nz(self.a)
            elif aaa == 6:
                self.cmp(self.a, self.rd(ad))
            else:
                self.sbc(self.rd(ad))
            return self.mc(bbb, 0)
        if cc == 2:
            if bbb == 2:
                if aaa == 0:
                    self.a = self.asl(self.a)
                elif aaa == 1:
                    self.a = self.rol(self.a)
                elif aaa == 2:
                    self.a = self.lsr(self.a)
                elif aaa == 3:
                    self.a = self.ror(self.a)
                return 2
            mode = bbb
            if bbb == 4 or (bbb == 0 and aaa < 4):
                self.jam = True
                return 2
            if bbb == 0:
                mode = 2
            if aaa in (4, 5):
                if bbb == 5:
                    mode = 8
                if bbb == 7:
                    mode = 6
            ad = self.ea(mode)
            if aaa <= 3:
                v = (self.asl, self.rol, self.lsr, self.ror)[aaa](self.rd(ad))
                self.wr(ad, v)
                return self.mc(mode, 2)
            if aaa == 4:
                self.wr(ad, self.x)
                return self.mc(mode, 1)
            if aaa == 5:
                self.x = self.rd(ad)
                self.nz(self.x)
                return self.mc(mode, 0)
            if aaa == 6:
                v = (self.rd(ad) - 1) & 0xFF
                self.wr(ad, v)
                self.nz(v)
                return self.mc(mode, 2)
            v = (self.rd(ad) + 1) & 0xFF
            self.wr(ad, v)
            self.nz(v)
            return self.mc(mode, 2)
        if cc == 0:
            mode = bbb
            if bbb == 0:
                mode = 2
            if bbb in (2, 4, 6):
                self.jam = True
                return 2
            ad = self.ea(mode)
            if aaa == 4:
                self.wr(ad, self.y)
                return self.mc(mode, 1)
            if aaa == 5:
                self.y = self.rd(ad)
                self.nz(self.y)
                return self.mc(mode, 0)
            if aaa == 6:
                self.cmp(self.y, self.rd(ad))
                return self.mc(mode, 0)
            if aaa == 7:
                self.cmp(self.x, self.rd(ad))
                return self.mc(mode, 0)
            self.jam = True
            return 2
        mode = bbb
        if aaa in (4, 5):
            if bbb == 5:
                mode = 8
            if bbb == 7:
                mode = 6
        if bbb == 2:
            self.jam = True
            return 2
        ad = self.ea(mode)
        if aaa == 0:
            v = self.asl(self.rd(ad))
            self.wr(ad, v)
            self.a |= v
            self.nz(self.a)
        elif aaa == 1:
            v = self.rol(self.rd(ad))
            self.wr(ad, v)
            self.a &= v
            self.nz(self.a)
        elif aaa == 2:
            v = self.lsr(self.rd(ad))
            self.wr(ad, v)
            self.a ^= v
            self.nz(self.a)
        elif aaa == 3:
            v = self.ror(self.rd(ad))
            self.wr(ad, v)
            self.adc(v)
        elif aaa == 4:
            self.wr(ad, self.a & self.x)
            return self.mc(mode, 1)
        elif aaa == 5:
            self.a = self.x = self.rd(ad)
            self.nz(self.a)
            return self.mc(mode, 0)
        elif aaa == 6:
            v = (self.rd(ad) - 1) & 0xFF
            self.wr(ad, v)
            self.cmp(self.a, v)
        else:
            v = (self.rd(ad) + 1) & 0xFF
            self.wr(ad, v)
            self.sbc(v)
        return self.mc(mode, 2)


def run_sub(m, addr, max_cycles, irq_style):
    m.jam = False
    if irq_style:
        m.push(STOP >> 8)
        m.push(STOP & 0xFF)
        m.push(m.getp(False))
    else:
        m.push((STOP - 1) >> 8)
        m.push((STOP - 1) & 0xFF)
    m.pc = addr
    start = m.cyc
    while m.cyc - start < max_cycles:
        if m.pc == STOP:
            return True
        m.cyc += m.step()
        if m.jam:
            return False
    return False


def _be16(d, o):
    return (d[o] << 8) | d[o + 1]


def play_psid(path, seconds, song=None, model=None, clock=None, want_wav_log=False):
    """Запуск как sidtrace play. Возвращает (машина, строки трассы, параметры)."""
    with open(path, 'rb') as f:
        d = f.read()
    if len(d) < 0x76 or d[:4] not in (b'PSID', b'RSID'):
        raise SystemExit("не PSID/RSID файл")
    version = _be16(d, 4)
    off = _be16(d, 6)
    load = _be16(d, 8)
    init = _be16(d, 10)
    play = _be16(d, 12)
    songs = _be16(d, 14)
    start_song = _be16(d, 16)
    speed = (d[18] << 24) | (d[19] << 16) | (d[20] << 8) | d[21]
    flags = _be16(d, 0x76) if version >= 2 else 0
    song = song if song else start_song
    song = max(1, min(song, songs))
    mdl = MOS8580 if (model == '8580' or (not model and ((flags >> 4) & 3) == 2)) else MOS6581
    pal = not (clock == 'ntsc' or (not clock and ((flags >> 2) & 3) == 2))
    m = Machine()
    m.clock_hz = 985248 if pal else 1022727
    pos = off
    if load == 0:
        load = d[pos] | (d[pos + 1] << 8)
        pos += 2
    n = min(len(d) - pos, 0x10000 - load)
    m.ram[load:load + n] = d[pos:pos + n]
    if init == 0:
        init = load
    m.bases = [0xD400]
    if version >= 3:
        for k in range(2):
            b = d[0x7A + k]
            if b and 0x42 <= b <= 0xFE and not (b & 1):
                m.bases.append(0xD000 | (b << 4))
    m.chips = [Chip(mdl) for _ in m.bases]
    if want_wav_log:
        for c in m.chips:
            c.wav_log = []
    m.tick_cycles = m.clock_hz // 200
    m.next_tick = m.tick_cycles
    lines = []
    m.ftrace = lines
    cia = bool((speed >> ((song - 1) & 31)) & 1)
    fps = 60 if cia else (50 if pal else 60)
    frame_cycles = m.clock_hz // fps
    lines.append('# clock %d\n# chips %d\n# model %s\n# fps %d\n' % (
        m.clock_hz, len(m.chips), '8580' if mdl == MOS8580 else '6581', fps))
    ram = m.ram
    ram[0xEA31] = 0x40
    ram[0xEA81] = 0x40
    ram[0xFEBC] = 0x40
    ram[STOP] = 0x4C
    ram[STOP + 1] = STOP & 0xFF
    ram[STOP + 2] = STOP >> 8
    ram[0x0314] = 0x31
    ram[0x0315] = 0xEA
    ram[0] = 0x2F
    ram[1] = 0x37
    m.a = (song - 1) & 0xFF
    m.x = m.y = 0
    m.sp = 0xFD
    m.fi = 0
    if not run_sub(m, init, 3000000, False):
        print("warning: INIT did not return", file=sys.stderr)
    lines.append('# frames_start %d\n' % m.cyc)
    total = int(seconds * m.clock_hz)
    frame_t = m.cyc
    bad = 0
    while frame_t < total:
        m.cyc = frame_t
        entry = play
        irq = False
        if not play:
            v = ram[0x0314] | (ram[0x0315] << 8)
            if v == 0xEA31:
                v = ram[0xFFFE] | (ram[0xFFFF] << 8)
            entry = v
            irq = True
        if entry:
            m.sp = 0xFD
            if not run_sub(m, entry, frame_cycles * 4, irq):
                bad += 1
                if bad > 50:
                    print("play routine keeps failing, stopping", file=sys.stderr)
                    break
        frame_t += frame_cycles
        if m.cyc < frame_t:
            m.cyc = frame_t
    m.ticks_until(total)
    lines.append('# end %d\n' % total)
    info = {'clock': m.clock_hz, 'chips': len(m.chips), 'model': mdl, 'fps': fps, 'total': total}
    return m, lines, info


# ═══════════════════════════════════════════════════════════════
# Рендер WAV (44.1 кГц, моно) по журналу записей
# ═══════════════════════════════════════════════════════════════

NOISE_CAP = 1 << 21
OVERSAMPLE = 8


def _step_fn(times, values, init=0):
    """Ступенчатая функция по записям: t -> последнее значение с временем <= t."""
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)

    def f(t):
        idx = np.searchsorted(times, t, side='right') - 1
        out = np.where(idx >= 0, values[np.maximum(idx, 0)], init)
        return out
    return f


def _reg_history(log, reg, nbits=8):
    ts = [c for c, r, v in log if r == reg]
    vs = [v for c, r, v in log if r == reg]
    return ts, vs


def _osc_acc(log, v, model, tt):
    """Накопленный (без модуля) аккумулятор голоса v в моменты tt (такты, возрастающие)."""
    ev = sorted([(c, r - v * 7, val) for c, r, val in log if r in (v * 7, v * 7 + 1, v * 7 + 4)])
    freq = 0
    test = 0
    pts_t = [0.0]
    pts_a = [0.0]
    pts_f = [0.0]
    pts_r = [0.0]                       # такт последнего сброса
    acc = 0.0
    t_prev = 0.0
    reset_t = 0.0
    lo = hi = 0
    ctrl = 0
    for c, r, val in ev:
        if not test:
            acc += freq * (c - t_prev)
        t_prev = float(c)
        if r == 0:
            lo = val
        elif r == 1:
            hi = val
        else:
            if (val & 8) and not (ctrl & 8):
                acc = 0.0
                reset_t = float(c)
            test = 1 if (val & 8) else 0
            ctrl = val
        freq = lo | (hi << 8)
        pts_t.append(float(c))
        pts_a.append(acc)
        pts_f.append(0.0 if test else float(freq))
        pts_r.append(reset_t)
    pts_t = np.array(pts_t)
    idx = np.searchsorted(pts_t, tt, side='right') - 1
    idx = np.maximum(idx, 0)
    A = np.array(pts_a)[idx] + np.array(pts_f)[idx] * (tt - pts_t[idx])
    R = np.array(pts_r)[idx]
    return A, R


class _SvfCache:
    def __init__(self):
        self.c = {}

    def get(self, w, q, n):
        key = (round(w, 7), round(q, 4), n)
        r = self.c.get(key)
        if r is None:
            A = np.array([[1 - w * q, w], [-w, 1.0]])
            B = np.array([w, 0.0])
            An = np.linalg.matrix_power(A, n)
            S = np.zeros((2, 2))
            P = np.eye(2)
            for _ in range(n):
                S += P
                P = P @ A
            r = (An, S @ B)
            if len(self.c) > 4000:
                self.c.clear()
            self.c[key] = r
        return r


def render_chip(model, log, total_cycles, clock_hz, out_rate=44100):
    """Моно float массив (шкала int16) одного чипа."""
    n_out = int(total_cycles * out_rate / clock_hz)
    cps = clock_hz / float(out_rate)
    log = sorted(log)
    if not log or n_out <= 0:
        return np.zeros(max(n_out, 0))
    S = np.arange(n_out, dtype=np.float64)
    tt = ((S[:, None] + (np.arange(OVERSAMPLE)[None, :] + 0.5) / OVERSAMPLE) * cps).ravel()
    voices = np.zeros((3, n_out))
    for v in range(3):
        wf_f = _step_fn(*_reg_history(log, v * 7 + 4))
        pwl = _step_fn(*_reg_history(log, v * 7 + 2))
        pwh = _step_fn(*_reg_history(log, v * 7 + 3))
        A, R = _osc_acc(log, v, model, tt)
        ctrl = wf_f(tt).astype(np.int64)
        wf = (ctrl >> 4) & 15
        pw = (pwl(tt).astype(np.int64)) | ((pwh(tt).astype(np.int64) & 15) << 8)
        acc = A.astype(np.int64) & 0xFFFFFF
        w12 = np.zeros(len(tt), dtype=np.int64)
        for f in np.unique(wf):
            m = wf == f
            if f == 0:
                continue
            nz = None
            if f & 8:
                n = ((A[m] - R[m] * 0 + (1 << 19)) // (1 << 20)).astype(np.int64)
                # номер сдвига считается от сброса аккумулятора (test); R - такт сброса, поэтому вычитаем
                # накопленное до сброса: аккумулятор уже обнулён в _osc_acc, так что n относительно сброса
                seq = np.array(_NOISE.get(min(int(n.max()), NOISE_CAP)), dtype=np.int64)
                nz = seq[n % len(seq)]
            w12[m] = waveform12(model, int(f), acc[m], pw[m], nz)
        val = (WAVE_DAC[model][w12] - WAVE_ZERO[model]).astype(np.float64)
        val[wf == 0] = 0.0
        # огибающая: точный генератор по тактам выборок
        voices[v] = val.reshape(n_out, OVERSAMPLE).mean(axis=1)
    # огибающие и микшер/фильтр по выборкам
    env = [Envelope() for _ in range(3)]
    env_vals = np.zeros((3, n_out))
    wi = 0
    L = len(log)
    t_env = 0
    for s in range(n_out):
        t_next = int((s + 1) * cps)
        while wi < L and log[wi][0] <= t_next:
            c, r, val = log[wi]
            wi += 1
            if c > t_env:
                for e in env:
                    e.clock(c - t_env)
                t_env = c
            if r < 21:
                vv, rr = divmod(r, 7)
                if rr == 4:
                    env[vv].write_control(val)
                elif rr == 5:
                    env[vv].write_ad(val)
                elif rr == 6:
                    env[vv].write_sr(val)
        if t_next > t_env:
            d = t_next - t_env
            for e in env:
                e.clock(d)
            t_env = t_next
        env_vals[0, s] = env[0].counter
        env_vals[1, s] = env[1].counter
        env_vals[2, s] = env[2].counter
    edac = ENV_DAC[model]
    voices = voices * edac[env_vals.astype(np.int64)]
    # регистры фильтра/режима на каждую выборку
    ts = S * cps
    fcl = _step_fn(*_reg_history(log, 21))(ts).astype(np.int64) & 7
    fch = _step_fn(*_reg_history(log, 22))(ts).astype(np.int64)
    fc = (fch << 3) | fcl
    rr = _step_fn(*_reg_history(log, 23))(ts).astype(np.int64)
    mv = _step_fn(*_reg_history(log, 24))(ts).astype(np.int64)
    return _mix(model, voices, fc, rr, mv, cps)


def _mix(model, voices, fc, rr, mv, cps):
    n = voices.shape[1]
    out = np.zeros(n)
    svf = _SvfCache()
    sb = sl = 0.0
    frac = 0.0
    n_lo = int(cps)
    prev_key = None
    hp = 0.0
    prev_in = 0.0
    k_v = K_VOICE[model]
    for s in range(n):
        filt = int(rr[s]) & 15
        res = (int(rr[s]) >> 4) & 15
        mode = (int(mv[s]) >> 4) & 15
        vol = int(mv[s]) & 15
        v1, v2, v3 = voices[0, s], voices[1, s], voices[2, s]
        vi = 0.0
        direct = 0.0
        for i, vv in enumerate((v1, v2, v3)):
            if filt & (1 << i):
                vi += vv
            elif not (i == 2 and (mode & 8)):
                direct += vv
        n_cyc = n_lo + (1 if frac + cps - n_lo >= 1.0 else 0)
        frac = frac + cps - n_cyc
        if filt & 7 and mode & 7:
            w, q, g = filter_params(model, int(fc[s]), res)
            An, Bn = svf.get(w, q, n_cyc)
            nb = An[0, 0] * sb + An[0, 1] * sl + Bn[0] * vi
            nl = An[1, 0] * sb + An[1, 1] * sl + Bn[1] * vi
            sb, sl = nb, nl
            vhp = q * sb - sl - vi
            f_out = 0.0
            if mode & 1:
                f_out += g[0] * sl
            if mode & 2:
                f_out += g[1] * sb
            if mode & 4:
                f_out += g[2] * vhp
        else:
            sb *= 0.9
            sl *= 0.9
            f_out = 0.0
        y = (direct + f_out) * (vol / 15.0) * k_v
        # ФВЧ ~16 Гц
        hp = 0.9977 * (hp + y - prev_in)
        prev_in = y
        out[s] = hp
    return out


def render_wav_raw(machine, info, path):
    """Сумма всех чипов -> int16 моно 44.1 кГц файл (как out.raw sidtrace)."""
    total = info['total']
    clk = info['clock']
    mix = None
    for chip in machine.chips:
        y = render_chip(chip.model, chip.wav_log, total, clk)
        mix = y if mix is None else mix + y[:len(mix)] if len(y) >= len(mix) else mix[:len(y)] + y
    mix = np.clip(np.round(mix), -32768, 32767).astype('<i2')
    mix.tofile(path)


# ═══════════════════════════════════════════════════════════════
# Интерфейс, как у sidtrace
# ═══════════════════════════════════════════════════════════════

def trace(path, prefix, seconds, raw_path=None, song=None, model=None, clock=None):
    """Аналог `sidtrace play`: prefix.trace и (по желанию) raw_path."""
    m, lines, info = play_psid(path, seconds, song or None, model or None, clock or None,
                               want_wav_log=bool(raw_path))
    with open(prefix + '.trace', 'w') as f:
        f.write(''.join(lines))
    if raw_path:
        render_wav_raw(m, info, raw_path)


def waves(model, specs):
    """Аналог `sidtrace waves`: specs 'ctrl:pw[:fc:rr:mv:freq]' -> список массивов."""
    mi = MOS8580 if str(model) == '8580' else MOS6581
    out = []
    for sp_ in specs:
        f = [int(float(x)) if '.' in x else int(x, 0) for x in sp_.split(':')]
        defaults = [0, 0, 0, 0, 0x0F, 8192]
        f += defaults[len(f):]
        out.append(capture_period(mi, f[0], f[1], f[2], f[3], f[4], f[5]))
    return out


if __name__ == '__main__':
    if len(sys.argv) >= 5 and sys.argv[1] == 'play':
        a = sys.argv
        trace(a[2], a[3], float(a[4]), a[5] if len(a) > 5 and a[5] else None,
              int(a[6]) if len(a) > 6 else None, a[7] if len(a) > 7 else None, a[8] if len(a) > 8 else None)
    else:
        print("usage: sid_pyemu.py play in.sid prefix seconds [out.raw] [song] [model] [clock]")
        sys.exit(2)
