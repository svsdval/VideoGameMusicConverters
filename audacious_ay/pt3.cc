// Audacious 4.x input plugin: ZX Spectrum Pro Tracker 3 (.pt3)
// Плеер - порт PT3Player из pt32xm-midi-wav.py (по pt3player.c, Bulba/Volutar).
#include "export.hpp"
#include <cstdio>
#include <cstring>
#include <string>
#include <sstream>
#include <libaudcore/audstrings.h>
#include <libaudcore/tuple.h>
#include "ayplugin.hpp"

namespace {

constexpr int FPS = 50;

const int NT_PT[96] = {
    0xC22,0xB73,0xACF,0xA33,0x9A1,0x917,0x894,0x819,0x7A4,0x737,0x6CF,0x66D,
    0x611,0x5BA,0x567,0x51A,0x4D0,0x48B,0x44A,0x40C,0x3D2,0x39B,0x367,0x337,
    0x308,0x2DD,0x2B4,0x28D,0x268,0x246,0x225,0x206,0x1E9,0x1CE,0x1B4,0x19B,
    0x184,0x16E,0x15A,0x146,0x134,0x123,0x112,0x103,0x0F5,0x0E7,0x0DA,0x0CE,
    0x0C2,0x0B7,0x0AD,0x0A3,0x09A,0x091,0x089,0x082,0x07A,0x073,0x06D,0x067,
    0x061,0x05C,0x056,0x052,0x04D,0x049,0x045,0x041,0x03D,0x03A,0x036,0x033,
    0x031,0x02E,0x02B,0x029,0x027,0x024,0x022,0x020,0x01F,0x01D,0x01B,0x01A,
    0x018,0x017,0x016,0x014,0x013,0x012,0x011,0x010,0x00F,0x00E,0x00D,0x00C};
const int NT_ASM[96] = {
    0xD10,0xC55,0xBA4,0xAFC,0xA5F,0x9CA,0x93D,0x8B8,0x83B,0x7C5,0x755,0x6EC,
    0x688,0x62A,0x5D2,0x57E,0x52F,0x4E5,0x49E,0x45C,0x41D,0x3E2,0x3AB,0x376,
    0x344,0x315,0x2E9,0x2BF,0x298,0x272,0x24F,0x22E,0x20F,0x1F1,0x1D5,0x1BB,
    0x1A2,0x18B,0x174,0x160,0x14C,0x139,0x128,0x117,0x107,0x0F9,0x0EB,0x0DD,
    0x0D1,0x0C5,0x0BA,0x0B0,0x0A6,0x09D,0x094,0x08C,0x084,0x07C,0x075,0x06F,
    0x069,0x063,0x05D,0x058,0x053,0x04E,0x04A,0x046,0x042,0x03E,0x03B,0x037,
    0x034,0x031,0x02F,0x02C,0x029,0x027,0x025,0x023,0x021,0x01F,0x01D,0x01C,
    0x01A,0x019,0x017,0x016,0x015,0x014,0x012,0x011,0x010,0x00F,0x00E,0x00D};
const int NT_ST[96] = {
    0xEF8,0xE10,0xD60,0xC80,0xBD8,0xB28,0xA88,0x9F0,0x960,0x8E0,0x858,0x7E0,
    0x77C,0x708,0x6B0,0x640,0x5EC,0x594,0x544,0x4F8,0x4B0,0x470,0x42C,0x3FD,
    0x3BE,0x384,0x358,0x320,0x2F6,0x2CA,0x2A2,0x27C,0x258,0x238,0x216,0x1F8,
    0x1DF,0x1C2,0x1AC,0x190,0x17B,0x165,0x151,0x13E,0x12C,0x11C,0x10A,0x0FC,
    0x0EF,0x0E1,0x0D6,0x0C8,0x0BD,0x0B2,0x0A8,0x09F,0x096,0x08E,0x085,0x07E,
    0x077,0x070,0x06B,0x064,0x05E,0x059,0x054,0x04F,0x04B,0x047,0x042,0x03F,
    0x03B,0x038,0x035,0x032,0x02F,0x02C,0x02A,0x027,0x025,0x023,0x021,0x01F,
    0x01D,0x01C,0x01A,0x019,0x017,0x016,0x015,0x013,0x012,0x011,0x010,0x00F};
const int NT_REAL[96] = {
    0xCDA,0xC22,0xB73,0xACF,0xA33,0x9A1,0x917,0x894,0x819,0x7A4,0x737,0x6CF,
    0x66D,0x611,0x5BA,0x567,0x51A,0x4D0,0x48B,0x44A,0x40C,0x3D2,0x39B,0x367,
    0x337,0x308,0x2DD,0x2B4,0x28D,0x268,0x246,0x225,0x206,0x1E9,0x1CE,0x1B4,
    0x19B,0x184,0x16E,0x15A,0x146,0x134,0x123,0x112,0x103,0x0F5,0x0E7,0x0DA,
    0x0CE,0x0C2,0x0B7,0x0AD,0x0A3,0x09A,0x091,0x089,0x082,0x07A,0x073,0x06D,
    0x067,0x061,0x05C,0x056,0x052,0x04D,0x049,0x045,0x041,0x03D,0x03A,0x036,
    0x033,0x031,0x02E,0x02B,0x029,0x027,0x024,0x022,0x020,0x01F,0x01D,0x01B,
    0x01A,0x018,0x017,0x016,0x014,0x013,0x012,0x011,0x010,0x00F,0x00E,0x00D};

const int VT34_STEP[16] = {0,18,36,54,72,94,111,132,145,161,174,194,207,225,247,256};

struct Chan
{
    int address = 0, orn_ptr = 0, smp_ptr = 0, tone = 0;
    int loop_orn = 0, orn_len = 0, pos_orn = 0;
    int loop_smp = 0, smp_len = 0, pos_smp = 0;
    int volume = 15, notes_to_skip = 1, note = 0;
    int slide_to_note = 0, amplitude = 0;
    bool env_enabled = false, enabled = false, simple_gliss = true;
    int cur_amp_slide = 0, cur_noise_slide = 0, cur_env_slide = 0;
    int ton_slide_count = 0, cur_onoff = 0, onoff_delay = 0, offon_delay = 0;
    int ton_slide_delay = 0, cur_ton_sliding = 0, ton_accum = 0;
    int ton_slide_step = 0, ton_delta = 0, skip_counter = 1;
    int smp_idx = 0;
    bool note_changed = false;
};

struct State
{
    int env_base = 0, cur_env_slide = 0, env_slide_add = 0;
    int cur_env_delay = 0, env_delay = 0, noise_base = 0;
    int delay = 6, add_to_noise = 0, delay_counter = 1, current_position = 0;
};

class PT3Player
{
public:
    std::vector<uint8_t> d;
    int version = 6, tone_table_id = 0, initial_delay = 6, num_positions = 0, loop_position = 0;
    int pat_ptr = 0;
    std::string title, author;
    std::vector<int> positions, smp_ptrs, orn_ptrs;
    Chan ch[3];
    State st;
    int ay[14] = {0};
    int temp_mixer = 0, add_to_env = 0;
    uint32_t muted_smp = 0;     // маска отключённых сэмплов (1..31)
    bool no_orn = false;        // игнорировать орнаменты

    explicit PT3Player(std::vector<uint8_t> data) : d(std::move(data))
    {
        parse_header();
        init_playback();
    }

    int b(int p) const { return (p >= 0 && p < (int)d.size()) ? d[p] : 0; }
    int w(int p) const { return (p >= 0 && p + 1 < (int)d.size()) ? (d[p] | (d[p + 1] << 8)) : 0; }
    int sw(int p) const { int v = w(p); return v >= 32768 ? v - 65536 : v; }

    static std::string text(const std::vector<uint8_t> & d, size_t a, size_t len)
    {
        std::string s;
        for (size_t i = a; i < a + len && i < d.size(); i++) s += (d[i] >= 32 && d[i] < 127) ? (char)d[i] : '?';
        while (!s.empty() && (s.back() == ' ' || s.back() == '?' )) s.pop_back();
        return s;
    }

    void parse_header()
    {
        int v = b(0x0D);
        version = (v >= 0x30 && v <= 0x39) ? v - 0x30 : 6;
        title = text(d, 0x1E, 32);
        author = text(d, 0x42, 32);
        tone_table_id = b(0x63);
        initial_delay = b(0x64);
        if (initial_delay == 0) initial_delay = 6;
        int i = 0;
        while (i < 65535 - 201) { if (b(0xC9 + i) == 255) break; i++; }
        num_positions = b(0x65) > 0 ? std::min(i, b(0x65)) : i;
        loop_position = b(0x66);
        pat_ptr = w(0x67);
        for (int k = 0; k < num_positions; k++) positions.push_back(b(0xC9 + k));
        for (int k = 0; k < 32; k++) smp_ptrs.push_back(w(0x69 + k * 2));
        for (int k = 0; k < 16; k++) orn_ptrs.push_back(w(0xA9 + k * 2));
    }

    int note_freq(int j) const
    {
        j = std::max(0, std::min(95, j));
        switch (tone_table_id) {
            case 0: return NT_PT[j];
            case 1: return NT_ST[j];
            case 2: return NT_ASM[j];
            default: return NT_REAL[j];
        }
    }

    void set_smp(Chan & c, int idx)
    {
        c.smp_idx = idx & 31;
        c.smp_ptr = smp_ptrs[idx & 31];
        c.loop_smp = b(c.smp_ptr);
        c.smp_len = b(c.smp_ptr + 1);
        c.smp_ptr += 2;
    }
    void set_orn(Chan & c, int idx)
    {
        c.orn_ptr = orn_ptrs[idx & 15];
        c.loop_orn = b(c.orn_ptr);
        c.orn_len = b(c.orn_ptr + 1);
        c.orn_ptr += 2;
    }

    void init_playback()
    {
        st.delay = initial_delay; st.delay_counter = 1;
        if (positions.empty()) return;
        int i = positions[0];
        for (int abc = 0; abc < 3; abc++)
        {
            Chan & c = ch[abc];
            c.address = w(pat_ptr + (i + abc) * 2);
            set_orn(c, 0);
            set_smp(c, 1);
            c.volume = 15; c.skip_counter = 1; c.notes_to_skip = 1;
            c.enabled = false; c.env_enabled = false; c.note = 0; c.tone = 0;
        }
    }

    void pattern_interpreter(int abc)
    {
        Chan & c = ch[abc];
        int pr_note = c.note, pr_sliding = c.cur_ton_sliding;
        c.note_changed = false;
        std::vector<int> ef;
        bool quit = false;
        while (!quit && c.address < (int)d.size())
        {
            int op = b(c.address);
            if (op >= 0xF0)
            {
                set_orn(c, op - 0xF0);
                c.pos_orn = 0; c.address++;
                set_smp(c, b(c.address) / 2);
                c.env_enabled = false;
            }
            else if (op >= 0xD1 && op <= 0xEF)
            {
                if (op <= 0xDF) set_smp(c, op - 0xD0);
                else { set_orn(c, op - 0xE0); c.pos_orn = 0; }
            }
            else if (op == 0xD0) quit = true;
            else if (op >= 0xC1 && op <= 0xCF) c.volume = op - 0xC0;
            else if (op == 0xC0)
            {
                c.pos_smp = 0; c.cur_amp_slide = 0; c.cur_noise_slide = 0; c.cur_env_slide = 0;
                c.pos_orn = 0; c.ton_slide_count = 0; c.cur_ton_sliding = 0; c.ton_accum = 0;
                c.cur_onoff = 0; c.enabled = false; quit = true;
            }
            else if (op >= 0xB2 && op <= 0xBF)
            {
                c.env_enabled = true; ay[13] = op - 0xB1;
                c.address++; int hi = b(c.address); c.address++; int lo = b(c.address);
                st.env_base = (hi << 8) | lo; c.pos_orn = 0; st.cur_env_slide = 0; st.cur_env_delay = 0;
            }
            else if (op == 0xB1) { c.address++; c.notes_to_skip = b(c.address); }
            else if (op == 0xB0) { c.env_enabled = false; c.pos_orn = 0; }
            else if (op >= 0x50 && op <= 0xAF)
            {
                c.note = op - 0x50; c.pos_smp = 0; c.cur_amp_slide = 0; c.cur_noise_slide = 0;
                c.cur_env_slide = 0; c.pos_orn = 0; c.ton_slide_count = 0; c.cur_ton_sliding = 0;
                c.ton_accum = 0; c.cur_onoff = 0; c.enabled = true; c.note_changed = true; quit = true;
            }
            else if (op >= 0x40 && op <= 0x4F) { set_orn(c, op - 0x40); c.pos_orn = 0; }
            else if (op >= 0x20 && op <= 0x3F) st.noise_base = op - 0x20;
            else if (op >= 0x11 && op <= 0x1F)
            {
                ay[13] = op - 0x10; c.address++; int hi = b(c.address);
                c.address++; int lo = b(c.address); st.env_base = (hi << 8) | lo;
                st.cur_env_slide = 0; st.cur_env_delay = 0; c.env_enabled = true;
                c.address++; set_smp(c, b(c.address) / 2); c.pos_orn = 0;
            }
            else if (op == 0x10)
            {
                c.env_enabled = false; c.address++; set_smp(c, b(c.address) / 2); c.pos_orn = 0;
            }
            else if (op >= 1 && op <= 9) ef.push_back(op);
            c.address++;
        }
        for (auto it = ef.rbegin(); it != ef.rend(); ++it)
        {
            int e = *it;
            if (e == 1)
            {
                c.ton_slide_delay = b(c.address); c.address++;
                c.ton_slide_count = c.ton_slide_delay;
                c.ton_slide_step = sw(c.address); c.address += 2;
                c.simple_gliss = true; c.cur_onoff = 0;
            }
            else if (e == 2)
            {
                c.simple_gliss = false; c.cur_onoff = 0;
                c.ton_slide_delay = b(c.address); c.address++;
                c.ton_slide_count = c.ton_slide_delay;
                c.ton_slide_step = std::abs(sw(c.address + 2));
                c.ton_delta = note_freq(c.note) - note_freq(pr_note);
                c.slide_to_note = c.note; c.note = pr_note;
                if (version >= 6) c.cur_ton_sliding = pr_sliding;
                if (c.ton_delta - c.cur_ton_sliding < 0) c.ton_slide_step = -c.ton_slide_step;
                c.address += 4;
            }
            else if (e == 3) { c.pos_smp = b(c.address); c.address++; }
            else if (e == 4) { c.pos_orn = b(c.address); c.address++; }
            else if (e == 5)
            {
                c.onoff_delay = b(c.address); c.address++;
                c.offon_delay = b(c.address); c.address++;
                c.cur_onoff = c.onoff_delay; c.ton_slide_count = 0; c.cur_ton_sliding = 0;
            }
            else if (e == 8)
            {
                st.env_delay = b(c.address); c.address++; st.cur_env_delay = st.env_delay;
                st.env_slide_add = sw(c.address); c.address += 2;
            }
            else if (e == 9) { st.delay = b(c.address); c.address++; }
        }
        c.skip_counter = c.notes_to_skip;
    }

    void change_registers(int abc)
    {
        Chan & c = ch[abc];
        if (c.enabled)
        {
            int sb = c.smp_ptr + c.pos_smp * 4;
            int b0 = b(sb), b1 = b(sb + 1);
            c.tone = w(sb + 2) + c.ton_accum;
            if (b1 & 0x40) c.ton_accum = c.tone;
            int ov = b(c.orn_ptr + c.pos_orn);
            if (ov >= 128) ov -= 256;
            if (no_orn) ov = 0;
            int j = std::max(0, std::min(95, c.note + ov));
            int wv = note_freq(j);
            c.tone = (c.tone + c.cur_ton_sliding + wv) & 0xFFF;
            if (c.ton_slide_count > 0)
            {
                c.ton_slide_count--;
                if (c.ton_slide_count == 0)
                {
                    c.cur_ton_sliding += c.ton_slide_step; c.ton_slide_count = c.ton_slide_delay;
                    if (!c.simple_gliss)
                        if ((c.ton_slide_step < 0 && c.cur_ton_sliding <= c.ton_delta) ||
                            (c.ton_slide_step >= 0 && c.cur_ton_sliding >= c.ton_delta))
                        { c.note = c.slide_to_note; c.ton_slide_count = 0; c.cur_ton_sliding = 0; }
                }
            }
            c.amplitude = b1 & 0x0F;
            if (b0 & 0x80)
            {
                if (b0 & 0x40) { if (c.cur_amp_slide < 15) c.cur_amp_slide++; }
                else if (c.cur_amp_slide > -15) c.cur_amp_slide--;
            }
            c.amplitude += c.cur_amp_slide;
            c.amplitude = std::max(0, std::min(15, c.amplitude));
            c.amplitude = version <= 4 ? ((c.amplitude * VT34_STEP[c.volume]) >> 8)
                                       : (c.volume * c.amplitude + 8) / 15;
            if (!(b0 & 1) && c.env_enabled) c.amplitude |= 0x10;
            if ((muted_smp >> c.smp_idx) & 1) c.amplitude = 0;
            if (b1 & 0x80)
            {
                int j2 = (b0 & 0x20) ? (((b0 >> 1) | 0xF0) + c.cur_env_slide)
                                     : (((b0 >> 1) & 0x0F) + c.cur_env_slide);
                if (b1 & 0x20) c.cur_env_slide = j2;
                add_to_env += j2;
            }
            else
            {
                st.add_to_noise = ((b0 >> 1) & 0xFF) + c.cur_noise_slide;
                if (b1 & 0x20) c.cur_noise_slide = st.add_to_noise;
            }
            temp_mixer |= ((b1 >> 1) & 0x48);
            c.pos_smp++;
            if (c.pos_smp >= c.smp_len) c.pos_smp = c.loop_smp;
            c.pos_orn++;
            if (c.pos_orn >= c.orn_len) c.pos_orn = c.loop_orn;
        }
        else c.amplitude = 0;
        temp_mixer >>= 1;
        if (c.cur_onoff > 0)
        {
            c.cur_onoff--;
            if (c.cur_onoff == 0)
            {
                c.enabled = !c.enabled;
                c.cur_onoff = c.enabled ? c.onoff_delay : c.offon_delay;
            }
        }
    }

    ay::Regs play_tick()
    {
        ay[13] = 0xFF;
        for (auto & c : ch) c.note_changed = false;
        if (positions.empty()) { ay::Regs z; z.fill(0); z[13] = 0xFF; z[7] = 0x3F; return z; }
        if (--st.delay_counter == 0)
        {
            if (--ch[0].skip_counter == 0)
            {
                if (b(ch[0].address) == 0)
                {
                    st.current_position++;
                    if (st.current_position >= num_positions) st.current_position = loop_position;
                    int i = positions[st.current_position % positions.size()];
                    for (int abc = 0; abc < 3; abc++) ch[abc].address = w(pat_ptr + (i + abc) * 2);
                    st.noise_base = 0;
                }
                pattern_interpreter(0);
            }
            for (int abc = 1; abc < 3; abc++)
                if (--ch[abc].skip_counter == 0) pattern_interpreter(abc);
            st.delay_counter = st.delay;
        }
        add_to_env = 0; temp_mixer = 0;
        change_registers(0); change_registers(1); change_registers(2);
        for (int k = 0; k < 3; k++) { ay[k * 2] = ch[k].tone & 0xFF; ay[k * 2 + 1] = ch[k].tone >> 8; }
        ay[6] = (st.noise_base + st.add_to_noise) & 0x1F; ay[7] = temp_mixer;
        for (int k = 0; k < 3; k++) ay[8 + k] = ch[k].amplitude;
        int env = (st.env_base + add_to_env + st.cur_env_slide) & 0xFFFF;
        ay[11] = env & 0xFF; ay[12] = env >> 8;
        if (st.cur_env_delay > 0)
        {
            st.cur_env_delay--;
            if (st.cur_env_delay == 0) { st.cur_env_delay = st.env_delay; st.cur_env_slide += st.env_slide_add; }
        }
        ay::Regs r;
        for (int k = 0; k < 14; k++) r[k] = (uint8_t)ay[k];
        return r;
    }

    bool finished(int frame) const
    {
        return st.current_position >= num_positions - 1 && st.delay_counter == 1 &&
               ch[0].skip_counter == 1 && frame > 100 && b(ch[0].address) == 0;
    }

    // как pt3_frames() в Python
    std::vector<ay::Regs> run(std::vector<std::array<bool, 3>> * started = nullptr)
    {
        std::vector<ay::Regs> out;
        int max_frames = num_positions * 64 * initial_delay + 1000;
        for (int f = 0; f < max_frames; f++)
        {
            out.push_back(play_tick());
            if (started) started->push_back({ch[0].note_changed, ch[1].note_changed, ch[2].note_changed});
            if (finished(f)) break;
        }
        return out;
    }
};

// "3, 5, 10-12" -> маска
uint32_t parse_sample_list(const char * str)
{
    uint32_t m = 0;
    int a = -1, cur = -1;
    bool range = false;
    auto flush = [&]() {
        if (cur >= 0)
        {
            int lo = range && a >= 0 ? a : cur;
            for (int k = lo; k <= cur && k < 32; k++) if (k >= 0) m |= 1u << k;
        }
        a = cur = -1; range = false;
    };
    for (const char * p = str; ; p++)
    {
        if (*p >= '0' && *p <= '9') cur = (cur < 0 ? 0 : cur * 10) + (*p - '0');
        else if (*p == '-') { a = cur; cur = -1; range = true; }
        else { flush(); if (!*p) break; }
    }
    return m;
}

bool read_file(const char * filename, std::vector<uint8_t> & d)
{
    StringBuf path = uri_to_filename(filename);
    if (!path) return false;
    FILE * f = fopen(path, "rb");
    if (!f) return false;
    uint8_t buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) d.insert(d.end(), buf, buf + n);
    fclose(f);
    return d.size() > 0xC9;
}

AY_DEFINE_COMMON("pt3")
AY_EXPORT_DEFINE_COMMON

const PreferencesWidget widgets[] = {
    AY_COMMON_WIDGETS("pt3"),
    WidgetSeparator(),
    WidgetLabel(N_("<b>Samples / ornaments</b> (applied at the next track start)")),
    WidgetEntry(N_("Muted samples (e.g. 3, 5, 10-12):"), WidgetString("pt3", "muted_samples")),
    WidgetCheck(N_("Disable ornaments (arpeggios / pitch tables)"), WidgetBool("pt3", "no_ornaments")),
    WidgetSeparator(),
    EXPORT_WIDGETS("pt3"),
    AY_EXPORT_WIDGETS("pt3")};
const PluginPreferences prefs = {{widgets}};
const char * const defaults[] = {AY_COMMON_DEFAULTS, "muted_samples", "", "no_ornaments", "FALSE", EXPORT_DEFAULTS, AY_EXPORT_DEFAULTS, nullptr};

} // namespace


static std::string pt3_export(const exporter::Job & j, bool xm, bool midi)
{
    std::vector<uint8_t> d;
    if (!read_file(("file://" + j.input).c_str(), d)) return "cannot read file";
    PT3Player p(std::move(d));
    std::vector<std::array<bool, 3>> started;
    auto frames = p.run(&started);
    return ayexp::export_ay(frames, started, p.title, j.out_base, xm, midi, ay_export_options("pt3", FPS));
}

EXPORT_DEFINE("pt3", "pt32xm-midi-wav.py", true, nullptr, "Export selected to XM/MIDI (PT3)...", pt3_export, "pt3")

class PT3Plugin : public AYPluginBase
{
public:
    static const char about[];
    static const char * const exts[];
    static constexpr PluginInfo info = {N_("PT3 Decoder (ZX Spectrum)"), "pt3", about, &prefs};

    constexpr PT3Plugin() : AYPluginBase(info, InputInfo().with_exts(exts), "pt3") {}
    bool init() { aud_config_set_defaults("pt3", defaults); export_register(); return true; }
    void cleanup() { export_unregister(); }

    bool read_tag(const char * filename, VFSFile &, Tuple & tuple, Index<char> *)
    {
        std::vector<uint8_t> d;
        if (!read_file(filename, d)) return false;
        PT3Player p(std::move(d));
        auto frames = p.run();
        if (!p.title.empty()) tuple.set_str(Tuple::Title, p.title.c_str());
        if (!p.author.empty()) tuple.set_str(Tuple::Artist, p.author.c_str());
        tuple.set_int(Tuple::Length, (int)(frames.size() * 1000 / FPS));
        tuple.set_str(Tuple::Codec, "Pro Tracker 3");
        tuple.set_str(Tuple::Quality, "sequenced");
        return true;
    }

    bool play(const char * filename, VFSFile &)
    {
        std::vector<uint8_t> d;
        if (!read_file(filename, d)) return false;
        PT3Player p(std::move(d));
        p.muted_smp = parse_sample_list(aud_get_str("pt3", "muted_samples"));
        p.no_orn = aud_get_bool("pt3", "no_ornaments");
        return play_frames(p.run(), FPS);
    }
};

const char PT3Plugin::about[] =
    N_("ZX Spectrum Pro Tracker 3 player\nPlayer based on pt3player.c by Bulba/Volutar.\n"
       "Part of VideoGameMusicConverters.");
const char * const PT3Plugin::exts[] = {"pt3", nullptr};

EXPORT PT3Plugin aud_plugin_instance;
