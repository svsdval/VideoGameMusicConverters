"""
PSG to XM/MIDI/WAV Converter v5
Конвертирует .psg файлы (AY-3-8910 register dump) в .xm, .mid и .wav

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

import numpy as np


NOTE_NAMES = ['C-', 'C#', 'D-', 'D#', 'E-', 'F-',
              'F#', 'G-', 'G#', 'A-', 'A#', 'B-']

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



AY_DAC = [
    0.0,0.0,0.00999465934234,0.00999465934234,
    0.0144502937362,0.0144502937362,0.0210574502174,0.0210574502174,
    0.0307011520562,0.0307011520562,0.0455481803616,0.0455481803616,
    0.0644998855573,0.0644998855573,0.107362478065,0.107362478065,
    0.126588845655,0.126588845655,0.20498970016,0.20498970016,
    0.292210269322,0.292210269322,0.372838941024,0.372838941024,
    0.492530708782,0.492530708782,0.635324635691,0.635324635691,
    0.805584802014,0.805584802014,1.0,1.0]

YM_DAC = [
    0.0,0.0,0.00465400167849,0.00772106507973,
    0.0109559777218,0.0139620050355,0.0169985503929,0.0200198367285,
    0.024368657969,0.029694056611,0.0350652323186,0.0403906309606,
    0.0485389486534,0.0583352407111,0.0680552376593,0.0777752346075,
    0.0925154497597,0.111085679408,0.129747463188,0.148485542077,
    0.17666895552,0.211551079576,0.246387426566,0.281101701381,
    0.333730067903,0.400427252613,0.467383840696,0.53443198291,
    0.635172045472,0.75800717174,0.879926756695,1.0]

def _lfsr_sequence():
    """One full period of the AY noise LFSR (17 bit, taps 0 and 3), output bit = bit 0."""
    n = 1
    out = bytearray(131071)
    for i in range(131071):
        bit = (n ^ (n >> 3)) & 1
        n = (n >> 1) | (bit << 16)
        out[i] = n & 1
    return out

_LFSR = None

def get_lfsr():
    global _LFSR
    if _LFSR is None:
        _LFSR = np.frombuffer(_lfsr_sequence(), dtype=np.uint8)
    return _LFSR

def env_level(shape, s):
    """Envelope level 0..31 after s envelope steps since the shape was written (vectorised)."""
    seg = s // 32
    pos = s % 32
    if not shape & 8:
        up = bool(shape & 4)
        return np.where(seg == 0, pos if up else 31 - pos, 0)
    up = bool(shape & 4)
    alt = bool(shape & 2)
    hold = bool(shape & 1)
    if hold:
        final = 31 if (up != alt) else 0
        first = pos if up else 31 - pos
        return np.where(seg == 0, first, final)
    up_seg = np.logical_xor(up, np.logical_and(alt, (seg & 1) == 1))
    return np.where(up_seg, pos, 31 - pos)


class AYEmulator:
    """AY-3-8910 / YM2149 emulator (numpy, one PT3 frame at a time).

    The chip is stepped at clock/8 (tone and noise dividers are exact), the
    result is averaged over every output sample (box filter).
    """
    # (left, right) gains of channels A, B, C
    PAN = {'abc': ((0.95, 0.27), (0.65, 0.62), (0.62, 1.03)),   # fitted to zxtune123 (ABC)
           'mono': ((0.7, 0.7), (0.7, 0.7), (0.7, 0.7))}

    def __init__(self, clock_rate=1773400, sample_rate=44100, is_ym=False, fps=50.0,
                 layout='abc'):
        get_lfsr()
        self.clock = clock_rate
        self.sr = sample_rate
        self.dac = np.array(YM_DAC if is_ym else AY_DAC)
        self.tick_rate = clock_rate / 8.0
        self.ticks_per_frame = self.tick_rate / fps
        self.samples_per_frame = sample_rate / fps
        self.pan = np.array(self.PAN[layout])        # 3 x 2
        self.tcnt = [0, 0, 0]
        self.tout = [0, 0, 0]
        self.ncnt = 0
        self.nidx = 0
        self.ecnt = 0
        self.estep = 0
        self.eshape = 0
        self.tick_pos = 0.0           # fractional tick position carried between frames
        self.sample_pos = 0.0
        self.regs = [0] * 14
        self.chan_rms = [0.0, 0.0, 0.0]   # AC rms of each channel in the last frame

    def set_registers(self, regs):
        self.regs = list(regs)
        if regs[13] != 0xFF:
            self.eshape = regs[13] & 0x0F
            self.ecnt = 0
            self.estep = 0

    def render_frame(self):
        """Render one frame with the current registers -> (n, 2) float array."""
        r = self.regs
        t0 = self.tick_pos
        t1 = t0 + self.ticks_per_frame
        n = int(t1) - int(t0)
        self.tick_pos = t1
        k = np.arange(1, n + 1, dtype=np.int64)
        mixer = r[7]
        noise_p = max(1, r[6] & 0x1F) * 2
        c = self.ncnt + k
        shifts = c // noise_p
        noise = _LFSR[(self.nidx + shifts) % len(_LFSR)]
        self.nidx = int((self.nidx + (self.ncnt + n) // noise_p) % len(_LFSR))
        self.ncnt = int((self.ncnt + n) % noise_p)
        ep = max(1, (r[12] << 8) | r[11])
        steps = (self.ecnt + k) // ep
        env = env_level(self.eshape, self.estep + steps)
        tot = self.ecnt + n
        self.estep = min(self.estep + tot // ep, 1 << 20)
        self.ecnt = int(tot % ep)
        out = np.zeros((n, 2))
        for i in range(3):
            p = max(1, ((r[i * 2 + 1] & 0x0F) << 8) | r[i * 2])
            cnt = min(self.tcnt[i], p - 1)
            cc = cnt + k
            tone = np.bitwise_xor(self.tout[i], (cc // p) & 1)
            self.tout[i] = int(tone[-1]) if n else self.tout[i]
            self.tcnt[i] = int(cc[-1] % p) if n else cnt
            t_dis = (mixer >> i) & 1
            n_dis = (mixer >> (i + 3)) & 1
            gate = np.logical_or(tone, t_dis) & np.logical_or(noise, n_dis)
            va = r[8 + i]
            if va & 0x10:
                lvl = env
            else:
                lvl = (va & 0x0F) * 2 + 1
            v = self.dac[np.where(gate, lvl, 0)] if va & 0x10 else self.dac[lvl] * gate
            self.chan_rms[i] = float(v.std()) if n else 0.0
            out += v[:, None] * self.pan[i][None, :]
        # box-average ticks into output samples
        s0 = self.sample_pos
        s1 = s0 + self.samples_per_frame
        ns = int(s1) - int(s0)
        self.sample_pos = s1
        cs = np.concatenate(([[0.0, 0.0]], np.cumsum(out, axis=0)))
        edges = np.round(np.arange(ns + 1) * (n / max(1, ns))).astype(np.int64)
        edges = np.clip(edges, 0, n)
        cnts = np.maximum(1, np.diff(edges))[:, None]
        return (cs[edges[1:]] - cs[edges[:-1]]) / cnts



# ═══════════════════════════════════════════════════════════════
# WAV Renderer
# ═══════════════════════════════════════════════════════════════

def render_wav(regs_frames, filename, sample_rate=44100, clock_rate=1773400,
               is_ym=False, stereo=True, volume=0.8, fps=50.0, layout='abc'):
    """Render register frames (iterable of 14 register values per frame) to WAV."""
    ay = AYEmulator(clock_rate, sample_rate, is_ym, fps, layout)
    chunks = []
    print(f"  Rendering WAV: {sample_rate}Hz, {'stereo' if stereo else 'mono'}, "
          f"{'YM' if is_ym else 'AY'} chip, {clock_rate} Hz, {fps:g} fps...")
    for regs in regs_frames:
        ay.set_registers(regs)
        chunks.append(ay.render_frame())
    pcm = np.concatenate(chunks)
    # the DAC is unipolar: volume changes leave slow DC steps. Real players remove
    # them with a high-pass; a zero-phase 1st order 20 Hz filter (in the spectrum) does the same
    spec = np.fft.rfft(pcm, axis=0)
    fr = np.fft.rfftfreq(len(pcm), 1.0 / sample_rate)
    spec *= (fr / np.sqrt(fr * fr + 20.0 ** 2))[:, None]
    pcm = np.fft.irfft(spec, n=len(pcm), axis=0)
    peak = float(np.abs(pcm).max()) or 1.0
    pcm = pcm * (volume / peak)
    if not stereo:
        pcm = pcm.mean(axis=1, keepdims=True)
    data = np.clip(pcm * 32767, -32768, 32767).astype('<i2')
    nch = data.shape[1]
    with open(filename, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + data.nbytes) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 1, nch, sample_rate,
                                       sample_rate * nch * 2, nch * 2, 16))
        f.write(b'data' + struct.pack('<I', data.nbytes))
        f.write(data.tobytes())
    print(f"    Rendered {len(data)} samples ({len(data)/sample_rate:.1f}s)")
    return len(data) / sample_rate

# ═══════════════════════════════════════════════════════════════
# Instruments (waveforms of the AY itself)
# ═══════════════════════════════════════════════════════════════

P_SQ = 0.62          # peak of the square sample (headroom for louder timbres)
FPS = 50             # PT3 player rate (ZX Spectrum interrupt)

def _pcm(vals, peak):
    """float list -> int16 list: DC removed, scaled so max |x| = peak."""
    a = np.array(vals, dtype=float)
    a = a - a.mean()
    m = float(np.abs(a).max()) or 1.0
    return [int(round(x)) for x in a * (peak * 32767.0 / m)]

class AYGen:
    """Samples for XM instruments: one period of what the chip really outputs."""
    @staticmethod
    def build(is_ym=False):
        dac = YM_DAC if is_ym else AY_DAC
        lf = _LFSR if _LFSR is not None else np.frombuffer(_lfsr_sequence(), dtype=np.uint8)
        square = _pcm([1] * 16 + [-1] * 16, P_SQ)
        saw = _pcm([dac[31 - i] for i in range(32)], 0.95)
        tri = _pcm([dac[31 - i] for i in range(32)] + [dac[i] for i in range(32)], 0.95)
        noise = _pcm([int(b) for b in lf[:32768]], P_SQ)
        mixed = _pcm([float(lf[i * 7 % 4000]) for i in range(32)] + [0.0] * 32, 0.95)
        def inst(name, data, rel=0, loop=True):
            rms = float(np.sqrt(np.mean(np.square(data, dtype=float)))) / 32767.0
            return {'name': name, 'data': data, 'rel': rel, 'loop': loop, 'rms': rms}
        return [inst('AY Tone', square), inst('AY Env+Tone', square),
                inst('AY Buzzer saw', saw), inst('AY Tone+Noise', mixed, 12),
                inst('AY Noise', noise), inst('AY Buzzer tri', tri, 12)]

INST_TONE, INST_ENVTONE, INST_BUZZ, INST_MIXED, INST_NOISE, INST_BUZZ_TRI = 1, 2, 3, 4, 5, 6
_INST_CACHE = {}
def instruments(is_ym=False):
    if is_ym not in _INST_CACHE:
        _INST_CACHE[is_ym] = AYGen.build(is_ym)
    return _INST_CACHE[is_ym]

def xm_volume(r, inst, is_ym, scale=1.0):
    """AC rms of a channel (1.0 = full-level square has 0.5) -> XM volume 1..64."""
    rms = instruments(is_ym)[inst - 1]['rms']
    v = 64.0 * r * (2.0 * P_SQ) / rms * scale
    return max(1, min(64, int(round(v))))

# ═══════════════════════════════════════════════════════════════
# Note extraction (raw & AY modes)
# ═══════════════════════════════════════════════════════════════

CLOCK = 1773400
REPEATING_ENV = {0x08, 0x0A, 0x0C, 0x0E}

def clamp_xm(n):
    return n if 1 <= n <= 96 else None

def freq_to_xm(freq):
    if freq < 15 or freq > 20000: return None
    return clamp_xm(int(round(69 + 12 * math.log2(freq / 440.0))) - 11)

def rate_to_xm(rate):
    """Playback rate (samples/s) of a looped 1-sample-per-step wave -> XM note."""
    return max(1, min(96, int(round(49 + 12 * math.log2(max(rate, 1.0) / 8363.0)))))

@dataclass
class NoteEvent:
    frame: int = 0; channel: int = 0; ay_channel: int = 0; mode: str = ""
    event_type: str = ""; note: int = 0; instrument: int = 1; volume: int = 64

def classify(regs, ac, clock):
    """What does AY channel `ac` play this frame?  -> (mode, xm_note, instrument) or None."""
    mixer = regs[7]; vr = regs[8 + ac]
    tone_on = not (mixer >> ac) & 1
    noise_on = not (mixer >> (ac + 3)) & 1
    env_on = bool(vr & 0x10)
    period = ((regs[ac * 2 + 1] & 0x0F) << 8) | regs[ac * 2]
    if tone_on:
        note = freq_to_xm(clock / (16.0 * max(1, period)))
        if note is None: return None
        if env_on: return MODE_ENV_TONE, note, INST_ENVTONE
        if noise_on: return MODE_MIXED, note, INST_MIXED
        return MODE_TONE, note, INST_TONE
    if noise_on:
        np_ = max(1, regs[6] & 0x1F)
        return MODE_NOISE, rate_to_xm(clock / (16.0 * np_)), INST_NOISE
    return None   # buzzer (envelope only) is recognised by the caller, it needs the last shape

def extract_notes_ay(frames, channel_map, clock=CLOCK, is_ym=False, scale=1.0):
    """frames: iterable of (registers, note_started[3]) per 50 Hz frame."""
    emu = AYEmulator(clock, 44100, is_ym)
    nx = max(channel_map.values()) + 1
    cur = [None] * nx                 # per XM channel: dict(note, inst, vol)
    last_xc = [-1] * 3
    events = []; shape = 0
    def gx(a, m):
        if (a, m) in channel_map: return channel_map[(a, m)]
        return a
    frame = -1
    for frame, (regs, started) in enumerate(frames):
        emu.set_registers(regs); emu.render_frame()
        if regs[13] != 0xFF: shape = regs[13] & 0x0F
        ep = max(1, (regs[12] << 8) | regs[11])
        for ac in range(3):
            info = classify(regs, ac, clock)
            vr = regs[8 + ac]
            mixer = regs[7]
            if info is None and (vr & 0x10) and (mixer >> ac) & 1 and (mixer >> (ac + 3)) & 1 \
                    and shape in REPEATING_ENV:
                tri = shape in (0x0A, 0x0E)
                f = clock / ((512.0 if tri else 256.0) * ep)
                n = freq_to_xm(f)
                if n is not None: info = (MODE_BUZZER, n, INST_BUZZ_TRI if tri else INST_BUZZ)
            r = emu.chan_rms[ac]
            xc_old = last_xc[ac]
            if info is None or r < 1e-4:
                if xc_old >= 0 and cur[xc_old] is not None:
                    events.append(NoteEvent(frame, xc_old, ac, '', 'note_off', 97)); cur[xc_old] = None
                last_xc[ac] = -1
                continue
            mode, note, inst = info
            xc = gx(ac, mode)
            if xc_old >= 0 and xc_old != xc and cur[xc_old] is not None:
                events.append(NoteEvent(frame, xc_old, ac, '', 'note_off', 97)); cur[xc_old] = None
            last_xc[ac] = xc
            vol = xm_volume(r, inst, is_ym, scale)
            c = cur[xc]
            trig = (c is None or c['inst'] != inst or started[ac] or
                    (mode in (MODE_BUZZER, MODE_ENV_TONE) and regs[13] != 0xFF))
            if trig:
                events.append(NoteEvent(frame, xc, ac, mode, 'note_on', note, inst, vol))
                cur[xc] = {'note': note, 'inst': inst, 'vol': vol}
            else:
                if note != c['note']:
                    events.append(NoteEvent(frame, xc, ac, mode, 'pitch', note, inst, vol)); c['note'] = note
                if vol != c['vol']:
                    events.append(NoteEvent(frame, xc, ac, mode, 'vol', note, inst, vol)); c['vol'] = vol
    for xc in range(nx):
        if cur[xc] is not None: events.append(NoteEvent(frame + 1, xc, 0, '', 'note_off', 97))
    return events


# ═══════════════════════════════════════════════════════════════
# XM & MIDI writers
# ═══════════════════════════════════════════════════════════════

@dataclass
class TN:
    note: int = 0; instrument: int = 0; volume: int = 0; effect: int = 0; effect_param: int = 0

class XMWriter:
    ROWS_PER_PATTERN = 128

    def build_and_write(self, fn, events, title="", nc=6, is_ym=False, fps=FPS):
        if nc % 2: nc += 1
        nc = max(4, min(32, nc))
        # one row per player frame: 2 ticks of 1/(2*fps) s (3xx needs >= 2 ticks per row)
        speed, bpm = 2, int(round(5 * fps))
        slide_ok = bpm <= 255
        if not slide_ok:
            speed, bpm = 1, max(32, min(255, int(round(2.5 * fps))))
        insts = instruments(is_ym)
        cells = {}
        for ev in events:
            row, ch = ev.frame, ev.channel
            if ch >= nc: continue
            c = cells.setdefault((row, ch), TN())
            if ev.event_type == 'note_on':
                c.note, c.instrument, c.volume = max(1, min(96, ev.note)), ev.instrument, 0x10 + ev.volume
                c.effect = c.effect_param = 0
            elif ev.event_type == 'pitch':
                if c.note != 97 and not (c.note and c.instrument):   # not over a fresh note
                    c.note = max(1, min(96, ev.note))
                    if slide_ok: c.effect, c.effect_param = 3, 0xFF
                    else: c.instrument = ev.instrument      # no ticks for 3xx: re-trigger
                    c.volume = c.volume or 0x10 + ev.volume
            elif ev.event_type == 'vol':
                if c.note != 97 and not (c.note and c.instrument):
                    c.volume = 0x10 + ev.volume
            elif ev.event_type == 'note_off':
                if not c.note: c.note = 97
        rpp = self.ROWS_PER_PATTERN
        max_row = max((k[0] for k in cells), default=0) + 1
        np_ = max(1, min(256, (max_row + rpp - 1) // rpp))
        if (max_row + rpp - 1) // rpp > 256:
            print("  Warning: song is longer than 256 patterns, truncated")
        pats = []
        for pi in range(np_):
            pat = []
            for ri in range(rpp):
                pat.append([cells.get((pi * rpp + ri, ch), TN()) for ch in range(nc)])
            pats.append(pat)
        self._wx(fn, title, nc, bpm, speed, pats, insts, list(range(np_)))

    def _wx(self, fn, title, nc, bpm, speed, pats, insts, order):
        with open(fn, 'wb') as f:
            f.write(b'Extended Module: ')
            f.write(title.encode('ascii', 'replace')[:20].ljust(20, b'\x00'))
            f.write(b'\x1a')
            f.write(b'PT32XM Converter    ')
            f.write(struct.pack('<HIHHHHHHH', 0x0104, 276, len(order), 0, nc, len(pats),
                                min(len(insts), 128), 1, speed) + struct.pack('<H', bpm))
            ot = bytearray(256)
            for i, o in enumerate(order[:256]): ot[i] = o
            f.write(ot)
            for pat in pats:
                pk = bytearray()
                for row in pat:
                    for ch in range(nc):
                        nd = row[ch] if ch < len(row) else TN()
                        hn, hi, hv = nd.note > 0, nd.instrument > 0, nd.volume > 0
                        hf, hp = nd.effect > 0, nd.effect_param > 0
                        if not (hn or hi or hv or hf or hp):
                            pk.append(0x80)
                        else:
                            pk.append(0x80 | (1 if hn else 0) | (2 if hi else 0) | (4 if hv else 0) |
                                      (8 if hf else 0) | (16 if hp else 0))
                            if hn: pk.append(nd.note & 0xFF)
                            if hi: pk.append(nd.instrument & 0xFF)
                            if hv: pk.append(nd.volume & 0xFF)
                            if hf: pk.append(nd.effect & 0xFF)
                            if hp: pk.append(nd.effect_param & 0xFF)
                f.write(struct.pack('<IBH', 9, 0, len(pat)) + struct.pack('<H', len(pk)) + pk)
            for inst in insts[:128]:
                data = inst['data']
                ihs = 263
                f.write(struct.pack('<I', ihs) + inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00'))
                f.write(struct.pack('<BHI', 0, 1, 40) + bytearray(96))
                ve = bytearray(48); struct.pack_into('<HHHH', ve, 0, 0, 64, 100, 64); f.write(ve)
                pe = bytearray(48); struct.pack_into('<HHHH', pe, 0, 0, 32, 100, 32); f.write(pe)
                # envelopes off, fadeout 0: note-off (97) cuts the sound at once
                f.write(bytes([2, 2, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0]))
                f.write(struct.pack('<H', 0))
                f.write(b'\x00' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
                n = len(data)
                f.write(struct.pack('<III', n * 2, 0, n * 2 if inst['loop'] else 0))
                f.write(struct.pack('<BbBBb', 64, 0, (1 if inst['loop'] else 0) | 0x10, 128, inst['rel']))
                f.write(b'\x00')
                f.write(inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00'))
                prev = 0
                for v in data:
                    f.write(struct.pack('<h', ((v - prev + 32768) % 65536) - 32768)); prev = v


class MIDIWriter:
    TPQN = 480
    PROGRAM = {MODE_TONE: 80, MODE_ENV_TONE: 81, MODE_BUZZER: 38, MODE_MIXED: 87, MODE_NOISE: 0}

    @staticmethod
    def _vlq(v):
        v = max(0, int(v)); r = [v & 0x7F]; v >>= 7
        while v: r.append((v & 0x7F) | 0x80); v >>= 7
        r.reverse(); return bytes(r)

    @staticmethod
    def _drum(xm_note):
        return 42 if xm_note >= 75 else 38 if xm_note >= 58 else 36

    def write(self, fn, events, bpm=125, fps=FPS, title=""):
        tpqn = self.TPQN
        tpf = tpqn * bpm / (60.0 * fps)
        usec = int(60_000_000 / bpm)
        by_ch = {}
        for ev in events: by_ch.setdefault(ev.channel, []).append(ev)
        t0 = [(0, bytes([0xFF, 0x51, 0x03, (usec >> 16) & 0xFF, (usec >> 8) & 0xFF, usec & 0xFF]))]
        if title:
            td = title.encode('ascii', errors='replace')[:127]; t0.append((0, bytes([0xFF, 0x01, len(td)]) + td))
        tracks = [t0]
        free = [c for c in range(16) if c != 9]
        for xc in sorted(by_ch):
            evs = by_ch[xc]
            modes = {e.mode for e in evs if e.event_type == 'note_on'}
            drum = modes == {MODE_NOISE}
            if drum: mc = 9
            elif free: mc = free.pop(0)
            else: break
            trk = []
            note = None; prog = None; vel = 0; expr = -99
            def off(t):
                nonlocal note
                if note is not None: trk.append((t, bytes([0x80 | mc, note, 0]))); note = None
            def on(t, n, v):
                nonlocal note
                trk.append((t, bytes([0x90 | mc, n, v]))); note = n
            last_t = 0
            for ev in evs:
                t = int(ev.frame * tpf); last_t = t
                if ev.event_type == 'note_on':
                    off(t)
                    vel = max(1, min(127, ev.volume * 2 - 1))
                    if drum:
                        on(t, self._drum(ev.note), vel)
                    else:
                        p = self.PROGRAM.get(ev.mode, 80)
                        if p != prog: trk.append((t, bytes([0xC0 | mc, p]))); prog = p
                        e = max(1, min(127, ev.volume * 2 - 1))
                        if abs(e - expr) >= 1: trk.append((t, bytes([0xB0 | mc, 11, e]))); expr = e
                        on(t, max(0, min(127, ev.note + 11)), 100)
                elif ev.event_type == 'pitch' and not drum and note is not None:
                    n = max(0, min(127, ev.note + 11))
                    if n != note: off(t); on(t, n, 100)
                elif ev.event_type == 'vol' and not drum:
                    e = max(1, min(127, ev.volume * 2 - 1))
                    if abs(e - expr) >= 4: trk.append((t, bytes([0xB0 | mc, 11, e]))); expr = e
                elif ev.event_type == 'note_off':
                    off(t)
            off(last_t + 1)
            tracks.append(trk)
        with open(fn, 'wb') as f:
            f.write(b'MThd' + struct.pack('>IHHH', 6, 1, len(tracks), tpqn))
            for t in tracks:
                t.sort(key=lambda x: x[0]); d = bytearray(); prev = 0
                for at, ed in t:
                    d.extend(self._vlq(max(0, at - prev))); d.extend(ed); prev = at
                d.extend(self._vlq(0)); d.extend(b'\xFF\x2F\x00')
                f.write(b'MTrk' + struct.pack('>I', len(d)) + d)



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
# PSG Parser
# ═══════════════════════════════════════════════════════════════

@dataclass
class PSGHeader:
    version: int = 0
    frame_rate: int = 50
    clock: int = 1773400
    num_frames: int = 0


class PSGParser:
    """PSG = дамп регистров AY: [рег, знач]* 0xFF (конец кадра), 0xFE n (n*4 пустых кадров), 0xFD (конец)."""

    def __init__(self, filename, clock=1773400):
        with open(filename, 'rb') as f:
            self.raw = f.read()
        self.header = PSGHeader()
        self.header.clock = clock
        self.frames = []        # 14 регистров на кадр; R13 = 0xFF, если в кадре не записывался
        self._parse()

    def _parse(self):
        d = self.raw
        if len(d) < 16:
            raise ValueError("File too small for PSG")
        if d[0:4] != b'PSG\x1A':
            raise ValueError(f"Not a PSG file (signature: {d[0:4]})")
        h = self.header
        h.version = d[4]
        h.frame_rate = d[5] or 50

        pos = 16
        regs = [0] * 14
        wrote13 = False
        pending = False
        n = len(d)
        while pos < n:
            cmd = d[pos]
            pos += 1
            if cmd == 0xFD:
                break
            elif cmd == 0xFF:
                self._emit(regs, wrote13)
                wrote13 = False
                pending = False
            elif cmd == 0xFE:
                if pos >= n:
                    break
                for _ in range(d[pos] * 4):
                    self._emit(regs, False)
                pos += 1
                wrote13 = False
                pending = False
            elif pos < n:
                if cmd <= 0x0D:
                    regs[cmd] = d[pos]
                    wrote13 = wrote13 or cmd == 13
                    pending = True
                pos += 1
        if pending:
            self._emit(regs, wrote13)
        h.num_frames = len(self.frames)

    def _emit(self, regs, wrote13):
        fr = list(regs)
        if not wrote13:
            fr[13] = 0xFF
        self.frames.append(fr)

    def get_duration(self):
        return self.header.num_frames / float(self.header.frame_rate)


def psg_frames(parser):
    """(регистры, note_started[3]) на кадр. В PSG нет событий "новая нота",
    поэтому старт ноты определяем по скачку громкости вверх."""
    prev = [0, 0, 0]
    for regs in parser.frames:
        started = []
        for ac in range(3):
            vr = regs[8 + ac]
            level = 15 if vr & 0x10 else vr & 0x0F
            started.append(level - prev[ac] >= 4)
            prev[ac] = level
        yield regs, started


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def shift_events(events, semitones):
    """--transpose/--octave: сдвиг мелодических нот (шум остаётся на своей шкале)."""
    if not semitones:
        return
    for e in events:
        if e.event_type in ('note_on', 'pitch') and e.mode != MODE_NOISE:
            e.note = max(1, min(96, e.note + semitones))


def convert_psg(input_file, output_file=None, transpose=0, octave=0,
                fmt='xm', midi_file=None, wav_file=None, channel_map=None,
                clock=1773400, sample_rate=44100, is_ym=False,
                wav_volume=0.8, norm_scale=1.0, layout='abc'):
    base = os.path.splitext(input_file)[0]
    if not output_file:
        output_file = base + '.xm'
    channel_map = channel_map or CHANNEL_PRESETS['default']
    num_xm = max(channel_map.values()) + 1

    print(f"Конвертация: {input_file}")
    parser = PSGParser(input_file, clock)
    h = parser.header
    fps = float(h.frame_rate)
    print(f"  PSG v{h.version}, {h.num_frames} кадров, {fps:g} Гц, {parser.get_duration():.1f} сек, "
          f"AY {clock} Гц")
    if not parser.frames:
        print("  Пустой файл!")
        return

    if fmt in ('wav', 'all') or wav_file:
        wf = wav_file or base + '.wav'
        dur = render_wav((f for f in parser.frames), wf, sample_rate, clock, is_ym,
                         stereo=True, volume=wav_volume, fps=fps, layout=layout)
        print(f"  WAV: {wf} ({os.path.getsize(wf)/1024:.1f} KB, {dur:.1f}s)")
        if fmt == 'wav' and not midi_file:
            print("\nГотово!")
            return

    events = extract_notes_ay(psg_frames(parser), channel_map, clock, is_ym, norm_scale)
    shift_events(events, transpose + 12 * octave)
    note_ons = [e for e in events if e.event_type == 'note_on']
    print(f"  Нот: {len(note_ons)}, XM-каналов: {num_xm}")
    if not note_ons:
        print("  Нет нот!")
        return

    if fmt in ('xm', 'both', 'all'):
        xf = output_file if output_file.endswith('.xm') else base + '.xm'
        XMWriter().build_and_write(xf, events, os.path.basename(base), num_xm, is_ym, fps)
        print(f"  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {num_xm} ch)")

    if fmt in ('midi', 'both', 'all') or midi_file:
        mf = midi_file or base + '.mid'
        MIDIWriter().write(mf, events, 125, fps, os.path.basename(base))
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")

    print(f"\n  Итого: {len(note_ons)} нот, {max(e.frame for e in events)/fps:.1f} сек")


def main():
    import argparse

    preset_help = "\n".join(
        f"    {name}: {max(m.values())+1} ch"
        for name, m in CHANNEL_PRESETS.items())

    ap = argparse.ArgumentParser(
        description='PSG to XM/MIDI/WAV Converter v5 (эмулятор AY)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Форматы вывода:
  (по умолчанию) XM
  --midi         XM + MIDI
  --midi-only    только MIDI
  --wav          только WAV (эмуляция AY)
  --all          XM + MIDI + WAV

Примеры:
  python psg2xm_split_channels.py music.psg
  python psg2xm_split_channels.py music.psg --midi --channel-map split-all
  python psg2xm_split_channels.py music.psg --all
  python psg2xm_split_channels.py music.psg --wav --ym --sample-rate 48000
  python psg2xm_split_channels.py music.psg --channel-map "A:tone=0,A:buzz=3,B:tone=1,B:buzz=4,C:tone=2,C:buzz=5"

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

XM: одна строка = один кадр (50 Гц), громкость следует за уровнем канала в каждом кадре,
смена высоты внутри ноты — портаменто 3FF (без повторного запуска ноты).
        """)
    ap.add_argument('input', nargs='*', help='PSG файл(ы)')
    ap.add_argument('-o', '--output', help='Выходной файл')
    ap.add_argument('--octave', type=int, default=0, help='Сдвиг в октавах')
    ap.add_argument('--transpose', type=int, default=0, help='Сдвиг в полутонах')
    ap.add_argument('--clock', type=int, default=1773400,
                    help='Тактовая частота AY, Гц (по умолч. 1773400 = ZX 128; Pentagon: 1750000)')
    ap.add_argument('--midi', action='store_true', help='XM + MIDI')
    ap.add_argument('--midi-only', action='store_true', help='Только MIDI')
    ap.add_argument('--midi-file', type=str, default=None, help='Имя MIDI-файла')
    ap.add_argument('--wav', action='store_true', help='Только WAV')
    ap.add_argument('--wav-file', type=str, default=None, help='Имя WAV-файла')
    ap.add_argument('--all', action='store_true', help='XM + MIDI + WAV')
    ap.add_argument('--sample-rate', type=int, default=44100)
    ap.add_argument('--ym', action='store_true', help='Таблица ЦАП YM2149 (по умолч. AY-3-8910)')
    ap.add_argument('--layout', choices=['abc', 'mono'], default='abc', help='Стерео-раскладка WAV')
    ap.add_argument('--wav-volume', type=float, default=0.8, help='Пиковый уровень WAV 0..1')
    ap.add_argument('--norm-scale', type=float, default=1.0, help='Множитель громкости XM')
    ap.add_argument('--channel-map', type=str, default='default',
                    help='Пресет или пользовательская раскладка каналов')
    ap.add_argument('--list-presets', action='store_true', help='Показать доступные пресеты')
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

    if not args.input:
        ap.print_help()
        sys.exit(1)

    if args.output and len(args.input) > 1:
        print("--output: only for single file")
        sys.exit(1)

    try:
        channel_map = parse_channel_map(args.channel_map)
    except ValueError as e:
        print(f"Error parsing channel map: {e}")
        sys.exit(1)

    if args.all:
        fmt = 'all'
    elif args.wav and not args.midi and not args.midi_only:
        fmt = 'wav'
    elif args.midi_only:
        fmt = 'midi'
    elif args.midi or args.midi_file:
        fmt = 'both'
    else:
        fmt = 'xm'

    for inp in args.input:
        if not os.path.exists(inp):
            print(f"Not found: {inp}")
            continue
        try:
            convert_psg(inp, args.output, args.transpose, args.octave, fmt,
                        args.midi_file, args.wav_file, channel_map, args.clock,
                        args.sample_rate, args.ym, args.wav_volume, args.norm_scale,
                        args.layout)
        except Exception as e:
            import traceback
            print(f"Error: {e}")
            traceback.print_exc()

    print("\nГотово!")


if __name__ == '__main__':
    main()
