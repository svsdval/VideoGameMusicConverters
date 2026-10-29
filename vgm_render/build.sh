#!/bin/sh
# Собирает vgm2wav (из libvgm) -> vgm_render/vgm2wav
# Источник: ../libvgm, если он есть, иначе клонируется с GitHub.
set -e
cd "$(dirname "$0")"
HERE=$(pwd)
if [ -f ../libvgm/CMakeLists.txt ]; then
    SRC=$(cd ../libvgm && pwd)
else
    mkdir -p build
    [ -d build/libvgm ] || git clone --depth 1 https://github.com/ValleyBell/libvgm build/libvgm
    SRC=$HERE/build/libvgm
fi
mkdir -p build/out
cd build/out
cmake "$SRC" -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON >/dev/null
make -j"$(nproc 2>/dev/null || echo 2)" vgm2wav
cp bin/vgm2wav "$HERE/vgm2wav"
echo "OK: $HERE/vgm2wav"
