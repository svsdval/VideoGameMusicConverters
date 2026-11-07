// Audacious 4.x input plugin: SNES .spc (Game Music Emu)
#include "export.hpp"
#include "gmeplugin.hpp"
#include "spcexport.hpp"

namespace {
GME_DEFINE_COMMON("spc")
GME_ROW1("spc")

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>DSP voices</b> (applied immediately; uncheck to mute)")),
    WidgetBox({{gme_row1}, true, false}),
    WidgetSeparator(),
    GME_COMMON_WIDGETS("spc"),
    WidgetSeparator(),
    EXPORT_WIDGETS("spc"),
    WidgetCheck(N_("Use the built-in converter (no Python needed)"), WidgetBool("spc", "exp_builtin")),
    WidgetSpin(N_("Row size multiplier (1 = finest grid):"), WidgetInt("spc", "exp_compact"), {1, 8, 1, ""}),
    WidgetSpin(N_("Transpose note names:"), WidgetInt("spc", "exp_transpose"), {-48, 48, 1, N_("semitones")}),
    WidgetSpin(N_("XM volume scale:"), WidgetInt("spc", "exp_volscale"), {10, 300, 5, "%"}),
    WidgetCheck(N_("Low-pass samples like the DSP gaussian interpolation"), WidgetBool("spc", "exp_gauss")),
    WidgetCheck(N_("Write panning (8xx)"), WidgetBool("spc", "exp_pan"))};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, EXPORT_DEFAULTS,
    "exp_builtin", "TRUE", "exp_compact", "1", "exp_transpose", "0", "exp_volscale", "100",
    "exp_gauss", "TRUE", "exp_pan", "TRUE", nullptr};
}

static std::string spc_export(const exporter::Job & j, bool xm, bool midi)
{
    spcexp::Options o;
    o.seconds = aud_get_int("spc", "exp_seconds");     // 0 = длина из ID666, иначе 120
    int cp = aud_get_int("spc", "exp_compact");
    o.compact = cp > 0 ? cp : 1;
    o.transpose = aud_get_int("spc", "exp_transpose");
    int vs = aud_get_int("spc", "exp_volscale");
    o.norm_scale = 100.0 / (vs > 0 ? vs : 100);        // 100% = как в скрипте (norm_scale 1.0)
    o.no_gauss = !aud_get_bool("spc", "exp_gauss");
    o.no_pan = !aud_get_bool("spc", "exp_pan");
    return spcexp::export_spc(j.input, j.out_base, xm, midi, o);
}

EXPORT_DEFINE("spc", "spc2xm-midi-wav.py", false, nullptr, "Export selected to XM/MIDI (SPC)...", spc_export, "spc")

class SPCPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("SPC Decoder (SNES)"), "spc", about, &prefs};
    constexpr SPCPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "spc") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "SPC"); }
    bool init() { aud_config_set_defaults("spc", defaults); export_register(); return true; }
    void cleanup() { export_unregister(); }
};
const char SPCPlugin::about[] = N_("SNES .spc player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const SPCPlugin::exts[] = {"spc", nullptr};
EXPORT SPCPlugin aud_plugin_instance;
