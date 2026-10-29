#!/usr/bin/env bash
# Собирает usftrace: lazyusf2 (N64 USF-эмулятор) + патч, логирующий аудио-команды RSP (ABI1).
# Результат: usf_tracer/usftrace
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HERE/build"
mkdir -p "$W"
cd "$W"
[ -d psflib ]   || git clone --depth 1 https://github.com/kode54/psflib psflib
[ -d lazyusf2 ] || git clone --depth 1 https://github.com/derselbst/lazyusf2 lazyusf2
make -C psflib -j"$(nproc)"
cd lazyusf2
if [ ! -f rsp_hle/trace.c ]; then
    git apply "$HERE/usftrace.patch"
fi
rm -f test/*.o        # в репозитории лежат устаревшие объектные файлы
make -j"$(nproc)" liblazyusf.a
make usftrace
cp usftrace "$HERE/usftrace"
echo "OK: $HERE/usftrace"
