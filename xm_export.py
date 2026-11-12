#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_export.py — единая запись XM (FastTracker 2, Extended Module) для всех конвертеров.

Раньше в каждом *2xm-midi-wav.py был свой XMWriter; здесь один общий:

    Cell                      ячейка паттерна (нота, инструмент, колонка громкости, эффект, параметр)
    XmSample, XmInstrument    описание сэмпла/инструмента (16 бит, прямая петля или без неё)
    grid_to_patterns(...)     словарь {(строка, канал): Cell} -> список паттернов
    write_xm(...)             записать готовый модуль
    XMWriter                  удобная обёртка: set_rows(grid, nrows) + instruments + write(path)

Огибающие. Все конвертеры пишут «плоские» огибающие, различаются лишь 14 служебных байт
инструмента (число точек, петли, флаги) и fadeout; их варианты вынесены в константы
ENV_* ниже, чтобы файлы остались побайтно теми же, что давали прежние отдельные писатели.
"""
import os
import struct
import sys

import numpy as np

MAX_XM_CHANNELS = 32
MAX_XM_INSTRUMENTS = 128
INSTRUMENT_HEADER_SIZE = 263

# 14 байт после огибающих: число точек громкости/панорамы, sustain/петли, тип огибающих, вибрато
ENV_OFF = (2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0)          # огибающие выключены: note-off (97) режет звук
ENV_OFF_PAN_LOOP = (2, 2, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0)  # то же, петля панорамы 0..1 (psg, pt3)
ENV_FLAT_FADEOUT = (2, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0)  # плоская огибающая громкости: нужна для fadeout (vgm)
ENV_FLAT_PAN_LOOP = (2, 2, 0, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0)  # плоская громкость + петля панорамы (sid)


class Cell:
    """Ячейка паттерна. Пустая ячейка (все поля 0) не пишется."""
    __slots__ = ('note', 'inst', 'vol', 'eff', 'par')

    def __init__(self, note=0, inst=0, vol=0, eff=0, par=0):
        self.note, self.inst, self.vol, self.eff, self.par = note, inst, vol, eff, par


class XmSample:
    """Один сэмпл. data - целые 16 бит (массив numpy или список); петля - в кадрах."""

    def __init__(self, data, name='', loop_start=0, loop_length=0, loop_type=None,
                 volume=64, finetune=0, panning=128, relative_note=0):
        self.data = data
        self.name = name
        self.loop_start = loop_start
        self.loop_length = loop_length
        # loop_type: 0 нет, 1 вперёд, 2 пинг-понг; по умолчанию - прямая петля, если длина петли > 0
        self.loop_type = (1 if loop_length else 0) if loop_type is None else loop_type
        self.volume = volume
        self.finetune = finetune
        self.panning = panning
        self.relative_note = relative_note


class XmInstrument:
    def __init__(self, name, samples=None, keymap=None, fadeout=0, env_flags=ENV_OFF, env_x=100):
        self.name = name
        self.samples = list(samples or [])
        self.keymap = keymap                  # 96 номеров сэмплов по нотам; None = все на первый сэмпл
        self.fadeout = fadeout
        self.env_flags = env_flags
        self.env_x = env_x                    # X второй точки огибающих громкости и панорамы

    @classmethod
    def simple(cls, name, data, loop=None, rel=0, finetune=0, **kw):
        """Инструмент из одного сэмпла; loop = (начало, длина) в кадрах или None."""
        ls, ll = loop if loop else (0, 0)
        return cls(name, [XmSample(data, name, ls, ll, finetune=finetune, relative_note=rel)], **kw)


def _name(s, n=22):
    return s.encode('ascii', 'replace')[:n].ljust(n, b'\x00')


def _delta16(data):
    d = np.clip(np.asarray(data, dtype=np.int64), -32768, 32767)
    d = np.diff(d, prepend=0)
    return ((d + 32768) % 65536 - 32768).astype('<i2').tobytes()


# ═══════════════════════════════════════════════════════════════
# Бесшовные петли
# ═══════════════════════════════════════════════════════════════

SMOOTH_LOOPS = os.environ.get('XM_LOOP_FIX', '1') != '0'   # исправлять стыки петель при записи; XM_LOOP_FIX=0 - писать как есть
LOOP_MIN = 64                # более короткие петли (однопериодные волны чипов) не трогаем
LOOP_CTX = 32                # длина окна, по которому сравниваются «что было перед началом» и «перед концом»


def _wrap_error(x, ls, e):
    """Насколько стык e-1 -> ls отличается от гладкого продолжения (предсказание по уровню и наклону)."""
    return abs(x[ls] - (2.0 * x[e - 1] - x[e - 2]))


def smooth_loop(data, loop_start, loop_length, ctx=LOOP_CTX):
    """Подбирает точки петли так, чтобы на стыке не было щелчка.

    Если после конца петли звук прыгает (ошибка стыка заметно больше самых резких мест внутри петли),
    ищем пару (начало, конец) рядом с исходной, у которой 32 кадра перед концом максимально похожи на
    32 кадра перед началом: тогда переход «конец -> начало» продолжает волну так же, как она шла перед
    началом петли. Данные не меняются, двигаются только границы (конец - только к началу, чтобы
    исходный разрыв не оказался внутри петли).

    -> (начало, длина, изменено)."""
    ls, ll = int(loop_start), int(loop_length)
    if ll < LOOP_MIN or ls + ll > len(data) or ls < 0:
        return ls, ll, False
    x = np.asarray(data, dtype=np.float64)
    e0 = ls + ll
    seg = x[ls:e0]
    d2 = np.abs(seg[2:] - 2.0 * seg[1:-1] + seg[:-2])
    c0 = _wrap_error(x, ls, e0)
    if c0 <= 1.05 * d2.max() + 2.0:                      # стык не хуже внутренних резких мест - оставляем
        return ls, ll, False
    ws = max(min(ll // 8, 1024), ctx - ls)               # насколько можно сдвинуть начало к концу
    we = min(ll // 4, 4096)                              # насколько можно сдвинуть конец к началу
    ls_c = np.arange(max(ls, ctx), ls + ws + 1)
    if len(ls_c) == 0:
        return ls, ll, False
    e_c = np.arange(max(int(ls_c[0]) + ll // 2, e0 - we), e0 + 1)
    if len(e_c) == 0:
        return ls, ll, False
    A = x[e_c[:, None] - ctx + np.arange(ctx)[None, :]]
    B = x[ls_c[:, None] - ctx + np.arange(ctx)[None, :]]
    D = (A ** 2).sum(1)[:, None] + (B ** 2).sum(1)[None, :] - 2.0 * (A @ B.T)
    # из лучших по совпадению окон берём ту пару, у которой ещё и сам стык гладкий
    order = np.argsort(D, axis=None, kind='stable')[:64]      # D - целые числа в float64 (точные), ничьи по номеру
    best = None
    for k in order:
        i, j = np.unravel_index(k, D.shape)
        ne, nls = int(e_c[i]), int(ls_c[j])
        err = _wrap_error(x, nls, ne)
        if best is None or err < best[0]:
            best = (err, nls, ne)
    err, nls, ne = best
    if err > 0.5 * c0:                                   # заметного улучшения нет - не трогаем
        return ls, ll, False
    return nls, ne - nls, True


def pack_pattern(rows, nch):
    """rows: список строк, строка - список из nch ячеек (Cell или None). -> байты упакованного паттерна."""
    packed = bytearray()
    for row in rows:
        for ch in range(nch):
            c = row[ch] if ch < len(row) else None
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
            for v, bit in ((c.note, 1), (c.inst, 2), (c.vol, 4), (c.eff, 8), (c.par, 16)):
                if flag & bit:
                    packed.append(v & 0xFF)
    return bytes(packed)


def grid_to_patterns(grid, nrows, nch, rows_per_pattern=64, tag='xm'):
    """{(строка, канал): Cell} -> список паттернов (максимум 256, хвост обрезается)."""
    rpp = rows_per_pattern
    npat = max(1, (nrows + rpp - 1) // rpp)
    if npat > 256:
        print(f"[{tag}] ВНИМАНИЕ: {npat} паттернов > 256, хвост обрезан (используйте --compact)", file=sys.stderr)
        npat = 256
    return [[[grid.get((p * rpp + r, c)) for c in range(nch)] for r in range(rpp)] for p in range(npat)]


def _write_instrument(f, inst):
    name = _name(inst.name)
    ss = inst.samples
    fixed_data = []
    if not ss:
        f.write(struct.pack('<I', 29) + name + struct.pack('<BH', 0, 0))     # по спецификации: без сэмплов заголовок 29 байт
        return []
    f.write(struct.pack('<I', INSTRUMENT_HEADER_SIZE) + name)
    f.write(struct.pack('<BHI', 0, len(ss), 40))
    km = bytearray(96)
    for i, s in enumerate((inst.keymap or [])[:96]):
        km[i] = s
    f.write(km)
    x = inst.env_x
    ve = bytearray(48)
    struct.pack_into('<HHHH', ve, 0, 0, 64, x, 64)
    f.write(ve)
    pe = bytearray(48)
    struct.pack_into('<HHHH', pe, 0, 0, 32, x, 32)
    f.write(pe)
    f.write(bytes(inst.env_flags))
    f.write(struct.pack('<H', int(inst.fadeout)))
    f.write(b'\x00' * (INSTRUMENT_HEADER_SIZE - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
    fixed = []
    for s in ss:
        data, ls, ll = s.data, s.loop_start, s.loop_length
        if SMOOTH_LOOPS and s.loop_type == 1:
            nls, nll, changed = smooth_loop(data, ls, ll)
            if changed:
                data = np.asarray(data)[:nls + nll]
                fixed.append((s, ls, ll, nls, nll))
                ls, ll = nls, nll
        fixed_data.append(data)
        f.write(struct.pack('<III', len(data) * 2, ls * 2, ll * 2))
        f.write(struct.pack('<Bb', s.volume, max(-128, min(127, s.finetune))))
        f.write(struct.pack('<BBbB', (s.loop_type & 3) | 0x10, s.panning, max(-128, min(127, s.relative_note)), 0))
        f.write(_name(s.name or inst.name))
    for data in fixed_data:
        f.write(_delta16(data))
    return fixed


def write_xm(path, title, tracker, nch, bpm, speed, patterns, instruments, order=None, linear=True):
    """patterns - список паттернов (строки из ячеек Cell/None); instruments - список XmInstrument.
    linear=False - таблица частот Amiga (флаг 0): так вибрато и слайды 1xx/2xx/3xx звучат как в MOD."""
    order = list(order) if order is not None else list(range(len(patterns)))
    instruments = instruments[:MAX_XM_INSTRUMENTS]
    with open(path, 'wb') as f:
        f.write(b'Extended Module: ')
        f.write(title.encode('ascii', 'replace')[:20].ljust(20, b' '))
        f.write(b'\x1a')
        f.write(tracker.encode('ascii', 'replace')[:20].ljust(20, b' '))
        f.write(struct.pack('<HI', 0x0104, 276))
        f.write(struct.pack('<HHHHHHHH', len(order), 0, nch, len(patterns), len(instruments), 1 if linear else 0, speed, bpm))
        ot = bytearray(256)
        for i, o in enumerate(order[:256]):
            ot[i] = o
        f.write(ot)
        for pat in patterns:
            packed = pack_pattern(pat, nch)
            f.write(struct.pack('<IBHH', 9, 0, len(pat), len(packed)))
            f.write(packed)
        fixed = []
        for inst in instruments:
            fixed += _write_instrument(f, inst)
    if fixed:
        worst = max(fixed, key=lambda t: t[2] - t[4])
        print(f"[xm] петли: подправлены стыки у {len(fixed)} сэмплов "
              f"(напр. {worst[0].name or '?'}: начало {worst[1]}->{worst[3]}, длина {worst[2]}->{worst[4]})")


class XMWriter:
    """title, nch, bpm, speed; tracker - 20-символьная подпись программы в заголовке."""

    def __init__(self, title, nch, bpm, speed, tracker='XM Converter'):
        self.title, self.nch, self.bpm, self.speed, self.tracker = title, nch, bpm, speed, tracker
        self.patterns = []
        self.instruments = []

    def set_rows(self, grid, nrows, rows_per_pattern=64):
        self.patterns = grid_to_patterns(grid, nrows, self.nch, rows_per_pattern)

    def write(self, path):
        write_xm(path, self.title, self.tracker, self.nch, self.bpm, self.speed, self.patterns, self.instruments)
