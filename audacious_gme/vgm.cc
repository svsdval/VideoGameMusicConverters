// Audacious 4.x input plugin: Sega / VGM .vgm .vgz (Game Music Emu)
#include "export.hpp"
#include "gmeplugin.hpp"
#include "vgmexport.hpp"
#include "vgmrender.hpp"

namespace {
GME_DEFINE_COMMON("vgm")
GME_ROW1("vgm")
GME_ROW2("vgm")

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>Chip voices</b> (applied immediately; uncheck to mute)")),
    WidgetLabel(N_("Order follows the chips used by the file, e.g. YM2612 FM 1-6 (Mega Drive), then PSG tone 1-3 and noise")),
    WidgetBox({{gme_row1}, true, false}),
    WidgetBox({{gme_row2}, true, false}),
    WidgetSeparator(),
    GME_COMMON_WIDGETS("vgm"),
    WidgetSeparator(),
    EXPORT_WIDGETS("vgm"),
    WidgetCheck(N_("Use the built-in converter (no Python needed)"), WidgetBool("vgm", "exp_builtin")),
    WidgetSpin(N_("Make XM rows N times coarser (1 = auto grid):"), WidgetInt("vgm", "exp_compact"), {1, 8, 1, ""}),
    WidgetSpin(N_("Transpose note names:"), WidgetInt("vgm", "exp_transpose"), {-48, 48, 1, N_("semitones")}),
    WidgetSpin(N_("XM volume scale:"), WidgetInt("vgm", "exp_volscale"), {10, 300, 5, "%"}),
    WidgetEntry(N_("vgm2wav (used when libvgm is not built in):"), WidgetString("vgm", "exp_vgm2wav"))};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, EXPORT_DEFAULTS,
    "exp_builtin", "TRUE", "exp_compact", "1", "exp_transpose", "0", "exp_volscale", "100",
    "exp_vgm2wav", EXPORT_SCRIPT_DIR "/vgm_render/vgm2wav", nullptr};
}


static std::string vgm_export(const exporter::Job & j, bool xm, bool midi)
{
    vgmexp::Options o;
    int cp = aud_get_int("vgm", "exp_compact");
    o.compact = cp > 0 ? cp : 1;
    o.transpose = aud_get_int("vgm", "exp_transpose");
    int vs = aud_get_int("vgm", "exp_volscale");
    o.norm_scale = 0.7 * 100.0 / (vs > 0 ? vs : 100);   // 100% = как в скрипте (--norm-scale 0.7)
    vgmexp::Renderer render;
#ifdef HAVE_LIBVGM
    render = vgmrender::render_libvgm;
#else
    std::string exe = (const char *)aud_get_str("vgm", "exp_vgm2wav");
    if (exe.empty()) exe = EXPORT_SCRIPT_DIR "/vgm_render/vgm2wav";
    if (access(exe.c_str(), X_OK)) return "vgm2wav not found: " + exe + " (build it with vgm_render/build.sh or build the plugin with VGM_NATIVE=1)";
    render = [exe](const std::vector<uint8_t> & v, std::vector<double> & m) {
        return vgmrender::render_cli(exe, v, m, [](const std::vector<std::string> & a, std::string & out) { return exporter::run(a, out); });
    };
#endif
    return vgmexp::export_vgm(j.input, j.out_base, xm, midi, o, render);
}

EXPORT_DEFINE("vgm", "vgm2xm-midi-wav.py", false, nullptr, "Export selected to XM/MIDI (VGM)...", vgm_export, "vgm", "vgz")

class VGMPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("VGM Decoder (Sega)"), "vgm", about, &prefs};
    constexpr VGMPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "vgm") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "VGM") || !strcasecmp(e, "VGZ"); }
    bool init() { aud_config_set_defaults("vgm", defaults); export_register(); return true; }
    void cleanup() { export_unregister(); }
};
const char VGMPlugin::about[] = N_("VGM / VGZ player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const VGMPlugin::exts[] = {"vgm", "vgz", nullptr};
EXPORT VGMPlugin aud_plugin_instance;
