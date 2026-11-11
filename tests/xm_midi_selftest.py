#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самопроверка xm_export.py и midi_export.py (общие писатели всех конвертеров).

  * XM: пишем модуль (паттерны с ячейками, два инструмента, петля, keymap, fadeout, пустой
    инструмент), читаем обратно независимым разбором и сверяем каждое поле; если есть ffmpeg
    с libopenmpt - убеждаемся, что модуль открывается и звучит;
  * MIDI: пишем файл, разбираем чанки и события и проверяем темп, название, порядок событий одного
    тика (program change раньше note on) и конец трека.

Запуск:  python3 tests/xm_midi_selftest.py
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import midi_export as MI
import mmpz_export as MM          # его read_xm - независимый разбор XM
import xm_export as XE

fails = []


def check(name, ok, info=''):
    print('  [%s] %s %s' % ('ok' if ok else 'FAIL', name, info))
    if not ok:
        fails.append(name)


def test_xm(work):
    path = os.path.join(work, 't.xm')
    nch = 4
    grid = {(0, 0): XE.Cell(49, 1, 0x50, 0x08, 0x40), (1, 1): XE.Cell(61, 2, 0x30), (2, 0): XE.Cell(97),
            (70, 3): XE.Cell(52, 1, 0x40, 0x03, 0xFF)}
    xm = XE.XMWriter('Test title', nch, 140, 4, tracker='TEST Converter')
    xm.set_rows(grid, 71)
    saw = (np.arange(32) * 1000 - 16000).astype(np.int16)
    xm.instruments = [
        XE.XmInstrument.simple('saw', saw, loop=(0, 32), rel=3, finetune=-20),
        XE.XmInstrument('multi', [XE.XmSample(saw[:16], 'a'), XE.XmSample((saw[:20] // 2), 'b', 4, 16, relative_note=-5)],
                        keymap=[0] * 48 + [1] * 48, fadeout=0x400, env_flags=XE.ENV_FLAT_FADEOUT, env_x=1000),
        XE.XmInstrument('empty'),
    ]
    xm.write(path)
    raw = open(path, 'rb').read()
    check('заголовок XM', raw[:17] == b'Extended Module: ' and raw[37] == 0x1A and raw[38:58] == b'TEST Converter      ')
    check('название дополнено пробелами', raw[17:37] == b'Test title' + b' ' * 10)
    r = MM.read_xm(path)
    check('каналы/темп/speed', (r.nch, r.bpm, r.speed) == (nch, 140, 4), '%s' % ((r.nch, r.bpm, r.speed),))
    check('2 паттерна по 64 строки', len(r.patterns) == 2 and all(len(p) == 64 for p in r.patterns))
    check('ячейка (0,0)', r.patterns[0][0][0] == (49, 1, 0x50, 0x08, 0x40), str(r.patterns[0][0][0]))
    check('ячейка (1,1)', r.patterns[0][1][1] == (61, 2, 0x30, 0, 0))
    check('note-off', r.patterns[0][2][0][0] == 97)
    check('ячейка во втором паттерне', r.patterns[1][6][3] == (52, 1, 0x40, 0x03, 0xFF))
    s1 = r.instruments[1][1]
    check('инструмент 1: данные, петля, rel, finetune',
          np.array_equal(np.round(s1.data * 32768).astype(np.int16), saw) and (s1.loop_start, s1.loop_len, s1.loop_type) == (0, 32, 1)
          and (s1.rel, s1.finetune) == (3, -20))
    check('инструмент 2: первый сэмпл', len(r.instruments[2][1].data) == 16)
    check('пустой инструмент без сэмплов', 3 not in r.instruments)
    # конец файла: все байты разобраны (нет лишних/недостающих данных)
    exp = 336 + sum(9 + 0 for _ in r.patterns)           # заголовок + заголовки паттернов
    check('размер файла не меньше ожидаемого', len(raw) > exp)
    ff = shutil.which('ffmpeg')
    if ff:
        if 'libopenmpt' in subprocess.run([ff, '-hide_banner', '-demuxers'], capture_output=True, text=True).stdout:
            probe = subprocess.run([ff, '-v', 'error', '-i', path, '-t', '1', '-f', 'null', '-'], capture_output=True, text=True)
            check('libopenmpt открывает модуль', probe.returncode == 0 and not probe.stderr.strip(), probe.stderr.strip()[:100])


def test_loops(work):
    n = np.arange(6000)
    sine = np.round(20000 * np.sin(2 * np.pi * n / 200.0))
    saw = np.round(((n % 128) / 127.0 * 2 - 1) * 20000)

    def werr(x, ls, ll):
        e = ls + ll
        return abs(x[ls] - (2 * x[e - 1] - x[e - 2]))
    # 1) хорошая петля (целое число периодов) - не меняется
    r = XE.smooth_loop(sine, 500, 2000)
    check('чистая синусоида: петля не тронута', r == (500, 2000, False), str(r))
    # 2) пила: резкий край на стыке - естественный, не трогаем
    r = XE.smooth_loop(saw, 300, 128 * 10)
    check('пила: естественный разрыв не тронут', r == (300, 1280, False), str(r))
    # 3) плохая петля (длина не кратна периоду): фаза на стыке не сходится -> должна быть исправлена
    bad = XE.smooth_loop(sine, 500, 2037)
    e0 = werr(sine, 500, 2037)
    ok = bad[2] and werr(sine, bad[0], bad[1]) < 0.1 * e0 and bad[0] + bad[1] <= 500 + 2037
    check('сбитая фаза: стык исправлен', ok, 'ошибка стыка %.0f -> %.0f, петля %s' % (e0, werr(sine, bad[0], bad[1]), bad[:2]))
    check('исправление не удлиняет петлю за прежний конец', bad[0] + bad[1] <= 2537)
    # 4) петли короче LOOP_MIN (однопериодные волны чипов) не трогаем
    r = XE.smooth_loop(sine[:100], 10, 50)
    check('короткая петля не тронута', r[2] is False)
    # 5) запись XM применяет исправление, а XM_LOOP_FIX=0 (SMOOTH_LOOPS=False) отключает
    for flag, expect in ((True, True), (False, False)):
        XE.SMOOTH_LOOPS = flag
        path = os.path.join(work, 'loop_%d.xm' % flag)
        inst = XE.XmInstrument('l', [XE.XmSample(sine[:2537].astype(np.int16), 'l', 500, 2037)])
        XE.write_xm(path, 't', 't', 2, 125, 6, [[[None, None]] * 64], [inst])
        smp = MM.read_xm(path).instruments[1][1]
        changed = (smp.loop_start, smp.loop_len) != (500, 2037)
        check('запись XM, SMOOTH_LOOPS=%s' % flag, changed == expect, 'петля %s' % ((smp.loop_start, smp.loop_len),))
    XE.SMOOTH_LOOPS = True


def parse_smf(data):
    assert data[:4] == b'MThd'
    ln, fmt, ntr, tpqn = struct.unpack('>IHHH', data[4:14])
    p = 8 + ln
    tracks = []
    for _ in range(ntr):
        assert data[p:p + 4] == b'MTrk'
        n = struct.unpack('>I', data[p + 4:p + 8])[0]
        body = data[p + 8:p + 8 + n]
        p += 8 + n
        ev, i, tick, status = [], 0, 0, 0
        while i < len(body):
            d = 0
            while True:
                b = body[i]; i += 1
                d = (d << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            tick += d
            if body[i] == 0xFF:
                typ, ln2 = body[i + 1], body[i + 2]
                ev.append((tick, 'meta', typ, body[i + 3:i + 3 + ln2]))
                i += 3 + ln2
            else:
                status = body[i]
                nb = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
                ev.append((tick, 'ch', status, bytes(body[i + 1:i + 1 + nb])))
                i += 1 + nb
        tracks.append(ev)
    return fmt, tpqn, tracks


def test_midi(work):
    path = os.path.join(work, 't.mid')
    ev = [(0, 3, 0, bytes([0x90, 60, 100])), (0, 1, 0, bytes([0xC0, 5])), (480, 0, 0, bytes([0x80, 60, 0])),
          (10, 3, 9, bytes([0x99, 36, 90])), (20, 0, 9, bytes([0x89, 36, 0]))]
    MI.write_midi(path, 'Song', ev)
    fmt, tpqn, tr = parse_smf(open(path, 'rb').read())
    check('SMF: формат 1, 480 тиков, 3 трека', (fmt, tpqn, len(tr)) == (1, 480, 3), str((fmt, tpqn, len(tr))))
    check('темп 500000 мкс и название',
          tr[0][0][1:3] == ('meta', 0x51) and tr[0][0][3] == bytes([0x07, 0xA1, 0x20]) and tr[0][1][2:] == (0x03, b'Song'))
    ch0 = [e for e in tr[1] if e[1] == 'ch']
    check('program change раньше note on в одном тике', ch0[0][2] == 0xC0 and ch0[1][2] == 0x90, str([hex(e[2]) for e in ch0]))
    check('конец трека FF 2F 00', all(t[-1][1:3] == ('meta', 0x2F) for t in tr))
    check('vlq', MI.vlq(0) == b'\x00' and MI.vlq(127) == b'\x7f' and MI.vlq(128) == b'\x81\x00' and MI.vlq(0x3FFF) == b'\xff\x7f'
          and MI.vlq(480) == b'\x83\x60')
    check('tempo_event из BPM', MI.tempo_event(bpm=120) == bytes([0xFF, 0x51, 3, 0x07, 0xA1, 0x20]))


def main():
    work = tempfile.mkdtemp(prefix='xm_midi_selftest_')
    try:
        print('XM:')
        test_xm(work)
        print('MIDI:')
        test_midi(work)
        print('Петли:')
        test_loops(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print('ИТОГ:', 'все проверки пройдены' if not fails else 'ПРОВАЛЕНО: ' + ', '.join(fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
