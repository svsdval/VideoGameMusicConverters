// Встроенный экспорт NSF -> XM / MIDI на C++ (порт nsf2xm-midi-wav.py).
// Результат эквивалентен скрипту (синтез сэмплов через прямое ДПФ вместо numpy.fft, фильтр - IIR).
#pragma once
#include <algorithm>
#include <complex>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "../audacious_common/xmloop.hpp"
#include "nsfemu.hpp"

namespace nsfexp {

using namespace nsfemu;

constexpr double XM_C4_RATE = 8363.0, XM_REF_AMP = 0.18;
constexpr int MAX_XM_CHANNELS = 32, MAX_XM_INSTRUMENTS = 128, MERGE_GAP = 200;
static const double DUTY_FRAC[4] = {0.125, 0.25, 0.5, 0.75};
static const char * DUTY_NAMES[4] = {"12.5%", "25%", "50%", "75%"};
static const int DRUM_NOTES[12] = {36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56};

struct Options
{
    int seconds = 120;
    double stop_silence = 5.0, tail = 1.0;
    int octave = 0, transpose = 0, compact = 2, channels = 32, zone_width = 12;
    double bend_threshold = 0.6, norm_scale = 1.0, limit_knee = 0.75;
};

enum Kind { K_PULSE, K_TRI, K_NOISE, K_DMC, K_DAC };

struct Key
{
    int kind = 0;
    int64_t p0 = 0, p1 = 0, p2 = 0;
    bool operator<(const Key & o) const { return std::tie(kind, p0, p1, p2) < std::tie(o.kind, o.p0, o.p1, o.p2); }
    bool operator==(const Key & o) const { return kind == o.kind && p0 == o.p0 && p1 == o.p1 && p2 == o.p2; }
};

struct Cur { double t, f, a; };

struct Note
{
    int kind, ch;
    double t0, t1;
    Key patch;                    // pulse: p0=скважность; noise: p0=режим; dmc/dac: ключ сэмпла
    std::vector<Cur> cur;
    std::vector<uint8_t> data;    // dmc/dac: байты
    int dlevel = 0;
    bool dloop = false;
    double drate = 0;
    int inst = 0;
    Key key;
    int start_row = 0, end_row = 0, channel = 0;
    Note(int kind, int ch, double t0, Key p) : kind(kind), ch(ch), t0(t0), t1(t0), patch(p) {}
};

inline double amp_of(int kind, int level, int patch)
{
    if (kind == K_PULSE)
    {
        double d = DUTY_FRAC[patch];
        return level * MIX_PULSE * std::max(d, 1.0 - d);
    }
    if (kind == K_TRI) return 15 * MIX_TRI * 0.5;
    return level * MIX_NOISE * 0.5;
}

inline std::vector<Ev> merge_events(const std::vector<Ev> & ev)
{
    std::vector<Ev> out;
    int64_t last_c = 0;
    bool have = false;
    for (const Ev & e : ev)
    {
        if (!out.empty() && e.cyc - last_c <= MERGE_GAP)
        {
            Ev & o = out.back();
            bool trig = o.trig || e.trig;
            int64_t first = o.cyc;
            o = e;
            o.cyc = first;
            o.trig = trig;
        }
        else out.push_back(e);
        last_c = e.cyc;
        have = true;
    }
    (void)have;
    return out;
}

inline std::vector<Note> channel_notes(const Trace & tr, int ch)
{
    int kind = ch < 2 ? K_PULSE : ch == 2 ? K_TRI : K_NOISE;
    double clock = tr.clock;
    std::vector<Note> notes;
    int cur = -1;
    for (const Ev & e : merge_events(tr.ev[ch]))
    {
        double t = std::max(0.0, (e.cyc - tr.start) / clock);
        double f;
        int patch;
        if (kind == K_PULSE) { f = clock / (16.0 * (e.a + 1)); patch = e.b; }
        else if (kind == K_TRI) { f = clock / (32.0 * (e.a + 1)); patch = 0; }
        else { f = clock / tr.noise_tab[e.a]; patch = e.b; }
        bool audible = e.lv > 0 && (kind == K_NOISE || (f >= 20.0 && f <= 20000.0));
        if (cur >= 0)
        {
            if (!audible) { notes[cur].t1 = t; cur = -1; continue; }
            if (!e.trig && patch == notes[cur].patch.p0)
            {
                notes[cur].cur.push_back({t, f, amp_of(kind, e.lv, patch)});
                continue;
            }
            notes[cur].t1 = t;
            cur = -1;
        }
        if (audible)
        {
            Key k; k.p0 = patch;
            notes.emplace_back(kind, ch, t, k);
            cur = (int)notes.size() - 1;
            notes[cur].cur.push_back({t, f, amp_of(kind, e.lv, patch)});
        }
    }
    double t_end = (tr.end - tr.start) / clock;
    if (cur >= 0) notes[cur].t1 = t_end;
    std::vector<Note> out;
    for (auto & n : notes) if (n.t1 > n.t0 || !n.cur.empty()) out.push_back(std::move(n));
    return out;
}

inline uint32_t fnv(const uint8_t * p, size_t n, uint32_t h = 2166136261u)
{
    for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

inline std::vector<Note> dmc_notes(const Trace & tr)
{
    double clock = tr.clock;
    std::vector<Note> out;
    const auto & evs = tr.dmc_events;
    for (size_t i = 0; i < evs.size(); i++)
    {
        const DmcEv & e = evs[i];
        if (!e.is_start || e.data.empty()) continue;
        int64_t c0 = e.cyc;
        int per = e.period;
        int64_t c1 = tr.end;
        for (size_t j = i + 1; j < evs.size(); j++) c1 = std::min(c1, evs[j].cyc);
        int64_t natural = c0 + (int64_t)e.data.size() * 8 * per;
        if (!e.loop) c1 = std::min(c1, natural);
        double t0 = std::max(0.0, (c0 - tr.start) / clock);
        double t1 = std::max(t0 + 0.001, (c1 - tr.start) / clock);
        Key k;
        k.p0 = fnv(e.data.data(), e.data.size());
        k.p1 = e.level; k.p2 = e.loop;
        Note n(K_DMC, 4, t0, k);
        n.t1 = t1;
        double rate = clock / (double)per;
        n.cur.push_back({t0, rate, 0.0});
        n.data = e.data; n.dlevel = e.level; n.dloop = e.loop; n.drate = rate;
        out.push_back(std::move(n));
    }
    return out;
}

inline std::vector<Note> dac_notes(const Trace & tr)
{
    const auto & w = tr.dac_writes;
    double clock = tr.clock;
    std::vector<Note> out;
    if (w.size() < 24) return out;
    int64_t gap = (int64_t)(clock * 0.004);
    std::vector<std::vector<DacW>> segs;
    segs.push_back({w[0]});
    for (size_t i = 1; i < w.size(); i++)
    {
        if (w[i].cyc - w[i - 1].cyc > gap) segs.push_back({});
        segs.back().push_back(w[i]);
    }
    for (auto & sg : segs)
    {
        if (sg.size() < 24) continue;
        std::vector<double> dts;
        for (size_t i = 1; i < sg.size(); i++) dts.push_back((double)(sg[i].cyc - sg[i - 1].cyc));
        std::sort(dts.begin(), dts.end());
        double dt = dts.size() % 2 ? dts[dts.size() / 2] : 0.5 * (dts[dts.size() / 2 - 1] + dts[dts.size() / 2]);
        std::vector<uint8_t> vals;
        for (auto & x : sg) vals.push_back((uint8_t)x.v);
        std::vector<uint8_t> uniq = vals;
        std::sort(uniq.begin(), uniq.end());
        size_t nu = std::unique(uniq.begin(), uniq.end()) - uniq.begin();
        double span = (double)(sg.back().cyc - sg.front().cyc);
        if (dt <= 0 || nu < 3 || span < clock * 0.01) continue;
        double t0 = std::max(0.0, (sg.front().cyc - tr.start) / clock);
        size_t N = vals.size();
        double mean = 0;
        for (auto v : vals) mean += v;
        mean /= N;
        std::vector<double> ac(N);
        double var = 0;
        for (size_t i = 0; i < N; i++) { ac[i] = vals[i] - mean; var += ac[i] * ac[i]; }
        int lag = 0;
        if (var > 0 && N > 32)
        {
            int best = -1;
            double bv = -1e300;
            for (size_t l = 8; l < N / 2; l++)
            {
                double s = 0;
                for (size_t i = 0; i + l < N; i++) s += ac[i] * ac[i + l];
                s /= var;
                if (s > bv) { bv = s; best = (int)l; }
            }
            if (best >= 0 && bv > 0.7) lag = best;
        }
        double stdv = std::sqrt(var / N);
        int64_t f0 = (int64_t)std::nearbyint(std::log2((double)N) * 3);
        int64_t f1 = (int64_t)std::nearbyint(std::log2(clock / dt) * 6);
        int64_t f2 = (int64_t)std::nearbyint(stdv / 3.0);
        Key k;
        k.p0 = (int64_t)((f0 * 1000003 + f1) * 1000003 + f2) * 1000003 + lag;
        Note n(K_DAC, 4, t0, k);
        n.t1 = (sg.back().cyc + dt - tr.start) / clock;
        double rate = clock / dt;
        n.cur.push_back({t0, rate, 0.0});
        n.data = vals; n.drate = rate;
        out.push_back(std::move(n));
    }
    return out;
}

// ── сэмплы ──
inline std::vector<double> stepwave(const std::vector<double> & v, int L)
{
    int M = (int)v.size(), K = L / 2;
    std::vector<std::complex<double>> c(K, 0.0);
    const double PI = M_PI;
    for (int k = 1; k < K; k++)
    {
        std::complex<double> s = 0;
        for (int m = 0; m < M; m++)
        {
            std::complex<double> e0 = std::polar(1.0, -2 * PI * k * m / M);
            std::complex<double> e1 = std::polar(1.0, -2 * PI * k * (m + 1) / M);
            s += (e0 - e1) * v[m];
        }
        c[k] = s / std::complex<double>(0, 2 * PI * k);
        double x = (double)k / K;
        c[k] *= std::sin(PI * x) / (PI * x);       // sinc(k/K)
    }
    std::vector<double> ct(L), st(L);
    for (int i = 0; i < L; i++) { ct[i] = std::cos(2 * PI * i / L); st[i] = std::sin(2 * PI * i / L); }
    std::vector<double> x(L, 0.0);
    for (int k = 1; k < K; k++)
        for (int n = 0; n < L; n++)
        {
            int idx = (int)(((int64_t)k * n) % L);
            x[n] += 2.0 * (c[k].real() * ct[idx] - c[k].imag() * st[idx]);
        }
    double pk = 0;
    for (double a : x) pk = std::max(pk, std::fabs(a));
    if (pk == 0) pk = 1.0;
    for (double & a : x) a /= pk;
    return x;
}

inline int pow2_at_least(double x, int lo = 16, int hi = 2048)
{
    int p = lo;
    while (p < x && p < hi) p *= 2;
    return p;
}

inline std::vector<double> highpass(const std::vector<double> & y, double sr, double tau = HP_TAU)
{
    double dt = 1.0 / sr, alpha = dt / (tau + dt);
    std::vector<double> out(y.size());
    double lp = 0;
    for (size_t i = 0; i < y.size(); i++)
    {
        lp = alpha * y[i] + (1 - alpha) * lp;
        out[i] = y[i] - lp;
    }
    return out;
}

inline std::vector<double> hp_sample(const std::vector<double> & x, double rate, bool loop)
{
    if (loop)
    {
        std::vector<double> t3;
        for (int i = 0; i < 3; i++) t3.insert(t3.end(), x.begin(), x.end());
        auto y = highpass(t3, rate);
        return std::vector<double>(y.begin() + x.size(), y.begin() + 2 * x.size());
    }
    std::vector<double> t = x;
    t.insert(t.end(), (size_t)(int)(rate * 0.02), 0.0);
    auto y = highpass(t, rate);
    y.resize(x.size());
    return y;
}

inline std::vector<double> lfsr_seq(int mode)
{
    int r = 1;
    std::vector<double> out;
    std::map<int, int> seen;
    while (!seen.count(r))
    {
        seen[r] = (int)out.size();
        out.push_back((r & 1) ? 0.0 : 1.0);
        int fb = (r & 1) ^ ((r >> (mode ? 6 : 1)) & 1);
        r = (r >> 1) | (fb << 14);
    }
    int start = seen[r];
    if (start) return std::vector<double>(out.begin() + start, out.end());
    return out;
}

inline std::vector<double> dmc_decode(const std::vector<uint8_t> & data, int level0, bool loop, size_t nbits)
{
    std::vector<uint8_t> bits;
    for (uint8_t b : data) for (int i = 0; i < 8; i++) bits.push_back((b >> i) & 1);
    if (loop && nbits > bits.size() && !bits.empty())
    {
        std::vector<uint8_t> t;
        size_t reps = (nbits + bits.size() - 1) / bits.size();
        for (size_t r = 0; r < reps; r++) t.insert(t.end(), bits.begin(), bits.end());
        bits = t;
    }
    if (bits.size() > nbits) bits.resize(nbits);
    std::vector<double> out(bits.size());
    int cur = level0;
    for (size_t i = 0; i < bits.size(); i++)
    {
        if (bits[i]) { if (cur <= 125) cur += 2; }
        else if (cur >= 2) cur -= 2;
        out[i] = cur;
    }
    return out;
}

// ── темп ──
inline int row_dur_i(int rate, double b) { return (int)(rate * 2.5 / b); }

inline int tempo_schedule(int bpm, int speed, double true_delta, int nrows, std::map<int, int> & sched)
{
    const int rate = 48000, block = 16;
    auto row_dur = [&](int b) { return speed * row_dur_i(rate, b) / (double)rate; };
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

inline void interval_to_bpm_speed(double delta, int min_speed, int & bpm_o, int & speed_o)
{
    bool have = false;
    double b_err = 0; int b_sp = 0, b_bpm = 0;
    for (int speed = min_speed; speed <= 12; speed++)
    {
        int bpm = (int)std::nearbyint(2.5 * speed / delta);
        if (!(32 <= bpm && bpm <= 255)) continue;
        double real = 2.5 * speed / bpm;
        double err = std::nearbyint(std::fabs(real - delta) / delta * 10000.0) / 10000.0;
        int sc2 = std::abs(speed - 6);
        if (!have || err < b_err || (err == b_err && sc2 < std::abs(b_sp - 6)))
        { have = true; b_err = err; b_sp = speed; b_bpm = bpm; }
    }
    if (!have)
    {
        int speed = std::max(min_speed, delta < 0.05 ? 1 : 12);
        bpm_o = std::max(32, std::min(255, (int)std::nearbyint(2.5 * speed / delta)));
        speed_o = speed;
        return;
    }
    bpm_o = b_bpm; speed_o = b_sp;
}

struct Cell { int note = 0, inst = 0, vol = 0, eff = 0, par = 0; };

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
        busy_until[c] = n->end_row;
        last_inst[c] = n->inst;
        started[c] = n->start_row;
        owner[c] = n;
    }
    return (int)busy_until.size();
}

// ── вспомогательное ──
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

inline double soft_limit(double x, double knee)
{
    if (x <= knee || knee >= 1.0) return std::min(x, 1.0);
    double w = 1.0 - knee;
    return knee + w * std::tanh((x - knee) / w);
}
inline double note_of(double f, double off) { return 69.0 + 12.0 * std::log2(f / 440.0) - off - 11.0; }
inline double freq_of(double n, double off) { return 440.0 * std::pow(2.0, (n + 11.0 - 69.0 + off) / 12.0); }

struct Zone
{
    int lo, hi, n0, L = 1, cell0 = 0;
    std::vector<double> data;
    bool has_loop = false;
    int loop_s = 0, loop_l = 0;
    Zone(int lo, int hi, int n0) : lo(lo), hi(hi), n0(n0) {}
};

struct Instrument
{
    int kind;
    Key key;
    std::string name;
    std::vector<Zone> zones;
    std::vector<Note *> notes;
    int fold = 0, index = 0;
    double rate0 = 1.0, peak = 1.0, centroid = 0.0;
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

struct XmSample { std::vector<int16_t> data; bool loop; int ls, ll, rel, fine; };
struct XmInst { std::string name; std::vector<XmSample> samples; std::array<uint8_t, 96> keymap; };

inline double centroid_of(const std::vector<double> & d0)
{
    size_t n = std::min<size_t>(d0.size(), 8192);
    if (n < 2) return 0;
    std::vector<double> w(n);
    for (size_t i = 0; i < n; i++) w[i] = d0[i] * (0.5 - 0.5 * std::cos(2 * M_PI * i / (n - 1)));
    double sw = 0, s = 0;
    size_t K = n / 2;
    for (size_t k = 0; k <= K; k++)
    {
        double re = 0, im = 0;
        for (size_t i = 0; i < n; i++)
        {
            double ang = -2 * M_PI * (double)((k * i) % n) / n;
            re += w[i] * std::cos(ang);
            im += w[i] * std::sin(ang);
        }
        double mag = std::sqrt(re * re + im * im);
        sw += mag * ((double)k / n);
        s += mag;
    }
    return sw / (s + 1e-9);
}

inline std::string convert_track(const NSF & nsf, int song, const std::string & base, const std::string & title,
                                 bool want_xm, bool want_midi, const Options & o)
{
    Player player(nsf, song);
    double clock = player.clock;
    Trace tr = player.run(o.seconds, o.stop_silence);
    if (tr.sound_end <= tr.start) return "silence: the APU plays nothing";
    double t_sound = (tr.sound_end - tr.start) / clock;
    double t_end = std::min((tr.end - tr.start) / clock, t_sound + o.tail);
    tr.end = std::min(tr.end, tr.start + (int64_t)(t_end * clock));

    std::vector<Note> store;
    for (int ch = 0; ch < 4; ch++) { auto v = channel_notes(tr, ch); for (auto & n : v) store.push_back(std::move(n)); }
    { auto v = dmc_notes(tr); for (auto & n : v) store.push_back(std::move(n)); }
    { auto v = dac_notes(tr); for (auto & n : v) store.push_back(std::move(n)); }
    std::vector<Note> filt;
    for (auto & n : store) if (n.t0 < t_end) filt.push_back(std::move(n));
    store = std::move(filt);
    for (auto & n : store) n.t1 = std::min(std::max(n.t1, n.t0 + 0.001), t_end);
    if (store.empty()) return "no notes found";
    std::vector<Note *> notes;
    for (auto & n : store) notes.push_back(&n);

    // ключи инструментов
    for (Note * n : notes)
    {
        Key k; k.kind = n->kind;
        if (n->kind == K_PULSE || n->kind == K_NOISE) k.p0 = n->patch.p0;
        else if (n->kind == K_DMC) { k.p0 = n->patch.p0; k.p1 = n->patch.p1; k.p2 = n->patch.p2; }
        else if (n->kind == K_DAC) k.p0 = n->patch.p0;
        n->key = k;
    }
    std::map<Key, double> first;
    for (Note * n : notes) if (!first.count(n->key)) first[n->key] = n->t0;
    std::vector<Key> order;
    for (auto & kv : first) order.push_back(kv.first);
    std::stable_sort(order.begin(), order.end(), [&](const Key & a, const Key & b) { return first[a] < first[b]; });
    std::vector<Instrument> insts;
    for (auto & k : order)
    {
        Instrument ins; ins.kind = k.kind; ins.key = k;
        for (Note * n : notes) if (n->key == k) ins.notes.push_back(n);
        insts.push_back(std::move(ins));
    }
    for (size_t i = 0; i < insts.size(); i++)
    {
        insts[i].index = (int)i + 1;
        Instrument & ins = insts[i];
        char b[64];
        if (ins.kind == K_PULSE) snprintf(b, sizeof b, "Pulse %s", DUTY_NAMES[ins.key.p0]);
        else if (ins.kind == K_TRI) snprintf(b, sizeof b, "Triangle");
        else if (ins.kind == K_NOISE) snprintf(b, sizeof b, "Noise%s", ins.key.p0 ? " (short)" : "");
        else if (ins.kind == K_DMC) snprintf(b, sizeof b, "DMC %08x", (unsigned)ins.key.p0);
        else snprintf(b, sizeof b, "PCM %08x", (unsigned)ins.key.p0);
        ins.name = b;
    }
    if ((int)insts.size() > MAX_XM_INSTRUMENTS) return "too many instruments (" + std::to_string(insts.size()) + " > 128)";
    std::vector<int> orig_index;
    for (auto & i : insts) orig_index.push_back(i.index);
    std::map<Key, Instrument *> inst_of;
    for (auto & i : insts) inst_of[i.key] = &i;

    // ── подстройка сетки полутонов ──
    double off = 0.0;
    {
        std::complex<double> z = 0;
        double wsum = 0;
        for (Note * n : notes)
            if ((n->kind == K_PULSE || n->kind == K_TRI) && !n->cur.empty() && n->cur[0].f > 0)
            {
                double p = 12.0 * std::log2(n->cur[0].f / 440.0);
                double w = std::sqrt(std::max(0.05, n->t1 - n->t0));
                z += w * std::polar(1.0, p * 2 * M_PI);
                wsum += w;
            }
        if (wsum > 0)
        {
            z /= wsum;
            if (std::abs(z) > 0.3) off = std::atan2(z.imag(), z.real()) / (2 * M_PI);
        }
    }
    int shift = o.transpose + 12 * o.octave;

    // ── зоны и сэмплы ──
    for (Instrument & ins : insts)
    {
        if (ins.kind == K_PULSE || ins.kind == K_TRI)
        {
            std::vector<int> ns;
            for (Note * n : ins.notes)
                for (auto & c : n->cur) ns.push_back(std::min(96, std::max(1, (int)std::nearbyint(note_of(c.f, off)))));
            std::sort(ns.begin(), ns.end());
            std::vector<Zone> zones;
            int lo = ns.front();
            while (lo <= ns.back())
            {
                int hi = lo + o.zone_width - 1;
                int first_in = -1, last_in = -1;
                for (int x : ns) if (lo <= x && x <= hi) { if (first_in < 0) first_in = x; last_in = x; }
                if (first_in >= 0) zones.emplace_back(lo, hi, (int)std::nearbyint((first_in + last_in) / 2.0));
                lo = hi + 1;
                if (zones.size() >= 16) break;
            }
            if (zones.size() >= 16 && zones.back().hi < ns.back()) zones.back().hi = 96;
            zones.front().lo = 1;
            zones.back().hi = 96;
            for (Zone & z : zones)
            {
                double f_ref = freq_of(z.n0, off);
                z.L = pow2_at_least(2.0 * 22050.0 * 1.5 / f_ref);
                std::vector<double> seq;
                if (ins.kind == K_PULSE) for (int i = 0; i < 8; i++) seq.push_back(DUTY_SEQ[ins.key.p0][i]);
                else
                {
                    for (int v = 15; v >= 0; v--) seq.push_back(v / 15.0);
                    for (int v = 0; v < 16; v++) seq.push_back(v / 15.0);
                }
                z.data = stepwave(seq, z.L);
                z.has_loop = true; z.loop_s = 0; z.loop_l = z.L;
            }
            ins.zones = std::move(zones);
        }
        else if (ins.kind == K_NOISE)
        {
            Zone z(1, 96, 1);
            auto seq = lfsr_seq((int)ins.key.p0);
            double mean = 0;
            for (double v : seq) mean += v;
            mean /= seq.size();
            double pk = 0;
            for (double & v : seq) { v -= mean; pk = std::max(pk, std::fabs(v)); }
            if (pk == 0) pk = 1.0;
            for (double & v : seq) v /= pk;
            z.data = seq; ins.peak = pk;
            z.has_loop = true; z.loop_s = 0; z.loop_l = (int)seq.size();
            ins.zones.push_back(z);
        }
        else if (ins.kind == K_DMC)
        {
            Note * n0 = ins.notes[0];
            size_t nbits = n0->data.size() * 8;
            auto c = dmc_decode(n0->data, n0->dlevel, n0->dloop, nbits);
            for (double & v : c) v *= DMC_STEP_GAIN;
            auto pcm = hp_sample(c, n0->drate, n0->dloop);
            double pk = 0;
            for (double v : pcm) pk = std::max(pk, std::fabs(v));
            if (pk == 0) pk = 1e-6;
            Zone z(1, 96, 49);
            for (double v : pcm) z.data.push_back(v / pk);
            if (n0->dloop) { z.has_loop = true; z.loop_s = 0; z.loop_l = (int)pcm.size(); }
            ins.zones.push_back(z);
            ins.rate0 = n0->drate; ins.peak = pk;
        }
        else
        {
            Note * n0 = ins.notes[0];
            std::vector<double> pcm;
            for (uint8_t v : n0->data) pcm.push_back(dmc_curve(v));
            pcm = hp_sample(pcm, n0->drate, false);
            double pk = 0;
            for (double v : pcm) pk = std::max(pk, std::fabs(v));
            if (pk == 0) pk = 1e-6;
            Zone z(1, 96, 49);
            for (double v : pcm) z.data.push_back(v / pk);
            ins.zones.push_back(z);
            ins.rate0 = n0->drate; ins.peak = pk;
        }
        if (ins.kind == K_DMC || ins.kind == K_DAC) ins.centroid = centroid_of(ins.zones[0].data);
    }
    for (Note * n : notes)
        if (n->kind == K_DMC || n->kind == K_DAC)
            for (auto & c : n->cur) c.a = inst_of[n->key]->peak;

    // ── уровни ──
    double norm = XM_REF_AMP * o.norm_scale;

    // ── сетка строк ──
    double frame_dt = player.cpp / clock;
    double true_delta = frame_dt * o.compact;
    int bpm, speed;
    interval_to_bpm_speed(true_delta, o.compact > 1 ? 2 : 1, bpm, speed);
    bool glide = speed >= 2;
    auto row_of = [&](double t) { return (int)std::floor(t / true_delta + 1e-9); };
    for (Note * n : notes)
    {
        n->start_row = row_of(n->t0);
        n->end_row = std::max(n->start_row + 1, (int)std::ceil(n->t1 / true_delta - 1e-6));
    }
    int sh = (*std::min_element(notes.begin(), notes.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; }))->start_row;
    for (Note * n : notes) { n->start_row -= sh; n->end_row -= sh; }
    double row_t0 = sh * true_delta;

    // ── fold ──
    for (Instrument & ins : insts)
        if (ins.kind == K_PULSE || ins.kind == K_TRI)
        {
            bool any = false;
            double lo = 0, hi = 0;
            for (Note * n : ins.notes)
                for (auto & c : n->cur)
                    if (c.f > 0)
                    {
                        double v = note_of(c.f, off) + shift;
                        if (!any) { lo = hi = v; any = true; }
                        lo = std::min(lo, v); hi = std::max(hi, v);
                    }
            if (!any) { lo = hi = 49; }
            int fold = 0;
            if (hi + 0.5 > 96) fold = -(int)std::ceil((hi + 0.5 - 96) / 12.0);
            else if (lo - 0.5 < 1) fold = (int)std::ceil((1 - (lo - 0.5)) / 12.0);
            ins.fold = fold;
        }
    auto cell_float = [&](const Instrument & ins, double f) {
        if (ins.kind == K_PULSE || ins.kind == K_TRI) return note_of(f, off) + shift + 12 * ins.fold;
        if (ins.kind == K_NOISE) return 1.0 + 12.0 * std::log2(std::max(f, 1.0) / 440.0);
        return 49.0 + 12.0 * std::log2(f / ins.rate0);
    };

    // ── инструменты XM ──
    std::vector<XmInst> xm_insts;
    for (size_t ii = 0; ii < insts.size(); ii++)
    {
        Instrument & ins = insts[ii];
        XmInst xi;
        xi.keymap.fill(0);
        for (size_t zi = 0; zi < ins.zones.size(); zi++)
        {
            Zone & z = ins.zones[zi];
            XmSample s;
            for (double v : z.data) s.data.push_back((int16_t)std::max(-32768.0, std::min(32767.0, std::nearbyint(v * 32767.0))));
            double tune;
            if (ins.kind == K_PULSE || ins.kind == K_TRI)
            {
                z.cell0 = z.n0 + shift + 12 * ins.fold;
                double play_rate = freq_of(z.n0, off) * z.L;
                tune = 12.0 * std::log2(play_rate / XM_C4_RATE) - (z.cell0 - 49);
            }
            else if (ins.kind == K_NOISE) tune = 48.0 + 12.0 * std::log2(440.0 / XM_C4_RATE);
            else tune = 12.0 * std::log2(ins.rate0 / XM_C4_RATE);
            int rel_ = (int)std::floor(tune);
            int fine = (int)std::nearbyint((tune - rel_) * 128);
            if (fine > 127) { rel_ += 1; fine -= 128; }
            rel_ = std::max(-96, std::min(95, rel_));
            s.rel = rel_;
            s.fine = std::max(-128, std::min(127, fine));
            s.loop = z.has_loop; s.ls = z.loop_s; s.ll = z.loop_l;
            xi.samples.push_back(std::move(s));
            if (ins.kind == K_PULSE || ins.kind == K_TRI)
                for (int cell = 1; cell <= 96; cell++)
                {
                    int n_real = cell - shift - 12 * ins.fold;
                    if (z.lo <= n_real && n_real <= z.hi) xi.keymap[cell - 1] = (uint8_t)zi;
                }
        }
        if (ins.kind == K_PULSE || ins.kind == K_TRI)
            for (int cell = 1; cell <= 96; cell++)
            {
                int n_real = cell - shift - 12 * ins.fold;
                bool in = false;
                for (auto & z : ins.zones) if (z.lo <= n_real && n_real <= z.hi) in = true;
                if (!in) xi.keymap[cell - 1] = (uint8_t)ins.zone_for(n_real);
            }
        char nb[64];
        snprintf(nb, sizeof nb, "%02d %s", orig_index[ii], ins.name.c_str());
        xi.name = std::string(nb).substr(0, 22);
        xm_insts.push_back(std::move(xi));
    }
    for (Note * n : notes) n->inst = inst_of[n->key]->index;
    std::vector<Note *> sorted = notes;
    std::stable_sort(sorted.begin(), sorted.end(), [](Note * a, Note * b) {
        return std::tie(a->start_row, a->ch) < std::tie(b->start_row, b->ch); });
    int nch = allocate_channels(sorted, std::max(2, std::min(MAX_XM_CHANNELS, o.channels)));
    nch = std::max(2, nch + (nch & 1));
    nch = std::min(nch, MAX_XM_CHANNELS);

    // ── XM ──
    if (want_xm)
    {
        std::map<std::pair<int, int>, Cell> grid;
        int last_row = 0;
        std::map<int, std::vector<Note *>> by_chan;
        for (Note * n : notes) by_chan[n->channel].push_back(n);
        auto put = [&](int row, int ch) -> Cell & { return grid[{row, ch}]; };
        for (auto & kv : by_chan)
        {
            int ch = kv.first;
            auto & lst = kv.second;
            std::stable_sort(lst.begin(), lst.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; });
            for (size_t k = 0; k < lst.size(); k++)
            {
                Note * n = lst[k];
                const Instrument & ins = *inst_of[n->key];
                bool has_next = k + 1 < lst.size();
                int nxt = has_next ? lst[k + 1]->start_row : 0;
                int end_row = !has_next ? n->end_row : std::min(n->end_row, nxt);
                n->end_row = end_row;
                last_row = std::max(last_row, end_row);
                std::vector<double> times;
                for (auto & c : n->cur) times.push_back(c.t);
                bool is_short = (n->t1 - n->t0) < 3 * true_delta;

                auto row_amp = [&](int row) {
                    double tw0 = row_t0 + row * true_delta;
                    double a0 = std::max(n->t0, tw0), a1 = std::min(n->t1, tw0 + true_delta);
                    if (a1 <= a0) return 0.0;
                    double e = 0.0;
                    int i = std::max(0, (int)(std::upper_bound(times.begin(), times.end(), a0) - times.begin()) - 1);
                    double t = a0;
                    while (t < a1 && i < (int)times.size())
                    {
                        double nx = i + 1 < (int)times.size() ? times[i + 1] : a1;
                        double te = std::min(a1, nx);
                        e += n->cur[i].a * n->cur[i].a * (te - t);
                        t = te;
                        i++;
                    }
                    double span = (row == n->start_row && !is_short) ? (a1 - a0) : true_delta;
                    return std::sqrt(e / span);
                };
                auto vol_of = [&](double a) {
                    return a > 0 ? std::max(1, std::min(64, (int)std::nearbyint(soft_limit(a / norm, o.limit_knee) * 64.0))) : 0;
                };

                Cell & c0 = put(n->start_row, ch);
                double x0 = cell_float(ins, n->cur[0].f);
                int cur_cell = std::max(1, std::min(96, (int)std::nearbyint(x0)));
                c0.note = cur_cell;
                c0.inst = n->inst;
                int last_vol = vol_of(row_amp(n->start_row));
                c0.vol = 0x10 + last_vol;
                double last_pitch = x0;
                if (ins.kind != K_DMC && ins.kind != K_DAC)
                    for (int row = n->start_row + 1; row < end_row; row++)
                    {
                        double t = row_t0 + row * true_delta;
                        int i = std::max(0, (int)(std::upper_bound(times.begin(), times.end(), t) - times.begin()) - 1);
                        double f = n->cur[i].f;
                        Cell * c = nullptr;
                        int vv = vol_of(row_amp(row));
                        if (vv != last_vol && std::abs(vv - last_vol) >= std::max(1, (int)(0.06 * last_vol)))
                        {
                            c = &put(row, ch);
                            c->vol = 0x10 + vv;
                            last_vol = vv;
                        }
                        double pf = cell_float(ins, f);
                        if (std::fabs(pf - last_pitch) >= o.bend_threshold)
                        {
                            int nc = std::max(1, std::min(96, (int)std::nearbyint(pf)));
                            if (nc != cur_cell)
                            {
                                if (!c) c = &put(row, ch);
                                c->note = nc;
                                if (glide) { c->eff = 0x03; c->par = 0xFF; }
                                else { c->inst = n->inst; c->vol = 0x10 + last_vol; }
                                cur_cell = nc;
                            }
                            last_pitch = pf;
                        }
                    }
                if (!has_next || end_row < nxt)
                {
                    Cell & oc = put(end_row, ch);
                    if (!oc.note) oc.note = 97;
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
        b.str("NSF2XM Converter    ", 20, ' ');
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
            for (auto & s : xi.samples) xmloop::fix_loop(s.data, s.ls, s.ll, s.loop);   // бесшовные петли
            const int ihs = 263;
            b.le32(ihs); b.str(xi.name, 22, 0);
            b.u8(0); b.le16((int)xi.samples.size());
            if (xi.samples.empty()) continue;
            b.le32(40);
            for (int i = 0; i < 96; i++) b.u8(xi.keymap[i]);
            { uint8_t ve[48] = {0}; ve[2] = 64; ve[4] = 1000 & 0xFF; ve[5] = 1000 >> 8; ve[6] = 64; b.raw({ve, ve + 48}); }
            { uint8_t pe[48] = {0}; pe[2] = 32; pe[4] = 1000 & 0xFF; pe[5] = 1000 >> 8; pe[6] = 32; b.raw({pe, pe + 48}); }
            b.raw({2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0});
            b.le16(0);
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
                for (int16_t v : s.data)
                {
                    int d = (((int)v - prev + 32768) % 65536 + 65536) % 65536 - 32768;
                    b.le16(d & 0xFFFF);
                    prev = v;
                }
            }
        }
        if (!b.save(base + ".xm")) return "cannot write " + base + ".xm";
    }

    // ── MIDI ──
    if (want_midi)
    {
        struct ME { int tick, order, ch; std::vector<uint8_t> data; };
        std::vector<ME> ev;
        const double tps = 960.0;
        std::set<int> init_done;
        std::map<int, int> ch_prog;
        std::map<Key, int> drums;
        std::vector<Instrument *> dr_ins;
        for (auto & i : insts) if (i.kind == K_DMC || i.kind == K_DAC) dr_ins.push_back(&i);
        std::stable_sort(dr_ins.begin(), dr_ins.end(), [](Instrument * a, Instrument * b) { return a->centroid < b->centroid; });
        for (size_t k = 0; k < dr_ins.size(); k++)
            drums[dr_ins[k]->key] = DRUM_NOTES[std::min<size_t>(11, k * 12 / std::max<size_t>(1, dr_ins.size()))];
        auto init_ch = [&](int mch) {
            if (init_done.count(mch)) return;
            init_done.insert(mch);
            ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 101, 0}});
            ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 100, 0}});
            ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 6, 12}});
            ev.push_back({0, 0, mch, {(uint8_t)(0xB0 | mch), 38, 0}});
        };
        std::vector<Note *> by_t = notes;
        std::stable_sort(by_t.begin(), by_t.end(), [](Note * a, Note * b) { return a->t0 < b->t0; });
        for (Note * n : by_t)
        {
            int tick0 = (int)std::nearbyint(n->t0 * tps);
            int tick1 = std::max(tick0 + 20, (int)std::nearbyint(n->t1 * tps));
            double maxa = 0;
            for (auto & c : n->cur) maxa = std::max(maxa, c.a);
            int vel = std::max(1, std::min(127, (int)std::nearbyint(soft_limit(maxa / norm, o.limit_knee) * 110)));
            if (n->kind == K_NOISE || n->kind == K_DMC || n->kind == K_DAC)
            {
                int mch = 9, key;
                if (n->kind == K_NOISE)
                {
                    double rate = n->cur[0].f;
                    key = n->patch.p0 == 1 ? 42 : rate > 12000 ? 42 : rate > 3000 ? 38 : 36;
                }
                else { auto it = drums.find(n->key); key = it != drums.end() ? it->second : 38; }
                init_ch(mch);
                ev.push_back({tick0, 3, mch, {(uint8_t)(0x90 | mch), (uint8_t)key, (uint8_t)vel}});
                ev.push_back({tick1, 0, mch, {(uint8_t)(0x80 | mch), (uint8_t)key, 0}});
                continue;
            }
            int mch = n->ch;   // 0,1,2
            init_ch(mch);
            int prog = n->kind == K_TRI ? 38 : 80;
            if (!ch_prog.count(mch) || ch_prog[mch] != prog)
            {
                ev.push_back({tick0, 1, mch, {(uint8_t)(0xC0 | mch), (uint8_t)prog}});
                ch_prog[mch] = prog;
            }
            int key = -1;
            bool have_b = false, have_e = false;
            double last_b = 0;
            int last_e = 0;
            for (auto & c : n->cur)
            {
                int tk = std::max(tick0, (int)std::nearbyint(c.t * tps));
                if (tk >= tick1) break;
                double midi_f = 69.0 + 12.0 * std::log2(c.f / 440.0) - off + shift;
                if (key < 0 || std::fabs(midi_f - key) > 0.7)
                {
                    if (key >= 0) ev.push_back({tk, 0, mch, {(uint8_t)(0x80 | mch), (uint8_t)key, 0}});
                    key = std::max(0, std::min(127, (int)std::nearbyint(midi_f)));
                    ev.push_back({tk, 2, mch, {(uint8_t)(0xB0 | mch), 10, 64}});
                    ev.push_back({tk, 3, mch, {(uint8_t)(0x90 | mch), (uint8_t)key, (uint8_t)vel}});
                    have_b = have_e = false;
                }
                double bend = std::max(-12.0, std::min(12.0, midi_f - key));
                if (!have_b || std::fabs(bend - last_b) >= 0.06)
                {
                    int val = (int)(8192 + bend / 12.0 * 8191);
                    ev.push_back({tk, 2, mch, {(uint8_t)(0xE0 | mch), (uint8_t)(val & 0x7F), (uint8_t)((val >> 7) & 0x7F)}});
                    last_b = bend; have_b = true;
                }
                int e = std::max(1, std::min(127, (int)std::nearbyint(c.a / std::max(maxa, 1e-9) * 127)));
                if (!have_e || std::abs(e - last_e) >= 3)
                {
                    ev.push_back({tk, 2, mch, {(uint8_t)(0xB0 | mch), 11, (uint8_t)e}});
                    last_e = e; have_e = true;
                }
            }
            if (key >= 0) ev.push_back({tick1, 0, mch, {(uint8_t)(0x80 | mch), (uint8_t)key, 0}});
        }
        // дорожки по каналу
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
            std::stable_sort(t.begin(), t.end(), [](const ME & a, const ME & b) {
                return std::tie(a.tick, a.order) < std::tie(b.tick, b.order); });
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

// song: номер трека с 1 (0 = стартовый)
inline std::string export_nsf(const std::string & path, int song, const std::string & out_base, bool want_xm,
                              bool want_midi, const Options & o)
{
    NSF nsf;
    if (!load_nsf(path, nsf)) return "not an NSF file";
    int s = song > 0 ? song : nsf.start_song;
    if (s < 1 || s > nsf.total_songs) return "track " + std::to_string(s) + " does not exist";
    std::string title = nsf.title;
    if (title.empty())
    {
        title = out_base.substr(out_base.rfind('/') + 1);
    }
    if (nsf.total_songs > 1) title += " #" + std::to_string(s);
    return convert_track(nsf, s - 1, out_base, title, want_xm, want_midi, o);
}

} // namespace nsfexp
