#!/usr/bin/env python3
"""
NSF (NES / Famicom) -> XM / MIDI / WAV

Тот же принцип, что у vgm2xm-midi-wav.py и spc2xm-midi-wav.py: звук берётся не "на глаз",
а из эмуляции чипа (встроенный на чистом Python + numpy, nsf_pyemu.py, ничего собирать не нужно):

  1. NSF исполняется на эмуляторе 6502 + APU 2A03: INIT, потом PLAY 60 (50) раз в секунду.
     Журнал APU учитывает всё, что слышно: счётчики длины, огибающую (затухание), sweep,
     линейный счётчик треугольника, перезапуск ноты записью в $4003/$4007/$400B/$400F.
  2. Журнал превращается в ноты: pulse (по скважности), треугольник, шум (обычный и
     "металлический" режим), DMC и PCM через $4011. Высота, громкость и перезапуск - из лога.
  3. Инструменты XM строятся из самих форм сигнала (Фурье-синтез импульса/треугольника с
     числом гармоник под высоту - по сэмплу на октаву; шум - настоящая последовательность
     LFSR; DMC - распакованный поток). Громкости в XM - в тех же единицах, что в WAV.
  4. Строка XM = кадр PLAY (или N кадров, --compact). Темп подгоняется под реальную длину
     кадра (60.0988 Гц), ноты раскладываются по каналам.
  5. WAV - рендер того же журнала (линейный микшер, как у libgme; сверено с ним).

Использование:
    python3 nsf2xm-midi-wav.py game.nsf                  # стартовый трек -> game.xm
    python3 nsf2xm-midi-wav.py game.nsf --track 5 --all  # XM + MIDI + WAV трека 5
    python3 nsf2xm-midi-wav.py game.nsf --all-tracks --midi
    python3 nsf2xm-midi-wav.py game.nsf --samples 1,3    # только эти инструменты
Расширения звука (VRC6, VRC7, FDS, MMC5, N163, Sunsoft 5B) не поддерживаются: звучит только 2A03.
"""

import argparse
import bisect
import collections
import math
import os
import struct
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nsf_pyemu as emu                     # noqa: E402

SR = 44100
XM_C4_RATE = 8363.0
MAX_XM_CHANNELS = 32
MAX_XM_INSTRUMENTS = 128
MERGE_GAP = 200                  # тактов: записи одного канала ближе друг к другу - одно событие
DUTY_FRAC = [0.125, 0.25, 0.5, 0.75]
DUTY_NAMES = ['12.5%', '25%', '50%', '75%']
DRUM_NOTES = [36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56]
# Нота XM с громкостью 64 и сэмплом на всю шкалу даёт у libopenmpt/OpenMPT rms ~0.18 от амплитуды сэмпла
# (запас сведения плеера). Так громкость нот XM привязана к WAV: амплитуда <= этой даёт точно
# такой же уровень, что в WAV; громче (ударные DMC у libgme упираются в клиппинг) - мягко прижимается.
XM_REF_AMP = 0.18


# ═══════════════════════════════════════════════════════════════
# Ноты из журнала APU
# ═══════════════════════════════════════════════════════════════

class Note:
    def __init__(self, kind, ch, t0, patch):
        self.kind = kind             # 'pulse' | 'tri' | 'noise' | 'dmc' | 'dac'
        self.ch = ch                 # канал APU (0..4)
        self.t0 = t0
        self.t1 = t0
        self.patch = patch           # pulse: скважность 0..3; noise: режим 0/1; dmc/dac: ключ сэмпла
        self.cur = []                # [(t, частота Гц, амплитуда)]
        self.data = None             # dmc/dac: (сэмпл, частота опорная)
        self.inst = 0
        self.key = None
        self.start_row = self.end_row = 0
        self.channel = 0


def amp_of(kind, level, patch):
    """Амплитуда пика (доля полной шкалы WAV) для уровня 0..15."""
    if kind == 'pulse':
        d = DUTY_FRAC[patch]
        return level * emu.MIX_PULSE * max(d, 1.0 - d)
    if kind == 'tri':
        return 15 * emu.MIX_TRI * 0.5
    return level * emu.MIX_NOISE * 0.5


def merge_events(ev):
    """Склеивает пачку записей драйвера (ближе MERGE_GAP тактов) в одно событие."""
    out = []
    last_c = None
    for e in ev:
        if out and e[0] - last_c <= MERGE_GAP:
            first = out[-1][0]
            trig = out[-1][1] or e[1]
            out[-1] = (first, trig) + tuple(e[2:])
        else:
            out.append(tuple(e))
        last_c = e[0]
    return out


def channel_notes(tr, ch):
    kind = ('pulse', 'pulse', 'tri', 'noise')[ch]
    clock = tr.clock
    notes = []
    cur = None
    for (c, trig, a, b, lv) in merge_events(tr.ev[ch]):
        t = max(0.0, (c - tr.start) / clock)
        if kind == 'pulse':
            f, patch = clock / (16.0 * (a + 1)), b
        elif kind == 'tri':
            f, patch = clock / (32.0 * (a + 1)), 0
        else:
            f, patch = clock / tr.noise_tab[a], b
        audible = lv > 0 and (kind == 'noise' or 20.0 <= f <= 20000.0)
        if cur is not None:
            if not audible:
                cur.t1 = t
                cur = None
                continue
            if not trig and patch == cur.patch:
                cur.cur.append((t, f, amp_of(kind, lv, patch)))
                continue
            cur.t1 = t
            cur = None
        if audible:
            cur = Note(kind, ch, t, patch)
            cur.cur.append((t, f, amp_of(kind, lv, patch)))
            notes.append(cur)
    t_end = (tr.end - tr.start) / clock
    if cur is not None:
        cur.t1 = t_end
    return [n for n in notes if n.t1 > n.t0 or len(n.cur) > 0]


def dmc_notes(tr):
    """Воспроизведение сэмплов DMC. -> ноты kind='dmc'."""
    clock = tr.clock
    out = []
    evs = tr.dmc_events
    for i, e in enumerate(evs):
        if e[0] != 'start':
            continue
        c0, info = e[1], e[2]
        data = info['data']
        if not data:
            continue
        per = info['period']
        nxt = [x[1] for x in evs[i + 1:] if x[0] in ('stop', 'start')]
        c1 = min(nxt + [tr.end])
        natural = c0 + len(data) * 8 * per
        if not info['loop']:
            c1 = min(c1, natural)
        t0 = max(0.0, (c0 - tr.start) / clock)
        t1 = max(t0 + 0.001, (c1 - tr.start) / clock)
        n = Note('dmc', 4, t0, (hash(data) & 0xFFFFFFFF, info['level'], bool(info['loop'])))
        n.t1 = t1
        rate = clock / float(per)
        n.cur.append((t0, rate, 0.0))
        n.data = (data, info['level'], bool(info['loop']), rate)
        out.append(n)
    return out


def dac_notes(tr):
    """PCM через $4011: серии быстрых записей (драйвер пишет отсчёты сам)."""
    w = tr.dac_writes
    clock = tr.clock
    out = []
    if len(w) < 24:
        return out
    gap = int(clock * 0.004)
    segs, cur = [], [w[0]]
    for a, b in zip(w, w[1:]):
        if b[0] - a[0] > gap:
            segs.append(cur)
            cur = []
        cur.append(b)
    segs.append(cur)
    for sg in segs:
        if len(sg) < 24:
            continue
        ts = np.array([x[0] for x in sg], dtype=np.float64)
        dt = float(np.median(np.diff(ts)))
        vals = bytes(x[1] for x in sg)
        if dt <= 0 or len(set(vals)) < 3 or (ts[-1] - ts[0]) < clock * 0.01:
            continue
        t0 = max(0.0, (ts[0] - tr.start) / clock)
        # ключ - по признакам, а не по байтам: шумовые ударные на $4011 каждый раз из случайных значений
        arr = np.frombuffer(vals, dtype=np.uint8).astype(np.float64)
        ac = arr - arr.mean()
        acf = np.correlate(ac, ac, 'full')[len(ac) - 1:]
        lag = 0
        if acf[0] > 0 and len(ac) > 32:
            acf = acf / acf[0]
            j = int(np.argmax(acf[8:len(ac) // 2])) + 8
            if acf[j] > 0.7:
                lag = j                 # периодический PCM (тон), а не шум
        feat = (int(round(math.log2(len(vals)) * 3)), int(round(math.log2(clock / dt) * 6)),
                int(round(arr.std() / 3.0)), lag)
        n = Note('dac', 4, t0, (hash(feat) & 0xFFFFFFFF,))
        n.t1 = (ts[-1] + dt - tr.start) / clock
        rate = clock / dt
        n.cur.append((t0, rate, 0.0))
        n.data = (vals, rate)
        out.append(n)
    return out


# ═══════════════════════════════════════════════════════════════
# Сэмплы
# ═══════════════════════════════════════════════════════════════

def stepwave(vals, L):
    """Ступенчатая волна (M шагов за период), ограниченная по полосе до L/2 гармоник,
    нулевое среднее. -> массив L, пик 1.0"""
    v = np.asarray(vals, dtype=np.float64)
    M = len(v)
    K = L // 2
    k = np.arange(1, K)
    m = np.arange(M)
    e0 = np.exp(-2j * np.pi * np.outer(k, m) / M)
    e1 = np.exp(-2j * np.pi * np.outer(k, m + 1) / M)
    c = ((e0 - e1) @ v) / (2j * np.pi * k)
    spec = np.zeros(K + 1, dtype=np.complex128)
    spec[1:K] = c * np.sinc(k / float(K)) * L
    x = np.fft.irfft(spec, L)
    pk = float(np.abs(x).max()) or 1.0
    return x / pk


def pow2_at_least(x, lo=16, hi=2048):
    p = lo
    while p < x and p < hi:
        p *= 2
    return p


def hp_sample(x, rate, loop):
    """Тот же ФВЧ, что в WAV (убирает постоянную составляющую), для сэмплов DMC/PCM."""
    x = np.asarray(x, dtype=np.float64)
    if loop:
        y = emu._highpass(np.tile(x, 3), rate)
        return y[len(x):2 * len(x)]
    return emu._highpass(np.concatenate((x, np.zeros(int(rate * 0.02)))), rate)[:len(x)]


def noise_sample(mode):
    seq = emu._lfsr_seq(mode)
    mean = float(seq.mean())
    x = seq - mean
    pk = float(np.abs(x).max()) or 1.0
    return x / pk, pk


# ═══════════════════════════════════════════════════════════════
# Темп, каналы, XM, MIDI (как в vgm2xm-midi-wav.py)
# ═══════════════════════════════════════════════════════════════

def tempo_schedule(bpm, speed, true_delta, nrows, block=16, rate=48000):
    """BPM в XM целое, а длина кадра - нет. Ноты стоят на сетке true_delta (ритм точный),
    а BPM по ходу меняется на близкие значения (Fxx), чтобы время строк не уходило от настоящего.
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
            best = cur
        if cur is None:
            start = best
        elif best != cur:
            sched[r] = best
        cur = best
        t_xm += n * row_dur(best)
    return start, sched


def interval_to_bpm_speed(delta, min_speed=1):
    best = None
    for speed in range(min_speed, 13):
        bpm = int(round(2.5 * speed / delta))
        if not 32 <= bpm <= 255:
            continue
        real = 2.5 * speed / bpm
        err = abs(real - delta) / delta
        score = (round(err, 4), abs(speed - 6))
        if best is None or score < best[0]:
            best = (score, bpm, speed, real)
    if best is None:
        speed = max(min_speed, 1 if delta < 0.05 else 12)
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
        busy_until[c] = n.end_row
        last_inst[c] = n.inst
        started[c] = n.start_row
        owner[c] = n
    return len(busy_until)


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
            f.write(b'NSF2XM Converter    ')
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
        # огибающие выключены: тогда "note off" (97) обрывает звук сразу
        f.write(bytes([2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
        f.write(struct.pack('<H', 0))
        f.write(b'\x00' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
        for s in smps:
            data = s['data']
            ls, ll = s['loop'] if s['loop'] else (0, 0)
            f.write(struct.pack('<III', len(data) * 2, ls * 2, ll * 2))
            f.write(struct.pack('<BbBBb', 64, s['finetune'], (1 if s['loop'] else 0) | 0x10, 128, s['rel']))
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
# Инструменты
# ═══════════════════════════════════════════════════════════════

def soft_limit(x, knee):
    """Относительная громкость (1.0 = громкость 64) -> 0..1: до knee линейно, выше плавно к 1.0."""
    if x <= knee or knee >= 1.0:
        return min(x, 1.0)
    w = 1.0 - knee
    return knee + w * math.tanh((x - knee) / w)


def note_of(freq, off):
    """Вещественный номер ноты XM (49 = C-4) по частоте."""
    return 69.0 + 12.0 * math.log2(freq / 440.0) - off - 11.0


def freq_of(n, off):
    return 440.0 * 2.0 ** ((n + 11.0 - 69.0 + off) / 12.0)


class Zone:
    def __init__(self, lo, hi, n0):
        self.lo, self.hi, self.n0 = lo, hi, n0
        self.data = None
        self.loop = None
        self.L = 1                       # отсчётов на период (тональные)


class Instrument:
    def __init__(self, kind, key):
        self.kind = kind
        self.key = key
        self.name = ''
        self.zones = []
        self.notes = []
        self.fold = 0
        self.index = 0
        self.rate0 = 1.0                 # dmc/dac: частота отсчётов сэмпла
        self.peak = 1.0                  # амплитуда пика сэмпла (для dmc/dac/noise)
        self.mean_f = 0.0
        self.centroid = 0.0

    def zone_for(self, n):
        for z in self.zones:
            if z.lo <= n <= z.hi:
                return z
        return min(self.zones, key=lambda z: min(abs(n - z.lo), abs(n - z.hi)))


def key_of(n):
    if n.kind == 'pulse':
        return ('pulse', n.patch)
    if n.kind == 'tri':
        return ('tri',)
    if n.kind == 'noise':
        return ('noise', n.patch)
    return (n.kind,) + tuple(n.patch)


def parse_sample_list(text):
    out = set()
    for part in text.replace(' ', '').split(','):
        if not part:
            continue
        a, _, b = part.partition('-')
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            raise SystemExit(f"--samples: не разобрать '{part}'")
        out.update(range(min(lo, hi), max(lo, hi) + 1))
    return out


def write_wav(path, y, gain=1.0):
    x = np.clip(np.round(y * 32767.0 * gain), -32768, 32767).astype('<i2')
    st = np.repeat(x[:, None], 2, axis=1)
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(st.tobytes())
    print(f"[wav] {path}")


# ═══════════════════════════════════════════════════════════════
# Конвертация одного трека
# ═══════════════════════════════════════════════════════════════

def convert_track(args, nsf, song, base, title):
    want_xm, want_midi, want_wav = args.want_xm, args.want_midi, args.want_wav
    sel = parse_sample_list(args.samples) if args.samples else None

    player = emu.NSFPlayer(nsf, song)
    clock = player.clock
    print(f"[nsf] трек {song + 1}/{nsf.total_songs}: эмуляция до {args.seconds:.0f} с "
          f"({'PAL' if nsf.pal else 'NTSC'}, PLAY каждые {player.cpp / clock * 1000:.2f} мс) ...")

    def prog(x):
        print(f"\r[nsf] {x * 100:3.0f}%", end='', file=sys.stderr, flush=True)
    tr = player.run(args.seconds, args.stop_silence, prog)
    print(file=sys.stderr)
    if tr.timeouts or tr.bad_ops:
        print(f"[nsf] ВНИМАНИЕ: PLAY не уложился в бюджет {tr.timeouts} раз, "
              f"неизвестных опкодов {tr.bad_ops} (код 0x{(player.bad or 0):02X})", file=sys.stderr)
    t_sound = (tr.sound_end - tr.start) / clock
    if tr.sound_end <= tr.start:
        print("[nsf] тишина: APU ничего не играет")
        return
    t_end = min((tr.end - tr.start) / clock, t_sound + args.tail)
    tr.end = min(tr.end, tr.start + int(t_end * clock))
    print(f"[nsf] кадров: {tr.frames}, звучит до {t_sound:.1f} с")

    # ── ноты ──
    notes = []
    for ch in range(4):
        notes += channel_notes(tr, ch)
    notes += dmc_notes(tr) + dac_notes(tr)
    notes = [n for n in notes if n.t0 < t_end]
    for n in notes:
        n.t1 = min(max(n.t1, n.t0 + 0.001), t_end)
    if not notes:
        print("[nsf] нот не найдено")
        return
    cnt = collections.Counter(n.kind for n in notes)
    print(f"[nsf] нот: pulse {cnt['pulse']}, треугольник {cnt['tri']}, шум {cnt['noise']}, "
          f"DMC {cnt['dmc']}, PCM {cnt['dac']}")

    # ── инструменты (по порядку первого появления) ──
    first = {}
    for n in notes:
        n.key = key_of(n)
        first.setdefault(n.key, n.t0)
    order = sorted(first, key=lambda k: first[k])
    by_key = collections.defaultdict(list)
    for n in notes:
        by_key[n.key].append(n)
    insts = []
    for k in order:
        ins = Instrument(k[0], k)
        ins.notes = by_key[k]
        insts.append(ins)
    for i, ins in enumerate(insts):
        ins.index = i + 1
    print("[nsf] инструменты (номер: тип, нот):")
    for ins in insts:
        if ins.kind == 'pulse':
            nm = f"Pulse {DUTY_NAMES[ins.key[1]]}"
        elif ins.kind == 'tri':
            nm = "Triangle"
        elif ins.kind == 'noise':
            nm = "Noise" + (" (short)" if ins.key[1] else "")
        elif ins.kind == 'dmc':
            nm = f"DMC {ins.key[1]:08x}"
        else:
            nm = f"PCM {ins.key[1]:08x}"
        ins.name = nm
        print(f"       {ins.index:3d}: {nm:18s} {len(ins.notes):5d}"
              f"{'   <-' if sel and ins.index in sel else ''}")
    if sel:
        keep = [i for i in insts if i.index in sel]
        if not keep:
            raise SystemExit(f"--samples: нет таких инструментов (есть 1..{len(insts)})")
        insts = keep
        notes = [n for n in notes if n.key in {i.key for i in insts}]
    if len(insts) > MAX_XM_INSTRUMENTS:
        raise SystemExit(f"инструментов {len(insts)} > {MAX_XM_INSTRUMENTS}")
    orig_index = {id(i): i.index for i in insts}
    for i, ins in enumerate(insts):
        ins.index = i + 1
    inst_of = {ins.key: ins for ins in insts}

    # ── WAV ──
    if want_wav:
        masks = None
        if sel:
            masks = {c: [] for c in range(5)}
            for n in notes:
                masks[n.ch].append((n.t0, n.t1 + 0.002))
        print("[wav] рендер ...")
        y = emu.render_audio(tr, SR, 4, masks=masks)
        write_wav(args.wav_file or base + '.wav', y, args.wav_gain)
    if not (want_xm or want_midi):
        return

    # ── подстройка сетки полутонов ──
    ps, wt = [], []
    for n in notes:
        if n.kind in ('pulse', 'tri') and n.cur and n.cur[0][1] > 0:
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

    # ── зоны и сэмплы ──
    for ins in insts:
        if ins.kind in ('pulse', 'tri'):
            ns = []
            for n in ins.notes:
                for c in n.cur:
                    ns.append(min(96, max(1, int(round(note_of(c[1], off))))))
            ns.sort()
            zones = []
            lo = ns[0]
            while lo <= ns[-1]:
                hi = lo + args.zone_width - 1
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
            for z in zones:
                f_ref = freq_of(z.n0, off)
                z.L = pow2_at_least(2.0 * 22050.0 * 1.5 / f_ref)
                if ins.kind == 'pulse':
                    seq = emu.DUTY_SEQ[ins.key[1]]
                else:
                    seq = [v / 15.0 for v in emu.TRI_SEQ]
                z.data = stepwave(seq, z.L)
                z.loop = (0, z.L)
        elif ins.kind == 'noise':
            z = Zone(1, 96, 1)
            z.data, ins.peak = noise_sample(ins.key[1])
            z.loop = (0, len(z.data))
            ins.zones = [z]
        elif ins.kind == 'dmc':
            data, level0, loop, rate = ins.notes[0].data
            nbits = len(data) * 8
            c = emu.dmc_decode(data, level0, loop, nbits)
            pcm = hp_sample(c * emu.DMC_STEP_GAIN, rate, loop)
            pk = float(np.abs(pcm).max()) or 1e-6
            z = Zone(1, 96, 49)
            z.data = pcm / pk
            z.loop = (0, len(pcm)) if loop else None
            ins.zones = [z]
            ins.rate0 = rate
            ins.peak = pk
        elif ins.kind == 'dac':
            vals, rate = ins.notes[0].data
            pcm = emu.dmc_curve(np.frombuffer(vals, dtype=np.uint8).astype(np.float64))
            pcm = hp_sample(pcm, rate, False)
            pk = float(np.abs(pcm).max()) or 1e-6
            z = Zone(1, 96, 49)
            z.data = pcm / pk
            ins.zones = [z]
            ins.rate0 = rate
            ins.peak = pk
        if ins.kind in ('noise', 'dmc', 'dac'):
            sp = np.abs(np.fft.rfft(ins.zones[0].data[:8192] * np.hanning(len(ins.zones[0].data[:8192]))))
            fr = np.fft.rfftfreq(len(ins.zones[0].data[:8192]), 1.0)
            ins.centroid = float((sp * fr).sum() / (sp.sum() + 1e-9))
    # амплитуды DMC/PCM/шума в нотах
    for n in notes:
        if n.kind in ('dmc', 'dac'):
            n.cur = [(c[0], c[1], inst_of[n.key].peak) for c in n.cur]

    # ── уровни ──
    peaks = [max(c[2] for c in n.cur) for n in notes]
    norm = XM_REF_AMP * args.norm_scale
    over = sum(1 for p in peaks if p > norm)
    print(f"[xm] норма громкости: {norm:.3f}; громче нормы {over} нот из {len(peaks)} "
          f"(пик {max(peaks):.2f}, они прижимаются)")

    # ── сетка строк ──
    frame_dt = player.cpp / clock
    if args.bpm:
        speed = args.speed or 6
        bpm = args.bpm
        true_delta = 2.5 * speed / bpm
    else:
        true_delta = frame_dt * args.compact
        # эффекты строки (3xx для смены высоты) срабатывают со 2-го тика: при 1 тике на строку
        # их нет, поэтому при сетке в 1 кадр используем повторный запуск ноты
        bpm, speed, _ = interval_to_bpm_speed(true_delta, 2 if args.compact > 1 else 1)
        if args.speed:
            speed = args.speed
            bpm = max(32, min(255, int(round(2.5 * speed / true_delta))))
    delta = 2.5 * speed / bpm
    per = '' if args.bpm else f"{true_delta / frame_dt:.0f} кадр. = "
    print(f"[xm] строка = {per}{true_delta * 1000:.2f} мс  "
          f"(BPM {bpm}, speed {speed}, ровно {delta * 1000:.2f} мс)")
    glide = speed >= 2

    def row_of(t):
        return int(math.floor(t / true_delta + 1e-9))

    for n in notes:
        n.start_row = row_of(n.t0)
        # последняя неполная строка тоже звучит (уровень по энергии ниже)
        n.end_row = max(n.start_row + 1, int(math.ceil(n.t1 / true_delta - 1e-6)))
    sh = min(n.start_row for n in notes)
    for n in notes:
        n.start_row -= sh
        n.end_row -= sh
    row_t0 = sh * true_delta

    # ── fold: ноты должны влезть в 1..96 ──
    for ins in insts:
        if ins.kind in ('pulse', 'tri'):
            vals = [note_of(c[1], off) + shift for n in ins.notes for c in n.cur if c[1] > 0]
            lo, hi = (min(vals), max(vals)) if vals else (49, 49)
            fold = 0
            if hi + 0.5 > 96:
                fold = -int(math.ceil((hi + 0.5 - 96) / 12.0))
            elif lo - 0.5 < 1:
                fold = int(math.ceil((1 - (lo - 0.5)) / 12.0))
            ins.fold = fold

    def cell_float(ins, f):
        if ins.kind in ('pulse', 'tri'):
            return note_of(f, off) + shift + 12 * ins.fold
        if ins.kind == 'noise':
            return 1.0 + 12.0 * math.log2(max(f, 1.0) / 440.0)
        return 49.0 + 12.0 * math.log2(f / ins.rate0)

    # ── инструменты XM ──
    xm_insts = []
    for ins in insts:
        smps = []
        keymap = [0] * 96
        for zi, z in enumerate(ins.zones):
            d16 = np.clip(np.round(z.data * 32767.0), -32768, 32767).astype(np.int16)
            if ins.kind in ('pulse', 'tri'):
                z.cell0 = z.n0 + shift + 12 * ins.fold
                play_rate = freq_of(z.n0, off) * z.L
                tune = 12.0 * math.log2(play_rate / XM_C4_RATE) - (z.cell0 - 49)
            elif ins.kind == 'noise':
                tune = 48.0 + 12.0 * math.log2(440.0 / XM_C4_RATE)
            else:
                tune = 12.0 * math.log2(ins.rate0 / XM_C4_RATE)
            rel_ = int(math.floor(tune))
            fine = int(round((tune - rel_) * 128))
            if fine > 127:
                rel_, fine = rel_ + 1, fine - 128
            rel_ = max(-96, min(95, rel_))
            smps.append({'data': d16, 'loop': z.loop, 'rel': rel_,
                         'finetune': max(-128, min(127, fine))})
            if ins.kind in ('pulse', 'tri'):
                for cell in range(1, 97):
                    n_real = cell - shift - 12 * ins.fold
                    if z.lo <= n_real <= z.hi:
                        keymap[cell - 1] = zi
        if ins.kind in ('pulse', 'tri'):
            for cell in range(1, 97):
                n_real = cell - shift - 12 * ins.fold
                if not any(z.lo <= n_real <= z.hi for z in ins.zones):
                    keymap[cell - 1] = ins.zones.index(ins.zone_for(n_real))
        xm_insts.append({'name': f"{orig_index[id(ins)]:02d} {ins.name}"[:22],
                         'samples': smps, 'keymap': keymap})
    print(f"[xm] инструментов: {len(xm_insts)}, сэмплов: {sum(len(x['samples']) for x in xm_insts)}")

    for n in notes:
        n.inst = inst_of[n.key].index
    nch = allocate_channels(sorted(notes, key=lambda n: (n.start_row, n.ch)), args.channels)
    nch = max(2, nch + (nch & 1))
    nch = min(nch, MAX_XM_CHANNELS)
    print(f"[xm] каналов: {nch}")

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
                times = [c[0] for c in n.cur]
                short = (n.t1 - n.t0) < 3 * true_delta

                def row_amp(row):
                    """Среднеквадратичная амплитуда ноты внутри строки: энергия коротких звуков
                    сохраняется (строка 33 мс длиннее хлопка шума на 15 мс)."""
                    tw0 = row_t0 + row * true_delta
                    a0, a1 = max(n.t0, tw0), min(n.t1, tw0 + true_delta)
                    if a1 <= a0:
                        return 0.0
                    e = 0.0
                    i = max(0, bisect.bisect_right(times, a0) - 1)
                    t = a0
                    while t < a1 and i < len(times):
                        nxt = times[i + 1] if i + 1 < len(times) else a1
                        te = min(a1, nxt)
                        e += n.cur[i][2] ** 2 * (te - t)
                        t = te
                        i += 1
                    # первая строка долгой ноты: по времени звучания, чтобы не было "наплыва"
                    span = (a1 - a0) if (row == n.start_row and not short) else true_delta
                    return math.sqrt(e / span)

                def vol_of(a):
                    return max(1, min(64, int(round(soft_limit(a / norm, args.limit_knee) * 64.0)))) \
                        if a > 0 else 0

                c0 = put(n.start_row, ch)
                x0 = cell_float(ins, n.cur[0][1])
                cur_cell = max(1, min(96, int(round(x0))))
                c0.note = cur_cell
                c0.inst = n.inst
                last_vol = vol_of(row_amp(n.start_row))
                c0.vol = 0x10 + last_vol
                last_pitch = x0
                if ins.kind not in ('dmc', 'dac'):
                    for row in range(n.start_row + 1, end_row):
                        t = row_t0 + row * true_delta
                        i = max(0, bisect.bisect_right(times, t) - 1)
                        _, f, _a = n.cur[i]
                        c = None
                        vv = vol_of(row_amp(row))
                        if vv != last_vol and abs(vv - last_vol) >= max(1, int(0.06 * last_vol)):
                            c = put(row, ch)
                            c.vol = 0x10 + vv
                            last_vol = vv
                        pf = cell_float(ins, f)
                        if abs(pf - last_pitch) >= args.bend_threshold:
                            nc = max(1, min(96, int(round(pf))))
                            if nc != cur_cell:
                                c = c or put(row, ch)
                                c.note = nc
                                if glide:
                                    c.eff, c.par = 0x03, 0xFF
                                else:               # без эффектов: повторный запуск, громкость вернуть
                                    c.inst = n.inst
                                    c.vol = 0x10 + last_vol
                                cur_cell = nc
                            last_pitch = pf
                if nxt is None or end_row < nxt:
                    off_c = put(end_row, ch)
                    if not off_c.note:
                        off_c.note = 97
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
        print(f"[xm] {xm_path}  ({len(xm.patterns)} паттернов, {os.path.getsize(xm_path) / 1024.0:.0f} КБ)")

    # ── MIDI ──
    if want_midi:
        ev = []
        tps = 960.0
        mch_of = {0: 0, 1: 1, 2: 2}
        init_done = set()
        ch_prog = {}
        drums = {}
        dr_ins = sorted([i for i in insts if i.kind in ('dmc', 'dac')], key=lambda i: i.centroid)
        for k, i in enumerate(dr_ins):
            drums[i.key] = DRUM_NOTES[min(len(DRUM_NOTES) - 1, k * len(DRUM_NOTES) // max(1, len(dr_ins)))]

        def noise_drum(n):
            rate = n.cur[0][1]
            if n.patch == 1:
                return 42
            if rate > 12000:
                return 42
            if rate > 3000:
                return 38
            return 36

        def init_ch(mch, t):
            if mch in init_done:
                return
            init_done.add(mch)
            ev.extend([(0, 0, mch, bytes([0xB0 | mch, 101, 0])), (0, 0, mch, bytes([0xB0 | mch, 100, 0])),
                       (0, 0, mch, bytes([0xB0 | mch, 6, 12])), (0, 0, mch, bytes([0xB0 | mch, 38, 0]))])

        for n in sorted(notes, key=lambda n: n.t0):
            tick0 = int(round(n.t0 * tps))
            tick1 = max(tick0 + 20, int(round(n.t1 * tps)))
            maxa = max(c[2] for c in n.cur)
            vel = max(1, min(127, int(round(soft_limit(maxa / norm, args.limit_knee) * 110))))
            if n.kind in ('noise', 'dmc', 'dac'):
                mch = 9
                key = noise_drum(n) if n.kind == 'noise' else drums.get(n.key, 38)
                init_ch(mch, tick0)
                ev.append((tick0, 3, mch, bytes([0x90 | mch, key, vel])))
                ev.append((tick1, 0, mch, bytes([0x80 | mch, key, 0])))
                continue
            mch = mch_of[n.ch]
            init_ch(mch, tick0)
            prog = 38 if n.kind == 'tri' else 80
            if ch_prog.get(mch) != prog:
                ev.append((tick0, 1, mch, bytes([0xC0 | mch, prog])))
                ch_prog[mch] = prog
            # ноты режутся на скачках высоты (>0.7 полутона), внутри - pitch bend и громкость (CC11)
            key = None
            last_b = last_e = None
            seg_start = tick0
            for i, (tt, f, a) in enumerate(n.cur):
                tk = max(tick0, int(round(tt * tps)))
                if tk >= tick1:
                    break
                midi_f = 69.0 + 12.0 * math.log2(f / 440.0) - off + shift
                if key is None or abs(midi_f - key) > 0.7:
                    if key is not None:
                        ev.append((tk, 0, mch, bytes([0x80 | mch, key, 0])))
                    key = max(0, min(127, int(round(midi_f))))
                    ev.append((tk, 2, mch, bytes([0xB0 | mch, 10, 64])))
                    ev.append((tk, 3, mch, bytes([0x90 | mch, key, vel])))
                    last_b = last_e = None
                bend = max(-12.0, min(12.0, midi_f - key))
                if last_b is None or abs(bend - last_b) >= 0.06:
                    val = int(8192 + bend / 12.0 * 8191)
                    ev.append((tk, 2, mch, bytes([0xE0 | mch, val & 0x7F, (val >> 7) & 0x7F])))
                    last_b = bend
                e = max(1, min(127, int(round(a / max(maxa, 1e-9) * 127))))
                if last_e is None or abs(e - last_e) >= 3:
                    ev.append((tk, 2, mch, bytes([0xB0 | mch, 11, e])))
                    last_e = e
            if key is not None:
                ev.append((tick1, 0, mch, bytes([0x80 | mch, key, 0])))
        midi_path = base + '.mid'
        write_midi(midi_path, title, ev)
        print(f"[mid] {midi_path}")


def main():
    ap = argparse.ArgumentParser(description="NSF (NES) -> XM / MIDI / WAV")
    ap.add_argument('input', nargs='?', help='файл .nsf')
    ap.add_argument('-o', '--output', help='базовое имя выходных файлов')
    ap.add_argument('--track', type=int, help='номер трека (с 1); по умолчанию стартовый')
    ap.add_argument('--all-tracks', action='store_true', help='все треки (файлы <имя>_trackNN.*)')
    ap.add_argument('--xm', action='store_true', help='записать XM (по умолчанию)')
    ap.add_argument('--midi', action='store_true', help='XM + MIDI')
    ap.add_argument('--midi-only', action='store_true', help='только MIDI')
    ap.add_argument('--wav', action='store_true', help='+ WAV (рендер эмулятора, 44.1 кГц)')
    ap.add_argument('--wav-only', action='store_true', help='только WAV')
    ap.add_argument('--wav-file', help='имя WAV файла')
    ap.add_argument('--wav-gain', type=float, default=1.0, help='усиление WAV (1.0 = как у libgme)')
    ap.add_argument('--all', action='store_true', help='XM + MIDI + WAV')
    ap.add_argument('--seconds', '--duration', type=float, dest='seconds', default=120.0,
                    help='сколько секунд эмулировать (по умолчанию 120)')
    ap.add_argument('--stop-silence', type=float, default=5.0,
                    help='остановиться после стольких секунд тишины (0 - не останавливаться)')
    ap.add_argument('--tail', type=float, default=1.0, help='секунд после последнего звука в результате')
    ap.add_argument('--samples', help='оставить только эти инструменты (номера из таблицы, напр. 1,3-4): '
                                      'XM, MIDI и WAV будут только с ними')
    ap.add_argument('--octave', type=int, default=0, help='сдвиг имён нот в октавах (звук не меняется)')
    ap.add_argument('--transpose', type=int, default=0, help='сдвиг имён нот в полутонах (звук не меняется)')
    ap.add_argument('--compact', type=int, default=2,
                    help='кадров PLAY на строку XM (по умолчанию 2; при 1 нет эффекта плавной смены '
                         'высоты, высота меняется повторным запуском ноты)')
    ap.add_argument('--bpm', type=int, help='задать BPM вручную')
    ap.add_argument('--speed', type=int, help='задать speed вручную')
    ap.add_argument('--channels', type=int, default=MAX_XM_CHANNELS, help='максимум каналов XM (<=32)')
    ap.add_argument('--zone-width', type=int, default=12,
                    help='ширина зоны одного сэмпла pulse/треугольника в полутонах')
    ap.add_argument('--bend-threshold', type=float, default=0.6,
                    help='порог (полутона), после которого внутри ноты меняется высота')
    ap.add_argument('--norm-scale', type=float, default=1.0,
                    help='громкость XM: 1.0 - уровень как в WAV (нота с амплитудой 0.18 = громкость 64); '
                         'меньше - громче, но громкие ноты сжимаются')
    ap.add_argument('--limit-knee', type=float, default=0.75,
                    help='с какой доли громкости 64 начинается мягкое ограничение (1.0 - выкл.)')
    args = ap.parse_args()
    args.channels = max(2, min(MAX_XM_CHANNELS, args.channels))
    args.compact = max(1, args.compact)
    only = args.midi_only or args.wav_only
    args.want_xm = args.xm or args.all or not only
    args.want_midi = args.midi or args.all or args.midi_only
    args.want_wav = args.wav or args.all or args.wav_only
    if not args.input or not os.path.isfile(args.input):
        ap.error(f"нужен файл .nsf: {args.input or ''}")

    nsf = emu.load_nsf(args.input)
    chips = emu.chips_of(nsf)
    print(f"[nsf] {os.path.basename(args.input)}: «{nsf.title}», {nsf.artist}, треков {nsf.total_songs}"
          f"{', банки' if nsf.banked else ''}")
    if chips:
        print(f"[nsf] ВНИМАНИЕ: звук расширений ({', '.join(chips)}) не эмулируется, "
              f"будет только 2A03", file=sys.stderr)
    base0 = os.path.splitext(args.output or args.input)[0]
    if args.all_tracks:
        songs = list(range(nsf.total_songs))
    elif args.track:
        if not 1 <= args.track <= nsf.total_songs:
            raise SystemExit(f"трек {args.track} не существует (1..{nsf.total_songs})")
        songs = [args.track - 1]
    else:
        songs = [nsf.start_song - 1]
    for s in songs:
        base = f"{base0}_track{s + 1:02d}" if args.all_tracks and nsf.total_songs > 1 else base0
        title = nsf.title or os.path.basename(base0)
        if nsf.total_songs > 1:
            title = f"{title} #{s + 1}"
        convert_track(args, nsf, s, base, title)


if __name__ == '__main__':
    main()
