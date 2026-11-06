// Встроенный экспорт VGM/VGZ (Mega Drive: YM2612 + SN76489) -> XM / MIDI на C++ (порт vgm2xm-midi-wav.py).
// Тембры FM снимаются с эмулятора чипа: рендер служебных VGM делает функция Renderer (libvgm).
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <tuple>
#include <vector>

#include <zlib.h>

namespace vgmexp {

constexpr int SR = 44100, SETTLE = 300, MAX_XM_CHANNELS = 32, MAX_XM_INSTRUMENTS = 128;
constexpr double XM_C4_RATE = 8363.0;
static const int DRUM_NOTES[12] = {36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56};
static const int ALG_CARRIERS[8][4] = {{4, 0, 0, 0}, {4, 0, 0, 0}, {4, 0, 0, 0}, {4, 0, 0, 0},
                                       {2, 4, 0, 0}, {2, 3, 4, 0}, {2, 3, 4, 0}, {1, 2, 3, 4}};
static const int ALG_NCAR[8] = {1, 1, 1, 1, 2, 3, 3, 4};
inline int op_off(int o) { return o == 1 ? 0 : o == 3 ? 4 : o == 2 ? 8 : 12; }
inline int slot_dummy() { return 0; }
inline bool is_carrier(int alg, int o)
{
    for (int i = 0; i < ALG_NCAR[alg]; i++) if (ALG_CARRIERS[alg][i] == o) return true;
    return false;
}

struct Options
{
    int octave = 0, transpose = 0, compact = 1, channels = 32, zone_width = 12, loops = 1;
    double bend_threshold = 0.6, norm_scale = 0.7;
    bool no_pan = false, no_frame_grid = false;
};

// mono float64 (шкала int16) = среднее L и R
using Renderer = std::function<bool(const std::vector<uint8_t> &, std::vector<double> &)>;

// ── numpy-совместимые суммы ──
inline double np_sum(const double * a, size_t n)
{
    if (n < 8)
    {
        double r = -0.0;
        for (size_t i = 0; i < n; i++) r += a[i];
        return r;
    }
    if (n <= 128)
    {
        double r[8];
        for (int j = 0; j < 8; j++) r[j] = a[j];
        size_t i;
        for (i = 8; i < n - (n % 8); i += 8)
            for (int j = 0; j < 8; j++) r[j] += a[i + j];
        double res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for (; i < n; i++) res += a[i];
        return res;
    }
    size_t n2 = n / 2;
    n2 -= n2 % 8;
    return np_sum(a, n2) + np_sum(a + n2, n - n2);
}
inline void pw_sum_c(const double * a, size_t n, double & rr, double & ri)   // n = число double (2 на элемент)
{
    if (n < 8)
    {
        rr = -0.0; ri = -0.0;
        for (size_t i = 0; i < n; i += 2) { rr += a[i]; ri += a[i + 1]; }
    }
    else if (n <= 128)
    {
        double r[8];
        for (int j = 0; j < 8; j++) r[j] = a[j];
        size_t i;
        for (i = 8; i < n - (n % 8); i += 8)
            for (int j = 0; j < 8; j++) r[j] += a[i + j];
        rr = (r[0] + r[2]) + (r[4] + r[6]);
        ri = (r[1] + r[3]) + (r[5] + r[7]);
        for (; i < n; i += 2) { rr += a[i]; ri += a[i + 1]; }
    }
    else
    {
        size_t n2 = n / 2;
        n2 -= n2 % 8;
        double r1, i1, r2, i2;
        pw_sum_c(a, n2, r1, i1);
        pw_sum_c(a + n2, n - n2, r2, i2);
        rr = r1 + r2; ri = i1 + i2;
    }
}
inline std::complex<double> np_mean_expi(const std::vector<double> & t, double d)
{
    std::vector<double> buf(t.size() * 2);
    for (size_t i = 0; i < t.size(); i++)
    {
        double th = 2 * M_PI * t[i] / d;
        buf[2 * i] = std::cos(th);
        buf[2 * i + 1] = std::sin(th);
    }
    double rr, ri;
    pw_sum_c(buf.data(), buf.size(), rr, ri);
    return {rr / (double)t.size(), ri / (double)t.size()};
}

using cd = std::complex<double>;
inline void fft_rec(std::vector<cd> & a)
{
    size_t n = a.size();
    if (n <= 1) return;
    size_t p = 2;
    while (p * p <= n && n % p) p++;
    if (n % p) p = n;
    if (p == n)
    {
        std::vector<cd> out(n);
        for (size_t k = 0; k < n; k++)
        {
            cd s = 0;
            for (size_t j = 0; j < n; j++) s += a[j] * std::polar(1.0, -2 * M_PI * (double)((k * j) % n) / n);
            out[k] = s;
        }
        a = out;
        return;
    }
    size_t m = n / p;
    std::vector<std::vector<cd>> sub(p, std::vector<cd>(m));
    for (size_t j = 0; j < n; j++) sub[j % p][j / p] = a[j];
    for (auto & s : sub) fft_rec(s);
    for (size_t k = 0; k < n; k++)
    {
        cd s = 0;
        for (size_t r = 0; r < p; r++) s += sub[r][k % m] * std::polar(1.0, -2 * M_PI * (double)((r * k) % n) / n);
        a[k] = s;
    }
}

// centroid = sum(|rfft(x*hanning)| * rfftfreq) / (sum(|rfft|) + 1e-9)
inline double spectral_centroid(const std::vector<double> & d0, double sample_rate)
{
    size_t n = std::min<size_t>(d0.size(), 8192);
    if (n == 0) return 0;
    std::vector<cd> a(n);
    for (size_t i = 0; i < n; i++)
    {
        double w = n == 1 ? 1.0 : 0.5 - 0.5 * std::cos(2 * M_PI * i / (n - 1));
        a[i] = d0[i] * w;
    }
    fft_rec(a);
    size_t K = n / 2 + 1;
    std::vector<double> sp(K), prod(K);
    double val = 1.0 / (n * (1.0 / sample_rate));
    for (size_t k = 0; k < K; k++) { sp[k] = std::abs(a[k]); prod[k] = sp[k] * ((double)k * val); }
    return np_sum(prod.data(), K) / (np_sum(sp.data(), K) + 1e-9);
}

// ── разбор VGM ──
struct Ev { int64_t t; char op; int a, b; };

struct VGM
{
    std::vector<uint8_t> raw;
    uint32_t version = 0, sn_clk = 0, ym_clk = 0;
    int sn_flag_a = 9, sn_flag_b = 16;
    std::vector<Ev> events;
    int64_t end_t = 0;
};

inline bool gunzip(const std::vector<uint8_t> & in, std::vector<uint8_t> & out)
{
    z_stream zs;
    memset(&zs, 0, sizeof zs);
    if (inflateInit2(&zs, 15 + 32) != Z_OK) return false;
    zs.next_in = const_cast<Bytef *>(in.data());
    zs.avail_in = (uInt)in.size();
    uint8_t buf[65536];
    int r;
    do
    {
        zs.next_out = buf;
        zs.avail_out = sizeof buf;
        r = inflate(&zs, Z_NO_FLUSH);
        if (r != Z_OK && r != Z_STREAM_END) { inflateEnd(&zs); return false; }
        out.insert(out.end(), buf, buf + (sizeof buf - zs.avail_out));
    } while (r != Z_STREAM_END);
    inflateEnd(&zs);
    return true;
}

inline bool parse_vgm(std::vector<uint8_t> d, VGM & v)
{
    if (d.size() >= 2 && d[0] == 0x1f && d[1] == 0x8b)
    {
        std::vector<uint8_t> o;
        if (!gunzip(d, o)) return false;
        d = std::move(o);
    }
    if (d.size() < 0x40 || memcmp(d.data(), "Vgm ", 4)) return false;
    auto u32 = [&](size_t o) -> uint32_t {
        return o + 4 <= d.size() ? (d[o] | (d[o + 1] << 8) | (d[o + 2] << 16) | ((uint32_t)d[o + 3] << 24)) : 0;
    };
    v.raw = d;
    v.version = u32(8);
    v.sn_clk = u32(0x0C) & 0x3FFFFFFF;
    v.ym_clk = v.version >= 0x110 ? (u32(0x2C) & 0x3FFFFFFF) : 0;
    if (v.version >= 0x110 && d.size() > 0x2A) { v.sn_flag_a = d[0x28] | (d[0x29] << 8); v.sn_flag_b = d[0x2A]; }
    if (v.sn_flag_a == 0) { v.sn_flag_a = 9; v.sn_flag_b = 16; }
    size_t data_off = (v.version >= 0x150 && u32(0x34)) ? (0x34 + u32(0x34)) : 0x40;
    std::vector<uint8_t> bank;
    size_t bank_off = 0;
    int64_t t = 0;
    size_t p = data_off;
    static const std::map<int, int> fixed = {{0x90, 5}, {0x91, 5}, {0x92, 6}, {0x93, 11}, {0x94, 2}, {0x95, 5}};
    while (p < d.size())
    {
        int o = d[p];
        auto byte = [&](size_t i) { return i < d.size() ? (int)d[i] : 0; };
        if (o == 0x4F) { v.events.push_back({t, 'G', byte(p + 1), 0}); p += 2; }
        else if (o == 0x50) { v.events.push_back({t, 'S', byte(p + 1), 0}); p += 2; }
        else if (o == 0x52 || o == 0x53) { v.events.push_back({t, 'Y', ((o - 0x52) << 8) | byte(p + 1), byte(p + 2)}); p += 3; }
        else if (o == 0x61) { t += byte(p + 1) | (byte(p + 2) << 8); p += 3; }
        else if (o == 0x62) { t += 735; p += 1; }
        else if (o == 0x63) { t += 882; p += 1; }
        else if (o == 0x66) break;
        else if (o == 0x67)
        {
            int typ = byte(p + 2);
            size_t size = u32(p + 3) & 0x7FFFFFFF;
            if (typ == 0x00)
                for (size_t i = 0; i < size && p + 7 + i < d.size(); i++) bank.push_back(d[p + 7 + i]);
            p += 7 + size;
        }
        else if (o == 0x68) p += 12;
        else if (o >= 0x70 && o <= 0x7F) { t += (o & 15) + 1; p += 1; }
        else if (o >= 0x80 && o <= 0x8F)
        {
            int val = bank_off < bank.size() ? bank[bank_off] : 0x80;
            bank_off++;
            v.events.push_back({t, 'Y', 0x2A, val});
            t += o & 15;
            p += 1;
        }
        else if (o == 0xE0) { bank_off = u32(p + 1); p += 5; }
        else if (fixed.count(o)) p += fixed.at(o);
        else if (o >= 0x30 && o <= 0x3F) p += 2;
        else if (o >= 0x40 && o <= 0x4E) p += 3;
        else if (o >= 0x51 && o <= 0x5F) p += 3;
        else if (o >= 0xA0 && o <= 0xBF) p += 3;
        else if (o >= 0xC0 && o <= 0xDF) p += 4;
        else if (o >= 0xE1) p += 5;
        else break;
    }
    v.end_t = t;
    return true;
}

// ── трекер ──
struct Patch
{
    std::array<std::array<int, 7>, 4> ops{};
    int b0 = 0, b4 = 0, lfo = 0;
    bool operator<(const Patch & o) const { return std::tie(ops, b0, b4, lfo) < std::tie(o.ops, o.b0, o.b4, o.lfo); }
    bool operator==(const Patch & o) const { return ops == o.ops && b0 == o.b0 && b4 == o.b4 && lfo == o.lfo; }
};

enum Kind { K_FM = 0, K_PSG, K_NOISE, K_DAC };
struct Cur { int64_t t; double f, lv; int pan; };

struct Note
{
    int kind, voice;
    int64_t start;
    int64_t end = -1;
    bool has_end = false;
    std::vector<Cur> cur;
    Patch patch;                   // FM
    int nfb = 0, nnf = 0;          // шум: (fb, nf)
    std::vector<uint8_t> dac;      // DAC
    double dac_rate = 0;
    bool pending = false;
    int64_t snap_at = 0;
    int inst = 0, start_row = 0, end_row = 0, channel = 0, tail_rows = 0;
    double t0 = 0, t1 = 0;
    std::vector<double> times;
    Note(int k, int v, int64_t s) : kind(k), voice(v), start(s) {}
};

class Tracker
{
public:
    const VGM & v;
    double fs;
    uint8_t R[2][256];
    int a4[6] = {0, 0, 0, 0, 0, 0};
    int fm_note[6] = {-1, -1, -1, -1, -1, -1};
    bool dac_en = false;
    std::vector<std::pair<int64_t, int>> dac_w;
    std::vector<std::unique_ptr<Note>> store;
    std::vector<Note *> notes;
    std::vector<std::pair<Note *, int>> pending;
    uint32_t sn_clk;
    int div[3] = {0, 0, 0}, att[4] = {15, 15, 15, 15}, noise = 0, latch_ch = 0, latch_typ = 0, gg = 0xFF;
    int psg_note[4] = {-1, -1, -1, -1};
    std::set<int> dirty;
    int64_t cur_t = 0;
    std::vector<Note *> out;

    explicit Tracker(const VGM & vv) : v(vv), fs(vv.ym_clk ? vv.ym_clk / 144.0 : 0.0), sn_clk(vv.sn_clk)
    {
        memset(R, 0, sizeof R);
    }

    Note * mk(int kind, int voice, int64_t s)
    {
        store.emplace_back(new Note(kind, voice, s));
        Note * n = store.back().get();
        notes.push_back(n);
        return n;
    }

    void feed(int64_t t, char op, int a, int b)
    {
        if (t != cur_t) { commit(cur_t); cur_t = t; }
        settle(t);
        if (op == 'Y') ym(t, a >> 8, a & 0xFF, b);
        else if (op == 'S') sn(t, a);
        else if (op == 'G') { gg = a; for (int i = 0; i < 4; i++) dirty.insert(i); }
    }

    void finish(int64_t t_end)
    {
        commit(cur_t);
        for (auto pr : std::vector<std::pair<Note *, int>>(pending)) finalize(pr.first);
        for (int c = 0; c < 6; c++) if (fm_note[c] >= 0) fm_off(t_end, c);
        for (int ch = 0; ch < 4; ch++)
            if (psg_note[ch] >= 0) { find(psg_note[ch])->end = t_end; find(psg_note[ch])->has_end = true; psg_note[ch] = -1; }
        make_dac();
        for (Note * n : notes)
        {
            if (!n->has_end) { n->end = t_end; n->has_end = true; }
            if (n->kind == K_DAC) { out.push_back(n); continue; }
            if (n->cur.empty()) continue;
            double mx = 0;
            for (auto & c : n->cur) mx = std::max(mx, c.lv);
            if (mx < 0.002) continue;
            out.push_back(n);
        }
        std::stable_sort(out.begin(), out.end(), [](Note * a, Note * b) { return std::tie(a->start, a->voice) < std::tie(b->start, b->voice); });
    }

private:
    // psg_note хранит индекс заметки в notes
    Note * find(int idx) { return notes[idx]; }

    void ym(int64_t t, int port, int reg, int val)
    {
        R[port][reg] = val;
        if (port == 0 && reg == 0x28)
        {
            int c = val & 3;
            if (c == 3) return;
            if (val & 4) c += 3;
            if (val >> 4)
            {
                if (fm_note[c] < 0 && !(c == 5 && dac_en) && fs != 0) fm_on(t, c);
            }
            else if (fm_note[c] >= 0) fm_off(t, c);
        }
        else if (port == 0 && reg == 0x2A) dac_w.push_back({t, val});
        else if (port == 0 && reg == 0x2B) dac_en = val & 0x80;
        else if ((reg >= 0xA0 && reg <= 0xA2) || (reg >= 0xA4 && reg <= 0xA6))
        {
            int i = reg & 3;
            if (i == 3) return;
            int c = port * 3 + i;
            if (reg >= 0xA4) a4[c] = val;
            else fm_update(t, c);
        }
        else if (reg >= 0x40 && reg <= 0x4F)
        {
            int i = reg & 3;
            if (i != 3) fm_update(t, port * 3 + i);
        }
        else if (reg >= 0xB4 && reg <= 0xB6) fm_update(t, port * 3 + (reg & 3));
    }

    double fm_freq(int c)
    {
        int port = c / 3, i = c % 3;
        int fnum = ((a4[c] & 7) << 8) | R[port][0xA0 + i];
        int blk = (a4[c] >> 3) & 7;
        return fnum * fs / (double)(1 << (21 - blk));
    }

    void fm_level_pan(int c, double & lv, int & pan)
    {
        int port = c / 3, i = c % 3;
        int alg = R[port][0xB0 + i] & 7;
        int mintl = 127;
        for (int k = 0; k < ALG_NCAR[alg]; k++)
            mintl = std::min(mintl, R[port][0x40 + i + op_off(ALG_CARRIERS[alg][k])] & 0x7F);
        lv = std::pow(10.0, -0.75 * mintl / 20.0);
        int b4 = R[port][0xB4 + i];
        bool left = b4 & 0x80, right = b4 & 0x40;
        if (left && right) pan = 128;
        else if (left) pan = 0;
        else if (right) pan = 255;
        else { pan = 128; lv = 0.0; }
    }

    int note_index(Note * n) { for (size_t i = 0; i < notes.size(); i++) if (notes[i] == n) return (int)i; return -1; }

    void fm_on(int64_t t, int c)
    {
        Note * n = mk(K_FM, c, t);
        n->pending = true;
        n->snap_at = t + SETTLE;
        fm_note[c] = note_index(n);
        pending.push_back({n, c});
        fm_update(t, c);
    }

    void fm_update(int64_t t, int c)
    {
        if (fm_note[c] < 0) return;
        Note * n = notes[fm_note[c]];
        double f = fm_freq(c), lv;
        int pan;
        fm_level_pan(c, lv, pan);
        if (n->cur.empty() || !(n->cur.back().f == f && n->cur.back().lv == lv && n->cur.back().pan == pan))
            n->cur.push_back({t, f, lv, pan});
    }

    void fm_off(int64_t t, int c)
    {
        if (fm_note[c] < 0) return;
        Note * n = notes[fm_note[c]];
        if (n->pending) finalize(n);
        n->end = t; n->has_end = true;
        fm_note[c] = -1;
    }

    void settle(int64_t t)
    {
        if (pending.empty()) return;
        for (auto item : std::vector<std::pair<Note *, int>>(pending))
            if (t >= item.first->snap_at) finalize(item.first);
    }

    void finalize(Note * n)
    {
        if (!n->pending) return;
        n->pending = false;
        std::vector<std::pair<Note *, int>> np;
        for (auto & pr : pending) if (pr.first != n) np.push_back(pr);
        pending = np;
        int c = n->voice, port = c / 3, i = c % 3;
        int b0 = R[port][0xB0 + i], alg = b0 & 7;
        int mintl = 127;
        for (int k = 0; k < ALG_NCAR[alg]; k++)
            mintl = std::min(mintl, R[port][0x40 + i + op_off(ALG_CARRIERS[alg][k])] & 0x7F);
        Patch p;
        for (int o = 1; o <= 4; o++)
        {
            int off = i + op_off(o);
            int tl = R[port][0x40 + off] & 0x7F;
            if (is_carrier(alg, o)) tl -= mintl;
            p.ops[o - 1] = {R[port][0x30 + off], tl, R[port][0x50 + off], R[port][0x60 + off], R[port][0x70 + off],
                            R[port][0x80 + off], R[port][0x90 + off] & 0x0F};
        }
        int lfo = (R[port][0xB4 + i] & 0x37) ? R[0][0x22] : 0;
        p.b0 = b0 & 0x3F; p.b4 = R[port][0xB4 + i] & 0x37; p.lfo = lfo;
        n->patch = p;
        double f = fm_freq(c), lv;
        int pan;
        fm_level_pan(c, lv, pan);
        std::vector<Cur> later;
        for (auto & e : n->cur) if (e.t >= n->snap_at) later.push_back(e);
        std::vector<Cur> cur;
        cur.push_back({n->start, f, lv, pan});
        cur.insert(cur.end(), later.begin(), later.end());
        n->cur = cur;
        if (!later.empty() && later[0].f == f && later[0].lv == lv && later[0].pan == pan)
        {
            if (later[0].t == n->start) n->cur = later;
            else { n->cur.resize(1); n->cur.insert(n->cur.end(), later.begin() + 1, later.end()); }
        }
    }

    void sn(int64_t, int val)
    {
        if (val & 0x80)
        {
            int ch = (val >> 5) & 3, typ = (val >> 4) & 1;
            latch_ch = ch; latch_typ = typ;
            int data = val & 15;
            if (typ) { att[ch] = data; dirty.insert(ch); }
            else if (ch < 3)
            {
                div[ch] = (div[ch] & 0x3F0) | data;
                dirty.insert(ch);
                if (ch == 2) dirty.insert(3);
            }
            else { noise = data & 7; dirty.insert(3); }
        }
        else
        {
            int ch = latch_ch, typ = latch_typ;
            if (typ) { att[ch] = val & 15; dirty.insert(ch); }
            else if (ch < 3)
            {
                div[ch] = (div[ch] & 0x0F) | ((val & 0x3F) << 4);
                dirty.insert(ch);
                if (ch == 2) dirty.insert(3);
            }
            else { noise = val & 7; dirty.insert(3); }
        }
    }

    void pan_of(int ch, int & pan, double & pg)
    {
        bool left = gg & (0x10 << ch), right = gg & (1 << ch);
        if (left && right) { pan = 128; pg = 1.0; }
        else if (left) { pan = 0; pg = 1.0; }
        else if (right) { pan = 255; pg = 1.0; }
        else { pan = 128; pg = 0.0; }
    }

    void commit(int64_t t)
    {
        if (dirty.empty()) return;
        std::set<int> dr;
        dr.swap(dirty);
        for (int ch : dr)
        {
            int a = att[ch], pan;
            double pg;
            pan_of(ch, pan, pg);
            double lv = a < 15 ? std::pow(10.0, -2.0 * a / 20.0) * pg : 0.0;
            double f;
            bool aud;
            int key_fb = 0, key_nf = 0;
            if (ch < 3)
            {
                int dv = div[ch] ? div[ch] : 1024;
                f = sn_clk / (32.0 * dv);
                aud = a < 15 && pg > 0 && 20 <= f && f <= 12000;
            }
            else
            {
                int nf = noise & 3;
                int n_ = nf < 3 ? (nf == 0 ? 16 : nf == 1 ? 32 : 64) : (div[2] ? div[2] : 1024);
                f = sn_clk / (32.0 * n_);
                aud = a < 15 && pg > 0;
                key_fb = (noise & 4) ? 1 : 0; key_nf = nf;
            }
            int idx = psg_note[ch];
            if (!aud)
            {
                if (idx >= 0) { notes[idx]->end = t; notes[idx]->has_end = true; psg_note[ch] = -1; }
                continue;
            }
            if (idx >= 0)
            {
                Note * n = notes[idx];
                const Cur & last = n->cur.back();
                double pf = last.f, plv = last.lv;
                int ppan = last.pan;
                bool retrig = false;
                if (ch < 3)
                {
                    double st = pf > 0 ? std::fabs(12 * std::log2(f / pf)) : 0;
                    if (lv > plv * 1.9 && st >= 0.5) retrig = true;
                    else if (lv > plv * 3.5) retrig = true;
                }
                else if (key_fb != n->nfb || key_nf != n->nnf || lv > plv * 3.5) retrig = true;
                if (retrig) { n->end = t; n->has_end = true; psg_note[ch] = -1; }
                else
                {
                    if (!(f == pf && lv == plv && pan == ppan)) n->cur.push_back({t, f, lv, pan});
                    continue;
                }
            }
            Note * n = mk(ch < 3 ? K_PSG : K_NOISE, ch < 3 ? 6 + ch : 9, t);
            n->nfb = key_fb; n->nnf = key_nf;
            n->cur.push_back({t, f, lv, pan});
            psg_note[ch] = note_index(n);
        }
    }

    void make_dac()
    {
        auto & w = dac_w;
        if (w.size() < 16) return;
        std::vector<std::vector<std::pair<int64_t, int>>> segs;
        segs.push_back({w[0]});
        for (size_t i = 1; i < w.size(); i++)
        {
            if (w[i].first - w[i - 1].first > 530) segs.push_back({});
            segs.back().push_back(w[i]);
        }
        for (auto & sg : segs)
        {
            if (sg.size() < 24) continue;
            std::vector<double> dts;
            for (size_t i = 1; i < sg.size(); i++) dts.push_back((double)(sg[i].first - sg[i - 1].first));
            std::sort(dts.begin(), dts.end());
            double dt = dts.size() % 2 ? dts[dts.size() / 2] : 0.5 * (dts[dts.size() / 2 - 1] + dts[dts.size() / 2]);
            if (dt <= 0) continue;
            std::vector<uint8_t> data;
            for (auto & x : sg) data.push_back((uint8_t)x.second);
            std::set<uint8_t> uniq(data.begin(), data.end());
            if (uniq.size() < 3) continue;
            Note * n = mk(K_DAC, 10, sg.front().first);
            n->end = (int64_t)((double)sg.back().first + dt);
            n->has_end = true;
            n->dac = data;
            n->dac_rate = SR / dt;
            n->cur.push_back({n->start, 0.0, 1.0, 128});
        }
    }
};

// ── генератор служебных VGM и рендер патчей ──
struct VgmBuilder
{
    uint32_t ym_clk, sn_clk;
    int fa, fb;
    std::vector<uint8_t> b;
    int64_t t = 0;
    VgmBuilder(uint32_t y, uint32_t s, int a, int bb) : ym_clk(y), sn_clk(s), fa(a), fb(bb) {}
    void ym(int port, int reg, int val) { b.push_back(0x52 + port); b.push_back(reg); b.push_back(val & 0xFF); }
    void sn(int val) { b.push_back(0x50); b.push_back(val & 0xFF); }
    void wait(double nn)
    {
        int n = (int)std::nearbyint(nn);
        while (n > 0)
        {
            int k = std::min(n, 65535);
            b.push_back(0x61); b.push_back(k & 0xFF); b.push_back(k >> 8);
            n -= k;
            t += k;
        }
    }
    std::vector<uint8_t> build()
    {
        b.push_back(0x66);
        std::vector<uint8_t> hdr(0x80, 0);
        auto p32 = [&](size_t o, uint32_t v) { for (int i = 0; i < 4; i++) hdr[o + i] = (v >> (8 * i)) & 0xFF; };
        memcpy(hdr.data(), "Vgm ", 4);
        p32(4, (uint32_t)(hdr.size() + b.size() - 4));
        p32(8, 0x151);
        p32(0x0C, sn_clk);
        p32(0x18, (uint32_t)t);
        hdr[0x28] = fa & 0xFF; hdr[0x29] = (fa >> 8) & 0xFF;
        hdr[0x2A] = fb;
        p32(0x2C, ym_clk);
        p32(0x34, 0x80 - 0x34);
        hdr.insert(hdr.end(), b.begin(), b.end());
        return hdr;
    }
};

constexpr int RENDER_TL = 24;
static const double RENDER_GAIN = std::pow(10.0, -0.75 * RENDER_TL / 20.0);
constexpr int T_ON = SR;
static const int T_REL = (int)(0.4 * SR), T_GAP = (int)(0.12 * SR);

inline void fm_regs_for(double freq, double fs, int & blk_o, int & fnum_o)
{
    for (int blk = 0; blk < 8; blk++)
    {
        double fnum = freq * (double)(1 << (21 - blk)) / fs;
        if (fnum < 1200 || blk == 7)
        {
            fnum_o = (int)std::nearbyint(std::max(1.0, std::min(2047.0, fnum)));
            blk_o = blk;
            return;
        }
    }
    blk_o = 7; fnum_o = 2047;
}

inline void write_patch(VgmBuilder & b, const Patch & p, int blk, int fnum, int ch, int base_tl)
{
    int alg = p.b0 & 7, port = ch / 3, i = ch % 3;
    for (int o = 1; o <= 4; o++)
    {
        int off = i + op_off(o);
        const auto & vals = p.ops[o - 1];
        int tl = vals[1];
        if (is_carrier(alg, o)) tl = std::min(127, tl + base_tl);
        b.ym(port, 0x30 + off, vals[0]);
        b.ym(port, 0x40 + off, tl);
        b.ym(port, 0x50 + off, vals[2]);
        b.ym(port, 0x60 + off, vals[3]);
        b.ym(port, 0x70 + off, vals[4]);
        b.ym(port, 0x80 + off, vals[5]);
        b.ym(port, 0x90 + off, vals[6]);
    }
    b.ym(port, 0xB0 + i, p.b0);
    b.ym(port, 0xB4 + i, 0xC0 | p.b4);
    b.ym(0, 0x22, p.lfo);
    b.ym(port, 0xA4 + i, (blk << 3) | (fnum >> 8));
    b.ym(port, 0xA0 + i, fnum & 0xFF);
}

struct Seg { int64_t a; double f; };

struct RenderJobs
{
    VgmBuilder b;
    double fs;
    int64_t psg_a, psg_b, dac_a, dac_b;
    std::map<std::pair<int, int>, Seg> segs;
    explicit RenderJobs(const VGM & v)
        : b(v.ym_clk ? v.ym_clk : 7670453, v.sn_clk ? v.sn_clk : 3579545, v.sn_flag_a, v.sn_flag_b),
          fs((v.ym_clk ? v.ym_clk : 7670453) / 144.0)
    {
        b.sn(0x80 | 0x0E);
        b.sn(0x0F);
        b.sn(0x90 | 0);
        b.sn(0xFF);
        int64_t t0 = b.t;
        b.wait((int)(0.25 * SR));
        psg_a = t0 + (int)(0.05 * SR); psg_b = t0 + (int)(0.25 * SR);
        b.sn(0x90 | 15);
        b.wait((int)(0.1 * SR));
        b.ym(0, 0x2B, 0x80);
        t0 = b.t;
        for (int k = 0; k < 120; k++)
        {
            b.ym(0, 0x2A, k % 2 == 0 ? 0xFF : 0x00);
            b.wait(60);
        }
        dac_a = t0; dac_b = b.t;
        b.ym(0, 0x2A, 0x80);
        b.ym(0, 0x2B, 0x00);
        b.wait((int)(0.1 * SR));
    }
    void add_patch(std::pair<int, int> key, const Patch & patch, double freq)
    {
        int blk, fnum;
        fm_regs_for(freq, fs, blk, fnum);
        double f_s = fnum * fs / (double)(1 << (21 - blk));
        b.ym(0, 0x28, 0x00);
        for (int o = 1; o <= 4; o++) b.ym(0, 0x80 + op_off(o), 0x0F);
        b.wait(T_GAP);
        write_patch(b, patch, blk, fnum, 0, RENDER_TL);
        b.wait(8);
        b.ym(0, 0x28, 0xF0);
        int64_t t_on = b.t;
        b.wait(T_ON);
        b.ym(0, 0x28, 0x00);
        b.wait(T_REL);
        segs[key] = {t_on, f_s};
    }
};

// ── обработка сэмплов ──
inline std::vector<double> rms_windows(const double * x, size_t len, size_t w)
{
    size_t n = len / w;
    std::vector<double> out;
    if (n == 0)
    {
        std::vector<double> sq(len);
        for (size_t i = 0; i < len; i++) sq[i] = x[i] * x[i];
        out.push_back(len ? std::sqrt(np_sum(sq.data(), len) / len) : 0.0);
        return out;
    }
    std::vector<double> sq(w);
    for (size_t k = 0; k < n; k++)
    {
        for (size_t i = 0; i < w; i++) sq[i] = x[k * w + i] * x[k * w + i];
        out.push_back(std::sqrt(np_sum(sq.data(), w) / w));
    }
    return out;
}

struct LoopRes { bool ok = false; int s = 0, L = 0; double err = 0; };

inline LoopRes find_loop(const std::vector<double> & x, double P, int s0, double rms_ref)
{
    int N = (int)x.size();
    LoopRes res;
    int Lmax = std::min((int)(0.5 * SR), (N - s0) / 2);
    if (Lmax < 48) return res;
    std::vector<int> L_list;
    for (int m = 1;; m++)
    {
        double L0 = m * P;
        if (L0 > Lmax || m > 400) break;
        for (int L : {(int)std::floor(L0), (int)std::ceil(L0)})
            if (32 <= L && L <= Lmax && (L_list.empty() || L_list.back() != L) &&
                std::find(L_list.begin(), L_list.end(), L) == L_list.end())
                L_list.push_back(L);
    }
    if (L_list.empty()) L_list.push_back(std::min(Lmax, std::max(32, (int)std::nearbyint(P))));
    int smax = std::min(N - 2 * 32, s0 + (int)(0.5 * SR));
    bool have = false;
    double best_err = 0;
    int best_s = 0, best_L = 0;
    std::vector<double> cs;
    for (int L : L_list)
    {
        int W = std::min(L, 1024);
        if (N - L - W <= s0) continue;
        int ne = N - L;
        cs.assign(ne + 1, 0.0);
        for (int i = 0; i < ne; i++) cs[i + 1] = cs[i] + std::fabs(x[L + i] - x[i]);
        int ma_len = ne + 1 - W;     // len(cs) - W
        int hi = std::min({ma_len, smax + 1, N - L - W + 1});
        if (hi <= s0) continue;
        int k = -1;
        double mv = 0;
        for (int s = s0; s < hi; s++)
        {
            double m = (cs[s + W] - cs[s]) / W;
            if (k < 0 || m < mv) { k = s - s0; mv = m; }
        }
        double err = mv / (rms_ref + 1e-9);
        if (!have || err < best_err) { have = true; best_err = err; best_s = s0 + k; best_L = L; }
        if (err < 0.02) break;
    }
    if (!have) return res;
    res.ok = true; res.s = best_s; res.L = best_L; res.err = best_err;
    return res;
}

struct FmSample
{
    std::vector<double> data;
    bool loop = false;
    int ls = 0, ll = 0;
    double peak = 1.0, rel = 0.4, mx = 1.0;
};

inline FmSample make_fm_sample(const std::vector<double> & xs, double f_s, const std::vector<double> & tail)
{
    FmSample out;
    double peak = 0;
    for (double v : xs) peak = std::max(peak, std::fabs(v));
    if (peak == 0) peak = 1.0;
    auto r = rms_windows(xs.data(), xs.size(), 441);
    double mx = *std::max_element(r.begin(), r.end());
    if (mx == 0) mx = 1.0;
    double final_;
    {
        std::vector<double> last8(r.end() - std::min<size_t>(8, r.size()), r.end());
        std::sort(last8.begin(), last8.end());
        size_t n = last8.size();
        final_ = n % 2 ? last8[n / 2] : 0.5 * (last8[n / 2 - 1] + last8[n / 2]);
    }
    double P = SR / f_s;
    double rel = 0.4;
    if (!tail.empty())
    {
        size_t m = std::min<size_t>(441, xs.size());
        std::vector<double> sq(m);
        for (size_t i = 0; i < m; i++) { double v = xs[xs.size() - m + i]; sq[i] = v * v; }
        double lvl0 = std::sqrt(np_sum(sq.data(), m) / m);
        if (lvl0 == 0) lvl0 = 1e-9;
        auto rt = rms_windows(tail.data(), tail.size(), 220);
        int idx = -1;
        for (size_t i = 0; i < rt.size(); i++) if (rt[i] < 0.25 * lvl0) { idx = (int)i; break; }
        rel = idx >= 0 ? (idx * 220 / (double)SR) / 0.75 : 0.4 / 0.75;
    }
    out.peak = peak; out.rel = rel; out.mx = mx;
    if (final_ < 0.03 * mx)
    {
        double thr = 0.01 * mx;
        auto r2 = rms_windows(xs.data(), xs.size(), 220);
        int last = -1;
        for (size_t i = 0; i < r2.size(); i++) if (r2[i] > thr) last = (int)i;
        int end = last >= 0 ? (last + 1) * 220 : (int)xs.size();
        end = std::max(end, 1024);
        end = std::min<int>(end, (int)xs.size());
        out.data.assign(xs.begin(), xs.begin() + end);
        int k = std::min(128, (int)out.data.size() / 4);
        // np.linspace(1, 0, k)
        double step = (0.0 - 1.0) / (k - 1);
        for (int i = 0; i < k; i++)
        {
            double w = (i == k - 1) ? 0.0 : i * step + 1.0;
            out.data[out.data.size() - k + i] *= w;
        }
        return out;
    }
    int s0 = (int)xs.size() / 2;
    int bad_last = -1;
    for (size_t i = 0; i < r.size(); i++)
        if (!(r[i] > 0.8 * final_ && r[i] < 1.25 * final_)) bad_last = (int)i;
    s0 = bad_last >= 0 ? (bad_last + 1) * 441 : 0;
    if (s0 >= (int)xs.size() / 2) s0 = (int)xs.size() / 2;
    auto res = find_loop(xs, P, s0, final_);
    if (!res.ok) { out.data = xs; return out; }
    out.data.assign(xs.begin(), xs.begin() + res.s + res.L);
    out.loop = true; out.ls = res.s; out.ll = res.L;
    return out;
}

inline std::vector<double> square_cycle(int c)
{
    std::vector<double> v(c);
    for (int i = 0; i < c; i++) v[i] = i < c / 2 ? 1.0 : -1.0;
    return v;
}

inline std::vector<double> noise_sample(int fb, double rate_shift, double seconds)
{
    int nshift = (int)(seconds * rate_shift) + 2;
    int reg = 0x8000;
    std::vector<double> bits(nshift);
    for (int i = 0; i < nshift; i++)
    {
        bits[i] = (reg & 1) ? 1.0 : -1.0;
        int fbk = fb ? (__builtin_popcount(reg & 0x0009) & 1) : (reg & 1);
        reg = (reg >> 1) | (fbk << 15);
    }
    int n = (int)(seconds * SR);
    std::vector<double> out(n);
    for (int i = 0; i < n; i++)
    {
        int64_t idx = (int64_t)((double)i * rate_shift / SR);
        out[i] = bits[std::min<int64_t>(idx, nshift - 1)];
    }
    return out;
}

inline double detect_row_interval(std::vector<double> t, double & t0_out, double & coh_out, double max_row = 0.06)
{
    std::sort(t.begin(), t.end());
    t0_out = 0; coh_out = 0;
    if (t.size() < 4) return 0.05;
    auto score = [&](double d) { return std::abs(np_mean_expi(t, d)); };
    const double g0 = 0.02, gdelta = (g0 + 0.0002) - g0;
    int ng = (int)std::ceil((0.45 - 0.02) / 0.0002);
    std::vector<double> grid(ng), sc(ng);
    for (int i = 0; i < ng; i++) { grid[i] = g0 + i * gdelta; sc[i] = score(grid[i]); }
    double best = *std::max_element(sc.begin(), sc.end());
    if (best < 0.25) { coh_out = best; return 0.025; }
    int bi = -1;
    for (int i = 1; i < ng - 1; i++)
        if (sc[i] >= sc[i - 1] && sc[i] >= sc[i + 1] && sc[i] >= 0.9 * best)
            if (bi < 0 || grid[i] > grid[bi]) bi = i;
    double d0 = grid[bi];
    const double f0 = d0 - 0.001, fdelta = (f0 + 0.000002) - f0;
    int nf = (int)std::ceil(((d0 + 0.001) - f0) / 0.000002);
    double bd = d0, bv = -1;
    for (int i = 0; i < nf; i++)
    {
        double d = f0 + i * fdelta;
        double v = score(d);
        if (v > bv) { bv = v; bd = d; }
    }
    d0 = bd;
    int m = std::max(1, (int)std::ceil(d0 / max_row - 1e-9));
    double delta = d0 / m;
    std::complex<double> z = np_mean_expi(t, delta);
    t0_out = (std::atan2(z.imag(), z.real()) / (2 * M_PI)) * delta;
    coh_out = score(d0);
    return delta;
}

inline int tempo_schedule(int bpm, int speed, double true_delta, int nrows, std::map<int, int> & sched)
{
    const int rate = 48000, block = 16;
    auto row_dur = [&](int b) { return speed * (int)(rate * 2.5 / b) / (double)rate; };
    double ideal = 2.5 * speed / true_delta;
    if (!(32 <= ideal && ideal <= 255)) return bpm;
    int lo = std::max(32, (int)ideal - 3), hi = std::min(255, (int)ideal + 4);
    double t_xm = 0;
    int cur = -1, start = -1;
    for (int r = 0; r < nrows; r += block)
    {
        int n = std::min(block, nrows - r);
        double target = (r + n) * true_delta;
        int best = lo;
        double bv = 1e300;
        for (int b = lo; b <= hi; b++)
        {
            double v = std::fabs(t_xm + n * row_dur(b) - target);
            if (v < bv) { bv = v; best = b; }
        }
        if (cur >= 0 && best != cur &&
            std::fabs(t_xm + n * row_dur(cur) - target) - std::fabs(t_xm + n * row_dur(best) - target) < 0.0005)
            best = cur;
        if (cur < 0) start = best;
        else if (best != cur) sched[r] = best;
        cur = best;
        t_xm += n * row_dur(best);
    }
    return start;
}

inline void interval_to_bpm_speed(double delta, int & bpm_o, int & speed_o, double & real_o)
{
    bool have = false;
    double b_err = 0, b_real = 0;
    int b_sp = 0, b_bpm = 0;
    for (int speed = 1; speed <= 12; speed++)
    {
        int bpm = (int)std::nearbyint(2.5 * speed / delta);
        if (!(32 <= bpm && bpm <= 255)) continue;
        double real = 2.5 * speed / bpm;
        double err = std::nearbyint(std::fabs(real - delta) / delta * 10000.0) / 10000.0;
        if (!have || err < b_err || (err == b_err && std::abs(speed - 6) < std::abs(b_sp - 6)))
        { have = true; b_err = err; b_sp = speed; b_bpm = bpm; b_real = real; }
    }
    if (!have)
    {
        int speed = delta < 0.05 ? 1 : 12;
        bpm_o = std::max(32, std::min(255, (int)std::nearbyint(2.5 * speed / delta)));
        speed_o = speed;
        real_o = 2.5 * speed / bpm_o;
        return;
    }
    bpm_o = b_bpm; speed_o = b_sp; real_o = b_real;
}

struct Cell { int note = 0, inst = 0, vol = 0, eff = 0, par = 0; };

inline double note_of(double f, double off) { return 69.0 + 12.0 * std::log2(f / 440.0) - off - 11.0; }
inline double freq_of(double n, double off) { return 440.0 * std::pow(2.0, (n + 11.0 - 69.0 + off) / 12.0); }

struct Zone
{
    int lo, hi, n0;
    std::vector<double> data;
    bool has_loop = false;
    int ls = 0, ll = 0;
    double f_s = 0, peak = 1.0, rate = SR;
    int cell0 = 0;
    Zone(int l, int h, int n) : lo(l), hi(h), n0(n) {}
};

// ключ инструмента
struct Key
{
    int kind = 0;
    Patch patch;
    int a = 0, b = 0, c = 0;
    bool operator<(const Key & o) const { return std::tie(kind, patch, a, b, c) < std::tie(o.kind, o.patch, o.a, o.b, o.c); }
    bool operator==(const Key & o) const { return !(*this < o) && !(o < *this); }
};

struct Instrument
{
    int kind;
    Key key;
    std::vector<Zone> zones;
    double rel_s = 0, bright = 0, centroid = 0;
    std::vector<Note *> notes;
    bool perc = false;
    int fold = 0, index = 0, fadeout = 0;
    int zone_for(int n) const
    {
        for (size_t i = 0; i < zones.size(); i++) if (zones[i].lo <= n && n <= zones[i].hi) return (int)i;
        int best = 0, bd = 1 << 30;
        for (size_t i = 0; i < zones.size(); i++)
        {
            int d = std::min(std::abs(n - zones[i].lo), std::abs(n - zones[i].hi));
            if (d < bd) { bd = d; best = (int)i; }
        }
        return best;
    }
};

struct Buf
{
    std::vector<uint8_t> d;
    void u8(int v) { d.push_back((uint8_t)v); }
    void le16(int v) { u8(v & 0xFF); u8((v >> 8) & 0xFF); }
    void le32(uint32_t v) { le16(v & 0xFFFF); le16(v >> 16); }
    void be16(int v) { u8((v >> 8) & 0xFF); u8(v & 0xFF); }
    void be32(uint32_t v) { be16(v >> 16); be16(v & 0xFFFF); }
    void str(const std::string & s, size_t n, char pad)
    {
        for (size_t i = 0; i < n; i++) u8(i < s.size() ? ((unsigned char)s[i] < 128 ? s[i] : '?') : pad);
    }
    void raw(const std::vector<uint8_t> & v) { d.insert(d.end(), v.begin(), v.end()); }
    bool save(const std::string & fn) const
    {
        FILE * f = fopen(fn.c_str(), "wb");
        if (!f) return false;
        bool ok = fwrite(d.data(), 1, d.size(), f) == d.size();
        return fclose(f) == 0 && ok;
    }
};
inline void vlq(Buf & b, int v)
{
    v = std::max(0, v);
    std::vector<uint8_t> r = {(uint8_t)(v & 0x7F)};
    v >>= 7;
    while (v) { r.push_back((uint8_t)((v & 0x7F) | 0x80)); v >>= 7; }
    std::reverse(r.begin(), r.end());
    b.raw(r);
}

inline int allocate_channels(std::vector<Note *> & notes, int max_channels)
{
    std::vector<int> busy_until, last_inst, started;
    std::vector<Note *> owner;
    for (Note * n : notes)
    {
        std::vector<int> fr;
        for (int c = 0; c < (int)busy_until.size(); c++) if (busy_until[c] <= n->start_row) fr.push_back(c);
        std::vector<int> same;
        for (int c : fr) if (last_inst[c] == n->inst) same.push_back(c);
        int c;
        if (!same.empty())
        {
            c = same[0];
            for (int s : same) if (busy_until[s] > busy_until[c]) c = s;
        }
        else if (!fr.empty()) c = *std::min_element(fr.begin(), fr.end());
        else if ((int)busy_until.size() < max_channels)
        {
            busy_until.push_back(0); last_inst.push_back(-1); started.push_back(0); owner.push_back(nullptr);
            c = (int)busy_until.size() - 1;
        }
        else
        {
            c = 0;
            for (int i = 1; i < (int)busy_until.size(); i++) if (started[i] < started[c]) c = i;
            Note * prev = owner[c];
            if (prev) prev->end_row = std::max(prev->start_row + 1, std::min(prev->end_row, n->start_row));
        }
        n->channel = c;
        busy_until[c] = n->end_row + n->tail_rows;
        last_inst[c] = n->inst;
        started[c] = n->start_row;
        owner[c] = n;
    }
    return (int)busy_until.size();
}

inline double np_median(std::vector<double> v)
{
    std::sort(v.begin(), v.end());
    size_t n = v.size();
    return n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
}

inline double np_percentile(std::vector<double> v, double q)
{
    std::sort(v.begin(), v.end());
    double pos = (v.size() - 1) * (q / 100.0);
    size_t lo = (size_t)std::floor(pos), hi = std::min(v.size() - 1, lo + 1);
    double t = pos - (double)lo, a = v[lo], b = v[hi], diff = b - a;
    double r = a + diff * t;
    if (t >= 0.5) r = b - diff * (1 - t);
    return r;
}

struct XmSample { std::vector<int16_t> data; bool loop; int ls, ll, rel, fine; };
struct XmInst { std::string name; std::vector<XmSample> samples; std::array<uint8_t, 96> keymap; int fadeout; };

inline std::string convert(const VGM & v, const std::string & base, const std::string & title, bool want_xm,
                           bool want_midi, const Options & o, const Renderer & render)
{
    std::vector<Ev> events = v.events;
    int64_t end_t = v.end_t;
    Tracker tr(v);
    for (auto & e : events) tr.feed(e.t, e.op, e.a, e.b);
    tr.finish(end_t);
    std::vector<Note *> notes = tr.out;
    if (notes.empty()) return "no notes found";
    for (Note * n : notes)
    {
        n->t0 = (double)n->start / SR;
        n->t1 = (double)std::max<int64_t>(n->end, n->start + 1) / SR;
        n->times.clear();
        for (auto & c : n->cur) n->times.push_back((double)c.t / SR);
    }

    // сдвиг сетки полутонов
    double off = 0.0;
    {
        std::vector<double> buf, wts;
        for (Note * n : notes)
            if ((n->kind == K_FM || n->kind == K_PSG) && !n->cur.empty() && n->cur[0].f > 0)
            {
                double p = 12.0 * std::log2(n->cur[0].f / 440.0);
                double w = std::sqrt(std::max(0.05, n->t1 - n->t0));
                double ang = p * 2 * M_PI;
                buf.push_back(w * std::cos(ang));
                buf.push_back(w * std::sin(ang));
                wts.push_back(w);
            }
        if (!wts.empty())
        {
            double rr, ri;
            pw_sum_c(buf.data(), buf.size(), rr, ri);
            double ws = np_sum(wts.data(), wts.size());
            std::complex<double> z(rr / ws, ri / ws);
            if (std::abs(z) > 0.3) off = std::atan2(z.imag(), z.real()) / (2 * M_PI);
        }
    }
    int shift = o.transpose + 12 * o.octave;

    // патчи
    std::vector<Patch> patches;
    std::map<Patch, int> patch_use;
    for (Note * n : notes) if (n->kind == K_FM) { if (!patch_use.count(n->patch)) patches.push_back(n->patch); patch_use[n->patch]++; }
    std::stable_sort(patches.begin(), patches.end(), [&](const Patch & a, const Patch & b) { return patch_use[a] > patch_use[b]; });
    if ((int)patches.size() > MAX_XM_INSTRUMENTS - 6)
    {
        std::set<Patch> keep(patches.begin(), patches.begin() + (MAX_XM_INSTRUMENTS - 6));
        auto dist = [](const Patch & a, const Patch & b) {
            int s = 0;
            for (int i = 0; i < 4; i++) for (int k = 0; k < 7; k++) s += std::abs(a.ops[i][k] - b.ops[i][k]);
            return s + std::abs(a.b0 - b.b0);
        };
        std::map<Patch, Patch> flat;
        for (auto & p : keep) flat[p] = p;
        for (auto & p : patches)
            if (!keep.count(p))
            {
                const Patch * best = nullptr;
                int bd = 1 << 30;
                for (auto & q : keep) { int d = dist(p, q); if (d < bd) { bd = d; best = &q; } }
                flat[p] = *best;
            }
        for (Note * n : notes) if (n->kind == K_FM) n->patch = flat[n->patch];
        std::vector<Patch> np;
        for (auto & p : patches) if (keep.count(p)) np.push_back(p);
        patches = np;
    }

    // ключи инструментов
    std::vector<Key> key_order;
    std::map<Key, double> first_t;
    std::map<Note *, Key> note_key;
    for (Note * n : notes)
    {
        Key k;
        k.kind = n->kind;
        if (n->kind == K_FM) k.patch = n->patch;
        else if (n->kind == K_NOISE) { k.a = n->nfb; k.b = n->nnf; }
        else if (n->kind == K_DAC)
        {
            uint32_t h = 2166136261u;
            for (uint8_t c : n->dac) { h ^= c; h *= 16777619u; }
            k.a = (int)h; k.b = (int)std::nearbyint(n->dac_rate);
        }
        if (!first_t.count(k)) { first_t[k] = n->t0; key_order.push_back(k); }
        note_key[n] = k;
    }
    std::stable_sort(key_order.begin(), key_order.end(), [&](const Key & a, const Key & b) { return first_t[a] < first_t[b]; });

    std::vector<Instrument> insts;
    std::map<Key, Instrument *> inst_of;
    for (auto & k : key_order)
    {
        Instrument ins; ins.kind = k.kind; ins.key = k;
        for (Note * n : notes) if (note_key[n] == k) ins.notes.push_back(n);
        insts.push_back(std::move(ins));
    }
    for (auto & i : insts) inst_of[i.key] = &i;
    for (Instrument & ins : insts)
        if (ins.kind == K_FM)
        {
            std::vector<int> ns;
            for (Note * n : ins.notes)
                for (auto & c : n->cur)
                    if (c.f > 0) ns.push_back((int)std::nearbyint(note_of(c.f, off)));
            std::sort(ns.begin(), ns.end());
            for (int & x : ns) x = std::min(96, std::max(1, x));
            if (ns.empty()) ns.push_back(49);
            std::vector<Zone> zones;
            int lo = ns.front();
            int zw = std::max(1, o.zone_width);
            while (lo <= ns.back())
            {
                int hi = lo + zw - 1;
                int fi = -1, la = -1;
                for (int x : ns) if (lo <= x && x <= hi) { if (fi < 0) fi = x; la = x; }
                if (fi >= 0) zones.emplace_back(lo, hi, (int)std::nearbyint((fi + la) / 2.0));
                lo = hi + 1;
                if (zones.size() >= 16) break;
            }
            if (zones.size() >= 16 && zones.back().hi < ns.back()) zones.back().hi = 96;
            zones.front().lo = 1;
            zones.back().hi = 96;
            ins.zones = std::move(zones);
        }

    // ── рендер FM-патчей ──
    RenderJobs jobs(v);
    struct ZRef { Instrument * ins; size_t zi; std::pair<int, int> key; };
    std::vector<ZRef> zone_list;
    for (size_t ii = 0; ii < insts.size(); ii++)
    {
        Instrument & ins = insts[ii];
        if (ins.kind != K_FM) continue;
        for (size_t zi = 0; zi < ins.zones.size(); zi++)
        {
            std::pair<int, int> key = {(int)ii, (int)zi};
            jobs.add_patch(key, ins.key.patch, freq_of(ins.zones[zi].n0, off));
            zone_list.push_back({&ins, zi, key});
        }
    }
    std::vector<double> mono;
    if (!render(jobs.b.build(), mono)) return "rendering of FM patches failed (libvgm)";
    auto peak_of = [&](int64_t a, int64_t b, double dflt) {
        double p = 0;
        for (int64_t i = a; i < b && i < (int64_t)mono.size(); i++) p = std::max(p, std::fabs(mono[i]));
        return p ? p : dflt;
    };
    double psg_peak = peak_of(jobs.psg_a, jobs.psg_b, 8000.0);
    double dac_peak = peak_of(jobs.dac_a, jobs.dac_b, 8000.0);
    double dac_scale = dac_peak / 32512.0;

    for (auto & zr : zone_list)
    {
        Seg sg = jobs.segs[zr.key];
        int64_t t_on = sg.a;
        std::vector<double> xs, tail;
        for (int64_t i = 0; i < T_ON; i++) xs.push_back(t_on + i < (int64_t)mono.size() ? mono[t_on + i] : 0.0);
        if (t_on + T_ON < (int64_t)mono.size())
            for (int64_t i = t_on + T_ON; i < t_on + T_ON + T_REL && i < (int64_t)mono.size(); i++) tail.push_back(mono[i]);
        // длина xs может быть меньше T_ON, если рендер короче
        if (t_on + T_ON > (int64_t)mono.size()) xs.resize(std::max<int64_t>(0, (int64_t)mono.size() - t_on));
        auto fs_ = make_fm_sample(xs, sg.f, tail);
        Zone & z = zr.ins->zones[zr.zi];
        z.data = fs_.data;
        z.has_loop = fs_.loop; z.ls = fs_.ls; z.ll = fs_.ll;
        z.f_s = sg.f;
        z.peak = fs_.peak / RENDER_GAIN;
        if (zr.zi == 0)
        {
            zr.ins->rel_s = fs_.rel;
            zr.ins->perc = !fs_.loop;
            zr.ins->centroid = spectral_centroid(fs_.data, SR);
            zr.ins->bright = zr.ins->centroid / sg.f;
        }
    }

    // PSG / шум / DAC
    for (Instrument & ins : insts)
    {
        if (ins.kind == K_PSG)
        {
            static const int zs[3][4] = {{1, 47, 36, 128}, {48, 71, 60, 64}, {72, 96, 84, 32}};
            for (auto & q : zs)
            {
                Zone z(q[0], q[1], q[2]);
                z.data = square_cycle(q[3]);
                z.has_loop = true; z.ls = 0; z.ll = q[3];
                z.f_s = (double)SR / q[3];
                z.peak = psg_peak;
                ins.zones.push_back(z);
            }
        }
        else if (ins.kind == K_NOISE)
        {
            int fb = ins.key.a, nf = ins.key.b;
            static const int ns_[4] = {16, 32, 64, 32};
            double shift_rate = v.sn_clk / (32.0 * ns_[nf]);
            Zone z(1, 96, 0);
            z.data = noise_sample(fb, shift_rate, fb == 0 ? 1.0 : 2.0);
            z.has_loop = true; z.ls = 0; z.ll = (int)z.data.size();
            z.f_s = shift_rate;
            z.peak = psg_peak;
            z.n0 = (int)std::nearbyint(std::min(96.0, std::max(1.0, note_of(shift_rate, off))));
            ins.zones.push_back(z);
        }
        else if (ins.kind == K_DAC)
        {
            Note * n0 = ins.notes[0];
            std::vector<double> pcm;
            for (uint8_t c : n0->dac) pcm.push_back(((double)c - 128.0) * 256.0);
            Zone z(1, 96, 49);
            z.data = pcm;
            z.rate = n0->dac_rate; z.f_s = n0->dac_rate;
            double mxp = 0;
            for (double x : pcm) mxp = std::max(mxp, std::fabs(x));
            z.peak = (mxp ? mxp : 1.0) * dac_scale;
            ins.zones.push_back(z);
            ins.perc = true;
            ins.centroid = spectral_centroid(pcm, n0->dac_rate);
        }
    }

    // уровни
    double norm;
    {
        std::vector<double> peaks;
        for (Note * n : notes)
        {
            Instrument * ins = inst_of[note_key[n]];
            const Zone & z = ins->zones[ins->zone_for(n->cur[0].f > 0 ? (int)std::nearbyint(note_of(n->cur[0].f, off)) : 49)];
            double mxl = 0;
            for (auto & c : n->cur) mxl = std::max(mxl, c.lv);
            peaks.push_back(mxl * z.peak);
        }
        double p = np_percentile(peaks, 99);
        norm = (p ? p : 1.0) * o.norm_scale;
    }

    // ── сетка строк ──
    double t0g = 0.0, true_delta, coh = 0;
    {
        std::vector<double> s_on;
        for (Note * n : notes) s_on.push_back((double)n->start);
        std::sort(s_on.begin(), s_on.end());
        bool have_frame = false;
        double fr_P = 0, fr_abs = 0, fr_ph = 0;
        for (int P : {735, 882})
        {
            auto z = np_mean_expi(s_on, P);
            double a = std::abs(z);
            if (a >= 0.6 && (!have_frame || a > fr_abs))
            {
                have_frame = true; fr_P = P; fr_abs = a;
                fr_ph = std::atan2(z.imag(), z.real()) / (2 * M_PI) * P;
            }
        }
        if (have_frame && !o.no_frame_grid) { true_delta = fr_P / SR; t0g = fr_ph / SR; coh = fr_abs; }
        else
        {
            std::vector<double> onsets;
            for (Note * n : notes) onsets.push_back(n->t0);
            true_delta = detect_row_interval(onsets, t0g, coh);
        }
    }
    true_delta *= o.compact;
    int bpm, speed;
    double delta;
    interval_to_bpm_speed(true_delta, bpm, speed, delta);
    double tick = 2.5 / bpm;
    for (Note * n : notes)
    {
        n->start_row = (int)std::nearbyint((n->t0 - t0g) / true_delta);
        n->end_row = std::max(n->start_row + 1, (int)std::nearbyint((n->t1 - t0g) / true_delta));
    }
    int sh = (*std::min_element(notes.begin(), notes.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; }))->start_row;
    for (Note * n : notes) { n->start_row -= sh; n->end_row -= sh; }
    double row_t0 = t0g + sh * true_delta;

    // fold и fadeout
    for (Instrument & ins : insts)
    {
        if (ins.kind == K_FM || ins.kind == K_PSG)
        {
            bool any = false;
            double lo = 0, hi = 0;
            for (Note * n : ins.notes)
                for (auto & c : n->cur)
                    if (c.f > 0)
                    {
                        double x = note_of(c.f, off) + shift;
                        if (!any) { lo = hi = x; any = true; }
                        lo = std::min(lo, x); hi = std::max(hi, x);
                    }
            if (!any) { lo = hi = 49; }
            int fold = 0;
            if (hi + 0.5 > 96) fold = -(int)std::ceil((hi + 0.5 - 96) / 12.0);
            else if (lo - 0.5 < 1) fold = (int)std::ceil((1 - (lo - 0.5)) / 12.0);
            ins.fold = fold;
        }
        double fade_s = ins.rel_s;
        if (ins.kind == K_FM && fade_s > 6 * tick)
            ins.fadeout = (int)std::min(4095.0, std::max(1.0, std::nearbyint(32768.0 * tick / fade_s)));
        else ins.fadeout = 0;
    }
    for (Note * n : notes)
    {
        Instrument * ins = inst_of[note_key[n]];
        n->tail_rows = 0;
        if (ins->fadeout) n->tail_rows = std::min(16, (int)std::ceil(ins->rel_s / delta));
    }
    for (size_t i = 0; i < insts.size(); i++) insts[i].index = (int)i + 1;
    for (Note * n : notes) n->inst = inst_of[note_key[n]]->index;
    if ((int)insts.size() > MAX_XM_INSTRUMENTS) return "too many instruments (" + std::to_string(insts.size()) + " > 128)";

    static const char * names[4] = {"FM", "PSG square", "PSG noise", "DAC"};
    std::vector<XmInst> xm_insts;
    for (Instrument & ins : insts)
    {
        XmInst xi;
        xi.keymap.fill(1);
        xi.fadeout = ins.fadeout;
        for (size_t zi = 0; zi < ins.zones.size(); zi++)
        {
            Zone & z = ins.zones[zi];
            double pk = 0;
            for (double x : z.data) pk = std::max(pk, std::fabs(x));
            if (pk == 0) pk = 1.0;
            XmSample s;
            for (double x : z.data) s.data.push_back((int16_t)std::max(-32768.0, std::min(32767.0, std::nearbyint(x * (32767.0 / pk)))));
            z.cell0 = z.n0 + shift + 12 * ins.fold;
            double nominal = freq_of(z.n0, off), tune;
            if (ins.kind == K_DAC) { tune = 12.0 * std::log2(z.rate / XM_C4_RATE); z.cell0 = 49; }
            else
            {
                double play_rate = SR * nominal / z.f_s;
                tune = 12.0 * std::log2(play_rate / XM_C4_RATE) - (z.cell0 - 49);
            }
            int rel_ = (int)std::floor(tune);
            int fine = (int)std::nearbyint((tune - rel_) * 128);
            if (fine > 127) { rel_ += 1; fine -= 128; }
            rel_ = std::max(-96, std::min(95, rel_));
            s.rel = rel_; s.fine = std::max(-128, std::min(127, fine));
            s.loop = z.has_loop; s.ls = z.ls; s.ll = z.ll;
            xi.samples.push_back(std::move(s));
            for (int cell = 1; cell <= 96; cell++)
            {
                int n_real = cell - shift - 12 * ins.fold;
                if (z.lo <= n_real && n_real <= z.hi) xi.keymap[cell - 1] = (uint8_t)zi;
            }
        }
        for (int cell = 1; cell <= 96; cell++)
        {
            int n_real = cell - shift - 12 * ins.fold;
            bool in = false;
            for (auto & z : ins.zones) if (z.lo <= n_real && n_real <= z.hi) in = true;
            if (!in) xi.keymap[cell - 1] = (uint8_t)ins.zone_for(n_real);
        }
        char nb[64];
        snprintf(nb, sizeof nb, "%02d %s", ins.index, names[ins.kind]);
        xi.name = std::string(nb).substr(0, 22);
        xm_insts.push_back(std::move(xi));
    }

    std::vector<Note *> alloc_order = notes;
    int nch = allocate_channels(alloc_order, std::max(2, std::min(MAX_XM_CHANNELS, o.channels)));
    nch = std::max(2, nch + (nch & 1));
    nch = std::min(nch, MAX_XM_CHANNELS);

    auto cell_note = [&](Note * n, const Instrument & ins, double freq) {
        (void)n;
        if (ins.kind == K_DAC) return 49.0;
        return note_of(freq, off) + shift + 12 * ins.fold;
    };

    if (want_xm)
    {
        std::map<std::pair<int, int>, Cell> grid;
        int last_row = 0;
        std::map<int, std::vector<Note *>> by_chan;
        for (Note * n : notes) by_chan[n->channel].push_back(n);
        for (auto & kv : by_chan)
        {
            int ch = kv.first;
            auto & lst = kv.second;
            std::stable_sort(lst.begin(), lst.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; });
            for (size_t k = 0; k < lst.size(); k++)
            {
                Note * n = lst[k];
                Instrument & ins = *inst_of[note_key[n]];
                bool has_next = k + 1 < lst.size();
                int nxt = has_next ? lst[k + 1]->start_row : 0;
                int end_row = !has_next ? n->end_row : std::min(n->end_row, nxt);
                n->end_row = end_row;
                last_row = std::max(last_row, end_row);
                const Zone & z0 = ins.zones[ins.zone_for(n->cur[0].f > 0 ? (int)std::nearbyint(note_of(n->cur[0].f, off)) : 49)];
                auto put = [&](int row) -> Cell & { return grid[{row, ch}]; };
                auto vol_of = [&](double lv, const Zone & z) {
                    return lv > 0 ? std::max(1, std::min(64, (int)std::nearbyint(lv * z.peak / norm * 64.0))) : 0;
                };
                Cell & c0 = put(n->start_row);
                double f0 = n->cur[0].f;
                double x0 = f0 > 0 ? cell_note(n, ins, f0) : 49;
                int cur_cell = std::max(1, std::min(96, (int)std::nearbyint(x0)));
                c0.note = cur_cell;
                c0.inst = n->inst;
                int last_vol = vol_of(n->cur[0].lv, z0);
                if (ins.kind == K_DAC) last_vol = std::max(1, std::min(64, (int)std::nearbyint(z0.peak / norm * 64.0)));
                c0.vol = 0x10 + last_vol;
                int last_pan = n->cur[0].pan;
                if (!o.no_pan && last_pan != 128) { c0.eff = 0x08; c0.par = std::max(1, std::min(255, last_pan)); }
                double last_pitch = x0;
                if (ins.kind != K_DAC)
                    for (int row = n->start_row + 1; row < end_row; row++)
                    {
                        double t = row_t0 + row * true_delta;
                        int i = std::max(0, (int)(std::upper_bound(n->times.begin(), n->times.end(), t) - n->times.begin()) - 1);
                        double f = n->cur[i].f, lv = n->cur[i].lv;
                        int pan = n->cur[i].pan;
                        Cell * c = nullptr;
                        int vv = vol_of(lv, z0);
                        if (vv != last_vol && std::abs(vv - last_vol) >= std::max(1, (int)(0.06 * last_vol)))
                        {
                            c = &put(row);
                            c->vol = 0x10 + vv;
                            last_vol = vv;
                        }
                        if (f > 0)
                        {
                            double pf = cell_note(n, ins, f);
                            if (std::fabs(pf - last_pitch) >= o.bend_threshold)
                            {
                                int nc = std::max(1, std::min(96, (int)std::nearbyint(pf)));
                                if (nc != cur_cell)
                                {
                                    if (!c) c = &put(row);
                                    c->note = nc;
                                    c->eff = 0x03; c->par = 0xFF;
                                    cur_cell = nc;
                                }
                                last_pitch = pf;
                            }
                        }
                        if (!o.no_pan && std::abs(pan - last_pan) >= 40)
                        {
                            if (!c) c = &put(row);
                            if (!c->eff) { c->eff = 0x08; c->par = std::max(1, std::min(255, pan)); last_pan = pan; }
                        }
                    }
                if (!has_next || end_row < nxt)
                {
                    Cell & oc = put(end_row);
                    if (!oc.note)
                    {
                        if (ins.fadeout) oc.note = 97;
                        else if (ins.kind != K_DAC) oc.vol = 0x10;
                    }
                }
            }
        }
        if (last_row + 1 > 256 * 64) last_row = 256 * 64 - 1;
        std::map<int, int> sched;
        int start_bpm = tempo_schedule(bpm, speed, true_delta, last_row + 2, sched);
        if (start_bpm < 0) start_bpm = bpm;
        for (auto & kv : sched)
            for (int chn = 0; chn < nch; chn++)
            {
                Cell & c = grid[{kv.first, chn}];
                if (!c.eff) { c.eff = 0x0F; c.par = kv.second; break; }
            }
        int nrows = last_row + 2;
        const int rpp = 64;
        int npat = std::max(1, (nrows + rpp - 1) / rpp);
        if (npat > 256) npat = 256;
        Buf b;
        b.str("Extended Module: ", 17, ' ');
        b.str(title, 20, ' ');
        b.u8(0x1a);
        b.str("VGM2XM Converter    ", 20, ' ');
        b.le16(0x0104); b.le32(276);
        b.le16(npat); b.le16(0); b.le16(nch); b.le16(npat); b.le16((int)xm_insts.size()); b.le16(1);
        b.le16(speed); b.le16(start_bpm);
        for (int i = 0; i < 256; i++) b.u8(i < npat ? i : 0);
        for (int p = 0; p < npat; p++)
        {
            Buf pk;
            for (int r = 0; r < rpp; r++)
                for (int c = 0; c < nch; c++)
                {
                    auto it = grid.find({p * rpp + r, c});
                    Cell cl = it != grid.end() ? it->second : Cell();
                    if (!(cl.note || cl.inst || cl.vol || cl.eff || cl.par)) { pk.u8(0x80); continue; }
                    int flag = 0x80;
                    if (cl.note) flag |= 1;
                    if (cl.inst) flag |= 2;
                    if (cl.vol) flag |= 4;
                    if (cl.eff) flag |= 8;
                    if (cl.par) flag |= 16;
                    pk.u8(flag);
                    if (cl.note) pk.u8(cl.note);
                    if (cl.inst) pk.u8(cl.inst);
                    if (cl.vol) pk.u8(cl.vol);
                    if (cl.eff) pk.u8(cl.eff);
                    if (cl.par) pk.u8(cl.par);
                }
            b.le32(9); b.u8(0); b.le16(rpp); b.le16((int)pk.d.size()); b.raw(pk.d);
        }
        for (auto & xi : xm_insts)
        {
            const int ihs = 263;
            b.le32(ihs); b.str(xi.name, 22, 0);
            b.u8(0); b.le16((int)xi.samples.size());
            if (xi.samples.empty()) continue;
            b.le32(40);
            for (int i = 0; i < 96; i++) b.u8(xi.keymap[i]);
            { uint8_t ve[48] = {0}; ve[2] = 64; ve[4] = 1000 & 0xFF; ve[5] = 1000 >> 8; ve[6] = 64; b.raw({ve, ve + 48}); }
            { uint8_t pe[48] = {0}; pe[2] = 32; pe[4] = 1000 & 0xFF; pe[5] = 1000 >> 8; pe[6] = 32; b.raw({pe, pe + 48}); }
            b.raw({2, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0});
            b.le16(xi.fadeout);
            for (int i = 0; i < ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2); i++) b.u8(0);
            for (auto & s : xi.samples)
            {
                int ls = s.loop ? s.ls : 0, ll = s.loop ? s.ll : 0;
                b.le32((uint32_t)s.data.size() * 2); b.le32(ls * 2); b.le32(ll * 2);
                b.u8(64); b.u8((uint8_t)(int8_t)s.fine); b.u8((s.loop ? 1 : 0) | 0x10); b.u8(128); b.u8((uint8_t)(int8_t)s.rel);
                b.u8(0);
                b.str(xi.name, 22, 0);
            }
            for (auto & s : xi.samples)
            {
                int prev = 0;
                for (int16_t x : s.data)
                {
                    int d = (((int)x - prev + 32768) % 65536 + 65536) % 65536 - 32768;
                    b.le16(d & 0xFFFF);
                    prev = x;
                }
            }
        }
        if (!b.save(base + ".xm")) return "cannot write " + base + ".xm";
    }

    if (want_midi)
    {
        struct ME { int tick, order, ch; std::vector<uint8_t> data; };
        std::vector<ME> ev;
        const double tps = 960.0;
        std::map<int, int> fm_progs;
        for (Instrument & ins : insts)
            if (ins.kind == K_FM)
            {
                std::vector<double> ms;
                for (Note * n : ins.notes) for (auto & c : n->cur) if (c.f > 0) ms.push_back(note_of(c.f, off));
                if (ms.empty()) ms.push_back(49);
                double med = np_median(ms);
                int prog;
                if (ins.perc) prog = med < 40 ? 38 : (med < 52 ? 36 : (ins.bright < 8 ? 11 : 12));
                else if (med < 40) prog = 39;
                else if (ins.bright < 2.5) prog = 89;
                else if (ins.bright < 6) prog = 80;
                else prog = 81;
                fm_progs[ins.index] = prog;
            }
        std::map<int, int> drums;
        std::vector<Instrument *> dac_ins;
        for (auto & i : insts) if (i.kind == K_DAC) dac_ins.push_back(&i);
        std::stable_sort(dac_ins.begin(), dac_ins.end(), [](Instrument * a, Instrument * b) { return a->centroid < b->centroid; });
        for (size_t k = 0; k < dac_ins.size(); k++)
            drums[dac_ins[k]->index] = DRUM_NOTES[std::min<size_t>(11, k * 12 / std::max<size_t>(1, dac_ins.size()))];
        static const int noise_drum[4] = {42, 38, 36, 40};
        std::set<int> init_done;
        std::map<int, int> ch_prog;
        std::vector<Note *> by_t = notes;
        std::stable_sort(by_t.begin(), by_t.end(), [](Note * a, Note * b) { return a->t0 < b->t0; });
        for (Note * n : by_t)
        {
            Instrument & ins = *inst_of[note_key[n]];
            int mch = n->voice <= 10 ? (n->voice == 10 ? 9 : n->voice) : 9;
            const Zone & z0 = ins.zones[ins.zone_for(n->cur[0].f > 0 ? (int)std::nearbyint(note_of(n->cur[0].f, off)) : 49)];
            int tick0 = (int)std::nearbyint(n->t0 * tps);
            int tick1 = std::max(tick0 + 20, (int)std::nearbyint(n->t1 * tps));
            int key;
            if (ins.kind == K_DAC || ins.kind == K_NOISE)
            {
                mch = 9;
                key = ins.kind == K_DAC ? drums[ins.index] : noise_drum[ins.key.b];
            }
            else
            {
                key = (int)std::nearbyint(note_of(n->cur[0].f, off) + 11 + shift);
                key = std::max(0, std::min(127, key));
            }
            if (!init_done.count(mch))
            {
                init_done.insert(mch);
                ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 101, 0}});
                ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 100, 0}});
                ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 6, 12}});
                ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 38, 0}});
            }
            if (mch != 9)
            {
                int prog = fm_progs.count(ins.index) ? fm_progs[ins.index] : 80;
                if (!ch_prog.count(mch) || ch_prog[mch] != prog)
                {
                    ev.push_back({tick0, 1, mch, {(uint8_t)(0xC0 | mch), (uint8_t)prog}});
                    ch_prog[mch] = prog;
                }
            }
            double maxlv = 0;
            for (auto & c : n->cur) maxlv = std::max(maxlv, c.lv);
            int vel = std::max(1, std::min(127, (int)std::nearbyint(maxlv * z0.peak / norm * 110)));
            ev.push_back({tick0, 2, mch, {(uint8_t)(0xB0 | mch), 10, (uint8_t)std::max(0, std::min(127, n->cur[0].pan / 2))}});
            ev.push_back({tick0, 3, mch, {(uint8_t)(0x90 | mch), (uint8_t)key, (uint8_t)vel}});
            if (mch != 9)
            {
                bool have_b = false, have_e = false;
                double last_b = 0;
                int last_e = 0;
                for (auto & c : n->cur)
                {
                    int tk = (int)std::nearbyint((double)c.t / SR * tps);
                    if (tk >= tick1) break;
                    if (c.f > 0)
                    {
                        double bend = std::max(-12.0, std::min(12.0, note_of(c.f, off) + 11 + shift - key));
                        if (!have_b || std::fabs(bend - last_b) >= 0.06)
                        {
                            int val = (int)(8192 + bend / 12.0 * 8191);
                            ev.push_back({tk, 2, mch, {(uint8_t)(0xE0 | mch), (uint8_t)(val & 0x7F), (uint8_t)((val >> 7) & 0x7F)}});
                            last_b = bend; have_b = true;
                        }
                    }
                    int e = std::max(1, std::min(127, (int)std::nearbyint(c.lv / std::max(maxlv, 1e-6) * 127)));
                    if (!have_e || std::abs(e - last_e) >= 3)
                    {
                        ev.push_back({tk, 2, mch, {(uint8_t)(0xB0 | mch), 11, (uint8_t)e}});
                        last_e = e; have_e = true;
                    }
                }
            }
            ev.push_back({tick1, 0, mch, {(uint8_t)(0x80 | mch), (uint8_t)key, 0}});
        }
        std::map<int, std::vector<ME>> tracks;
        for (auto & e : ev) tracks[e.ch].push_back(e);
        std::vector<std::vector<ME>> all;
        std::vector<ME> tempo;
        tempo.push_back({0, 0, 0, {0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20}});
        if (!title.empty())
        {
            std::string t;
            for (char ch : title.substr(0, 100)) t += (unsigned char)ch < 128 ? ch : '?';
            std::vector<uint8_t> d = {0xFF, 0x03, (uint8_t)t.size()};
            d.insert(d.end(), t.begin(), t.end());
            tempo.push_back({0, 1, 0, d});
        }
        all.push_back(tempo);
        for (auto & kv : tracks) all.push_back(kv.second);
        Buf f;
        f.str("MThd", 4, 0); f.be32(6); f.be16(1); f.be16((int)all.size()); f.be16(480);
        for (auto & t : all)
        {
            std::stable_sort(t.begin(), t.end(), [](const ME & a, const ME & b) { return std::tie(a.tick, a.order) < std::tie(b.tick, b.order); });
            Buf body;
            int prev = 0;
            for (auto & e : t) { vlq(body, e.tick - prev); body.raw(e.data); prev = e.tick; }
            vlq(body, 0); body.raw({0xFF, 0x2F, 0x00});
            f.str("MTrk", 4, 0); f.be32((uint32_t)body.d.size()); f.raw(body.d);
        }
        if (!f.save(base + ".mid")) return "cannot write " + base + ".mid";
    }
    return "";
}

inline std::string export_vgm(const std::string & path, const std::string & out_base, bool want_xm, bool want_midi,
                              const Options & o, const Renderer & render)
{
    FILE * f = fopen(path.c_str(), "rb");
    if (!f) return "cannot read file";
    std::vector<uint8_t> d;
    uint8_t buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) d.insert(d.end(), buf, buf + n);
    fclose(f);
    VGM v;
    if (!parse_vgm(std::move(d), v)) return "not a VGM file";
    std::string title = out_base.substr(out_base.rfind('/') + 1);
    return convert(v, out_base, title, want_xm, want_midi, o, render);
}

} // namespace vgmexp
