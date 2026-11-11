#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самопроверка mmpz_export.py на синтетическом XM (без внешних файлов).

  1. собирает XM: петля из 37 кадров (нечётная длина проверяет целочисленность петли),
     нота C-4, изгиб 3xx до C-5, падение громкости в 4 раза, note-off;
  2. читает его обратно (read_xm) и конвертирует в .mmpz (внешние WAV и встроенные);
  3. проверяет, что проект - корректный XML;
  4. если в PATH есть lmms: рендерит и сверяет частоты и амплитуду с расчётом по формулам XM.

Запуск:  python3 tests/mmpz_selftest.py
"""
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import xml.etree.ElementTree as ET
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mmpz_export as M

LOOP = 37
ROW = 6 * 2.5 / 125.0                         # speed 6, BPM 125 -> 0.12 с


def write_xm(path, bits8=False, pingpong=False):
    if pingpong:                                   # половина «пилы»; пинг-понг даёт треугольник с периодом 2N-2
        cyc = np.linspace(-1.0, 1.0, LOOP)
    else:
        cyc = np.sin(2 * np.pi * np.arange(LOOP) / LOOP) + 0.3 * np.sin(4 * np.pi * np.arange(LOOP) / LOOP)
    if bits8:
        data = np.round(cyc / abs(cyc).max() * 100).astype(np.int8)
        d = np.diff(data.astype(np.int32), prepend=0)
        d = ((d + 128) % 256 - 128).astype(np.int8)
    else:
        data = np.round(cyc / abs(cyc).max() * 20000).astype('<i2')
        d = np.diff(data.astype(np.int32), prepend=0)
        d = ((d + 32768) % 65536 - 32768).astype('<i2')
    unit = 1 if bits8 else 2
    nch, rows = 2, 64
    cells = {(0, 0): (49, 1, 0x50, 0, 0), (8, 0): (61, 1, 0, 3, 0xFF),
             (16, 0): (0, 0, 0x10 + 16, 0, 0), (24, 0): (97, 0, 0, 0, 0)}
    pk = bytearray()
    for r in range(rows):
        for c in range(nch):
            cell = cells.get((r, c))
            if not cell:
                pk.append(0x80)
                continue
            note, inst, vol, eff, par = cell
            fl = 0x80 | (1 if note else 0) | (2 if inst else 0) | (4 if vol else 0) | (8 if eff else 0) | (16 if par else 0)
            pk.append(fl)
            for v, bit in ((note, 1), (inst, 2), (vol, 4), (eff, 8), (par, 16)):
                if fl & bit:
                    pk.append(v)
    with open(path, 'wb') as f:
        f.write(b'Extended Module: ' + b'selftest'.ljust(20, b'\0') + b'\x1a' + b'selftest'.ljust(20, b'\0'))
        f.write(struct.pack('<HIHHHHHHHH', 0x0104, 276, 1, 0, nch, 1, 1, 1, 6, 125))
        f.write(bytes([0] + [0] * 255))
        f.write(struct.pack('<IBHH', 9, 0, rows, len(pk)) + pk)
        ihs = 263
        f.write(struct.pack('<I', ihs) + b'loop37'.ljust(22, b'\0') + struct.pack('<BHI', 0, 1, 40) + bytes(96))
        f.write(bytes(48) + bytes(48) + bytes([0] * 14) + struct.pack('<H', 0))
        f.write(b'\0' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
        f.write(struct.pack('<III', LOOP * unit, 0, LOOP * unit))
        f.write(struct.pack('<BbBBb', 64, 0, (2 if pingpong else 1) | (0 if bits8 else 0x10), 128, 0) + b'\0' + b'loop37'.ljust(22, b'\0'))
        f.write(d.tobytes())



def write_multi_xm(path):
    """Один инструмент XM с двумя сэмплами (петли 37 и 50 кадров), выбор по номеру ноты: 1..48 -> первый,
    49..96 -> второй, у каждого свой relative note. Две ноты: 40 (первый сэмпл) и 60 (второй)."""
    import xm_export as XE
    a = np.round(20000 * np.sin(2 * np.pi * np.arange(37) / 37.0)).astype(np.int16)
    b = np.round(20000 * np.sin(2 * np.pi * np.arange(50) / 50.0)).astype(np.int16)
    inst = XE.XmInstrument('multi', [XE.XmSample(a, 'A', 0, 37, relative_note=2),
                                     XE.XmSample(b, 'B', 0, 50, relative_note=-5)],
                           keymap=[0] * 48 + [1] * 48)
    grid = {(0, 0): XE.Cell(40, 1, 0x50), (16, 0): XE.Cell(60, 1, 0x50), (30, 0): XE.Cell(97)}
    XE.write_xm(path, 't', 't', 2, 125, 6, XE.grid_to_patterns(grid, 64, 2, 64), [inst])


def f0(seg, sr):
    seg = seg - seg.mean()
    n = 1 << 18
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n))
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    ok = (fr > 80) & (fr < 1500)
    return fr[ok][sp[ok].argmax()]


def expect_freq(xm_note, period=LOOP):
    return M.XM_C4_RATE * 2.0 ** ((xm_note - 49) / 12.0) / period


def main():
    work = tempfile.mkdtemp(prefix='mmpz_selftest_')
    fails = []

    def check(name, ok, info=''):
        print('  [%s] %s %s' % ('ok' if ok else 'FAIL', name, info))
        if not ok:
            fails.append(name)
    try:
        xm_path = os.path.join(work, 't.xm')
        write_xm(xm_path)
        xm = M.read_xm(xm_path)
        check('read_xm: инструмент и петля', 1 in xm.instruments and xm.instruments[1][1].loop_len == LOOP)
        notes, total, _ = M.build_notes(xm)
        check('build_notes: одна нота с изгибом и сменой громкости',
              len(notes) == 1 and len(notes[0].bends) == 1 and len(notes[0].vol) == 2,
              'нот %d' % len(notes))
        for embed in (False, True):
            out = os.path.join(work, 'e.mmpz' if embed else 'f.mmpz')
            M.convert_file(xm_path, out, embed=embed, log=lambda m: None)
            raw = open(out, 'rb').read()
            xml = zlib.decompress(raw[4:])
            check('mmpz (%s): длина в заголовке и XML' % ('встроенные' if embed else 'WAV'),
                  struct.unpack('>I', raw[:4])[0] == len(xml) and ET.fromstring(xml).tag == 'lmms-project')
        lmms = shutil.which('lmms')
        if not lmms:
            print('  lmms не найден - проверка рендера пропущена')
        else:
            out = os.path.join(work, 'f.mmpz')
            wav_path = os.path.join(work, 'r.wav')
            env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
            subprocess.run([lmms, '-r', out, '-f', 'wav', '-o', wav_path], env=env, capture_output=True, timeout=300)
            w = wave.open(wav_path)
            sr = w.getframerate()
            d = np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1, w.getnchannels()).mean(1)
            t0 = 0.0
            # окна внутри трёх участков ноты: до изгиба, после изгиба, после падения громкости
            wins = {'C-4 (до изгиба)': (1 * ROW, 7 * ROW, 49), 'C-5 (после 3xx)': (9 * ROW, 15 * ROW, 61),
                    'C-5 (громкость /4)': (17 * ROW, 23 * ROW, 61)}
            rms = {}
            for name, (a, b, nt) in wins.items():
                seg = d[int(a * sr):int(b * sr)]
                f = f0(seg, sr)
                e = expect_freq(nt)
                check('частота %s' % name, abs(f / e - 1) < 0.004, '%.2f Гц, ожидалось %.2f (%.3f%%)' % (f, e, (f / e - 1) * 100))
                rms[name] = seg.std()
            r = rms['C-5 (громкость /4)'] / rms['C-5 (после 3xx)']
            check('громкость 64 -> 16', abs(r - 0.25) < 0.03, 'отношение %.3f' % r)
            tail = d[int(26 * ROW * sr):int(30 * ROW * sr)]
            check('note-off: тишина', np.abs(tail).max() < 0.01 * np.abs(d).max())
        # 8 бит + пинг-понг-петля (в собственных писателях не используются, но встречаются в чужих XM)
        xm2 = os.path.join(work, 't2.xm')
        write_xm(xm2, bits8=True, pingpong=True)
        r2 = M.read_xm(xm2)
        s2 = r2.instruments[1][1]
        check('8 бит: чтение сэмпла и тип петли', len(s2.data) == LOOP and s2.loop_type == 2, 'тип %d' % s2.loop_type)
        if lmms:
            out2 = os.path.join(work, 'p.mmpz')
            M.convert_file(xm2, out2, log=lambda m: None)
            wav2 = os.path.join(work, 'p.wav')
            subprocess.run([lmms, '-r', out2, '-f', 'wav', '-o', wav2], env=env, capture_output=True, timeout=300)
            w = wave.open(wav2)
            d2 = np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1, w.getnchannels()).mean(1)
            f = f0(d2[int(1 * ROW * sr):int(7 * ROW * sr)], sr)
            e = expect_freq(49, 2 * LOOP - 2)
            check('пинг-понг: частота C-4', abs(f / e - 1) < 0.004, '%.2f Гц, ожидалось %.2f' % (f, e))
        # инструмент с несколькими сэмплами и keymap (так упаковываются инструменты, когда сэмплов > 128)
        xm3 = os.path.join(work, 'm.xm')
        write_multi_xm(xm3)
        n3, _, _ = M.build_notes(M.read_xm(xm3))
        check('multi: ноты выбирают сэмпл по keymap', sorted(n.inst for n in n3) == [(1, 0), (1, 1)], str([n.inst for n in n3]))
        if lmms:
            out3 = os.path.join(work, 'm.mmpz')
            M.convert_file(xm3, out3, log=lambda m: None)
            wav3 = os.path.join(work, 'm.wav')
            subprocess.run([lmms, '-r', out3, '-f', 'wav', '-o', wav3], env=env, capture_output=True, timeout=300)
            w = wave.open(wav3)
            d3 = np.frombuffer(w.readframes(w.getnframes()), '<i2').reshape(-1, w.getnchannels()).mean(1)
            fa = f0(d3[int(1 * ROW * sr):int(14 * ROW * sr)], sr)
            fb = f0(d3[int(17 * ROW * sr):int(29 * ROW * sr)], sr)
            ea = M.XM_C4_RATE * 2.0 ** ((40 - 49 + 2) / 12.0) / 37
            eb = M.XM_C4_RATE * 2.0 ** ((60 - 49 - 5) / 12.0) / 50
            check('multi: частота первого сэмпла', abs(fa / ea - 1) < 0.004, '%.2f Гц, ожидалось %.2f' % (fa, ea))
            check('multi: частота второго сэмпла', abs(fb / eb - 1) < 0.004, '%.2f Гц, ожидалось %.2f' % (fb, eb))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print('ИТОГ:', 'все проверки пройдены' if not fails else 'ПРОВАЛЕНО: ' + ', '.join(fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
