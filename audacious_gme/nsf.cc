// Audacious 4.x input plugin: NES .nsf / .nsfe (Game Music Emu)
#include "export.hpp"
#include "gmeplugin.hpp"
#include "nsfexport.hpp"

namespace {
GME_DEFINE_COMMON("nsf")

GME_ROW1("nsf")
GME_ROW2("nsf")

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>Channels</b> (applied immediately; uncheck to mute)")),
    WidgetLabel(N_("1 Square 1, 2 Square 2, 3 Triangle, 4 Noise, 5 DMC; then expansion chips "
                   "(VRC6, VRC7, FDS, MMC5, Namco 163, Sunsoft 5B) in that order")),
    WidgetBox({{gme_row1}, true, false}),
    WidgetBox({{gme_row2}, true, false}),
    WidgetSeparator(),
    GME_COMMON_WIDGETS("nsf"),
    WidgetSeparator(),
    EXPORT_WIDGETS("nsf"),
    WidgetCheck(N_("Use the built-in converter (no Python needed)"), WidgetBool("nsf", "exp_builtin")),
    WidgetSpin(N_("Play frames per XM row:"), WidgetInt("nsf", "exp_compact"), {1, 8, 1, ""}),
    WidgetSpin(N_("Transpose note names:"), WidgetInt("nsf", "exp_transpose"), {-48, 48, 1, N_("semitones")}),
    WidgetSpin(N_("XM volume scale:"), WidgetInt("nsf", "exp_volscale"), {10, 300, 5, "%"})};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, EXPORT_DEFAULTS,
    "exp_builtin", "TRUE", "exp_compact", "2", "exp_transpose", "0", "exp_volscale", "100", nullptr};
}


static std::string nsf_export(const exporter::Job & j, bool xm, bool midi)
{
    nsfexp::Options o;
    int secs = aud_get_int("nsf", "exp_seconds");
    o.seconds = secs > 0 ? secs : 120;
    int cp = aud_get_int("nsf", "exp_compact");
    o.compact = cp > 0 ? cp : 2;
    o.transpose = aud_get_int("nsf", "exp_transpose");
    int vs = aud_get_int("nsf", "exp_volscale");
    // громкость XM: масштаб 100% = как в скрипте (norm_scale 1.0); больше % = громче (меньше норма)
    o.norm_scale = 100.0 / (vs > 0 ? vs : 100);
    return nsfexp::export_nsf(j.input, j.sub, j.out_base, xm, midi, o);
}

EXPORT_DEFINE("nsf", "nsf2xm-midi-wav.py", false, "--track", "Export selected to XM/MIDI (NSF)...", nsf_export, "nsf")

class NSFPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("NSF Decoder (NES)"), "nsf", about, &prefs};
    constexpr NSFPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "nsf") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "NSF") || !strcasecmp(e, "NSFE"); }
    bool init() { aud_config_set_defaults("nsf", defaults); export_register(); return true; }
    void cleanup() { export_unregister(); }
};
const char NSFPlugin::about[] = N_("NES .nsf / .nsfe player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const NSFPlugin::exts[] = {"nsf", "nsfe", nullptr};
EXPORT NSFPlugin aud_plugin_instance;
