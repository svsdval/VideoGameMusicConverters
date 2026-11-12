// Audacious 4.x input plugin: Nintendo 64 .usf / .miniusf
// Рендер через lazyusf2 (тот же эмулятор, что использует usf2xm-midi-wav.py для WAV).
#include "export.hpp"
#include "usfexport.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <string>
#include <strings.h>

#include <libaudcore/plugin.h>
#include <libaudcore/audstrings.h>
#include <libaudcore/runtime.h>
#include <libaudcore/i18n.h>

#include <libaudcore/preferences.h>

extern "C" {
void usf_trace_reset(void);
extern FILE * usf_trace_f, * usf_trace_pcm;
extern uint32_t usf_voice_mute_mask;    // патч voicemute.patch в lazyusf2 (rsp_hle/alist.c)
#include "psflib.h"
#include "usf/usf.h"
}

#ifndef EXPORT
#define EXPORT __attribute__((visibility("default")))
#endif

namespace {

constexpr int OUT_RATE = 44100;

// ── настройки (Настройки → Плагины → Входные → USF Decoder) ──
#define V(n) WidgetCheck(N_(#n), WidgetBool("usf", "voice" #n))
const PreferencesWidget voice_row1[] = {V(1), V(2), V(3), V(4), V(5), V(6), V(7), V(8)};
const PreferencesWidget voice_row2[] = {V(9), V(10), V(11), V(12), V(13), V(14), V(15), V(16)};
const PreferencesWidget voice_row3[] = {V(17), V(18), V(19), V(20), V(21), V(22), V(23), V(24)};
const PreferencesWidget voice_row4[] = {V(25), V(26), V(27), V(28), V(29), V(30), V(31), V(32)};
#undef V

const ComboItem out_modes[] = {
    ComboItem(N_("Stereo"), 0), ComboItem(N_("Mono"), 1),
    ComboItem(N_("Swap left/right"), 2), ComboItem(N_("Left channel only"), 3),
    ComboItem(N_("Right channel only"), 4)};

const PreferencesWidget widgets[] = {
    WidgetLabel(N_("<b>Voice slots</b> (envelope-mixer calls per audio task; "
                   "uncheck to mute a slot)")),
    WidgetBox({{voice_row1}, true, false}),
    WidgetBox({{voice_row2}, true, false}),
    WidgetBox({{voice_row3}, true, false}),
    WidgetBox({{voice_row4}, true, false}),
    WidgetSeparator(),
    WidgetLabel(N_("<b>Output</b>")),
    WidgetCombo(N_("Channels:"), WidgetInt("usf", "output"), {{out_modes}}),
    WidgetSpin(N_("Volume:"), WidgetInt("usf", "gain"), {10, 300, 5, "%"}),
    WidgetSeparator(),
    WidgetLabel(N_("<b>Length</b>")),
    WidgetCheck(N_("Ignore length/fade tags, always use the default"),
                WidgetBool("usf", "ignore_tags")),
    WidgetSpin(N_("Default length:"), WidgetInt("usf", "default_length"), {10, 3600, 10, N_("s")}),
    WidgetSpin(N_("Default fade-out:"), WidgetInt("usf", "default_fade"), {0, 60, 1, N_("s")}),
    WidgetLabel(N_("Length/fade changes apply to the next track start.")),
    WidgetSeparator(),
    EXPORT_WIDGETS("usf"),
    WidgetCheck(N_("Use the built-in converter (no Python needed)"), WidgetBool("usf", "exp_builtin")),
    WidgetSpin(N_("Make XM rows N times coarser (1 = auto grid):"), WidgetInt("usf", "exp_compact"), {1, 8, 1, ""}),
    WidgetSpin(N_("Transpose note names:"), WidgetInt("usf", "exp_transpose"), {-48, 48, 1, N_("semitones")}),
    WidgetLabel(N_("Do not play USF tracks while an export is running."))};

const PluginPreferences prefs = {{widgets}};

const char * const defaults[] = {
    "voice1", "TRUE", "voice2", "TRUE", "voice3", "TRUE", "voice4", "TRUE",
    "voice5", "TRUE", "voice6", "TRUE", "voice7", "TRUE", "voice8", "TRUE",
    "voice9", "TRUE", "voice10", "TRUE", "voice11", "TRUE", "voice12", "TRUE",
    "voice13", "TRUE", "voice14", "TRUE", "voice15", "TRUE", "voice16", "TRUE",
    "voice17", "TRUE", "voice18", "TRUE", "voice19", "TRUE", "voice20", "TRUE",
    "voice21", "TRUE", "voice22", "TRUE", "voice23", "TRUE", "voice24", "TRUE",
    "voice25", "TRUE", "voice26", "TRUE", "voice27", "TRUE", "voice28", "TRUE",
    "voice29", "TRUE", "voice30", "TRUE", "voice31", "TRUE", "voice32", "TRUE",
    "output", "0", "gain", "100", "ignore_tags", "FALSE",
    "default_length", "180", "default_fade", "5", EXPORT_DEFAULTS,
    "exp_builtin", "TRUE", "exp_compact", "1", "exp_transpose", "0", nullptr};

uint32_t read_mute_mask()
{
    uint32_t m = 0;
    char name[16];
    for (int i = 1; i <= 32; i++)
    {
        snprintf(name, sizeof name, "voice%d", i);
        if (!aud_get_bool("usf", name)) m |= 1u << (i - 1);
    }
    return m;
}


// ── файловые колбэки psflib (stdio) ──
void * f_open(void *, const char * p) { return fopen(p, "rb"); }
size_t f_read(void * b, size_t s, size_t n, void * f) { return fread(b, s, n, (FILE *)f); }
int f_seek(void * f, int64_t o, int w) { return fseek((FILE *)f, o, w); }
int f_close(void * f) { return fclose((FILE *)f); }
long f_tell(void * f) { return ftell((FILE *)f); }
const psf_file_callbacks cbs = {"\\/:", nullptr, f_open, f_read, f_seek, f_close, f_tell};

struct Loaded
{
    void * state = nullptr;
    bool compare = false, fifo_full = false;
    std::string title, artist, game, genre, year, comment, copyright;
    double length = 0, fade = 0;
    bool have_length = false;
};

double parse_time(const char * s)
{
    double sec = 0;
    char * end;
    std::string t(s);
    for (auto & c : t) if (c == ',') c = '.';
    const char * p = t.c_str();
    while (true)
    {
        sec = sec * 60 + strtod(p, &end);
        if (*end != ':') break;
        p = end + 1;
    }
    return sec;
}

int loader(void * ctx, const uint8_t * exe, size_t exe_size, const uint8_t * res, size_t res_size)
{
    auto * L = (Loaded *)ctx;
    if (exe && exe_size > 0) return -1;
    return usf_upload_section(L->state, res, res_size);
}

int info_cb(void * ctx, const char * n, const char * v)
{
    auto * L = (Loaded *)ctx;
    if (!strcasecmp(n, "_enablecompare") && *v) L->compare = true;
    else if (!strcasecmp(n, "_enablefifofull") && *v) L->fifo_full = true;
    else if (!strcasecmp(n, "title")) L->title = v;
    else if (!strcasecmp(n, "artist")) L->artist = v;
    else if (!strcasecmp(n, "game")) L->game = v;
    else if (!strcasecmp(n, "genre")) L->genre = v;
    else if (!strcasecmp(n, "year")) L->year = v;
    else if (!strcasecmp(n, "comment")) L->comment = v;
    else if (!strcasecmp(n, "length")) { L->length = parse_time(v); L->have_length = true; }
    else if (!strcasecmp(n, "fade")) L->fade = parse_time(v);
    return 0;
}

void msg_cb(void *, const char *) {}

// need_state=false → только теги
bool load_usf(const char * filename, Loaded & L, bool need_state)
{
    StringBuf path = uri_to_filename(filename);
    if (!path) return false;
    if (need_state)
    {
        L.state = malloc(usf_get_state_size());
        usf_clear(L.state);
    }
    // nested_tags=1 и при загрузке состояния: флаги _enablefifofull/_enablecompare лежат в .usflib,
    // без них некоторые игры (Blast Corps) зависают в эмуляции и Audacious вечно «буферизует»
    int ver = psf_load(path, &cbs, 0x21, need_state ? loader : nullptr, &L, info_cb, &L, 1, msg_cb, nullptr);
    if (ver <= 0)
    {
        if (L.state) { free(L.state); L.state = nullptr; }
        return false;
    }
    return true;
}

void apply_output(int16_t * buf, int n)
{
    int mode = aud_get_int("usf", "output");
    int gain = aud_get_int("usf", "gain");
    if (mode == 0 && gain == 100) return;
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
        l = l * gain / 100;
        r = r * gain / 100;
        buf[2 * i] = (int16_t)std::clamp(l, -32768, 32767);
        buf[2 * i + 1] = (int16_t)std::clamp(r, -32768, 32767);
    }
}

} // namespace


// Эмуляция с трассировкой аудио-микрокода -> встроенный конвертер в XM/MIDI.
// Трассировка и маска голосов глобальны в lazyusf2: на время экспорта не проигрывайте USF в Audacious.
static std::string usf_export(const exporter::Job & j, bool xm, bool midi)
{
    StringBuf uri = filename_to_uri(j.input.c_str());
    Loaded T;
    load_usf(uri, T, false);                      // только теги
    double secs = aud_get_int("usf", "exp_seconds");
    if (secs <= 0) secs = T.have_length ? T.length + T.fade : 120.0;
    std::string title = !T.title.empty() ? T.title : !T.game.empty() ? T.game
                                                    : j.out_base.substr(j.out_base.rfind('/') + 1);

    Loaded L;
    if (!load_usf(uri, L, true)) return "cannot load the file (is the .usflib next to it?)";
    void * st = L.state;
    usf_set_compare(st, L.compare);
    usf_set_fifo_full(st, L.fifo_full);
    usf_set_hle_audio(st, 1);

    FILE * tf = tmpfile(), * pf = tmpfile();
    if (!tf || !pf) { if (tf) fclose(tf); if (pf) fclose(pf); usf_shutdown(st); free(st); return "cannot create temp files"; }
    usf_voice_mute_mask = 0;
    usf_trace_reset();
    usf_trace_f = tf;
    usf_trace_pcm = pf;
    int32_t rate = 0;
    const char * err = usf_render(st, nullptr, 0, &rate);
    if (!err)
    {
        fprintf(tf, "# rate %d\n", (int)rate);
        int16_t buf[2048];
        long total = (long)(secs * 44100);
        while (total > 0 && !err)
        {
            int n = total > 1024 ? 1024 : (int)total;
            err = usf_render_resampled(st, buf, n, 44100);
            total -= n;
        }
    }
    usf_trace_f = usf_trace_pcm = nullptr;
    usf_shutdown(st);
    free(st);
    if (err) { fclose(tf); fclose(pf); return std::string("emulator error: ") + err; }

    auto slurp = [](FILE * f) {
        std::string out;
        fflush(f);
        rewind(f);
        char b[65536];
        size_t k;
        while ((k = fread(b, 1, sizeof b, f)) > 0) out.append(b, k);
        return out;
    };
    std::string text = slurp(tf);
    std::string raw = slurp(pf);
    fclose(tf);
    fclose(pf);
    std::vector<int16_t> pcm(raw.size() / 2);
    memcpy(pcm.data(), raw.data(), pcm.size() * 2);

    usfexp::Options o;
    int cp = aud_get_int("usf", "exp_compact");
    o.compact = cp > 0 ? cp : 1;
    o.transpose = aud_get_int("usf", "exp_transpose");
    return usfexp::convert(usfexp::parse_trace(text), pcm, j.out_base, title, xm, midi, o);
}

EXPORT_DEFINE("usf", "usf2xm-midi-wav.py", false, nullptr, "Export selected to XM/MIDI (USF)...", usf_export, "usf", "miniusf")

class USFPlugin : public InputPlugin
{
public:
    static const char about[];
    static const char * const exts[];

    static constexpr PluginInfo info = {N_("USF Decoder (N64)"), "usf", about, &prefs};

    constexpr USFPlugin() : InputPlugin(info, InputInfo().with_exts(exts)) {}

    bool is_our_file(const char *, VFSFile &) { return false; }   // только по расширению
    bool init() { aud_config_set_defaults("usf", defaults); export_register(); return true; }
    void cleanup() { export_unregister(); }
    bool read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *);
    bool play(const char * filename, VFSFile &);
};

const char USFPlugin::about[] =
    N_("Nintendo 64 USF / miniusf player\n"
       "Emulator: lazyusf2 (kode54 / derselbst)\n"
       "Part of VideoGameMusicConverters.");

const char * const USFPlugin::exts[] = {"usf", "miniusf", nullptr};

EXPORT USFPlugin aud_plugin_instance;

bool USFPlugin::read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *)
{
    Loaded L;
    if (!load_usf(filename, L, false)) return false;

    if (!L.title.empty()) tuple.set_str(Tuple::Title, L.title.c_str());
    if (!L.artist.empty()) tuple.set_str(Tuple::Artist, L.artist.c_str());
    if (!L.game.empty()) tuple.set_str(Tuple::Album, L.game.c_str());
    if (!L.genre.empty()) tuple.set_str(Tuple::Genre, L.genre.c_str());
    if (!L.comment.empty()) tuple.set_str(Tuple::Comment, L.comment.c_str());
    if (!L.year.empty()) tuple.set_int(Tuple::Year, atoi(L.year.c_str()));
    double total = (L.have_length && !aud_get_bool("usf", "ignore_tags"))
                       ? L.length + L.fade
                       : aud_get_int("usf", "default_length");
    tuple.set_int(Tuple::Length, (int)(total * 1000));
    tuple.set_str(Tuple::Codec, "Nintendo 64 USF");
    tuple.set_str(Tuple::Quality, "sequenced");
    return true;
}

bool USFPlugin::play(const char * filename, VFSFile &)
{
    Loaded L;
    if (!load_usf(filename, L, true)) return false;

    void * st = L.state;
    usf_set_compare(st, L.compare);
    usf_set_fifo_full(st, L.fifo_full);
    usf_set_hle_audio(st, 1);

    int32_t native_rate = 0;
    if (usf_render(st, nullptr, 0, &native_rate)) { usf_shutdown(st); free(st); return false; }

    bool use_tags = L.have_length && !aud_get_bool("usf", "ignore_tags");
    double len = use_tags ? L.length : aud_get_int("usf", "default_length");
    double fade = use_tags ? L.fade : aud_get_int("usf", "default_fade");
    int64_t total = (int64_t)((len + fade) * OUT_RATE);
    int64_t fade_start = (int64_t)(len * OUT_RATE);

    open_audio(FMT_S16_NE, OUT_RATE, 2);

    constexpr int CHUNK = 1024;
    int16_t buf[CHUNK * 2];
    int64_t pos = 0;
    bool ok = true;

    usf_voice_mute_mask = 0;
    while (!check_stop())
    {
        usf_voice_mute_mask = read_mute_mask();
        int seek = check_seek();
        if (seek >= 0)
        {
            int64_t target = (int64_t)seek * OUT_RATE / 1000;
            if (target < pos) { usf_restart(st); pos = 0; }
            while (pos < target && !check_stop())      // перемотка вперёд = быстрый рендер
            {
                int n = (int)std::min<int64_t>(CHUNK, target - pos);
                if (usf_render_resampled(st, buf, n, OUT_RATE)) { ok = false; break; }
                pos += n;
            }
            if (!ok) break;
        }
        if (pos >= total) break;

        int n = (int)std::min<int64_t>(CHUNK, total - pos);
        if (usf_render_resampled(st, buf, n, OUT_RATE)) { ok = false; break; }

        if (pos + n > fade_start && fade > 0)
        {
            for (int i = 0; i < n; i++)
            {
                int64_t p = pos + i;
                if (p < fade_start) continue;
                double g = 1.0 - (double)(p - fade_start) / (fade * OUT_RATE);
                if (g < 0) g = 0;
                buf[2 * i] = (int16_t)(buf[2 * i] * g);
                buf[2 * i + 1] = (int16_t)(buf[2 * i + 1] * g);
            }
        }
        apply_output(buf, n);
        write_audio(buf, n * 4);
        pos += n;
    }

    usf_voice_mute_mask = 0;
    usf_shutdown(st);
    free(st);
    return ok;
}
