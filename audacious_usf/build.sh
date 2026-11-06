#!/usr/bin/env bash
# Собирает Audacious-плагин usf.so (N64 USF/miniusf): lazyusf2 + psflib линкуются внутрь.
# Нужно: g++, make, git, zlib-dev, и либо audacious-dev (pkg-config audacious),
# либо исходники Audacious (скачаются в build/aud автоматически, версия = установленной).
# Установка:  ./build.sh install   ->  <каталог плагинов audacious>/Input/usf.so (sudo)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HERE/build"; mkdir -p "$W"; cd "$W"

[ -d psflib ]   || git clone --depth 1 https://github.com/kode54/psflib psflib
[ -d lazyusf2 ] || git clone --depth 1 https://github.com/derselbst/lazyusf2 lazyusf2

# lazyusf2 (PIC, без трейс-патча, но с патчем мьюта голосов)
rm -f lazyusf2/test/*.o
grep -q usf_voice_mute_mask lazyusf2/rsp_hle/alist.c || (cd lazyusf2 && git apply "$HERE/voicemute.patch")
make -C lazyusf2 -j"$(nproc)" liblazyusf.a OPTFLAGS="-O2 -fPIC"
gcc -O2 -fPIC -c psflib/psflib.c -o psflib.o -I psflib

# заголовки Audacious
AUDINC="$("$HERE/../audacious_inc.sh")"

g++ -std=c++17 -O2 -fPIC -shared -Wall $AUDINC $(pkg-config --cflags glib-2.0) \
    -I psflib -I lazyusf2 -o "$HERE/usf.so" "$HERE/usf.cc" psflib.o lazyusf2/liblazyusf.a -lz
echo "OK: $HERE/usf.so"

if [ "${1:-}" = install ]; then
    # Audacious 4.4 не имеет пользовательского каталога плагинов - ставим в системный
    D="$(pkg-config --variable=plugin_dir audacious 2>/dev/null || true)"
    [ -n "$D" ] || D="$(dirname "$(ls /usr/lib/*/audacious/Input/*.so /usr/lib/audacious/Input/*.so 2>/dev/null | head -1)")"
    echo "Копирую usf.so в $D (нужен sudo)"
    sudo install -m 755 "$HERE/usf.so" "$D/usf.so"
    echo "Готово. Перезапустите Audacious."
fi
