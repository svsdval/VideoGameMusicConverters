"""
PSG to XM/MIDI Converter v3 (with buzzer/envelope engine support)
"""

import struct
import sys
import os
import math
from dataclasses import dataclass
from typing import Optional, List


NOTE_NAMES = ['C-', 'C#', 'D-', 'D#', 'E-', 'F-',
              'F#', 'G-', 'G#', 'A-', 'A#', 'B-']
SAMPLE_PERIOD = 32


def note_name(n):
    if n < 1 or n > 96:
        return "---"
    v = n - 1
    return f"{NOTE_NAMES[v % 12]}{v // 12}"


def xm_note_to_midi(xm_note):
    return xm_note + 11


def ay_period_to_freq(period, clock):
    if period <= 0:
        return 0
    return clock / (16.0 * period)


def ay_envelope_period_to_freq(env_period, clock):
    """Частота envelope (для buzzer engine).
    
    Envelope period определяет частоту повторения огибающей.
    Для shapes 0x08, 0x0A, 0x0C, 0x0E (повторяющиеся) —
    это создаёт слышимый тон.
    freq = clock / (256 * env_period)
    """
    if env_period <= 0:
        return 0
    return clock / (256.0 * env_period)


# Повторяющиеся envelope shapes, которые создают слышимый тон
REPEATING_ENV_SHAPES = {0x08, 0x0A, 0x0C, 0x0E}


def freq_to_xm_note(freq):
    if freq <= 0:
        return None
    if freq < 15 or freq > 20000:
        return None
    midi = 69 + 12 * math.log2(freq / 440.0)
    xm = int(round(midi)) - 11
    return xm if 1 <= xm <= 96 else None


@dataclass
class PSGHeader:
    version: int = 0
    frame_rate: int = 50
    clock: int = 1773400
    num_frames: int = 0


@dataclass
class TrackerNote:
    note: int = 0
    instrument: int = 0
    volume: int = 0
    effect: int = 0
    effect_param: int = 0


@dataclass
class NoteEvent:
    frame: int = 0
    channel: int = 0
    event_type: str = ""
    note: int = 0
    instrument: int = 0
    volume: int = 64


# ═══════════════════════════════════════════════════════════════
# PSG Parser
# ═══════════════════════════════════════════════════════════════

class PSGParser:
    def __init__(self, filename, clock=1773400):
        with open(filename, 'rb') as f:
            self.raw = f.read()
        self.header = PSGHeader()
        self.header.clock = clock
        self.frames = []
        self._parse()

    def _parse(self):
        d = self.raw
        if len(d) < 16:
            raise ValueError("File too small for PSG")
        if d[0:4] != b'PSG\x1A':
            raise ValueError(f"Not a PSG file (signature: {d[0:4]})")

        h = self.header
        h.version = d[4]
        h.frame_rate = d[5]
        if h.frame_rate == 0:
            h.frame_rate = 50

        pos = 16
        regs = [0] * 14
        current_frame_has_data = False

        while pos < len(d):
            cmd = d[pos]
            pos += 1

            if cmd == 0xFD:
                if current_frame_has_data:
                    self.frames.append(tuple(regs[:]))
                break
            elif cmd == 0xFF:
                self.frames.append(tuple(regs[:]))
                current_frame_has_data = False
            elif cmd == 0xFE:
                if current_frame_has_data:
                    self.frames.append(tuple(regs[:]))
                for _ in range(4):
                    self.frames.append(tuple(regs[:]))
                current_frame_has_data = False
            elif cmd <= 0x0D:
                if pos < len(d):
                    val = d[pos]
                    pos += 1
                    regs[cmd] = val
                    current_frame_has_data = True

        if current_frame_has_data:
            self.frames.append(tuple(regs[:]))

        h.num_frames = len(self.frames)

    def get_duration(self):
        if self.header.frame_rate > 0:
            return self.header.num_frames / self.header.frame_rate
        return 0


# ═══════════════════════════════════════════════════════════════
# AY Register Analyzer v3
# ═══════════════════════════════════════════════════════════════

class AYAnalyzer:
    """
    Анализ AY-3-8910 с поддержкой:
    
    1. Обычный тон (tone enabled, mixer bit=0)
    2. Шумовой канал (noise enabled, tone disabled)  
    3. Тон + шум
    4. **Buzzer/Envelope engine** — tone и noise выключены,
       но envelope включён (R8/R9/R10 bit 4).
       Звук генерируется через envelope period (R11:R12)
       с повторяющимся envelope shape (R13 = 08,0A,0C,0E).
       Частота: clock / (256 * env_period)
    5. Retriggering (та же нота после паузы)
    6. Volume changes
    """

    def __init__(self, frames, clock=1773400):
        self.frames = frames
        self.clock = clock

    def _noise_period_to_note(self, noise_period):
        if noise_period <= 4:
            return 72
        elif noise_period <= 8:
            return 66
        elif noise_period <= 14:
            return 60
        elif noise_period <= 21:
            return 54
        else:
            return 48

    def _detect_channel_state(self, regs, ch):
        """
        Определяет состояние канала.
        
        Возвращает (note, instrument, volume) или (None, 0, 0) если тишина.
        
        Порядок приоритетов:
        1. Envelope buzzer (tone+noise off, envelope on, repeating shape)
        2. Тон (tone on)
        3. Тон + шум
        4. Только шум
        """
        mixer = regs[7]
        vol_reg = regs[8 + ch]
        use_envelope = bool(vol_reg & 0x10)
        volume = vol_reg & 0x0F
        
        tone_disabled = bool(mixer & (1 << ch))
        noise_disabled = bool(mixer & (1 << (ch + 3)))
        
        period_lo = regs[ch * 2]
        period_hi = regs[ch * 2 + 1] & 0x0F
        tone_period = period_lo | (period_hi << 8)
        
        env_period = regs[11] | (regs[12] << 8)
        env_shape = regs[13] & 0x0F
        noise_period = regs[6] & 0x1F
        
        # ══════════════════════════════════════════════
        # Случай 1: BUZZER / ENVELOPE ENGINE
        # ══════════════════════════════════════════════
        # Tone и Noise выключены, но envelope включён
        # с повторяющимся shape → звук через envelope
        if use_envelope and tone_disabled and noise_disabled:
            if env_shape in REPEATING_ENV_SHAPES and env_period > 0:
                freq = ay_envelope_period_to_freq(env_period, self.clock)
                note = freq_to_xm_note(freq)
                if note is not None:
                    # Громкость buzzer = максимальная (envelope сам
                    # модулирует, но средняя ~8 из 15)
                    return note, 4, 15  # inst 4 = buzzer
            # Envelope включён но shape не повторяющийся
            # или период 0 — тишина
            return None, 0, 0
        
        # ══════════════════════════════════════════════
        # Случай 2: ENVELOPE + TONE
        # ══════════════════════════════════════════════
        # Tone включён, envelope включён — частота из tone period,
        # громкость из envelope (считаем максимальной)
        if use_envelope and not tone_disabled:
            if tone_period > 0:
                freq = ay_period_to_freq(tone_period, self.clock)
                note = freq_to_xm_note(freq)
                if note is not None:
                    inst = 6 if not noise_disabled else 4
                    return note, inst, 15
            return None, 0, 0
        
        # ══════════════════════════════════════════════
        # Без envelope — обычная обработка
        # ══════════════════════════════════════════════
        if volume == 0:
            return None, 0, 0
        
        # Случай 3: Тон включён (возможно + шум)
        if not tone_disabled and tone_period > 0:
            freq = ay_period_to_freq(tone_period, self.clock)
            note = freq_to_xm_note(freq)
            if note is not None:
                if not noise_disabled:
                    inst = 6  # tone + noise
                else:
                    inst = 1  # чистый тон
                return note, inst, volume
            return None, 0, 0
        
        # Случай 4: Только шум
        if not noise_disabled:
            note = self._noise_period_to_note(noise_period)
            return note, 8, volume
        
        # Всё выключено
        return None, 0, 0

    def analyze(self):
        events = []
        
        prev_note = [-1, -1, -1]
        prev_vol = [0, 0, 0]
        prev_inst = [0, 0, 0]
        prev_was_silent = [True, True, True]

        for frame_num, regs in enumerate(self.frames):
            if len(regs) < 14:
                continue

            for ch in range(3):
                note, inst, raw_vol = self._detect_channel_state(regs, ch)
                
                if note is None:
                    # ── Канал молчит ──
                    if prev_note[ch] >= 0:
                        events.append(NoteEvent(
                            frame=frame_num, channel=ch,
                            event_type='note_off', note=97))
                        prev_note[ch] = -1
                        prev_vol[ch] = 0
                        prev_inst[ch] = 0
                    prev_was_silent[ch] = True
                    continue

                # ── Канал звучит ──
                xm_vol = max(1, int(64 * raw_vol / 15))
                
                need_new_note = False
                
                if prev_note[ch] < 0:
                    need_new_note = True
                elif note != prev_note[ch]:
                    need_new_note = True
                elif inst != prev_inst[ch]:
                    need_new_note = True
                elif prev_was_silent[ch]:
                    need_new_note = True
                elif (xm_vol > prev_vol[ch] and 
                      (xm_vol - prev_vol[ch]) >= 16):
                    need_new_note = True
                
                if need_new_note:
                    if prev_note[ch] >= 0:
                        events.append(NoteEvent(
                            frame=frame_num, channel=ch,
                            event_type='note_off', note=97))
                    events.append(NoteEvent(
                        frame=frame_num, channel=ch,
                        event_type='note_on', note=note,
                        instrument=inst, volume=xm_vol))
                    prev_note[ch] = note
                    prev_vol[ch] = xm_vol
                    prev_inst[ch] = inst
                else:
                    if xm_vol != prev_vol[ch]:
                        events.append(NoteEvent(
                            frame=frame_num, channel=ch,
                            event_type='vol_change', note=note,
                            instrument=inst, volume=xm_vol))
                        prev_vol[ch] = xm_vol
                
                prev_was_silent[ch] = False

        last_frame = len(self.frames) - 1
        for ch in range(3):
            if prev_note[ch] >= 0:
                events.append(NoteEvent(
                    frame=last_frame, channel=ch,
                    event_type='note_off', note=97))

        return events


# ═══════════════════════════════════════════════════════════════
# Samples
# ═══════════════════════════════════════════════════════════════

class AYSampleGenerator:
    P = SAMPLE_PERIOD

    @classmethod
    def square_50(cls):
        p = cls.P
        return [int(max(-1, min(1, sum(
            math.sin(2 * math.pi * h * i / p) / h
            for h in range(1, 20, 2)) * 1.2)) * 24000)
            for i in range(p)]

    @classmethod
    def square_25(cls):
        p = cls.P
        return [int(max(-1, min(1, sum(
            (2.0 / (h * math.pi)) * math.sin(math.pi * h * 0.25)
            * math.sin(2 * math.pi * h * i / p)
            for h in range(1, 20)) * 1.3)) * 24000)
            for i in range(p)]

    @classmethod
    def lead(cls):
        p = cls.P
        return [int(max(-1, min(1, sum(
            math.sin(2 * math.pi * i / p * h) * (1.0 / h ** 0.8)
            for h in range(1, 12)) * 0.5)) * 24000)
            for i in range(p)]

    @classmethod
    def buzzer(cls):
        p = cls.P * 2
        return [int(max(-1, min(1,
            math.sin(2 * math.pi * i / p) * 0.5 +
            math.sin(6 * math.pi * i / p) * 0.3 +
            math.sin(10 * math.pi * i / p) * 0.2)) * 24000)
            for i in range(p)]

    @classmethod
    def bass(cls):
        p = cls.P * 2
        return [int(max(-1, min(1,
            math.sin(2 * math.pi * i / p) * 0.7 +
            math.sin(4 * math.pi * i / p) * 0.2 +
            math.sin(math.pi * i / p) * 0.3)) * 24000)
            for i in range(p)]

    @classmethod
    def noise_tone(cls):
        import random
        random.seed(42)
        p = cls.P
        return [int((math.sin(2 * math.pi * i / p) * 0.5 +
                      random.gauss(0, 0.3)) * 24000)
            for i in range(p)]

    @classmethod
    def triangle(cls):
        p = cls.P
        st = list(range(16)) + list(range(15, -1, -1))
        return [int((st[int(i / p * 32) % 32] - 7.5) / 7.5 * 24000)
            for i in range(p)]

    @staticmethod
    def noise(length=4096):
        s = []
        lfsr = 1
        for _ in range(length):
            bit = ((lfsr >> 0) ^ (lfsr >> 1)) & 1
            lfsr = (lfsr >> 1) | (bit << 14)
            s.append(24000 if lfsr & 1 else -24000)
        return s


# ═══════════════════════════════════════════════════════════════
# MIDI Writer
# ═══════════════════════════════════════════════════════════════

class MIDIWriter:
    def __init__(self):
        self.tracks = []
        self.tpqn = 480
        self.bpm = 120

    def build(self, events, bpm=120, transpose=0, octave=0,
              title="", frame_rate=50.0):
        ts = transpose + octave * 12
        self.bpm = bpm
        bps = bpm / 60.0
        spt = 1.0 / (bps * self.tpqn)
        f2t = (1.0 / frame_rate) / spt

        chs = {}
        for ev in events:
            chs.setdefault(ev.channel, []).append(ev)

        self.tracks = []
        usec = int(60_000_000 / bpm)
        t0 = [(0, bytes([0xFF, 0x51, 0x03,
                         (usec >> 16) & 0xFF,
                         (usec >> 8) & 0xFF,
                         usec & 0xFF]))]
        if title:
            td = title.encode('ascii', errors='replace')[:127]
            t0.append((0, bytes([0xFF, 0x01, len(td)]) + td))
        self.tracks.append(t0)

        cn = ['Channel A', 'Channel B', 'Channel C']
        gm = [80, 81, 74]

        for ci, ch in enumerate(sorted(chs.keys())):
            if ci >= 15:
                break
            mc = ci if ci < 9 else ci + 1
            trk = [(0, bytes([0xC0 | mc, gm[ci % 3]]))]
            n = (cn[ch] if ch < 3 else f"Ch{ch}").encode()[:127]
            trk.append((0, bytes([0xFF, 0x03, len(n)]) + n))

            act = {}
            for ev in chs[ch]:
                mt = int(ev.frame * f2t)

                if ev.event_type == 'note_on':
                    mn = max(0, min(127,
                                    xm_note_to_midi(ev.note) + ts))
                    vel = max(1, min(127, ev.volume * 2))
                    if ch in act:
                        pn, _ = act[ch]
                        trk.append((mt, bytes([0x80 | mc, pn, 0])))
                    trk.append((mt, bytes([0x90 | mc, mn, vel])))
                    act[ch] = (mn, mt)

                elif ev.event_type == 'note_off':
                    if ch in act:
                        pn, _ = act[ch]
                        trk.append((mt, bytes([0x80 | mc, pn, 0])))
                        del act[ch]

                elif ev.event_type == 'vol_change':
                    midi_vol = max(0, min(127, ev.volume * 2))
                    trk.append((mt, bytes([0xB0 | mc, 11, midi_vol])))

            for pn, pt in act.values():
                trk.append((pt + self.tpqn,
                            bytes([0x80 | mc, pn, 0])))
            self.tracks.append(trk)

    def write(self, fn):
        with open(fn, 'wb') as f:
            f.write(b'MThd')
            f.write(struct.pack('>I', 6))
            f.write(struct.pack('>HHH', 1,
                                len(self.tracks), self.tpqn))
            for t in self.tracks:
                t.sort(key=lambda x: x[0])
                d = bytearray()
                prev = 0
                for at, ed in t:
                    d.extend(self._vlq(max(0, at - prev)))
                    d.extend(ed)
                    prev = at
                d.extend(self._vlq(0))
                d.extend(b'\xFF\x2F\x00')
                f.write(b'MTrk')
                f.write(struct.pack('>I', len(d)))
                f.write(d)

    @staticmethod
    def _vlq(v):
        if v < 0:
            v = 0
        r = [v & 0x7F]
        v >>= 7
        while v:
            r.append((v & 0x7F) | 0x80)
            v >>= 7
        r.reverse()
        return bytes(r)


# ═══════════════════════════════════════════════════════════════
# XM Writer
# ═══════════════════════════════════════════════════════════════

class XMWriter:
    def __init__(self):
        self.title = ""
        self.nc = 4
        self.bpm = 150
        self.speed = 1
        self.patterns = []
        self.instruments = []
        self.order = []

    def build(self, events, title="", bpm=150, speed=1,
              transpose=0, octave=0, finetune=0, compact=1,
              frame_rate=50.0):
        self.title = title[:20]
        self.bpm = bpm
        self.speed = speed
        self.nc = 4
        tt = transpose + octave * 12
        self._build_inst(tt, finetune)

        row_seconds = speed * 2.5 / bpm
        frames_per_row = max(1, int(frame_rate * row_seconds))

        row_events = []
        for ev in events:
            row = ev.frame // frames_per_row
            if compact > 1:
                row = row // compact
            row_events.append(NoteEvent(
                frame=row, channel=ev.channel,
                event_type=ev.event_type, note=ev.note,
                instrument=ev.instrument, volume=ev.volume))

        if compact > 1:
            self.bpm = max(32, bpm // compact)

        self._build_pat(row_events)

    def _build_inst(self, transpose=0, finetune=0):
        gen = AYSampleGenerator
        rel = max(-128, min(127, transpose))
        ft = max(-128, min(127, finetune))

        def mk(name, pcm, loop=True, periods=1):
            oshift = (int(round(12 * math.log2(periods)))
                      if periods > 1 else 0)
            ar = max(-128, min(127, rel + oshift))
            return {
                'name': name[:22],
                'samples': [{
                    'name': name[:22], 'data': pcm,
                    'length': len(pcm),
                    'loop_start': 0,
                    'loop_length': len(pcm) if loop else 0,
                    'loop_type': 1 if loop else 0,
                    'volume': 64, 'finetune': ft,
                    'panning': 128,
                    'relative_note': ar, 'bits': 16
                }]
            }

        self.instruments = [
            mk("AY Square 50%", gen.square_50()),     # 1
            mk("AY Square 25%", gen.square_25()),     # 2
            mk("AY Lead", gen.lead()),                # 3
            mk("AY Buzzer", gen.buzzer(), periods=2), # 4
            mk("AY Bass", gen.bass(), periods=2),     # 5
            mk("AY Noise+Tone", gen.noise_tone()),    # 6
            mk("AY Triangle", gen.triangle()),        # 7
            mk("AY Noise", gen.noise(), loop=False),  # 8
        ]

    def _build_pat(self, events):
        rpp = 64
        if not events:
            self.patterns = [
                [[TrackerNote() for _ in range(self.nc)]
                 for _ in range(rpp)]]
            self.order = [0]
            return

        mx = max(e.frame for e in events)
        np_ = max(1, min((mx + rpp) // rpp, 256))
        self.patterns = [
            [[TrackerNote() for _ in range(self.nc)]
             for _ in range(rpp)]
            for _ in range(np_)]

        seen = {}
        for ev in events:
            ch = ev.channel
            if ch >= self.nc:
                continue
            pi = ev.frame // rpp
            ri = ev.frame % rpp
            if pi >= len(self.patterns):
                continue

            nd = self.patterns[pi][ri][ch]
            key = (ev.frame, ch)

            if ev.event_type == 'note_on':
                nd.note = max(1, min(96, ev.note))
                nd.instrument = min(ev.instrument,
                                    len(self.instruments))
                nd.volume = 0x10 + min(0x40, max(0, ev.volume))
                seen[key] = 'on'

            elif ev.event_type == 'note_off':
                if key not in seen or seen[key] != 'on':
                    if nd.note == 0:
                        nd.note = 97

            elif ev.event_type == 'vol_change':
                if nd.note == 0 and nd.volume == 0:
                    nd.volume = 0x10 + min(0x40, max(0, ev.volume))

        self.order = list(range(np_))

    def write(self, fn):
        with open(fn, 'wb') as f:
            f.write(b'Extended Module: ')
            f.write(self.title.encode('ascii', 'replace')[:20]
                    .ljust(20, b'\x00'))
            f.write(b'\x1a')
            f.write(b'PSG2XM Converter    ')
            f.write(struct.pack('<H', 0x0104))
            f.write(struct.pack('<I', 276))
            f.write(struct.pack('<H', len(self.order)))
            f.write(struct.pack('<H', 0))
            f.write(struct.pack('<H', self.nc))
            f.write(struct.pack('<H', len(self.patterns)))
            f.write(struct.pack('<H',
                                min(len(self.instruments), 128)))
            f.write(struct.pack('<H', 1))
            f.write(struct.pack('<H', self.speed))
            f.write(struct.pack('<H', self.bpm))
            ot = bytearray(256)
            for i, o in enumerate(self.order[:256]):
                ot[i] = o
            f.write(ot)

            for pat in self.patterns:
                pk = bytearray()
                for row in pat:
                    for ch in range(self.nc):
                        nd = (row[ch] if ch < len(row)
                              else TrackerNote())
                        hn = nd.note > 0
                        hi = nd.instrument > 0
                        hv = nd.volume > 0
                        hf = nd.effect > 0
                        hp = nd.effect_param > 0
                        if not (hn or hi or hv or hf or hp):
                            pk.append(0x80)
                        else:
                            pb = 0x80
                            if hn: pb |= 1
                            if hi: pb |= 2
                            if hv: pb |= 4
                            if hf: pb |= 8
                            if hp: pb |= 16
                            pk.append(pb)
                            if hn: pk.append(nd.note & 0xFF)
                            if hi: pk.append(nd.instrument & 0xFF)
                            if hv: pk.append(nd.volume & 0xFF)
                            if hf: pk.append(nd.effect & 0xFF)
                            if hp: pk.append(nd.effect_param & 0xFF)

                f.write(struct.pack('<I', 9))
                f.write(struct.pack('<B', 0))
                f.write(struct.pack('<H', len(pat)))
                f.write(struct.pack('<H', len(pk)))
                f.write(pk)

            for inst in self.instruments[:128]:
                samples = inst.get('samples', [])
                if not samples:
                    f.write(struct.pack('<I', 29))
                    f.write(inst['name']
                            .encode('ascii', 'replace')[:22]
                            .ljust(22, b'\x00'))
                    f.write(struct.pack('<BH', 0, 0))
                    continue

                ihs = 263
                f.write(struct.pack('<I', ihs))
                f.write(inst['name']
                        .encode('ascii', 'replace')[:22]
                        .ljust(22, b'\x00'))
                f.write(struct.pack('<B', 0))
                f.write(struct.pack('<H', len(samples)))
                f.write(struct.pack('<I', 40))
                f.write(bytearray(96))

                ve = bytearray(48)
                struct.pack_into('<HH', ve, 0, 0, 64)
                struct.pack_into('<HH', ve, 4, 100, 64)
                f.write(ve)

                pe = bytearray(48)
                struct.pack_into('<HH', pe, 0, 0, 32)
                struct.pack_into('<HH', pe, 4, 100, 32)
                f.write(pe)

                f.write(bytes([2, 2, 0, 0, 1, 0, 0,
                               1, 1, 0, 0, 0, 0, 0]))
                f.write(struct.pack('<H', 0x800))

                rem = ihs - (4+22+1+2+4+96+48+48+14+2)
                if rem > 0:
                    f.write(b'\x00' * rem)

                for s in samples:
                    bits = s.get('bits', 16)
                    bps = 2 if bits == 16 else 1
                    f.write(struct.pack('<I',
                                        len(s['data']) * bps))
                    f.write(struct.pack('<I',
                                        s['loop_start'] * bps))
                    f.write(struct.pack('<I',
                                        s['loop_length'] * bps))
                    f.write(struct.pack('<B',
                                        s.get('volume', 64)))
                    f.write(struct.pack('<b',
                        max(-128, min(127,
                                      s.get('finetune', 0)))))
                    tb = s.get('loop_type', 0) & 3
                    if bits == 16:
                        tb |= 0x10
                    f.write(struct.pack('<B', tb))
                    f.write(struct.pack('<B',
                                        s.get('panning', 128)))
                    f.write(struct.pack('<b',
                        max(-128, min(127,
                                      s.get('relative_note',
                                            0)))))
                    f.write(struct.pack('<B', 0))
                    f.write(s.get('name', '')
                            .encode('ascii', 'replace')[:22]
                            .ljust(22, b'\x00'))

                for s in samples:
                    prev = 0
                    for v in s['data']:
                        v = max(-32768, min(32767, v))
                        d_ = (((v - prev) + 32768) % 65536
                              - 32768)
                        f.write(struct.pack('<h', d_))
                        prev = v


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_psg(input_file, output_file=None, transpose=0,
                octave=0, finetune=0, bpm_override=None,
                speed_override=None, compact=1,
                output_format='xm', midi_file=None,
                clock=1773400):

    base = os.path.splitext(input_file)[0]
    if not output_file:
        output_file = base + '.xm'

    print(f"Конвертация: {input_file}")
    psg = PSGParser(input_file, clock=clock)
    h = psg.header

    print(f"  Frame rate: {h.frame_rate} fps")
    print(f"  Clock: {h.clock} Hz")
    print(f"  Фреймов: {h.num_frames}")
    print(f"  Длительность: {psg.get_duration():.1f} сек")

    # ── Отладочный вывод первых фреймов ──
    print(f"  Первые 15 фреймов (R0..R13):")
    for i in range(min(15, len(psg.frames))):
        regs = psg.frames[i]
        r_str = ' '.join(f'{r:02X}' for r in regs)
        per_a = regs[0] | ((regs[1] & 0x0F) << 8)
        per_b = regs[2] | ((regs[3] & 0x0F) << 8)
        per_c = regs[4] | ((regs[5] & 0x0F) << 8)
        mixer = regs[7]
        vol_a_raw = regs[8]
        vol_b_raw = regs[9]
        vol_c_raw = regs[10]
        env_per = regs[11] | (regs[12] << 8)
        env_shape = regs[13]
        
        def ch_info(name, period, vol_raw, tone_bit, noise_bit):
            vol = vol_raw & 0x0F
            env = "E" if vol_raw & 0x10 else " "
            t = "T" if not (mixer & tone_bit) else "."
            n = "N" if not (mixer & noise_bit) else "."
            freq = ay_period_to_freq(period, h.clock) if period > 0 else 0
            
            # Определяем тип звука
            tone_off = bool(mixer & tone_bit)
            noise_off = bool(mixer & noise_bit)
            use_env = bool(vol_raw & 0x10)
            
            sound_type = ""
            if use_env and tone_off and noise_off:
                if env_shape in REPEATING_ENV_SHAPES and env_per > 0:
                    env_freq = ay_envelope_period_to_freq(env_per, h.clock)
                    env_note = freq_to_xm_note(env_freq)
                    sound_type = f" BUZZER={env_freq:.1f}Hz"
                    if env_note:
                        sound_type += f"({note_name(env_note)})"
                else:
                    sound_type = " ENV-silent"
            
            return (f"{name}:{t}{n} per={period:4d} "
                    f"({freq:7.1f}Hz) vol={vol:2d}{env}"
                    f"{sound_type}")
        
        print(f"    [{i:4d}] {r_str}  env_per={env_per} shape={env_shape:02X}")
        a_info = ch_info("A", per_a, vol_a_raw, 1, 8)
        b_info = ch_info("B", per_b, vol_b_raw, 2, 16)
        c_info = ch_info("C", per_c, vol_c_raw, 4, 32)
        print(f"           {a_info}")
        print(f"           {b_info}")
        print(f"           {c_info}")

    tt = transpose + octave * 12
    if tt:
        print(f"  Transpose: {tt:+d}")
    if compact > 1:
        print(f"  Compact: x{compact}")

    print(f"\n  Анализ AY регистров...")
    analyzer = AYAnalyzer(psg.frames, h.clock)
    events = analyzer.analyze()

    note_ons = [e for e in events if e.event_type == 'note_on']
    note_offs = [e for e in events if e.event_type == 'note_off']
    vol_changes = [e for e in events if e.event_type == 'vol_change']
    print(f"  Нот: {len(note_ons)}, Note-off: {len(note_offs)}, "
          f"Vol changes: {len(vol_changes)}")

    # Статистика по каналам и инструментам
    for ch in range(3):
        ch_notes = [e for e in note_ons if e.channel == ch]
        ch_name = ['A', 'B', 'C'][ch]
        if ch_notes:
            notes_set = set(e.note for e in ch_notes)
            insts = {}
            for e in ch_notes:
                insts[e.instrument] = insts.get(e.instrument, 0) + 1
            inst_str = ", ".join(f"i{k}={v}" for k, v in sorted(insts.items()))
            print(f"    Ch {ch_name}: {len(ch_notes)} нот, "
                  f"диапазон {note_name(min(notes_set))}-"
                  f"{note_name(max(notes_set))}, "
                  f"инструменты: {inst_str}")

    if not note_ons:
        print("  Нет нот!")
        return

    bpm = bpm_override or max(32, min(255,
                                       int(h.frame_rate * 2.5)))
    speed = speed_override or 1
    print(f"  XM: BPM={bpm}, Speed={speed}")

    cn = ['A', 'B', 'C']
    print(f"  Первые 25 нот:")
    for e in note_ons[:25]:
        c = cn[e.channel] if e.channel < 3 else str(e.channel)
        t = e.frame / h.frame_rate
        print(f"    t={t:7.3f}s ch={c} "
              f"{note_name(e.note):>4s} "
              f"inst={e.instrument} vol={e.volume}")

    if output_format in ('xm', 'both'):
        xf = (output_file if output_file.endswith('.xm')
              else base + '.xm')
        w = XMWriter()
        w.build(events,
                title=os.path.basename(base),
                bpm=bpm, speed=speed, transpose=transpose,
                octave=octave, finetune=finetune,
                compact=compact, frame_rate=h.frame_rate)
        w.write(xf)
        print(f"\n  XM: {xf} "
              f"({os.path.getsize(xf) / 1024:.1f} KB, "
              f"{len(w.patterns)} pat)")

    if output_format in ('midi', 'both') or midi_file:
        mf = midi_file or (base + '.mid')
        mw = MIDIWriter()
        mw.build(events, bpm=bpm, transpose=transpose,
                 octave=octave,
                 title=os.path.basename(base),
                 frame_rate=h.frame_rate)
        mw.write(mf)
        print(f"  MIDI: {mf} "
              f"({os.path.getsize(mf) / 1024:.1f} KB, "
              f"{len(mw.tracks)} треков)")

    print(f"\n  Итого: {len(note_ons)} нот, "
          f"{psg.get_duration():.1f} сек")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='PSG to XM/MIDI Converter v3 (buzzer support)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python psg2xm.py music.psg
  python psg2xm.py music.psg --midi
  python psg2xm.py music.psg --midi-only
  python psg2xm.py music.psg --octave 1 --compact 2
  python psg2xm.py *.psg --midi

Поддерживаемые режимы AY:
  - Обычный тон (tone period)
  - Шум (noise)
  - Тон + шум
  - Buzzer engine (envelope как генератор тона)
        """)
    ap.add_argument('input', nargs='+', help='PSG файл(ы)')
    ap.add_argument('-o', '--output', help='Выходной файл')
    ap.add_argument('--octave', type=int, default=0)
    ap.add_argument('--transpose', type=int, default=0)
    ap.add_argument('--finetune', type=int, default=0)
    ap.add_argument('--bpm', type=int, default=None)
    ap.add_argument('--speed', type=int, default=None)
    ap.add_argument('--compact', type=int, default=1)
    ap.add_argument('--clock', type=int, default=1773400)
    ap.add_argument('--midi', action='store_true')
    ap.add_argument('--midi-only', action='store_true')
    ap.add_argument('--midi-file', type=str, default=None)
    args = ap.parse_args()

    if args.output and len(args.input) > 1:
        print("--output: only for single file")
        sys.exit(1)

    fmt = ('midi' if args.midi_only
           else ('both' if args.midi or args.midi_file
                 else 'xm'))

    for inp in args.input:
        if not os.path.exists(inp):
            print(f"Not found: {inp}")
            continue
        try:
            convert_psg(inp, args.output,
                        transpose=args.transpose,
                        octave=args.octave,
                        finetune=args.finetune,
                        bpm_override=args.bpm,
                        speed_override=args.speed,
                        compact=args.compact,
                        output_format=fmt,
                        midi_file=args.midi_file,
                        clock=args.clock)
        except Exception as e:
            import traceback
            print(f"Error: {e}")
            traceback.print_exc()

    print("\nГотово!")


if __name__ == '__main__':
    main()