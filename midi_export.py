#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
midi_export.py — единая запись стандартного MIDI-файла (SMF формата 1) для всех конвертеров.

Раньше у каждого *2xm-midi-wav.py был свой vlq()/запись файла; здесь они общие:

    vlq(v)                           число переменной длины (VLQ)
    tempo_event(bpm=None, usec=None) мета-событие «темп»
    text_event(text, kind, maxlen)   мета-событие «текст» (kind 0x01) или «имя трека» (0x03)
    write_smf(path, tracks, tpqn)    записать файл; track = список (тик, порядок, байты события)
    write_midi(path, title, events)  вариант для конвертеров, у которых события уже разложены
                                     по MIDI-каналам: events = [(тик, порядок, канал, байты)]

События одного тика сортируются по `порядок` (меньше - раньше), поэтому, например,
program change / controller ставятся перед note on. Конец трека (FF 2F 00) добавляется сам.
"""
import collections
import struct

TPQN = 480

# общие таблицы для разбора ударных и подбора GM-программ
DRUM_NOTES = [36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56]
GM_PROGRAMS = [0, 25, 33, 48, 80, 81, 73, 56, 4, 11, 19, 24, 38, 46, 52, 61,
               71, 65, 87, 90, 95, 98, 104, 108]


def vlq(v):
    v = max(0, int(v))
    out = [v & 0x7F]
    v >>= 7
    while v:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    return bytes(reversed(out))


def tempo_event(bpm=None, usec=None):
    """FF 51 03 tt tt tt: темп в микросекундах на четверть (или из BPM)."""
    if usec is None:
        usec = int(60000000 / bpm) if bpm else 500000
    return bytes([0xFF, 0x51, 0x03]) + int(usec).to_bytes(3, 'big')


def text_event(text, kind=0x03, maxlen=100):
    """FF kind len text: kind 0x01 - текст, 0x03 - имя трека."""
    t = text.encode('ascii', 'replace')[:maxlen]
    return bytes([0xFF, kind, len(t)]) + t


def track_chunk(events):
    """events: [(тик, порядок, байты)] -> готовый чанк MTrk (с сортировкой и концом трека)."""
    body = bytearray()
    prev = 0
    for tick, _, data in sorted(events, key=lambda e: (e[0], e[1])):
        body += vlq(tick - prev) + data
        prev = tick
    body += vlq(0) + b'\xFF\x2F\x00'
    return b'MTrk' + struct.pack('>I', len(body)) + bytes(body)


def write_smf(path, tracks, tpqn=TPQN):
    """tracks: список треков, трек - список (тик, порядок, байты)."""
    out = bytearray(b'MThd' + struct.pack('>IHHH', 6, 1, len(tracks), tpqn))
    for tr in tracks:
        out += track_chunk(tr)
    with open(path, 'wb') as f:
        f.write(out)


def write_midi(path, title, midi_events, tpqn=TPQN, bpm=None):
    """midi_events: список (тик, порядок, канал, байты). Один трек на MIDI-канал,
    первым идёт служебный трек с темпом (по умолчанию 120 BPM) и названием."""
    tracks = collections.defaultdict(list)
    for tick, order, ch, data in midi_events:
        tracks[ch].append((tick, order, data))
    head = [(0, 0, tempo_event(bpm))]
    if title:
        head.append((0, 1, text_event(title, 0x03, 100)))
    write_smf(path, [head] + [tracks[c] for c in sorted(tracks)], tpqn)
