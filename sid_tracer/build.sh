#!/bin/sh
# Собирает sidtrace: reSID + собственный 6510/PSID-плеер -> sid_tracer/sidtrace
set -e
cd "$(dirname "$0")"
HERE=$(pwd)
mkdir -p build
[ -d build/resid ] || git clone --depth 1 https://github.com/daglem/reSID build/resid
cd build/resid/src
sed -e 's/@RESID_INLINING@/1/' -e 's/@RESID_INLINE@/inline/' -e 's/@RESID_BRANCH_HINTS@/1/' \
    -e 's/@RESID_FPGA_CODE@/0/' -e 's/@RESID_CONSTEVAL@//' -e 's/@RESID_CONSTEXPR@/const/' \
    -e 's/@RESID_CONSTINIT@//' -e 's/@HAVE_BUILTIN_EXPECT@/1/' siddefs.h.in > siddefs.h
g++ -O2 -DVERSION=\"1.0\" -I. -o "$HERE/sidtrace" "$HERE/sidtrace.cpp" envelope.cc extfilt.cc filter.cc pot.cc sid.cc voice.cc wave.cc version.cc
echo "OK: $HERE/sidtrace"
