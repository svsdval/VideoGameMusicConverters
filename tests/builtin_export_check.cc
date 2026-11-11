// Тест-стенд встроенных C++ экспортёров плагинов Audacious (без Audacious): вызывает их напрямую,
// чтобы сравнить .xm/.mid побайтно с Python-скриптами (tests/builtin_vs_python.sh).
//
//   usf <префикс трассы usftrace> <выход> <название>
//   spc <файл> <выход> <секунды>
//   nsf <файл> <номер песни> <выход> <секунды>
//   vgm <файл> <выход> <путь к vgm2wav>
//   sid <файл> <номер песни> <выход> <секунды>
//   psg <файл> <выход> <название>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

#include "../audacious_gme/nsfexport.hpp"
#include "../audacious_gme/spcexport.hpp"
#include "../audacious_gme/vgmexport.hpp"
#include "../audacious_gme/vgmrender.hpp"
#include "../audacious_sid/sidexport.hpp"
#include "../audacious_usf/usfexport.hpp"
#include "../audacious_ay/ayexport.hpp"

static std::string slurp(const std::string & p)
{
    std::ifstream f(p, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

static int run_cmd(const std::vector<std::string> & a, std::string & out)
{
    std::string cmd;
    for (auto & s : a)
    {
        cmd += "'";
        for (char c : s) { if (c == '\'') cmd += "'\\''"; else cmd += c; }
        cmd += "' ";
    }
    cmd += "2>&1";
    FILE * p = popen(cmd.c_str(), "r");
    if (!p) return 127;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, p)) > 0) out.append(buf, n);
    int rc = pclose(p);
    return rc == -1 ? 127 : (rc >> 8) & 0xFF;
}


// загрузчик PSG (повторяет load_psg из audacious_ay/psg.cc без Audacious)
static bool load_psg(const std::string & path, std::vector<ay::Regs> & frames, int & fps)
{
    std::string raw = slurp(path);
    std::vector<uint8_t> d(raw.begin(), raw.end());
    if (d.size() < 16 || memcmp(d.data(), "PSG\x1A", 4)) return false;
    fps = d[5] ? d[5] : 50;
    ay::Regs regs;
    regs.fill(0);
    bool wrote13 = false, pending = false;
    auto emit = [&](bool w13) { ay::Regs fr = regs; if (!w13) fr[13] = 0xFF; frames.push_back(fr); };
    size_t pos = 16, sz = d.size();
    while (pos < sz)
    {
        uint8_t cmd = d[pos++];
        if (cmd == 0xFD) break;
        else if (cmd == 0xFF) { emit(wrote13); wrote13 = pending = false; }
        else if (cmd == 0xFE)
        {
            if (pos >= sz) break;
            for (int k = d[pos] * 4; k > 0; k--) emit(false);
            pos++;
            wrote13 = pending = false;
        }
        else if (pos < sz)
        {
            if (cmd <= 0x0D) { regs[cmd] = d[pos]; wrote13 = wrote13 || cmd == 13; pending = true; }
            pos++;
        }
    }
    if (pending) emit(wrote13);
    return !frames.empty();
}

int main(int argc, char ** argv)
{
    if (argc < 2) { fprintf(stderr, "usage: see header\n"); return 2; }
    std::string mode = argv[1], err;
    if (mode == "usf" && argc >= 5)
    {
        std::string text = slurp(std::string(argv[2]) + ".trace"), raw = slurp(std::string(argv[2]) + ".pcm");
        std::vector<int16_t> pcm(raw.size() / 2);
        memcpy(pcm.data(), raw.data(), pcm.size() * 2);
        usfexp::Options o;
        err = usfexp::convert(usfexp::parse_trace(text), pcm, argv[3], argv[4], true, true, o);
    }
    else if (mode == "spc" && argc >= 5)
    {
        spcexp::Options o;
        o.seconds = atof(argv[4]);
        err = spcexp::export_spc(argv[2], argv[3], true, true, o);
    }
    else if (mode == "nsf" && argc >= 6)
    {
        nsfexp::Options o;
        o.seconds = atoi(argv[5]);
        err = nsfexp::export_nsf(argv[2], atoi(argv[3]), argv[4], true, true, o);
    }
    else if (mode == "vgm" && argc >= 5)
    {
        vgmexp::Options o;
        std::string exe = argv[4];
        vgmexp::Renderer render = [exe](const std::vector<uint8_t> & v, std::vector<double> & m) {
            return vgmrender::render_cli(exe, v, m, run_cmd);
        };
        err = vgmexp::export_vgm(argv[2], argv[3], true, true, o, render);
    }
    else if (mode == "sid" && argc >= 6)
    {
        err = sidexp::export_sid(argv[2], atoi(argv[3]), argv[4], true, true, atoi(argv[5]), 0);
    }
    else if (mode == "psg" && argc >= 5)
    {
        std::vector<ay::Regs> fr;
        int fps;
        if (!load_psg(argv[2], fr, fps)) { fprintf(stderr, "cannot read PSG\n"); return 1; }
        std::vector<std::array<bool, 3>> started;
        int prev[3] = {0, 0, 0};
        for (auto & r : fr)
        {
            std::array<bool, 3> st{};
            for (int ac = 0; ac < 3; ac++)
            {
                int vr = r[8 + ac], level = (vr & 0x10) ? 15 : (vr & 0x0F);
                st[ac] = level - prev[ac] >= 4;
                prev[ac] = level;
            }
            started.push_back(st);
        }
        ayexp::Options o;
        o.clock = 1773400;
        o.fps = fps;
        err = ayexp::export_ay(fr, started, argv[4], argv[3], true, true, o);
    }
    else { fprintf(stderr, "bad arguments\n"); return 2; }
    if (!err.empty()) { fprintf(stderr, "error: %s\n", err.c_str()); return 1; }
    return 0;
}
