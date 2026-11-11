#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mmpz_export.py — XM -> проект LMMS (.mmpz)

Общий модуль для всех *2xm-midi-wav.py: берёт готовый XM (его пишут все конвертеры)
и собирает из него проект LMMS 1.2+ (AudioFileProcessor + ноты + автоматизация).
Только numpy, LMMS для работы не нужен.

Соответствие XM -> LMMS
  * каждый инструмент XM (сэмпл) = инструмент AudioFileProcessor; одновременно звучащие
    ноты одного инструмента раскладываются по нескольким дорожкам ("дорожка" = полоса,
    на которой ноты идут строго друг за другом);
  * ноты XM -> ноты в Piano Roll (XM C-4 = ключ 60); сэмпл заранее приводится к 44100 Гц
    так, чтобы ключ 60 звучал как XM C-4 (relative note и finetune учтены), поэтому
    высота совпадает с XM;
  * громкость (колонка громкости, Cxx) и панорама (8xx) -> автоматизация громкости и
    панорамы дорожки (в LMMS 1.2 у отдельной ноты нет огибающей громкости);
  * плавные смены высоты (3xx) -> автоматизация detuning внутри ноты (без новой атаки);
  * темп: общий BPM проекта подбирается так, чтобы тик LMMS был заметно короче строки XM,
    а позиции нот считаются по реальному времени (смены темпа Fxx учтены);
  * петли сэмплов сохраняются; длина петли делается целой в кадрах, остаток высоты
    компенсируется подстройкой Pitch дорожки.

Сэмплы по умолчанию кладутся рядом в каталог <имя>_samples/*.wav (src= абсолютный путь):
это работает везде, в том числе в `lmms -r`. Встраивание (--mmpz-embed, sampledata=) делает
проект одним файлом, но LMMS 1.2.2 теряет встроенные сэмплы при экспорте через `lmms -r`.

Запуск:  python3 mmpz_export.py song.xm [song.mmpz] [--embed] [--bpm N]
"""
import base64
import math
import os
import struct
import sys
import wave
import zlib
from xml.sax.saxutils import quoteattr

import numpy as np

import xm_export

LMMS_RATE = 44100
TICKS_PER_BEAT = 48                   # 192 тиков на такт 4/4
XM_C4_RATE = 8363.0                   # частота воспроизведения C-4 в XM при relative note = 0


# ═══════════════════════════════════════════════════════════════
# Чтение XM
# ═══════════════════════════════════════════════════════════════

class XmSample:
    def __init__(self):
        self.name = ''
        self.data = np.zeros(0)       # float, -1..1
        self.loop_start = 0
        self.loop_len = 0
        self.loop_type = 0            # 0 нет, 1 вперёд, 2 пинг-понг
        self.volume = 64
        self.finetune = 0
        self.pan = 128
        self.rel = 0


class XmModule:
    def __init__(self):
        self.title = ''
        self.nch = 0
        self.speed = 6
        self.bpm = 125
        self.order = []
        self.patterns = []            # [паттерн][строка] -> список ячеек (note, inst, vol, eff, par)
        self.instruments = {}         # номер (с 1) -> (имя, XmSample)


def _unpack_pattern(raw, nrows, nch):
    rows = []
    empty = (0, 0, 0, 0, 0)
    p = 0
    n = len(raw)
    for _ in range(nrows):
        row = []
        for _ in range(nch):
            if p >= n:
                row.append(empty)
                continue
            b = raw[p]
            p += 1
            note = inst = vol = eff = par = 0
            if b & 0x80:
                if b & 1:
                    note = raw[p]; p += 1
                if b & 2:
                    inst = raw[p]; p += 1
                if b & 4:
                    vol = raw[p]; p += 1
                if b & 8:
                    eff = raw[p]; p += 1
                if b & 16:
                    par = raw[p]; p += 1
            else:
                note = b
                inst, vol, eff, par = raw[p], raw[p + 1], raw[p + 2], raw[p + 3]
                p += 4
            row.append((note, inst, vol, eff, par))
        rows.append(row)
    return rows


def read_xm(path):
    with open(path, 'rb') as f:
        d = f.read()
    if d[:17] != b'Extended Module: ':
        raise ValueError('это не XM: ' + path)
    xm = XmModule()
    xm.title = d[17:37].split(b'\0')[0].decode('latin1').strip()
    hsize = struct.unpack_from('<I', d, 60)[0]
    songlen, _restart, nch, npat, ninst, _flags, speed, bpm = struct.unpack_from('<8H', d, 64)
    xm.nch, xm.speed, xm.bpm = nch, speed or 6, bpm or 125
    xm.order = list(d[80:80 + 256])[:songlen]
    p = 60 + hsize
    for _ in range(npat):
        phl, _packing, nrows, psize = struct.unpack_from('<IBHH', d, p)
        p += phl
        xm.patterns.append(_unpack_pattern(d[p:p + psize], nrows, nch))
        p += psize
    for i in range(1, ninst + 1):
        ihs = struct.unpack_from('<I', d, p)[0]
        name = d[p + 4:p + 26].split(b'\0')[0].decode('latin1').strip()
        ns = struct.unpack_from('<H', d, p + 27)[0]
        q = p + ihs
        if ns == 0:
            p = q
            continue
        shs = struct.unpack_from('<I', d, p + 29)[0]
        heads = []
        for k in range(ns):
            h = d[q:q + shs]
            length, ls, ll = struct.unpack_from('<III', h, 0)
            vol, fine, typ, pan, rel = struct.unpack_from('<BbBBb', h, 12)
            heads.append((length, ls, ll, vol, fine, typ, pan, rel, h[18:40]))
            q += shs
        first = None
        for (length, ls, ll, vol, fine, typ, pan, rel, sname) in heads:
            raw = d[q:q + length]
            q += length
            s = XmSample()
            s.name = sname.split(b'\0')[0].decode('latin1').strip()
            if typ & 0x10:
                arr = np.frombuffer(raw[:len(raw) // 2 * 2], dtype='<i2').astype(np.int64)
                arr = ((np.cumsum(arr) + 32768) & 0xFFFF) - 32768
                s.data = arr / 32768.0
                s.loop_start, s.loop_len = ls // 2, ll // 2
            else:
                arr = np.frombuffer(raw, dtype=np.int8).astype(np.int64)
                arr = ((np.cumsum(arr) + 128) & 0xFF) - 128
                s.data = arr / 128.0
                s.loop_start, s.loop_len = ls, ll
            s.loop_type = typ & 3
            s.volume, s.finetune, s.pan, s.rel = vol, fine, pan, rel
            if first is None:
                first = s
        if first is not None and len(first.data):
            xm.instruments[i] = (name or first.name or ('S%02d' % i), first)
        p = q
    return xm


# ═══════════════════════════════════════════════════════════════
# Временная шкала и ноты
# ═══════════════════════════════════════════════════════════════

class Note:
    def __init__(self, inst, key, t, vol, pan):
        self.inst = inst
        self.key = key                # ключ XM (1..96) в момент старта
        self.t0 = t
        self.t1 = None
        self.vol = [(t, vol)]         # (время, 0..64)
        self.pan = [(t, pan)]         # (время, 0..255)
        self.bends = []               # (время, ключ XM)
        self.lane = 0


def build_notes(xm):
    """Проходит по порядку паттернов и возвращает (ноты, длительность, минимальная длина строки)."""
    speed, bpm = xm.speed, xm.bpm
    t = 0.0
    chan = [None] * xm.nch            # текущая нота канала
    last_inst = [0] * xm.nch
    chan_vol = [64] * xm.nch
    chan_pan = [128] * xm.nch
    notes = []
    min_row = None
    for o in xm.order:
        if o >= len(xm.patterns):
            continue
        for row in xm.patterns[o]:
            for c in row:
                if c[3] == 0x0F and c[4]:
                    if c[4] < 32:
                        speed = c[4]
                    else:
                        bpm = c[4]
            for ch, (note, inst, vcol, eff, par) in enumerate(row):
                porta = (eff == 3)
                if inst:
                    last_inst[ch] = inst
                if note == 97:
                    cur = chan[ch]
                    if cur is not None:
                        cur.t1 = t
                        chan[ch] = None
                elif 1 <= note <= 96:
                    cur = chan[ch]
                    ins = xm.instruments.get(last_inst[ch])
                    if porta and cur is not None:
                        if not cur.bends or cur.bends[-1][1] != note:
                            cur.bends.append((t, note))
                    elif ins is not None:
                        if cur is not None:
                            cur.t1 = t
                        smp = ins[1]
                        if inst:
                            chan_vol[ch] = smp.volume
                            chan_pan[ch] = smp.pan
                        if 0x10 <= vcol <= 0x50:
                            chan_vol[ch] = vcol - 0x10
                        if eff == 0x08:
                            chan_pan[ch] = par
                        if eff == 0x0C:
                            chan_vol[ch] = min(64, par)
                        n = Note(last_inst[ch], note, t, chan_vol[ch], chan_pan[ch])
                        chan[ch] = n
                        notes.append(n)
                        continue
                cur = chan[ch]
                if 0x10 <= vcol <= 0x50:
                    chan_vol[ch] = vcol - 0x10
                    if cur is not None:
                        cur.vol.append((t, chan_vol[ch]))
                if eff == 0x0C:
                    chan_vol[ch] = min(64, par)
                    if cur is not None:
                        cur.vol.append((t, chan_vol[ch]))
                if eff == 0x08:
                    chan_pan[ch] = par
                    if cur is not None:
                        cur.pan.append((t, par))
            dur = speed * 2.5 / bpm
            if min_row is None or dur < min_row:
                min_row = dur
            t += dur
    for n in notes:
        if n.t1 is None:
            n.t1 = t
    return notes, t, (min_row or 0.02)


def choose_bpm(total_row_times, min_row):
    """BPM проекта LMMS: тик (1.25/BPM с) должен быть заметно короче строки XM."""
    typical = float(np.median(total_row_times)) if len(total_row_times) else min_row
    for k in range(1, 33):
        b = 1.25 * k / typical
        if b >= 100:
            break
    b = max(b, 2.5 / min_row)
    return int(max(20, min(999, round(b))))


# ═══════════════════════════════════════════════════════════════
# Сэмплы: приведение к 44100 Гц
# ═══════════════════════════════════════════════════════════════

def _sinc_interp(ext, offset, pos, cutoff, half=12):
    """ext[offset + i] — сигнал на целых позициях i; pos — позиции (в тех же единицах)."""
    out = np.empty(len(pos))
    k = np.arange(-half + 1, half + 1)
    for s0 in range(0, len(pos), 20000):
        p = pos[s0:s0 + 20000]
        base = np.floor(p).astype(np.int64)
        frac = p - base
        d = k[None, :] - frac[:, None]
        w = np.sinc(d * cutoff) * cutoff * (0.42 + 0.5 * np.cos(np.pi * d / half) + 0.08 * np.cos(2 * np.pi * d / half))
        idx = base[:, None] + k[None, :] + offset
        out[s0:s0 + 20000] = (w * ext[idx]).sum(axis=1)
    return out


class LmmsSample:
    """Сэмпл XM, приведённый к 44100 Гц (ключ 60 == XM C-4)."""

    def __init__(self, inst_no, name, smp):
        self.inst_no = inst_no
        self.name = name
        self.looped = False
        self.loop_start = 0           # кадры (в итоговом сэмпле)
        self.base_note = 60
        self.pitch_cents = 0
        rate = XM_C4_RATE * 2.0 ** ((smp.rel + smp.finetune / 128.0) / 12.0)
        factor = LMMS_RATE / rate
        x = smp.data.astype(np.float64)
        ls, ll = smp.loop_start, smp.loop_len
        loop = smp.loop_type in (1, 2) and ll >= 2 and ls + ll <= len(x)
        if loop and smp.loop_type == 1 and xm_export.SMOOTH_LOOPS:     # чужие XM тоже могут иметь щёлкающие петли
            ls, ll, _ = xm_export.smooth_loop(x * 32768.0, ls, ll)
            x = x[:ls + ll]
        half = 12
        if loop:
            seg = x[ls:ls + ll]
            if smp.loop_type == 2:                       # пинг-понг -> прямая петля
                seg = np.concatenate([seg, seg[-2:0:-1]]) if len(seg) > 2 else seg
                ll = len(seg)
            m = int(min(64, max(1, math.ceil(256.0 / (ll * factor)))))
            lt = max(1, int(round(m * ll * factor)))
            f2 = lt / float(m * ll)
            cents = 1200.0 * math.log2(f2 / factor)
            ls_out = int(round(ls * f2))
            if ls_out < 1:
                ls_out = lt                               # петля с нулевого кадра: LMMS её не умеет, берём второй цикл
            cut = min(1.0, f2)
            # выходной кадр j берётся из исходной позиции (j - ls_out) / f2 + ls; после ls сигнал
            # периодичен (петля), перед нулём - ноль (или периодическое продолжение назад, если ls == 0)
            j = np.arange(ls_out + lt)
            pos = (j - ls_out) / f2 + ls
            imin = int(math.floor(pos[0])) - half
            imax = int(math.ceil(pos[-1])) + half + 1
            idx = np.arange(imin, imax + 1)
            ext = np.zeros(len(idx))
            inloop = idx >= ls
            ext[inloop] = seg[(idx[inloop] - ls) % ll]
            if ls == 0:
                neg = idx < 0
                ext[neg] = seg[idx[neg] % ll]
            else:
                hd = (idx >= 0) & (idx < ls)
                ext[hd] = x[:ls][idx[hd]]
            y = _sinc_interp(ext, -imin, pos, cut, half)
            self.looped = True
            self.loop_start = ls_out
            total = cents
        else:
            n_out = int(math.ceil(len(x) * factor)) + 2
            pad = half + 4 + int(math.ceil(2.0 / factor))
            ext = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
            pos = np.arange(n_out) / factor
            y = _sinc_interp(ext, pad, pos, min(1.0, factor), half)
            total = 0.0
        pk = float(np.abs(y).max()) if len(y) else 0.0
        if pk > 1.0:
            y = y / pk                                   # защита от клиппинга sinc-выбросов
        self.data = y.astype(np.float32)
        # подстройка высоты: целые полутоны -> basenote, остаток (в центах) -> Pitch дорожки
        semis = int(round(total / 100.0))
        self.base_note = 60 - semis
        self.pitch_cents = int(round(total - 100.0 * semis))
        self.volume = smp.volume

    def write_wav(self, path):
        pcm = np.clip(np.round(self.data * 32767.0), -32768, 32767).astype('<i2')
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(LMMS_RATE)
            w.writeframes(pcm.tobytes())

    def sampledata(self):
        st = np.repeat(self.data[:, None], 2, axis=1).astype('<f4')
        return base64.b64encode(st.tobytes()).decode()


# ═══════════════════════════════════════════════════════════════
# Сборка проекта
# ═══════════════════════════════════════════════════════════════

def _pct_vol(v):
    return max(0, min(200, int(round(v / 64.0 * 100))))


def _pct_pan(p):
    return max(-100, min(100, int(round((p - 128) / 128.0 * 100))))


def _compress_points(pts):
    """pts: [(тик, значение)] -> без повторов подряд и с последним значением на тик."""
    out = []
    for tk, v in sorted(pts, key=lambda a: a[0]):
        if out and out[-1][0] == tk:
            out[-1] = (tk, v)
        else:
            out.append((tk, v))
    res = []
    for tk, v in out:
        if not res or res[-1][1] != v:
            res.append((tk, v))
    return res


def estimate_gain(notes, samples, tau_frame=0.02, target=0.9, crest=1.6):
    """Процент общей громкости проекта, при котором сумма нот (по оценке) не клиппует.
    Оценка пика кадра: crest * sqrt(sum(a_i^2)), a_i = громкость ноты * пик её сэмпла."""
    nfr = int(max(n.t1 for n in notes) / tau_frame) + 2
    acc = np.zeros(nfr + 1)
    for n in notes:
        pk = float(np.abs(samples[n.inst].data).max()) if len(samples[n.inst].data) else 0.0
        a = max(v for _, v in n.vol) / 64.0 * pk
        i0, i1 = int(n.t0 / tau_frame), int(n.t1 / tau_frame) + 1
        acc[i0] += a * a
        acc[i1] -= a * a
    est = crest * np.sqrt(np.maximum(np.cumsum(acc), 0.0))
    peak = float(est.max()) or 1.0
    return max(5, min(200, int(math.floor(100.0 * min(2.0, target / peak)))))


def xm_to_project(xm, out_path, embed=False, bpm=None, samples_dir=None, gain=None, log=print):
    notes, total_t, min_row = build_notes(xm)
    if not notes:
        raise ValueError('в XM нет нот')
    # длительности строк для выбора BPM
    row_times = []
    speed, cur_bpm = xm.speed, xm.bpm
    for o in xm.order:
        if o >= len(xm.patterns):
            continue
        for row in xm.patterns[o]:
            for c in row:
                if c[3] == 0x0F and c[4]:
                    if c[4] < 32:
                        speed = c[4]
                    else:
                        cur_bpm = c[4]
            row_times.append(speed * 2.5 / cur_bpm)
    bpm = int(bpm) if bpm else choose_bpm(row_times, min_row)
    tau = 60.0 / (bpm * TICKS_PER_BEAT)                   # длительность тика, с

    def tick(t):
        return int(round(t / tau))

    # сэмплы
    used = sorted({n.inst for n in notes})
    samples = {}
    for i in used:
        name, smp = xm.instruments[i]
        samples[i] = LmmsSample(i, name, smp)
    if not embed:
        base = os.path.splitext(out_path)[0]
        samples_dir = samples_dir or (base + '_samples')
        os.makedirs(samples_dir, exist_ok=True)
        for i, s in samples.items():
            fn = '%02d %s.wav' % (i, ''.join(ch if ch.isalnum() or ch in ' -_.' else '_' for ch in s.name)[:40].strip())
            s.path = os.path.abspath(os.path.join(samples_dir, fn))
            s.write_wav(s.path)

    gain = int(gain) if gain else estimate_gain(notes, samples)

    # тики и дорожки
    for n in notes:
        n.k0 = tick(n.t0)
        n.k1 = max(n.k0 + 1, tick(n.t1))
    lanes = {i: [] for i in used}                         # i -> список ends
    for n in sorted(notes, key=lambda n: (n.k0, n.inst)):
        ends = lanes[n.inst]
        for li, e in enumerate(ends):
            if e <= n.k0:
                ends[li] = n.k1
                n.lane = li
                break
        else:
            ends.append(n.k1)
            n.lane = len(ends) - 1
    song_ticks = max(n.k1 for n in notes)
    pat_len = ((song_ticks + 191) // 192) * 192

    ids = [1000]

    def new_id():
        ids[0] += 1
        return ids[0]

    tracks = []
    auto_tracks = []
    for i in used:
        s = samples[i]
        for li in range(len(lanes[i])):
            ln = sorted([n for n in notes if n.inst == i and n.lane == li], key=lambda n: n.k0)
            vol_pts, pan_pts = [], []
            for n in ln:
                for t, v in n.vol:
                    vol_pts.append((tick(t), _pct_vol(v)))
                for t, v in n.pan:
                    pan_pts.append((tick(t), _pct_pan(v)))
            vol_pts = _compress_points(vol_pts)
            pan_pts = _compress_points(pan_pts)
            tname = s.name if li == 0 else '%s #%d' % (s.name, li + 1)
            vol_node = pan_node = ''
            vol_attr = 'vol="100"'
            pan_attr = 'pan="0"'
            if len({v for _, v in vol_pts}) > 1:
                vid = new_id()
                vol_node = '<vol scale_type="linear" id="%d" value="%d"/>' % (vid, vol_pts[0][1])
                vol_attr = ''
                auto_tracks.append(_automation_track('Vol: ' + tname, vid, vol_pts, pat_len))
            else:
                vol_attr = 'vol="%d"' % (vol_pts[0][1] if vol_pts else 100)
            if len({v for _, v in pan_pts}) > 1:
                pid = new_id()
                pan_node = '<pan scale_type="linear" id="%d" value="%d"/>' % (pid, pan_pts[0][1])
                pan_attr = ''
                auto_tracks.append(_automation_track('Pan: ' + tname, pid, pan_pts, pat_len))
            else:
                pan_attr = 'pan="%d"' % (pan_pts[0][1] if pan_pts else 0)
            nx = []
            for n in ln:
                key = max(0, min(119, n.key + 11))
                det = ''
                if n.bends:
                    pts = [(0, 0)]
                    for t, k in n.bends:
                        rel = max(1, min(n.k1 - n.k0 - 1, tick(t) - n.k0)) if n.k1 - n.k0 > 1 else 1
                        pts.append((rel, max(-48, min(48, k - n.key))))
                    pts = _compress_points(pts)
                    if len(pts) > 1:
                        det = ('<automationpattern><detuning pos="0" len="%d" name="" prog="0" tens="1" mute="0">'
                               % (n.k1 - n.k0)
                               + ''.join('<time pos="%d" value="%d"/>' % p for p in pts)
                               + '</detuning></automationpattern>')
                nx.append('<note pan="0" len="%d" key="%d" vol="100" pos="%d">%s</note>' % (n.k1 - n.k0, key, n.k0, det))
            nframes = len(s.data)
            if s.looped:
                lf = (s.loop_start - 0.25) / nframes
                loop = 'looped="1" lframe="%.9f"' % lf
            else:
                loop = 'looped="0" lframe="0"'
            if embed:
                src = 'src="" sampledata="%s"' % s.sampledata()
            else:
                src = 'src=%s' % quoteattr(s.path)
            afp = ('<audiofileprocessor amp="100" %s sframe="0" eframe="1" reversed="0" stutter="0" interp="1" %s/>'
                   % (loop, src))
            tracks.append(
                '<track name=%s solo="0" muted="0" type="0">'
                '<instrumenttrack %s %s fxch="0" basenote="%d" pitchrange="1" usemasterpitch="1" pitch="%d">'
                '%s%s<instrument name="audiofileprocessor">%s</instrument></instrumenttrack>'
                '<pattern steps="%d" len="%d" name=%s pos="0" muted="0" type="1">%s</pattern></track>'
                % (quoteattr(tname), pan_attr, vol_attr, s.base_note, s.pitch_cents, vol_node, pan_node, afp,
                   pat_len // 12, pat_len, quoteattr(tname), ''.join(nx)))
    xml = ('<?xml version="1.0"?>\n<!DOCTYPE lmms-project>\n'
           '<lmms-project version="1.0" creatorversion="1.2.2" creator="LMMS" type="song">\n'
           '<head timesig_numerator="4" masterpitch="0" timesig_denominator="4" bpm="%d" mastervol="%d"/>\n'
           '<song><trackcontainer type="song">%s%s</trackcontainer>'
           '<timeline lp0pos="0" lp1pos="%d" lpstate="0"/></song></lmms-project>\n'
           % (bpm, gain, ''.join(tracks), ''.join(auto_tracks), pat_len)).encode('utf-8')
    if out_path.lower().endswith('.mmp'):
        data = xml
    else:
        data = struct.pack('>I', len(xml)) + zlib.compress(xml, 9)
    with open(out_path, 'wb') as f:
        f.write(data)
    log('[mmpz] %s  (BPM %d, громкость %d%%, %d дорожек, %d нот, %d инструментов%s)' % (
        out_path, bpm, gain, len(tracks), len(notes), len(used), ', сэмплы встроены' if embed else
        ', сэмплы: ' + os.path.basename(samples_dir) + '/'))
    return out_path


def _automation_track(name, oid, pts, length):
    t = ''.join('<time pos="%d" value="%d"/>' % p for p in pts)
    return ('<track name=%s solo="0" muted="0" type="5"><automationtrack/>'
            '<automationpattern pos="0" len="%d" name=%s tens="1" mute="0" prog="0">%s<object id="%d"/>'
            '</automationpattern></track>' % (quoteattr(name), length, quoteattr(name), t, oid))


def convert_file(xm_path, out_path=None, embed=False, bpm=None, gain=None, samples_dir=None, log=print):
    """XM -> .mmpz (или .mmp - без сжатия, если out_path заканчивается на .mmp). Возвращает путь к проекту."""
    out_path = out_path or os.path.splitext(xm_path)[0] + '.mmpz'
    return xm_to_project(read_xm(xm_path), out_path, embed=embed, bpm=bpm, gain=gain,
                         samples_dir=samples_dir, log=log)


def export_after_xm(xm_path, embed=False, keep_xm=True, bpm=None, log=print):
    """Для скриптов-конвертеров: XM уже записан -> рядом кладём <имя>.mmpz.
    keep_xm=False удаляет XM (если он был нужен только как промежуточный файл)."""
    try:
        return convert_file(xm_path, embed=embed, bpm=bpm, log=log)
    except Exception as e:
        print('[mmpz] ошибка: %s' % e, file=sys.stderr)
        return None
    finally:
        if not keep_xm:
            try:
                os.remove(xm_path)
            except OSError:
                pass


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='XM -> LMMS (.mmpz)')
    ap.add_argument('input', help='файл .xm')
    ap.add_argument('output', nargs='?', help='файл .mmpz (по умолчанию рядом с XM)')
    ap.add_argument('--embed', action='store_true', help='встроить сэмплы в проект (иначе WAV в <имя>_samples/)')
    ap.add_argument('--gain', type=int, help='общая громкость проекта, %% (по умолчанию подбирается без клиппинга)')
    ap.add_argument('--bpm', type=int, help='задать темп проекта LMMS (по умолчанию подбирается)')
    a = ap.parse_args(argv)
    convert_file(a.input, a.output, embed=a.embed, bpm=a.bpm, gain=a.gain)


if __name__ == '__main__':
    main()
