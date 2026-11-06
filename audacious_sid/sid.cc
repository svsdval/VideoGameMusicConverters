// Audacious 4.x input plugin: Commodore 64 .sid (PSID/RSID, поддержка нескольких SID и подтреков)
// Эмуляция - libsidplayfp (reSIDfp); sid2xm-midi-wav.py использует собственную упрощённую модель.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>

#include <sidplayfp/sidplayfp.h>
#include <sidplayfp/SidTune.h>
#include <sidplayfp/SidTuneInfo.h>
#include <sidplayfp/SidConfig.h>
#include <sidplayfp/builders/residfp.h>

#include <libaudcore/plugin.h>
#include <libaudcore/audstrings.h>
#include <libaudcore/runtime.h>
#include <libaudcore/tuple.h>
#include <libaudcore/i18n.h>
#include <libaudcore/preferences.h>
#include <algorithm>

#ifndef EXPORT
#define EXPORT __attribute__((visibility("default")))
#endif

namespace {

constexpr int OUT_RATE = 44100;

// ── настройки (Настройки → Плагины → Входные → SID Decoder) ──
#define SV(s, v) WidgetCheck(N_("Voice " #v), WidgetBool("sid", "s" #s "v" #v))
#define SROW(s) const PreferencesWidget sid_row##s[] = { \
    WidgetLabel(N_("SID " #s)), SV(s, 1), SV(s, 2), SV(s, 3)};
SROW(1) SROW(2) SROW(3)
#undef SROW
#undef SV

const ComboItem sid_models[] = {ComboItem(N_("From file (default 6581)"), 0),
                                ComboItem(N_("Force MOS 6581"), 1),
                                ComboItem(N_("Force MOS 8580"), 2)};
const ComboItem c64_models[] = {ComboItem(N_("From file (default PAL)"), 0),
                                ComboItem(N_("Force PAL"), 1), ComboItem(N_("Force NTSC"), 2)};
const ComboItem samplings[] = {ComboItem(N_("Interpolate (fast)"), 0),
                               ComboItem(N_("Resample (accurate)"), 1)};

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>Voices</b> (applied immediately; SID 2/3 exist in multi-SID tunes)")),
    WidgetBox({{sid_row1}, true, false}),
    WidgetBox({{sid_row2}, true, false}),
    WidgetBox({{sid_row3}, true, false}),
    WidgetSeparator(),
    WidgetLabel(N_("<b>Chip / emulation</b> (applied at the next track start)")),
    WidgetCombo(N_("SID model:"), WidgetInt("sid", "sid_model"), {{sid_models}}),
    WidgetCombo(N_("C64 model:"), WidgetInt("sid", "c64_model"), {{c64_models}}),
    WidgetCheck(N_("Emulate SID filter"), WidgetBool("sid", "filter")),
    WidgetCheck(N_("Digi boost (8580 4-bit samples)"), WidgetBool("sid", "digiboost")),
    WidgetCombo(N_("Sampling:"), WidgetInt("sid", "sampling"), {{samplings}}),
    WidgetSeparator(),
    WidgetLabel(N_("<b>Output</b>")),
    WidgetSpin(N_("Volume:"), WidgetInt("sid", "gain"), {10, 300, 5, "%"}),
    WidgetSpin(N_("Default length:"), WidgetInt("sid", "default_length"), {10, 3600, 10, N_("s")}),
    WidgetSpin(N_("Fade-out:"), WidgetInt("sid", "default_fade"), {0, 60, 1, N_("s")}),
    WidgetLabel(N_("A .sid file has no length of its own, so the default length is used."))};
const PluginPreferences prefs = {{widgets}};

const char * const defaults[] = {
    "s1v1", "TRUE", "s1v2", "TRUE", "s1v3", "TRUE",
    "s2v1", "TRUE", "s2v2", "TRUE", "s2v3", "TRUE",
    "s3v1", "TRUE", "s3v2", "TRUE", "s3v3", "TRUE",
    "sid_model", "0", "c64_model", "0", "filter", "TRUE", "digiboost", "FALSE",
    "sampling", "0", "gain", "100", "default_length", "180", "default_fade", "4", nullptr};

void apply_voice_mutes(sidplayfp & engine)
{
    for (int s = 1; s <= 3; s++)
        for (int v = 1; v <= 3; v++)
        {
            char k[8];
            snprintf(k, sizeof k, "s%dv%d", s, v);
            engine.mute(s - 1, v - 1, !aud_get_bool("sid", k));  // в libsidplayfp true = заглушить (вопреки doc)
        }
}

// "file.sid?N" -> путь + номер подтюна
struct Spec { std::string path; int sub = 0; };

Spec parse_spec(const char * filename)
{
    Spec s;
    StringBuf p = uri_to_filename(filename);
    std::string f = p ? (const char *)p : "";
    size_t q = f.rfind('?');
    if (q != std::string::npos)
    {
        s.sub = atoi(f.c_str() + q + 1);
        f.resize(q);
    }
    s.path = f;
    return s;
}

} // namespace

class SIDPlugin : public InputPlugin
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("SID Decoder (C64)"), "sid", about, &prefs};

    constexpr SIDPlugin()
        : InputPlugin(info, InputInfo(FlagSubtunes).with_exts(exts)) {}

    bool is_our_file(const char *, VFSFile &) { return false; }
    bool init() { aud_config_set_defaults("sid", defaults); return true; }

    bool read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *)
    {
        Spec s = parse_spec(filename);
        SidTune tune(s.path.c_str());
        if (!tune.getStatus()) return false;
        const SidTuneInfo * ti = tune.getInfo();
        if (s.sub <= 0)
        {
            // сам файл: сообщаем число подтреков
            if (ti->songs() > 1)
            {
                Index<short> subs;
                for (unsigned i = 1; i <= ti->songs(); i++) subs.append(i);
                tuple.set_subtunes(subs.len(), subs.begin());
            }
            s.sub = ti->startSong();
        }
        else
        {
            tuple.set_int(Tuple::Subtune, s.sub);
        }
        ti = tune.getInfo(s.sub);
        if (!ti) return false;
        if (ti->numberOfInfoStrings() > 0) tuple.set_str(Tuple::Title, ti->infoString(0));
        if (ti->numberOfInfoStrings() > 1) tuple.set_str(Tuple::Artist, ti->infoString(1));
        if (ti->numberOfInfoStrings() > 2) tuple.set_str(Tuple::Copyright, ti->infoString(2));
        tuple.set_int(Tuple::Length, aud_get_int("sid", "default_length") * 1000);
        tuple.set_str(Tuple::Codec, ti->formatString());
        tuple.set_str(Tuple::Quality, "sequenced");
        return true;
    }

    bool play(const char * filename, VFSFile &)
    {
        Spec s = parse_spec(filename);
        SidTune tune(s.path.c_str());
        if (!tune.getStatus()) return false;
        tune.selectSong(s.sub > 0 ? s.sub : 0);

        sidplayfp engine;
        ReSIDfpBuilder builder("audacious");
        SidConfig cfg;
        cfg.frequency = OUT_RATE;
        cfg.playback = SidConfig::STEREO;
        cfg.samplingMethod = aud_get_int("sid", "sampling") ? SidConfig::RESAMPLE_INTERPOLATE
                                                              : SidConfig::INTERPOLATE;
        cfg.fastSampling = false;
        int c64m = aud_get_int("sid", "c64_model");
        cfg.defaultC64Model = c64m == 2 ? SidConfig::NTSC : SidConfig::PAL;
        cfg.forceC64Model = c64m != 0;
        int sidm = aud_get_int("sid", "sid_model");
        cfg.defaultSidModel = sidm == 2 ? SidConfig::MOS8580 : SidConfig::MOS6581;
        cfg.forceSidModel = sidm != 0;
        cfg.digiBoost = aud_get_bool("sid", "digiboost");
        cfg.sidEmulation = &builder;
        builder.create(tune.getInfo()->sidChips());
        if (!builder.getStatus()) return false;
        if (!engine.config(cfg)) return false;
        builder.filter(aud_get_bool("sid", "filter"));
        if (!engine.load(&tune)) return false;
        apply_voice_mutes(engine);

        open_audio(FMT_S16_NE, OUT_RATE, 2);

        constexpr int CHUNK = 1024;
        short buf[CHUNK * 2];
        int64_t pos = 0;                          // в кадрах
        const int FADE = aud_get_int("sid", "default_fade");
        const int64_t fade_start = (int64_t)aud_get_int("sid", "default_length") * OUT_RATE;
        const int64_t total = fade_start + (int64_t)FADE * OUT_RATE;

        while (!check_stop())
        {
            apply_voice_mutes(engine);
            const int gain = aud_get_int("sid", "gain");
            int seek = check_seek();
            if (seek >= 0)
            {
                int64_t target = (int64_t)seek * OUT_RATE / 1000;
                if (target < pos)
                {
                    engine.stop();
                    if (!engine.load(&tune)) return false;
                    apply_voice_mutes(engine);
                    pos = 0;
                }
                engine.fastForward(3200);          // быстрая перемотка вперёд
                while (pos < target && !check_stop())
                {
                    int n = (int)std::min<int64_t>(CHUNK, target - pos);
                    if ((int)engine.play(buf, n * 2) < 0) break;
                    pos += n;
                }
                engine.fastForward(100);
            }
            if (pos >= total) break;
            int n = (int)std::min<int64_t>(CHUNK, total - pos);
            int got = (int)engine.play(buf, n * 2);   // count = число short
            if (got <= 0) return false;
            int frames = got / 2;
            if (pos + frames > fade_start)
                for (int i = 0; i < frames; i++)
                {
                    int64_t p = pos + i;
                    if (p < fade_start) continue;
                    double g = 1.0 - (double)(p - fade_start) / ((double)FADE * OUT_RATE);
                    if (g < 0) g = 0;
                    buf[2 * i] = (short)(buf[2 * i] * g);
                    buf[2 * i + 1] = (short)(buf[2 * i + 1] * g);
                }
            if (gain != 100)
                for (int i = 0; i < frames * 2; i++)
                    buf[i] = (short)std::clamp(buf[i] * gain / 100, -32768, 32767);
            write_audio(buf, frames * 4);
            pos += frames;
        }
        engine.stop();
        return true;
    }
};

const char SIDPlugin::about[] =
    N_("Commodore 64 SID player\nEmulation: libsidplayfp (reSIDfp).\nPart of VideoGameMusicConverters.");
const char * const SIDPlugin::exts[] = {"sid", "psid", "rsid", nullptr};

EXPORT SIDPlugin aud_plugin_instance;
