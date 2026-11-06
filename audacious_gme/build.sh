#!/usr/bin/env bash
# Собирает Audacious-плагины spc.so, nsf.so, vgm.so поверх установленной libgme (пакет libgme0).
# Заголовки gme.h берутся из libgme-dev либо скачиваются в build/ (версия 0.6.3).
# ./build.sh [install]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUDINC="$("$HERE/../audacious_inc.sh")"
if pkg-config --exists libgme 2>/dev/null; then
    GMEINC="$(pkg-config --cflags libgme)"; GMELIB="$(pkg-config --libs libgme)"
else
    LIB="$(ls /usr/lib/*/libgme.so.0 /usr/lib/libgme.so.0 2>/dev/null | head -1 || true)"
    [ -n "$LIB" ] || { echo "нет libgme.so.0 (apt install libgme0)"; exit 1; }
    W="$HERE/build"; mkdir -p "$W"
    [ -d "$W/src" ] || git clone -q --depth 1 --branch "${GME_TAG:-0.6.3}" https://github.com/libgme/game-music-emu "$W/src"
    GMEINC="-I$W/src"; GMELIB="$LIB"
fi
for p in spc nsf vgm; do
    g++ -std=c++17 -O2 -fPIC -shared -Wall $AUDINC $GMEINC $(pkg-config --cflags glib-2.0) \
        -o "$HERE/$p.so" "$HERE/$p.cc" $GMELIB
    echo "OK: $HERE/$p.so"
done
if [ "${1:-}" = install ]; then
    D="$(pkg-config --variable=plugin_dir audacious 2>/dev/null || true)"
    [ -n "$D" ] || D="$(dirname "$(ls /usr/lib/*/audacious/Input/*.so /usr/lib/audacious/Input/*.so 2>/dev/null | head -1)")"
    echo "Копирую в $D (нужен sudo)"
    sudo install -m 755 "$HERE/spc.so" "$HERE/nsf.so" "$HERE/vgm.so" "$D/"
    echo "Готово. Перезапустите Audacious."
fi
