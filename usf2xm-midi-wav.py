#!/usr/bin/env python3
"""
USF / miniusf (Nintendo 64) -> XM / MIDI / WAV

Как это работает (аналог spc2xm: там эмулятор SPC700+DSP логирует регистры DSP,
здесь эмулятор N64 логирует команды аудио-микрокода RSP):

  1. usf_tracer/usftrace (lazyusf2 + патч) запускает .miniusf (+ .usflib) и пишет:
       * trace  - команды alist (ABI1): ADPCM / RESAMPLE / ENVMIXER каждого голоса
                  на каждом 5-мс кадре (аналог KON/PITCH/VOL регистров SNES DSP)
       * pcm    - уже декодированные VADPCM-сэмплы (то, что слышно "до" ресемплера)
       * raw    - готовый рендер звука для WAV
  2. Этот скрипт превращает лог в ноты (старт голоса = ADPCM с флагом INIT,
     высота = pitch ресемплера, громкость/панорама = ENVMIXER), сам вырезает
     сэмплы (с поиском точки лупа и определением тона) и пишет XM / MIDI / WAV.

Поддерживается стандартный libultra ABI1 (большинство игр). Игры с MusyX/ABI2/ABI3
пока не поддерживаются (скрипт сообщит об этом).

Использование:
    python3 usf2xm-midi-wav.py sparse00.miniusf
    python3 usf2xm-midi-wav.py sparse00.miniusf --midi --seconds 90
    python3 usf2xm-midi-wav.py sparse00.miniusf --all --compact 2
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
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TRACER_DIR = os.path.join(HERE, 'usf_tracer')

C4_HZ = 261.6256
XM_C4_RATE = 8363.0
MAX_XM_CHANNELS = 32
MAX_XM_INSTRUMENTS = 128


# ═══════════════════════════════════════════════════════════════
# PSF-теги (title / game / length ...)
# ═══════════════════════════════════════════════════════════════

def parse_time(s):
    try:
        parts = [float(x.replace(',', '.')) for x in s.strip().split(':')]
    except ValueError:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def read_psf_tags(path):
    tags = {}
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if data[:4] != b'PSF\x21':
            return tags
        res, exe = struct.unpack_from('<II', data, 4)
        tail = data[16 + res + exe:]
        if tail[:5] == b'[TAG]':
            for line in tail[5:].decode('utf-8', 'replace').splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    tags.setdefault(k.strip().lower(), v.strip())
    except OSError:
        pass
    return tags


# ═══════════════════════════════════════════════════════════════
# Запуск эмулятора-трассировщика
# ═══════════════════════════════════════════════════════════════

def find_tracer():
    cands = [os.environ.get('USFTRACE', ''),
             os.path.join(TRACER_DIR, 'usftrace'),
             shutil.which('usftrace') or '']
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def build_tracer():
    script = os.path.join(TRACER_DIR, 'build.sh')
    print(f"[usf] сборка трассировщика: {script}")
    subprocess.run(['bash', script], check=True)


def run_tracer(tracer, usf_path, prefix, seconds, want_raw):
    usf_path = os.path.abspath(usf_path)
    if tracer is None:                      # встроенный эмулятор на Python
        import usf_pyemu
        usf_pyemu.trace(usf_path, prefix, seconds, prefix + '.raw' if want_raw else None)
        return prefix + '.raw' if want_raw else None
    cmd = [tracer, os.path.basename(usf_path), prefix, str(seconds)]
    raw = prefix + '.raw'
    if want_raw:
        cmd.append(raw)
    # usflib ищется рядом с файлом (тег _lib), поэтому cwd = каталог файла
    res = subprocess.run(cmd, cwd=os.path.dirname(usf_path),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        sys.stderr.write(res.stderr.decode('utf-8', 'replace'))
        raise SystemExit(f"usftrace завершился с кодом {res.returncode} "
                         f"(нет .usflib рядом с файлом?)")
    log = res.stderr.decode('utf-8', 'replace')
    if 'File closed' not in log and 'Done' not in log:
        sys.stderr.write(log)
    return raw if want_raw else None


# ═══════════════════════════════════════════════════════════════
# Разбор лога
# ═══════════════════════════════════════════════════════════════

class VoiceFrame:
    __slots__ = ('frame', 'key', 'init', 'has_adpcm', 'hash', 'cum', 'ratio',
                 'left', 'right', 'dry')


def parse_trace(prefix):
    rate = 32000
    ucode = None
    lines = []
    with open(prefix + '.trace', 'r') as f:
        for ln in f:
            lines.append(ln.split())

    for p in lines:
        if p and p[0] == '#':
            if p[1] == 'rate':
                rate = int(p[2])
            elif p[1] == 'ucode':
                ucode = ' '.join(p[2:])

    # Размер одного кадра вывода = самый большой SAVEBUFF (стерео-буфер)
    frame_bytes = 0
    for p in lines:
        if p and p[0] == 'S':
            frame_bytes = max(frame_bytes, int(p[2]))
    if frame_bytes == 0:
        return rate, ucode, [], [0.0], 0

    frame_times = [0.0]
    samples = 0
    voices = []
    cur_a = None
    cur_r = None
    frame = 0
    for p in lines:
        if not p:
            continue
        t = p[0]
        if t == 'A':
            cur_a = p
        elif t == 'R':
            cur_r = p
        elif t == 'E':
            if cur_r is not None:
                vf = VoiceFrame()
                vf.frame = frame
                vf.key = int(cur_r[3], 16)
                rflags = int(cur_r[1])
                vf.ratio = int(cur_r[2]) * 2 / 65536.0
                vf.has_adpcm = cur_a is not None
                if cur_a is not None:
                    vf.init = bool(int(cur_a[1]) & 1)
                    vf.hash = int(cur_a[7], 16)
                    vf.cum = int(cur_a[8])
                else:
                    vf.init = bool(rflags & 1)
                    vf.hash = None
                    vf.cum = 0
                vol0, vol1, tg0, tg1 = (abs(int(x)) for x in p[5:9])
                vf.left = max(vol0, tg0) / 32768.0
                vf.right = max(vol1, tg1) / 32768.0
                vf.dry = abs(int(p[11])) / 32768.0
                voices.append(vf)
            cur_a = None
            cur_r = None
        elif t == 'S':
            if int(p[2]) == frame_bytes:
                samples += frame_bytes // 4
                frame += 1
                frame_times.append(samples / float(rate))
    # хвост лога - ненабранный кадр игнорируем
    return rate, ucode, voices, frame_times, frame_bytes // 4


def load_pcm_chunks(prefix):
    """hash -> список (cum, offset, count_bytes) из trace; сам pcm читаем лениво."""
    chunks = collections.defaultdict(list)
    with open(prefix + '.trace', 'r') as f:
        for ln in f:
            if ln[0] != 'A' or ln[1] != ' ':
                continue
            p = ln.split()
            off = int(p[9])
            if off >= 0:
                chunks[int(p[7], 16)].append((int(p[8]), off, int(p[4])))
    pcm = np.fromfile(prefix + '.pcm', dtype='<i2')
    streams = {}
    for h, lst in chunks.items():
        end = max(c + n for c, _, n in lst) // 2
        buf = np.zeros(end, dtype=np.int16)
        for cum, off, n in lst:
            seg = pcm[off // 2: off // 2 + n // 2]
            buf[cum // 2: cum // 2 + len(seg)] = seg
        streams[h] = buf
    return streams


# ═══════════════════════════════════════════════════════════════
# Ноты
# ═══════════════════════════════════════════════════════════════

class Note:
    def __init__(self, key, start_frame, h):
        self.key = key
        self.start_frame = start_frame
        self.end_frame = start_frame + 1
        self.hash = h
        self.frames = []       # номера кадров
        self.ratio = []        # pitch/32768
        self.level = []        # 0..1
        self.pan = []          # 0..255
        self.peak = 0.0
        # заполняются позже
        self.start_t = 0.0
        self.end_t = 0.0
        self.sample = None
        self.retrig = False
        self.cum = 0
        self.channel = 0
        self.start_row = 0
        self.end_row = 0


def frame_level_pan(vf):
    lv = max(vf.left, vf.right) * vf.dry
    tot = vf.left + vf.right
    pan = int(round(255 * vf.right / tot)) if tot > 0 else 128
    return lv, pan


def build_notes(voices, release_ratio):
    """Превращает покадровые записи голосов в ноты.

    Нота начинается:
      * с ADPCM-кадра с флагом INIT (голос запущен заново), либо
      * с нового нарастания огибающей на голосе, чей сэмпл продолжает играть
        по лупу (многие драйверы не перезапускают сэмпл, а только огибающую)."""
    by_key = collections.defaultdict(list)
    for vf in voices:
        by_key[vf.key].append(vf)

    notes = []
    skipped_pcm = 0
    for key, lst in by_key.items():
        cur = None
        quiet = 0
        last_hash = None          # сэмпл, который сейчас крутится на голосе
        pending = False           # голос запущен, а сэмпл ещё не появился
        valley = run_peak = 0.0
        for vf in lst:
            lv, pan = frame_level_pan(vf)
            if vf.has_adpcm and vf.hash is not None:
                last_hash = vf.hash
                pending = False
            start = None             # (hash, retrig)
            if vf.init:
                if cur is not None:
                    cur.end_frame = vf.frame
                    notes.append(cur)
                    cur = None
                if pending:
                    skipped_pcm += 1
                if vf.hash is None:
                    pending = True
                    last_hash = None
                else:
                    start = (vf.hash, False)
            elif last_hash is not None:
                if cur is None:
                    if lv >= 0.02:
                        start = (last_hash, True)
                elif lv > valley * 2.5 + 0.02 and valley < 0.6 * run_peak:
                    cur.end_frame = vf.frame
                    notes.append(cur)
                    cur = None
                    start = (last_hash, True)
            if start is not None:
                cur = Note(key, vf.frame, start[0])
                cur.retrig = start[1]
                cur.cum = vf.cum
                quiet = 0
                valley = run_peak = lv
            if cur is None:
                continue
            cur.frames.append(vf.frame)
            cur.ratio.append(vf.ratio)
            cur.level.append(lv)
            cur.pan.append(pan)
            cur.peak = max(cur.peak, lv)
            cur.end_frame = vf.frame + 1
            if lv > run_peak:
                run_peak = lv
                valley = lv
            else:
                valley = min(valley, lv)
            # затухание -> конец ноты
            if lv < max(0.002, release_ratio * cur.peak) and len(cur.frames) > 1:
                quiet += 1
                if quiet >= 2:
                    cur.end_frame = vf.frame - 1
                    if cur.end_frame <= cur.start_frame:
                        cur.end_frame = cur.start_frame + 1
                    notes.append(cur)
                    cur = None
                    valley = run_peak = 0.0
            else:
                quiet = 0
        if pending:
            skipped_pcm += 1
        if cur is not None:
            notes.append(cur)
    notes = [n for n in notes if n.peak > 0.002]
    notes.sort(key=lambda n: (n.start_frame, n.key))
    return notes, skipped_pcm


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
    def __init__(self, h, pcm):
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

    def analyse(self):
        s = self.data
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
# Основная конвертация
# ═══════════════════════════════════════════════════════════════

def convert(args):
    in_path = args.input
    tags = read_psf_tags(in_path)
    base = os.path.splitext(args.output or in_path)[0]

    only = args.midi_only or args.wav_only
    want_xm = args.xm or args.all or not only
    want_midi = args.midi or args.all or args.midi_only
    want_wav = args.wav or args.all or args.wav_only

    seconds = args.seconds
    if seconds is None:
        ln = parse_time(tags.get('length', ''))
        fd = parse_time(tags.get('fade', '')) or 0
        seconds = (ln + fd) if ln else 120.0
    title = tags.get('title') or tags.get('game') or os.path.basename(base)

    tracer = find_tracer() if args.tracer in ('auto', 'native') else None
    if args.tracer == 'native' and tracer is None:
        build_tracer()
        tracer = find_tracer()
        if tracer is None:
            raise SystemExit("usftrace не собран (см. usf_tracer/build.sh)")
    if tracer is None:
        print("[usf] эмулятор: встроенный на Python (usf_pyemu.py, ~2.5x медленнее реального времени, "
              "только аудио-микрокод ABI1); быстрее и шире - C++ lazyusf2: --build-tracer")

    work = tempfile.mkdtemp(prefix='usf2xm_')
    prefix = os.path.join(work, 'tr')
    try:
        print(f"[usf] эмуляция {os.path.basename(in_path)}, {seconds:.0f} с ...")
        raw = run_tracer(tracer, in_path, prefix, seconds, want_wav)
        if args.keep_trace:
            for ext in ('.trace', '.pcm'):
                shutil.copy(prefix + ext, base + '.usftrace' + ext)

        rate, ucode, voices, frame_times, frame_samples = parse_trace(prefix)
        if want_wav and raw:
            wav_path = (args.wav_file or base + '.wav')
            with open(raw, 'rb') as f:
                pcm = f.read()
            with wave.open(wav_path, 'wb') as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(pcm)
            print(f"[wav] {wav_path}")
        if not (want_xm or want_midi):
            return

        if not voices:
            raise SystemExit(
                f"В логе нет голосов ADPCM/ENVMIXER. Микрокод игры: {ucode}. "
                f"Поддерживается только стандартный ABI1 (libultra).")
        print(f"[usf] {rate} Гц, кадр {frame_samples} сэмплов "
              f"({1000.0 * frame_samples / rate:.2f} мс), записей голосов: {len(voices)}")

        notes, skipped = build_notes(voices, args.release_threshold)
        if skipped:
            print(f"[usf] пропущено {skipped} запусков голосов без сэмпла (нет данных)")
        if not notes:
            raise SystemExit("нот не найдено")
        for n in notes:
            n.start_t = frame_times[min(n.start_frame, len(frame_times) - 1)]
            n.end_t = frame_times[min(n.end_frame, len(frame_times) - 1)]
        print(f"[usf] нот: {len(notes)}, голосов-слотов: {len(set(n.key for n in notes))}")

        # ── сэмплы ──
        streams = load_pcm_chunks(prefix)
        samples = {}
        cnt = collections.Counter(n.hash for n in notes)
        used = [h for h, _ in cnt.most_common()]
        if len(used) > MAX_XM_INSTRUMENTS:
            print(f"[xm] ВНИМАНИЕ: {len(used)} сэмплов > {MAX_XM_INSTRUMENTS}, "
                  f"редкие отброшены", file=sys.stderr)
            used = used[:MAX_XM_INSTRUMENTS]
        # порядок инструментов - по первому появлению
        first = {}
        for n in notes:
            first.setdefault(n.hash, n.start_frame)
        used.sort(key=lambda h: first[h])
        for i, h in enumerate(used):
            pcm = streams.get(h)
            if pcm is None or len(pcm) < 16:
                continue
            s = Sample(h, pcm)
            s.index = len(samples) + 1
            s.use_count = cnt[h]
            s.analyse()
            s.set_pitch(args.ref_rate if args.ref_rate else rate)
            samples[h] = s
        notes = [n for n in notes if n.hash in samples]
        for n in notes:
            n.sample = samples[n.hash]
        print(f"[usf] сэмплов: {len(samples)} "
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
        peaks = np.array([n.peak * n.sample.peak / 32768.0 for n in notes])
        norm = np.percentile(peaks, 98) if len(peaks) else 1.0
        norm = norm or 1.0

        # ── сетка строк ──
        onsets = [n.start_t for n in notes]
        t0 = 0.0
        if args.bpm:
            speed = args.speed or 6
            bpm = args.bpm
            delta = 2.5 * speed / bpm
            true_delta = delta
        else:
            true_delta, t0, coh = detect_row_interval(onsets)
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
                        v = n.level[i] * s.peak / 32768.0 / norm * 64.0
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
                    if not args.no_pan:
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
                nm = f"S{s.index:02d} {s.hash:08x}"
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
                pk = n.peak * s.peak / 32768.0 / norm
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
            print(f"[usf] рабочий каталог: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(
        description="Nintendo 64 (USF/miniusf + usflib) -> XM / MIDI / WAV")
    ap.add_argument('input', nargs='?', help='файл .miniusf или .usf (рядом должен лежать .usflib)')
    ap.add_argument('-o', '--output', help='базовое имя выходных файлов')
    ap.add_argument('--xm', action='store_true', help='записать XM (по умолчанию, если не выбрано другое)')
    ap.add_argument('--midi', action='store_true', help='XM + MIDI')
    ap.add_argument('--midi-only', action='store_true', help='только MIDI')
    ap.add_argument('--wav', action='store_true', help='+ WAV (рендер эмулятора, 44.1 кГц стерео)')
    ap.add_argument('--wav-only', action='store_true', help='только WAV')
    ap.add_argument('--wav-file', help='имя WAV файла')
    ap.add_argument('--all', action='store_true', help='XM + MIDI + WAV')
    ap.add_argument('--seconds', type=float, help='сколько секунд эмулировать (по умолчанию length+fade из тегов, иначе 120)')
    ap.add_argument('--octave', type=int, default=0, help='сдвиг имён нот в октавах (звук не меняется)')
    ap.add_argument('--transpose', type=int, default=0, help='сдвиг имён нот в полутонах (звук не меняется)')
    ap.add_argument('--compact', type=int, default=1, help='сделать строки в N раз крупнее (меньше строк)')
    ap.add_argument('--bpm', type=int, help='задать BPM вручную')
    ap.add_argument('--speed', type=int, help='задать speed вручную')
    ap.add_argument('--channels', type=int, default=MAX_XM_CHANNELS, help='максимум каналов XM (<=32)')
    ap.add_argument('--release-threshold', type=float, default=0.04,
                    help='нота кончается, когда громкость < доли от пика (по умолчанию 0.04)')
    ap.add_argument('--bend-threshold', type=float, default=0.75,
                    help='порог (полутона), после которого внутри ноты делается глиссандо 3FF')
    ap.add_argument('--no-pan', action='store_true', help='не писать панораму (8xx)')
    ap.add_argument('--ref-rate', type=float, help='опорная частота ресемплера в Гц (по умолчанию - частота DAC)')
    ap.add_argument('--keep-trace', action='store_true', help='сохранить .usftrace.trace/.pcm рядом с результатом')
    ap.add_argument('--keep-work', action='store_true', help='не удалять временный каталог')
    ap.add_argument('--tracer', choices=('auto', 'native', 'python'), default='auto',
                    help='эмулятор: auto - C++ usftrace, если собран, иначе встроенный Python; '
                         'native - всегда usftrace (соберёт); python - всегда встроенный')
    ap.add_argument('--build-tracer', action='store_true', help='собрать usf_tracer/usftrace (C++ lazyusf2, быстрее и шире) и выйти')
    args = ap.parse_args()
    args.channels = max(2, min(MAX_XM_CHANNELS, args.channels))
    args.compact = max(1, args.compact)
    if args.build_tracer:
        build_tracer()
        return
    if not args.input:
        ap.error('нужен входной файл')
    if not os.path.isfile(args.input):
        raise SystemExit(f"нет файла: {args.input}")
    convert(args)


if __name__ == '__main__':
    main()
