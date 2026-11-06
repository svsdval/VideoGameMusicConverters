// AY-3-8910 / YM2149 - порт AYEmulator из pt32xm-midi-wav.py / psg2xm-midi-wav.py
// (потоковый вариант: один кадр регистров -> PCM). Общий для плагинов pt3 и psg.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <vector>

namespace ay {

using Regs = std::array<uint8_t, 14>;

static const double AY_DAC[32] = {
    0.0,0.0,0.00999465934234,0.00999465934234,
    0.0144502937362,0.0144502937362,0.0210574502174,0.0210574502174,
    0.0307011520562,0.0307011520562,0.0455481803616,0.0455481803616,
    0.0644998855573,0.0644998855573,0.107362478065,0.107362478065,
    0.126588845655,0.126588845655,0.20498970016,0.20498970016,
    0.292210269322,0.292210269322,0.372838941024,0.372838941024,
    0.492530708782,0.492530708782,0.635324635691,0.635324635691,
    0.805584802014,0.805584802014,1.0,1.0};
static const double YM_DAC[32] = {
    0.0,0.0,0.00465400167849,0.00772106507973,
    0.0109559777218,0.0139620050355,0.0169985503929,0.0200198367285,
    0.024368657969,0.029694056611,0.0350652323186,0.0403906309606,
    0.0485389486534,0.0583352407111,0.0680552376593,0.0777752346075,
    0.0925154497597,0.111085679408,0.129747463188,0.148485542077,
    0.17666895552,0.211551079576,0.246387426566,0.281101701381,
    0.333730067903,0.400427252613,0.467383840696,0.53443198291,
    0.635172045472,0.75800717174,0.879926756695,1.0};

inline const std::vector<uint8_t> & lfsr()
{
    static std::vector<uint8_t> t;
    if (t.empty())
    {
        t.resize(131071);
        uint32_t n = 1;
        for (auto & o : t)
        {
            uint32_t bit = (n ^ (n >> 3)) & 1;
            n = (n >> 1) | (bit << 16);
            o = n & 1;
        }
    }
    return t;
}

inline int env_level(int shape, int64_t s)
{
    int64_t seg = s / 32;
    int pos = (int)(s % 32);
    bool up = shape & 4;
    if (!(shape & 8)) return seg == 0 ? (up ? pos : 31 - pos) : 0;
    bool alt = shape & 2, hold = shape & 1;
    if (hold)
    {
        int fin = (up != alt) ? 31 : 0;
        int first = up ? pos : 31 - pos;
        return seg == 0 ? first : fin;
    }
    bool up_seg = up != (alt && (seg & 1));
    return up_seg ? pos : 31 - pos;
}

class Chip
{
public:
    Chip(int clock = 1773400, int sample_rate = 44100, bool ym = false, double fps = 50.0,
         int layout = 0)
        : sr(sample_rate), dac(ym ? YM_DAC : AY_DAC), lf(&lfsr())
    {
        ticks_per_frame = clock / 8.0 / fps;
        samples_per_frame = sample_rate / fps;
        // layout: 0 = ABC, 1 = ACB, 2 = mono
        static const double abc[3][2] = {{0.95, 0.27}, {0.65, 0.62}, {0.62, 1.03}};
        static const int acb[3] = {0, 2, 1};
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 2; j++)
                pan[i][j] = layout == 2 ? 0.7 : abc[layout == 1 ? acb[i] : i][j];
        regs.fill(0);
        hp_a = 1.0 / (1.0 + 2 * M_PI * 20.0 / sample_rate);
    }

    // Рендерит один кадр, добавляет int16 стерео в out. Возвращает число сэмплов.
    int render_frame(const Regs & r, std::vector<int16_t> & out, double gain = 0.45)
    {
        regs = r;
        if (r[13] != 0xFF) { eshape = r[13] & 0x0F; ecnt = 0; estep = 0; }

        double t1 = tick_pos + ticks_per_frame;
        int n = (int)t1 - (int)tick_pos;
        tick_pos = t1;
        tmp.assign((size_t)n * 2, 0.0f);
        double sum[3] = {0, 0, 0}, sum2[3] = {0, 0, 0};

        int mixer = r[7];
        int noise_p = std::max(1, r[6] & 0x1F) * 2;
        int ep = std::max(1, (r[12] << 8) | r[11]);
        int tp[3];
        for (int i = 0; i < 3; i++)
        {
            tp[i] = std::max(1, ((r[i * 2 + 1] & 0x0F) << 8) | r[i * 2]);
            tcnt[i] = std::min(tcnt[i], tp[i] - 1);
        }

        for (int k = 0; k < n; k++)
        {
            if (++ncnt >= noise_p) { nidx = (int)((nidx + ncnt / noise_p) % lf->size()); ncnt %= noise_p; }
            int noise = (*lf)[nidx];
            if (++ecnt >= ep) { estep = std::min<int64_t>(estep + ecnt / ep, 1 << 20); ecnt %= ep; }
            int env = env_level(eshape, estep);
            double l = 0, rr = 0;
            for (int i = 0; i < 3; i++)
            {
                if (++tcnt[i] >= tp[i]) { tcnt[i] %= tp[i]; tout[i] ^= 1; }
                bool gate = (tout[i] || ((mixer >> i) & 1)) && (noise || ((mixer >> (i + 3)) & 1));
                int va = r[8 + i];
                double v;
                if (va & 0x10) v = dac[gate ? env : 0];
                else v = gate ? dac[(va & 0x0F) * 2 + 1] : 0.0;
                sum[i] += v; sum2[i] += v * v;
                l += v * pan[i][0];
                rr += v * pan[i][1];
            }
            tmp[2 * k] = (float)l;
            tmp[2 * k + 1] = (float)rr;
        }

        for (int i = 0; i < 3; i++)
        {
            double m = n ? sum[i] / n : 0.0;
            chan_rms[i] = n ? std::sqrt(std::max(0.0, sum2[i] / n - m * m)) : 0.0;
        }

        double s1 = sample_pos + samples_per_frame;
        int ns = (int)s1 - (int)sample_pos;
        sample_pos = s1;
        int prev = 0;
        for (int i = 1; i <= ns; i++)
        {
            int e = (int)std::lround((double)i * n / std::max(1, ns));
            e = std::clamp(e, 0, n);
            double sl = 0, sr_ = 0;
            for (int k = prev; k < e; k++) { sl += tmp[2 * k]; sr_ += tmp[2 * k + 1]; }
            int cnt = std::max(1, e - prev);
            prev = e;
            double x[2] = {sl / cnt, sr_ / cnt}, y[2];
            for (int c = 0; c < 2; c++)      // убираем постоянную составляющую (ЦАП однополярный)
            {
                y[c] = hp_a * (hp_y[c] + x[c] - hp_x[c]);
                hp_x[c] = x[c];
                hp_y[c] = y[c];
                double s = std::clamp(y[c] * gain * 32767.0, -32768.0, 32767.0);
                out.push_back((int16_t)s);
            }
        }
        return ns;
    }

    double chan_rms[3] = {0, 0, 0};   // AC rms каналов в последнем кадре

private:
    int sr;
    const double * dac;
    const std::vector<uint8_t> * lf;
    double ticks_per_frame, samples_per_frame, pan[3][2];
    double tick_pos = 0, sample_pos = 0, hp_a, hp_x[2] = {0, 0}, hp_y[2] = {0, 0};
    int tcnt[3] = {0, 0, 0}, tout[3] = {0, 0, 0};
    int ncnt = 0, nidx = 0, ecnt = 0, eshape = 0;
    int64_t estep = 0;
    Regs regs;
    std::vector<float> tmp;
};

} // namespace ay
