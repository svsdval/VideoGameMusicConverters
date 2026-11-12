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
# досыпание тишины, если игра перестала выдавать звук (иначе usf_render зависает навсегда)
grep -q vi_without_audio usf/usf_internal.h || git apply "$HERE/silence.patch"
# игра ждёт прерывание DP после пропущенной графической задачи, иначе её планировщик встаёт
grep -q MI_INTR_DP rsp_hle/hle.c || git apply "$HERE/gfxdone.patch"
# ENVMIXER_GE (GoldenEye, Blast Corps) тоже пишет строки E, иначе голосов в логе нет
grep -c '"E %u' rsp_hle/alist_audio.c | grep -qx 2 || git apply "$HERE/envmixge.patch"
rm -f test/*.o        # в репозитории лежат устаревшие объектные файлы
make -j"$(nproc)" liblazyusf.a
make usftrace
cp usftrace "$HERE/usftrace"
echo "OK: $HERE/usftrace"
