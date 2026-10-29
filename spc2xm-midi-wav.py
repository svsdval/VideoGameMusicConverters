#!/usr/bin/env python3
"""
SPC (Super Nintendo) -> XM / MIDI / WAV

Как это работает (тот же принцип, что у usf2xm-midi-wav.py и vgm2xm-midi-wav.py:
звук берётся не "на глаз", а из точной эмуляции):

  1. spc_tracer/spctrace (Game Music Emu + небольшой патч в S-DSP) проигрывает .spc
     и пишет:
       * trace - старт каждого голоса (KON: номер сэмпла, высота, громкость) и
                 состояние активных голосов каждые 4 мс (высота, огибающая ADSR/GAIN,
                 громкость L/R)
       * pcm   - уже декодированные BRR-блоки (ровно то, что слышит DSP)
       * raw   - рендер эмулятора для WAV (44.1 кГц стерео)
  2. Скрипт превращает лог в ноты, вырезает сэмплы (петля берётся из самого BRR),
     определяет тон по автокорреляции и пишет XM / MIDI / WAV.

Использование:
    python3 spc2xm-midi-wav.py music.spc
    python3 spc2xm-midi-wav.py music.spc --midi --seconds 90
    python3 spc2xm-midi-wav.py music.spc --all --compact 2
"""

import argparse
import collections
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
TRACER_DIR = os.path.join(HERE, 'spc_tracer')

DSP_RATE = 32000
FRAME_SAMPLES = 128           # шаг лога голосов (4 мс)
C4_HZ = 261.6256
XM_C4_RATE = 8363.0
MAX_XM_CHANNELS = 32
MAX_XM_INSTRUMENTS = 128


# ═══════════════════════════════════════════════════════════════
# Сэмплы: луп, тон
# ═══════════════════════════════════════════════════════════════

def detect_loop(s):
    """Ищем точный период в хвосте декодированного потока (луп ADPCM
    даёт побитно одинаковые повторы). Возвращает (loop_start, loop_len) или None."""
    n = len(s)
    if n < 96 or not np.any(s):
        return None
    w = 48
    key = s[n - w:]
    idx = np.nonzero(s[:n - w] == key[0])[0]
    for i in idx[::-1]:
        p = n - w - int(i)          # кандидат-период
        if p < 16 or n < 2 * p:
            continue
        if not np.array_equal(s[i:i + w], key):
            continue
        eq = s[p:] == s[:n - p]
        bad = np.nonzero(~eq)[0]
        start = int(bad[-1]) + 1 if len(bad) else 0
        if n - start >= 2 * p:
            # минимальный период: пробуем делители
            for d in range(2, 9):
                if p % d == 0 and np.array_equal(s[start + p // d:start + p],
                                                 s[start:start + p - p // d]):
                    p //= d
            return start, p
    return None


def detect_period(x):
    """Нормированная автокорреляция. -> (период в сэмплах, уверенность) или (None, 0)."""
    x = x.astype(np.float64)
    n = len(x)
    if n < 64:
        return None, 0.0
    x = x - x.mean()
    if not np.any(x):
        return None, 0.0
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    maxlag = min(n // 2, 1024)
    minlag = 6
    if maxlag <= minlag + 2:
        return None, 0.0
    lags = np.arange(maxlag)
    r = ac[:maxlag] / ac[0] * (n / (n - lags))
    seg = r[minlag:maxlag]
    best = seg.max()
    if best < 0.5:
        return None, float(best)
    # первый локальный максимум, достаточно близкий к лучшему (против октавных ошибок)
    for i in range(1, len(seg) - 1):
        if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1] and seg[i] >= 0.9 * best:
            a, b, c = seg[i - 1], seg[i], seg[i + 1]
            den = a - 2 * b + c
            off = 0.5 * (a - c) / den if den != 0 else 0.0
            return minlag + i + off, float(b)
    i = int(seg.argmax())
    return float(minlag + i), float(best)


class Sample:
    def __init__(self, h, pcm, loop_info=None):
        # loop_info: (start, len) - петля известна из BRR; 'none' - сэмпл не зациклен; None - искать
        self.loop_info = loop_info
        self.post = None
        self.hash = h
        self.index = 0
        self.use_count = 0
        self.loop = None
        self.data = pcm
        self.period = None            # период основного тона в сэмплах (на 1:1)
        self.confidence = 0.0
        self.m1 = 0.0                 # высота (полутона от C-4) при ratio == 1
        self.pitched = False
        self.centroid = 0.0
        self.fold = 0                 # сдвиг октав, чтобы ноты влезли в 1..96
        self.peak = 1.0
        self.tune = 0.0               # relative_note + finetune/128
        self.raw = None               # данные до ФНЧ (для синтеза WAV)
        self.raw_loop = None
        self.orig = 0                 # номер сэмпла в полном прогоне

    def analyse(self):
        s = self.data
        if isinstance(self.loop_info, tuple):
            lp = self.loop_info
        elif self.loop_info == 'none':
            lp = None
        else:
            lp = detect_loop(s)
        if lp is not None:
            start, p = lp
            self.loop = (start, p)
            self.data = s[:start + p]
            seg = np.tile(s[start:start + p], max(1, 4096 // p + 1))[:8192]
        else:
            nz = np.nonzero(s)[0]
            if len(nz):
                self.data = s[:nz[-1] + 1]
            seg = self.data[min(len(self.data) // 8, 256):][:6000]
        self.raw, self.raw_loop = self.data, self.loop
        if self.post is not None:
            self.data = self.post(self.data, self.loop)
        self.peak = float(np.abs(self.data.astype(np.int32)).max()) or 1.0
        period, conf = detect_period(seg)
        self.confidence = conf
        if period is not None and conf >= 0.6:
            self.period = period
            self.pitched = True
        if len(seg) > 64:
            spec = np.abs(np.fft.rfft(seg.astype(np.float64) * np.hanning(len(seg))))
            fr = np.fft.rfftfreq(len(seg))
            self.centroid = float((spec * fr).sum() / (spec.sum() + 1e-9))

    def set_pitch(self, ref_rate):
        if self.pitched:
            f1 = ref_rate / self.period
            self.m1 = 12.0 * math.log2(f1 / C4_HZ)


# ═══════════════════════════════════════════════════════════════
# Темп
# ═══════════════════════════════════════════════════════════════

def detect_row_interval(times, max_row=0.06):
    """Ищет шаг сетки (сек) и фазу, на которые ложатся старты нот.

    Метрика - когерентность |mean(exp(2*pi*i*t/d))| (не зависит от фазы).
    Находим сильнейший пик (доли такта), потом делим его на целое число m
    так, чтобы строка получилась не длиннее max_row секунд.
    Возвращает (delta, t0, coherence)."""
    t = np.array(sorted(times), dtype=np.float64)
    if len(t) < 4:
        return 0.05, 0.0, 0.0
    grid = np.arange(0.02, 0.45, 0.0002)
    sc = np.array([abs(np.exp(2j * np.pi * t / d).mean()) for d in grid])
    best = sc.max()
    if best < 0.25:
        return 0.025, 0.0, float(best)
    # самый крупный пик, не слабее 90% лучшего
    peaks = [i for i in range(1, len(grid) - 1)
             if sc[i] >= sc[i - 1] and sc[i] >= sc[i + 1] and sc[i] >= 0.9 * best]
    d0 = grid[max(peaks, key=lambda i: grid[i])]
    # уточнение
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
    """delta (сек/строку) -> (bpm, speed, реальная delta)."""
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


# ═══════════════════════════════════════════════════════════════
# Распределение по каналам и событиям трекера
# ═══════════════════════════════════════════════════════════════

class Cell:
    __slots__ = ('note', 'inst', 'vol', 'eff', 'par')

    def __init__(self):
        self.note = self.inst = self.vol = self.eff = self.par = 0


def allocate_channels(notes, max_channels):
    """Жадно: свободный канал с тем же инструментом, иначе первый свободный,
    иначе вытесняем самую старую ноту."""
    busy_until = []
    last_inst = []
    started = []
    owner = []
    for n in notes:
        free = [c for c in range(len(busy_until)) if busy_until[c] <= n.start_row]
        same = [c for c in free if last_inst[c] == n.sample.index]
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
        last_inst[c] = n.sample.index
        started[c] = n.start_row
        owner[c] = n
    return len(busy_until)


def soft_limit(x, knee):
    """Относительная громкость (1.0 = громкость 64) -> 0..1: до knee линейно,
    выше плавно прижимается к 1.0 (вместо жёсткого обрезания баланс сохраняется)."""
    if x <= knee or knee >= 1.0:
        return min(x, 1.0)
    w = 1.0 - knee
    return knee + w * math.tanh((x - knee) / w)


def cell_float(note, ratio_idx, sample, shift):
    """Нота XM (вещественная) для i-го кадра ноты."""
    r = max(note.ratio[ratio_idx], 1e-4)
    return 49.0 + sample.m1 + 12.0 * math.log2(r) + shift


# ═══════════════════════════════════════════════════════════════
# XM writer
# ═══════════════════════════════════════════════════════════════

class XMWriter:
    def __init__(self, title, nch, bpm, speed):
        self.title = title
        self.nch = nch
        self.bpm = bpm
        self.speed = speed
        self.patterns = []
        self.instruments = []

    def set_rows(self, grid, nrows):
        rpp = 64
        npat = max(1, (nrows + rpp - 1) // rpp)
        if npat > 256:
            print(f"[xm] ВНИМАНИЕ: {npat} паттернов > 256, хвост обрезан "
                  f"(используйте --compact)", file=sys.stderr)
            npat = 256
        self.patterns = []
        for p in range(npat):
            rows = []
            for r in range(rpp):
                rows.append([grid.get((p * rpp + r, c)) for c in range(self.nch)])
            self.patterns.append(rows)

    def write(self, path):
        with open(path, 'wb') as f:
            f.write(b'Extended Module: ')
            f.write(self.title.encode('ascii', 'replace')[:20].ljust(20, b' '))
            f.write(b'\x1a')
            f.write(b'USF2XM Converter    ')
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
        f.write(struct.pack('<IBHH', 9, 0, len(self.patterns[0]), len(packed)))
        f.write(packed)

    def _write_instrument(self, f, inst):
        name = inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00')
        data = inst['data']
        ihs = 263
        f.write(struct.pack('<I', ihs))
        f.write(name)
        f.write(struct.pack('<BH', 0, 1))
        f.write(struct.pack('<I', 40))
        f.write(bytearray(96))
        ve = bytearray(48)
        struct.pack_into('<HHHH', ve, 0, 0, 64, 100, 64)
        f.write(ve)
        pe = bytearray(48)
        struct.pack_into('<HHHH', pe, 0, 0, 32, 100, 32)
        f.write(pe)
        f.write(bytes([2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
        f.write(struct.pack('<H', 0))
        f.write(b'\x00' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
        # sample header (16 бит)
        loop = inst['loop']
        ls, ll = (loop if loop else (0, 0))
        f.write(struct.pack('<III', len(data) * 2, ls * 2, ll * 2))
        f.write(struct.pack('<BbBBb', 64, inst['finetune'], (1 if loop else 0) | 0x10,
                            128, inst['rel']))
        f.write(b'\x00')
        f.write(name)
        # дельта-кодирование
        d = np.diff(data.astype(np.int32), prepend=0)
        d = ((d + 32768) % 65536 - 32768).astype('<i2')
        f.write(d.tobytes())


# ═══════════════════════════════════════════════════════════════
# MIDI writer
# ═══════════════════════════════════════════════════════════════

def vlq(v):
    v = max(0, int(v))
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


DRUM_NOTES = [36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56]
GM_PROGRAMS = [0, 25, 33, 48, 80, 81, 73, 56, 4, 11, 19, 24, 38, 46, 52, 61,
               71, 65, 87, 90, 95, 98, 104, 108]


def write_midi(path, title, midi_events):
    """midi_events: список (tick, order, bytes). Один трек на MIDI-канал."""
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
# SPC-файл
# ═══════════════════════════════════════════════════════════════

class SpcFile:
    pass


def _txt(b):
    return b.split(b'\x00')[0].decode('latin-1', 'replace').strip()


def read_spc(path):
    with open(path, 'rb') as f:
        d = f.read()
    if d[:11] != b'SNES-SPC700':
        raise SystemExit(f"{path}: это не SPC")
    s = SpcFile()
    s.ram = d[0x100:0x10100]
    s.title = s.game = ''
    s.length = 0.0
    if len(d) >= 0xB0 and d[0x23] == 26:
        s.title = _txt(d[0x2E:0x4E])
        s.game = _txt(d[0x4E:0x6E])
        try:
            s.length = float(_txt(d[0xA9:0xAC]) or 0)
            fade = float(_txt(d[0xAC:0xB1]) or 0) / 1000.0
            if s.length:
                s.length += fade
        except ValueError:
            s.length = 0.0
    return s


def brr_extent(ram, start):
    """Блоки BRR от start до блока с флагом конца -> (число блоков, зацикливается ли)."""
    a = start
    for n in range(1, 4097):
        h = ram[a]
        if h & 1:
            return n, bool(h & 2)
        a = (a + 9) & 0xFFFF
    return 4096, False


# ═══════════════════════════════════════════════════════════════
# Гауссова интерполяция S-DSP
# ═══════════════════════════════════════════════════════════════
# DSP воспроизводит сэмпл через 4-точечный гауссов фильтр - это заметный ФНЧ
# (на частоте Найквиста сэмпла -10 дБ и ниже). XM-плеер этого не делает, поэтому
# сэмплы заранее фильтруются усреднённой амплитудной характеристикой (таблица из GME).

GAUSS_IG = [
    370, 1305, 366, 1305, 362, 1304, 358, 1304, 354, 1304, 351, 1304, 347, 1304, 343, 1303,
    339, 1303, 336, 1303, 332, 1302, 328, 1302, 325, 1301, 321, 1300, 318, 1300, 314, 1299,
    311, 1298, 307, 1297, 304, 1297, 300, 1296, 297, 1295, 293, 1294, 290, 1293, 286, 1292,
    283, 1291, 280, 1290, 276, 1288, 273, 1287, 270, 1286, 267, 1284, 263, 1283, 260, 1282,
    257, 1280, 254, 1279, 251, 1277, 248, 1275, 245, 1274, 242, 1272, 239, 1270, 236, 1269,
    233, 1267, 230, 1265, 227, 1263, 224, 1261, 221, 1259, 218, 1257, 215, 1255, 212, 1253,
    210, 1251, 207, 1248, 204, 1246, 201, 1244, 199, 1241, 196, 1239, 193, 1237, 191, 1234,
    188, 1232, 186, 1229, 183, 1227, 180, 1224, 178, 1221, 175, 1219, 173, 1216, 171, 1213,
    168, 1210, 166, 1207, 163, 1205, 161, 1202, 159, 1199, 156, 1196, 154, 1193, 152, 1190,
    150, 1186, 147, 1183, 145, 1180, 143, 1177, 141, 1174, 139, 1170, 137, 1167, 134, 1164,
    132, 1160, 130, 1157, 128, 1153, 126, 1150, 124, 1146, 122, 1143, 120, 1139, 118, 1136,
    117, 1132, 115, 1128, 113, 1125, 111, 1121, 109, 1117, 107, 1113, 106, 1109, 104, 1106,
    102, 1102, 100, 1098, 99, 1094, 97, 1090, 95, 1086, 94, 1082, 92, 1078, 90, 1074,
    89, 1070, 87, 1066, 86, 1061, 84, 1057, 83, 1053, 81, 1049, 80, 1045, 78, 1040,
    77, 1036, 76, 1032, 74, 1027, 73, 1023, 71, 1019, 70, 1014, 69, 1010, 67, 1005,
    66, 1001, 65, 997, 64, 992, 62, 988, 61, 983, 60, 978, 59, 974, 58, 969,
    56, 965, 55, 960, 54, 955, 53, 951, 52, 946, 51, 941, 50, 937, 49, 932,
    48, 927, 47, 923, 46, 918, 45, 913, 44, 908, 43, 904, 42, 899, 41, 894,
    40, 889, 39, 884, 38, 880, 37, 875, 36, 870, 36, 865, 35, 860, 34, 855,
    33, 851, 32, 846, 32, 841, 31, 836, 30, 831, 29, 826, 29, 821, 28, 816,
    27, 811, 27, 806, 26, 802, 25, 797, 24, 792, 24, 787, 23, 782, 23, 777,
    22, 772, 21, 767, 21, 762, 20, 757, 20, 752, 19, 747, 19, 742, 18, 737,
    17, 732, 17, 728, 16, 723, 16, 718, 15, 713, 15, 708, 15, 703, 14, 698,
    14, 693, 13, 688, 13, 683, 12, 678, 12, 674, 11, 669, 11, 664, 11, 659,
    10, 654, 10, 649, 10, 644, 9, 640, 9, 635, 9, 630, 8, 625, 8, 620,
    8, 615, 7, 611, 7, 606, 7, 601, 6, 596, 6, 592, 6, 587, 6, 582,
    5, 577, 5, 573, 5, 568, 5, 563, 4, 559, 4, 554, 4, 550, 4, 545,
    4, 540, 3, 536, 3, 531, 3, 527, 3, 522, 3, 517, 2, 513, 2, 508,
    2, 504, 2, 499, 2, 495, 2, 491, 2, 486, 1, 482, 1, 477, 1, 473,
    1, 469, 1, 464, 1, 460, 1, 456, 1, 451, 1, 447, 1, 443, 1, 439,
    0, 434, 0, 430, 0, 426, 0, 422, 0, 418, 0, 414, 0, 410, 0, 405,
    0, 401, 0, 397, 0, 393, 0, 389, 0, 385, 0, 381, 0, 378, 0, 374,
]


def gauss_response(n_bins):
    """Средняя по фазам |H(f)| гауссова фильтра, f = 0..0.5 цикла/сэмпл."""
    w = np.linspace(0, np.pi, n_bins)
    k = np.arange(4)
    acc = np.zeros(n_bins)
    for off in range(0, 512, 2):
        taps = np.array([GAUSS_IG[off], GAUSS_IG[off + 1],
                         GAUSS_IG[511 - off], GAUSS_IG[510 - off]], dtype=np.float64) / 2048.0
        acc += np.abs((taps[None, :] * np.exp(-1j * w[:, None] * k[None, :])).sum(axis=1))
    return acc / 256.0


def gauss_filter(data, loop):
    """Нуль-фазовый ФНЧ с характеристикой гауссова фильтра DSP."""
    x = data.astype(np.float64)
    if loop is not None:
        ls, ll = loop
        ext = np.concatenate([x[:ls], np.tile(x[ls:ls + ll], 6)])
    else:
        ext = np.concatenate([x, np.zeros(64)])
    n = len(ext)
    spec = np.fft.rfft(ext)
    h = gauss_response(len(spec))
    h = h / h[0]
    y = np.fft.irfft(spec * h, n)
    if loop is not None:
        ls, ll = loop
        out = np.concatenate([y[:ls], y[ls + 2 * ll:ls + 3 * ll]])
    else:
        out = y[:len(x)]
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


# ═══════════════════════════════════════════════════════════════
# Запуск трассировщика
# ═══════════════════════════════════════════════════════════════

def find_tracer():
    for c in (os.environ.get('SPCTRACE', ''), os.path.join(TRACER_DIR, 'spctrace'),
              shutil.which('spctrace') or ''):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def build_tracer():
    script = os.path.join(TRACER_DIR, 'build.sh')
    print(f"[spc] сборка трассировщика: {script}")
    subprocess.run(['sh', script], check=True)


def resample_32k_to_44k(x, half=16):
    """32000 -> 44100 Гц (коэффициент 441/320) полифазным sinc-фильтром с окном Блэкмана; только numpy."""
    n_in = len(x)
    n_out = n_in * 441 // 320
    taps = 2 * half
    j = np.arange(n_out, dtype=np.int64)
    base = (j * 320) // 441
    frac = (j * 320 % 441) / 441.0
    k = np.arange(-half + 1, half + 1)
    xp = np.concatenate((np.zeros((half, x.shape[1])), x, np.zeros((half + 1, x.shape[1]))))
    out = np.empty((n_out, x.shape[1]))
    for s0 in range(0, n_out, 50000):
        s1 = min(n_out, s0 + 50000)
        d = k[None, :] - frac[s0:s1, None]                       # расстояние от точки до отсчёта
        h = np.sinc(d) * (0.42 + 0.5 * np.cos(np.pi * d / half) + 0.08 * np.cos(2 * np.pi * d / half))
        idx = base[s0:s1, None] + k[None, :] + half
        out[s0:s1] = np.einsum('jk,jkc->jc', h, xp[idx])
    return out


def parse_sample_list(text):
    """'1,3,5-7' -> {1, 3, 5, 6, 7}"""
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


def render_notes_wav(notes, samples, nsamples):
    """Стерео-синтез (32 кГц) только переданных нот: BRR-данные сэмпла, высота и
    огибающая из трассы, гауссова интерполяция DSP. Эхо и мастер-громкость не учитываются."""
    out = np.zeros((nsamples + FRAME_SAMPLES, 2))
    g = np.arange(256)
    taps = np.stack([np.array(GAUSS_IG)[2 * g], np.array(GAUSS_IG)[2 * g + 1],
                     np.array(GAUSS_IG)[511 - 2 * g], np.array(GAUSS_IG)[510 - 2 * g]], axis=1) / 2048.0
    for n in notes:
        s = samples.get(n.hash)
        if s is None or not n.frames:
            continue
        raw = s.raw.astype(np.float64)
        ratio = np.repeat(np.array(n.ratio), FRAME_SAMPLES)
        pos = np.concatenate(([0.0], np.cumsum(ratio)[:-1]))
        if s.raw_loop:
            ls, ll = s.raw_loop
            need = int(pos[-1]) + 4
            reps = max(1, (need - ls) // ll + 2)
            ext = np.concatenate((raw[:ls], np.tile(raw[ls:ls + ll], reps)))
        else:
            ext = np.concatenate((raw, np.zeros(4)))
            keep = pos < len(raw) - 1
            pos = pos[keep]
        if not len(pos):
            continue
        i0 = pos.astype(np.int64)
        ph = ((pos - i0) * 256).astype(np.int64)
        xp = np.concatenate(([ext[0]], ext, np.zeros(4)))     # i-1 -> смещение +1
        y = (taps[ph, 0] * xp[i0] + taps[ph, 1] * xp[i0 + 1] +
             taps[ph, 2] * xp[i0 + 2] + taps[ph, 3] * xp[i0 + 3])
        m = len(y)
        env = np.repeat(np.array(n.env, dtype=np.float64), FRAME_SAMPLES)[:m] / 2048.0
        vl = np.repeat(np.array(n.vl, dtype=np.float64), FRAME_SAMPLES)[:m] / 128.0
        vr = np.repeat(np.array(n.vr, dtype=np.float64), FRAME_SAMPLES)[:m] / 128.0
        a = n.frames[0] * FRAME_SAMPLES
        m = min(m, len(out) - a)
        if m <= 0:
            continue
        out[a:a + m, 0] += y[:m] * env[:m] * vl[:m]
        out[a:a + m, 1] += y[:m] * env[:m] * vr[:m]
    return out[:nsamples]


def write_wav(path, x32):
    y = resample_32k_to_44k(x32)
    x = np.clip(np.round(y), -32768, 32767).astype('<i2')
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(x.tobytes())
    print(f"[wav] {path}")


def run_tracer(tracer, spc_path, prefix, seconds, want_raw):
    raw = prefix + '.raw'
    if tracer is None:                      # встроенный эмулятор на Python
        import spc_pyemu

        def prog(x):
            print(f"\r[spc] {x * 100:3.0f}%", end='', file=sys.stderr, flush=True)
        spc_pyemu.trace(spc_path, prefix, seconds, raw if want_raw else None, prog)
        print(file=sys.stderr)
        return raw if want_raw else None
    cmd = [tracer, os.path.abspath(spc_path), prefix, str(seconds)]
    if want_raw:
        cmd.append(raw)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        sys.stderr.write(res.stderr.decode('utf-8', 'replace'))
        raise SystemExit(f"spctrace завершился с кодом {res.returncode}")
    return raw if want_raw else None


# ═══════════════════════════════════════════════════════════════
# Разбор лога -> ноты
# ═══════════════════════════════════════════════════════════════

class Note:
    def __init__(self, voice, start_sample, h):
        self.key = voice
        self.hash = h
        self.start_t = start_sample / float(DSP_RATE)
        self.start_frame = start_sample // FRAME_SAMPLES
        self.end_frame = self.start_frame + 1
        self.end_t = self.start_t
        self.frames = []
        self.ratio = []        # pitch / 0x1000
        self.level = []        # 0..1
        self.pan = []          # 0..255
        self.env = []          # огибающая DSP, 0..2047
        self.vl = []           # громкость L/R со знаком
        self.vr = []
        self.peak = 0.0
        self.sample = None
        self.channel = 0
        self.start_row = 0
        self.end_row = 0


def parse_trace(prefix, release_ratio):
    """-> (ноты, meta{start: loop_addr}, frame_times)."""
    notes = []
    meta = {}
    cur = {}                  # голос -> Note
    quiet = {}
    last_frame = {}
    max_frame = 0

    def close(v, end_frame):
        n = cur.pop(v, None)
        if n is None:
            return
        n.end_frame = max(n.start_frame + 1, end_frame)
        n.end_t = max(n.start_t + 0.001, n.end_frame * FRAME_SAMPLES / float(DSP_RATE))
        notes.append(n)

    with open(prefix + '.trace', 'r') as f:
        for ln in f:
            p = ln.split()
            if not p:
                continue
            if p[0] == 'K':
                smp, v, srcn, pitch, vl, vr, start, loop = (int(x) for x in p[1:9])
                close(v, smp // FRAME_SAMPLES + 1)
                meta.setdefault(start, loop)
                cur[v] = Note(v, smp, start)
                quiet[v] = 0
            elif p[0] == 'F':
                frame, v, srcn, pitch, env, vl, vr, kon, pmon, non = (int(x) for x in p[1:11])
                max_frame = max(max_frame, frame)
                n = cur.get(v)
                if n is None:
                    continue
                if n.frames and frame - n.frames[-1] > 1:
                    close(v, n.frames[-1] + 1)
                    continue
                if env <= 0 or pitch <= 0 or kon > 0:
                    continue
                if non:
                    n.hash = -1                     # шум вместо сэмпла
                n.env.append(env)
                n.vl.append(vl)
                n.vr.append(vr)
                vl, vr = abs(vl), abs(vr)
                lv = env / 2048.0 * max(vl, vr) / 128.0
                tot = vl + vr
                pan = int(round(255 * vr / tot)) if tot else 128
                n.frames.append(frame)
                n.ratio.append(pitch / 4096.0)
                n.level.append(lv)
                n.pan.append(pan)
                n.peak = max(n.peak, lv)
                n.end_frame = frame + 1
                if lv < max(0.002, release_ratio * n.peak) and len(n.frames) > 1:
                    quiet[v] += 1
                    if quiet[v] >= 2:
                        close(v, frame - 1)
                else:
                    quiet[v] = 0
    for v in list(cur):
        close(v, cur[v].end_frame)
    notes = [n for n in notes if n.frames and n.peak > 0.002]
    notes.sort(key=lambda n: (n.start_t, n.key))
    frame_times = [i * FRAME_SAMPLES / float(DSP_RATE) for i in range(max_frame + 4)]
    return notes, meta, frame_times


def load_streams(prefix):
    """key (адрес начала сэмпла) -> декодированный BRR (int16)."""
    dt = np.dtype([('key', '<i4'), ('off', '<i4'), ('s', '<i2', (4,))])
    rec = np.fromfile(prefix + '.pcm', dtype=dt)
    streams = {}
    for key in np.unique(rec['key']):
        r = rec[rec['key'] == key]
        end = int(r['off'].max()) + 4
        buf = np.zeros(end, dtype=np.int16)
        idx = r['off'][:, None] + np.arange(4)[None, :]
        buf[idx.ravel()] = r['s'].ravel()
        streams[int(key)] = buf
    return streams


def make_sample_data(stream, ram, start, loop_addr):
    """Сэмпл и петля из BRR. Петля берётся с установившегося прохода
    (фильтры BRR на первом проходе ещё не сошлись). -> (pcm, loop_info)"""
    nblocks, loops = brr_extent(ram, start)
    total = nblocks * 16
    if not loops:
        return stream[:total], 'none'
    off = (loop_addr - start) & 0xFFFF
    if off >= 9 * nblocks or off % 9:
        return stream, None                       # петля в другое место - ищем по данным
    ls = off // 9 * 16
    L = total - ls
    if L < 1:
        return stream[:total], 'none'
    passes = (len(stream) - ls) // L              # сколько полных проходов петли есть
    if passes < 1:
        return stream, 'none'
    k = 0
    for cand in (1, 2):
        if passes >= cand + 1 and np.array_equal(stream[ls + cand * L:ls + (cand + 1) * L],
                                                 stream[ls + (cand - 1) * L:ls + cand * L]):
            k = cand
            break
    else:
        k = min(passes - 1, 2)
    return stream[:ls + (k + 1) * L], (ls + k * L, L)


def noise_pcm():
    rng = np.random.RandomState(1)
    return (rng.randint(-1, 1, 8192) * 2 + 1).astype(np.int16) * 12000


# ═══════════════════════════════════════════════════════════════
# Основная конвертация
# ═══════════════════════════════════════════════════════════════

def convert(args):
    in_path = args.input
    spc = read_spc(in_path)
    base = os.path.splitext(args.output or in_path)[0]

    only = args.midi_only or args.wav_only
    want_xm = args.xm or args.all or not only
    want_midi = args.midi or args.all or args.midi_only
    want_wav = args.wav or args.all or args.wav_only
    sel = parse_sample_list(args.samples) if args.samples else None

    seconds = args.seconds or spc.length or 120.0
    title = spc.title or spc.game or os.path.basename(base)

    tracer = find_tracer() if args.tracer in ('auto', 'native') else None
    if args.tracer == 'native' and tracer is None:
        build_tracer()
        tracer = find_tracer()
        if tracer is None:
            raise SystemExit("spctrace не собран (см. spc_tracer/build.sh)")
    if tracer is None:
        print("[spc] эмулятор: встроенный на Python (spc_pyemu.py, медленнее реального времени); "
              "быстрее - собрать C++ GME: --build-tracer")

    work = tempfile.mkdtemp(prefix='spc2xm_')
    prefix = os.path.join(work, 'tr')
    try:
        print(f"[spc] эмуляция {os.path.basename(in_path)}, {seconds:.0f} с ...")
        raw = run_tracer(tracer, in_path, prefix, seconds, want_wav and not sel)
        if args.keep_trace:
            for ext in ('.trace', '.pcm'):
                shutil.copy(prefix + ext, base + '.spctrace' + ext)
        if want_wav and raw:
            wav_path = args.wav_file or base + '.wav'
            x = np.fromfile(raw, dtype='<i2').reshape(-1, 2)
            write_wav(wav_path, x.astype(np.float64))
        if not (want_xm or want_midi or (want_wav and sel)):
            return

        rate = DSP_RATE
        notes, meta, frame_times = parse_trace(prefix, args.release_threshold)
        if not notes:
            raise SystemExit("нот не найдено (пустой или тихий SPC?)")
        print(f"[spc] нот: {len(notes)}, голосов: {len(set(n.key for n in notes))}")

        # ── сэмплы ──
        streams = load_streams(prefix)
        samples = {}
        cnt = collections.Counter(n.hash for n in notes)
        used = [h for h, _ in cnt.most_common()]
        if len(used) > MAX_XM_INSTRUMENTS:
            print(f"[xm] ВНИМАНИЕ: {len(used)} сэмплов > {MAX_XM_INSTRUMENTS}, "
                  f"редкие отброшены", file=sys.stderr)
            used = used[:MAX_XM_INSTRUMENTS]
        first = {}
        for n in notes:
            first.setdefault(n.hash, n.start_t)
        used.sort(key=lambda h: first[h])
        for h in used:
            if h == -1:
                pcm, info = noise_pcm(), (0, 8192)
            else:
                stream = streams.get(h)
                if stream is None or len(stream) < 16:
                    continue
                pcm, info = make_sample_data(stream, spc.ram, h, meta.get(h, 0))
            s = Sample(h, pcm, info)
            if not args.no_gauss:
                s.post = gauss_filter
            s.index = len(samples) + 1
            s.use_count = cnt[h]
            s.analyse()
            s.set_pitch(args.ref_rate if args.ref_rate else rate)
            samples[h] = s
        notes = [n for n in notes if n.hash in samples]
        for h, s in samples.items():
            s.orig = s.index
        # норма громкости - по всем нотам, до фильтра --samples (шкала не зависит от выбора)
        norm_peaks = np.array([n.peak * samples[n.hash].peak / 32768.0 for n in notes])
        if sel:
            print("[spc] сэмплы (номер: адрес, нот, тональный, луп):")
            for h, s in samples.items():
                print(f"       {s.orig:3d}: {h:08x}  {s.use_count:5d}  "
                      f"{'да' if s.pitched else 'нет'}  {'да' if s.loop else 'нет'}"
                      f"{'   <-' if s.orig in sel else ''}")
            keep = {h: s for h, s in samples.items() if s.orig in sel}
            if not keep:
                raise SystemExit(f"--samples: нет таких сэмплов (есть 1..{len(samples)})")
            if want_wav:
                wnotes, _, _ = parse_trace(prefix, 0.0)
                wnotes = [n for n in wnotes if n.hash in keep]
                nsm = int(max(n.end_frame for n in wnotes) * FRAME_SAMPLES) if wnotes else 0
                write_wav(args.wav_file or base + '.wav', render_notes_wav(wnotes, keep, nsm))
            samples = keep
            for i, s in enumerate(samples.values(), 1):
                s.index = i
            notes = [n for n in notes if n.hash in samples]
            if not notes:
                raise SystemExit("--samples: у выбранных сэмплов нет нот")
            if not (want_xm or want_midi):
                return
        for n in notes:
            n.sample = samples[n.hash]
        print(f"[spc] сэмплов: {len(samples)} "
              f"(с лупом: {sum(1 for s in samples.values() if s.loop)}, "
              f"тональных: {sum(1 for s in samples.values() if s.pitched)})")

        # ── подстройка высоты: ноты каждого сэмпла должны попадать в сетку полутонов ──
        total_shift = args.transpose + 12 * args.octave
        by_sample = collections.defaultdict(list)
        for n in notes:
            by_sample[n.hash].append(n)
        for h, s in samples.items():
            fr = []
            for n in by_sample[h]:
                j = len(n.ratio) // 2
                fr.append(12.0 * math.log2(max(n.ratio[j], 1e-4)) + s.m1)
            if fr and s.pitched:
                ang = np.array(fr) * 2 * np.pi
                c, sn = np.cos(ang).mean(), np.sin(ang).mean()
                if math.hypot(c, sn) > 0.5:
                    delta = math.atan2(sn, c) / (2 * np.pi)       # доля полутона
                    s.m1 -= delta
            # сдвиг на октавы, чтобы ноты влезли в 1..96
            lo = min(49 + s.m1 + 12 * math.log2(max(r, 1e-4)) + total_shift
                     for n in by_sample[h] for r in n.ratio)
            hi = max(49 + s.m1 + 12 * math.log2(max(r, 1e-4)) + total_shift
                     for n in by_sample[h] for r in n.ratio)
            fold = 0
            if hi + 0.5 > 96 or lo - 0.5 < 1:
                mid = (lo + hi) / 2
                fold = int(round((mid - 48.5) / 12.0))
            s.fold = fold
            # XM: rate(cell) = 8363 * 2^((cell-49+tune)/12) должен равняться ref_rate * ratio
            ref = args.ref_rate if args.ref_rate else rate
            s.tune = 12.0 * math.log2(ref / XM_C4_RATE) - s.m1 - total_shift + 12 * fold

        # ── громкость ──
        peaks = norm_peaks
        # самая громкая нота = громкость 64; иначе всё, что громче, обрезается и
        # баланс между инструментами (бас/барабаны против пэдов) уезжает
        norm_max = (peaks.max() if len(peaks) else 1.0) or 1.0
        norm = norm_max * args.norm_scale

        # ── сетка строк ──
        onsets = [n.start_t for n in notes]
        t0 = 0.0
        if args.bpm:
            speed = args.speed or 6
            bpm = args.bpm
            delta = 2.5 * speed / bpm
            true_delta = delta
        else:
            true_delta, t0, coh = detect_row_interval(onsets, args.max_row)
            true_delta *= args.compact
            bpm, speed, delta = interval_to_bpm_speed(true_delta)
            if args.speed:
                speed = args.speed
                bpm = max(32, min(255, int(round(2.5 * speed / true_delta))))
                delta = 2.5 * speed / bpm
            print(f"[xm] сетка нот: {true_delta * 1000:.3f} мс/строка, когерентность {coh:.2f}")
        print(f"[xm] строка = {delta * 1000:.1f} мс  (BPM {bpm}, speed {speed})")

        for n in notes:
            n.start_row = int(round((n.start_t - t0) / true_delta))
            n.end_row = int(round((n.end_t - t0) / true_delta))
        shift = min(n.start_row for n in notes)
        for n in notes:
            n.start_row -= shift
            n.end_row = max(n.start_row + 1, n.end_row - shift)
        delta = true_delta                           # индекс кадра ниже считаем по реальному шагу
        row_t0 = t0 + shift * true_delta
        nch = allocate_channels(notes, args.channels)
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
            for ch, lst in by_chan.items():
                lst.sort(key=lambda n: n.start_row)
                for k, n in enumerate(lst):
                    s = n.sample
                    nxt = lst[k + 1].start_row if k + 1 < len(lst) else None
                    end_row = n.end_row if nxt is None else min(n.end_row, nxt)
                    n.end_row = end_row
                    last_row = max(last_row, end_row)

                    def put(row):
                        c = grid.get((row, ch))
                        if c is None:
                            c = grid[(row, ch)] = Cell()
                        return c

                    def vol_at(i):
                        v = soft_limit(n.level[i] * s.peak / 32768.0 / norm,
                                       args.limit_knee) * 64.0
                        return max(1, min(64, int(round(v))))

                    # индекс кадра ноты для времени строки
                    def idx_at(row):
                        t = row_t0 + row * delta
                        fr = int(round(np.searchsorted(frame_times, t)))
                        return max(0, min(len(n.frames) - 1, fr - n.start_frame))

                    f0 = cell_float(n, 0, s, total_shift - 12 * s.fold)
                    cur_cell = int(round(f0))
                    cur_cell = max(1, min(96, cur_cell))
                    c0 = put(n.start_row)
                    c0.note = cur_cell
                    c0.inst = s.index
                    ci = next(i for i, l in enumerate(n.level) if l >= 0.5 * n.peak)
                    last_vol = vol_at(idx_at(n.start_row))
                    c0.vol = 0x10 + last_vol
                    last_pan = n.pan[ci]
                    if not args.no_pan and last_pan != 128:
                        c0.eff, c0.par = 0x08, max(1, min(255, last_pan))
                    last_pitch = f0
                    for row in range(n.start_row + 1, end_row):
                        i = idx_at(row)
                        v = vol_at(i)
                        pf = cell_float(n, i, s, total_shift - 12 * s.fold)
                        c = None
                        if abs(v - last_vol) >= max(2, int(0.08 * last_vol)):
                            c = put(row)
                            c.vol = 0x10 + v
                            last_vol = v
                        if abs(pf - last_pitch) >= args.bend_threshold:
                            c = c or put(row)
                            nc = max(1, min(96, int(round(pf))))
                            if nc != cur_cell:
                                c.note = nc
                                c.eff, c.par = 0x03, 0xFF
                                cur_cell = nc
                            last_pitch = pf
                    # note off
                    if nxt is None or end_row < nxt:
                        if end_row <= last_row:
                            off = put(end_row)
                            if not off.note:
                                off.note = 97

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
            for h, s in sorted(samples.items(), key=lambda kv: kv[1].index):
                data = s.data.astype(np.float64) * (32767.0 / s.peak)
                data = np.clip(np.round(data), -32768, 32767).astype(np.int16)
                rel = int(round(s.tune))
                rel = max(-96, min(95, rel))
                fine = int(round((s.tune - rel) * 128))
                fine = max(-128, min(127, fine))
                nm = f"S{s.orig:02d} {s.hash:08x}"
                xm.instruments.append({'name': nm, 'data': data, 'loop': s.loop,
                                       'rel': rel, 'finetune': fine})
            xm_path = base + '.xm'
            xm.write(xm_path)
            print(f"[xm] {xm_path}  ({len(xm.patterns)} паттернов, "
                  f"{len(xm.instruments)} инструментов)")

        # ── MIDI ──
        if want_midi:
            ev = []
            order = sorted([s for s in samples.values() if not s.pitched],
                           key=lambda s: s.centroid)
            drum_of = {s.hash: DRUM_NOTES[min(len(DRUM_NOTES) - 1,
                                              i * len(DRUM_NOTES) // max(1, len(order)))]
                       for i, s in enumerate(order)}
            midi_ch_of = {}
            ch_prog = {}
            ch_list = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]
            init_done = set()
            tps = 960.0                                  # 120 bpm * 480 tpqn
            for n in sorted(notes, key=lambda n: n.start_t):
                s = n.sample
                t0 = int(round(n.start_t * tps))
                t1 = max(t0 + 20, int(round(n.end_t * tps)))
                if not s.pitched:
                    mch = 9
                    key = drum_of[s.hash]
                else:
                    mch = ch_list[n.channel % len(ch_list)]
                    f0 = 49 + s.m1 + 12 * math.log2(max(n.ratio[0], 1e-4))
                    key = int(round(f0 + 11)) + args.transpose + 12 * args.octave
                    key = max(0, min(127, key))
                if mch not in init_done:
                    init_done.add(mch)
                    # диапазон pitch bend = 12 полутонов
                    ev += [(0, 0, mch, bytes([0xB0 | mch, 101, 0])),
                           (0, 0, mch, bytes([0xB0 | mch, 100, 0])),
                           (0, 0, mch, bytes([0xB0 | mch, 6, 12])),
                           (0, 0, mch, bytes([0xB0 | mch, 38, 0]))]
                if mch != 9:
                    prog = GM_PROGRAMS[(s.index - 1) % len(GM_PROGRAMS)]
                    if ch_prog.get(mch) != prog:
                        ev.append((t0, 1, mch, bytes([0xC0 | mch, prog])))
                        ch_prog[mch] = prog
                pk = n.peak * s.peak / 32768.0 / norm_max
                vel = max(1, min(127, int(round(pk * 110))))
                ev.append((t0, 2, mch, bytes([0xB0 | mch, 10, max(0, min(127, n.pan[0] // 2))])))
                ev.append((t0, 3, mch, bytes([0x90 | mch, key, vel])))
                if s.pitched:
                    base_f = 49 + s.m1 + 12 * math.log2(max(n.ratio[0], 1e-4))
                    last_b = None
                    last_e = None
                    for i, fr in enumerate(n.frames):
                        t = int(round(frame_times[min(fr, len(frame_times) - 1)] * tps))
                        if t >= t1:
                            break
                        pf = 49 + s.m1 + 12 * math.log2(max(n.ratio[i], 1e-4))
                        bend = max(-12.0, min(12.0, pf - (key - 11 - args.transpose - 12 * args.octave)))
                        if last_b is None or abs(bend - last_b) >= 0.06:
                            v = int(8192 + bend / 12.0 * 8191)
                            ev.append((t, 2, mch, bytes([0xE0 | mch, v & 0x7F, (v >> 7) & 0x7F])))
                            last_b = bend
                        e = max(1, min(127, int(round(n.level[i] / max(n.peak, 1e-6) * 127))))
                        if last_e is None or abs(e - last_e) >= 4:
                            ev.append((t, 2, mch, bytes([0xB0 | mch, 11, e])))
                            last_e = e
                ev.append((t1, 0, mch, bytes([0x80 | mch, key, 0])))
            midi_path = base + '.mid'
            write_midi(midi_path, title, ev)
            print(f"[mid] {midi_path}")
    finally:
        if args.keep_work:
            print(f"[spc] рабочий каталог: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="SPC (SNES) -> XM / MIDI / WAV")
    ap.add_argument('input', nargs='?', help='файл .spc')
    ap.add_argument('-o', '--output', help='базовое имя выходных файлов')
    ap.add_argument('--xm', action='store_true', help='записать XM (по умолчанию, если не выбрано другое)')
    ap.add_argument('--midi', action='store_true', help='XM + MIDI')
    ap.add_argument('--midi-only', action='store_true', help='только MIDI')
    ap.add_argument('--wav', action='store_true', help='+ WAV (рендер эмулятора GME, 44.1 кГц стерео)')
    ap.add_argument('--wav-only', action='store_true', help='только WAV')
    ap.add_argument('--wav-file', help='имя WAV файла')
    ap.add_argument('--all', action='store_true', help='XM + MIDI + WAV')
    ap.add_argument('--seconds', '--duration', type=float, dest='seconds',
                    help='сколько секунд эмулировать (по умолчанию - длина из ID666, иначе 120)')
    ap.add_argument('--octave', type=int, default=0, help='сдвиг имён нот в октавах (звук не меняется)')
    ap.add_argument('--transpose', type=int, default=0, help='сдвиг имён нот в полутонах (звук не меняется)')
    ap.add_argument('--compact', type=int, default=1, help='сделать строки в N раз крупнее (меньше строк)')
    ap.add_argument('--max-row', type=float, default=0.06,
                    help='максимальная длина строки в секундах (меньше - точнее огибающие, больше паттернов)')
    ap.add_argument('--bpm', type=int, help='задать BPM вручную')
    ap.add_argument('--speed', type=int, help='задать speed вручную')
    ap.add_argument('--channels', type=int, default=MAX_XM_CHANNELS, help='максимум каналов XM (<=32)')
    ap.add_argument('--release-threshold', type=float, default=0.04,
                    help='нота кончается, когда громкость < доли от пика (по умолчанию 0.04)')
    ap.add_argument('--bend-threshold', type=float, default=0.75,
                    help='порог (полутона), после которого внутри ноты делается глиссандо 3FF')
    ap.add_argument('--norm-scale', type=float, default=1.0,
                    help='громкость XM: 1.0 (по умолчанию) - самая громкая нота = 64, баланс '
                         'точный, без искажений; плеер при этом играет заметно тише WAV '
                         '(у OpenMPT/libopenmpt нота на 64 даёт ~0.2 от полной шкалы) - '
                         'поднимайте pre-amp плеера. Меньше 1.0 - громче, но громкие ноты '
                         'сжимаются (см. --limit-knee)')
    ap.add_argument('--limit-knee', type=float, default=0.6,
                    help='с какой доли громкости 64 начинается мягкое ограничение (1.0 - выкл., '
                         'жёсткое обрезание на 64)')
    ap.add_argument('--samples',
                    help='оставить только эти сэмплы (номера инструментов XM из полного прогона, '
                         'напр. 5 или 1,3,5-7): XM, MIDI и WAV будут только с ними. '
                         'WAV в этом режиме синтезируется по трассе (без эха и мастер-громкости)')
    ap.add_argument('--no-gauss', action='store_true',
                    help='не применять ФНЧ гауссовой интерполяции DSP к сэмплам')
    ap.add_argument('--no-pan', action='store_true', help='не писать панораму (8xx)')
    ap.add_argument('--ref-rate', type=float, help='опорная частота DSP в Гц (по умолчанию 32000)')
    ap.add_argument('--keep-trace', action='store_true', help='сохранить .spctrace.trace/.pcm рядом с результатом')
    ap.add_argument('--keep-work', action='store_true', help='не удалять временный каталог')
    ap.add_argument('--tracer', choices=('auto', 'native', 'python'), default='auto',
                    help='эмулятор: auto - C++ spctrace, если собран, иначе встроенный Python; '
                         'native - всегда spctrace (соберёт); python - всегда встроенный')
    ap.add_argument('--build-tracer', action='store_true', help='собрать spc_tracer/spctrace (C++ GME, быстрее) и выйти')
    args = ap.parse_args()
    args.channels = max(2, min(MAX_XM_CHANNELS, args.channels))
    args.compact = max(1, args.compact)
    if args.build_tracer:
        build_tracer()
        return
    if not args.input or not os.path.isfile(args.input):
        ap.error(f"нужен файл .spc: {args.input or ''}")
    convert(args)


if __name__ == '__main__':
    main()
