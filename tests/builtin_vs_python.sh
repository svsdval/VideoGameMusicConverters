#!/usr/bin/env bash
# Сверка встроенных C++ конвертеров плагинов Audacious с Python-скриптами: .xm и .mid побайтно.
# Собирает тест-стенд tests/builtin_export_check.cc и гоняет оба на файлах из examples/
# (SPC, VGM, NSF, SID, PSG, USF). Для USF нужны usf_tracer/usftrace и .usflib рядом с .miniusf
# (ищется в examples/ и 2/), для VGM - vgm_render/vgm2wav.
# Запуск:  tests/builtin_vs_python.sh        (код возврата 1, если какой-то XM не совпал)
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
X="$HERE/examples"
W="$(mktemp -d /tmp/builtin_vs_python_XXXX)"
export PYTHONHASHSEED=0
g++ -std=c++17 -O2 -I"$HERE/audacious_common" -o "$W/check" "$HERE/tests/builtin_export_check.cc" -lz || exit 2
fail=0
cmp_files() {  # имя ext_a ext_b файлы...
    local name="$1" a="$2" b="$3"
    if [ ! -f "$a" ] || [ ! -f "$b" ]; then echo "  $name: нет файла ($a / $b)"; [ "${a##*.}" = xm ] && fail=1; return; fi
    if cmp -s "$a" "$b"; then echo "  $name: идентичен"
    else echo "  $name: РАЗЛИЧАЕТСЯ ($(cmp -l "$a" "$b" 2>/dev/null | wc -l) байт)"; [ "${a##*.}" = xm ] && fail=1; fi
}
mkdir -p "$W/A" "$W/B"
pick() { find "$X" -maxdepth 1 -name "$1" 2>/dev/null | sort | head -1; }

f="$(pick '03 Abobo.spc')"
if [ -n "$f" ]; then echo "SPC: $(basename "$f")"
    python3 "$HERE/spc2xm-midi-wav.py" "$f" --midi --seconds 60 -o "$W/A/spc" >/dev/null 2>&1
    "$W/check" spc "$f" "$W/B/spc" 60
    cmp_files xm "$W/A/spc.xm" "$W/B/spc.xm"; cmp_files mid "$W/A/spc.mid" "$W/B/spc.mid"; fi
f="$(pick '04*.vgm')"
if [ -n "$f" ] && [ -x "$HERE/vgm_render/vgm2wav" ]; then echo "VGM: $(basename "$f")"
    python3 "$HERE/vgm2xm-midi-wav.py" "$f" --midi -o "$W/A/vgm" >/dev/null 2>&1
    "$W/check" vgm "$f" "$W/B/vgm" "$HERE/vgm_render/vgm2wav"
    cmp_files xm "$W/A/vgm.xm" "$W/B/vgm.xm"; cmp_files mid "$W/A/vgm.mid" "$W/B/vgm.mid"; fi
f="$(pick 'Battletoads*.nsf')"
if [ -n "$f" ]; then echo "NSF: $(basename "$f")"
    python3 "$HERE/nsf2xm-midi-wav.py" "$f" --midi --seconds 40 -o "$W/A/nsf" >/dev/null 2>&1
    "$W/check" nsf "$f" 0 "$W/B/nsf" 40
    cmp_files xm "$W/A/nsf.xm" "$W/B/nsf.xm"; cmp_files mid "$W/A/nsf.mid" "$W/B/nsf.mid"; fi
f="$(pick '*.sid')"
if [ -n "$f" ]; then echo "SID: $(basename "$f")"
    cp "$f" "$W/in.sid"                    # скрипт SID пишет .mid рядом с входным файлом - работаем во временном каталоге
    ( cd "$W" && python3 "$HERE/sid2xm-midi-wav.py" in.sid --midi --seconds 20 >/dev/null 2>&1 )
    "$W/check" sid "$W/in.sid" 0 "$W/B/sid" 20
    cmp_files xm "$W/in.xm" "$W/B/sid.xm"; cmp_files mid "$W/in.mid" "$W/B/sid.mid"; fi
f="$(pick '*.psg')"
if [ -n "$f" ]; then echo "PSG: $(basename "$f")"
    cp "$f" "$W/in.psg"
    ( cd "$W" && python3 "$HERE/psg2xm-midi-wav.py" in.psg --midi >/dev/null 2>&1 )
    "$W/check" psg "$W/in.psg" "$W/B/psg" in
    cmp_files xm "$W/in.xm" "$W/B/psg.xm"; cmp_files mid "$W/in.mid" "$W/B/psg.mid"; fi
f="$(pick 'sparse05.miniusf')"
LN="$(grep -a -o '_lib=[^[:space:]]*' "$f" 2>/dev/null | head -1 | cut -d= -f2)"      # .usflib, указанная в теге _lib
L="$(find "$X" "$HERE/2" -maxdepth 1 -name "$LN" 2>/dev/null | head -1)"
if [ -n "$f" ] && [ -n "$L" ] && [ -x "$HERE/usf_tracer/usftrace" ]; then echo "USF: $(basename "$f")"
    cp "$f" "$L" "$W/"
    ( cd "$W" && python3 "$HERE/usf2xm-midi-wav.py" sparse05.miniusf --midi --seconds 25 --keep-trace -o A/usf >/dev/null 2>&1 )
    T="$(grep -a -o '[^_a-z]title=[^[:cntrl:]]*' "$f" | head -1 | cut -d= -f2-)"           # название как в скрипте: тег title, иначе game
    [ -n "$T" ] || T="$(grep -a -o 'game=[^[:cntrl:]]*' "$f" | head -1 | cut -d= -f2-)"
    "$W/check" usf "$W/A/usf.usftrace" "$W/B/usf" "$T"
    cmp_files xm "$W/A/usf.xm" "$W/B/usf.xm"; cmp_files mid "$W/A/usf.mid" "$W/B/usf.mid"
    echo "  (в MIDI USF бывают различия в значениях pitch bend на 1 единицу - разное округление numpy/C++)"; fi
rm -rf "$W"
[ $fail = 0 ] && echo "ИТОГ: все XM совпали" || echo "ИТОГ: есть расхождения в XM"
exit $fail
