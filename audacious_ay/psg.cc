// Audacious 4.x input plugin: ZX Spectrum / AY .psg (дамп регистров AY)
// Парсер и эмулятор - порт psg2xm-midi-wav.py.
#include <cstdio>
#include <cstring>
#include <libaudcore/audstrings.h>
#include <libaudcore/tuple.h>
#include "ayplugin.hpp"

namespace {


bool load_psg(const char * filename, std::vector<ay::Regs> & frames, int & fps)
{
    StringBuf path = uri_to_filename(filename);
    if (!path) return false;
    FILE * f = fopen(path, "rb");
    if (!f) return false;
    std::vector<uint8_t> d;
    uint8_t buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) d.insert(d.end(), buf, buf + n);
    fclose(f);

    if (d.size() < 16 || memcmp(d.data(), "PSG\x1A", 4)) return false;
    fps = d[5] ? d[5] : 50;

    ay::Regs regs;
    regs.fill(0);
    bool wrote13 = false, pending = false;
    auto emit = [&](bool w13) {
        ay::Regs fr = regs;
        if (!w13) fr[13] = 0xFF;
        frames.push_back(fr);
    };
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
            if (cmd <= 0x0D)
            {
                regs[cmd] = d[pos];
                wrote13 = wrote13 || cmd == 13;
                pending = true;
            }
            pos++;
        }
    }
    if (pending) emit(wrote13);
    return !frames.empty();
}

AY_DEFINE_COMMON("psg")

const PreferencesWidget widgets[] = {AY_COMMON_WIDGETS("psg")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {AY_COMMON_DEFAULTS, nullptr};

} // namespace

class PSGPlugin : public AYPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("PSG Decoder (AY)"), "psg", about, &prefs};

    constexpr PSGPlugin() : AYPluginBase(info, InputInfo().with_exts(exts), "psg") {}
    bool init() { aud_config_set_defaults("psg", defaults); return true; }

    bool read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *)
    {
        std::vector<ay::Regs> fr;
        int fps;
        if (!load_psg(filename, fr, fps)) return false;
        tuple.set_int(Tuple::Length, (int)(fr.size() * 1000.0 / fps));
        tuple.set_str(Tuple::Codec, "AY register dump (PSG)");
        tuple.set_str(Tuple::Quality, "sequenced");
        return true;
    }

    bool play(const char * filename, VFSFile &)
    {
        std::vector<ay::Regs> fr;
        int fps;
        if (!load_psg(filename, fr, fps)) return false;
        return play_frames(fr, fps);
    }
};

const char PSGPlugin::about[] =
    N_("ZX Spectrum / AY .psg player\nPart of VideoGameMusicConverters.");
const char * const PSGPlugin::exts[] = {"psg", nullptr};

EXPORT PSGPlugin aud_plugin_instance;
