#!/usr/bin/env bash
# Собирает Audacious-плагин sid.so (C64) поверх установленной libsidplayfp (пакет libsidplayfp6).
# Заголовки libsidplayfp (та же версия, что у библиотеки) скачиваются в build/, если нет libsidplayfp-dev.
# ./build.sh [install]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUDINC="$("$HERE/../audacious_inc.sh")"
if pkg-config --exists libsidplayfp 2>/dev/null; then
    SIDINC="$(pkg-config --cflags libsidplayfp)"; SIDLIB="$(pkg-config --libs libsidplayfp)"
else
    W="$HERE/build"; mkdir -p "$W"
    LIB="$(ls /usr/lib/*/libsidplayfp.so.6 /usr/lib/libsidplayfp.so.6 2>/dev/null | head -1 || true)"
    [ -n "$LIB" ] || { echo "нет libsidplayfp.so.6 (apt install libsidplayfp6)"; exit 1; }
    TAG="${SIDFP_TAG:-v$(dpkg-query -W -f='${Version}' libsidplayfp6 2>/dev/null | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' || echo 2.9.0)}"
    [ -d "$W/src" ] || git clone -q --depth 1 --branch "$TAG" https://github.com/libsidplayfp/libsidplayfp "$W/src"
    rm -rf "$W/inc"; mkdir -p "$W/inc/sidplayfp/builders"
    cp "$W"/src/src/sidplayfp/*.h "$W/inc/sidplayfp/"
    cp "$W"/src/src/builders/residfp-builder/residfp.h "$W/inc/sidplayfp/builders/"
    IFS=. read -r MA MI LV <<< "${TAG#v}"
    sed "s/@LIB_MAJOR@/$MA/;s/@LIB_MINOR@/$MI/;s/@LIB_LEVEL@/$LV/" \
        "$W/src/src/sidplayfp/sidversion.h.in" > "$W/inc/sidplayfp/sidversion.h"
    SIDINC="-I$W/inc"; SIDLIB="$LIB"
fi
g++ -std=c++17 -O2 -fPIC -shared -Wall $AUDINC $SIDINC $(pkg-config --cflags glib-2.0) \
    -o "$HERE/sid.so" "$HERE/sid.cc" $SIDLIB
echo "OK: $HERE/sid.so"
if [ "${1:-}" = install ]; then
    D="$(pkg-config --variable=plugin_dir audacious 2>/dev/null || true)"
    [ -n "$D" ] || D="$(dirname "$(ls /usr/lib/*/audacious/Input/*.so /usr/lib/audacious/Input/*.so 2>/dev/null | head -1)")"
    echo "Копирую в $D (нужен sudo)"
    sudo install -m 755 "$HERE/sid.so" "$D/"
    echo "Готово. Перезапустите Audacious."
fi
