// Audacious 4.x input plugin: NES .nsf / .nsfe (Game Music Emu)
#include "gmeplugin.hpp"

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
    GME_COMMON_WIDGETS("nsf")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {GME_COMMON_DEFAULTS, nullptr};
}

class NSFPlugin : public GMEPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("NSF Decoder (NES)"), "nsf", about, &prefs};
    constexpr NSFPlugin() : GMEPluginBase(info, InputInfo(FlagSubtunes).with_exts(exts), "nsf") {}
    bool is_type(const char * e) const { return !strcasecmp(e, "NSF") || !strcasecmp(e, "NSFE"); }
    bool init() { aud_config_set_defaults("nsf", defaults); return true; }
};
const char NSFPlugin::about[] = N_("NES .nsf / .nsfe player\nEmulation: Game Music Emu.\nPart of VideoGameMusicConverters.");
const char * const NSFPlugin::exts[] = {"nsf", "nsfe", nullptr};
EXPORT NSFPlugin aud_plugin_instance;
