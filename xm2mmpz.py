#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm2mmpz.py — конвертер XM (FastTracker 2) -> проект LMMS (.mmpz / .mmp)

Берёт любой .xm (не только полученный конвертерами этого репозитория) и собирает проект
LMMS 1.2+: инструменты AudioFileProcessor, ноты, петли, автоматизация громкости/панорамы,
изгибы высоты (3xx), смены темпа. Подробности и ограничения - в mmpz_export.py и README.

Примеры:
  python3 xm2mmpz.py song.xm                      # song.mmpz + song_samples/*.wav
  python3 xm2mmpz.py song.xm --mmp                # song.mmp (несжатый XML)
  python3 xm2mmpz.py song.xm --embed              # сэмплы внутри проекта, один файл
  python3 xm2mmpz.py a.xm b.xm -d out/            # пакетно, всё в каталог out/
  python3 xm2mmpz.py song.xm -o proj/my.mmpz --bpm 140 --gain 40

Только numpy; LMMS для создания файла не нужен.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mmpz_export  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='XM -> проект LMMS (.mmpz / .mmp)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Примеры:' + __doc__.split('Примеры:')[1].split('Только numpy')[0].rstrip())
    ap.add_argument('input', nargs='+', help='файл(ы) .xm')
    ap.add_argument('-o', '--output', help='выходной файл (только для одного входного); расширение .mmp или .mmpz '
                                           'определяет формат')
    ap.add_argument('-d', '--outdir', help='каталог для результатов (по умолчанию рядом с .xm)')
    ap.add_argument('--mmp', action='store_true', help='писать несжатый .mmp вместо .mmpz')
    ap.add_argument('--embed', action='store_true',
                    help='встроить сэмплы в проект (иначе WAV в <имя>_samples/; LMMS 1.2.2 теряет встроенные '
                         'сэмплы при экспорте через `lmms -r`)')
    ap.add_argument('--samples-dir', help='куда класть WAV-сэмплы (по умолчанию <проект>_samples/)')
    ap.add_argument('--bpm', type=int, help='темп проекта LMMS (по умолчанию подбирается по строке XM)')
    ap.add_argument('--gain', type=int, help='общая громкость проекта, %% (по умолчанию подбирается без клиппинга)')
    a = ap.parse_args(argv)
    if a.output and len(a.input) > 1:
        ap.error('-o работает только с одним входным файлом, для нескольких используйте -d')
    if a.samples_dir and len(a.input) > 1:
        ap.error('--samples-dir работает только с одним входным файлом')
    if a.outdir:
        os.makedirs(a.outdir, exist_ok=True)
    ext = '.mmp' if a.mmp else '.mmpz'
    done = failed = 0
    for src in a.input:
        if not os.path.isfile(src):
            print(f'Не найден: {src}', file=sys.stderr)
            failed += 1
            continue
        if a.output:
            out = a.output
        else:
            base = os.path.splitext(os.path.basename(src))[0] + ext
            out = os.path.join(a.outdir or os.path.dirname(os.path.abspath(src)), base)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            mmpz_export.convert_file(src, out, embed=a.embed, bpm=a.bpm, gain=a.gain, samples_dir=a.samples_dir)
            done += 1
        except Exception as e:
            print(f'{src}: ошибка: {e}', file=sys.stderr)
            failed += 1
    if len(a.input) > 1:
        print(f'Готово: {done}, ошибок: {failed}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
