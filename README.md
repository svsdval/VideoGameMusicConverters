# VideoGameMusicConverters
Python utils to convert spc to xm, spc to midi, vgm to xm, vgm to midi, nsf to xm , nsf to midi

EN:
!!These tools were created with the help of A.I. For testing purposes, but they turned out to be fully functional =)
Of course, after conversion, final polishing will be needed, since some samples are in the wrong key; for example, C-4 is used, but they should be in C#4. This can be fixed using OpenMPT.

The converters are pretty good, SPC and NSF have very good converters/generators for samples, but I couldn't get good quality samples in VGM.
```bash
python3 spc2xm-midi2.py --transpose -12 --clean-samples --compact 4 --midi 03\ Abobo.spc
python3 vgm2xm-midi3.py --compact 4 --midi 04\ -\ On\ da\ Ship\'s\ Tail\ (Stage\ 1\).vgm
python3 nsf2xm-midi2.py --track 7 --midi Battletoads\ &\ Double\ Dragon\ -\ The\Ultimate\Team\(1993-06\)\(Rare\)\(Tradewest\).nsf
python3 psg2xm_split_channels.py BlEd.\!m.psg --midi --channel-map split-all
```


# Using SPC to XM / MIDI
```bash
# 1 octave higher (your case)
python spc2xm.py music.spc --octave 1

# 2 octaves higher
python spc2xm.py music.spc --octave 2

# 1 octave lower
python spc2xm.py music.spc --octave -1

# Fine tuning: +3 semitones
python spc2xm.py music.spc --transpose 3

# Combination: +1 octave and +3 semitones = +15 semitones
python spc2xm.py music.spc --octave 1 --transpose 3

# Fine tuning of pitch
python spc2xm.py music.spc --finetune 50

# Manual tempo
python spc2xm.py music.spc --bpm 140 --speed 4

# Pattern compression (2, 4, 8...)
python spc2xm.py music.spc --compact 4

# All together
python spc2xm.py music.spc --octave 1 --bpm 140 --duration 180 --compact 4

```
# Using VGM to XM / MIDI
```bash
# XM only
python vgm2xm.py music.vgm

# XM + MIDI
python vgm2xm.py music.vgm --midi

# MIDI only
python vgm2xm.py music.vgm --midi-only

# VGZ (compressed) VGM)
python vgm2xm.py music.vgz --midi

# Pitch correction
python vgm2xm.py music.vgm --octave 1 --transpose 3

# Pattern compression
python vgm2xm.py music.vgm --compact 4

# Manual tempo
python vgm2xm.py music.vgm --bpm 150 --speed 3

# Batch processing
python vgm2xm.py *.vgm --midi --compact 2
python vgm2xm.py *.vgz --midi-only
```

# Using NSF to XM/MIDI
```bash
# Default track
python nsf2xm.py game.nsf

# Specific track
python nsf2xm.py game.nsf --track 5

# All tracks → individual files
python nsf2xm.py game.nsf --all-tracks
# → game_track01.xm, game_track02.xm, ...

# All tracks with MIDI
python nsf2xm.py game.nsf --all-tracks --midi
# → game_track01.xm, game_track01.mid, game_track02.xm, ...

# MIDI only
python nsf2xm.py game.nsf --midi-only --all-tracks

# With parameters
python nsf2xm.py game.nsf --track 3 --octave 1 --compact 2 --duration 60

# Batch
python nsf2xm.py *.nsf --all-tracks --midi
```

# Using PSG to XM/MIDI
```bash
# Default: Buzzer on separate channels 3-5
python psg2xm.py music.psg --midi

# Compact in 4 channels
python psg2xm.py music.psg --channel-map compact

# Maximum split
python psg2xm.py music.psg --channel-map split-all

# Custom mapping: Channel A buzzer to XM channel 6
python psg2xm.py music.psg --channel-map "A:tone=0,A:buzz=6,B:tone=1,C:tone=2"

# View all presets
python psg2xm.py --list-presets
```


# RU:

!! Данные инструменты были созданы с помощью и.и. для теста возможностей, но вплоне оказались рабочими =)
Конечно после конвертации нужна будет финальная полировка, так как некоторые сэмплы не в той тональности к примеру  ставится C-4 но должны быть в C#4, правится через OpenMPT

Конвертеры довольно неплохие, у SPC и NSF есть очень хорошие конвертеры/генераторы для сэмплов, но мне не удалось получить сэмплы хорошего качества в VGM.

```bash
python3 spc2xm-midi2.py --transpose -12  --clean-samples --compact 4 --midi 03\ Abobo.spc
python3 vgm2xm-midi3.py  --compact 4  --midi 04\ -\ On\ da\ Ship\'s\ Tail\ \(Stage\ 1\).vgm 
python3 nsf2xm-midi2.py --track 7 --midi Battletoads\ \&\ Double\ Dragon\ -\ The\ Ultimate\ Team\ \(1993-06\)\(Rare\)\(Tradewest\).nsf
```

# Использование SPC в XM / MIDI
```bash
# На 1 октаву выше (ваш случай)
python spc2xm.py music.spc --octave 1

# На 2 октавы выше
python spc2xm.py music.spc --octave 2

# На 1 октаву ниже
python spc2xm.py music.spc --octave -1

# Точная подстройка: +3 полутона
python spc2xm.py music.spc --transpose 3

# Комбинация: +1 октава и +3 полутона = +15 полутонов
python spc2xm.py music.spc --octave 1 --transpose 3

# Тонкая подстройка высоты
python spc2xm.py music.spc --finetune 50

# Ручной темп
python spc2xm.py music.spc --bpm 140 --speed 4

# Сжатие паттернов (2, 4, 8...)
python spc2xm.py music.spc --compact 4

# Всё вместе
python spc2xm.py music.spc --octave 1 --bpm 140 --duration 180 --compact 4

```
# Использование VGM в XM / MIDI
```bash
# Только XM
python vgm2xm.py music.vgm

# XM + MIDI
python vgm2xm.py music.vgm --midi

# Только MIDI
python vgm2xm.py music.vgm --midi-only

# VGZ (сжатый VGM)
python vgm2xm.py music.vgz --midi

# Коррекция тональности
python vgm2xm.py music.vgm --octave 1 --transpose 3

# Сжатие паттернов
python vgm2xm.py music.vgm --compact 4

# Ручной темп
python vgm2xm.py music.vgm --bpm 150 --speed 3

# Пакетная обработка
python vgm2xm.py *.vgm --midi --compact 2
python vgm2xm.py *.vgz --midi-only
```

# Использование NSF в XM / MIDI
```bash
# Трек по умолчанию
python nsf2xm.py game.nsf

# Конкретный трек
python nsf2xm.py game.nsf --track 5

# Все треки → отдельные файлы
python nsf2xm.py game.nsf --all-tracks
# → game_track01.xm, game_track02.xm, ...

# Все треки с MIDI
python nsf2xm.py game.nsf --all-tracks --midi
# → game_track01.xm, game_track01.mid, game_track02.xm, ...

# Только MIDI
python nsf2xm.py game.nsf --midi-only --all-tracks

# С параметрами
python nsf2xm.py game.nsf --track 3 --octave 1 --compact 2 --duration 60

# Пакетно
python nsf2xm.py *.nsf --all-tracks --midi
```

# Использование PSG в XM / MID
```bash
# По умолчанию: buzzer на отдельных каналах 3-5
python psg2xm.py music.psg --midi

# Компактно в 4 канала
python psg2xm.py music.psg --channel-map compact

# Максимальное разделение
python psg2xm.py music.psg --channel-map split-all

# Свой маппинг: buzzer канала A на XM канал 6
python psg2xm.py music.psg --channel-map "A:tone=0,A:buzz=6,B:tone=1,C:tone=2"

# Посмотреть все пресеты
python psg2xm.py --list-presets 
```
