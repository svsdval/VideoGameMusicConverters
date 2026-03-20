"""
VGM to XM/MIDI Converter v2
Исправлены сэмплы: правильная длина периода для корректной тональности.
"""

import struct
import gzip
import sys
import os
import math
from dataclasses import dataclass, field
from typing import Optional


VGM_SAMPLE_RATE = 44100
NOTE_NAMES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-']
XM_BASE_RATE = 8363  # XM sample rate для C-4 при relative_note=0, finetune=0


def freq_to_xm_note(freq):
    if freq <= 0 or freq < 20 or freq > 16000:
        return None
    midi = 69 + 12 * math.log2(freq / 440.0)
    xm = int(round(midi)) - 11
    return xm if 1 <= xm <= 96 else None


def xm_note_to_midi(xm_note):
    return xm_note + 11


def note_name(xm_note):
    if not xm_note or xm_note < 1 or xm_note > 96:
        return "---"
    n = xm_note - 1
    return f"{NOTE_NAMES[n % 12]}{n // 12}"


@dataclass
class VGMHeader:
    eof_offset: int = 0; version: int = 0
    sn76489_clock: int = 0; ym2612_clock: int = 0
    ym2151_clock: int = 0; ay8910_clock: int = 0
    total_samples: int = 0; loop_offset: int = 0
    loop_samples: int = 0; rate: int = 0; data_offset: int = 0
    title: str = ""; game: str = ""; author: str = ""; system: str = ""

@dataclass
class TrackerNote:
    note: int = 0; instrument: int = 0; volume: int = 0
    effect: int = 0; effect_param: int = 0

@dataclass
class NoteEvent:
    sample: int = 0; channel: int = 0; event_type: str = ""
    note: int = 0; instrument: int = 0; volume: int = 64; freq: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Генератор сэмплов — ИСПРАВЛЕНО
# ═══════════════════════════════════════════════════════════════

class SampleGenerator:
    """
    Генерация реалистичных синтетических сэмплов.
    
    Период = 32 сэмпла → C-4 (261.63 Hz) при XM rate 8363 Hz.
    FM инструменты используют ×2 или ×4 период для лучшего качества
    (компенсируется через relative_note в XMWriter).
    """

    PERIOD = 32

    # ─── PSG (прямоугольные волны с band-limited сглаживанием) ────

    @classmethod
    def square_wave(cls, duty=0.5):
        """Прямоугольная волна с лёгким сглаживанием переходов"""
        p = cls.PERIOD
        s = []
        for i in range(p):
            phase = i / p
            # Основная прямоугольная + сглаживание через гармоники
            val = 0.0
            for h in range(1, 16, 2):  # нечётные гармоники
                coeff = math.sin(2 * math.pi * h * duty) / h if duty != 0.5 else 1.0 / h
                val += coeff * math.sin(2 * math.pi * h * phase)
            s.append(int(max(-1, min(1, val * 1.2)) * 24000))
        return s

    @classmethod
    def sine_wave(cls):
        p = cls.PERIOD
        return [int(24000 * math.sin(2 * math.pi * i / p)) for i in range(p)]

    @classmethod
    def triangle_wave(cls):
        """Треугольная волна через гармоники"""
        p = cls.PERIOD
        s = []
        for i in range(p):
            phase = i / p
            val = 0.0
            for h in range(0, 8):
                n = 2 * h + 1
                val += ((-1) ** h) * math.sin(2 * math.pi * n * phase) / (n * n)
            s.append(int(max(-1, min(1, val * 1.5)) * 24000))
        return s

    @classmethod
    def sawtooth_wave(cls):
        """Пилообразная через гармоники"""
        p = cls.PERIOD
        s = []
        for i in range(p):
            phase = i / p
            val = 0.0
            for h in range(1, 16):
                val += ((-1) ** (h + 1)) * math.sin(2 * math.pi * h * phase) / h
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    # ─── Noise ────────────────────────────────────────────────

    @staticmethod
    def noise(length=4096):
        """Металлический шум (LFSR-стиль как в PSG)"""
        import random
        random.seed(42)
        s = []
        lfsr = 0x7FFF
        for _ in range(length):
            bit = ((lfsr >> 0) ^ (lfsr >> 1)) & 1
            lfsr = (lfsr >> 1) | (bit << 14)
            s.append(24000 if lfsr & 1 else -24000)
        return s

    # ─── FM синтез (YM2612-style, 4-оператора) ───────────────

    @classmethod
    def fm_organ(cls):
        """Органная труба: carrier + 2 модулятора, много гармоник"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Drawbar organ: несколько гармоник с разной амплитудой
            val = (math.sin(ph) * 0.6 +           # 8'
                   math.sin(ph * 2) * 0.4 +        # 4'
                   math.sin(ph * 3) * 0.3 +        # 2 2/3'
                   math.sin(ph * 4) * 0.25 +       # 2'
                   math.sin(ph * 6) * 0.15 +       # 1 1/3'
                   math.sin(ph * 8) * 0.1 +        # 1'
                   math.sin(ph * 10) * 0.05 +      # 4/5'
                   math.sin(ph * 12) * 0.03)        # 2/3'
            s.append(int(max(-1, min(1, val / 1.3)) * 24000))
        return s

    @classmethod
    def fm_brass(cls):
        """Медные духовые: сильная FM модуляция, яркий тембр"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # 2 каскада модуляции
            mod2 = math.sin(ph * 1) * 1.5
            mod1 = math.sin(ph * 1 + mod2) * 2.5
            carrier = math.sin(ph + mod1)
            # Добавляем гармоники для рычания
            val = carrier * 0.7 + math.sin(ph * 2 + mod1 * 0.5) * 0.2 + math.sin(ph * 3) * 0.1
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_bass(cls):
        """Бас: глубокий FM с суб-гармониками"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Модулятор с высоким ratio для металлического призвука
            mod = math.sin(ph * 2) * 2.0
            carrier = math.sin(ph + mod)
            # Суб-бас
            sub = math.sin(ph * 0.5) * 0.3
            # Лёгкий обертон
            overtone = math.sin(ph * 3 + mod * 0.3) * 0.1
            val = carrier * 0.6 + sub + overtone
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_lead(cls):
        """Лид-синтезатор: яркий, пронзительный"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Несколько модуляторов для сложного тембра
            mod1 = math.sin(ph * 3) * 1.8
            mod2 = math.sin(ph * 7 + mod1 * 0.5) * 0.6
            carrier = math.sin(ph + mod1 + mod2)
            # Октавный дублёр
            octave = math.sin(ph * 2 + mod1 * 0.3) * 0.25
            val = carrier * 0.7 + octave
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_strings(cls):
        """Струнные: хорус-эффект через расстройку, мягкий тембр"""
        p = cls.PERIOD * 8  # длинный для детализации
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Несколько слегка расстроенных осцилляторов (хорус)
            detune = 0.003
            v1 = math.sin(ph * (1.0 - detune))
            v2 = math.sin(ph * (1.0 + detune))
            v3 = math.sin(ph * (2.0 - detune * 2)) * 0.4
            v4 = math.sin(ph * (2.0 + detune * 2)) * 0.4
            v5 = math.sin(ph * 3.0) * 0.15
            v6 = math.sin(ph * 4.0) * 0.08
            val = (v1 + v2) * 0.35 + (v3 + v4) * 0.5 + v5 + v6
            s.append(int(max(-1, min(1, val / 1.2)) * 24000))
        return s

    @classmethod
    def fm_piano(cls):
        """Пианино: яркая атака с быстрым затуханием гармоник"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Колокольная FM (высокий ratio модулятора)
            mod = math.sin(ph * 14) * 0.8
            carrier = math.sin(ph + mod)
            # Гармоники деревянного резонанса
            h2 = math.sin(ph * 2 + mod * 0.3) * 0.35
            h3 = math.sin(ph * 3) * 0.2
            h4 = math.sin(ph * 4 + mod * 0.1) * 0.12
            h5 = math.sin(ph * 5) * 0.06
            val = carrier * 0.5 + h2 + h3 + h4 + h5
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_guitar(cls):
        """Электрогитара: karplus-strong приближение + FM обертоны"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Основной тон с сильными нечётными гармониками (как у струны)
            fundamental = math.sin(ph)
            h2 = math.sin(ph * 2) * 0.5
            h3 = math.sin(ph * 3) * 0.35
            h4 = math.sin(ph * 4) * 0.25
            h5 = math.sin(ph * 5) * 0.2
            h6 = math.sin(ph * 6) * 0.15
            h7 = math.sin(ph * 7) * 0.12
            h8 = math.sin(ph * 8) * 0.08
            h9 = math.sin(ph * 9) * 0.06
            # FM грязь для overdrive
            mod = math.sin(ph * 3) * 0.3
            dirt = math.sin(ph + mod) * 0.15
            val = (fundamental + h2 + h3 + h4 + h5 + h6 + h7 + h8 + h9) * 0.4 + dirt
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_bell(cls):
        """Колокол / Marimba: нечётные обертоны, металлический призвук"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Классический FM bell: carrier:modulator = 1:3.5
            mod = math.sin(ph * 3.5) * 2.5
            carrier = math.sin(ph + mod)
            # Дополнительные обертоны
            h1 = math.sin(ph * 5.3 + mod * 0.2) * 0.15
            h2 = math.sin(ph * 7.1) * 0.08
            val = carrier * 0.7 + h1 + h2
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_flute(cls):
        """Флейта: чистый тон с лёгким дыханием"""
        p = cls.PERIOD * 4
        s = []
        import random
        random.seed(123)
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Почти чистый синус с лёгкой модуляцией (вибрато воздуха)
            breath = random.gauss(0, 0.02)
            val = (math.sin(ph) * 0.8 +
                   math.sin(ph * 2) * 0.12 +
                   math.sin(ph * 3) * 0.04 +
                   breath * 0.3)
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_trumpet(cls):
        """Труба: много нечётных гармоник, яркий звук"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Труба: сильные нечётные гармоники
            val = 0.0
            for h in range(1, 12):
                amp = 1.0 / (h ** 0.7)  # медленное затухание гармоник
                if h % 2 == 0:
                    amp *= 0.6  # чётные тише
                val += amp * math.sin(ph * h)
            # FM buzz
            mod = math.sin(ph * 5) * 0.4
            val = val * 0.5 + math.sin(ph + mod) * 0.3
            s.append(int(max(-1, min(1, val / 2.0)) * 24000))
        return s

    @classmethod
    def fm_epiano(cls):
        """Электропиано (Rhodes-style): колокольные обертоны"""
        p = cls.PERIOD * 4
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            # Rhodes: каждый зубец вилки = синус + колокольная FM
            mod = math.sin(ph * 7) * 0.5
            tine = math.sin(ph + mod)
            # Bark (деревянный корпус)
            bark = (math.sin(ph) * 0.5 +
                    math.sin(ph * 2) * 0.2 +
                    math.sin(ph * 3) * 0.1)
            val = tine * 0.5 + bark * 0.5
            s.append(int(max(-1, min(1, val)) * 24000))
        return s

    @classmethod
    def fm_synth_pad(cls):
        """Синт-пэд: мягкий, широкий, с хорусом"""
        p = cls.PERIOD * 8
        s = []
        for i in range(p):
            ph = 2 * math.pi * i / p
            d = 0.005  # сильный детюн для ширины
            v1 = math.sin(ph * (1.0 - d))
            v2 = math.sin(ph * (1.0 + d))
            v3 = math.sin(ph * (0.5 - d * 0.5)) * 0.3  # суб-октава
            v4 = math.sin(ph * (2.0 + d * 1.5)) * 0.2  # верхняя октава
            # Мягкая FM
            mod = math.sin(ph * 3) * 0.2
            v5 = math.sin(ph + mod) * 0.15
            val = (v1 + v2) * 0.35 + v3 + v4 + v5
            s.append(int(max(-1, min(1, val / 1.1)) * 24000))
        return s


# ═══════════════════════════════════════════════════════════════
# Чипы
# ═══════════════════════════════════════════════════════════════

class SN76489State:
    def __init__(self, clock=3579545):
        self.clock = clock
        self.freq_reg = [0, 0, 0, 0]
        self.attenuation = [15, 15, 15, 15]
        self.current_note = [-1, -1, -1, -1]
        self.latch_channel = 0; self.latch_type = 0
    def get_frequency(self, ch):
        if ch >= 3 or self.freq_reg[ch] <= 0: return 0
        return self.clock / (32.0 * self.freq_reg[ch])
    def get_volume(self, ch):
        a = self.attenuation[ch]
        return 0 if a >= 15 else max(1, int(64 * (15 - a) / 15))


class YM2612State:
    def __init__(self, clock=7670453):
        self.clock = clock
        self.regs = [bytearray(256), bytearray(256)]
        self.fnum = [0]*6; self.block = [0]*6; self.algorithm = [0]*6
        self.total_level = [[127]*4 for _ in range(6)]
        self.key_on = [False]*6; self.current_note = [-1]*6
    def get_frequency(self, ch):
        fn = self.fnum[ch]; bl = self.block[ch]
        if fn <= 0: return 0
        return (fn * self.clock) / (144.0 * (1 << (21 - bl)))
    def get_volume(self, ch):
        algo = self.algorithm[ch]
        carriers = {0:[3],1:[3],2:[3],3:[3],4:[1,3],5:[1,2,3],6:[1,2,3],7:[0,1,2,3]}
        ops = carriers.get(algo, [3])
        avg = sum(self.total_level[ch][o] for o in ops) / len(ops)
        return max(1, int(64 * (1.0 - avg / 127.0)))


class AY8910State:
    def __init__(self, clock=1789773):
        self.clock = clock; self.regs = bytearray(16)
        self.current_note = [-1, -1, -1]
    def get_frequency(self, ch):
        if ch >= 3: return 0
        period = self.regs[ch*2] | ((self.regs[ch*2+1] & 0x0F) << 8)
        return self.clock / (16.0 * period) if period > 0 else 0
    def get_volume(self, ch):
        if ch >= 3: return 0
        return max(1, int(64 * (self.regs[8+ch] & 0x0F) / 15))
    def is_tone_enabled(self, ch):
        return not bool(self.regs[7] & (1 << ch))


class YM2151State:
    def __init__(self, clock=3579545):
        self.clock = clock; self.regs = bytearray(256)
        self.key_on = [False]*8; self.current_note = [-1]*8
        self.kc = [0]*8; self.kf = [0]*8
    def get_frequency(self, ch):
        kc = self.kc[ch]; octave = (kc >> 4) & 7; ni = kc & 0x0F
        nm = {0:0,1:1,2:2,4:3,5:4,6:5,8:6,9:7,10:8,12:9,13:10,14:11}
        semi = nm.get(ni, 0)
        return 440.0 * (2.0 ** ((semi - 9) / 12.0)) * (2.0 ** (octave - 4))
    def get_volume(self, ch):
        tl = self.regs[0x60 + ch] & 0x7F
        return max(1, int(64 * (1.0 - tl / 127.0)))


# ═══════════════════════════════════════════════════════════════
# VGM Parser
# ═══════════════════════════════════════════════════════════════

class VGMParser:
    def __init__(self, filename):
        self.filename = filename
        self.data = self._load(filename)
        self.header = self._parse_header()
        self.events = []
        self.current_sample = 0
        self.sn76489 = SN76489State(self.header.sn76489_clock) if self.header.sn76489_clock else None
        self.ym2612 = YM2612State(self.header.ym2612_clock) if self.header.ym2612_clock else None
        self.ay8910 = AY8910State(self.header.ay8910_clock) if self.header.ay8910_clock else None
        self.ym2151 = YM2151State(self.header.ym2151_clock) if self.header.ym2151_clock else None

    def _load(self, fn):
        with open(fn, 'rb') as f: d = f.read()
        if d[:2] == b'\x1f\x8b': d = gzip.decompress(d)
        if d[:4] != b'Vgm ': raise ValueError("Not VGM")
        return d

    def _r32(self, o):
        return struct.unpack_from('<I', self.data, o)[0] if o+4 <= len(self.data) else 0

    def _parse_header(self):
        h = VGMHeader(); d = self.data
        h.eof_offset = self._r32(0x04); h.version = self._r32(0x08)
        h.sn76489_clock = self._r32(0x0C); h.total_samples = self._r32(0x18)
        h.loop_offset = self._r32(0x1C); h.loop_samples = self._r32(0x20)
        h.rate = self._r32(0x24)
        if h.version >= 0x110 and len(d) > 0x34:
            h.ym2612_clock = self._r32(0x2C); h.ym2151_clock = self._r32(0x30)
        if h.version >= 0x150 and len(d) > 0x38:
            do = self._r32(0x34); h.data_offset = 0x34+do if do else 0x40
        else: h.data_offset = 0x40
        if h.version >= 0x151 and len(d) > 0x78:
            h.ay8910_clock = self._r32(0x74)
        gd3 = self._r32(0x14)
        if gd3: self._parse_gd3(0x14+gd3, h)
        return h

    def _parse_gd3(self, off, h):
        d = self.data
        if off+12 > len(d) or d[off:off+4] != b'Gd3 ': return
        sz = self._r32(off+8); pos = off+12; end = min(pos+sz, len(d))
        ss = []; cur = b''
        while pos < end and len(ss) < 11:
            if pos+1 >= end: break
            c = d[pos:pos+2]; pos += 2
            if c == b'\x00\x00':
                try: ss.append(cur.decode('utf-16-le', errors='replace'))
                except: ss.append('')
                cur = b''
            else: cur += c
        if len(ss)>0: h.title=ss[0]
        if len(ss)>2: h.game=ss[2]
        if len(ss)>4: h.system=ss[4]
        if len(ss)>6: h.author=ss[6]

    def get_duration(self):
        return self.header.total_samples / VGM_SAMPLE_RATE if self.header.total_samples > 0 else 0

    def _handle_sn76489(self, bv):
        p = self.sn76489
        if not p: return
        if bv & 0x80:
            ch=(bv>>5)&3; iv=(bv>>4)&1; dat=bv&0x0F
            p.latch_channel=ch; p.latch_type=iv
            if iv: p.attenuation[ch]=dat
            else: p.freq_reg[ch]=(p.freq_reg[ch]&0x3F0)|dat
            self._upd_psg(ch)
        else:
            ch=p.latch_channel; dat=bv&0x3F
            if p.latch_type==0:
                if ch<3: p.freq_reg[ch]=(p.freq_reg[ch]&0x0F)|(dat<<4)
                else: p.freq_reg[ch]=dat
            else: p.attenuation[ch]=dat&0x0F
            self._upd_psg(ch)

    def _upd_psg(self, ch):
        p=self.sn76489; vol=p.get_volume(ch)
        if ch<3:
            xc=ch; freq=p.get_frequency(ch); note=freq_to_xm_note(freq) if freq>0 else None; inst=ch+1
            if vol==0 or note is None:
                if p.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    p.current_note[ch]=-1
            else:
                if p.current_note[ch]!=note:
                    if p.current_note[ch]>=0:
                        self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_on',
                        note=note,instrument=inst,volume=vol,freq=freq))
                    p.current_note[ch]=note
        else:
            xc=3; inst=4; nn=48
            if vol==0:
                if p.current_note[3]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    p.current_note[3]=-1
            else:
                if p.current_note[3]<0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_on',
                        note=nn,instrument=inst,volume=vol))
                    p.current_note[3]=nn

    def _handle_ym2612(self, port, reg, val):
        ym=self.ym2612
        if not ym: return
        ym.regs[port][reg]=val; co=port*3
        if reg==0x28 and port==0:
            cn=val&7; ci=cn-4+3 if cn>=4 else cn
            if ci>5: return
            ko=(val&0xF0)!=0; xc=ci+4
            if ko and not ym.key_on[ci]:
                freq=ym.get_frequency(ci); note=freq_to_xm_note(freq); vol=ym.get_volume(ci)
                if note and 1<=note<=96:
                    if ym.current_note[ci]>=0:
                        self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    inst=5+min(ym.algorithm[ci],5)
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_on',
                        note=note,instrument=inst,volume=vol,freq=freq))
                    ym.current_note[ci]=note
            elif not ko and ym.key_on[ci]:
                if ym.current_note[ci]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    ym.current_note[ci]=-1
            ym.key_on[ci]=ko; return
        if 0xA0<=reg<=0xA6:
            ci2=reg&3
            if ci2>2: return
            ci=ci2+co
            if ci>5: return
            if reg<0xA4: ym.fnum[ci]=(ym.fnum[ci]&0x700)|val
            else: ym.fnum[ci]=(ym.fnum[ci]&0xFF)|((val&7)<<8); ym.block[ci]=(val>>3)&7
            return
        if 0xB0<=reg<=0xB2:
            ci=(reg-0xB0)+co
            if ci<=5: ym.algorithm[ci]=val&7
            return
        if 0x40<=reg<=0x4F:
            ci2=reg&3
            if ci2>2: return
            oi=(reg-0x40)>>2; ci=ci2+co
            if ci<=5 and oi<=3:
                ym.total_level[ci][[0,2,1,3][oi]]=val&0x7F

    def _handle_ay8910(self, reg, val):
        ay=self.ay8910
        if not ay or reg>=16: return
        ay.regs[reg]=val
        if reg<=5: self._upd_ay(reg//2)
        elif reg==7:
            for c in range(3): self._upd_ay(c)
        elif 8<=reg<=10: self._upd_ay(reg-8)

    def _upd_ay(self, ch):
        ay=self.ay8910; xc=ch+10; inst=11+ch
        if not ay.is_tone_enabled(ch):
            if ay.current_note[ch]>=0:
                self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                ay.current_note[ch]=-1
            return
        freq=ay.get_frequency(ch); vol=ay.get_volume(ch)
        note=freq_to_xm_note(freq) if freq>0 else None
        if vol==0 or note is None:
            if ay.current_note[ch]>=0:
                self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                ay.current_note[ch]=-1
        else:
            if ay.current_note[ch]!=note:
                if ay.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_on',
                    note=note,instrument=inst,volume=vol,freq=freq))
                ay.current_note[ch]=note

    def _handle_ym2151(self, reg, val):
        ym=self.ym2151
        if not ym: return
        ym.regs[reg]=val
        if reg==0x08:
            ch=val&7; ko=(val&0x78)!=0; xc=ch+13
            if ko and not ym.key_on[ch]:
                freq=ym.get_frequency(ch); note=freq_to_xm_note(freq); vol=ym.get_volume(ch)
                if note and 1<=note<=96:
                    if ym.current_note[ch]>=0:
                        self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_on',
                        note=note,instrument=14,volume=vol,freq=freq))
                    ym.current_note[ch]=note
            elif not ko and ym.key_on[ch]:
                if ym.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=xc,event_type='note_off',note=97))
                    ym.current_note[ch]=-1
            ym.key_on[ch]=ko
        elif 0x28<=reg<=0x2F: ym.kc[reg&7]=val
        elif 0x30<=reg<=0x37: ym.kf[reg&7]=(val>>2)&0x3F

    def parse(self):
        pos=self.header.data_offset; d=self.data; dl=len(d)
        while pos<dl:
            cmd=d[pos]; pos+=1
            if cmd==0x50:
                if pos<dl: self._handle_sn76489(d[pos]); pos+=1
            elif cmd==0x52:
                if pos+1<dl: self._handle_ym2612(0,d[pos],d[pos+1]); pos+=2
            elif cmd==0x53:
                if pos+1<dl: self._handle_ym2612(1,d[pos],d[pos+1]); pos+=2
            elif cmd==0x54:
                if pos+1<dl: self._handle_ym2151(d[pos],d[pos+1]); pos+=2
            elif cmd==0xA0:
                if pos+1<dl: self._handle_ay8910(d[pos],d[pos+1]); pos+=2
            elif cmd==0x61:
                if pos+1<dl: self.current_sample+=struct.unpack_from('<H',d,pos)[0]; pos+=2
            elif cmd==0x62: self.current_sample+=735
            elif cmd==0x63: self.current_sample+=882
            elif cmd==0x66: break
            elif 0x70<=cmd<=0x7F: self.current_sample+=(cmd&0x0F)+1
            elif 0x80<=cmd<=0x8F: self.current_sample+=cmd&0x0F
            elif cmd==0x67:
                if pos+6<dl: pos+=2; pos+=4+struct.unpack_from('<I',d,pos)[0]
                else: break
            elif cmd==0x4F: pos+=1
            elif 0xA0<=cmd<=0xBF: pos+=2
            elif 0xC0<=cmd<=0xDF: pos+=3
            elif 0xE0<=cmd<=0xFF: pos+=4
            elif 0x30<=cmd<=0x4E: pos+=1
            elif 0x55<=cmd<=0x5F: pos+=2
        self._close_all()

    def _close_all(self):
        if self.sn76489:
            for ch in range(4):
                if self.sn76489.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=ch if ch<3 else 3,event_type='note_off',note=97))
        if self.ym2612:
            for ch in range(6):
                if self.ym2612.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=ch+4,event_type='note_off',note=97))
        if self.ay8910:
            for ch in range(3):
                if self.ay8910.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=ch+10,event_type='note_off',note=97))
        if self.ym2151:
            for ch in range(8):
                if self.ym2151.current_note[ch]>=0:
                    self.events.append(NoteEvent(sample=self.current_sample,channel=ch+13,event_type='note_off',note=97))


# ═══════════════════════════════════════════════════════════════
# MIDI Writer
# ═══════════════════════════════════════════════════════════════

class MIDIWriter:
    GM = {0:80,1:80,2:80,3:115, 4:19,5:18,6:33,7:81,8:48,9:5,
          10:80,11:80,12:80, 13:81,14:81,15:81,16:81,17:81,18:81,19:81,20:81}

    def __init__(self): self.tracks=[]; self.tpqn=480; self.bpm=120

    def build(self, events, bpm=120, transpose=0, octave=0, title=""):
        ts=transpose+octave*12; self.bpm=bpm
        bps=bpm/60.0; spt=1.0/(bps*self.tpqn)
        s2t=(1.0/VGM_SAMPLE_RATE)/spt
        chs={}
        for ev in events: chs.setdefault(ev.channel,[]).append(ev)
        self.tracks=[]
        t0=[]; usec=int(60_000_000/bpm)
        t0.append((0,bytes([0xFF,0x51,0x03,(usec>>16)&0xFF,(usec>>8)&0xFF,usec&0xFF])))
        if title:
            td=title.encode('ascii',errors='replace')[:127]
            t0.append((0,bytes([0xFF,0x01,len(td)])+td))
        self.tracks.append(t0)
        for ci,ch in enumerate(sorted(chs.keys())):
            if ci>=15: break
            mc=ci if ci<9 else ci+1; trk=[]
            trk.append((0,bytes([0xC0|mc,self.GM.get(ch,0)])))
            tn=f"Ch {ch}".encode()[:127]
            trk.append((0,bytes([0xFF,0x03,len(tn)])+tn))
            act={}
            for ev in chs[ch]:
                mt=int(ev.sample*s2t)
                if ev.event_type=='note_on':
                    mn=max(0,min(127,xm_note_to_midi(ev.note)+ts))
                    vel=max(1,min(127,ev.volume*2))
                    if ch in act:
                        pn,_=act[ch]; trk.append((mt,bytes([0x80|mc,pn,0])))
                    trk.append((mt,bytes([0x90|mc,mn,vel]))); act[ch]=(mn,mt)
                elif ev.event_type=='note_off':
                    if ch in act:
                        pn,_=act[ch]; trk.append((mt,bytes([0x80|mc,pn,0]))); del act[ch]
            for pn,pt in act.values():
                trk.append((pt+self.tpqn,bytes([0x80|mc,pn,0])))
            self.tracks.append(trk)

    def write(self, fn):
        with open(fn,'wb') as f:
            f.write(b'MThd'); f.write(struct.pack('>I',6))
            f.write(struct.pack('>HHH',1,len(self.tracks),self.tpqn))
            for trk in self.tracks:
                td=self._enc(trk); f.write(b'MTrk'); f.write(struct.pack('>I',len(td))); f.write(td)

    def _enc(self, evts):
        evts.sort(key=lambda x:x[0]); d=bytearray(); prev=0
        for at,ed in evts:
            d.extend(self._vlq(max(0,at-prev))); d.extend(ed); prev=at
        d.extend(self._vlq(0)); d.extend(b'\xFF\x2F\x00'); return bytes(d)

    @staticmethod
    def _vlq(v):
        if v<0: v=0
        r=[v&0x7F]; v>>=7
        while v: r.append((v&0x7F)|0x80); v>>=7
        r.reverse(); return bytes(r)


# ═══════════════════════════════════════════════════════════════
# XM Writer — с исправленными сэмплами
# ═══════════════════════════════════════════════════════════════

class XMWriter:
    def __init__(self):
        self.title=""; self.num_channels=8; self.bpm=150; self.speed=1
        self.patterns=[]; self.instruments=[]; self.order=[]; self.channel_map={}

    def build(self, events, title="", bpm=150, speed=1,
              transpose=0, octave=0, finetune=0, compact=1):
        self.title=title[:20]; self.bpm=bpm; self.speed=speed
        used=sorted(set(e.channel for e in events)) if events else []
        self.channel_map={ch:i for i,ch in enumerate(used)}
        self.num_channels=max(len(used),1)
        if self.num_channels%2: self.num_channels+=1

        total_transpose=transpose+octave*12
        self._build_instruments(total_transpose, finetune)

        if compact>1:
            events=self._compact_events(events, compact)
            self.bpm=max(32, bpm//compact)

        self._build_patterns(events)

    def _build_instruments(self, transpose=0, finetune=0):
        gen = SampleGenerator
        rel = max(-128, min(127, transpose))
        ft = max(-128, min(127, finetune))
        p = SampleGenerator.PERIOD

        def mk(name, pcm, loop=True):
            return {'name': name[:22], 'samples': [{
                'name': name[:22], 'data': pcm, 'length': len(pcm),
                'loop_start': 0,
                'loop_length': len(pcm) if loop else 0,
                'loop_type': 1 if loop else 0,
                'volume': 64, 'finetune': ft, 'panning': 128,
                'relative_note': rel, 'bits': 16}]}

        def mk_fm(name, pcm, periods):
            """FM с коррекцией octave для кратного периода"""
            octave_shift = int(round(12 * math.log2(periods))) if periods > 1 else 0
            adj_rel = max(-128, min(127, rel + octave_shift))
            return {'name': name[:22], 'samples': [{
                'name': name[:22], 'data': pcm, 'length': len(pcm),
                'loop_start': 0, 'loop_length': len(pcm),
                'loop_type': 1, 'volume': 64, 'finetune': ft,
                'panning': 128, 'relative_note': adj_rel, 'bits': 16}]}

        self.instruments = [
            # 1-3: PSG
            mk("PSG Square 50%", gen.square_wave(0.5)),
            mk("PSG Square 25%", gen.square_wave(0.25)),
            mk("PSG Square 12%", gen.square_wave(0.125)),
            # 4: Noise
            mk("PSG Noise", gen.noise(), loop=False),
            # 5-10: YM2612 FM (по алгоритмам)
            mk_fm("FM Organ", gen.fm_organ(), 4),
            mk_fm("FM Brass", gen.fm_brass(), 4),
            mk_fm("FM Bass", gen.fm_bass(), 4),
            mk_fm("FM Lead", gen.fm_lead(), 4),
            mk_fm("FM Strings", gen.fm_strings(), 8),
            mk_fm("FM E.Piano", gen.fm_epiano(), 4),
            # 11-13: AY-3-8910
            mk("AY Square", gen.square_wave(0.5)),
            mk("AY Sawtooth", gen.sawtooth_wave()),
            mk("AY Triangle", gen.triangle_wave()),
            # 14: YM2151
            mk_fm("OPM Trumpet", gen.fm_trumpet(), 4),
        ]

    @staticmethod
    def _compact_events(events, factor):
        if factor<=1: return events
        return events  # compact через samples_per_row в _build_patterns

    def _build_patterns(self, events):
        rpp=64
        if not events:
            self.patterns=[[[TrackerNote() for _ in range(self.num_channels)] for _ in range(rpp)]]
            self.order=[0]; return
        row_sec=self.speed*2.5/self.bpm
        spr=max(1,int(VGM_SAMPLE_RATE*row_sec))
        max_s=max(e.sample for e in events)
        max_row=max_s//spr+1
        np_=max(1,min((max_row+rpp)//rpp,256))
        self.patterns=[[[TrackerNote() for _ in range(self.num_channels)] for _ in range(rpp)] for _ in range(np_)]
        seen={}
        for ev in events:
            row=ev.sample//spr
            ch=self.channel_map.get(ev.channel)
            if ch is None: continue
            pi=row//rpp; ri=row%rpp
            if pi>=len(self.patterns): continue
            nd=self.patterns[pi][ri][ch]; key=(row,ch)
            if ev.event_type=='note_on':
                nd.note=max(1,min(96,ev.note))
                nd.instrument=min(ev.instrument,len(self.instruments))
                nd.volume=0x10+min(0x40,max(0,ev.volume))
                seen[key]='note_on'
            elif ev.event_type=='note_off':
                if key not in seen or seen[key]!='note_on':
                    if nd.note==0: nd.note=97
                    seen[key]='note_off'
        self.order=list(range(np_))

    def write(self, fn):
        with open(fn,'wb') as f:
            self._wh(f)
            for p in self.patterns: self._wp(f,p)
            for inst in self.instruments[:128]: self._wi(f,inst)

    def _wh(self,f):
        f.write(b'Extended Module: ')
        f.write(self.title.encode('ascii','replace')[:20].ljust(20,b'\x00'))
        f.write(b'\x1a'); f.write(b'VGM2XM Converter    ')
        f.write(struct.pack('<H',0x0104)); f.write(struct.pack('<I',276))
        f.write(struct.pack('<H',len(self.order))); f.write(struct.pack('<H',0))
        f.write(struct.pack('<H',self.num_channels)); f.write(struct.pack('<H',len(self.patterns)))
        f.write(struct.pack('<H',min(len(self.instruments),128)))
        f.write(struct.pack('<H',1)); f.write(struct.pack('<H',self.speed))
        f.write(struct.pack('<H',self.bpm))
        ot=bytearray(256)
        for i,o in enumerate(self.order[:256]): ot[i]=o
        f.write(ot)

    def _wp(self,f,pat):
        pk=bytearray()
        for row in pat:
            for ch in range(self.num_channels):
                nd=row[ch] if ch<len(row) else TrackerNote()
                hn=nd.note>0;hi=nd.instrument>0;hv=nd.volume>0;hf=nd.effect>0;hp=nd.effect_param>0
                if not(hn or hi or hv or hf or hp): pk.append(0x80)
                else:
                    pb=0x80
                    if hn:pb|=1
                    if hi:pb|=2
                    if hv:pb|=4
                    if hf:pb|=8
                    if hp:pb|=16
                    pk.append(pb)
                    if hn:pk.append(nd.note&0xFF)
                    if hi:pk.append(nd.instrument&0xFF)
                    if hv:pk.append(nd.volume&0xFF)
                    if hf:pk.append(nd.effect&0xFF)
                    if hp:pk.append(nd.effect_param&0xFF)
        f.write(struct.pack('<I',9)); f.write(struct.pack('<B',0))
        f.write(struct.pack('<H',len(pat))); f.write(struct.pack('<H',len(pk))); f.write(pk)

    def _wi(self,f,inst):
        samples=inst.get('samples',[])
        if not samples:
            f.write(struct.pack('<I',29))
            f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
            f.write(struct.pack('<BH',0,0)); return
        ihs=263; f.write(struct.pack('<I',ihs))
        f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
        f.write(struct.pack('<B',0)); f.write(struct.pack('<H',len(samples)))
        f.write(struct.pack('<I',40)); f.write(bytearray(96))
        ve=bytearray(48); struct.pack_into('<HH',ve,0,0,64); struct.pack_into('<HH',ve,4,100,64); f.write(ve)
        pe=bytearray(48); struct.pack_into('<HH',pe,0,0,32); struct.pack_into('<HH',pe,4,100,32); f.write(pe)
        f.write(bytes([2,2,0,0,1,0,0,1,1,0,0,0,0,0])); f.write(struct.pack('<H',0x800))
        rem=ihs-(4+22+1+2+4+96+48+48+14+2)
        if rem>0: f.write(b'\x00'*rem)
        for s in samples: self._wsh(f,s)
        for s in samples: self._wsd(f,s)

    def _wsh(self,f,s):
        bits=s.get('bits',16); bps=2 if bits==16 else 1
        f.write(struct.pack('<I',len(s['data'])*bps))
        f.write(struct.pack('<I',s['loop_start']*bps))
        f.write(struct.pack('<I',s['loop_length']*bps))
        f.write(struct.pack('<B',s.get('volume',64)))
        f.write(struct.pack('<b',max(-128,min(127,s.get('finetune',0)))))
        tb=s.get('loop_type',0)&3
        if bits==16: tb|=0x10
        f.write(struct.pack('<B',tb))
        f.write(struct.pack('<B',s.get('panning',128)))
        f.write(struct.pack('<b',max(-128,min(127,s.get('relative_note',0)))))
        f.write(struct.pack('<B',0))
        f.write(s.get('name','').encode('ascii','replace')[:22].ljust(22,b'\x00'))

    def _wsd(self,f,s):
        prev=0
        for v in s['data']:
            v=max(-32768,min(32767,v)); d=((v-prev)+32768)%65536-32768
            f.write(struct.pack('<h',d)); prev=v


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_vgm(input_file, output_file=None,
                transpose=0, octave=0, finetune=0,
                bpm_override=None, speed_override=None,
                compact=1, output_format='xm', midi_file=None):
    base=os.path.splitext(input_file)[0]
    if not output_file: output_file=base+'.xm'

    print(f"Конвертация: {input_file}")
    parser=VGMParser(input_file); h=parser.header
    print(f"  VGM версия: {h.version:#06x}")
    if h.title: print(f"  Название: {h.title}")
    if h.game: print(f"  Игра: {h.game}")
    if h.system: print(f"  Система: {h.system}")
    if h.author: print(f"  Автор: {h.author}")
    dur=parser.get_duration()
    print(f"  Длительность: {dur:.1f} сек")

    chips=[]
    if h.sn76489_clock: chips.append(f"SN76489")
    if h.ym2612_clock: chips.append(f"YM2612")
    if h.ym2151_clock: chips.append(f"YM2151")
    if h.ay8910_clock: chips.append(f"AY-3-8910")
    print(f"  Чипы: {', '.join(chips)}")

    tt=transpose+octave*12
    if tt: print(f"  Transpose: {tt:+d}")
    if compact>1: print(f"  Compact: x{compact}")

    print(f"\n  Парсинг...")
    parser.parse()
    nos=[e for e in parser.events if e.event_type=='note_on']
    uch=sorted(set(e.channel for e in parser.events))
    print(f"  Нот: {len(nos)}, Каналов: {uch}")

    bpm=bpm_override or (max(32,min(255,int(h.rate*2.5))) if h.rate else 150)
    speed=speed_override or 1
    print(f"  XM: BPM={bpm}, Speed={speed}")

    if nos:
        print(f"\n  Первые 15 нот:")
        for e in nos[:15]:
            t=e.sample/VGM_SAMPLE_RATE
            print(f"    t={t:7.3f}s ch={e.channel:>2d} {note_name(e.note):>4s} inst={e.instrument} vol={e.volume}")

    if output_format in ('xm','both'):
        xf=output_file if output_file.endswith('.xm') else base+'.xm'
        w=XMWriter()
        w.build(parser.events,title=h.title or h.game or os.path.basename(base),
                bpm=bpm,speed=speed,transpose=transpose,octave=octave,
                finetune=finetune,compact=compact)
        w.write(xf)
        print(f"\n  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB)")
        print(f"    Паттернов: {len(w.patterns)}, Каналов: {w.num_channels}, Инстр: {len(w.instruments)}")

    if output_format in ('midi','both') or midi_file:
        mf=midi_file or base+'.mid'
        mw=MIDIWriter()
        mw.build(parser.events,bpm=bpm,transpose=transpose,octave=octave,
                 title=h.title or h.game or os.path.basename(base))
        mw.write(mf)
        print(f"\n  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")
        print(f"    Треков: {len(mw.tracks)}")

    print(f"\n  Итого: {len(nos)} нот, {dur:.1f} сек")


def main():
    import argparse
    ap=argparse.ArgumentParser(
        description='VGM/VGZ to XM/MIDI Converter v2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python vgm2xm.py music.vgm
  python vgm2xm.py music.vgz --midi
  python vgm2xm.py music.vgm --midi-only
  python vgm2xm.py music.vgm --octave 1
  python vgm2xm.py music.vgm --compact 4
  python vgm2xm.py *.vgm --midi

Чипы: SN76489, YM2612, AY-3-8910, YM2151
Инструменты XM: синтетические (Square, FM Organ/Brass/Bass/Lead/Strings/Piano, Noise)
        """)
    ap.add_argument('input',nargs='+')
    ap.add_argument('-o','--output')
    ap.add_argument('--octave',type=int,default=0)
    ap.add_argument('--transpose',type=int,default=0)
    ap.add_argument('--finetune',type=int,default=0)
    ap.add_argument('--bpm',type=int,default=None)
    ap.add_argument('--speed',type=int,default=None)
    ap.add_argument('--compact',type=int,default=1)
    ap.add_argument('--midi',action='store_true')
    ap.add_argument('--midi-only',action='store_true')
    ap.add_argument('--midi-file',type=str,default=None)
    args=ap.parse_args()
    if args.output and len(args.input)>1: print("--output: one file"); sys.exit(1)
    if args.compact<1: print("--compact >= 1"); sys.exit(1)
    fmt='midi' if args.midi_only else ('both' if args.midi or args.midi_file else 'xm')
    for inp in args.input:
        if not os.path.exists(inp): print(f"Not found: {inp}"); continue
        try:
            convert_vgm(inp,args.output,transpose=args.transpose,octave=args.octave,
                         finetune=args.finetune,bpm_override=args.bpm,speed_override=args.speed,
                         compact=args.compact,output_format=fmt,midi_file=args.midi_file)
        except Exception as e:
            import traceback; print(f"Error: {e}"); traceback.print_exc()
    print("\nГотово!")

if __name__=='__main__': main()