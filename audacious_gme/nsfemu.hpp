// Эмулятор NSF (6502 + APU 2A03) с журналом событий - порт nsf_pyemu.py на C++.
// Расширения звука (VRC6/VRC7/FDS/MMC5/N163/5B) не эмулируются: только 2A03.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <tuple>
#include <vector>

namespace nsfemu {

constexpr int NTSC_CLOCK = 1789773, PAL_CLOCK = 1662607;
constexpr double MIX_PULSE = 0.0210, MIX_TRI = 0.02375, MIX_NOISE = 0.0127, DMC_GAIN = 1.385;
constexpr double DMC_STEP_GAIN = DMC_GAIN * 159.79 / 22638.0;
constexpr double HP_TAU = 0.0028;
constexpr int HALT_ADDR = 0x4800;

static const int LENGTH_TABLE[32] = {10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
                                     12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30};
static const int NOISE_PERIOD[2][16] = {{4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068},
                                        {4, 8, 14, 30, 60, 88, 118, 148, 188, 236, 354, 472, 708, 944, 1890, 3778}};
static const int DMC_PERIOD[2][16] = {{428, 380, 340, 320, 286, 254, 226, 214, 190, 160, 142, 128, 106, 84, 72, 54},
                                      {398, 354, 316, 298, 276, 236, 210, 198, 176, 148, 132, 118, 98, 78, 66, 50}};
static const int DUTY_SEQ[4][8] = {{0, 1, 0, 0, 0, 0, 0, 0}, {0, 1, 1, 0, 0, 0, 0, 0},
                                   {0, 1, 1, 1, 1, 0, 0, 0}, {1, 0, 0, 1, 1, 1, 1, 1}};

inline double dmc_curve(double d)
{
    return DMC_GAIN * (d > 0 ? 159.79 / (22638.0 / std::max(d, 1e-9) + 100.0) : 0.0);
}

struct NSF
{
    int version = 0, total_songs = 1, start_song = 1;
    int load_addr = 0, init_addr = 0, play_addr = 0;
    std::string title, artist, copyright;
    int ntsc_us = 0, pal_us = 0;
    uint8_t banks[8] = {0};
    bool pal = false, banked = false;
    int chips = 0;
    std::vector<uint8_t> data;
};

inline std::string cstr(const std::vector<uint8_t> & d, size_t a, size_t n)
{
    std::string s;
    for (size_t i = a; i < a + n && i < d.size() && d[i]; i++) s += (char)d[i];
    while (!s.empty() && s.back() == ' ') s.pop_back();
    while (!s.empty() && s.front() == ' ') s.erase(0, 1);
    return s;
}

inline bool load_nsf(const std::string & path, NSF & n)
{
    FILE * f = fopen(path.c_str(), "rb");
    if (!f) return false;
    std::vector<uint8_t> d;
    uint8_t buf[65536];
    size_t k;
    while ((k = fread(buf, 1, sizeof buf, f)) > 0) d.insert(d.end(), buf, buf + k);
    fclose(f);
    if (d.size() < 0x80 || memcmp(d.data(), "NESM\x1a", 5)) return false;
    auto w = [&](size_t o) { return d[o] | (d[o + 1] << 8); };
    n.version = d[5];
    n.total_songs = d[6] ? d[6] : 1;
    n.start_song = d[7] ? d[7] : 1;
    n.load_addr = w(8); n.init_addr = w(10); n.play_addr = w(12);
    n.title = cstr(d, 0x0E, 32); n.artist = cstr(d, 0x2E, 32); n.copyright = cstr(d, 0x4E, 32);
    n.ntsc_us = w(0x6E);
    for (int i = 0; i < 8; i++) n.banks[i] = d[0x70 + i];
    n.pal_us = w(0x78);
    n.pal = d[0x7A] & 1;
    n.chips = d[0x7B];
    n.data.assign(d.begin() + 0x80, d.end());
    n.banked = false;
    for (int i = 0; i < 8; i++) if (n.banks[i]) n.banked = true;
    return true;
}

// ── APU ──
struct Env
{
    bool start = false, loop = false, cst = false;
    int div = 0, decay = 0, vol = 0;
    void clock()
    {
        if (start) { start = false; decay = 15; div = vol; }
        else if (div == 0)
        {
            div = vol;
            if (decay > 0) decay--;
            else if (loop) decay = 15;
        }
        else div--;
    }
    int level() const { return cst ? vol : decay; }
};

struct Pulse
{
    explicit Pulse(bool ones) : ones(ones) {}
    bool ones;
    Env env;
    int duty = 0, timer = 0, length = 0, sw_period = 0, sw_shift = 0, sw_div = 0;
    bool halt = false, sw_en = false, sw_neg = false, sw_reload = false;
    int target() const
    {
        int d = timer >> sw_shift;
        if (sw_neg) return timer - d - (ones ? 1 : 0);
        return timer + d;
    }
    void clock_sweep()
    {
        int t = target();
        if (sw_div == 0 && sw_en && sw_shift && timer >= 8 && t <= 0x7FF) timer = std::max(0, t);
        if (sw_div == 0 || sw_reload) { sw_div = sw_period; sw_reload = false; }
        else sw_div--;
    }
    int level() const
    {
        if (length == 0 || timer < 8 || target() > 0x7FF) return 0;
        return env.level();
    }
};

struct Ev { int64_t cyc; bool trig; int a, b, lv; };
struct DmcEv
{
    bool is_start;
    int64_t cyc;
    int idx = 0, period = 0, level = 0;
    bool loop = false;
    std::vector<uint8_t> data;
};
struct DacW { int64_t cyc; int v; };

struct FcStep { int cyc, q, h; };
static const FcStep FC4[4] = {{7457, 1, 0}, {14913, 1, 1}, {22371, 1, 0}, {29829, 1, 1}};
static const FcStep FC5[5] = {{7457, 1, 0}, {14913, 1, 1}, {22371, 1, 0}, {29829, 0, 0}, {37281, 1, 1}};
constexpr int FC4_LEN = 29830, FC5_LEN = 37282;

struct APU
{
    int clock;
    bool pal;
    const int * noise_tab, * dmc_tab;
    bool en[5] = {false, false, false, false, false};
    Pulse pl[2] = {Pulse(true), Pulse(false)};
    int tri_timer = 0, tri_length = 0, tri_reload_val = 0, tri_counter = 0;
    bool tri_ctrl = false, tri_reload = false;
    Env nz_env;
    int nz_length = 0, nz_mode = 0, nz_idx = 0;
    bool nz_halt = false;
    int dmc_idx = 0, dmc_addr = 0xC000, dmc_len = 1, dmc_out = 0;
    bool dmc_loop = false, dmc_irq = false, dmc_active = false;
    int64_t dmc_end = 0;
    const uint8_t * mem = nullptr;
    std::vector<DmcEv> dmc_events;
    std::vector<DacW> dac_writes;
    bool fc5 = false, fc_inhibit = false, fc_irq = false;
    int64_t fc_start = 0, next_tick = FC4[0].cyc;
    int fc_step = 0;
    std::vector<Ev> ev[4];
    bool have_last[4] = {false, false, false, false};
    std::array<int, 3> last[4];
    int64_t sound_cycle = 0, cyc = 0;

    APU(int clock, bool pal) : clock(clock), pal(pal), noise_tab(NOISE_PERIOD[pal]), dmc_tab(DMC_PERIOD[pal]) {}

    std::array<int, 3> obs(int ch) const
    {
        if (ch < 2) { const Pulse & p = pl[ch]; return {p.timer, p.duty, p.level()}; }
        if (ch == 2)
        {
            bool on = en[2] && tri_length > 0 && tri_counter > 0 && tri_timer >= 2;
            return {tri_timer, 0, on ? 15 : 0};
        }
        int lv = (en[3] && nz_length > 0) ? nz_env.level() : 0;
        return {nz_idx, nz_mode, lv};
    }
    void log(int ch, int64_t c, bool trig = false)
    {
        auto s = obs(ch);
        if (trig || !have_last[ch] || s != last[ch])
        {
            ev[ch].push_back({c, trig, s[0], s[1], s[2]});
            last[ch] = s;
            have_last[ch] = true;
        }
        if (s[2]) sound_cycle = c;
    }
    void log_all(int64_t c) { for (int ch = 0; ch < 4; ch++) log(ch, c); }

    void tick(int64_t c, int q, int h)
    {
        if (q)
        {
            for (auto & p : pl) p.env.clock();
            nz_env.clock();
            if (tri_reload) tri_counter = tri_reload_val;
            else if (tri_counter > 0) tri_counter--;
            if (!tri_ctrl) tri_reload = false;
        }
        if (h)
        {
            for (auto & p : pl)
            {
                if (!p.halt && p.length > 0) p.length--;
                p.clock_sweep();
            }
            if (!tri_ctrl && tri_length > 0) tri_length--;
            if (!nz_halt && nz_length > 0) nz_length--;
        }
        log_all(c);
    }

    void advance(int64_t c)
    {
        while (next_tick <= c)
        {
            int64_t t = next_tick;
            const FcStep * seq = fc5 ? FC5 : FC4;
            int len = fc5 ? 5 : 4;
            tick(t, seq[fc_step].q, seq[fc_step].h);
            fc_step++;
            if (fc_step >= len) { fc_step = 0; fc_start += fc5 ? FC5_LEN : FC4_LEN; }
            next_tick = fc_start + seq[fc_step].cyc;
        }
        if (dmc_active && !dmc_loop && c >= dmc_end) dmc_active = false;
        cyc = c;
    }

    void dmc_start(int64_t c)
    {
        int n = dmc_len;
        DmcEv e;
        e.is_start = true; e.cyc = c;
        if (mem) for (int i = 0; i < n; i++) e.data.push_back(mem[0x8000 | ((dmc_addr + i) & 0x7FFF)]);
        int per = dmc_tab[dmc_idx];
        e.idx = dmc_idx; e.period = per; e.loop = dmc_loop; e.level = dmc_out;
        dmc_active = true;
        dmc_end = c + (int64_t)n * 8 * per;
        dmc_events.push_back(std::move(e));
        sound_cycle = c;
    }

    void write(int addr, int v, int64_t c)
    {
        advance(c);
        if (addr <= 0x4007)
        {
            int ch = (addr - 0x4000) >> 2;
            Pulse & p = pl[ch];
            int r = addr & 3;
            bool trig = false;
            if (r == 0)
            {
                p.duty = v >> 6;
                p.halt = p.env.loop = v & 0x20;
                p.env.cst = v & 0x10;
                p.env.vol = v & 15;
            }
            else if (r == 1)
            {
                p.sw_en = v & 0x80; p.sw_period = (v >> 4) & 7; p.sw_neg = v & 8; p.sw_shift = v & 7;
                p.sw_reload = true;
            }
            else if (r == 2) p.timer = (p.timer & 0x700) | v;
            else
            {
                p.timer = (p.timer & 0xFF) | ((v & 7) << 8);
                if (en[ch]) p.length = LENGTH_TABLE[v >> 3];
                p.env.start = true;
                trig = true;
            }
            log(ch, c, trig);
        }
        else if (addr <= 0x400B)
        {
            int r = addr & 3;
            bool trig = false;
            if (r == 0) { tri_ctrl = v & 0x80; tri_reload_val = v & 0x7F; }
            else if (r == 2) tri_timer = (tri_timer & 0x700) | v;
            else if (r == 3)
            {
                tri_timer = (tri_timer & 0xFF) | ((v & 7) << 8);
                if (en[2]) tri_length = LENGTH_TABLE[v >> 3];
                tri_reload = true;
                trig = true;
            }
            log(2, c, trig);
        }
        else if (addr <= 0x400F)
        {
            int r = addr & 3;
            bool trig = false;
            if (r == 0) { nz_halt = nz_env.loop = v & 0x20; nz_env.cst = v & 0x10; nz_env.vol = v & 15; }
            else if (r == 2) { nz_mode = v >> 7; nz_idx = v & 15; }
            else if (r == 3)
            {
                if (en[3]) nz_length = LENGTH_TABLE[v >> 3];
                nz_env.start = true;
                trig = true;
            }
            log(3, c, trig);
        }
        else if (addr == 0x4010) { dmc_idx = v & 15; dmc_loop = v & 0x40; dmc_irq = v & 0x80; }
        else if (addr == 0x4011) { dmc_out = v & 0x7F; dac_writes.push_back({c, dmc_out}); sound_cycle = c; }
        else if (addr == 0x4012) dmc_addr = 0xC000 + v * 64;
        else if (addr == 0x4013) dmc_len = v * 16 + 1;
        else if (addr == 0x4015)
        {
            for (int ch = 0; ch < 4; ch++) en[ch] = v & (1 << ch);
            if (!en[0]) pl[0].length = 0;
            if (!en[1]) pl[1].length = 0;
            if (!en[2]) tri_length = 0;
            if (!en[3]) nz_length = 0;
            en[4] = v & 16;
            if (en[4]) { if (!dmc_active) dmc_start(c); }
            else if (dmc_active)
            {
                dmc_active = false;
                DmcEv e; e.is_start = false; e.cyc = c;
                dmc_events.push_back(e);
            }
            log_all(c);
        }
        else if (addr == 0x4017)
        {
            fc5 = v & 0x80; fc_inhibit = v & 0x40;
            fc_start = c; fc_step = 0;
            next_tick = c + (fc5 ? FC5[0].cyc : FC4[0].cyc);
            if (fc5) tick(c, 1, 1);
        }
    }

    int read_status(int64_t c)
    {
        advance(c);
        int v = 0;
        if (pl[0].length) v |= 1;
        if (pl[1].length) v |= 2;
        if (tri_length) v |= 4;
        if (nz_length) v |= 8;
        if (dmc_active) v |= 16;
        if (fc_irq) v |= 0x40;
        fc_irq = false;
        return v;
    }
};

struct Trace
{
    int64_t start = 0, end = 0, sound_end = 0;
    int clock = 0, frames = 0, timeouts = 0, bad_ops = 0;
    bool pal = false;
    double cpp = 0;
    std::vector<Ev> ev[4];
    std::vector<DmcEv> dmc_events;
    std::vector<DacW> dac_writes;
    const int * noise_tab = nullptr;
};

// ── плеер + 6502 ──
class Player
{
public:
    const NSF & nsf;
    int song, clock;
    double cpp;
    int a = 0, x = 0, y = 0, sp = 0xFD, pc = 0, c = 0, ov = 0, fn = 0, fi = 1, fd = 0;
    bool fz = false;
    int64_t cyc = 0;
    int bad = -1, vblank = 0;
    std::vector<uint8_t> mem;
    APU apu;
    bool banked, pal;
    int rom_start;
    std::vector<uint8_t> image;

    Player(const NSF & n, int song)
        : nsf(n), song(song), clock(n.pal ? PAL_CLOCK : NTSC_CLOCK), mem(65536, 0), apu(clock, n.pal),
          banked(n.banked), pal(n.pal)
    {
        int us = n.pal ? n.pal_us : n.ntsc_us;
        if (!us) us = n.pal ? 20000 : 16666;
        if (us == (n.pal ? 20000 : 16666)) cpp = n.pal ? 33247.5 : 29780.5;
        else cpp = us * (double)clock / 1e6;
        apu.mem = mem.data();
        if (banked)
        {
            image.assign(n.load_addr & 0xFFF, 0);
            image.insert(image.end(), n.data.begin(), n.data.end());
        }
        load();
    }

    void set_bank(int slot, int bank)
    {
        size_t src = (size_t)bank * 0x1000;
        for (int i = 0; i < 0x1000; i++)
            mem[0x8000 + slot * 0x1000 + i] = (src + i < image.size()) ? image[src + i] : 0;
    }
    void load()
    {
        if (banked) for (int s = 0; s < 8; s++) set_bank(s, nsf.banks[s]);
        else
        {
            size_t la = nsf.load_addr, n = std::min<size_t>(nsf.data.size(), 0x10000 - la);
            memcpy(&mem[la], nsf.data.data(), n);
        }
        mem[HALT_ADDR] = 0x02;
        rom_start = banked ? 0x8000 : std::min(0x8000, nsf.load_addr);
    }

    int rd_io(int ad, int64_t cy)
    {
        if (ad == 0x4015) return apu.read_status(cy);
        if (ad == 0x2002) { vblank ^= 0x80; return vblank; }
        if (ad == 0x4016 || ad == 0x4017) return 0;
        return mem[ad];
    }
    void wr(int ad, int v, int64_t cy)
    {
        if (ad < 0x2000) mem[ad & 0x7FF] = v;
        else if (ad >= 0x4000 && ad <= 0x4017) { if (ad != 0x4014 && ad != 0x4016) apu.write(ad, v, cy); }
        else if (ad >= 0x5FF8 && ad <= 0x5FFF) { if (banked) set_bank(ad - 0x5FF8, v); }
        else if (ad < 0x8000) mem[ad] = v;
        else if (!banked && ad < rom_start) mem[ad] = v;
    }

    // 0 - вернулась (HALT), 1 - лимит тактов, 2 - плохой опкод
    int run_core(int64_t limit);

    int call(int addr, int ra = 0, int rx = 0, int64_t budget = 2000000)
    {
        a = ra; x = rx; y = 0;
        sp = 0xFD;
        int t = (HALT_ADDR - 1) & 0xFFFF;
        mem[0x100 + sp] = t >> 8;
        mem[0x100 + ((sp - 1) & 255)] = t & 255;
        sp = (sp - 2) & 255;
        pc = addr;
        fi = 1;
        return run_core(cyc + budget);
    }

    int init_song()
    {
        for (int i = 0; i < 0x800; i++) mem[i] = 0;
        if (banked) for (int s = 0; s < 8; s++) set_bank(s, nsf.banks[s]);
        for (int r = 0x4000; r < 0x4014; r++) apu.write(r, 0, 0);
        apu.write(0x4015, 0x0F, 0);
        apu.write(0x4017, 0x40, 0);
        return call(nsf.init_addr, song, pal ? 1 : 0, (int64_t)(clock * 3));
    }

    Trace run(double seconds, double stop_silence)
    {
        init_song();
        apu.advance(cyc);
        int64_t base = cyc, total = (int64_t)(seconds * clock);
        int64_t k = 0;
        int timeouts = 0, badc = 0;
        bool heard = false;
        int64_t silence = (int64_t)(stop_silence * clock);
        while (true)
        {
            int64_t target = base + (int64_t)(k * cpp);
            if (target - base >= total) break;
            if (cyc < target) cyc = target;
            int r = call(nsf.play_addr, 0, 0, (int64_t)(cpp * 4));
            if (r == 1) timeouts++;
            else if (r == 2) badc++;
            apu.advance(cyc);
            k++;
            if (apu.sound_cycle) heard = true;
            if (silence && heard && cyc - std::max(apu.sound_cycle, base) > silence) break;
        }
        int64_t end = std::max(cyc, base + (int64_t)(k * cpp));
        apu.advance(end);
        Trace tr;
        tr.start = base; tr.clock = clock; tr.pal = pal; tr.cpp = cpp; tr.frames = (int)k; tr.end = end;
        for (int i = 0; i < 4; i++) tr.ev[i] = apu.ev[i];
        tr.dmc_events = apu.dmc_events; tr.dac_writes = apu.dac_writes;
        tr.sound_end = apu.sound_cycle; tr.timeouts = timeouts; tr.bad_ops = badc;
        tr.noise_tab = apu.noise_tab;
        return tr;
    }
};

#define RDM(ea) (((ea) < 8192) ? mem[(ea) & 2047] : ((ea) >= 24576 ? mem[ea] : rd_io((ea), cyc)))
#define NZ(r) do { fn = (r) & 128; fz = ((r) == 0); } while (0)

inline int Player::run_core(int64_t limit)
{
    uint8_t * m = mem.data();
    (void)m;
    int res = 1;
    while (cyc < limit)
    {
        int op = mem[pc];
        pc = (pc + 1) & 65535;
        int ea = 0, t = 0, v = 0;
        bool direct = false, pen = false;
        int rc = 0, sc = 0, rmwc = 0, mode = -1;
        // режимы: 0 imm,1 zp,2 zpx,3 zpy,4 abs,5 abx,6 aby,7 inx,8 iny
        auto setmode = [&](int md) {
            mode = md;
            switch (md)
            {
            case 0: ea = pc; pc = (pc + 1) & 65535; rc = 2; direct = true; break;
            case 1: ea = mem[pc]; pc = (pc + 1) & 65535; rc = 3; sc = 3; rmwc = 5; direct = true; break;
            case 2: ea = (mem[pc] + x) & 255; pc = (pc + 1) & 65535; rc = 4; sc = 4; rmwc = 6; direct = true; break;
            case 3: ea = (mem[pc] + y) & 255; pc = (pc + 1) & 65535; rc = 4; sc = 4; rmwc = 0; direct = true; break;
            case 4: ea = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535; rc = 4; sc = 4; rmwc = 6; break;
            case 5: t = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535; ea = (t + x) & 65535;
                    rc = 4; sc = 5; rmwc = 7; pen = true; break;
            case 6: t = mem[pc] | (mem[(pc + 1) & 65535] << 8); pc = (pc + 2) & 65535; ea = (t + y) & 65535;
                    rc = 4; sc = 5; rmwc = 7; pen = true; break;
            case 7: t = (mem[pc] + x) & 255; pc = (pc + 1) & 65535; ea = mem[t] | (mem[(t + 1) & 255] << 8);
                    rc = 6; sc = 6; rmwc = 8; break;
            case 8: t = mem[pc]; pc = (pc + 1) & 65535; t = mem[t] | (mem[(t + 1) & 255] << 8); ea = (t + y) & 65535;
                    rc = 5; sc = 6; rmwc = 8; pen = true; break;
            }
        };
        auto ADC_ = [&]() {
            t = a + v + c;
            ov = (~(a ^ v) & (a ^ t) & 128) >> 7;
            c = t >> 8; a = t & 255;
            NZ(a);
        };
        auto CMP_ = [&](int R) { t = R - v; c = t >= 0 ? 1 : 0; t &= 255; fn = t & 128; fz = t == 0; };
        auto wrv = [&]() { if (direct) mem[ea] = v; else wr(ea, v, cyc); };
        auto pushb = [&](int val) { mem[256 + sp] = val; sp = (sp - 1) & 255; };
        auto pullb = [&]() { sp = (sp + 1) & 255; return (int)mem[256 + sp]; };
        auto setp = [&](int p) { c = p & 1; fz = p & 2; fi = (p >> 2) & 1; fd = (p >> 3) & 1; ov = (p >> 6) & 1; fn = p & 128; };

        enum { R_LDA, R_LDX, R_LDY, R_AND, R_ORA, R_EOR, R_ADC, R_SBC, R_CMP, R_CPX, R_CPY, R_BIT, R_LAX, R_NOP,
               R_ANC, R_ALR, R_ARR, R_AXS };
        enum { M_ASL, M_LSR, M_ROL, M_ROR, M_INC, M_DEC, M_DCP, M_ISB, M_SLO, M_RLA, M_SRE, M_RRA };
        auto do_read = [&](int name, int md) {
            setmode(md);
            cyc += rc;
            v = direct ? mem[ea] : RDM(ea);
            if (pen && ((t ^ ea) & 65280)) cyc += 1;
            switch (name)
            {
            case R_LDA: a = v; NZ(a); break;
            case R_LDX: x = v; NZ(x); break;
            case R_LDY: y = v; NZ(y); break;
            case R_AND: a &= v; NZ(a); break;
            case R_ORA: a |= v; NZ(a); break;
            case R_EOR: a ^= v; NZ(a); break;
            case R_ADC: ADC_(); break;
            case R_SBC: v ^= 255; ADC_(); break;
            case R_CMP: CMP_(a); break;
            case R_CPX: CMP_(x); break;
            case R_CPY: CMP_(y); break;
            case R_BIT: fn = v & 128; ov = (v >> 6) & 1; fz = (a & v) == 0; break;
            case R_LAX: a = x = v; NZ(a); break;
            case R_NOP: break;
            case R_ANC: a &= v; NZ(a); c = fn >> 7; break;
            case R_ALR: a &= v; c = a & 1; a >>= 1; NZ(a); break;
            case R_ARR: a &= v; a = (a >> 1) | (c << 7); NZ(a); c = (a >> 6) & 1; ov = ((a >> 6) ^ (a >> 5)) & 1; break;
            case R_AXS: t = (a & x) - v; c = t >= 0 ? 1 : 0; x = t & 255; NZ(x); break;
            }
        };
        auto do_store = [&](int val_kind, int md) {
            setmode(md);
            cyc += sc;
            int val = val_kind == 0 ? a : val_kind == 1 ? x : val_kind == 2 ? y : (a & x);
            if (direct) mem[ea] = val; else wr(ea, val, cyc);
        };
        auto do_rmw = [&](int name, int md) {
            setmode(md);
            cyc += rmwc;
            v = direct ? mem[ea] : RDM(ea);
            bool wrote = false;
            switch (name)
            {
            case M_ASL: c = v >> 7; v = (v << 1) & 255; NZ(v); break;
            case M_LSR: c = v & 1; v >>= 1; NZ(v); break;
            case M_ROL: t = (v << 1) | c; c = v >> 7; v = t & 255; NZ(v); break;
            case M_ROR: t = (v >> 1) | (c << 7); c = v & 1; v = t; NZ(v); break;
            case M_INC: v = (v + 1) & 255; NZ(v); break;
            case M_DEC: v = (v - 1) & 255; NZ(v); break;
            case M_DCP: v = (v - 1) & 255; wrv(); wrote = true; CMP_(a); break;
            case M_ISB: v = (v + 1) & 255; wrv(); wrote = true; v ^= 255; ADC_(); break;
            case M_SLO: c = v >> 7; v = (v << 1) & 255; wrv(); wrote = true; a |= v; NZ(a); break;
            case M_RLA: t = (v << 1) | c; c = v >> 7; v = t & 255; wrv(); wrote = true; a &= v; NZ(a); break;
            case M_SRE: c = v & 1; v >>= 1; wrv(); wrote = true; a ^= v; NZ(a); break;
            case M_RRA: t = (v >> 1) | (c << 7); c = v & 1; v = t; wrv(); wrote = true; ADC_(); break;
            }
            if (!wrote) wrv();
        };

        static const int alu[8] = {R_ORA, R_AND, R_EOR, R_ADC, -1, R_LDA, R_CMP, R_SBC};
        static const int modes01[8] = {7, 1, 0, 4, 8, 2, 6, 5};

        if ((op & 3) == 1)
        {
            int aaa = op >> 5, bbb = (op >> 2) & 7;
            if (aaa == 4)
            {
                if (bbb == 2) { goto nop_imm; }     // STA #imm не существует (0x89 = NOP imm)
                do_store(0, modes01[bbb]);
            }
            else do_read(alu[aaa], modes01[bbb]);
            continue;
        }
        switch (op)
        {
        // cc=10 RMW: сдвиги, INC/DEC
        case 0x06: do_rmw(M_ASL, 1); break; case 0x16: do_rmw(M_ASL, 2); break;
        case 0x0E: do_rmw(M_ASL, 4); break; case 0x1E: do_rmw(M_ASL, 5); break;
        case 0x26: do_rmw(M_ROL, 1); break; case 0x36: do_rmw(M_ROL, 2); break;
        case 0x2E: do_rmw(M_ROL, 4); break; case 0x3E: do_rmw(M_ROL, 5); break;
        case 0x46: do_rmw(M_LSR, 1); break; case 0x56: do_rmw(M_LSR, 2); break;
        case 0x4E: do_rmw(M_LSR, 4); break; case 0x5E: do_rmw(M_LSR, 5); break;
        case 0x66: do_rmw(M_ROR, 1); break; case 0x76: do_rmw(M_ROR, 2); break;
        case 0x6E: do_rmw(M_ROR, 4); break; case 0x7E: do_rmw(M_ROR, 5); break;
        case 0xC6: do_rmw(M_DEC, 1); break; case 0xD6: do_rmw(M_DEC, 2); break;
        case 0xCE: do_rmw(M_DEC, 4); break; case 0xDE: do_rmw(M_DEC, 5); break;
        case 0xE6: do_rmw(M_INC, 1); break; case 0xF6: do_rmw(M_INC, 2); break;
        case 0xEE: do_rmw(M_INC, 4); break; case 0xFE: do_rmw(M_INC, 5); break;
        // LDX / STX
        case 0xA2: do_read(R_LDX, 0); break; case 0xA6: do_read(R_LDX, 1); break;
        case 0xB6: do_read(R_LDX, 3); break; case 0xAE: do_read(R_LDX, 4); break;
        case 0xBE: do_read(R_LDX, 6); break;
        case 0x86: do_store(1, 1); break; case 0x96: do_store(1, 3); break; case 0x8E: do_store(1, 4); break;
        // LDY / CPY / CPX / BIT / STY
        case 0xA0: do_read(R_LDY, 0); break; case 0xA4: do_read(R_LDY, 1); break;
        case 0xB4: do_read(R_LDY, 2); break; case 0xAC: do_read(R_LDY, 4); break;
        case 0xBC: do_read(R_LDY, 5); break;
        case 0xC0: do_read(R_CPY, 0); break; case 0xC4: do_read(R_CPY, 1); break; case 0xCC: do_read(R_CPY, 4); break;
        case 0xE0: do_read(R_CPX, 0); break; case 0xE4: do_read(R_CPX, 1); break; case 0xEC: do_read(R_CPX, 4); break;
        case 0x24: do_read(R_BIT, 1); break; case 0x2C: do_read(R_BIT, 4); break;
        case 0x84: do_store(2, 1); break; case 0x94: do_store(2, 2); break; case 0x8C: do_store(2, 4); break;
        // переходы
        case 0x10: case 0x30: case 0x50: case 0x70: case 0x90: case 0xB0: case 0xD0: case 0xF0:
        {
            cyc += 2;
            t = mem[pc];
            pc = (pc + 1) & 65535;
            bool cond = false;
            switch (op)
            {
            case 0x10: cond = !fn; break; case 0x30: cond = fn; break;
            case 0x50: cond = !ov; break; case 0x70: cond = ov; break;
            case 0x90: cond = !c; break; case 0xB0: cond = c; break;
            case 0xD0: cond = !fz; break; case 0xF0: cond = fz; break;
            }
            if (cond)
            {
                cyc += 1;
                t = (t & 128) ? ((pc + t - 256) & 65535) : ((pc + t) & 65535);
                if ((t ^ pc) & 65280) cyc += 1;
                pc = t;
            }
            break;
        }
        // implied
        case 0x18: cyc += 2; c = 0; break; case 0x38: cyc += 2; c = 1; break;
        case 0x58: cyc += 2; fi = 0; break; case 0x78: cyc += 2; fi = 1; break;
        case 0xB8: cyc += 2; ov = 0; break; case 0xD8: cyc += 2; fd = 0; break; case 0xF8: cyc += 2; fd = 1; break;
        case 0xAA: cyc += 2; x = a; NZ(x); break; case 0xA8: cyc += 2; y = a; NZ(y); break;
        case 0x8A: cyc += 2; a = x; NZ(a); break; case 0x98: cyc += 2; a = y; NZ(a); break;
        case 0xBA: cyc += 2; x = sp; NZ(x); break; case 0x9A: cyc += 2; sp = x; break;
        case 0xE8: cyc += 2; x = (x + 1) & 255; NZ(x); break; case 0xC8: cyc += 2; y = (y + 1) & 255; NZ(y); break;
        case 0xCA: cyc += 2; x = (x - 1) & 255; NZ(x); break; case 0x88: cyc += 2; y = (y - 1) & 255; NZ(y); break;
        case 0xEA: cyc += 2; break;
        case 0x0A: cyc += 2; c = a >> 7; a = (a << 1) & 255; NZ(a); break;
        case 0x4A: cyc += 2; c = a & 1; a >>= 1; NZ(a); break;
        case 0x2A: cyc += 2; t = (a << 1) | c; c = a >> 7; a = t & 255; NZ(a); break;
        case 0x6A: cyc += 2; t = (a >> 1) | (c << 7); c = a & 1; a = t; NZ(a); break;
        case 0x48: cyc += 3; pushb(a); break;
        case 0x08: cyc += 3; pushb(c | (fz << 1) | (fi << 2) | (fd << 3) | 48 | (ov << 6) | fn); break;
        case 0x68: cyc += 4; a = pullb(); NZ(a); break;
        case 0x28: cyc += 4; setp(pullb()); break;
        case 0x4C: cyc += 3; pc = mem[pc] | (mem[(pc + 1) & 65535] << 8); break;
        case 0x6C:
        {
            cyc += 5;
            ea = mem[pc] | (mem[(pc + 1) & 65535] << 8);
            int e2 = (ea & 65280) | ((ea + 1) & 255);
            int lo = RDM(ea);
            int hi = RDM(e2);
            pc = lo | (hi << 8);
            break;
        }
        case 0x20:
        {
            cyc += 6;
            ea = mem[pc] | (mem[(pc + 1) & 65535] << 8);
            t = (pc + 1) & 65535;
            mem[256 + sp] = t >> 8; mem[256 + ((sp - 1) & 255)] = t & 255; sp = (sp - 2) & 255;
            pc = ea;
            break;
        }
        case 0x60:
            cyc += 6;
            t = mem[256 + ((sp + 1) & 255)] | (mem[256 + ((sp + 2) & 255)] << 8);
            sp = (sp + 2) & 255; pc = (t + 1) & 65535;
            break;
        case 0x40:
            cyc += 6;
            setp(pullb());
            pc = mem[256 + ((sp + 1) & 255)] | (mem[256 + ((sp + 2) & 255)] << 8);
            sp = (sp + 2) & 255;
            break;
        // неофициальные
        case 0xA3: do_read(R_LAX, 7); break; case 0xA7: do_read(R_LAX, 1); break; case 0xAF: do_read(R_LAX, 4); break;
        case 0xB3: do_read(R_LAX, 8); break; case 0xB7: do_read(R_LAX, 3); break; case 0xBF: do_read(R_LAX, 6); break;
        case 0x83: do_store(3, 7); break; case 0x87: do_store(3, 1); break; case 0x8F: do_store(3, 4); break;
        case 0x97: do_store(3, 3); break;
        case 0xEB: cyc += 2; ea = pc; pc = (pc + 1) & 65535; v = mem[ea] ^ 255; ADC_(); break;
        case 0x0B: case 0x2B: do_read(R_ANC, 0); break;
        case 0x4B: do_read(R_ALR, 0); break;
        case 0x6B: do_read(R_ARR, 0); break;
        case 0xCB: do_read(R_AXS, 0); break;
        case 0x1A: case 0x3A: case 0x5A: case 0x7A: case 0xDA: case 0xFA: cyc += 2; break;
        case 0x80: case 0x82: case 0x89: case 0xC2: case 0xE2:
        nop_imm:
            do_read(R_NOP, 0); break;
        case 0x04: case 0x44: case 0x64: do_read(R_NOP, 1); break;
        case 0x14: case 0x34: case 0x54: case 0x74: case 0xD4: case 0xF4: do_read(R_NOP, 2); break;
        case 0x0C: do_read(R_NOP, 4); break;
        case 0x1C: case 0x3C: case 0x5C: case 0x7C: case 0xDC: case 0xFC: do_read(R_NOP, 5); break;
        case 0x02: res = 0; goto done;
        default:
        {
            // SLO/RLA/SRE/RRA/DCP/ISB (xx3, xx7, xxF, ...)
            static const int rm_modes_lo[7] = {0x03, 0x07, 0x0F, 0x13, 0x17, 0x1B, 0x1F};
            static const int rm_modes[7] = {7, 1, 4, 8, 2, 6, 5};
            int hi = op & 0xE0, lo = op & 0x1F;
            int name = -1;
            switch (hi)
            {
            case 0x00: name = M_SLO; break; case 0x20: name = M_RLA; break; case 0x40: name = M_SRE; break;
            case 0x60: name = M_RRA; break; case 0xC0: name = M_DCP; break; case 0xE0: name = M_ISB; break;
            }
            int md = -1;
            for (int i = 0; i < 7; i++) if (lo == rm_modes_lo[i]) md = rm_modes[i];
            if (name >= 0 && md >= 0 && (op & 3) == 3) { do_rmw(name, md); break; }
            bad = op;
            res = 2;
            goto done;
        }
        }
    }
done:
    return res;
}

} // namespace nsfemu
