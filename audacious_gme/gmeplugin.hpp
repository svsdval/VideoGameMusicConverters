// Общая часть Audacious-плагинов на базе Game Music Emu (SPC / NSF / VGM):
// воспроизведение, подтреки, общие настройки (голоса, темп, стерео, эквалайзер, громкость, длина).
#pragma once
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include <gme/gme.h>

#include <libaudcore/audstrings.h>
#include <libaudcore/i18n.h>
#include <libaudcore/plugin.h>
#include <libaudcore/preferences.h>
#include <libaudcore/runtime.h>
#include <libaudcore/tuple.h>

#ifndef EXPORT
#define EXPORT __attribute__((visibility("default")))
#endif

// ── виджеты ──
#define GME_V(SEC, n) WidgetCheck(N_(#n), WidgetBool(SEC, "voice" #n))
#define GME_ROW1(SEC) const PreferencesWidget gme_row1[] = { \
    GME_V(SEC, 1), GME_V(SEC, 2), GME_V(SEC, 3), GME_V(SEC, 4), \
    GME_V(SEC, 5), GME_V(SEC, 6), GME_V(SEC, 7), GME_V(SEC, 8)};
#define GME_ROW2(SEC) const PreferencesWidget gme_row2[] = { \
    GME_V(SEC, 9), GME_V(SEC, 10), GME_V(SEC, 11), GME_V(SEC, 12), \
    GME_V(SEC, 13), GME_V(SEC, 14), GME_V(SEC, 15), GME_V(SEC, 16)};

#define GME_DEFINE_COMMON(SEC) \
    const ComboItem gme_out_modes[] = { \
        ComboItem(N_("Stereo"), 0), ComboItem(N_("Mono"), 1), ComboItem(N_("Swap left/right"), 2), \
        ComboItem(N_("Left channel only"), 3), ComboItem(N_("Right channel only"), 4)};

// Всё, кроме голосов (голоса вставляются самим плагином)
#define GME_COMMON_WIDGETS(SEC) \
    WidgetLabel(N_("<b>Playback</b> (applied immediately)")), \
    WidgetSpin(N_("Tempo:"), WidgetInt(SEC, "tempo"), {25, 400, 5, "%"}), \
    WidgetSpin(N_("Stereo echo depth (not SPC / Genesis VGM):"), WidgetInt(SEC, "stereo"), {0, 100, 5, "%"}), \
    WidgetSpin(N_("Treble:"), WidgetInt(SEC, "treble"), {-50, 5, 1, "dB"}), \
    WidgetSpin(N_("Bass (cut-off, lower = more bass):"), WidgetInt(SEC, "bass"), {1, 16000, 10, "Hz"}), \
    WidgetCombo(N_("Channels:"), WidgetInt(SEC, "output"), {{gme_out_modes}}), \
    WidgetSpin(N_("Volume:"), WidgetInt(SEC, "gain"), {10, 300, 5, "%"}), \
    WidgetSeparator(), \
    WidgetLabel(N_("<b>Emulation / length</b> (applied at the next track start)")), \
    WidgetCheck(N_("Accurate emulation (slower)"), WidgetBool(SEC, "accuracy")), \
    WidgetCheck(N_("Ignore length tags, always use the default"), WidgetBool(SEC, "ignore_tags")), \
    WidgetSpin(N_("Default length:"), WidgetInt(SEC, "default_length"), {10, 3600, 10, N_("s")}), \
    WidgetSpin(N_("Fade-out:"), WidgetInt(SEC, "default_fade"), {0, 60, 1, N_("s")})

#define GME_COMMON_DEFAULTS \
    "voice1", "TRUE", "voice2", "TRUE", "voice3", "TRUE", "voice4", "TRUE", \
    "voice5", "TRUE", "voice6", "TRUE", "voice7", "TRUE", "voice8", "TRUE", \
    "voice9", "TRUE", "voice10", "TRUE", "voice11", "TRUE", "voice12", "TRUE", \
    "voice13", "TRUE", "voice14", "TRUE", "voice15", "TRUE", "voice16", "TRUE", \
    "tempo", "100", "stereo", "0", "treble", "0", "bass", "90", "output", "0", "gain", "100", \
    "accuracy", "FALSE", "ignore_tags", "FALSE", "default_length", "150", "default_fade", "5"

class GMEPluginBase : public InputPlugin
{
public:
    constexpr GMEPluginBase(PluginInfo info, InputInfo ii, const char * section)
        : InputPlugin(info, ii), section(section) {}

    // по сигнатуре (на случай конфликта с другими плагинами на те же расширения, напр. "console")
    bool is_our_file(const char *, VFSFile & file)
    {
        char hdr[4] = {0};
        if (file.fread(hdr, 1, 4) != 4) return false;
        const char * t = gme_identify_header(hdr);
        return t && *t && is_type(t);
    }

    bool read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *)
    {
        Spec sp = parse_spec(filename);
        Music_Emu * emu = nullptr;
        if (gme_open_file(sp.path.c_str(), &emu, OUT_RATE)) return false;
        int tracks = gme_track_count(emu);
        int track = 0;
        if (sp.sub <= 0)
        {
            if (tracks > 1)
            {
                Index<short> subs;
                for (int i = 1; i <= tracks; i++) subs.append(i);
                tuple.set_subtunes(subs.len(), subs.begin());
            }
        }
        else { track = std::min(sp.sub, tracks) - 1; tuple.set_int(Tuple::Subtune, sp.sub); }

        gme_info_t * info = nullptr;
        if (!gme_track_info(emu, &info, track) && info)
        {
            if (*info->song) tuple.set_str(Tuple::Title, info->song);
            if (*info->author) tuple.set_str(Tuple::Artist, info->author);
            if (*info->game) tuple.set_str(Tuple::Album, info->game);
            if (*info->copyright) tuple.set_str(Tuple::Copyright, info->copyright);
            if (*info->comment) tuple.set_str(Tuple::Comment, info->comment);
            if (*info->system) tuple.set_str(Tuple::Codec, info->system);
            tuple.set_int(Tuple::Length, length_ms(info) );
            gme_free_info(info);
        }
        else tuple.set_int(Tuple::Length, aud_get_int(section, "default_length") * 1000);
        tuple.set_str(Tuple::Quality, "sequenced");
        gme_delete(emu);
        return true;
    }

    bool play(const char * filename, VFSFile &)
    {
        Spec sp = parse_spec(filename);
        Music_Emu * emu = nullptr;
        if (const char * e = gme_open_file(sp.path.c_str(), &emu, OUT_RATE))
        { fprintf(stderr, "gme open %s: %s\n", sp.path.c_str(), e); return false; }
        int track = sp.sub > 0 ? std::min(sp.sub, gme_track_count(emu)) - 1 : 0;

        gme_enable_accuracy(emu, aud_get_bool(section, "accuracy"));
        if (gme_start_track(emu, track)) { gme_delete(emu); return false; }

        int len_ms = aud_get_int(section, "default_length") * 1000;
        int fade_ms = aud_get_int(section, "default_fade") * 1000;
        gme_info_t * info = nullptr;
        if (!aud_get_bool(section, "ignore_tags") && !gme_track_info(emu, &info, track) && info)
        {
            if (info->length > 0) len_ms = info->length;
            else if (info->intro_length >= 0 && info->loop_length > 0)
                len_ms = info->intro_length + 2 * info->loop_length;
            else if (info->play_length > 0 && info->play_length != 150000) len_ms = info->play_length;
        }
        if (info) gme_free_info(info);

        open_audio(FMT_S16_NE, OUT_RATE, 2);

        constexpr int CHUNK = 1024;
        short buf[CHUNK * 2];
        const int64_t fade_start = (int64_t)len_ms * OUT_RATE / 1000;
        const int64_t total = fade_start + (int64_t)fade_ms * OUT_RATE / 1000;
        int64_t pos = 0;
        bool ok = true;

        while (!check_stop())
        {
            apply_live(emu);
            int seek = check_seek();
            if (seek >= 0)
            {
                int64_t target = (int64_t)seek * OUT_RATE / 1000;
                if (gme_seek(emu, seek)) { ok = false; break; }
                pos = target;
            }
            if (pos >= total || gme_track_ended(emu)) break;
            int n = (int)std::min<int64_t>(CHUNK, total - pos);
            if (const char * e = gme_play(emu, n * 2, buf)) { fprintf(stderr, "gme_play: %s\n", e); ok = false; break; }

            const int gain = aud_get_int(section, "gain");
            const int mode = aud_get_int(section, "output");
            for (int i = 0; i < n; i++)
            {
                int l = buf[2 * i], r = buf[2 * i + 1];
                switch (mode)
                {
                case 1: l = r = (l + r) / 2; break;
                case 2: std::swap(l, r); break;
                case 3: r = l; break;
                case 4: l = r; break;
                }
                int64_t p = pos + i;
                if (p >= fade_start && fade_ms > 0)
                {
                    double g = 1.0 - (double)(p - fade_start) / ((double)fade_ms * OUT_RATE / 1000);
                    g = std::max(0.0, g);
                    l = (int)(l * g); r = (int)(r * g);
                }
                buf[2 * i] = (short)std::clamp(l * gain / 100, -32768, 32767);
                buf[2 * i + 1] = (short)std::clamp(r * gain / 100, -32768, 32767);
            }
            write_audio(buf, n * 4);
            pos += n;
        }
        gme_delete(emu);
        return ok;
    }

protected:
    static constexpr int OUT_RATE = 44100;
    const char * const section;
    virtual bool is_type(const char * ext) const = 0;     // "SPC", "NSF", "VGM" ...

    struct Spec { std::string path; int sub = 0; };

    static Spec parse_spec(const char * filename)
    {
        Spec s;
        StringBuf p = uri_to_filename(filename);
        std::string f = p ? (const char *)p : "";
        size_t q = f.rfind('?');
        if (q != std::string::npos) { s.sub = atoi(f.c_str() + q + 1); f.resize(q); }
        s.path = f;
        return s;
    }

    int length_ms(const gme_info_t * info) const
    {
        int def = aud_get_int(section, "default_length") * 1000;
        if (aud_get_bool(section, "ignore_tags")) return def;
        if (info->length > 0) return info->length + aud_get_int(section, "default_fade") * 1000;
        if (info->intro_length >= 0 && info->loop_length > 0)
            return info->intro_length + 2 * info->loop_length + aud_get_int(section, "default_fade") * 1000;
        return def;
    }

    // настройки, применяемые «на лету»
    void apply_live(Music_Emu * emu) const
    {
        int mask = 0;
        char k[16];
        for (int i = 1; i <= 16; i++)
        {
            snprintf(k, sizeof k, "voice%d", i);
            if (!aud_get_bool(section, k)) mask |= 1 << (i - 1);   // бит = заглушить
        }
        gme_mute_voices(emu, mask);
        gme_set_tempo(emu, aud_get_int(section, "tempo") / 100.0);
        gme_set_stereo_depth(emu, aud_get_int(section, "stereo") / 100.0);
        gme_equalizer_t eq;
        gme_equalizer(emu, &eq);
        eq.treble = aud_get_int(section, "treble");
        eq.bass = aud_get_int(section, "bass");
        gme_set_equalizer(emu, &eq);
    }
};
