# VideoGameMusicConverters

Python tools that convert video game music into **XM** (tracker module), **MIDI** and **WAV**.

| Script | Source | Backend |
|---|---|---|
| `spc2xm-midi-wav.py` | SNES `.spc` | `spc_pyemu.py` (Python) / `spc_tracer/spctrace` (C++ GME, optional) |
| `usf2xm-midi-wav.py` | N64 `.usf` / `.miniusf` | `usf_pyemu.py` (Python) / `usf_tracer/usftrace` (C++ lazyusf2, optional) |
| `nsf2xm-midi-wav.py` | NES `.nsf` | `nsf_pyemu.py` (Python) |
| `vgm2xm-midi-wav.py` | Mega Drive `.vgm` / `.vgz` | `vgm_pyemu.py` (Python) / `vgm_render/vgm2wav` (C++ libvgm, optional) |
| `sid2xm-midi-wav.py` | Commodore 64 `.sid` | built-in 6502 + SID model |
| `psg2xm-midi-wav.py` | ZX Spectrum / AY `.psg` dumps | built-in AY emulator |
| `pt32xm-midi-wav.py` | ZX Spectrum Pro Tracker 3 `.pt3` | built-in AY emulator |

!! These tools were created with the help of A.I. for testing purposes, but they turned out to be fully functional =)
After conversion some final polishing may be needed ...

## Requirements
`python3` + `numpy` (+ `scipy` for 44.1 kHz WAV in the SPC converter). The C++ backends are optional and only make things faster:
`--build-tracer` (SPC, USF) / `--build-renderer` (VGM) build them (needs git, cmake/gcc/make; USF also zlib).
`--tracer auto|native|python` (`--renderer` for VGM) selects the backend, `auto` = the C++ one if it is built, otherwise Python.

## Common options
Most `*2xm-midi-wav.py` scripts share these:

| Option | Meaning |
|---|---|
| `-o NAME` | base name of the output files |
| *(none)* / `--midi` / `--midi-only` | XM / XM + MIDI / MIDI only |
| `--wav` / `--wav-only` / `--wav-file F` | add WAV / WAV only / WAV file name |
| `--mmpz` / `--mmpz-embed` | add an LMMS project (`.mmpz`); embed the samples into it |
| `--all` | XM + MIDI + WAV + LMMS |
| `--seconds N` | how many seconds to emulate (if the length is not in the file) |
| `--octave N`, `--transpose N` | shift note *names* only, the sound is unchanged |
| `--compact N` | make rows N times coarser (fewer patterns) |
| `--bpm N`, `--speed N` | set the tempo manually |
| `--norm-scale X` | XM volume scale (smaller = louder) |

Run `python3 <script> --help` for the full list.

## LMMS project (.mmpz)
`--mmpz` adds `NAME.mmpz` (LMMS 1.2+) to any converter. It is built from the XM by the shared `mmpz_export.py` (numpy only, LMMS is not needed to create the file):

* every XM instrument becomes an *AudioFileProcessor* instrument; notes sounding together on one instrument are spread over several tracks;
* samples are written next to the project to `NAME_samples/*.wav` (44.1 kHz, already pitched so that LMMS key C4 = XM C-4, `relative note`/`finetune` included) and referenced by absolute path, so move the project together with that folder;
  `--mmpz-embed` stores them inside the `.mmpz` instead (one file), but LMMS 1.2.2 loses embedded samples when exporting with `lmms -r` (not checked in the GUI);
* loops are kept; the loop length is rounded to whole frames and the leftover is compensated with the track's *Pitch*, so looped chip waveforms stay in tune;
* volume (volume column, `Cxx`) and panning (`8xx`) become automation of the track; smooth pitch changes (`3xx`) become *detuning* automation inside one note (no re-attack);
* the tempo of the project is chosen so that an LMMS tick is much shorter than an XM row; note positions follow real time, XM tempo changes (`Fxx`) are respected;
* the master volume is set automatically so the sum does not clip (`mmpz_export.py --gain N` to override).

**`xm2mmpz.py`** converts any existing `.xm` (also not from these tools): `python3 xm2mmpz.py song.xm` -> `song.mmpz`; `--mmp` writes plain `.mmp`, `--embed` embeds the samples, `-d DIR` / `-o FILE` choose the output, several files at once are fine, `--bpm`/`--gain` override the automatic values. `python3 tests/mmpz_selftest.py` checks pitch, loops, bends and volume on a synthetic module (renders with `lmms` if it is installed).
Limits: LMMS 1.2 has no per-note volume envelope, so volume changes inside a note go through the track automation; tempo is not exported as automation.

## Shared writers
All converters write their files through three common modules (no more per-script copies):

| Module | Purpose |
|---|---|
| `xm_export.py` | XM writer: `Cell`, `XmSample`/`XmInstrument`, `grid_to_patterns`, `write_xm`, `XMWriter` |
| `midi_export.py` | Standard MIDI File writer: `write_smf`, `write_midi`, `vlq`, tempo/text events, GM tables |
| `mmpz_export.py` | LMMS project from an XM (see above) |

Self-checks: `python3 tests/xm_midi_selftest.py` (XM/MIDI round trip), `python3 tests/mmpz_selftest.py` (LMMS project).
Seamless loops: `xm_export.smooth_loop()` runs when any converter writes an XM (and when `mmpz_export.py` reads one). If the jump from the loop end back to its start is clearly worse than the sharpest spot inside the loop (this is what makes a sustained note crackle), it looks for a nearby loop start/end pair whose 32 preceding samples match best, so the wave continues smoothly; the data itself is not changed, only the loop points (the end is only moved towards the start). Normal loops (clean periods, saw edges, one-cycle chip waves) are left untouched. `XM_LOOP_FIX=0` in the environment turns this off.
The module title in XM is padded with spaces (the PSG/PT3/SID scripts used to pad it with NUL bytes); all other bytes of the files are unchanged.

## SPC (SNES) -> XM / MIDI / WAV
`spc_tracer/spctrace` = [Game Music Emu](https://github.com/libgme/game-music-emu) + a small patch in the S-DSP: it logs every voice start
(sample, pitch, volume), the state of every active voice every 4 ms (pitch, envelope, volume L/R) and the decoded BRR blocks.
`spc_pyemu.py` is a bit-exact Python port of the same code (1-2.5x real time, the C++ one takes under a second per minute).

* loops are taken from the BRR data (steady-state pass), the sample pitch is found by autocorrelation, the DSP Gaussian filter is applied;
* note volume = envelope x voice volume, pan from volume L/R, pitch slides become `3FF` glides;
* the row grid is detected from the note starts, the BPM is nudged with `Fxx` to keep the time exact (drift < 10 ms over 150 s);
* spectrum correlation with the emulator render: 0.90-0.96, pitch agreement 0.96-0.98.

```bash
python3 spc2xm-midi-wav.py music.spc                    # XM, length from ID666 or 120 s
python3 spc2xm-midi-wav.py music.spc --midi --seconds 90
python3 spc2xm-midi-wav.py music.spc --all              # XM + MIDI + WAV (GME render, 44.1 kHz, needs scipy; otherwise 32 kHz)
python3 spc2xm-midi-wav.py music.spc --compact 2        # coarser rows
python3 spc2xm-midi-wav.py music.spc --max-row 0.03     # finer rows (more patterns)
python3 spc2xm-midi-wav.py music.spc --octave 1 --transpose 2
python3 spc2xm-midi-wav.py music.spc --tracer python    # force the Python emulator
python3 spc2xm-midi-wav.py --build-tracer               # (optional) build the C++ tracer
```
Other options: `--channels` (max XM channels, 32), `--release-threshold`, `--bend-threshold`, `--limit-knee`, `--samples`, `--no-gauss`, `--no-pan`, `--keep-trace`.

Limitations: echo/reverb is not reproduced in XM/MIDI (it is in the WAV); pitch modulation between voices is ignored; noise voices become a
generic noise sample; the XM is ~6 dB quieter than the emulator (see `--norm-scale`).

## USF / miniusf (N64) -> XM / MIDI / WAV
Works like the SPC converter, but the log comes from an N64 emulator ([lazyusf2](https://github.com/derselbst/lazyusf2) + a patch in `usf_tracer/`):
every 5 ms it logs the RSP audio commands of each voice (ADPCM / RESAMPLE / ENVMIXER = sample, pitch, volume/pan) and the decoded VADPCM samples.
Without a compiler `usf_pyemu.py` is used (R4300 interpreter + HLE of the RSP audio microcode; 2-4x slower than real time).

```bash
# the .usflib named in the _lib tag must lie next to the .miniusf
python3 usf2xm-midi-wav.py sparse00.miniusf                    # XM
python3 usf2xm-midi-wav.py sparse00.miniusf --all --seconds 90 # XM + MIDI + WAV
python3 usf2xm-midi-wav.py sparse00.miniusf --compact 2
python3 usf2xm-midi-wav.py sparse00.miniusf --bpm 150 --speed 3
python3 usf2xm-midi-wav.py --build-tracer                      # (optional) build the C++ tracer
```
Other options: `--seconds` (default: `length`+`fade` tags, else 120), `--channels` (32), `--release-threshold` (0.04), `--bend-threshold` (0.75 semitones, bigger changes become `3FF` glides), `--no-pan`, `--keep-trace`.

Limitations: audio microcodes libultra ABI1, ABI2 ("nead": Zelda, Mario Kart, Star Fox, ...), ABI3 ("naudio": Banjo, DK64, Perfect Dark, Conker, ...) and MusyX v1/v2 (007 TWINE, Rogue Squadron, Indiana Jones, ...). The native tracer handles ABI1 and MusyX; ABI2/ABI3 are handled by the built-in Python emulator (`usf2xm-midi-wav.py` switches to it automatically when the native tracer finds no voices; the Python ports were checked against the lazyusf2 C code on random alist programs, but there was no real ABI2/ABI3 game to test the whole chain). MusyX in the Python emulator matches the native tracer bit-for-bit on 900 real 007 tasks, but the Python R4300 core does not get 007 past the first audio frames yet, so for MusyX games use the native tracer. The ABI2/ABI3 ports can be re-checked against the C code with `usf_tracer/abitest/run.sh`. MP3 commands (ABI3) and the ENVMIXER of GoldenEye/Blast Corps are not emulated (rebuild the native tracer with `--build-tracer` after updating); envelopes are
approximated with volume-column steps; sample reverb (aux bus) is not reproduced in XM/MIDI (it is in the WAV).

## NSF (NES) -> XM / MIDI / WAV
`nsf_pyemu.py` = a 6502 interpreter (official + common illegal opcodes, cycle counts, bank switching) + a real 2A03 model: frame counter,
pulse (envelope, sweep, length), triangle (linear counter), noise (LFSR), DMC and `$4011` PCM. Pure Python + numpy. PLAY runs at the exact
console rate (60.0988 / 50.007 Hz). The WAV is rendered from the same log (checked against libgme: RMS 0.94-1.05, envelope correlation median 0.90).

* a note = re-trigger or silence -> sound / duty change; pitch and volume changes inside a note come from the log, the end = length counter / volume 0;
* instruments = the waveforms themselves: pulse (4 duties) and triangle (band-limited, one sample per octave), noise = the real LFSR, DMC = the decoded delta stream;
* rows = PLAY frames (`--compact 2` by default so that `3FF` works); XM volume is tied to the WAV, loud notes are softly limited (`--limit-knee`).

```bash
python3 nsf2xm-midi-wav.py game.nsf                     # starting track -> game.xm
python3 nsf2xm-midi-wav.py game.nsf --track 5 --all     # XM + MIDI + WAV of track 5
python3 nsf2xm-midi-wav.py game.nsf --all-tracks --midi # game_track01.xm, game_track01.mid, ...
python3 nsf2xm-midi-wav.py game.nsf --samples 1,3       # only these instruments (numbers from the printed table)
python3 nsf2xm-midi-wav.py game.nsf --seconds 60 --compact 4
```
Other options: `--stop-silence` (stop after N s of silence, 5), `--tail`, `--wav-gain`, `--zone-width`, `--bend-threshold`, `--channels`.

Limitations: expansion chips (VRC6, VRC7, FDS, MMC5, N163, Sunsoft 5B) are not emulated (only the 2A03 is heard, a warning is printed);
IRQ/NMI-driven players are not supported; fast arpeggios/tremolo (faster than a row) are smoothed by the XM grid.

## VGM (Mega Drive) -> XM / MIDI / WAV
YM2612 + SN76489 only (other chips are reported and skipped). The timbres come from a real chip emulator:

* `vgm_pyemu.py` - Python + numpy, follows the libvgm cores (output matches libvgm sample for sample), 4-8x faster than real time;
* `vgm_render/vgm2wav` ([libvgm](https://github.com/ValleyBell/libvgm), optional, faster) is used automatically when built.

The VGM is parsed into notes; every unique FM patch is rendered by the emulator at the needed pitches, a seamless loop is cut out and the release time
is measured -> one XM instrument per patch with one sample per octave. PSG = square + a real 16-bit LFSR noise. If the notes lie on the 60/50 Hz
driver frames, one row = one frame (BPM 150, speed 1), otherwise the tempo is searched. Spectrum correlation with the emulator render: 0.91-0.97.

```bash
python3 vgm2xm-midi-wav.py music.vgm                    # XM
python3 vgm2xm-midi-wav.py music.vgz --midi             # XM + MIDI
python3 vgm2xm-midi-wav.py music.vgm --all              # XM + MIDI + WAV
python3 vgm2xm-midi-wav.py music.vgm --loops 2          # play the loop twice
python3 vgm2xm-midi-wav.py music.vgm --compact 2
python3 vgm2xm-midi-wav.py music.vgm --zone-width 6     # more samples per FM patch (better pitch tracking, bigger file)
python3 vgm2xm-midi-wav.py music.vgm --renderer python  # force the Python emulator
python3 vgm2xm-midi-wav.py --build-renderer             # (optional) build the C++ vgm2wav
```
Other options: `--bend-threshold`, `--no-frame-grid`, `--channels`, `--no-pan`, `--debug`. Default `--norm-scale` is 0.7.

Limitations: FM channel 3 "special mode" is treated as a normal channel; LFO vibrato inside a patch is baked into the sample; the sound of FM notes changes with pitch
in the original, here it is approximated by one sample per octave.

## SID (Commodore 64) -> XM / MIDI / WAV
A 6502 emulator runs the tune's INIT/PLAY routines, the writes to `$D400-$D418` are captured every frame and fed to a simplified SID model
(3 oscillators + ADSR + a state-variable filter). Multi-SID (up to 3 chips, `--sids`), 6581/8580 (`--sid-model`), PAL/NTSC (`--clock`).

```bash
python3 sid2xm-midi-wav.py tune.sid --midi --seconds 335
python3 sid2xm-midi-wav.py tune.sid --all --seconds 335
python3 sid2xm-midi-wav.py tune.sid --song 3 --sid-model 8580 --clock ntsc
python3 sid2xm-midi-wav.py tune.sid --mono               # mix multi-SID to mono
python3 sid2xm-midi-wav.py --list-presets                # channel-map presets
```
The song length is not in the file, use `--seconds` (default 60). Channel layout: `--channel-map` (like in the PSG converter), `--raw-notes` (MIDI without envelope processing).

Note: `sid_pyemu.py` and `sid_tracer/` (reSID-based, more exact) are in the repository but are not used by `sid2xm-midi-wav.py` yet.

## PSG (ZX Spectrum / AY dumps) -> XM / MIDI / WAV
Uses the same AY engine as the PT3 converter (see below): the AY is emulated at clock/8 with exact tone/noise/envelope dividers,
XM rows are 50 Hz frames (BPM 250 / speed 2), the row volume follows the real channel level, a pitch change inside a note (arpeggio,
ornament, slide) is a `3FF` portamento. XM instruments are the AY waveforms themselves (square, noise from the real 17-bit LFSR, buzzer = 32-step envelope DAC, tone & noise).
`--bpm/--speed/--compact` do not exist (the grid is the frame).

```bash
python3 psg2xm-midi-wav.py music.psg --midi                       # buzzer on separate channels 3-5 (default)
python3 psg2xm-midi-wav.py music.psg --channel-map compact        # 4 channels
python3 psg2xm-midi-wav.py music.psg --channel-map split-all      # maximum split
python3 psg2xm-midi-wav.py music.psg --channel-map "A:tone=0,A:buzz=6,B:tone=1,C:tone=2"   # custom mapping
python3 psg2xm-midi-wav.py music.psg --all --wav-file output.wav
python3 psg2xm-midi-wav.py --list-presets
```
Other options: `--clock` (1773400 ZX 128 / 1750000 Pentagon), `--ym` (YM2149 DAC table instead of AY-3-8910), `--layout abc|mono` (WAV stereo), `--wav-volume`, `--sample-rate`, `--norm-scale`.

## PT3 (ZX Spectrum Pro Tracker 3) -> XM / MIDI / WAV
The PT3 player produces the same AY registers as `zxtune123` (checked register by register); the WAV matches the zxtune render by loudness, stereo balance and pitch.
A 3-minute track takes ~2 s (numpy). Options are the same as for PSG, plus `--raw-notes` (MIDI notes straight from the player, without envelope processing).

```bash
python3 pt32xm-midi-wav.py music.pt3 --wav                         # WAV only
python3 pt32xm-midi-wav.py music.pt3 --wav --ym --sample-rate 48000 --clock 1750000
python3 pt32xm-midi-wav.py music.pt3 --all                         # XM + MIDI + WAV
python3 pt32xm-midi-wav.py music.pt3 --midi --channel-map split-all --raw-notes
python3 pt32xm-midi-wav.py music.pt3 --wav --wav-file output.wav --wav-volume 0.5
python3 pt32xm-midi-wav.py *.pt3 --wav --midi                      # batch
```

---

# RU

Python-утилиты для конвертации музыки из видеоигр в **XM** (трекерный модуль), **MIDI** и **WAV**.

| Скрипт | Источник | Бэкенд |
|---|---|---|
| `spc2xm-midi-wav.py` | SNES `.spc` | `spc_pyemu.py` (Python) / `spc_tracer/spctrace` (C++ GME, опционально) |
| `usf2xm-midi-wav.py` | N64 `.usf` / `.miniusf` | `usf_pyemu.py` (Python) / `usf_tracer/usftrace` (C++ lazyusf2, опционально) |
| `nsf2xm-midi-wav.py` | NES `.nsf` | `nsf_pyemu.py` (Python) |
| `vgm2xm-midi-wav.py` | Mega Drive `.vgm` / `.vgz` | `vgm_pyemu.py` (Python) / `vgm_render/vgm2wav` (C++ libvgm, опционально) |
| `sid2xm-midi-wav.py` | Commodore 64 `.sid` | встроенный 6502 + модель SID |
| `psg2xm-midi-wav.py` | ZX Spectrum / AY, дампы `.psg` | встроенный эмулятор AY |
| `pt32xm-midi-wav.py` | ZX Spectrum Pro Tracker 3 `.pt3` | встроенный эмулятор AY |

!! Данные инструменты были созданы с помощью и.и. для теста возможностей, но вполне оказались рабочими =)
После конвертации может понадобиться финальная полировка...

## Требования
`python3` + `numpy` (+ `scipy` для WAV 44.1 кГц в SPC-конвертере). C++ бэкенды опциональны и только ускоряют работу:
`--build-tracer` (SPC, USF) / `--build-renderer` (VGM) собирают их (нужны git, cmake/gcc/make; для USF ещё zlib).
`--tracer auto|native|python` (для VGM `--renderer`) выбирает бэкенд: `auto` = C++, если собран, иначе Python.

## Общие опции
Большинство скриптов `*2xm-midi-wav.py` имеют общие опции:

| Опция | Значение |
|---|---|
| `-o NAME` | базовое имя выходных файлов |
| *(нет)* / `--midi` / `--midi-only` | XM / XM + MIDI / только MIDI |
| `--wav` / `--wav-only` / `--wav-file F` | добавить WAV / только WAV / имя WAV-файла |
| `--mmpz` / `--mmpz-embed` | добавить проект LMMS (`.mmpz`); встроить сэмплы в него |
| `--all` | XM + MIDI + WAV + LMMS |
| `--seconds N` | сколько секунд эмулировать (если длина не указана в файле) |
| `--octave N`, `--transpose N` | сдвиг только *имён* нот, звук не меняется |
| `--compact N` | строки в N раз крупнее (меньше паттернов) |
| `--bpm N`, `--speed N` | задать темп вручную |
| `--norm-scale X` | масштаб громкости XM (меньше = громче) |

Полный список: `python3 <скрипт> --help`.

## Проект LMMS (.mmpz)
`--mmpz` добавляет к любому конвертеру файл `NAME.mmpz` (LMMS 1.2+). Он собирается из XM общим модулем `mmpz_export.py` (нужен только numpy, для создания файла LMMS не нужен):

* каждый инструмент XM становится инструментом *AudioFileProcessor*; одновременно звучащие ноты одного инструмента раскладываются по нескольким дорожкам;
* сэмплы пишутся рядом с проектом в `NAME_samples/*.wav` (44,1 кГц, заранее приведены к высоте так, что ключ C4 в LMMS = C-4 в XM, `relative note` и `finetune` учтены) и подключаются по абсолютному пути, поэтому проект переносите вместе с этой папкой;
  `--mmpz-embed` кладёт их внутрь `.mmpz` (один файл), но LMMS 1.2.2 теряет встроенные сэмплы при экспорте через `lmms -r` (в самой программе не проверялось);
* петли сохраняются; длина петли округляется до целых кадров, остаток компенсируется параметром *Pitch* дорожки, поэтому зацикленные «чиповые» волны остаются в строе;
* громкость (колонка громкости, `Cxx`) и панорама (`8xx`) превращаются в автоматизацию дорожки; плавные смены высоты (`3xx`) - в автоматизацию *detuning* внутри одной ноты (без новой атаки);
* темп проекта подбирается так, чтобы тик LMMS был заметно короче строки XM; позиции нот считаются по реальному времени, смены темпа XM (`Fxx`) учтены;
* общая громкость проекта подбирается автоматически, чтобы сумма не клиппила (`mmpz_export.py --gain N` - задать вручную).

**`xm2mmpz.py`** конвертирует любой готовый `.xm` (в том числе не из этих инструментов): `python3 xm2mmpz.py song.xm` -> `song.mmpz`; `--mmp` пишет несжатый `.mmp`, `--embed` встраивает сэмплы, `-d КАТАЛОГ` / `-o ФАЙЛ` задают вывод, можно сразу несколько файлов, `--bpm`/`--gain` заменяют автоматические значения. `python3 tests/mmpz_selftest.py` проверяет высоту, петли, изгибы и громкость на синтетическом модуле (рендерит через `lmms`, если он установлен).
Ограничения: в LMMS 1.2 нет огибающей громкости отдельной ноты, поэтому изменения громкости внутри ноты идут через автоматизацию дорожки; темп не экспортируется автоматизацией.

## Общие писатели
Все конвертеры пишут файлы через три общих модуля (копий в каждом скрипте больше нет):

| Модуль | Назначение |
|---|---|
| `xm_export.py` | запись XM: `Cell`, `XmSample`/`XmInstrument`, `grid_to_patterns`, `write_xm`, `XMWriter` |
| `midi_export.py` | запись Standard MIDI File: `write_smf`, `write_midi`, `vlq`, события темпа/текста, таблицы GM |
| `mmpz_export.py` | проект LMMS из XM (см. выше) |

Самопроверки: `python3 tests/xm_midi_selftest.py` (запись и чтение XM/MIDI), `python3 tests/mmpz_selftest.py` (проект LMMS).
Бесшовные петли: `xm_export.smooth_loop()` вызывается при записи XM любым конвертером (и при чтении XM в `mmpz_export.py`). Если скачок с конца петли на её начало заметно хуже самого резкого места внутри петли (из-за этого длинная нота «трещит» на стыке), подбирается близкая пара начало/конец, у которой 32 кадра перед ними лучше всего совпадают, и волна продолжается плавно; сами данные не меняются, двигаются только точки петли (конец - только к началу). Нормальные петли (чистые периоды, острые края «пилы», однопериодные волны чипов) не затрагиваются. Отключить: переменная окружения `XM_LOOP_FIX=0`.
Название модуля в XM дополняется пробелами (скрипты PSG/PT3/SID раньше дополняли его нулевыми байтами); остальные байты файлов не изменились.

## SPC (SNES) -> XM / MIDI / WAV
`spc_tracer/spctrace` = [Game Music Emu](https://github.com/libgme/game-music-emu) + небольшой патч в S-DSP: пишет каждый старт голоса
(сэмпл, высота, громкость), состояние каждого активного голоса каждые 4 мс (высота, огибающая, громкость L/R) и декодированные блоки BRR.
`spc_pyemu.py` - побитово точный порт того же кода на Python (1-2.5x реального времени; C++ тратит меньше секунды на минуту трека).

* петли берутся из данных BRR (установившийся проход), высота сэмпла - автокорреляцией, применяется гауссов фильтр DSP;
* громкость ноты = огибающая x громкость голоса, панорама из громкости L/R, слайды высоты - глиссандо `3FF`;
* сетка строк определяется по стартам нот, BPM подправляется командами `Fxx`, чтобы не уплывало время (дрейф < 10 мс на 150 с);
* корреляция спектра с рендером эмулятора: 0.90-0.96, совпадение высоты 0.96-0.98.

```bash
python3 spc2xm-midi-wav.py music.spc                    # XM, длина из ID666 или 120 с
python3 spc2xm-midi-wav.py music.spc --midi --seconds 90
python3 spc2xm-midi-wav.py music.spc --all              # XM + MIDI + WAV (рендер GME, 44.1 кГц, нужен scipy; иначе 32 кГц)
python3 spc2xm-midi-wav.py music.spc --compact 2        # более крупные строки
python3 spc2xm-midi-wav.py music.spc --max-row 0.03     # более мелкие строки (больше паттернов)
python3 spc2xm-midi-wav.py music.spc --octave 1 --transpose 2
python3 spc2xm-midi-wav.py music.spc --tracer python    # принудительно Python-эмулятор
python3 spc2xm-midi-wav.py --build-tracer               # (опционально) собрать C++ tracer
```
Прочие опции: `--channels` (макс. каналов XM, 32), `--release-threshold`, `--bend-threshold`, `--limit-knee`, `--samples`, `--no-gauss`, `--no-pan`, `--keep-trace`.

Ограничения: эхо/реверберация не переносится в XM/MIDI (есть в WAV); модуляция высоты между голосами игнорируется; шумовые голоса превращаются в
типовой шумовой сэмпл; XM тише эмулятора примерно на 6 дБ (см. `--norm-scale`).

## USF / miniusf (N64) -> XM / MIDI / WAV
Работает как SPC-конвертер, но лог берётся из эмулятора N64 ([lazyusf2](https://github.com/derselbst/lazyusf2) + патч в `usf_tracer/`):
каждые 5 мс пишутся аудиокоманды RSP каждого голоса (ADPCM / RESAMPLE / ENVMIXER = сэмпл, высота, громкость/панорама) и декодированные сэмплы VADPCM.
Без компилятора используется `usf_pyemu.py` (интерпретатор R4300 + HLE аудио-микрокода RSP; в 2-4 раза медленнее реального времени).

```bash
# .usflib, указанный в теге _lib, должен лежать рядом с .miniusf
python3 usf2xm-midi-wav.py sparse00.miniusf                    # XM
python3 usf2xm-midi-wav.py sparse00.miniusf --all --seconds 90 # XM + MIDI + WAV
python3 usf2xm-midi-wav.py sparse00.miniusf --compact 2
python3 usf2xm-midi-wav.py sparse00.miniusf --bpm 150 --speed 3
python3 usf2xm-midi-wav.py --build-tracer                      # (опционально) собрать C++ tracer
```
Прочие опции: `--seconds` (по умолчанию теги `length`+`fade`, иначе 120), `--channels` (32), `--release-threshold` (0.04), `--bend-threshold` (0.75 полутона, большие изменения становятся глиссандо `3FF`), `--no-pan`, `--keep-trace`.

Ограничения: аудио-микрокоды libultra ABI1, ABI2 («nead»: Zelda, Mario Kart, Star Fox и др.), ABI3 («naudio»: Banjo, DK64, Perfect Dark, Conker и др.) и MusyX v1/v2 (007 TWINE, Rogue Squadron, Indiana Jones и др.). Нативный трассировщик знает ABI1 и MusyX, ABI2/ABI3 - встроенный Python-эмулятор (`usf2xm-midi-wav.py` переключается на него сам, если нативный не нашёл голосов; порты сверены с C-кодом lazyusf2 на случайных alist-программах, но реальной игры с ABI2/ABI3 для проверки всей цепочки не было). MusyX в Python-эмуляторе совпадает с нативным трассировщиком бит-в-бит на 900 реальных задачах 007, но ядро R4300 на Python пока не доводит 007 дальше первых аудио-кадров, поэтому для игр с MusyX нужен нативный трассировщик. Порты ABI2/ABI3 можно перепроверить по C-коду: `usf_tracer/abitest/run.sh`. MP3-команды (ABI3) и ENVMIXER GoldenEye/Blast Corps не эмулируются (после обновления пересоберите нативный трассировщик: `--build-tracer`);
огибающие аппроксимируются ступенями volume-колонки; реверберация сэмплов (aux-шина) не переносится в XM/MIDI (есть в WAV).

## NSF (NES) -> XM / MIDI / WAV
`nsf_pyemu.py` = интерпретатор 6502 (официальные + частые недокументированные опкоды, такты, банки) + настоящая модель 2A03: frame counter,
pulse (огибающая, sweep, длина), треугольник (линейный счётчик), шум (LFSR), DMC и PCM через `$4011`. Чистый Python + numpy. PLAY вызывается
с точной частотой приставки (60.0988 / 50.007 Гц). WAV рендерится из того же лога (проверено на libgme: RMS 0.94-1.05, корреляция огибающих, медиана 0.90).

* нота = повторный запуск или тишина -> звук / смена duty; изменения высоты и громкости внутри ноты берутся из лога, конец = счётчик длины / громкость 0;
* инструменты - сами формы волны: pulse (4 duty) и треугольник (с ограниченным спектром, один сэмпл на октаву), шум = настоящий LFSR, DMC = декодированный поток дельт;
* строки = кадры PLAY (по умолчанию `--compact 2`, чтобы работал `3FF`); громкость XM привязана к WAV, громкие ноты мягко ограничиваются (`--limit-knee`).

```bash
python3 nsf2xm-midi-wav.py game.nsf                     # стартовый трек -> game.xm
python3 nsf2xm-midi-wav.py game.nsf --track 5 --all     # XM + MIDI + WAV трека 5
python3 nsf2xm-midi-wav.py game.nsf --all-tracks --midi # game_track01.xm, game_track01.mid, ...
python3 nsf2xm-midi-wav.py game.nsf --samples 1,3       # только эти инструменты (номера из выведенной таблицы)
python3 nsf2xm-midi-wav.py game.nsf --seconds 60 --compact 4
```
Прочие опции: `--stop-silence` (остановка после N с тишины, 5), `--tail`, `--wav-gain`, `--zone-width`, `--bend-threshold`, `--channels`.

Ограничения: чипы расширения (VRC6, VRC7, FDS, MMC5, N163, Sunsoft 5B) не эмулируются (слышен только 2A03, выводится предупреждение);
плееры на IRQ/NMI не поддерживаются; быстрые арпеджио/тремоло (быстрее строки) сглаживаются сеткой XM.

## VGM (Mega Drive) -> XM / MIDI / WAV
Только YM2612 + SN76489 (другие чипы сообщаются и пропускаются). Тембры берутся из настоящего эмулятора чипов:

* `vgm_pyemu.py` - Python + numpy, повторяет ядра libvgm (результат совпадает с libvgm сэмпл в сэмпл), в 4-8 раз быстрее реального времени;
* `vgm_render/vgm2wav` ([libvgm](https://github.com/ValleyBell/libvgm), опционально, быстрее) используется автоматически, если собран.

VGM разбирается на ноты; каждый уникальный FM-патч рендерится эмулятором на нужных высотах, вырезается бесшовная петля и измеряется время релиза
-> один инструмент XM на патч, один сэмпл на октаву. PSG = меандр + настоящий 16-битный LFSR-шум. Если ноты лежат на кадрах драйвера 60/50 Гц,
одна строка = один кадр (BPM 150, speed 1), иначе темп подбирается. Корреляция спектра с рендером эмулятора: 0.91-0.97.

```bash
python3 vgm2xm-midi-wav.py music.vgm                    # XM
python3 vgm2xm-midi-wav.py music.vgz --midi             # XM + MIDI
python3 vgm2xm-midi-wav.py music.vgm --all              # XM + MIDI + WAV
python3 vgm2xm-midi-wav.py music.vgm --loops 2          # проиграть петлю дважды
python3 vgm2xm-midi-wav.py music.vgm --compact 2
python3 vgm2xm-midi-wav.py music.vgm --zone-width 6     # больше сэмплов на FM-патч (точнее высота, больше файл)
python3 vgm2xm-midi-wav.py music.vgm --renderer python  # принудительно Python-эмулятор
python3 vgm2xm-midi-wav.py --build-renderer             # (опционально) собрать C++ vgm2wav
```
Прочие опции: `--bend-threshold`, `--no-frame-grid`, `--channels`, `--no-pan`, `--debug`. `--norm-scale` по умолчанию 0.7.

Ограничения: «special mode» FM-канала 3 считается обычным каналом; LFO-вибрато внутри патча запекается в сэмпл; звук FM-нот в оригинале меняется с высотой,
здесь это приближено одним сэмплом на октаву.

## SID (Commodore 64) -> XM / MIDI / WAV
Эмулятор 6502 выполняет INIT/PLAY мелодии, записи в `$D400-$D418` фиксируются каждый кадр и подаются в упрощённую модель SID
(3 генератора + ADSR + один state-variable фильтр). Поддерживаются multi-SID (до 3 чипов, `--sids`), 6581/8580 (`--sid-model`), PAL/NTSC (`--clock`).

```bash
python3 sid2xm-midi-wav.py tune.sid --midi --seconds 335
python3 sid2xm-midi-wav.py tune.sid --all --seconds 335
python3 sid2xm-midi-wav.py tune.sid --song 3 --sid-model 8580 --clock ntsc
python3 sid2xm-midi-wav.py tune.sid --mono               # смешать multi-SID в моно
python3 sid2xm-midi-wav.py --list-presets                # пресеты channel-map
```
Длины мелодии в файле нет, используйте `--seconds` (по умолчанию 60). Раскладка каналов: `--channel-map` (как в PSG-конвертере), `--raw-notes` (MIDI без обработки огибающих).

Примечание: `sid_pyemu.py` и `sid_tracer/` (на reSID, точнее) лежат в репозитории, но `sid2xm-midi-wav.py` пока их не использует.

## PSG (ZX Spectrum / дампы AY) -> XM / MIDI / WAV
Использует тот же движок AY, что и PT3-конвертер (см. ниже): AY эмулируется с частотой clock/8 с точными делителями тона, шума и огибающей,
строки XM - кадры 50 Гц (BPM 250 / speed 2), громкость строки следует за реальным уровнем канала, смена высоты внутри ноты (арпеджио,
орнамент, слайд) - портаменто `3FF`. Инструменты XM - сами формы волны AY (меандр, шум из настоящего 17-битного LFSR, buzzer = 32-ступенчатый ЦАП огибающей, тон и шум).
Опций `--bpm/--speed/--compact` нет (сетка = кадр).

```bash
python3 psg2xm-midi-wav.py music.psg --midi                       # buzzer на отдельных каналах 3-5 (по умолчанию)
python3 psg2xm-midi-wav.py music.psg --channel-map compact        # 4 канала
python3 psg2xm-midi-wav.py music.psg --channel-map split-all      # максимальное разделение
python3 psg2xm-midi-wav.py music.psg --channel-map "A:tone=0,A:buzz=6,B:tone=1,C:tone=2"   # свой маппинг
python3 psg2xm-midi-wav.py music.psg --all --wav-file output.wav
python3 psg2xm-midi-wav.py --list-presets
```
Прочие опции: `--clock` (1773400 ZX 128 / 1750000 Pentagon), `--ym` (таблица ЦАП YM2149 вместо AY-3-8910), `--layout abc|mono` (стерео WAV), `--wav-volume`, `--sample-rate`, `--norm-scale`.

## PT3 (ZX Spectrum Pro Tracker 3) -> XM / MIDI / WAV
Плеер PT3 даёт те же регистры AY, что и `zxtune123` (проверено регистр в регистр); WAV совпадает с рендером zxtune по громкости, стерео-балансу и высоте.
Трёхминутный трек считается ~2 с (numpy). Опции те же, что у PSG, плюс `--raw-notes` (ноты MIDI напрямую из плеера, без обработки огибающих).

```bash
python3 pt32xm-midi-wav.py music.pt3 --wav                         # только WAV
python3 pt32xm-midi-wav.py music.pt3 --wav --ym --sample-rate 48000 --clock 1750000
python3 pt32xm-midi-wav.py music.pt3 --all                         # XM + MIDI + WAV
python3 pt32xm-midi-wav.py music.pt3 --midi --channel-map split-all --raw-notes
python3 pt32xm-midi-wav.py music.pt3 --wav --wav-file output.wav --wav-volume 0.5
python3 pt32xm-midi-wav.py *.pt3 --wav --midi                      # пакетно
```

## Audacious plugin: USF (N64)
`audacious_usf/` - input plugin for [Audacious](https://audacious-media-player.org/) 4.x, plays `.usf` / `.miniusf` directly
(lazyusf2 + psflib, the same emulator as `usf2xm-midi-wav.py` uses for WAV; PSF tags, length/fade, seeking).
```bash
cd audacious_usf
./build.sh            # builds usf.so (clones lazyusf2/psflib and, if audacious-dev is missing, the Audacious headers)
./build.sh install    # copies usf.so into the Audacious Input plugin dir (sudo)
```
Needs g++, make, git, zlib and glib dev packages. `.usflib` must lie next to the `.miniusf`.

### More Audacious plugins
| Dir | Plugin | Formats | Engine |
|---|---|---|---|
| `audacious_ay/` | `pt3.so`, `psg.so` | ZX Spectrum `.pt3`, `.psg` | C++ ports of the PT3 player and the AY emulator from `pt32xm-midi-wav.py` / `psg2xm-midi-wav.py` |
| `audacious_sid/` | `sid.so` | C64 `.sid` (subtunes, multi-SID) | installed `libsidplayfp` (reSIDfp), not the Python SID model |

Each dir has `./build.sh` and `./build.sh install` (sudo, copies into the Audacious `Input` plugin dir). Audacious headers are fetched
automatically if `audacious-dev` is missing; SID needs the `libsidplayfp6` package (its headers are fetched too).

### Plugin settings (Settings -> Plugins -> Input -> ... -> gear icon)
* **PT3 / PSG:** per channel A/B/C - output, tone, noise, envelope on/off (live); AY/YM chip, ABC/ACB/mono layout, clock, volume.
  PT3 also: mute samples by number (`3, 5, 10-12`) and disable ornaments (applied at track start).
* **SID:** mute each of the 3 voices of up to 3 SID chips (live); SID/C64 model, filter, digi boost, sampling, volume, default length/fade.
* **USF:** mute "voice slots" 1-32 (the N-th envelope-mixer call per RSP audio task; patched into lazyusf2 via `audacious_usf/voicemute.patch`),
  mono / swap / single channel output, volume, default length/fade. N64 games have no fixed channels, so a slot is whatever voice the game
  engine allocated to that position.

### SPC / NSF / VGM plugins
`audacious_gme/` -> `spc.so`, `nsf.so`, `vgm.so` (Game Music Emu from the installed `libgme0`; headers fetched automatically). Settings per plugin:
voice/channel mute (SPC 8 voices, NSF/VGM 16 slots), tempo, stereo echo depth, treble/bass, channels mode, volume, accurate emulation,
default length/fade. NSF/NSFE subtunes are supported. The stock `console` plugin handles the same formats; disable it if Audacious picks it first.

### Export to XM / MIDI from Audacious
Every plugin adds a playlist context-menu item "Export selected to XM/MIDI (<format>)..." that runs the matching `*2xm-midi-wav.py`
converter of this repo on the selected tracks (subtunes of NSF/SID are exported as `name_N`). Per-plugin settings: write XM / MIDI,
seconds to emulate (0 = the converter default), output folder (empty = next to the source file), converter scripts folder
(defaults to the repo dir at build time) and the Python command (`python3`, needs numpy). Errors are shown in a dialog, success via `notify-send`.

**Built-in converters (no Python):** PT3 and PSG export is implemented in C++ (`audacious_ay/ayexport.hpp`) and produces byte-identical
XM/MIDI to `pt32xm-midi-wav.py` / `psg2xm-midi-wav.py` (checked with `cmp`). It is used by default (`Use the built-in converter`) and has extra
settings: XM channel layout preset, transpose, XM volume scale. NSF export is also built in (`audacious_gme/nsfemu.hpp` = 6502 + APU emulator, `nsfexport.hpp` = converter): on the example files it gives
the same `.mid` and the same `.xm` as `nsf2xm-midi-wav.py` byte for byte (only the hash-based names of PCM instruments differ, Python's `hash()` is random per run).
SID export is built in as well (`audacious_sid/sidexport.hpp`: 6502 + frame register capture + XM/MIDI writers), byte-identical to
`sid2xm-midi-wav.py` on `Devils_ReSIDence_3SID.sid` (XM and MIDI). USF export is built in too (`audacious_usf/usfexport.hpp`; lazyusf2 is built with `usf_tracer/usftrace.patch`): all 10 `examples/sparse*.miniusf`
give byte-identical `.xm` and `.mid` to `usf2xm-midi-wav.py`, in ~3 s instead of ~20 s. VGM export is built in (`audacious_gme/vgmexport.hpp`, byte-identical to `vgm2xm-midi-wav.py` on the example file); FM instruments are rendered with
libvgm: either in-process (`VGM_NATIVE=1 ./build.sh` in `audacious_gme/`, builds libvgm statically) or, by default, with the repo's `vgm_render/vgm2wav` binary (path in the plugin settings).
SPC export is built in as well (`audacious_gme/spcemu.hpp` = SPC700 + S-DSP emulator ported from `spc_pyemu.py`, `spcexport.hpp` = converter, no external tracer needed): the trace and decoded BRR are identical to `spc_tracer/spctrace`, and `.xm`/`.mid` are byte-identical to `spc2xm-midi-wav.py` (60 s of each of the 3 example `.spc`). Emulation takes about 1 s per 100 s of music. All exporters can still fall back to the Python scripts by unchecking `Use the built-in converter`.
