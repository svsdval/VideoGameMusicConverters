#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
s3m2xm.py — S3M (ScreamTracker 3) -> XM (через xm_export) -> .mmp и .mmpz (через mmpz_export)

Запуск:  python3 s3m2xm.py song.s3m [...] [-o каталог] [--no-xm]

Соответствие S3M -> XM
  * ноты: октава*12 + нота + 1; частота C-4 (c2spd) -> relative note + finetune сэмпла;
  * громкость ноты -> колонка громкости; нота 254 (обрыв) -> key off (97);
  * панорама каналов (стерео, default pan) -> панорама сэмпла: инструмент копируется по одной копии
    на каждую используемую панораму, поэтому каналы слева/справа остаются на своих местах;
  * эффекты: Axx Fxx, Bxx Bxx, Cxx Dxx, Dxy Axy/EAx/EBx, Exx/Fxx 2xx/1xx (+E1x/E2x/X1x/X2x),
    Gxx 3xx, Hxy 4xy, Ixy Txy, Jxy 0xy, Kxy 6xy, Lxy 5xy, Oxx 9xx, Pxy Pxy, Qxy Rxy, Rxy 7xy,
    S-подкоманды в E-команды, Txx Fxx, Uxy 4xy (мельче), Vxx Gxx, Wxy Hxy, Xxx 8xx;
  * таблица частот XM - Amiga (периоды), как и у S3M;
  * не переносятся: инструменты OPL (AdLib) и их каналы, канальная громкость, панбрелло, фильтр.
"""
import argparse
import math
import os
import struct
import sys
import zlib

import numpy as np

import mmpz_export
import xm_export
from xm_export import Cell, XmInstrument, XmSample

FX = {c: i + 1 for i, c in enumerate('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}      # A=1 ... Z=26


def _fine_pan(v):
    return max(0, min(255, v * 17))


def convert_effect(eff, par):
    """(команда S3M 1..26, параметр) -> (команда XM, параметр)."""
    x, y = par >> 4, par & 15
    if eff == FX['A']:
        return (0x0F, par) if 0 < par < 32 else (0, 0)
    if eff == FX['B']:
        return 0x0B, par
    if eff == FX['C']:
        return 0x0D, par
    if eff == FX['D']:
        if par == 0:
            return 0x0A, 0
        if y == 0:
            return 0x0A, par                         # D x0 - вверх
        if x == 0:
            return 0x0A, par                         # D 0y - вниз
        if y == 15:
            return 0x0E, 0xA0 | x                    # DxF - тонко вверх
        if x == 15:
            return 0x0E, 0xB0 | y                    # DFy - тонко вниз
        return 0x0A, y                               # оба ненулевые: ST3 уменьшает
    if eff in (FX['E'], FX['F']):
        down = eff == FX['E']
        if par >= 0xF0:
            return 0x0E, (0x20 if down else 0x10) | y        # тонкий слайд
        if par >= 0xE0:
            return 0x21, (0x20 if down else 0x10) | y        # очень тонкий слайд (X2y / X1y)
        return (0x02 if down else 0x01), par
    if eff == FX['G']:
        return 0x03, par
    if eff == FX['H']:
        return 0x04, par
    if eff == FX['I']:
        return 0x1D, par                             # Txy тремор
    if eff == FX['J']:
        return 0x00, par
    if eff == FX['K']:
        return 0x06, par
    if eff == FX['L']:
        return 0x05, par
    if eff == FX['O']:
        return 0x09, par
    if eff == FX['P']:
        return 0x19, par
    if eff == FX['Q']:
        return 0x1B, par
    if eff == FX['R']:
        return 0x07, par
    if eff == FX['S']:
        sub = {0x1: 0x3, 0x2: 0x5, 0x3: 0x4, 0x4: 0x7, 0xB: 0x6, 0xC: 0xC, 0xD: 0xD, 0xE: 0xE}.get(x)
        if x == 0x8:
            return 0x08, _fine_pan(y)
        if sub is not None:
            if x == 0x2:
                y = (y - 8) & 15                     # S2x: 0..F, 8 = без изменения; E5x: 8 = 0
            if x == 0x3 or x == 0x4:
                y = y & 3                            # формы волны
            return 0x0E, (sub << 4) | y
        return 0, 0
    if eff == FX['T']:
        return (0x0F, par) if par >= 32 else (0, 0)
    if eff == FX['U']:
        return 0x04, (x << 4) | (min(15, (y + 3) // 4) if y else 0)
    if eff == FX['V']:
        return 0x10, min(par, 64)
    if eff == FX['W']:
        return 0x11, par
    if eff == FX['X']:
        return 0x08, par
    return 0, 0


def read_s3m(path):
    d = open(path, 'rb').read()
    if d[0x2C:0x30] != b'SCRM':
        raise ValueError('не S3M: ' + path)
    title = d[:28].split(b'\0')[0].decode('latin1')
    ordn, insn, patn, flags, cwtv, ffi = struct.unpack_from('<6H', d, 0x20)
    gvol, speed, tempo, mvol = d[0x30], d[0x31], d[0x32], d[0x33]
    stereo = bool(mvol & 0x80)
    chset = list(d[0x40:0x60])
    order = [o for o in d[0x60:0x60 + ordn] if o < 254]
    ip = struct.unpack_from('<%dH' % insn, d, 0x60 + ordn)
    pp = struct.unpack_from('<%dH' % patn, d, 0x60 + ordn + insn * 2)
    pos = 0x60 + ordn + insn * 2 + patn * 2
    defpan = list(d[pos:pos + 32]) if d[0x35] == 252 else None

    # каналы
    chmap = {}
    for c, v in enumerate(chset):
        if v < 16:
            chmap[c] = len(chmap)
    pans = {}
    for c, v in enumerate(chset):
        if c in chmap:
            if not stereo:
                pans[c] = 128
            elif defpan is not None and defpan[c] & 0x20:
                pans[c] = _fine_pan(defpan[c] & 15)
            else:
                pans[c] = 0x30 if v < 8 else 0xC0

    # инструменты
    inst = {}
    for i, p in enumerate(ip, 1):
        o = p * 16
        if not o or o + 80 > len(d) or d[o] != 1:
            continue
        mem = (d[o + 13] << 16) | struct.unpack_from('<H', d, o + 14)[0]
        length, lbeg, lend = struct.unpack_from('<3I', d, o + 16)
        vol, _, pack, fl = d[o + 28], d[o + 29], d[o + 30], d[o + 31]
        c2spd = struct.unpack_from('<I', d, o + 32)[0] or 8363
        name = d[o + 48:o + 76].split(b'\0')[0].decode('latin1')
        bits16, st = bool(fl & 4), bool(fl & 2)
        nb = length * (2 if bits16 else 1) * (2 if st else 1)
        raw = d[mem * 16:mem * 16 + nb]
        if bits16:
            a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype='<u2' if ffi == 2 else '<i2').astype(np.int32)
            a = a - 32768 if ffi == 2 else a
        else:
            a = np.frombuffer(raw, dtype=np.uint8 if ffi == 2 else np.int8).astype(np.int32)
            a = (a - 128) * 256 if ffi == 2 else a * 256
        if st and len(a) >= 2 * length:
            a = (a[:length] + a[length:2 * length]) // 2          # стерео -> моно
        a = a[:length]
        loop = None
        if (fl & 1) and lend > lbeg and lend <= len(a):
            loop = (lbeg, lend - lbeg)
        inst[i] = dict(name=name, data=a, vol=min(vol, 64), c2spd=c2spd, loop=loop)

    # паттерны
    patterns = []
    for p in pp:
        o = p * 16
        rows = [[None] * len(chmap) for _ in range(64)]
        if o:
            ln = struct.unpack_from('<H', d, o)[0]
            q, end, r = o + 2, o + ln, 0
            while r < 64 and q < min(end, len(d)):
                b = d[q]
                q += 1
                if b == 0:
                    r += 1
                    continue
                c = b & 31
                note = ins = vol = eff = par = None
                if b & 32:
                    note, ins = d[q], d[q + 1]
                    q += 2
                if b & 64:
                    vol = d[q]
                    q += 1
                if b & 128:
                    eff, par = d[q], d[q + 1]
                    q += 2
                if c not in chmap:
                    continue
                rows[r][chmap[c]] = (c, note, ins, vol, eff, par)
        patterns.append(rows)
    return dict(title=title, speed=speed or 6, tempo=tempo or 125, gvol=gvol, pans=pans, chmap=chmap,
                order=order, inst=inst, patterns=patterns, flags=flags)


def build_xm(s):
    nch = len(s['chmap'])
    variants = {}                                   # (инструмент S3M, панорама) -> номер инструмента XM
    xm_insts = []

    def xm_inst(i, pan):
        key = (i, pan)
        if key in variants:
            return variants[key]
        if len(xm_insts) >= xm_export.MAX_XM_INSTRUMENTS:
            same = [v for (ii, _), v in variants.items() if ii == i]
            return same[0] if same else 0
        v = s['inst'][i]
        rate = math.log2(v['c2spd'] / 8363.0) * 12.0
        rel = int(math.floor(rate + 0.5))
        fine = int(round((rate - rel) * 128))
        if fine > 127:
            rel, fine = rel + 1, fine - 128
        ls, ll = v['loop'] or (0, 0)
        smp = XmSample(v['data'], v['name'], ls, ll, volume=v['vol'], finetune=fine, panning=pan, relative_note=rel)
        xm_insts.append(XmInstrument(v['name'], [smp]))
        variants[key] = len(xm_insts)
        return variants[key]

    ch_pan = {s['chmap'][c]: p for c, p in s['pans'].items()}
    patterns = []
    for rows in s['patterns']:
        out = []
        for row in rows:
            new = []
            for xc, info in enumerate(row):
                n = Cell()
                if info:
                    _c, note, ins, vol, eff, par = info
                    if note is not None:
                        if note == 254:
                            n.note = 97
                        elif note != 255:
                            n.note = min(96, max(1, (note >> 4) * 12 + (note & 15) + 1))
                        if ins and ins in s['inst']:
                            n.inst = xm_inst(ins, ch_pan.get(xc, 128))
                    if vol is not None:
                        n.vol = 0x10 + min(vol, 64)
                    if eff:
                        n.eff, n.par = convert_effect(eff, par)
                new.append(n)
            out.append(new)
        patterns.append(out)
    if s['gvol'] < 64 and patterns and s['order']:             # глобальная громкость из заголовка
        first = patterns[s['order'][0]][0]
        for n in first:
            if not n.eff:
                n.eff, n.par = 0x10, s['gvol']
                break
    return nch, patterns, xm_insts


def convert(path, outdir, keep_xm=True):
    s = read_s3m(path)
    nch, patterns, insts = build_xm(s)
    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(outdir, exist_ok=True)
    xm_path = os.path.join(outdir, base + '.xm')
    xm_export.write_xm(xm_path, s['title'] or base, 's3m2xm', nch, s['tempo'], s['speed'], patterns, insts,
                       s['order'], linear=False)
    mmpz = os.path.join(outdir, base + '.mmpz')
    mmpz_export.convert_file(xm_path, mmpz)
    with open(mmpz, 'rb') as f:
        xml = zlib.decompress(f.read()[4:])
    with open(os.path.join(outdir, base + '.mmp'), 'wb') as f:
        f.write(xml)
    if not keep_xm:
        os.remove(xm_path)


def main():
    ap = argparse.ArgumentParser(description='S3M -> XM -> MMP/MMPZ')
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('-o', '--outdir', default='export')
    ap.add_argument('--no-xm', action='store_true', help='не оставлять промежуточный XM')
    a = ap.parse_args()
    for p in a.inputs:
        try:
            convert(p, a.outdir, not a.no_xm)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print('[ошибка] %s: %s' % (p, e), file=sys.stderr)


if __name__ == '__main__':
    main()
