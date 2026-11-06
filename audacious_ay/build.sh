#!/usr/bin/env bash
# Собирает Audacious-плагины pt3.so и psg.so (ZX Spectrum / AY). Без внешних библиотек.
# ./build.sh [install]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AUDINC="$("$HERE/../audacious_inc.sh")"
for p in pt3 psg; do
    g++ -std=c++17 -O2 -fPIC -shared -Wall $AUDINC $(pkg-config --cflags glib-2.0) \
        -o "$HERE/$p.so" "$HERE/$p.cc"
    echo "OK: $HERE/$p.so"
done
if [ "${1:-}" = install ]; then
    D="$(pkg-config --variable=plugin_dir audacious 2>/dev/null || true)"
    [ -n "$D" ] || D="$(dirname "$(ls /usr/lib/*/audacious/Input/*.so /usr/lib/audacious/Input/*.so 2>/dev/null | head -1)")"
    echo "Копирую в $D (нужен sudo)"
    sudo install -m 755 "$HERE/pt3.so" "$HERE/psg.so" "$D/"
    echo "Готово. Перезапустите Audacious."
fi
