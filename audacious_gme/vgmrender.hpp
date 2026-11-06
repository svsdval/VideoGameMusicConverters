// Рендер служебных VGM для vgmexport.hpp.
//  * HAVE_LIBVGM: libvgm внутри плагина (повторяет примерное приложение vgm2wav: 44.1 кГц, 16 бит, 1 проход, без затухания);
//  * иначе: внешняя программа vgm2wav (собирается из libvgm скриптом vgm_render/build.sh, лежит в репозитории).
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>
#include <unistd.h>

#ifdef HAVE_LIBVGM
#include "player/playerbase.hpp"
#include "player/vgmplayer.hpp"
#include "player/playera.hpp"
#include "utils/DataLoader.h"
#include "utils/MemoryLoader.h"

namespace vgmrender {

inline bool render_libvgm(const std::vector<uint8_t> & vgm, std::vector<double> & mono)
{
    constexpr unsigned SRATE = 44100, BUFFER_LEN = 2048;
    PlayerA player;
    player.RegisterPlayerEngine(new VGMPlayer);
    if (player.SetOutputSettings(SRATE, 2, 16, BUFFER_LEN)) return false;
    {
        PlayerA::Config cfg = player.GetConfiguration();
        cfg.masterVol = 0x10000;
        cfg.loopCount = 1;
        cfg.fadeSmpls = 0;
        cfg.endSilenceSmpls = 0;
        cfg.pbSpeed = 1.0;
        player.SetConfiguration(cfg);
    }
    DATA_LOADER * loader = MemoryLoader_Init(vgm.data(), (UINT32)vgm.size());
    if (!loader) return false;
    DataLoader_SetPreloadBytes(loader, 0x100);
    if (DataLoader_Load(loader)) { DataLoader_Deinit(loader); return false; }
    if (player.LoadFile(loader)) { DataLoader_Deinit(loader); return false; }
    PlayerBase * eng = player.GetPlayer();
    if (eng->GetPlayerType() == FCC_VGM)
    {
        VGMPlayer * vp = dynamic_cast<VGMPlayer *>(eng);
        player.SetLoopCount(vp->GetModifiedLoopCount(1));
    }
    player.Start();
    unsigned total = eng->Tick2Sample(eng->GetTotalPlayTicks(1));
    if (eng->GetLoopTicks() > 0) total += player.GetFadeSamples();
    std::vector<int16_t> buf(BUFFER_LEN * 2);
    mono.clear();
    mono.reserve(total);
    while (total)
    {
        std::fill(buf.begin(), buf.end(), 0);
        unsigned cur = BUFFER_LEN > total ? total : BUFFER_LEN;
        player.Render(cur * 4, buf.data());
        for (unsigned i = 0; i < cur; i++) mono.push_back(((double)buf[2 * i] + (double)buf[2 * i + 1]) / 2.0);
        total -= cur;
    }
    player.Stop();
    player.UnloadFile();
    player.UnregisterAllPlayers();
    DataLoader_Deinit(loader);
    return true;
}

} // namespace vgmrender
#endif

namespace vgmrender {

// Запасной вариант: внешний vgm2wav (путь берётся из настроек)
template<class RunFn>
inline bool render_cli(const std::string & exe, const std::vector<uint8_t> & vgm, std::vector<double> & mono, RunFn run)
{
    char src[] = "/tmp/vgmexpXXXXXX";
    int fd = mkstemp(src);
    if (fd < 0) return false;
    std::string s = src, o = s + ".wav";
    bool ok = false;
    {
        FILE * f = fdopen(fd, "wb");
        ok = f && fwrite(vgm.data(), 1, vgm.size(), f) == vgm.size();
        if (f) fclose(f);
    }
    if (ok)
    {
        std::string out;
        ok = run({exe, "--loops=1", "--fade=0.0", "--bps=16", s, o}, out) == 0;
    }
    std::vector<uint8_t> d;
    if (ok)
    {
        FILE * f = fopen(o.c_str(), "rb");
        if (f)
        {
            uint8_t b[65536];
            size_t n;
            while ((n = fread(b, 1, sizeof b, f)) > 0) d.insert(d.end(), b, b + n);
            fclose(f);
        }
        else ok = false;
    }
    unlink(s.c_str());
    unlink(o.c_str());
    if (!ok) return false;
    size_t p = 12;
    int ch = 2;
    std::vector<uint8_t> data;
    while (p + 8 <= d.size())
    {
        uint32_t sz = d[p + 4] | (d[p + 5] << 8) | (d[p + 6] << 16) | ((uint32_t)d[p + 7] << 24);
        if (!memcmp(&d[p], "fmt ", 4)) ch = d[p + 10] | (d[p + 11] << 8);
        else if (!memcmp(&d[p], "data", 4)) { data.assign(d.begin() + p + 8, d.begin() + std::min<size_t>(d.size(), p + 8 + sz)); break; }
        p += 8 + sz + (sz & 1);
    }
    size_t nf = data.size() / (2 * ch);
    mono.resize(nf);
    for (size_t i = 0; i < nf; i++)
    {
        double sum = 0;
        for (int c = 0; c < ch; c++) sum += (int16_t)(data[(i * ch + c) * 2] | (data[(i * ch + c) * 2 + 1] << 8));
        mono[i] = sum / ch;
    }
    return nf > 0;
}

} // namespace vgmrender
