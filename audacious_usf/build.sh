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
# патчи lazyusf2: трассировка аудио-микрокода (для экспорта XM/MIDI) и мьют голосов
(cd lazyusf2 &&
 { [ -f rsp_hle/trace.c ] || git apply "$HERE/../usf_tracer/usftrace.patch"; } &&
 { grep -q usf_voice_mute_mask rsp_hle/alist.c || git apply "$HERE/voicemute.patch"; } &&
 { grep -q usf_trace_reset rsp_hle/trace.c || cat >> rsp_hle/trace.c <<'EOT'

#include <string.h>
void usf_trace_reset(void)
{
    memset(voices, 0, sizeof voices);
    memset(covs, 0, sizeof covs);
    usf_adpcm_seen = usf_adpcm_init = 0;
    usf_last_load_cnt = 0;
    usf_cur_table = 0;
}
EOT
 })
make -C lazyusf2 -j"$(nproc)" liblazyusf.a OPTFLAGS="-O2 -fPIC"
gcc -O2 -fPIC -c psflib/psflib.c -o psflib.o -I psflib

# заголовки Audacious
AUDINC="$("$HERE/../audacious_inc.sh")"

g++ -std=c++17 -I"$HERE/../audacious_common" -DEXPORT_SCRIPT_DIR="\"$(cd "$HERE/.." && pwd)\"" -O2 -fPIC -shared -Wall $AUDINC $(pkg-config --cflags glib-2.0) \
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
