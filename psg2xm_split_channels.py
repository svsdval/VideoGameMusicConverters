"""
PSG to XM/MIDI Converter v4
Конвертирует .psg файлы (AY-3-8910 register dump) в .xm и/или .mid

Режимы AY раскладываются по отдельным XM-каналам:
  - tone:    обычный тон
  - noise:   только шум  
  - buzzer:  envelope engine (тон через envelope period)
  - mixed:   тон + шум одновременно

Пользователь может настроить маппинг через --channel-map
"""

import struct
import sys
import os
import math
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple


NOTE_NAMES = ['C-', 'C#', 'D-', 'D#', 'E-', 'F-',
              'F#', 'G-', 'G#', 'A-', 'A#', 'B-']
SAMPLE_PERIOD = 32
REPEATING_ENV_SHAPES = {0x08, 0x0A, 0x0C, 0x0E}

# ── Режимы звука AY ──
MODE_TONE = 'tone'
MODE_NOISE = 'noise'
MODE_BUZZER = 'buzzer'
MODE_MIXED = 'mixed'       # tone + noise
MODE_ENV_TONE = 'env_tone'  # tone + envelope

ALL_MODES = [MODE_TONE, MODE_NOISE, MODE_BUZZER, MODE_MIXED, MODE_ENV_TONE]

# ── Предустановленные раскладки каналов ──
CHANNEL_PRESETS = {
    # Формат: {(ay_channel, mode): xm_channel}
    # Нумерация XM-каналов с 0

    'default': {
        # 6 каналов: A/B/C tone + A/B/C buzzer на отдельных
        (0, MODE_TONE): 0,
        (0, MODE_MIXED): 0,
        (0, MODE_ENV_TONE): 0,
        (0, MODE_BUZZER): 3,
        (0, MODE_NOISE): 6,
        (1, MODE_TONE): 1,
        (1, MODE_MIXED): 1,
        (1, MODE_ENV_TONE): 1,
        (1, MODE_BUZZER): 4,
        (1, MODE_NOISE): 7,
        (2, MODE_TONE): 2,
        (2, MODE_MIXED): 2,
        (2, MODE_ENV_TONE): 2,
        (2, MODE_BUZZER): 5,
        (2, MODE_NOISE): 8,
    },

    'compact': {
        # 4 канала: A/B/C tone, общий buzzer
        (0, MODE_TONE): 0,
        (0, MODE_MIXED): 0,
        (0, MODE_ENV_TONE): 0,
        (0, MODE_BUZZER): 3,
        (0, MODE_NOISE): 0,
        (1, MODE_TONE): 1,
        (1, MODE_MIXED): 1,
        (1, MODE_ENV_TONE): 1,
        (1, MODE_BUZZER): 3,
        (1, MODE_NOISE): 1,
        (2, MODE_TONE): 2,
        (2, MODE_MIXED): 2,
        (2, MODE_ENV_TONE): 2,
        (2, MODE_BUZZER): 3,
        (2, MODE_NOISE): 2,
    },

    'split-all': {
        # 12 каналов: каждый режим на отдельном канале
        (0, MODE_TONE): 0,
        (0, MODE_MIXED): 3,
        (0, MODE_ENV_TONE): 6,
        (0, MODE_BUZZER): 9,
        (0, MODE_NOISE): 12,
        (1, MODE_TONE): 1,
        (1, MODE_MIXED): 4,
        (1, MODE_ENV_TONE): 7,
        (1, MODE_BUZZER): 10,
        (1, MODE_NOISE): 13,
        (2, MODE_TONE): 2,
        (2, MODE_MIXED): 5,
        (2, MODE_ENV_TONE): 8,
        (2, MODE_BUZZER): 11,
        (2, MODE_NOISE): 14,
    },

    'minimal': {
        # 3 канала: всё на своих AY-каналах
        (0, MODE_TONE): 0,
        (0, MODE_MIXED): 0,
        (0, MODE_ENV_TONE): 0,
        (0, MODE_BUZZER): 0,
        (0, MODE_NOISE): 0,
        (1, MODE_TONE): 1,
        (1, MODE_MIXED): 1,
        (1, MODE_ENV_TONE): 1,
        (1, MODE_BUZZER): 1,
        (1, MODE_NOISE): 1,
        (2, MODE_TONE): 2,
        (2, MODE_MIXED): 2,
        (2, MODE_ENV_TONE): 2,
        (2, MODE_BUZZER): 2,
        (2, MODE_NOISE): 2,
    },

    'buzzer-split': {
        # 7 каналов: A/B/C tone, A/B/C buzzer, 1 noise
        (0, MODE_TONE): 0,
        (0, MODE_MIXED): 0,
        (0, MODE_ENV_TONE): 0,
        (0, MODE_BUZZER): 3,
        (0, MODE_NOISE): 6,
        (1, MODE_TONE): 1,
        (1, MODE_MIXED): 1,
        (1, MODE_ENV_TONE): 1,
        (1, MODE_BUZZER): 4,
        (1, MODE_NOISE): 6,
        (2, MODE_TONE): 2,
        (2, MODE_MIXED): 2,
        (2, MODE_ENV_TONE): 2,
        (2, MODE_BUZZER): 5,
        (2, MODE_NOISE): 6,
    },
}

# ── Названия каналов для отладки ──
def get_channel_name(ay_ch, mode):
    ch_name = ['A', 'B', 'C'][ay_ch] if ay_ch < 3 else str(ay_ch)
    mode_names = {
        MODE_TONE: 'Tone',
        MODE_NOISE: 'Noise',
        MODE_BUZZER: 'Buzz',
        MODE_MIXED: 'T+N',
        MODE_ENV_TONE: 'T+E',
    }
    return f"{ch_name}-{mode_names.get(mode, mode)}"


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
    if env_period <= 0:
        return 0
    return clock / (256.0 * env_period)


def freq_to_xm_note(freq):
    if freq <= 0 or freq < 15 or freq > 20000:
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
    channel: int = 0         # XM-канал (после маппинга)
    ay_channel: int = 0      # исходный AY-канал (0-2)
    mode: str = ""           # режим звука
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
# AY Analyzer v4 — с маппингом режимов на XM-каналы
# ═══════════════════════════════════════════════════════════════

# Инструменты по режимам
MODE_INSTRUMENTS = {
    MODE_TONE: 1,      # Square 50%
    MODE_MIXED: 6,     # Noise+Tone
    MODE_ENV_TONE: 4,  # Buzzer
    MODE_BUZZER: 4,    # Buzzer
    MODE_NOISE: 8,     # Noise
}


class AYAnalyzer:
    """
    Анализ AY с разделением режимов на разные XM-каналы.
    
    Каждый AY-канал (A/B/C) может быть в одном из режимов:
      - tone:     обычный тон
      - noise:    только шум
      - buzzer:   envelope engine
      - mixed:    тон + шум
      - env_tone: тон + envelope
    
    channel_map определяет на какой XM-канал попадёт каждая 
    комбинация (ay_channel, mode).
    """

    def __init__(self, frames, clock=1773400, channel_map=None):
        self.frames = frames
        self.clock = clock
        self.channel_map = channel_map or CHANNEL_PRESETS['default']

    def _get_xm_channel(self, ay_ch, mode):
        """Получить XM-канал для данного AY-канала и режима."""
        key = (ay_ch, mode)
        if key in self.channel_map:
            return self.channel_map[key]
        # Фолбэк: ищем ближайший подходящий
        for fallback_mode in [MODE_TONE, MODE_MIXED, MODE_BUZZER]:
            fkey = (ay_ch, fallback_mode)
            if fkey in self.channel_map:
                return self.channel_map[fkey]
        return ay_ch  # крайний фолбэк

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
        Возвращает (note, mode, volume) или (None, None, 0).
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

        # ── BUZZER: tone+noise off, envelope on, repeating shape ──
        if use_envelope and tone_disabled and noise_disabled:
            if env_shape in REPEATING_ENV_SHAPES and env_period > 0:
                freq = ay_envelope_period_to_freq(env_period, self.clock)
                note = freq_to_xm_note(freq)
                if note is not None:
                    return note, MODE_BUZZER, 15
            return None, None, 0

        # ── ENVELOPE + TONE ──
        if use_envelope and not tone_disabled:
            if tone_period > 0:
                freq = ay_period_to_freq(tone_period, self.clock)
                note = freq_to_xm_note(freq)
                if note is not None:
                    mode = MODE_ENV_TONE if noise_disabled else MODE_MIXED
                    return note, mode, 15
            return None, None, 0

        # ── Без envelope ──
        if volume == 0:
            return None, None, 0

        # Тон (возможно + шум)
        if not tone_disabled and tone_period > 0:
            freq = ay_period_to_freq(tone_period, self.clock)
            note = freq_to_xm_note(freq)
            if note is not None:
                if not noise_disabled:
                    return note, MODE_MIXED, volume
                else:
                    return note, MODE_TONE, volume
            return None, None, 0

        # Только шум
        if not noise_disabled:
            note = self._noise_period_to_note(noise_period)
            return note, MODE_NOISE, volume

        return None, None, 0

    def analyze(self):
        events = []

        # Состояние отслеживается по XM-каналам
        # (потому что на один XM-канал может маппиться
        #  несколько комбинаций ay_ch+mode)
        max_xm_ch = max(self.channel_map.values()) + 1

        prev_note = [-1] * max_xm_ch
        prev_vol = [0] * max_xm_ch
        prev_inst = [0] * max_xm_ch
        prev_was_silent = [True] * max_xm_ch

        # Также отслеживаем состояние по AY-каналам
        # чтобы знать когда AY-канал замолчал
        prev_ay_mode = [None, None, None]
        prev_ay_xm = [-1, -1, -1]  # на какой XM-канал был направлен

        for frame_num, regs in enumerate(self.frames):
            if len(regs) < 14:
                continue

            for ay_ch in range(3):
                note, mode, raw_vol = self._detect_channel_state(
                    regs, ay_ch)

                if note is None or mode is None:
                    # ── AY-канал молчит ──
                    # Закрываем ноту на том XM-канале,
                    # куда он был направлен
                    old_xm = prev_ay_xm[ay_ch]
                    if old_xm >= 0 and prev_note[old_xm] >= 0:
                        events.append(NoteEvent(
                            frame=frame_num,
                            channel=old_xm,
                            ay_channel=ay_ch,
                            mode=prev_ay_mode[ay_ch] or '',
                            event_type='note_off', note=97))
                        prev_note[old_xm] = -1
                        prev_vol[old_xm] = 0
                        prev_inst[old_xm] = 0
                        prev_was_silent[old_xm] = True
                    prev_ay_mode[ay_ch] = None
                    prev_ay_xm[ay_ch] = -1
                    continue

                # ── AY-канал звучит ──
                xm_ch = self._get_xm_channel(ay_ch, mode)
                inst = MODE_INSTRUMENTS.get(mode, 1)
                xm_vol = max(1, int(64 * raw_vol / 15))

                # Если AY-канал сменил режим и теперь идёт
                # на другой XM-канал — закрываем старый
                old_xm = prev_ay_xm[ay_ch]
                if old_xm >= 0 and old_xm != xm_ch:
                    if prev_note[old_xm] >= 0:
                        events.append(NoteEvent(
                            frame=frame_num,
                            channel=old_xm,
                            ay_channel=ay_ch,
                            mode=prev_ay_mode[ay_ch] or '',
                            event_type='note_off', note=97))
                        prev_note[old_xm] = -1
                        prev_vol[old_xm] = 0
                        prev_inst[old_xm] = 0
                        prev_was_silent[old_xm] = True

                prev_ay_mode[ay_ch] = mode
                prev_ay_xm[ay_ch] = xm_ch

                # ── Решаем, нужна ли новая нота ──
                need_new_note = False

                if prev_note[xm_ch] < 0:
                    need_new_note = True
                elif note != prev_note[xm_ch]:
                    need_new_note = True
                elif inst != prev_inst[xm_ch]:
                    need_new_note = True
                elif prev_was_silent[xm_ch]:
                    need_new_note = True
                elif (xm_vol > prev_vol[xm_ch] and
                      (xm_vol - prev_vol[xm_ch]) >= 16):
                    need_new_note = True

                if need_new_note:
                    if prev_note[xm_ch] >= 0:
                        events.append(NoteEvent(
                            frame=frame_num,
                            channel=xm_ch,
                            ay_channel=ay_ch,
                            mode=mode,
                            event_type='note_off', note=97))
                    events.append(NoteEvent(
                        frame=frame_num,
                        channel=xm_ch,
                        ay_channel=ay_ch,
                        mode=mode,
                        event_type='note_on', note=note,
                        instrument=inst, volume=xm_vol))
                    prev_note[xm_ch] = note
                    prev_vol[xm_ch] = xm_vol
                    prev_inst[xm_ch] = inst
                else:
                    if xm_vol != prev_vol[xm_ch]:
                        events.append(NoteEvent(
                            frame=frame_num,
                            channel=xm_ch,
                            ay_channel=ay_ch,
                            mode=mode,
                            event_type='vol_change', note=note,
                            instrument=inst, volume=xm_vol))
                        prev_vol[xm_ch] = xm_vol

                prev_was_silent[xm_ch] = False

        # ── Закрываем все ──
        last_frame = len(self.frames) - 1
        for xm_ch in range(max_xm_ch):
            if prev_note[xm_ch] >= 0:
                events.append(NoteEvent(
                    frame=last_frame,
                    channel=xm_ch,
                    event_type='note_off', note=97))

        return events

    def get_num_channels(self):
        """Количество XM-каналов, необходимых для данного маппинга."""
        if not self.channel_map:
            return 4
        return max(self.channel_map.values()) + 1


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
              title="", frame_rate=50.0, num_channels=4):
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

        # GM instruments по типу звука
        gm_by_mode = {
            MODE_TONE: 80,      # Square lead
            MODE_MIXED: 81,     # Saw lead
            MODE_BUZZER: 87,    # Bass lead
            MODE_ENV_TONE: 84,  # Charang
            MODE_NOISE: 119,    # Synth drum (или можно на канал 9)
        }

        for ci, ch in enumerate(sorted(chs.keys())):
            if ci >= 15:
                break
            mc = ci if ci < 9 else ci + 1

            # Определяем основной режим канала
            modes = set()
            for ev in chs[ch]:
                if ev.mode:
                    modes.add(ev.mode)
            primary_mode = modes.pop() if len(modes) == 1 else MODE_TONE
            gm_prog = gm_by_mode.get(primary_mode, 80)

            trk = [(0, bytes([0xC0 | mc, gm_prog]))]

            # Имя трека
            mode_str = '/'.join(sorted(modes)) if modes else '?'
            name = f"XM{ch} ({mode_str})"
            n = name.encode()[:127]
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
              frame_rate=50.0, num_channels=4):
        self.title = title[:20]
        self.bpm = bpm
        self.speed = speed
        # XM поддерживает до 32 каналов, но должно быть чётным
        self.nc = min(32, max(4, num_channels))
        if self.nc % 2 != 0:
            self.nc += 1

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
                ay_channel=ev.ay_channel,
                mode=ev.mode,
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
# Channel Map Parser
# ═══════════════════════════════════════════════════════════════

def parse_channel_map(spec):
    """
    Парсит пользовательскую раскладку каналов.
    
    Формат: "AY_CH:MODE=XM_CH,..."
    
    Примеры:
      "A:tone=0,A:buzzer=3,B:tone=1,B:buzzer=4,C:tone=2,C:buzzer=5"
      "A:tone=0,A:buzz=3"  (сокращения допускаются)
    
    Или имя пресета: "default", "compact", "split-all", "minimal"
    """
    if spec in CHANNEL_PRESETS:
        return CHANNEL_PRESETS[spec]

    mode_aliases = {
        't': MODE_TONE, 'tone': MODE_TONE,
        'n': MODE_NOISE, 'noise': MODE_NOISE,
        'b': MODE_BUZZER, 'buzz': MODE_BUZZER, 'buzzer': MODE_BUZZER,
        'm': MODE_MIXED, 'mix': MODE_MIXED, 'mixed': MODE_MIXED,
        'e': MODE_ENV_TONE, 'env': MODE_ENV_TONE, 'env_tone': MODE_ENV_TONE,
    }
    ch_aliases = {'a': 0, 'b': 1, 'c': 2, '0': 0, '1': 1, '2': 2}

    result = {}
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '=' not in part or ':' not in part:
            raise ValueError(
                f"Bad channel map entry: '{part}'. "
                f"Expected format: AY_CH:MODE=XM_CH")
        left, right = part.split('=', 1)
        ay_str, mode_str = left.split(':', 1)

        ay_ch = ch_aliases.get(ay_str.strip().lower())
        if ay_ch is None:
            raise ValueError(f"Unknown AY channel: '{ay_str}'")

        mode = mode_aliases.get(mode_str.strip().lower())
        if mode is None:
            raise ValueError(
                f"Unknown mode: '{mode_str}'. "
                f"Available: {list(mode_aliases.keys())}")

        xm_ch = int(right.strip())
        result[(ay_ch, mode)] = xm_ch

    # Заполняем отсутствующие маппинги фолбэками
    for ay_ch in range(3):
        for mode in ALL_MODES:
            if (ay_ch, mode) not in result:
                # Ищем фолбэк: tone → тот же AY канал
                if (ay_ch, MODE_TONE) in result:
                    result[(ay_ch, mode)] = result[(ay_ch, MODE_TONE)]
                else:
                    result[(ay_ch, mode)] = ay_ch

    return result


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_psg(input_file, output_file=None, transpose=0,
                octave=0, finetune=0, bpm_override=None,
                speed_override=None, compact=1,
                output_format='xm', midi_file=None,
                clock=1773400, channel_map=None):

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

    if channel_map is None:
        channel_map = CHANNEL_PRESETS['default']

    # Показываем маппинг каналов
    print(f"  Маппинг каналов:")
    used_xm = set()
    for (ay_ch, mode), xm_ch in sorted(channel_map.items()):
        name = get_channel_name(ay_ch, mode)
        print(f"    {name:>8s} → XM ch {xm_ch}")
        used_xm.add(xm_ch)
    num_xm = max(used_xm) + 1 if used_xm else 4
    print(f"  XM каналов: {num_xm}")

    # ── Отладочный вывод ──
    print(f"  Первые 12 фреймов:")
    for i in range(min(12, len(psg.frames))):
        regs = psg.frames[i]
        r_str = ' '.join(f'{r:02X}' for r in regs)
        env_per = regs[11] | (regs[12] << 8)
        env_shape = regs[13]
        mixer = regs[7]

        print(f"    [{i:4d}] {r_str}  "
              f"env={env_per} sh={env_shape:02X}")

        for ch in range(3):
            per = regs[ch*2] | ((regs[ch*2+1] & 0x0F) << 8)
            vol_reg = regs[8 + ch]
            vol = vol_reg & 0x0F
            use_env = bool(vol_reg & 0x10)
            tone_off = bool(mixer & (1 << ch))
            noise_off = bool(mixer & (1 << (ch + 3)))
            freq = ay_period_to_freq(per, h.clock) if per > 0 else 0

            t = "." if tone_off else "T"
            n = "." if noise_off else "N"
            e = "E" if use_env else " "

            extra = ""
            if use_env and tone_off and noise_off:
                if env_shape in REPEATING_ENV_SHAPES and env_per > 0:
                    ef = ay_envelope_period_to_freq(env_per, h.clock)
                    en = freq_to_xm_note(ef)
                    extra = f" →BUZZER {ef:.1f}Hz"
                    if en:
                        extra += f" ({note_name(en)})"
                else:
                    extra = " →ENV-silent"

            ch_name = ['A', 'B', 'C'][ch]
            print(f"           {ch_name}:{t}{n} per={per:4d} "
                  f"({freq:7.1f}Hz) v={vol:2d}{e}{extra}")

    tt = transpose + octave * 12
    if tt:
        print(f"  Transpose: {tt:+d}")

    print(f"\n  Анализ AY регистров...")
    analyzer = AYAnalyzer(psg.frames, h.clock, channel_map)
    events = analyzer.analyze()

    note_ons = [e for e in events if e.event_type == 'note_on']
    note_offs = [e for e in events if e.event_type == 'note_off']
    vol_changes = [e for e in events if e.event_type == 'vol_change']
    print(f"  Нот: {len(note_ons)}, Note-off: {len(note_offs)}, "
          f"Vol changes: {len(vol_changes)}")

    # Статистика по XM-каналам
    xm_ch_stats = {}
    for e in note_ons:
        key = e.channel
        if key not in xm_ch_stats:
            xm_ch_stats[key] = {'notes': 0, 'min': 999, 'max': 0,
                                'modes': set()}
        xm_ch_stats[key]['notes'] += 1
        xm_ch_stats[key]['min'] = min(xm_ch_stats[key]['min'], e.note)
        xm_ch_stats[key]['max'] = max(xm_ch_stats[key]['max'], e.note)
        if e.mode:
            xm_ch_stats[key]['modes'].add(e.mode)

    for xm_ch in sorted(xm_ch_stats.keys()):
        s = xm_ch_stats[xm_ch]
        modes = '/'.join(sorted(s['modes']))
        print(f"    XM ch {xm_ch}: {s['notes']} нот, "
              f"{note_name(s['min'])}-{note_name(s['max'])}, "
              f"modes: {modes}")

    if not note_ons:
        print("  Нет нот!")
        return

    bpm = bpm_override or max(32, min(255,
                                       int(h.frame_rate * 2.5)))
    speed = speed_override or 1
    print(f"  XM: BPM={bpm}, Speed={speed}")

    print(f"  Первые 25 нот:")
    for e in note_ons[:25]:
        t = e.frame / h.frame_rate
        mode_str = e.mode or '?'
        print(f"    t={t:7.3f}s xm={e.channel} ay={e.ay_channel} "
              f"{mode_str:>7s} {note_name(e.note):>4s} "
              f"i={e.instrument} v={e.volume}")

    if output_format in ('xm', 'both'):
        xf = (output_file if output_file.endswith('.xm')
              else base + '.xm')
        w = XMWriter()
        w.build(events,
                title=os.path.basename(base),
                bpm=bpm, speed=speed, transpose=transpose,
                octave=octave, finetune=finetune,
                compact=compact, frame_rate=h.frame_rate,
                num_channels=num_xm)
        w.write(xf)
        print(f"\n  XM: {xf} "
              f"({os.path.getsize(xf) / 1024:.1f} KB, "
              f"{len(w.patterns)} pat, {w.nc} ch)")

    if output_format in ('midi', 'both') or midi_file:
        mf = midi_file or (base + '.mid')
        mw = MIDIWriter()
        mw.build(events, bpm=bpm, transpose=transpose,
                 octave=octave,
                 title=os.path.basename(base),
                 frame_rate=h.frame_rate,
                 num_channels=num_xm)
        mw.write(mf)
        print(f"  MIDI: {mf} "
              f"({os.path.getsize(mf) / 1024:.1f} KB, "
              f"{len(mw.tracks)} треков)")

    print(f"\n  Итого: {len(note_ons)} нот, "
          f"{psg.get_duration():.1f} сек")


def main():
    import argparse

    preset_help = "\n".join(
        f"    {name}: {max(m.values())+1} ch"
        for name, m in CHANNEL_PRESETS.items())

    ap = argparse.ArgumentParser(
        description='PSG to XM/MIDI Converter v4',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Примеры:
  python psg2xm.py music.psg
  python psg2xm.py music.psg --midi --channel-map default
  python psg2xm.py music.psg --channel-map compact
  python psg2xm.py music.psg --channel-map split-all
  python psg2xm.py music.psg --channel-map "A:tone=0,A:buzz=3,B:tone=1,B:buzz=4,C:tone=2,C:buzz=5"

Пресеты раскладок (--channel-map):
{preset_help}

Пользовательский формат: "AY_CH:MODE=XM_CH,..."
  AY_CH: A, B, C
  MODE:  tone, noise, buzzer (buzz), mixed (mix), env_tone (env)
  XM_CH: номер XM-канала (с 0)

Режимы AY:
  tone     — обычный тон (tone period)
  noise    — только шум
  mixed    — тон + шум одновременно
  buzzer   — envelope engine (тон через envelope period)
  env_tone — тон + envelope (громкость из envelope)
        """)
    ap.add_argument('input', nargs='+', help='PSG файл(ы)')
    ap.add_argument('-o', '--output', help='Выходной файл')
    ap.add_argument('--octave', type=int, default=0,
                    help='Сдвиг октав (-2..+2)')
    ap.add_argument('--transpose', type=int, default=0,
                    help='Транспонирование полутонов')
    ap.add_argument('--finetune', type=int, default=0,
                    help='Finetune (-128..127)')
    ap.add_argument('--bpm', type=int, default=None,
                    help='BPM (по умолчанию авто)')
    ap.add_argument('--speed', type=int, default=None,
                    help='Speed (по умолчанию 1)')
    ap.add_argument('--compact', type=int, default=1,
                    help='Компактность (1=нормально, 2=сжать)')
    ap.add_argument('--clock', type=int, default=1773400,
                    help='AY clock в Hz')
    ap.add_argument('--midi', action='store_true',
                    help='Генерировать и MIDI')
    ap.add_argument('--midi-only', action='store_true',
                    help='Генерировать только MIDI')
    ap.add_argument('--midi-file', type=str, default=None,
                    help='Путь к MIDI файлу')
    ap.add_argument('--channel-map', type=str, default='default',
                    help='Раскладка каналов (пресет или формула)')
    ap.add_argument('--list-presets', action='store_true',
                    help='Показать доступные пресеты')
    args = ap.parse_args()

    if args.list_presets:
        print("Доступные пресеты раскладок:\n")
        for name, mapping in CHANNEL_PRESETS.items():
            num_ch = max(mapping.values()) + 1
            print(f"  {name} ({num_ch} XM каналов):")
            for (ay_ch, mode), xm_ch in sorted(mapping.items()):
                cname = get_channel_name(ay_ch, mode)
                print(f"    {cname:>10s} → XM {xm_ch}")
            print()
        sys.exit(0)

    if args.output and len(args.input) > 1:
        print("--output: only for single file")
        sys.exit(1)

    try:
        channel_map = parse_channel_map(args.channel_map)
    except ValueError as e:
        print(f"Error parsing channel map: {e}")
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
                        clock=args.clock,
                        channel_map=channel_map)
        except Exception as e:
            import traceback
            print(f"Error: {e}")
            traceback.print_exc()

    print("\nГотово!")


if __name__ == '__main__':
    main()