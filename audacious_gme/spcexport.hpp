// Встроенный экспорт SPC -> XM / MIDI: порт spc2xm-midi-wav.py (без WAV) поверх spcemu.hpp.
// Результат совпадает с Python-версией (проверено на примерах).
#pragma once
#include "../audacious_usf/usfexport.hpp"
#include "spcemu.hpp"

namespace spcexp {

using usfexp::Cell;
using usfexp::Note;
using cd = std::complex<double>;

constexpr int FRAME_SAMPLES = 128;
constexpr double DSP_RATE = 32000.0;

struct Options
{
    int octave = 0, transpose = 0, compact = 1, channels = 32;
    double seconds = 0;                // 0 = длина из ID666, иначе 120
    double release_threshold = 0.04, bend_threshold = 0.75, max_row = 0.06;
    double norm_scale = 1.0, limit_knee = 0.6;
    bool no_pan = false, no_gauss = false;
};

// numpy.add.reduce для float64: попарное суммирование
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

// ── БПФ любой длины: степень двойки + Bluestein ──
inline void fft_pow2(std::vector<cd> & a, bool inv)
{
    size_t n = a.size();
    for (size_t i = 1, j = 0; i < n; i++)
    {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(a[i], a[j]);
    }
    for (size_t len = 2; len <= n; len <<= 1)
    {
        double ang = 2 * M_PI / len * (inv ? 1 : -1);
        std::vector<cd> w(len / 2);
        for (size_t k = 0; k < len / 2; k++) w[k] = std::polar(1.0, ang * k);
        for (size_t i = 0; i < n; i += len)
            for (size_t k = 0; k < len / 2; k++)
            {
                cd u = a[i + k], v = a[i + k + len / 2] * w[k];
                a[i + k] = u + v;
                a[i + k + len / 2] = u - v;
            }
    }
    if (inv) for (auto & v : a) v /= (double)n;
}

inline void fft_any(std::vector<cd> & a, bool inv)
{
    size_t n = a.size();
    if (n <= 1) return;
    if ((n & (n - 1)) == 0) { fft_pow2(a, inv); return; }
    size_t m = 1;
    while (m < 2 * n - 1) m <<= 1;
    double sgn = inv ? 1 : -1;
    std::vector<cd> w(n), A(m, 0), B(m, 0);
    for (size_t k = 0; k < n; k++)
    {
        double ang = sgn * M_PI * (double)((unsigned long long)k * k % (2 * n)) / n;
        w[k] = std::polar(1.0, ang);
    }
    for (size_t k = 0; k < n; k++) A[k] = a[k] * w[k];
    B[0] = std::conj(w[0]);
    for (size_t k = 1; k < n; k++) B[k] = B[m - k] = std::conj(w[k]);
    fft_pow2(A, false);
    fft_pow2(B, false);
    for (size_t k = 0; k < m; k++) A[k] *= B[k];
    fft_pow2(A, true);
    for (size_t k = 0; k < n; k++) a[k] = A[k] * w[k] / (inv ? (double)n : 1.0);
}

// ── гауссов ФНЧ DSP ──
inline std::vector<double> gauss_response(size_t n_bins)
{
    std::vector<double> acc(n_bins, 0.0);
    const int * G = spcemu::GAUSS;
    double step = n_bins > 1 ? M_PI / (double)(n_bins - 1) : 0.0;
    for (size_t b = 0; b < n_bins; b++)
    {
        double w = b + 1 == n_bins ? M_PI : step * (double)b;
        cd z[4];
        for (int k = 0; k < 4; k++) z[k] = std::polar(1.0, -w * k);
        double s = 0;
        for (int off = 0; off < 512; off += 2)
        {
            double t0 = G[off] / 2048.0, t1 = G[off + 1] / 2048.0, t2 = G[511 - off] / 2048.0, t3 = G[510 - off] / 2048.0;
            s += std::abs(t0 * z[0] + t1 * z[1] + t2 * z[2] + t3 * z[3]);
        }
        acc[b] = s / 256.0;
    }
    return acc;
}

inline std::vector<int16_t> gauss_filter(const std::vector<int16_t> & data, bool has_loop, int ls, int ll)
{
    std::vector<double> ext;
    if (has_loop)
    {
        for (int i = 0; i < ls; i++) ext.push_back(data[i]);
        for (int r = 0; r < 6; r++) for (int i = 0; i < ll; i++) ext.push_back(data[ls + i]);
    }
    else
    {
        for (auto v : data) ext.push_back(v);
        for (int i = 0; i < 64; i++) ext.push_back(0);
    }
    size_t n = ext.size();
    std::vector<cd> sp(n);
    for (size_t i = 0; i < n; i++) sp[i] = ext[i];
    fft_any(sp, false);
    size_t nb = n / 2 + 1;
    std::vector<double> h = gauss_response(nb);
    double h0 = h[0];
    std::vector<cd> full(n);
    for (size_t k = 0; k < nb; k++) full[k] = sp[k] * (h[k] / h0);
    full[0] = cd(full[0].real(), 0);
    if (n % 2 == 0) full[n / 2] = cd(full[n / 2].real(), 0);
    for (size_t k = 1; k < (n + 1) / 2; k++) full[n - k] = std::conj(full[k]);
    fft_any(full, true);
    std::vector<int16_t> out;
    auto cl = [&](size_t i) {
        double v = std::nearbyint(full[i].real());
        return (int16_t)std::max(-32768.0, std::min(32767.0, v));
    };
    if (has_loop)
    {
        for (int i = 0; i < ls; i++) out.push_back(cl(i));
        for (int i = 0; i < ll; i++) out.push_back(cl(ls + 2 * ll + i));
    }
    else
        for (size_t i = 0; i < data.size(); i++) out.push_back(cl(i));
    return out;
}

// ── сэмпл ──
struct SSample : usfexp::Sample
{
    int loop_mode;                 // 0 - искать, 1 - известна (li_start, li_len), 2 - нет петли
    int li_start = 0, li_len = 0;
    int orig = 0;
    bool gauss = true;
    SSample(uint32_t h, std::vector<int16_t> pcm, int mode) : usfexp::Sample(h, std::move(pcm)), loop_mode(mode) {}

    void analyse2()
    {
        std::vector<int16_t> s = data, seg;
        int ls = 0, lp = 0;
        bool has = false;
        if (loop_mode == 1) { has = true; ls = li_start; lp = li_len; }
        else if (loop_mode == 0) has = usfexp::detect_loop(s, ls, lp);
        if (has)
        {
            loop.has = true; loop.start = ls; loop.len = lp;
            if ((int)s.size() > ls + lp) s.resize(ls + lp);
            data = s;
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
        if (gauss && !data.empty()) data = gauss_filter(data, loop.has, loop.start, loop.len);
        int mx = 0;
        for (int16_t v : data) mx = std::max(mx, std::abs((int)v));
        peak = mx ? mx : 1.0;
        std::vector<double> sd(seg.begin(), seg.end());
        double per, conf;
        bool ok = usfexp::detect_period(sd, per, conf);
        confidence = conf;
        if (ok && conf >= 0.6) { period = per; pitched = true; }
        if (seg.size() > 64)
        {
            size_t n = seg.size();
            std::vector<cd> a(n);
            for (size_t i = 0; i < n; i++) a[i] = sd[i] * (0.5 - 0.5 * std::cos(2 * M_PI * i / (n - 1)));
            usfexp::fft_rec(a);
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
};

// ── разбор трассы -> ноты ──
struct Parsed
{
    std::vector<Note> notes;
    std::map<int, int> meta;
    std::vector<double> frame_times;
};

constexpr uint32_t NOISE_HASH = 0xFFFFFFFFu;

inline Parsed parse_trace(const spcemu::Trace & tr, double release_ratio)
{
    Parsed P;
    std::vector<Note> done;
    std::map<int, Note> cur;
    std::map<int, int> quiet;
    int max_frame = 0;
    auto close = [&](int v, int end_frame) {
        auto it = cur.find(v);
        if (it == cur.end()) return;
        Note n = it->second;
        cur.erase(it);
        n.end_frame = std::max(n.start_frame + 1, end_frame);
        n.end_t = std::max(n.start_t + 0.001, n.end_frame * FRAME_SAMPLES / DSP_RATE);
        done.push_back(std::move(n));
    };
    for (const auto & e : tr.events)
    {
        if (e.is_k)
        {
            const auto & k = e.k;
            close(k.v, (int)(k.smp / FRAME_SAMPLES) + 1);
            P.meta.insert({k.start, k.loop});
            Note n(k.v, (int)(k.smp / FRAME_SAMPLES), (uint32_t)k.start);
            n.start_t = k.smp / DSP_RATE;
            n.end_t = n.start_t;
            cur.erase(k.v);
            cur.insert({k.v, n});
            quiet[k.v] = 0;
        }
        else
        {
            const auto & f = e.f;
            max_frame = std::max(max_frame, f.frame);
            auto it = cur.find(f.v);
            if (it == cur.end()) continue;
            Note & n = it->second;
            if (!n.frames.empty() && f.frame - n.frames.back() > 1)
            {
                close(f.v, n.frames.back() + 1);
                continue;
            }
            if (f.env <= 0 || f.pitch <= 0 || f.kon > 0) continue;
            if (f.non) n.hash = NOISE_HASH;
            int vl = std::abs(f.vl), vr = std::abs(f.vr);
            double lv = f.env / 2048.0 * std::max(vl, vr) / 128.0;
            int tot = vl + vr;
            int pan = tot ? (int)std::nearbyint(255.0 * vr / tot) : 128;
            n.frames.push_back(f.frame);
            n.ratio.push_back(f.pitch / 4096.0);
            n.level.push_back(lv);
            n.pan.push_back(pan);
            n.peak = std::max(n.peak, lv);
            n.end_frame = f.frame + 1;
            if (lv < std::max(0.002, release_ratio * n.peak) && n.frames.size() > 1)
            {
                quiet[f.v]++;
                if (quiet[f.v] >= 2) close(f.v, f.frame - 1);
            }
            else quiet[f.v] = 0;
        }
    }
    std::vector<int> left;
    for (auto & kv : cur) left.push_back(kv.first);
    for (int v : left) close(v, cur.at(v).end_frame);
    for (auto & n : done) if (!n.frames.empty() && n.peak > 0.002) P.notes.push_back(n);
    std::stable_sort(P.notes.begin(), P.notes.end(), [](const Note & a, const Note & b) {
        return std::tie(a.start_t, a.key) < std::tie(b.start_t, b.key); });
    for (int i = 0; i < max_frame + 4; i++) P.frame_times.push_back(i * (double)FRAME_SAMPLES / DSP_RATE);
    return P;
}

// ── BRR ──
inline void brr_extent(const uint8_t * ram, int start, int & nblocks, bool & loops)
{
    int a = start;
    for (int n = 1; n <= 4096; n++)
    {
        int h = ram[a];
        if (h & 1) { nblocks = n; loops = (h & 2) != 0; return; }
        a = (a + 9) & 0xFFFF;
    }
    nblocks = 4096; loops = false;
}

// mode: 0 - искать петлю, 1 - известна, 2 - нет
inline std::vector<int16_t> make_sample_data(const std::vector<int16_t> & stream, const uint8_t * ram, int start,
                                             int loop_addr, int & mode, int & lstart, int & llen)
{
    int nblocks;
    bool loops;
    brr_extent(ram, start, nblocks, loops);
    int total = nblocks * 16;
    auto head = [&](int n) { return std::vector<int16_t>(stream.begin(), stream.begin() + std::min<int>(n, stream.size())); };
    if (!loops) { mode = 2; return head(total); }
    int off = (loop_addr - start) & 0xFFFF;
    if (off >= 9 * nblocks || off % 9) { mode = 0; return stream; }
    int ls = off / 9 * 16;
    int L = total - ls;
    if (L < 1) { mode = 2; return head(total); }
    int passes = (int)stream.size() - ls < 0 ? -1 : ((int)stream.size() - ls) / L;
    if (passes < 1) { mode = 2; return stream; }
    int k = 0;
    bool found = false;
    for (int cand = 1; cand <= 2; cand++)
        if (passes >= cand + 1 && !memcmp(&stream[ls + cand * L], &stream[ls + (cand - 1) * L], L * sizeof(int16_t)))
        {
            k = cand; found = true; break;
        }
    if (!found) k = std::min(passes - 1, 2);
    mode = 1; lstart = ls + k * L; llen = L;
    return head(ls + (k + 1) * L);
}

// numpy.random.RandomState(1).randint(-1, 1, 8192) * 2 + 1  (MT19937, младший бит каждого слова)
inline std::vector<int16_t> noise_pcm()
{
    uint32_t mt[624];
    mt[0] = 1;
    for (int i = 1; i < 624; i++) mt[i] = 1812433253u * (mt[i - 1] ^ (mt[i - 1] >> 30)) + i;
    int idx = 624;
    auto next = [&]() -> uint32_t {
        if (idx >= 624)
        {
            for (int k = 0; k < 624; k++)
            {
                uint32_t y = (mt[k] & 0x80000000u) | (mt[(k + 1) % 624] & 0x7FFFFFFFu);
                mt[k] = mt[(k + 397) % 624] ^ (y >> 1) ^ ((y & 1) ? 0x9908B0DFu : 0);
            }
            idx = 0;
        }
        uint32_t y = mt[idx++];
        y ^= y >> 11;
        y ^= (y << 7) & 0x9D2C5680u;
        y ^= (y << 15) & 0xEFC60000u;
        y ^= y >> 18;
        return y;
    };
    std::vector<int16_t> out(8192);
    for (auto & v : out)
    {
        int r = -1 + (int)(next() & 1);
        v = (int16_t)((r * 2 + 1) * 12000);
    }
    return out;
}

inline double soft_limit(double x, double knee)
{
    if (x <= knee || knee >= 1.0) return std::min(x, 1.0);
    double w = 1.0 - knee;
    return knee + w * std::tanh((x - knee) / w);
}

struct SpcInfo { std::string title, game; double length = 0; };

inline std::string id666_text(const uint8_t * p, size_t n)
{
    std::string s;
    for (size_t i = 0; i < n && p[i]; i++) s += (char)p[i];
    size_t a = 0, b = s.size();
    while (a < b && isspace((unsigned char)s[a])) a++;
    while (b > a && isspace((unsigned char)s[b - 1])) b--;
    return s.substr(a, b - a);
}

inline bool parse_num(const std::string & t, double & v)
{
    if (t.empty()) { v = 0; return true; }
    char * e;
    v = strtod(t.c_str(), &e);
    return *e == 0;
}

inline SpcInfo read_info(const std::vector<uint8_t> & d)
{
    SpcInfo s;
    if (d.size() >= 0xB0 && d[0x23] == 26)
    {
        s.title = id666_text(&d[0x2E], 0x20);
        s.game = id666_text(&d[0x4E], 0x20);
        double len = 0, fade = 0;
        if (parse_num(id666_text(&d[0xA9], 3), len) && parse_num(id666_text(&d[0xAC], 5), fade))
        {
            s.length = len;
            fade /= 1000.0;
            if (s.length) s.length += fade;
        }
    }
    return s;
}

inline std::string convert(const spcemu::Trace & tr, const uint8_t * ram, const std::string & base,
                           const std::string & title, bool want_xm, bool want_midi, const Options & o)
{
    using usfexp::MAX_XM_CHANNELS;
    using usfexp::MAX_XM_INSTRUMENTS;
    Parsed P = parse_trace(tr, o.release_threshold);
    if (P.notes.empty()) return "no notes found (empty or silent SPC?)";
    const auto & frame_times = P.frame_times;
    const double rate = DSP_RATE;
    std::vector<Note> & notes_v = P.notes;

    std::vector<uint32_t> seen_order;
    std::map<uint32_t, int> cnt;
    std::map<uint32_t, double> first;
    for (auto & n : notes_v)
    {
        if (!cnt.count(n.hash)) { seen_order.push_back(n.hash); first[n.hash] = n.start_t; }
        cnt[n.hash]++;
    }
    std::vector<uint32_t> used = seen_order;
    std::stable_sort(used.begin(), used.end(), [&](uint32_t a, uint32_t b) { return cnt[a] > cnt[b]; });
    if ((int)used.size() > MAX_XM_INSTRUMENTS) used.resize(MAX_XM_INSTRUMENTS);
    std::stable_sort(used.begin(), used.end(), [&](uint32_t a, uint32_t b) { return first[a] < first[b]; });

    std::map<uint32_t, std::unique_ptr<SSample>> samples;
    std::vector<SSample *> sample_list;
    for (uint32_t h : used)
    {
        std::unique_ptr<SSample> s;
        if (h == NOISE_HASH)
        {
            s.reset(new SSample(h, noise_pcm(), 1));
            s->li_start = 0; s->li_len = 8192;
        }
        else
        {
            auto it = tr.streams.find((int)h);
            if (it == tr.streams.end() || it->second.size() < 16) continue;
            int mode, ls = 0, ll = 0;
            int la = 0;
            auto mi = P.meta.find((int)h);
            if (mi != P.meta.end()) la = mi->second;
            std::vector<int16_t> pcm = make_sample_data(it->second, ram, (int)h, la, mode, ls, ll);
            s.reset(new SSample(h, std::move(pcm), mode));
            s->li_start = ls; s->li_len = ll;
        }
        s->gauss = !o.no_gauss;
        s->index = (int)samples.size() + 1;
        s->use_count = cnt[h];
        s->analyse2();
        s->set_pitch(rate);
        s->orig = s->index;
        sample_list.push_back(s.get());
        samples[h] = std::move(s);
    }
    std::vector<Note *> notes;
    for (auto & n : notes_v) if (samples.count(n.hash)) { n.sample = samples[n.hash].get(); notes.push_back(&n); }
    if (notes.empty()) return "no notes with sample data found";

    double norm_max = 0;
    for (Note * n : notes) norm_max = std::max(norm_max, n->peak * n->sample->peak / 32768.0);
    // Python: (peaks.max() if len(peaks) else 1.0) or 1.0
    if (norm_max == 0) norm_max = 1.0;
    const double norm = norm_max * o.norm_scale;

    const int total_shift = o.transpose + 12 * o.octave;
    std::map<uint32_t, std::vector<Note *>> by_sample;
    for (Note * n : notes) by_sample[n->hash].push_back(n);
    for (SSample * s : sample_list)
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
            std::vector<double> cs, sn;
            for (double f : fr) { cs.push_back(std::cos(f * 2 * M_PI)); sn.push_back(std::sin(f * 2 * M_PI)); }
            // numpy mean (попарное суммирование)
            auto mean = [](const std::vector<double> & v) { return np_sum(v.data(), v.size()) / v.size(); };
            double c = mean(cs), sc = mean(sn);
            if (std::hypot(c, sc) > 0.5) s->m1 -= std::atan2(sc, c) / (2 * M_PI);
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
        s->tune = 12.0 * std::log2(rate / usfexp::XM_C4_RATE) - s->m1 - total_shift + 12 * fold;
    }

    // ── сетка строк ──
    std::vector<double> onsets;
    for (Note * n : notes) onsets.push_back(n->start_t);
    double t0 = 0, coh;
    double true_delta = usfexp::detect_row_interval(onsets, t0, coh, o.max_row);
    true_delta *= o.compact;
    int bpm, speed;
    usfexp::interval_to_bpm_speed(true_delta, bpm, speed);
    for (Note * n : notes)
    {
        n->start_row = (int)std::nearbyint((n->start_t - t0) / true_delta);
        n->end_row = (int)std::nearbyint((n->end_t - t0) / true_delta);
    }
    int shift = (*std::min_element(notes.begin(), notes.end(), [](Note * a, Note * b) { return a->start_row < b->start_row; }))->start_row;
    for (Note * n : notes) { n->start_row -= shift; n->end_row = std::max(n->start_row + 1, n->end_row - shift); }
    const double delta = true_delta;
    const double row_t0 = t0 + shift * true_delta;
    std::vector<Note *> by_row = notes;
    int nch = usfexp::allocate_channels(by_row, std::max(2, std::min(MAX_XM_CHANNELS, o.channels)));
    nch = std::max(2, nch + (nch & 1));
    nch = std::min(nch, MAX_XM_CHANNELS);

    using usfexp::Buf;
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
                usfexp::Sample & s = *n->sample;
                bool has_next = k + 1 < lst.size();
                int nxt = has_next ? lst[k + 1]->start_row : 0;
                int end_row = !has_next ? n->end_row : std::min(n->end_row, nxt);
                n->end_row = end_row;
                last_row = std::max(last_row, end_row);
                auto put = [&](int row) -> Cell & { return grid[{row, ch}]; };
                auto vol_at = [&](int i) {
                    double v = soft_limit(n->level[i] * s.peak / 32768.0 / norm, o.limit_knee) * 64.0;
                    return std::max(1, std::min(64, (int)std::nearbyint(v)));
                };
                auto idx_at = [&](int row) {
                    double t = row_t0 + row * delta;
                    int fr = (int)(std::lower_bound(frame_times.begin(), frame_times.end(), t) - frame_times.begin());
                    return std::max(0, std::min((int)n->frames.size() - 1, fr - n->start_frame));
                };
                double cshift = total_shift - 12 * s.fold;
                double f0 = usfexp::cell_float(*n, 0, s, cshift);
                int cur_cell = std::max(1, std::min(96, (int)std::nearbyint(f0)));
                Cell & c0 = put(n->start_row);
                c0.note = cur_cell;
                c0.inst = s.index;
                int ci = 0;
                for (size_t i = 0; i < n->level.size(); i++) if (n->level[i] >= 0.5 * n->peak) { ci = (int)i; break; }
                int last_vol = vol_at(idx_at(n->start_row));
                c0.vol = 0x10 + last_vol;
                int last_pan = n->pan[ci];
                if (!o.no_pan && last_pan != 128) { c0.eff = 0x08; c0.par = std::max(1, std::min(255, last_pan)); }
                double last_pitch = f0;
                for (int row = n->start_row + 1; row < end_row; row++)
                {
                    int i = idx_at(row);
                    int v = vol_at(i);
                    double pf = usfexp::cell_float(*n, i, s, cshift);
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
        int start_bpm = usfexp::tempo_schedule(bpm, speed, true_delta, last_row + 2, sched);
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
        for (SSample * s : sample_list)
        {
            std::vector<int16_t> data;
            for (int16_t v : s->data)
                data.push_back((int16_t)std::max(-32768.0, std::min(32767.0, std::nearbyint(v * (32767.0 / s->peak)))));
            int rel = std::max(-96, std::min(95, (int)std::nearbyint(s->tune)));
            int fine = std::max(-128, std::min(127, (int)std::nearbyint((s->tune - rel) * 128)));
            char nm[48];
            if (s->hash == NOISE_HASH) snprintf(nm, sizeof nm, "S%02d -0000001", s->orig);
            else snprintf(nm, sizeof nm, "S%02d %08x", s->orig, s->hash);
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
        std::vector<SSample *> order;
        for (SSample * s : sample_list) if (!s->pitched) order.push_back(s);
        std::stable_sort(order.begin(), order.end(), [](SSample * a, SSample * b) { return a->centroid < b->centroid; });
        std::map<uint32_t, int> drum_of;
        for (size_t i = 0; i < order.size(); i++)
            drum_of[order[i]->hash] = usfexp::DRUM_NOTES[std::min<size_t>(11, i * 12 / std::max<size_t>(1, order.size()))];
        std::map<int, int> ch_prog;
        static const int ch_list[15] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15};
        std::set<int> init_done;
        const double tps = 960.0;
        std::vector<Note *> by_t = notes;
        std::stable_sort(by_t.begin(), by_t.end(), [](Note * a, Note * b) { return a->start_t < b->start_t; });
        for (Note * n : by_t)
        {
            usfexp::Sample & s = *n->sample;
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
                int prog = usfexp::GM_PROGRAMS[(s.index - 1) % 24];
                if (!ch_prog.count(mch) || ch_prog[mch] != prog)
                {
                    ev.push_back({t0m, 1, mch, {(uint8_t)(0xC0 | mch), (uint8_t)prog}});
                    ch_prog[mch] = prog;
                }
            }
            double pk = n->peak * s.peak / 32768.0 / norm_max;
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
            for (auto & e : t) { usfexp::vlq(body, e.tick - prev); body.raw(e.data); prev = e.tick; }
            usfexp::vlq(body, 0); body.raw({0xFF, 0x2F, 0x00});
            f.str("MTrk", 4, 0); f.be32((uint32_t)body.d.size()); f.raw(body.d);
        }
        if (!f.save(base + ".mid")) return "cannot write " + base + ".mid";
    }
    return "";
}

// path -> base.xm / base.mid. Возвращает "" или текст ошибки.
inline std::string export_spc(const std::string & path, const std::string & out_base, bool xm, bool midi,
                              const Options & o)
{
    std::vector<uint8_t> d;
    FILE * f = fopen(path.c_str(), "rb");
    if (!f) return "cannot open " + path;
    int c;
    while ((c = fgetc(f)) != EOF) d.push_back((uint8_t)c);
    fclose(f);
    if (!spcemu::Spc::valid(d)) return path + ": not an SPC file";
    SpcInfo info = read_info(d);
    double seconds = o.seconds > 0 ? o.seconds : (info.length > 0 ? info.length : 120.0);
    std::string title = !info.title.empty() ? info.title : !info.game.empty() ? info.game : "";
    if (title.empty())
    {
        size_t sl = out_base.rfind('/');
        title = sl == std::string::npos ? out_base : out_base.substr(sl + 1);
    }
    spcemu::Trace tr;
    if (!spcemu::trace(d, seconds, tr)) return path + ": emulation failed";
    return convert(tr, d.data() + 0x100, out_base, title, xm, midi, o);
}

} // namespace spcexp
