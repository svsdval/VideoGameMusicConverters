// Audacious 4.x input plugin: SNES .spc (Game Music Emu)
#include "gmeplugin.hpp"

namespace {
GME_DEFINE_COMMON("spc")
GME_ROW1("spc")

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>DSP voices</b> (applied immediately; uncheck to mute)")),
    WidgetBox({{gme_row1}, true, false}),
    WidgetSeparator(),
    GME_COMMON_WIDGETS("spc")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, nullptr};
}

class SPCPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("SPC Decoder (SNES)"), "spc", about, &prefs};
    constexpr SPCPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "spc") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "SPC"); }
    bool init() { aud_config_set_defaults("spc", defaults); return true; }
};
const char SPCPlugin::about[] = N_("SNES .spc player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const SPCPlugin::exts[] = {"spc", nullptr};
EXPORT SPCPlugin aud_plugin_instance;
