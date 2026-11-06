#!/usr/bin/env bash
# Печатает флаги -I для заголовков Audacious: audacious-dev (pkg-config) либо
# исходники Audacious нужной версии (скачиваются в .audacious_inc/).
set -euo pipefail
if pkg-config --exists audacious 2>/dev/null; then
    pkg-config --cflags audacious; exit 0
fi
HERE="$(cd "$(dirname "$0")" && pwd)"; W="$HERE/.audacious_inc"
if [ ! -f "$W/inc/libaudcore/audio.h" ]; then
    VER="$(audacious --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || echo 4.4)"
    mkdir -p "$W"
    [ -d "$W/aud" ] || git clone -q --depth 1 --branch "audacious-$VER" \
        https://github.com/audacious-media-player/audacious "$W/aud" >&2
    mkdir -p "$W/inc" && rm -rf "$W/inc/libaudcore" && cp -r "$W/aud/src/libaudcore" "$W/inc/libaudcore"
    sed 's/@BIGENDIAN@/0/' "$W/inc/libaudcore/audio.h.in" > "$W/inc/libaudcore/audio.h"
fi
echo "-I$W/inc"
