// Встроенный экспорт USF/miniusf (N64) -> XM / MIDI на C++ (порт usf2xm-midi-wav.py).
// Вход - журнал трассировки аудио-микрокода lazyusf2 (текст) и PCM-поток декодированных сэмплов.
#pragma once
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <vector>

namespace usfexp {

constexpr double C4_HZ = 261.6256, XM_C4_RATE = 8363.0;
constexpr int MAX_XM_CHANNELS = 32, MAX_XM_INSTRUMENTS = 128;
static const int DRUM_NOTES[12] = {36, 38, 41, 43, 45, 42, 46, 49, 51, 39, 37, 56};
static const int GM_PROGRAMS[24] = {0, 25, 33, 48, 80, 81, 73, 56, 4, 11, 19, 24, 38, 46, 52, 61,
                                    71, 65, 87, 90, 95, 98, 104, 108};

struct Options
{
    int octave = 0, transpose = 0, compact = 1, channels = 32;
    double release_threshold = 0.04, bend_threshold = 0.75;
    bool no_pan = false;
};

// ── FFT (смешанное основание, для любой длины) ──
using cd = std::complex<double>;
inline void fft_rec(std::vector<cd> & a)
{
    size_t n = a.size();
    if (n <= 1) return;
    size_t p = 2;
    while (p * p <= n && n % p) p++;
    if (n % p) p = n;                     // простое
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
        for (size_t r = 0; r < p; r++)
            s += sub[r][k % m] * std::polar(1.0, -2 * M_PI * (double)((r * k) % n) / n);
        a[k] = s;
    }
}

// ── разбор журнала ──
struct VoiceFrame
{
    int frame = 0;
    uint32_t key = 0;
    bool init = false, has_adpcm = false, has_hash = false;
    uint32_t hash = 0;
    long cum = 0;
    double ratio = 0, left = 0, right = 0, dry = 0;
};

struct Trace
{
    int rate = 32000;
    std::vector<VoiceFrame> voices;
    std::vector<double> frame_times{0.0};
    int frame_samples = 0;
    // hash -> список (cum, offset, count_bytes)
    std::map<uint32_t, std::vector<std::tuple<long, long, long>>> chunks;
};

inline Trace parse_trace(const std::string & text)
{
    Trace tr;
    std::vector<std::vector<std::string>> lines;
    {
        std::istringstream is(text);
        std::string ln;
        while (std::getline(is, ln))
        {
            std::vector<std::string> p;
            std::istringstream ls(ln);
            std::string w;
            while (ls >> w) p.push_back(w);
            lines.push_back(std::move(p));
        }
    }
    for (auto & p : lines)
        if (p.size() >= 3 && p[0] == "#" && p[1] == "rate") tr.rate = atoi(p[2].c_str());
    int frame_bytes = 0;
    for (auto & p : lines)
        if (!p.empty() && p[0] == "S" && p.size() > 2) frame_bytes = std::max(frame_bytes, atoi(p[2].c_str()));
    if (frame_bytes == 0) return tr;
    long samples = 0;
    int frame = 0;
    const std::vector<std::string> * cur_a = nullptr, * cur_r = nullptr;
    for (auto & p : lines)
    {
        if (p.empty()) continue;
        const std::string & t = p[0];
        if (t == "A" && p.size() >= 10) cur_a = &p;
        else if (t == "R" && p.size() >= 4) cur_r = &p;
        else if (t == "E" && p.size() >= 12)
        {
            if (cur_r)
            {
                VoiceFrame vf;
                vf.frame = frame;
                vf.key = (uint32_t)strtoul((*cur_r)[3].c_str(), nullptr, 16);
                int rflags = atoi((*cur_r)[1].c_str());
                vf.ratio = atoi((*cur_r)[2].c_str()) * 2 / 65536.0;
                vf.has_adpcm = cur_a != nullptr;
                if (cur_a)
                {
                    vf.init = atoi((*cur_a)[1].c_str()) & 1;
                    vf.hash = (uint32_t)strtoul((*cur_a)[7].c_str(), nullptr, 16);
                    vf.has_hash = true;
                    vf.cum = atol((*cur_a)[8].c_str());
                }
                else { vf.init = rflags & 1; vf.has_hash = false; vf.cum = 0; }
                int v0 = std::abs(atoi(p[5].c_str())), v1 = std::abs(atoi(p[6].c_str()));
                int g0 = std::abs(atoi(p[7].c_str())), g1 = std::abs(atoi(p[8].c_str()));
                vf.left = std::max(v0, g0) / 32768.0;
                vf.right = std::max(v1, g1) / 32768.0;
                vf.dry = std::abs(atoi(p[11].c_str())) / 32768.0;
                tr.voices.push_back(vf);
            }
            cur_a = cur_r = nullptr;
        }
        else if (t == "S" && p.size() > 2)
        {
            if (atoi(p[2].c_str()) == frame_bytes)
            {
                samples += frame_bytes / 4;
                frame++;
                tr.frame_times.push_back(samples / (double)tr.rate);
            }
        }
        if (t == "A" && p.size() >= 10)
        {
            long off = atol(p[9].c_str());
            if (off >= 0)
                tr.chunks[(uint32_t)strtoul(p[7].c_str(), nullptr, 16)].push_back(
                    {atol(p[8].c_str()), off, atol(p[4].c_str())});
        }
    }
    tr.frame_samples = frame_bytes / 4;
    return tr;
}

inline std::map<uint32_t, std::vector<int16_t>> load_streams(const Trace & tr, const std::vector<int16_t> & pcm)
{
    std::map<uint32_t, std::vector<int16_t>> streams;
    for (auto & kv : tr.chunks)
    {
        long end = 0;
        for (auto & c : kv.second) end = std::max(end, std::get<0>(c) + std::get<2>(c));
        end /= 2;
        std::vector<int16_t> buf(end, 0);
        for (auto & c : kv.second)
        {
            long cum = std::get<0>(c), off = std::get<1>(c), n = std::get<2>(c);
            long a = off / 2;
            for (long i = 0; i < n / 2 && a + i < (long)pcm.size() && cum / 2 + i < end; i++) buf[cum / 2 + i] = pcm[a + i];
        }
        streams[kv.first] = std::move(buf);
    }
    return streams;
}

// ── ноты ──
struct Sample;
struct Note
{
    uint32_t key, hash;
    int start_frame, end_frame;
    std::vector<int> frames;
    std::vector<double> ratio, level;
    std::vector<int> pan;
    double peak = 0, start_t = 0, end_t = 0;
    Sample * sample = nullptr;
    bool retrig = false;
    long cum = 0;
    int channel = 0, start_row = 0, end_row = 0;
    Note(uint32_t k, int sf, uint32_t h) : key(k), hash(h), start_frame(sf), end_frame(sf + 1) {}
};

inline void frame_level_pan(const VoiceFrame & vf, double & lv, int & pan)
{
    lv = std::max(vf.left, vf.right) * vf.dry;
    double tot = vf.left + vf.right;
    pan = tot > 0 ? (int)std::nearbyint(255 * vf.right / tot) : 128;
}

inline std::vector<Note> build_notes(const std::vector<VoiceFrame> & voices, double release_ratio)
{
    std::vector<uint32_t> key_order;
    std::map<uint32_t, std::vector<const VoiceFrame *>> by_key;
    for (auto & vf : voices)
    {
        if (!by_key.count(vf.key)) key_order.push_back(vf.key);
        by_key[vf.key].push_back(&vf);
    }
    std::vector<Note> notes;
    for (uint32_t key : key_order)
    {
        std::unique_ptr<Note> cur;
        int quiet = 0;
        bool have_last = false, pending = false;
        uint32_t last_hash = 0;
        double valley = 0, run_peak = 0;
        for (const VoiceFrame * pv : by_key[key])
        {
            const VoiceFrame & vf = *pv;
            double lv;
            int pan;
            frame_level_pan(vf, lv, pan);
            if (vf.has_adpcm && vf.has_hash) { last_hash = vf.hash; have_last = true; pending = false; }
            bool have_start = false, start_retrig = false;
            uint32_t start_hash = 0;
            if (vf.init)
            {
                if (cur) { cur->end_frame = vf.frame; notes.push_back(std::move(*cur)); cur.reset(); }
                if (!vf.has_hash) { pending = true; have_last = false; }
                else { have_start = true; start_hash = vf.hash; start_retrig = false; }
            }
            else if (have_last)
            {
                if (!cur)
                {
                    if (lv >= 0.02) { have_start = true; start_hash = last_hash; start_retrig = true; }
                }
                else if (lv > valley * 2.5 + 0.02 && valley < 0.6 * run_peak)
                {
                    cur->end_frame = vf.frame;
                    notes.push_back(std::move(*cur));
                    cur.reset();
                    have_start = true; start_hash = last_hash; start_retrig = true;
                }
            }
            if (have_start)
            {
                cur.reset(new Note(key, vf.frame, start_hash));
                cur->retrig = start_retrig;
                cur->cum = vf.cum;
                quiet = 0;
                valley = run_peak = lv;
            }
            if (!cur) continue;
            cur->frames.push_back(vf.frame);
            cur->ratio.push_back(vf.ratio);
            cur->level.push_back(lv);
            cur->pan.push_back(pan);
            cur->peak = std::max(cur->peak, lv);
            cur->end_frame = vf.frame + 1;
            if (lv > run_peak) { run_peak = lv; valley = lv; }
            else valley = std::min(valley, lv);
            if (lv < std::max(0.002, release_ratio * cur->peak) && cur->frames.size() > 1)
            {
                quiet++;
                if (quiet >= 2)
                {
                    cur->end_frame = vf.frame - 1;
                    if (cur->end_frame <= cur->start_frame) cur->end_frame = cur->start_frame + 1;
                    notes.push_back(std::move(*cur));
                    cur.reset();
                    valley = run_peak = 0.0;
                }
            }
            else quiet = 0;
        }
        if (cur) notes.push_back(std::move(*cur));
    }
    std::vector<Note> out;
    for (auto & n : notes) if (n.peak > 0.002) out.push_back(std::move(n));
    std::stable_sort(out.begin(), out.end(), [](const Note & a, const Note & b) {
        return std::tie(a.start_frame, a.key) < std::tie(b.start_frame, b.key); });
    return out;
}

// ── сэмплы ──
struct Loop { bool has = false; int start = 0, len = 0; };

inline bool detect_loop(const std::vector<int16_t> & s, int & lstart, int & lp)
{
    int n = (int)s.size();
    bool any = false;
    for (auto v : s) if (v) { any = true; break; }
    if (n < 96 || !any) return false;
    const int w = 48;
    const int16_t * key = &s[n - w];
    for (int i = n - w - 1; i >= 0; i--)
    {
        if (s[i] != key[0]) continue;
        int p = n - w - i;
        if (p < 16 || n < 2 * p) continue;
        if (memcmp(&s[i], key, w * sizeof(int16_t))) continue;
        int bad_last = -1;
        for (int k = n - p - 1; k >= 0; k--) if (s[p + k] != s[k]) { bad_last = k; break; }
        int start = bad_last >= 0 ? bad_last + 1 : 0;
        if (n - start >= 2 * p)
        {
            for (int d = 2; d < 9; d++)
                if (p % d == 0 && !memcmp(&s[start + p / d], &s[start], (p - p / d) * sizeof(int16_t))) p /= d;
            lstart = start; lp = p;
            return true;
        }
    }
    return false;
}

inline bool detect_period(const std::vector<double> & x0, double & period, double & conf)
{
    int n = (int)x0.size();
    period = 0; conf = 0;
    if (n < 64) return false;
    double mean = 0;
    for (double v : x0) mean += v;
    mean /= n;
    std::vector<double> x(n);
    bool any = false;
    for (int i = 0; i < n; i++) { x[i] = x0[i] - mean; if (x[i] != 0) any = true; }
    if (!any) return false;
    int maxlag = std::min(n / 2, 1024), minlag = 6;
    if (maxlag <= minlag + 2) return false;
    std::vector<double> ac(maxlag);
    for (int k = 0; k < maxlag; k++)
    {
        double s = 0;
        for (int i = 0; i + k < n; i++) s += x[i] * x[i + k];
        ac[k] = s;
    }
    std::vector<double> seg;
    for (int k = minlag; k < maxlag; k++) seg.push_back(ac[k] / ac[0] * ((double)n / (n - k)));
    double best = *std::max_element(seg.begin(), seg.end());
    if (best < 0.5) { conf = best; return false; }
    for (int i = 1; i < (int)seg.size() - 1; i++)
        if (seg[i] >= seg[i - 1] && seg[i] >= seg[i + 1] && seg[i] >= 0.9 * best)
        {
            double a = seg[i - 1], b = seg[i], c = seg[i + 1];
            double den = a - 2 * b + c;
            double off = den != 0 ? 0.5 * (a - c) / den : 0.0;
            period = minlag + i + off; conf = b;
            return true;
        }
    int im = (int)(std::max_element(seg.begin(), seg.end()) - seg.begin());
    period = minlag + im; conf = best;
    return true;
}

struct Sample
{
    uint32_t hash;
    int index = 0, use_count = 0;
    Loop loop;
    std::vector<int16_t> data;
    double period = 0, confidence = 0, m1 = 0, centroid = 0, peak = 1.0, tune = 0;
    bool pitched = false;
    int fold = 0;
    Sample(uint32_t h, std::vector<int16_t> d) : hash(h), data(std::move(d)) {}

    void analyse()
    {
        std::vector<int16_t> s = data;
        std::vector<int16_t> seg;
        int ls, lp;
        if (detect_loop(s, ls, lp))
        {
            loop.has = true; loop.start = ls; loop.len = lp;
            data.assign(s.begin(), s.begin() + ls + lp);
            int reps = std::max(1, 4096 / lp + 1);
            for (int r = 0; r < reps; r++) for (int i = 0; i < lp; i++) seg.push_back(s[ls + i]);
            if (seg.size() > 8192) seg.resize(8192);
        }
        else
        {
            int last = -1;
            for (int i = (int)s.size() - 1; i >= 0; i--) if (s[i]) { last = i; break; }
            if (last >= 0) data.assign(s.begin(), s.begin() + last + 1);
            size_t from = std::min<size_t>(data.size() / 8, 256);
            for (size_t i = from; i < data.size() && seg.size() < 6000; i++) seg.push_back(data[i]);
        }
        int mx = 0;
        for (int16_t v : data) mx = std::max(mx, std::abs((int)v));
        peak = mx ? mx : 1.0;
        std::vector<double> sd(seg.begin(), seg.end());
        double per, conf;
        bool ok = detect_period(sd, per, conf);
        confidence = conf;
        if (ok && conf >= 0.6) { period = per; pitched = true; }
        if (seg.size() > 64)
        {
            size_t n = seg.size();
            std::vector<cd> a(n);
            for (size_t i = 0; i < n; i++) a[i] = sd[i] * (0.5 - 0.5 * std::cos(2 * M_PI * i / (n - 1)));
            fft_rec(a);
            double sw = 0, ss = 0;
            for (size_t k = 0; k <= n / 2; k++)
            {
                double m = std::abs(a[k]);
                sw += m * ((double)k / n);
                ss += m;
            }
            centroid = sw / (ss + 1e-9);
        }
    }
    void set_pitch(double ref_rate)
    {
        if (pitched) { double f1 = ref_rate / period; m1 = 12.0 * std::log2(f1 / C4_HZ); }
    }
};

// numpy.add.reduce для complex128: попарное суммирование (pairwise_sum_CDOUBLE), n - число double (2 на элемент)
inline void pw_sum(const double * a, size_t n, double & rr, double & ri)
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
        pw_sum(a, n2, r1, i1);
        pw_sum(a + n2, n - n2, r2, i2);
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
    pw_sum(buf.data(), buf.size(), rr, ri);
    return {rr / (double)t.size(), ri / (double)t.size()};
}

// ── темп, каналы ──
inline double detect_row_interval(std::vector<double> t, double & t0_out, double & coh_out, double max_row = 0.06)
{
    std::sort(t.begin(), t.end());
    t0_out = 0; coh_out = 0;
    if (t.size() < 4) return 0.05;
    auto score = [&](double d) { return std::abs(np_mean_expi(t, d)); };
    // numpy.arange: delta = (start + step) - start, элемент i = start + i*delta
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

inline void interval_to_bpm_speed(double delta, int & bpm_o, int & speed_o)
{
    bool have = false;
    double b_err = 0;
    int b_sp = 0, b_bpm = 0;
    for (int speed = 1; speed <= 12; speed++)
    {
        int bpm = (int)std::nearbyint(2.5 * speed / delta);
        if (!(32 <= bpm && bpm <= 255)) continue;
        double real = 2.5 * speed / bpm;
        double err = std::nearbyint(std::fabs(real - delta) / delta * 10000.0) / 10000.0;
        if (!have || err < b_err || (err == b_err && std::abs(speed - 6) < std::abs(b_sp - 6)))
        { have = true; b_err = err; b_sp = speed; b_bpm = bpm; }
    }
    if (!have)
    {
        int speed = delta < 0.05 ? 1 : 12;
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
        for (int c : fr) if (last_inst[c] == n->sample->index) same.push_back(c);
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
        last_inst[c] = n->sample->index;
        started[c] = n->start_row;
        owner[c] = n;
    }
    return (int)busy_until.size();
}

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

inline double cell_float(const Note & n, int idx, const Sample & s, double shift)
{
    double r = std::max(n.ratio[idx], 1e-4);
    return 49.0 + s.m1 + 12.0 * std::log2(r) + shift;
}

// Конвертация по готовому журналу. rate берётся из журнала.
inline std::string convert(const Trace & tr, const std::vector<int16_t> & pcm, const std::string & base,
                           const std::string & title, bool want_xm, bool want_midi, const Options & o)
{
    if (tr.voices.empty()) return "no ADPCM/ENVMIXER voices in the log (supported: libultra ABI1 and MusyX v1/v2; ABI2/ABI3 are not)";
    const auto & frame_times = tr.frame_times;
    double rate = tr.rate;
    auto notes_v = build_notes(tr.voices, o.release_threshold);
    if (notes_v.empty()) return "no notes found";
    for (auto & n : notes_v)
    {
        n.start_t = frame_times[std::min<size_t>(n.start_frame, frame_times.size() - 1)];
        n.end_t = frame_times[std::min<size_t>(n.end_frame, frame_times.size() - 1)];
    }
    auto streams = load_streams(tr, pcm);
    // частоты использования
    std::vector<uint32_t> seen_order;
    std::map<uint32_t, int> cnt;
    std::map<uint32_t, int> first;
    for (auto & n : notes_v)
    {
        if (!cnt.count(n.hash)) seen_order.push_back(n.hash);
        cnt[n.hash]++;
        if (!first.count(n.hash)) first[n.hash] = n.start_frame;
    }
    std::vector<uint32_t> used = seen_order;
    std::stable_sort(used.begin(), used.end(), [&](uint32_t a, uint32_t b) { return cnt[a] > cnt[b]; });
    if ((int)used.size() > MAX_XM_INSTRUMENTS) used.resize(MAX_XM_INSTRUMENTS);
    std::stable_sort(used.begin(), used.end(), [&](uint32_t a, uint32_t b) { return first[a] < first[b]; });
    std::map<uint32_t, std::unique_ptr<Sample>> samples;
    std::vector<Sample *> sample_list;
    for (uint32_t h : used)
    {
        auto it = streams.find(h);
        if (it == streams.end() || it->second.size() < 16) continue;
        auto s = std::unique_ptr<Sample>(new Sample(h, it->second));
        s->index = (int)samples.size() + 1;
        s->use_count = cnt[h];
        s->analyse();
        s->set_pitch(rate);
        sample_list.push_back(s.get());
        samples[h] = std::move(s);
    }
    std::vector<Note *> notes;
    for (auto & n : notes_v) if (samples.count(n.hash)) { n.sample = samples[n.hash].get(); notes.push_back(&n); }
    if (notes.empty()) return "no notes with sample data found";

    int total_shift = o.transpose + 12 * o.octave;
    std::map<uint32_t, std::vector<Note *>> by_sample;
    for (Note * n : notes) by_sample[n->hash].push_back(n);
    for (Sample * s : sample_list)
    {
        auto & ns = by_sample[s->hash];
        std::vector<double> fr;
        for (Note * n : ns)
        {
            size_t j = n->ratio.size() / 2;
            fr.push_back(12.0 * std::log2(std::max(n->ratio[j], 1e-4)) + s->m1);
        }
        if (!fr.empty() && s->pitched)
        {
            double c = 0, sn = 0;
            for (double f : fr) { c += std::cos(f * 2 * M_PI); sn += std::sin(f * 2 * M_PI); }
            c /= fr.size(); sn /= fr.size();
            if (std::hypot(c, sn) > 0.5) s->m1 -= std::atan2(sn, c) / (2 * M_PI);
        }
        double lo = 1e300, hi = -1e300;
        for (Note * n : ns)
            for (double r : n->ratio)
            {
                double v = 49 + s->m1 + 12 * std::log2(std::max(r, 1e-4)) + total_shift;
                lo = std::min(lo, v); hi = std::max(hi, v);
            }
        int fold = 0;
        if (hi + 0.5 > 96 || lo - 0.5 < 1)
        {
            double mid = (lo + hi) / 2;
            fold = (int)std::nearbyint((mid - 48.5) / 12.0);
        }
        s->fold = fold;
        s->tune = 12.0 * std::log2(rate / XM_C4_RATE) - s->m1 - total_shift + 12 * fold;
    }

    // громкость: 98-й перцентиль пиков
    double norm;
    {
        std::vector<double> peaks;
        for (Note * n : notes) peaks.push_back(n->peak * n->sample->peak / 32768.0);
        std::sort(peaks.begin(), peaks.end());
        double pos = 0.98 * (peaks.size() - 1);
        size_t lo = (size_t)std::floor(pos), hi = std::min(peaks.size() - 1, lo + 1);
        norm = peaks[lo] + (peaks[hi] - peaks[lo]) * (pos - lo);
        if (norm == 0) norm = 1.0;
    }

    // ── сетка строк ──
    std::vector<double> onsets;
    for (Note * n : notes) onsets.push_back(n->start_t);
    double t0 = 0, coh;
    double true_delta = detect_row_interval(onsets, t0, coh);
    true_delta *= o.compact;
    int bpm, speed;
    interval_to_bpm_speed(true_delta, bpm, speed);
    for (Note * n : notes)
    {
        n->start_row = (int)std::nearbyint((n->start_t - t0) / true_delta);
        n->end_row = (int)std::nearbyint((n->end_t - t0) / true_delta);
    }
    int shift = (*std::min_element(notes.begin(), notes.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; }))->start_row;
    for (Note * n : notes) { n->start_row -= shift; n->end_row = std::max(n->start_row + 1, n->end_row - shift); }
    double delta = true_delta;
    double row_t0 = t0 + shift * true_delta;
    std::vector<Note *> by_row = notes;      // порядок как в Python: notes уже отсортированы по (кадр, голос)
    int nch = allocate_channels(by_row, std::max(2, std::min(MAX_XM_CHANNELS, o.channels)));
    nch = std::max(2, nch + (nch & 1));
    nch = std::min(nch, MAX_XM_CHANNELS);

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
                Sample & s = *n->sample;
                bool has_next = k + 1 < lst.size();
                int nxt = has_next ? lst[k + 1]->start_row : 0;
                int end_row = !has_next ? n->end_row : std::min(n->end_row, nxt);
                n->end_row = end_row;
                last_row = std::max(last_row, end_row);
                auto put = [&](int row) -> Cell & { return grid[{row, ch}]; };
                auto vol_at = [&](int i) {
                    double v = n->level[i] * s.peak / 32768.0 / norm * 64.0;
                    return std::max(1, std::min(64, (int)std::nearbyint(v)));
                };
                auto idx_at = [&](int row) {
                    double t = row_t0 + row * delta;
                    int fr = (int)(std::lower_bound(frame_times.begin(), frame_times.end(), t) - frame_times.begin());
                    return std::max(0, std::min((int)n->frames.size() - 1, fr - n->start_frame));
                };
                double cshift = total_shift - 12 * s.fold;
                double f0 = cell_float(*n, 0, s, cshift);
                int cur_cell = std::max(1, std::min(96, (int)std::nearbyint(f0)));
                Cell & c0 = put(n->start_row);
                c0.note = cur_cell;
                c0.inst = s.index;
                int ci = 0;
                for (size_t i = 0; i < n->level.size(); i++) if (n->level[i] >= 0.5 * n->peak) { ci = (int)i; break; }
                int last_vol = vol_at(idx_at(n->start_row));
                c0.vol = 0x10 + last_vol;
                int last_pan = n->pan[ci];
                if (!o.no_pan) { c0.eff = 0x08; c0.par = std::max(1, std::min(255, last_pan)); }
                double last_pitch = f0;
                for (int row = n->start_row + 1; row < end_row; row++)
                {
                    int i = idx_at(row);
                    int v = vol_at(i);
                    double pf = cell_float(*n, i, s, cshift);
                    Cell * c = nullptr;
                    if (std::abs(v - last_vol) >= std::max(2, (int)(0.08 * last_vol)))
                    {
                        c = &put(row);
                        c->vol = 0x10 + v;
                        last_vol = v;
                    }
                    if (std::fabs(pf - last_pitch) >= o.bend_threshold)
                    {
                        if (!c) c = &put(row);
                        int nc = std::max(1, std::min(96, (int)std::nearbyint(pf)));
                        if (nc != cur_cell) { c->note = nc; c->eff = 0x03; c->par = 0xFF; cur_cell = nc; }
                        last_pitch = pf;
                    }
                }
                if (!has_next || end_row < nxt)
                    if (end_row <= last_row)
                    {
                        Cell & off = put(end_row);
                        if (!off.note) off.note = 97;
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
        b.str("USF2XM Converter    ", 20, ' ');
        b.le16(0x0104); b.le32(276);
        b.le16(npat); b.le16(0); b.le16(nch); b.le16(npat); b.le16((int)sample_list.size()); b.le16(1);
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
        for (Sample * s : sample_list)
        {
            std::vector<int16_t> data;
            for (int16_t v : s->data)
                data.push_back((int16_t)std::max(-32768.0, std::min(32767.0, std::nearbyint(v * (32767.0 / s->peak)))));
            int rel = std::max(-96, std::min(95, (int)std::nearbyint(s->tune)));
            int fine = std::max(-128, std::min(127, (int)std::nearbyint((s->tune - rel) * 128)));
            char nm[40];
            snprintf(nm, sizeof nm, "S%02d %08x", s->index, s->hash);
            std::string name = nm;
            const int ihs = 263;
            b.le32(ihs); b.str(name, 22, 0);
            b.u8(0); b.le16(1); b.le32(40);
            for (int i = 0; i < 96; i++) b.u8(0);
            { uint8_t ve[48] = {0}; ve[2] = 64; ve[4] = 100; ve[6] = 64; b.raw({ve, ve + 48}); }
            { uint8_t pe[48] = {0}; pe[2] = 32; pe[4] = 100; pe[6] = 32; b.raw({pe, pe + 48}); }
            b.raw({2, 2, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0});
            b.le16(0);
            for (int i = 0; i < ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2); i++) b.u8(0);
            int ls = s->loop.has ? s->loop.start : 0, ll = s->loop.has ? s->loop.len : 0;
            b.le32((uint32_t)data.size() * 2); b.le32(ls * 2); b.le32(ll * 2);
            b.u8(64); b.u8((uint8_t)(int8_t)fine); b.u8((s->loop.has ? 1 : 0) | 0x10); b.u8(128); b.u8((uint8_t)(int8_t)rel);
            b.u8(0);
            b.str(name, 22, 0);
            int prev = 0;
            for (int16_t v : data)
            {
                int d = (((int)v - prev + 32768) % 65536 + 65536) % 65536 - 32768;
                b.le16(d & 0xFFFF);
                prev = v;
            }
        }
        if (!b.save(base + ".xm")) return "cannot write " + base + ".xm";
    }

    if (want_midi)
    {
        struct ME { int tick, order, ch; std::vector<uint8_t> data; };
        std::vector<ME> ev;
        std::vector<Sample *> order;
        for (Sample * s : sample_list) if (!s->pitched) order.push_back(s);
        std::stable_sort(order.begin(), order.end(), [](Sample * a, Sample * b) { return a->centroid < b->centroid; });
        std::map<uint32_t, int> drum_of;
        for (size_t i = 0; i < order.size(); i++)
            drum_of[order[i]->hash] = DRUM_NOTES[std::min<size_t>(11, i * 12 / std::max<size_t>(1, order.size()))];
        std::map<int, int> ch_prog;
        static const int ch_list[15] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15};
        std::set<int> init_done;
        const double tps = 960.0;
        std::vector<Note *> by_t = notes;
        std::stable_sort(by_t.begin(), by_t.end(), [](Note * a, Note * b) { return a->start_t < b->start_t; });
        for (Note * n : by_t)
        {
            Sample & s = *n->sample;
            int t0m = (int)std::nearbyint(n->start_t * tps);
            int t1m = std::max(t0m + 20, (int)std::nearbyint(n->end_t * tps));
            int mch, key;
            if (!s.pitched) { mch = 9; key = drum_of[s.hash]; }
            else
            {
                mch = ch_list[n->channel % 15];
                double f0 = 49 + s.m1 + 12 * std::log2(std::max(n->ratio[0], 1e-4));
                key = (int)std::nearbyint(f0 + 11) + o.transpose + 12 * o.octave;
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
                int prog = GM_PROGRAMS[(s.index - 1) % 24];
                if (!ch_prog.count(mch) || ch_prog[mch] != prog)
                {
                    ev.push_back({t0m, 1, mch, {(uint8_t)(0xC0 | mch), (uint8_t)prog}});
                    ch_prog[mch] = prog;
                }
            }
            double pk = n->peak * s.peak / 32768.0 / norm;
            int vel = std::max(1, std::min(127, (int)std::nearbyint(pk * 110)));
            ev.push_back({t0m, 2, mch, {(uint8_t)(0xB0 | mch), 10, (uint8_t)std::max(0, std::min(127, n->pan[0] / 2))}});
            ev.push_back({t0m, 3, mch, {(uint8_t)(0x90 | mch), (uint8_t)key, (uint8_t)vel}});
            if (s.pitched)
            {
                bool have_b = false, have_e = false;
                double last_b = 0;
                int last_e = 0;
                for (size_t i = 0; i < n->frames.size(); i++)
                {
                    int fr = n->frames[i];
                    int t = (int)std::nearbyint(frame_times[std::min<size_t>(fr, frame_times.size() - 1)] * tps);
                    if (t >= t1m) break;
                    double pf = 49 + s.m1 + 12 * std::log2(std::max(n->ratio[i], 1e-4));
                    double bend = std::max(-12.0, std::min(12.0, pf - (key - 11 - o.transpose - 12 * o.octave)));
                    if (!have_b || std::fabs(bend - last_b) >= 0.06)
                    {
                        int v = (int)(8192 + bend / 12.0 * 8191);
                        ev.push_back({t, 2, mch, {(uint8_t)(0xE0 | mch), (uint8_t)(v & 0x7F), (uint8_t)((v >> 7) & 0x7F)}});
                        last_b = bend; have_b = true;
                    }
                    int e = std::max(1, std::min(127, (int)std::nearbyint(n->level[i] / std::max(n->peak, 1e-6) * 127)));
                    if (!have_e || std::abs(e - last_e) >= 4)
                    {
                        ev.push_back({t, 2, mch, {(uint8_t)(0xB0 | mch), 11, (uint8_t)e}});
                        last_e = e; have_e = true;
                    }
                }
            }
            ev.push_back({t1m, 0, mch, {(uint8_t)(0x80 | mch), (uint8_t)key, 0}});
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

} // namespace usfexp
