#!/usr/bin/env bash
# Сверяет Python-порты ABI2/ABI3 (usf_pyemu.py) с C-кодом lazyusf2 на случайных alist-программах.
# Использование: ./run.sh [N]   (N программ на каждый вариант микрокода, по умолчанию 20)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
N="${1:-20}"
[ -f "$ROOT/usf_tracer/build/lazyusf2/liblazyusf.a" ] || bash "$ROOT/usf_tracer/build.sh"
LZ="$ROOT/usf_tracer/build/lazyusf2"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
gcc -O1 -I"$LZ" -I"$LZ/rsp_hle" -o "$W/abitest" "$HERE/abitest.c" "$LZ/liblazyusf.a" -lm
export ABITEST="$W/abitest" PYEMU_DIR="$ROOT"
cd "$HERE"
python3 difftest.py "$W" naudio,bk,dk,mp3,cbfd,mk,sf,sfj,fz,wrjb,ys,1080,oot,mm,mmb,ac "$N"
