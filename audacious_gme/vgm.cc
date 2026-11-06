// Audacious 4.x input plugin: Sega / VGM .vgm .vgz (Game Music Emu)
#include "gmeplugin.hpp"

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
    GME_COMMON_WIDGETS("vgm")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, nullptr};
}

class VGMPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("VGM Decoder (Sega)"), "vgm", about, &prefs};
    constexpr VGMPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "vgm") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "VGM") || !strcasecmp(e, "VGZ"); }
    bool init() { aud_config_set_defaults("vgm", defaults); return true; }
};
const char VGMPlugin::about[] = N_("VGM / VGZ player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const VGMPlugin::exts[] = {"vgm", "vgz", nullptr};
EXPORT VGMPlugin aud_plugin_instance;
