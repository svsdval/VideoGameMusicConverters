// Audacious 4.x input plugin: SNES .spc (Game Music Emu)
#include "export.hpp"
#include "gmeplugin.hpp"

namespace {
GME_DEFINE_COMMON("spc")
GME_ROW1("spc")

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>DSP voices</b> (applied immediately; uncheck to mute)")),
    WidgetBox({{gme_row1}, true, false}),
    WidgetSeparator(),
    GME_COMMON_WIDGETS("spc"),
    WidgetSeparator(),
    EXPORT_WIDGETS("spc")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, EXPORT_DEFAULTS, nullptr};
}

EXPORT_DEFINE("spc", "spc2xm-midi-wav.py", false, nullptr, "Export selected to XM/MIDI (SPC)...", nullptr, "spc")

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
