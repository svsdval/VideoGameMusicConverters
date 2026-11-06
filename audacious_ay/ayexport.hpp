// Встроенный экспорт AY-музыки (PT3 / PSG) в XM и MIDI на C++.
// Порт extract_notes_ay / XMWriter / MIDIWriter из pt32xm-midi-wav.py и psg2xm-midi-wav.py.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include "ay.hpp"

namespace ayexp {

enum Mode { M_TONE = 0, M_NOISE, M_BUZZER, M_MIXED, M_ENV_TONE, M_NONE = -1 };
enum Ev { E_NOTE_ON, E_PITCH, E_VOL, E_NOTE_OFF };
constexpr double P_SQ = 0.62;
constexpr int INST_TONE = 1, INST_ENVTONE = 2, INST_BUZZ = 3, INST_MIXED = 4, INST_NOISE = 5, INST_BUZZ_TRI = 6;

struct NoteEvent
{
    int frame = 0, channel = 0, ay_channel = 0, mode = M_NONE;
    Ev type = E_NOTE_OFF;
    int note = 0, instrument = 1, volume = 64;
};

using ChannelMap = std::map<std::pair<int, int>, int>;

inline ChannelMap preset(int id)
{
    // id: 0 default, 1 compact, 2 split-all, 3 minimal, 4 buzzer-split ; порядок режимов: tone,noise,buzzer,mixed,env
    static const int T[5][3][5] = {
        // для каждого ay-канала: {tone, noise, buzzer, mixed, env_tone}
        {{0, 6, 3, 0, 0}, {1, 7, 4, 1, 1}, {2, 8, 5, 2, 2}},        // default
        {{0, 0, 3, 0, 0}, {1, 1, 3, 1, 1}, {2, 2, 3, 2, 2}},        // compact
        {{0, 12, 9, 3, 6}, {1, 13, 10, 4, 7}, {2, 14, 11, 5, 8}},   // split-all
        {{0, 0, 0, 0, 0}, {1, 1, 1, 1, 1}, {2, 2, 2, 2, 2}},        // minimal
        {{0, 6, 3, 0, 0}, {1, 6, 4, 1, 1}, {2, 6, 5, 2, 2}}};       // buzzer-split
    ChannelMap m;
    id = std::max(0, std::min(4, id));
    for (int a = 0; a < 3; a++)
        for (int mo = 0; mo < 5; mo++) m[{a, mo}] = T[id][a][mo];
    return m;
}

// ── инструменты (формы волн AY) ──
struct Inst
{
    std::string name;
    std::vector<int16_t> data;
    int rel;
    bool loop;
    double rms;
};

inline std::vector<int16_t> pcm(const std::vector<double> & v, double peak)
{
    double mean = 0;
    for (double x : v) mean += x;
    mean /= v.size();
    double m = 0;
    for (double x : v) m = std::max(m, std::fabs(x - mean));
    if (m == 0) m = 1.0;
    std::vector<int16_t> out;
    for (double x : v) out.push_back((int16_t)std::nearbyint((x - mean) * (peak * 32767.0 / m)));
    return out;
}

inline const std::vector<Inst> & instruments(bool ym)
{
    static std::vector<Inst> cache[2];
    auto & c = cache[ym];
    if (!c.empty()) return c;
    const double * dac = ym ? ay::YM_DAC : ay::AY_DAC;
    const auto & lf = ay::lfsr();
    std::vector<double> sq;
    for (int i = 0; i < 16; i++) sq.push_back(1);
    for (int i = 0; i < 16; i++) sq.push_back(-1);
    std::vector<double> saw, tri, noise, mixed;
    for (int i = 0; i < 32; i++) saw.push_back(dac[31 - i]);
    tri = saw;
    for (int i = 0; i < 32; i++) tri.push_back(dac[i]);
    for (int i = 0; i < 32768; i++) noise.push_back(lf[i]);
    for (int i = 0; i < 32; i++) mixed.push_back((double)lf[(i * 7) % 4000]);
    for (int i = 0; i < 32; i++) mixed.push_back(0.0);
    auto mk = [&](const char * name, std::vector<int16_t> d, int rel) {
        double s = 0;
        for (int16_t x : d) s += (double)x * x;
        c.push_back({name, d, rel, true, std::sqrt(s / d.size()) / 32767.0});
    };
    auto square = pcm(sq, P_SQ);
    mk("AY Tone", square, 0);
    mk("AY Env+Tone", square, 0);
    mk("AY Buzzer saw", pcm(saw, 0.95), 0);
    mk("AY Tone+Noise", pcm(mixed, 0.95), 12);
    mk("AY Noise", pcm(noise, P_SQ), 0);
    mk("AY Buzzer tri", pcm(tri, 0.95), 12);
    return c;
}

inline int xm_volume(double r, int inst, bool ym, double scale)
{
    double rms = instruments(ym)[inst - 1].rms;
    double v = 64.0 * r * (2.0 * P_SQ) / rms * scale;
    return std::max(1, std::min(64, (int)std::nearbyint(v)));
}

inline int clamp_xm(int n) { return (n >= 1 && n <= 96) ? n : 0; }   // 0 = None

inline int freq_to_xm(double f)
{
    if (f < 15 || f > 20000) return 0;
    return clamp_xm((int)std::nearbyint(69 + 12 * std::log2(f / 440.0)) - 11);
}

inline int rate_to_xm(double rate)
{
    return std::max(1, std::min(96, (int)std::nearbyint(49 + 12 * std::log2(std::max(rate, 1.0) / 8363.0))));
}

struct Info { int mode = M_NONE, note = 0, inst = 0; bool ok = false; };

inline Info classify(const ay::Regs & r, int ac, double clock)
{
    int mixer = r[7], vr = r[8 + ac];
    bool tone_on = !((mixer >> ac) & 1), noise_on = !((mixer >> (ac + 3)) & 1), env_on = vr & 0x10;
    int period = ((r[ac * 2 + 1] & 0x0F) << 8) | r[ac * 2];
    Info i;
    if (tone_on)
    {
        int note = freq_to_xm(clock / (16.0 * std::max(1, period)));
        if (!note) return i;
        i.ok = true; i.note = note;
        if (env_on) { i.mode = M_ENV_TONE; i.inst = INST_ENVTONE; }
        else if (noise_on) { i.mode = M_MIXED; i.inst = INST_MIXED; }
        else { i.mode = M_TONE; i.inst = INST_TONE; }
        return i;
    }
    if (noise_on)
    {
        int np = std::max(1, r[6] & 0x1F);
        i.ok = true; i.mode = M_NOISE; i.note = rate_to_xm(clock / (16.0 * np)); i.inst = INST_NOISE;
    }
    return i;
}

struct Options
{
    int clock = 1773400;
    bool ym = false;
    double scale = 1.0;
    int map_preset = 0;
    int transpose = 0;     // полутонов (включая октавы*12)
    double fps = 50.0;
};

// frames[i], started[i][3]
inline std::vector<NoteEvent> extract(const std::vector<ay::Regs> & frames,
                                      const std::vector<std::array<bool, 3>> & started,
                                      const ChannelMap & cmap, const Options & o)
{
    const int REPEATING = (1 << 8) | (1 << 0xA) | (1 << 0xC) | (1 << 0xE);
    ay::Chip emu(o.clock, 44100, o.ym, 50.0, 0);
    int nx = 0;
    for (auto & kv : cmap) nx = std::max(nx, kv.second + 1);
    struct Cur { bool on = false; int note = 0, inst = 0, vol = 0; };
    std::vector<Cur> cur(nx);
    int last_xc[3] = {-1, -1, -1};
    std::vector<NoteEvent> ev;
    int shape = 0;
    std::vector<int16_t> scratch;
    auto push = [&](int frame, int ch, int ac, int mode, Ev t, int note, int inst, int vol) {
        NoteEvent e; e.frame = frame; e.channel = ch; e.ay_channel = ac; e.mode = mode;
        e.type = t; e.note = note; e.instrument = inst; e.volume = vol;
        ev.push_back(e);
    };
    int frame = -1;
    for (size_t fi = 0; fi < frames.size(); fi++)
    {
        frame = (int)fi;
        const ay::Regs & regs = frames[fi];
        scratch.clear();
        emu.render_frame(regs, scratch);
        if (regs[13] != 0xFF) shape = regs[13] & 0x0F;
        int ep = std::max(1, (regs[12] << 8) | regs[11]);
        for (int ac = 0; ac < 3; ac++)
        {
            Info info = classify(regs, ac, o.clock);
            int vr = regs[8 + ac], mixer = regs[7];
            if (!info.ok && (vr & 0x10) && ((mixer >> ac) & 1) && ((mixer >> (ac + 3)) & 1) &&
                ((REPEATING >> shape) & 1))
            {
                bool tri = shape == 0x0A || shape == 0x0E;
                double f = o.clock / ((tri ? 512.0 : 256.0) * ep);
                int n = freq_to_xm(f);
                if (n) { info.ok = true; info.mode = M_BUZZER; info.note = n; info.inst = tri ? INST_BUZZ_TRI : INST_BUZZ; }
            }
            double r = emu.chan_rms[ac];
            int xc_old = last_xc[ac];
            if (!info.ok || r < 1e-4)
            {
                if (xc_old >= 0 && cur[xc_old].on) { push(frame, xc_old, ac, M_NONE, E_NOTE_OFF, 97, 1, 64); cur[xc_old].on = false; }
                last_xc[ac] = -1;
                continue;
            }
            auto it = cmap.find({ac, info.mode});
            int xc = it != cmap.end() ? it->second : ac;
            if (xc_old >= 0 && xc_old != xc && cur[xc_old].on)
            { push(frame, xc_old, ac, M_NONE, E_NOTE_OFF, 97, 1, 64); cur[xc_old].on = false; }
            last_xc[ac] = xc;
            int vol = xm_volume(r, info.inst, o.ym, o.scale);
            Cur & c = cur[xc];
            bool st = fi < started.size() && started[fi][ac];
            bool trig = !c.on || c.inst != info.inst || st ||
                        ((info.mode == M_BUZZER || info.mode == M_ENV_TONE) && regs[13] != 0xFF);
            if (trig)
            {
                push(frame, xc, ac, info.mode, E_NOTE_ON, info.note, info.inst, vol);
                c.on = true; c.note = info.note; c.inst = info.inst; c.vol = vol;
            }
            else
            {
                if (info.note != c.note) { push(frame, xc, ac, info.mode, E_PITCH, info.note, info.inst, vol); c.note = info.note; }
                if (vol != c.vol) { push(frame, xc, ac, info.mode, E_VOL, info.note, info.inst, vol); c.vol = vol; }
            }
        }
    }
    for (int xc = 0; xc < nx; xc++)
        if (cur[xc].on) push(frame + 1, xc, 0, M_NONE, E_NOTE_OFF, 97, 1, 64);
    if (o.transpose)
        for (auto & e : ev)
            if ((e.type == E_NOTE_ON || e.type == E_PITCH) && e.mode != M_NOISE)
                e.note = std::max(1, std::min(96, e.note + o.transpose));
    return ev;
}

// ── запись ──
struct Buf
{
    std::vector<uint8_t> d;
    void u8(int v) { d.push_back((uint8_t)v); }
    void le16(int v) { u8(v & 0xFF); u8((v >> 8) & 0xFF); }
    void le32(uint32_t v) { le16(v & 0xFFFF); le16(v >> 16); }
    void be16(int v) { u8((v >> 8) & 0xFF); u8(v & 0xFF); }
    void be32(uint32_t v) { be16(v >> 16); be16(v & 0xFFFF); }
    void str(const std::string & s, size_t n)
    {
        for (size_t i = 0; i < n; i++) u8(i < s.size() ? ((unsigned char)s[i] < 128 ? s[i] : '?') : 0);
    }
    void zeros(size_t n) { d.insert(d.end(), n, 0); }
    void raw(const std::vector<uint8_t> & v) { d.insert(d.end(), v.begin(), v.end()); }
    bool save(const std::string & fn) const
    {
        FILE * f = fopen(fn.c_str(), "wb");
        if (!f) return false;
        bool ok = fwrite(d.data(), 1, d.size(), f) == d.size();
        return fclose(f) == 0 && ok;
    }
};

struct TN { int note = 0, instrument = 0, volume = 0, effect = 0, param = 0; };

inline bool write_xm(const std::string & fn, const std::vector<NoteEvent> & events, const std::string & title,
                     int nc, bool ym, double fps)
{
    if (nc % 2) nc++;
    nc = std::max(4, std::min(32, nc));
    int speed = 2, bpm = (int)std::nearbyint(5 * fps);
    bool slide_ok = bpm <= 255;
    if (!slide_ok) { speed = 1; bpm = std::max(32, std::min(255, (int)std::nearbyint(2.5 * fps))); }
    const auto & insts = instruments(ym);

    std::map<std::pair<int, int>, TN> cells;
    for (const auto & e : events)
    {
        if (e.channel >= nc) continue;
        TN & c = cells[{e.frame, e.channel}];
        if (e.type == E_NOTE_ON)
        {
            c.note = std::max(1, std::min(96, e.note)); c.instrument = e.instrument; c.volume = 0x10 + e.volume;
            c.effect = c.param = 0;
        }
        else if (e.type == E_PITCH)
        {
            if (c.note != 97 && !(c.note && c.instrument))
            {
                c.note = std::max(1, std::min(96, e.note));
                if (slide_ok) { c.effect = 3; c.param = 0xFF; }
                else c.instrument = e.instrument;
                c.volume = c.volume ? c.volume : 0x10 + e.volume;
            }
        }
        else if (e.type == E_VOL)
        {
            if (c.note != 97 && !(c.note && c.instrument)) c.volume = 0x10 + e.volume;
        }
        else if (!c.note) c.note = 97;
    }
    const int rpp = 128;
    int max_row = 0;
    for (auto & kv : cells) max_row = std::max(max_row, kv.first.first);
    max_row += 1;
    int np = std::max(1, std::min(256, (max_row + rpp - 1) / rpp));
    if ((max_row + rpp - 1) / rpp > 256) fprintf(stderr, "XM: song is longer than 256 patterns, truncated\n");

    Buf b;
    b.str("Extended Module: ", 17);
    b.str(title, 20);
    b.u8(0x1a);
    b.str("PT32XM Converter    ", 20);
    b.le16(0x0104); b.le32(276); b.le16(np); b.le16(0); b.le16(nc); b.le16(np);
    b.le16(std::min<int>(insts.size(), 128)); b.le16(1); b.le16(speed); b.le16(bpm);
    for (int i = 0; i < 256; i++) b.u8(i < np ? i : 0);
    for (int pi = 0; pi < np; pi++)
    {
        Buf pk;
        for (int ri = 0; ri < rpp; ri++)
            for (int ch = 0; ch < nc; ch++)
            {
                auto it = cells.find({pi * rpp + ri, ch});
                TN nd = it != cells.end() ? it->second : TN();
                bool hn = nd.note > 0, hi = nd.instrument > 0, hv = nd.volume > 0, hf = nd.effect > 0, hp = nd.param > 0;
                if (!(hn || hi || hv || hf || hp)) { pk.u8(0x80); continue; }
                pk.u8(0x80 | (hn ? 1 : 0) | (hi ? 2 : 0) | (hv ? 4 : 0) | (hf ? 8 : 0) | (hp ? 16 : 0));
                if (hn) pk.u8(nd.note & 0xFF);
                if (hi) pk.u8(nd.instrument & 0xFF);
                if (hv) pk.u8(nd.volume & 0xFF);
                if (hf) pk.u8(nd.effect & 0xFF);
                if (hp) pk.u8(nd.param & 0xFF);
            }
        b.le32(9); b.u8(0); b.le16(rpp); b.le16((int)pk.d.size()); b.raw(pk.d);
    }
    for (size_t k = 0; k < insts.size() && k < 128; k++)
    {
        const Inst & in = insts[k];
        const int ihs = 263;
        b.le32(ihs); b.str(in.name, 22);
        b.u8(0); b.le16(1); b.le32(40); b.zeros(96);
        { uint8_t ve[48] = {0}; ve[2] = 64; ve[4] = 100; ve[6] = 64; b.raw({ve, ve + 48}); }
        { uint8_t pe[48] = {0}; pe[2] = 32; pe[4] = 100; pe[6] = 32; b.raw({pe, pe + 48}); }
        b.raw({2, 2, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0});
        b.le16(0);
        b.zeros(ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2));
        uint32_t n = (uint32_t)in.data.size();
        b.le32(n * 2); b.le32(0); b.le32(in.loop ? n * 2 : 0);
        b.u8(64); b.u8(0); b.u8((in.loop ? 1 : 0) | 0x10); b.u8(128); b.u8((uint8_t)(int8_t)in.rel);
        b.u8(0);
        b.str(in.name, 22);
        int prev = 0;
        for (int16_t v : in.data)
        {
            int d = (((int)v - prev + 32768) % 65536 + 65536) % 65536 - 32768;
            b.le16(d & 0xFFFF);
            prev = v;
        }
    }
    return b.save(fn);
}

inline void vlq(Buf & b, int v)
{
    v = std::max(0, v);
    std::vector<uint8_t> r = {(uint8_t)(v & 0x7F)};
    v >>= 7;
    while (v) { r.push_back((uint8_t)((v & 0x7F) | 0x80)); v >>= 7; }
    std::reverse(r.begin(), r.end());
    b.raw(r);
}

inline bool write_midi(const std::string & fn, const std::vector<NoteEvent> & events, int bpm, double fps,
                       const std::string & title)
{
    const int tpqn = 480;
    double tpf = tpqn * (double)bpm / (60.0 * fps);
    int usec = (int)(60000000 / bpm);
    std::map<int, std::vector<const NoteEvent *>> by_ch;
    for (auto & e : events) by_ch[e.channel].push_back(&e);
    using TEv = std::pair<int, std::vector<uint8_t>>;
    std::vector<std::vector<TEv>> tracks;
    std::vector<TEv> t0;
    t0.push_back({0, {0xFF, 0x51, 0x03, (uint8_t)((usec >> 16) & 0xFF), (uint8_t)((usec >> 8) & 0xFF), (uint8_t)(usec & 0xFF)}});
    if (!title.empty())
    {
        std::string td;
        for (char c : title.substr(0, 127)) td += (unsigned char)c < 128 ? c : '?';
        std::vector<uint8_t> m = {0xFF, 0x01, (uint8_t)td.size()};
        m.insert(m.end(), td.begin(), td.end());
        t0.push_back({0, m});
    }
    tracks.push_back(t0);
    std::vector<int> freech;
    for (int c = 0; c < 16; c++) if (c != 9) freech.push_back(c);
    static const std::map<int, int> PROGRAM = {{M_TONE, 80}, {M_ENV_TONE, 81}, {M_BUZZER, 38}, {M_MIXED, 87}, {M_NOISE, 0}};
    for (auto & kv : by_ch)
    {
        auto & evs = kv.second;
        bool any_on = false, only_noise = true;
        for (auto * e : evs)
            if (e->type == E_NOTE_ON) { any_on = true; if (e->mode != M_NOISE) only_noise = false; }
        bool drum = any_on && only_noise;
        int mc;
        if (drum) mc = 9;
        else if (!freech.empty()) { mc = freech.front(); freech.erase(freech.begin()); }
        else break;
        std::vector<TEv> trk;
        int note = -1, prog = -1, expr = -99, last_t = 0;
        auto off = [&](int t) { if (note >= 0) { trk.push_back({t, {(uint8_t)(0x80 | mc), (uint8_t)note, 0}}); note = -1; } };
        auto on = [&](int t, int n, int v) { trk.push_back({t, {(uint8_t)(0x90 | mc), (uint8_t)n, (uint8_t)v}}); note = n; };
        for (auto * e : evs)
        {
            int t = (int)(e->frame * tpf);
            last_t = t;
            if (e->type == E_NOTE_ON)
            {
                off(t);
                int vel = std::max(1, std::min(127, e->volume * 2 - 1));
                if (drum) on(t, e->note >= 75 ? 42 : e->note >= 58 ? 38 : 36, vel);
                else
                {
                    auto pi = PROGRAM.find(e->mode);
                    int p = pi != PROGRAM.end() ? pi->second : 80;
                    if (p != prog) { trk.push_back({t, {(uint8_t)(0xC0 | mc), (uint8_t)p}}); prog = p; }
                    int ex = std::max(1, std::min(127, e->volume * 2 - 1));
                    if (std::abs(ex - expr) >= 1) { trk.push_back({t, {(uint8_t)(0xB0 | mc), 11, (uint8_t)ex}}); expr = ex; }
                    on(t, std::max(0, std::min(127, e->note + 11)), 100);
                }
            }
            else if (e->type == E_PITCH && !drum && note >= 0)
            {
                int n = std::max(0, std::min(127, e->note + 11));
                if (n != note) { off(t); on(t, n, 100); }
            }
            else if (e->type == E_VOL && !drum)
            {
                int ex = std::max(1, std::min(127, e->volume * 2 - 1));
                if (std::abs(ex - expr) >= 4) { trk.push_back({t, {(uint8_t)(0xB0 | mc), 11, (uint8_t)ex}}); expr = ex; }
            }
            else if (e->type == E_NOTE_OFF) off(t);
        }
        off(last_t + 1);
        tracks.push_back(trk);
    }
    Buf f;
    f.str("MThd", 4); f.be32(6); f.be16(1); f.be16((int)tracks.size()); f.be16(tpqn);
    for (auto & t : tracks)
    {
        std::stable_sort(t.begin(), t.end(), [](const TEv & a, const TEv & b) { return a.first < b.first; });
        Buf d;
        int prev = 0;
        for (auto & e : t) { vlq(d, std::max(0, e.first - prev)); d.raw(e.second); prev = e.first; }
        vlq(d, 0); d.raw({0xFF, 0x2F, 0x00});
        f.str("MTrk", 4); f.be32((uint32_t)d.d.size()); f.raw(d.d);
    }
    return f.save(fn);
}

// Полный экспорт. Возвращает "" при успехе, иначе текст ошибки.
inline std::string export_ay(const std::vector<ay::Regs> & frames, const std::vector<std::array<bool, 3>> & started,
                             const std::string & title, const std::string & out_base, bool want_xm, bool want_midi,
                             const Options & o)
{
    if (frames.empty()) return "empty file";
    ChannelMap cmap = preset(o.map_preset);
    int nx = 0;
    for (auto & kv : cmap) nx = std::max(nx, kv.second + 1);
    auto ev = extract(frames, started, cmap, o);
    int note_ons = 0;
    for (auto & e : ev) if (e.type == E_NOTE_ON) note_ons++;
    if (!note_ons) return "no notes found";
    if (want_xm && !write_xm(out_base + ".xm", ev, title, nx, o.ym, o.fps)) return "cannot write " + out_base + ".xm";
    if (want_midi && !write_midi(out_base + ".mid", ev, 125, o.fps, title)) return "cannot write " + out_base + ".mid";
    return "";
}

} // namespace ayexp
