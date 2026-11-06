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
# Необязательно: libvgm внутри vgm.so (VGM_NATIVE=1 ./build.sh) - иначе экспорт VGM рендерит тембры внешним vgm2wav
VGMINC=""; VGMLIB=""
if [ "${VGM_NATIVE:-0}" = 1 ]; then
    W="$HERE/build"; mkdir -p "$W"
    [ -d "$W/libvgm" ] || git clone -q --depth 1 https://github.com/ValleyBell/libvgm "$W/libvgm"
    mkdir -p "$W/libvgm_out"
    (cd "$W/libvgm_out" && cmake ../libvgm -DCMAKE_BUILD_TYPE=Release -DBUILD_LIBAUDIO=OFF -DBUILD_PLAYER=OFF \
        -DBUILD_VGM2WAV=OFF -DBUILD_TESTS=OFF -DUSE_SANITIZERS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON >/dev/null &&
     make -j"$(nproc)" vgm-player vgm-emu vgm-utils)
    VGMINC="-DHAVE_LIBVGM -I$W/libvgm"
    VGMLIB="$(find "$W/libvgm_out" -name 'libvgm-player.a') $(find "$W/libvgm_out" -name 'libvgm-emu.a') $(find "$W/libvgm_out" -name 'libvgm-utils.a') -liconv"
    ldconfig -p 2>/dev/null | grep -q libiconv || VGMLIB="${VGMLIB% -liconv}"
fi
for p in spc nsf vgm; do
    EXTRA=""; [ "$p" = vgm ] && EXTRA="$VGMINC"
    LIBS="$GMELIB"; [ "$p" = vgm ] && LIBS="$GMELIB $VGMLIB -lz"
    g++ -std=c++17 -I"$HERE/../audacious_common" -DEXPORT_SCRIPT_DIR="\"$(cd "$HERE/.." && pwd)\"" -O2 -fPIC -shared -Wall $EXTRA $AUDINC $GMEINC $(pkg-config --cflags glib-2.0) \
        -o "$HERE/$p.so" "$HERE/$p.cc" $LIBS
    echo "OK: $HERE/$p.so"
done
if [ "${1:-}" = install ]; then
    D="$(pkg-config --variable=plugin_dir audacious 2>/dev/null || true)"
    [ -n "$D" ] || D="$(dirname "$(ls /usr/lib/*/audacious/Input/*.so /usr/lib/audacious/Input/*.so 2>/dev/null | head -1)")"
    echo "Копирую в $D (нужен sudo)"
    sudo install -m 755 "$HERE/spc.so" "$HERE/nsf.so" "$HERE/vgm.so" "$D/"
    echo "Готово. Перезапустите Audacious."
fi
