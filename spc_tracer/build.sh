#!/bin/sh
# Собирает spctrace: Game Music Emu + патч с хуками в S-DSP -> spc_tracer/spctrace
set -e
cd "$(dirname "$0")"
HERE=$(pwd)
mkdir -p build
if [ ! -d build/gme ]; then
    git clone https://github.com/libgme/game-music-emu build/gme
    (cd build/gme && git checkout -q "$(cat "$HERE/GME_COMMIT")" 2>/dev/null || true)
fi
cd build/gme
git checkout -q -- gme 2>/dev/null || true
git apply "$HERE/spctrace.patch"
mkdir -p ../out && cd ../out
cmake ../gme -DCMAKE_BUILD_TYPE=Release -DGME_BUILD_STATIC=ON -DGME_BUILD_SHARED=OFF -DGME_BUILD_EXAMPLES=OFF -DGME_BUILD_TESTING=OFF -DGME_ZLIB=OFF >/dev/null
make -j"$(nproc 2>/dev/null || echo 2)" gme_static
LIB=$(find . -name 'libgme.a' | head -1)
g++ -O2 -I ../gme -o "$HERE/spctrace" "$HERE/spctrace.cpp" "$LIB"
echo "OK: $HERE/spctrace"
