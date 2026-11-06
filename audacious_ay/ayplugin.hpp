// Общая часть Audacious-плагинов для AY-форматов: проигрывание массива кадров регистров
// + общие настройки (каналы, тон/шум/огибающая, чип, раскладка, частота, громкость).
#pragma once
#include <libaudcore/plugin.h>
#include <libaudcore/runtime.h>
#include <libaudcore/preferences.h>
#include <libaudcore/i18n.h>
#include "ay.hpp"
#include "ayexport.hpp"

#ifndef EXPORT
#define EXPORT __attribute__((visibility("default")))
#endif

// Строка канала: [x] Output [x] Tone [x] Noise [x] Envelope
#define AY_ROW(SEC, L, p) \
    const PreferencesWidget ay_row_##p[] = { \
        WidgetLabel(N_(L)), \
        WidgetCheck(N_("Output"), WidgetBool(SEC, #p "_on")), \
        WidgetCheck(N_("Tone"), WidgetBool(SEC, #p "_tone")), \
        WidgetCheck(N_("Noise"), WidgetBool(SEC, #p "_noise")), \
        WidgetCheck(N_("Envelope"), WidgetBool(SEC, #p "_env"))};

#define AY_DEFINE_COMMON(SEC) \
    AY_ROW(SEC, "Channel A", a) AY_ROW(SEC, "Channel B", b) AY_ROW(SEC, "Channel C", c) \
    const ComboItem ay_chips[] = {ComboItem(N_("AY-3-8910"), 0), ComboItem(N_("YM2149"), 1)}; \
    const ComboItem ay_layouts[] = {ComboItem(N_("ABC stereo"), 0), ComboItem(N_("ACB stereo"), 1), \
                                    ComboItem(N_("Mono"), 2)}; \
    const ComboItem ay_clocks[] = {ComboItem(N_("ZX Spectrum 1.7734 MHz"), 1773400), \
                                   ComboItem(N_("Pentagon 1.75 MHz"), 1750000), \
                                   ComboItem(N_("MSX 1.7898 MHz"), 1789772), \
                                   ComboItem(N_("Atari ST 2 MHz"), 2000000), \
                                   ComboItem(N_("Amstrad CPC 1 MHz"), 1000000)};

// Виджеты, общие для pt3 и psg (вставляются в список инициализации)
#define AY_COMMON_WIDGETS(SEC) \
    WidgetLabel(N_("<b>Channels</b> (applied immediately)")), \
    WidgetBox({{ay_row_a}, true, false}), WidgetBox({{ay_row_b}, true, false}), \
    WidgetBox({{ay_row_c}, true, false}), \
    WidgetLabel(N_("Unchecking Tone/Noise removes that component; Output mutes the whole channel.")), \
    WidgetSeparator(), \
    WidgetLabel(N_("<b>Chip</b> (applied immediately)")), \
    WidgetCombo(N_("Chip type:"), WidgetInt(SEC, "chip"), {{ay_chips}}), \
    WidgetCombo(N_("Stereo layout:"), WidgetInt(SEC, "layout"), {{ay_layouts}}), \
    WidgetCombo(N_("Clock:"), WidgetInt(SEC, "clock"), {{ay_clocks}}), \
    WidgetSpin(N_("Volume:"), WidgetInt(SEC, "gain"), {10, 300, 5, "%"})

#define AY_COMMON_DEFAULTS \
    "a_on", "TRUE", "a_tone", "TRUE", "a_noise", "TRUE", "a_env", "TRUE", \
    "b_on", "TRUE", "b_tone", "TRUE", "b_noise", "TRUE", "b_env", "TRUE", \
    "c_on", "TRUE", "c_tone", "TRUE", "c_noise", "TRUE", "c_env", "TRUE", \
    "chip", "0", "layout", "0", "clock", "1773400", "gain", "100"

// Настройки встроенного экспорта (вставляются после EXPORT_WIDGETS)
#define AY_EXPORT_WIDGETS(SEC) \
    WidgetCheck(N_("Use the built-in converter (no Python needed)"), WidgetBool(SEC, "exp_builtin")), \
    WidgetCombo(N_("XM channel layout:"), WidgetInt(SEC, "exp_layout"), {{ay_exp_layouts}}), \
    WidgetSpin(N_("Transpose note names:"), WidgetInt(SEC, "exp_transpose"), {-48, 48, 1, N_("semitones")}), \
    WidgetSpin(N_("XM volume scale:"), WidgetInt(SEC, "exp_volscale"), {10, 300, 5, "%"})
#define AY_EXPORT_DEFINE_COMMON \
    const ComboItem ay_exp_layouts[] = {ComboItem(N_("default (tone / buzzer / noise separate)"), 0), \
        ComboItem(N_("compact"), 1), ComboItem(N_("split-all"), 2), ComboItem(N_("minimal (3 channels)"), 3), \
        ComboItem(N_("buzzer-split"), 4)};
#define AY_EXPORT_DEFAULTS "exp_builtin", "TRUE", "exp_layout", "0", "exp_transpose", "0", "exp_volscale", "100"

// Параметры встроенного экспорта из настроек плагина
inline ayexp::Options ay_export_options(const char * sec, double fps)
{
    ayexp::Options o;
    o.clock = aud_get_int(sec, "clock");
    if (o.clock < 100000) o.clock = 1773400;
    o.ym = aud_get_int(sec, "chip") == 1;
    int vs = aud_get_int(sec, "exp_volscale");
    o.scale = (vs > 0 ? vs : 100) / 100.0;
    o.map_preset = aud_get_int(sec, "exp_layout");
    o.transpose = aud_get_int(sec, "exp_transpose");
    o.fps = fps;
    return o;
}

struct AYSettings
{
    bool on[3], tone[3], noise[3], env[3];
    int chip, layout, clock, gain;

    explicit AYSettings(const char * sec)
    {
        static const char * const ch = "abc";
        for (int i = 0; i < 3; i++)
        {
            char k[16];
            snprintf(k, sizeof k, "%c_on", ch[i]);    on[i] = aud_get_bool(sec, k);
            snprintf(k, sizeof k, "%c_tone", ch[i]);  tone[i] = aud_get_bool(sec, k);
            snprintf(k, sizeof k, "%c_noise", ch[i]); noise[i] = aud_get_bool(sec, k);
            snprintf(k, sizeof k, "%c_env", ch[i]);   env[i] = aud_get_bool(sec, k);
        }
        chip = aud_get_int(sec, "chip");
        layout = aud_get_int(sec, "layout");
        clock = aud_get_int(sec, "clock");
        gain = aud_get_int(sec, "gain");
        if (clock < 100000) clock = 1773400;
    }

    // применяет мьют/отключение составляющих к регистрам кадра
    void apply(ay::Regs & r) const
    {
        int mix = r[7];
        for (int i = 0; i < 3; i++)
        {
            int amp = r[8 + i];
            if (!on[i] || ((amp & 0x10) && !env[i])) { r[8 + i] = 0; continue; }
            bool tdis = (mix >> i) & 1, ndis = (mix >> (i + 3)) & 1;
            if (tdis && ndis) continue;             // "ни тон, ни шум" - ЦАП/сэмплы, не трогаем
            bool ut = !tdis && tone[i], un = !ndis && noise[i];
            if (!ut && !un) { r[8 + i] = 0; continue; }
            if (!ut) mix |= 1 << i;
            if (!un) mix |= 1 << (i + 3);
        }
        r[7] = (uint8_t)mix;
    }
};

class AYPluginBase : public InputPlugin
{
public:
    constexpr AYPluginBase(PluginInfo info, InputInfo ii, const char * section)
        : InputPlugin(info, ii), section(section) {}
    bool is_our_file(const char *, VFSFile &) { return false; }   // только по расширению

protected:
    static constexpr int OUT_RATE = 44100;
    const char * const section;

    bool play_frames(const std::vector<ay::Regs> & frames, double fps)
    {
        if (frames.empty()) return false;
        AYSettings s(section);
        ay::Chip chip(s.clock, OUT_RATE, s.chip == 1, fps, s.layout);
        open_audio(FMT_S16_NE, OUT_RATE, 2);
        std::vector<int16_t> pcm;
        size_t i = 0;
        while (!check_stop())
        {
            AYSettings n(section);
            if (n.clock != s.clock || n.chip != s.chip || n.layout != s.layout)
                chip = ay::Chip(n.clock, OUT_RATE, n.chip == 1, fps, n.layout);
            s = n;
            int seek = check_seek();
            if (seek >= 0)
            {
                i = std::min<size_t>(frames.size(), (size_t)((double)seek * fps / 1000.0));
                chip = ay::Chip(s.clock, OUT_RATE, s.chip == 1, fps, s.layout);
            }
            if (i >= frames.size()) break;
            ay::Regs r = frames[i++];
            s.apply(r);
            pcm.clear();
            chip.render_frame(r, pcm, 0.45 * s.gain / 100.0);
            write_audio(pcm.data(), (int)(pcm.size() * sizeof(int16_t)));
        }
        return true;
    }
};
