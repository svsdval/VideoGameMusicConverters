#!/usr/bin/env bash
# Ручная пересборка бинарных релизов в releases/ (Linux x86_64):
#   audacious-plugins-linux-x86_64.tar.gz  - psg, pt3, nsf, spc, vgm, sid, usf (.so)
#   tracers-linux-x86_64.tar.gz            - usftrace, sidtrace, spctrace, vgm2wav
#   SHA256SUMS
#
# Использование:
#   ./make_releases.sh            упаковать уже собранные файлы (ничего не собирает)
#   ./make_releases.sh --build    сначала пересобрать плагины и usftrace (build.sh в каталогах)
#   ./make_releases.sh --build-all  то же, плюс sidtrace/spctrace/vgm2wav (их скрипты сборки могут не работать)
#
# Ничего не коммитит: после проверки - git add releases && git commit.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
BUILD=0; ALL=0
case "${1:-}" in
    "") ;;
    --build) BUILD=1 ;;
    --build-all) BUILD=1; ALL=1 ;;
    -h|--help) sed -n 2,12p "$0"; exit 0 ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
esac

PLUGINS=(audacious_ay/psg.so audacious_ay/pt3.so audacious_gme/nsf.so audacious_gme/spc.so
         audacious_gme/vgm.so audacious_sid/sid.so audacious_usf/usf.so)
TRACERS=(usf_tracer/usftrace sid_tracer/sidtrace spc_tracer/spctrace vgm_render/vgm2wav)

if [ "$BUILD" = 1 ]; then
    for d in audacious_ay audacious_gme audacious_sid audacious_usf usf_tracer; do
        echo "== сборка $d"; bash "$d/build.sh"
    done
    if [ "$ALL" = 1 ]; then
        for d in sid_tracer spc_tracer vgm_render; do echo "== сборка $d"; bash "$d/build.sh"; done
    fi
fi

for f in "${PLUGINS[@]}" "${TRACERS[@]}"; do
    [ -x "$f" ] || { echo "нет файла $f (запустите с --build)" >&2; exit 1; }
done

REV="$(git rev-parse --short HEAD)"
if [ -n "$(git status --porcelain --untracked-files=no -- '*.cc' '*.hpp' '*.c' '*.patch' '*.sh' '*.py')" ]; then
    REV="$REV+изменения"
    echo "ВНИМАНИЕ: в исходниках есть незакоммиченные изменения, в INSTALL.txt будет пометка '$REV'" >&2
fi

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
P="audacious-plugins-linux-x86_64"; T="tracers-linux-x86_64"
mkdir -p "$W/$P" "$W/$T"
cp "${PLUGINS[@]}" "$W/$P/"
cp "${TRACERS[@]}" "$W/$T/"
cat > "$W/$P/INSTALL.txt" <<TXT
Плагины Audacious 4.x (Linux x86_64), сборка из коммита $REV.
Установка: sudo install -m 755 *.so <каталог плагинов>/Input/   (pkg-config --variable=plugin_dir audacious)
Затем перезапустите Audacious. Экспорт в XM/MIDI/LMMS через Python-скрипты и xm2mmpz.py требует исходников проекта
(каталог скриптов указывается в настройках плагина).
TXT
cat > "$W/$T/README.txt" <<TXT
Трассировщики (Linux x86_64), сборка из коммита $REV:
usftrace, sidtrace, spctrace, vgm2wav - кладутся рядом со скриптами конвертации
(usf_tracer/, sid_tracer/, spc_tracer/, vgm_render/).
TXT

mkdir -p releases
tar -C "$W" -czf "$ROOT/releases/$P.tar.gz" "$P"
tar -C "$W" -czf "$ROOT/releases/$T.tar.gz" "$T"
(cd releases && sha256sum "$P.tar.gz" "$T.tar.gz" | tee SHA256SUMS)
echo "Готово. Проверьте и закоммитьте: git add releases && git commit"
