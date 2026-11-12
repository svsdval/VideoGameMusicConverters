#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mod2xm.py — MOD (ProTracker и совместимые) -> XM (через xm_export) -> .mmp и .mmpz (через mmpz_export)

Запуск:  python3 mod2xm.py song.mod [...] [-o каталог] [--no-xm]
Для каждого MOD в каталоге вывода появляются <имя>.xm, <имя>.mmpz, <имя>.mmp и <имя>_samples/*.wav.
"""
import argparse
import os
import struct
import sys
import zlib

import numpy as np

import mmpz_export
import xm_export
from xm_export import Cell, XmInstrument, XmSample

# периоды ProTracker, finetune 0, C-1..B-3
PERIODS = [856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
           428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
           214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113]
SIGNATURES = {b'M.K.': 4, b'M!K!': 4, b'FLT4': 4, b'4CHN': 4, b'6CHN': 6, b'8CHN': 8, b'FLT8': 8, b'OCTA': 8, b'CD81': 8}


def period_to_note(period):
    if period <= 0:
        return 0
    i = min(range(len(PERIODS)), key=lambda k: abs(PERIODS[k] - period))
    return 37 + i                                  # MOD C-2 (428) = XM C-4 (49)


def read_mod(path):
    d = open(path, 'rb').read()
    sig = d[1080:1084]
    nch = SIGNATURES.get(sig)
    if nch is None:
        if sig[2:] == b'CH' and sig[:2].isdigit():
            nch = int(sig[:2])
        else:
            raise ValueError('не похоже на MOD с 31 сэмплом: ' + path)
    title = d[:20].split(b'\0')[0].decode('latin1')
    samples = []
    for i in range(31):
        h = d[20 + i * 30:50 + i * 30]
        name = h[:22].split(b'\0')[0].decode('latin1')
        length, ft, vol, ls, ll = struct.unpack('>HBBHH', h[22:30])
        ft &= 15
        samples.append((name, length * 2, ft - 16 if ft > 7 else ft, min(vol, 64), ls * 2, ll * 2))
    songlen = d[950]
    order = list(d[952:952 + 128])[:max(1, songlen)]
    npat = max(order) + 1
    p = 1084
    patterns = []
    for _ in range(npat):
        rows = []
        for _r in range(64):
            row = []
            for _c in range(nch):
                b = d[p:p + 4]
                p += 4
                if len(b) < 4:
                    b = b'\0\0\0\0'
                period = ((b[0] & 15) << 8) | b[1]
                inst = (b[0] & 0xF0) | (b[2] >> 4)
                eff, par = b[2] & 15, b[3]
                row.append(Cell(period_to_note(period), inst, 0, eff, par))
            rows.append(row)
        patterns.append(rows)
    insts = []
    for name, length, ft, vol, ls, ll in samples:
        raw = d[p:p + length]
        p += length
        data = np.frombuffer(raw, dtype=np.int8).astype(np.int32) * 256
        if len(data) == 0:
            insts.append(XmInstrument(name))
            continue
        loop = None
        if ll > 2 and ls + ll <= len(data):
            loop = (ls, ll)
        smp = XmSample(data, name, *(loop or (0, 0)), volume=vol, finetune=ft * 16)
        insts.append(XmInstrument(name, [smp]))
    return title, nch, patterns, order, insts


def convert(path, outdir, write_xm_file=True):
    title, nch, patterns, order, insts = read_mod(path)
    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir, exist_ok=True)
    xm_path = os.path.join(outdir, base + '.xm')
    xm_export.write_xm(xm_path, title or base, 'mod2xm', nch, 125, 6, patterns, insts, order, linear=False)
    mmpz = os.path.join(outdir, base + '.mmpz')
    mmpz_export.convert_file(xm_path, mmpz)
    with open(mmpz, 'rb') as f:
        xml = zlib.decompress(f.read()[4:])
    with open(os.path.join(outdir, base + '.mmp'), 'wb') as f:
        f.write(xml)
    if not write_xm_file:
        os.remove(xm_path)


def main():
    ap = argparse.ArgumentParser(description='MOD -> XM -> MMP/MMPZ')
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--outdir', default='export')
    ap.add_argument('--no-xm', action='store_true', help='не оставлять промежуточный XM')
    a = ap.parse_args()
    for p in a.inputs:
        try:
            convert(p, a.outdir, not a.no_xm)
        except Exception as e:
            print('[ошибка] %s: %s' % (p, e), file=sys.stderr)


if __name__ == '__main__':
    main()
