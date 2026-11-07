// Порт spc_pyemu.py (SNES SPC700 + S-DSP, по Game_Music_Emu) на C++ для встроенного экспорта SPC.
// Выдаёт те же события, что spc_tracer/spctrace: старт голоса (K), состояние голосов раз в 128
// отсчётов (F) и декодированный BRR.
#pragma once
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace spcemu {

static const int GAUSS[512] = {
    370, 1305, 366, 1305, 362, 1304, 358, 1304, 354, 1304, 351, 1304, 347, 1304, 343, 1303,
    339, 1303, 336, 1303, 332, 1302, 328, 1302, 325, 1301, 321, 1300, 318, 1300, 314, 1299,
    311, 1298, 307, 1297, 304, 1297, 300, 1296, 297, 1295, 293, 1294, 290, 1293, 286, 1292,
    283, 1291, 280, 1290, 276, 1288, 273, 1287, 270, 1286, 267, 1284, 263, 1283, 260, 1282,
    257, 1280, 254, 1279, 251, 1277, 248, 1275, 245, 1274, 242, 1272, 239, 1270, 236, 1269,
    233, 1267, 230, 1265, 227, 1263, 224, 1261, 221, 1259, 218, 1257, 215, 1255, 212, 1253,
    210, 1251, 207, 1248, 204, 1246, 201, 1244, 199, 1241, 196, 1239, 193, 1237, 191, 1234,
    188, 1232, 186, 1229, 183, 1227, 180, 1224, 178, 1221, 175, 1219, 173, 1216, 171, 1213,
    168, 1210, 166, 1207, 163, 1205, 161, 1202, 159, 1199, 156, 1196, 154, 1193, 152, 1190,
    150, 1186, 147, 1183, 145, 1180, 143, 1177, 141, 1174, 139, 1170, 137, 1167, 134, 1164,
    132, 1160, 130, 1157, 128, 1153, 126, 1150, 124, 1146, 122, 1143, 120, 1139, 118, 1136,
    117, 1132, 115, 1128, 113, 1125, 111, 1121, 109, 1117, 107, 1113, 106, 1109, 104, 1106,
    102, 1102, 100, 1098, 99, 1094, 97, 1090, 95, 1086, 94, 1082, 92, 1078, 90, 1074,
    89, 1070, 87, 1066, 86, 1061, 84, 1057, 83, 1053, 81, 1049, 80, 1045, 78, 1040,
    77, 1036, 76, 1032, 74, 1027, 73, 1023, 71, 1019, 70, 1014, 69, 1010, 67, 1005,
    66, 1001, 65, 997, 64, 992, 62, 988, 61, 983, 60, 978, 59, 974, 58, 969,
    56, 965, 55, 960, 54, 955, 53, 951, 52, 946, 51, 941, 50, 937, 49, 932,
    48, 927, 47, 923, 46, 918, 45, 913, 44, 908, 43, 904, 42, 899, 41, 894,
    40, 889, 39, 884, 38, 880, 37, 875, 36, 870, 36, 865, 35, 860, 34, 855,
    33, 851, 32, 846, 32, 841, 31, 836, 30, 831, 29, 826, 29, 821, 28, 816,
    27, 811, 27, 806, 26, 802, 25, 797, 24, 792, 24, 787, 23, 782, 23, 777,
    22, 772, 21, 767, 21, 762, 20, 757, 20, 752, 19, 747, 19, 742, 18, 737,
    17, 732, 17, 728, 16, 723, 16, 718, 15, 713, 15, 708, 15, 703, 14, 698,
    14, 693, 13, 688, 13, 683, 12, 678, 12, 674, 11, 669, 11, 664, 11, 659,
    10, 654, 10, 649, 10, 644, 9, 640, 9, 635, 9, 630, 8, 625, 8, 620,
    8, 615, 7, 611, 7, 606, 7, 601, 6, 596, 6, 592, 6, 587, 6, 582,
    5, 577, 5, 573, 5, 568, 5, 563, 4, 559, 4, 554, 4, 550, 4, 545,
    4, 540, 3, 536, 3, 531, 3, 527, 3, 522, 3, 517, 2, 513, 2, 508,
    2, 504, 2, 499, 2, 495, 2, 491, 2, 486, 1, 482, 1, 477, 1, 473,
    1, 469, 1, 464, 1, 460, 1, 456, 1, 451, 1, 447, 1, 443, 1, 439,
    0, 434, 0, 430, 0, 426, 0, 422, 0, 418, 0, 414, 0, 410, 0, 405,
    0, 401, 0, 397, 0, 393, 0, 389, 0, 385, 0, 381, 0, 378, 0, 374,
};

static const uint8_t INITIAL_REGS[128] = {
    0x45, 0x8B, 0x5A, 0x9A, 0xE4, 0x82, 0x1B, 0x78, 0x00, 0x00, 0xAA, 0x96, 0x89, 0x0E, 0xE0, 0x80,
    0x2A, 0x49, 0x3D, 0xBA, 0x14, 0xA0, 0xAC, 0xC5, 0x00, 0x00, 0x51, 0xBB, 0x9C, 0x4E, 0x7B, 0xFF,
    0xF4, 0xFD, 0x57, 0x32, 0x37, 0xD9, 0x42, 0x22, 0x00, 0x00, 0x5B, 0x3C, 0x9F, 0x1B, 0x87, 0x9A,
    0x6F, 0x27, 0xAF, 0x7B, 0xE5, 0x68, 0x0A, 0xD9, 0x00, 0x00, 0x9A, 0xC5, 0x9C, 0x4E, 0x7B, 0xFF,
    0xEA, 0x21, 0x78, 0x4F, 0xDD, 0xED, 0x24, 0x14, 0x00, 0x00, 0x77, 0xB1, 0xD1, 0x36, 0xC1, 0x67,
    0x52, 0x57, 0x46, 0x3D, 0x59, 0xF4, 0x87, 0xA4, 0x00, 0x00, 0x7E, 0x44, 0x9C, 0x4E, 0x7B, 0xFF,
    0x75, 0xF5, 0x06, 0x97, 0x10, 0xC3, 0x24, 0xBB, 0x00, 0x00, 0x7B, 0x7A, 0xE0, 0x60, 0x12, 0x0F,
    0xF7, 0x74, 0x1C, 0xE5, 0x39, 0x3D, 0x73, 0xC1, 0x00, 0x00, 0x7A, 0xB3, 0xFF, 0x4E, 0x7B, 0xFF,
};

static const uint8_t CYCLE_PACKED[128] = {
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x54, 0x68, 0x48, 0x47, 0x45, 0x56, 0x55, 0x65, 0x22, 0x46,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x54, 0x74, 0x48, 0x47, 0x45, 0x56, 0x55, 0x65, 0x22, 0x38,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x44, 0x54, 0x66, 0x48, 0x47, 0x45, 0x56, 0x55, 0x45, 0x22, 0x43,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x44, 0x54, 0x75, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0x36,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x52, 0x45, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0xC5,
    0x38, 0x47, 0x34, 0x36, 0x26, 0x44, 0x52, 0x44, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0x34,
    0x38, 0x47, 0x45, 0x47, 0x25, 0x64, 0x52, 0x49, 0x48, 0x47, 0x56, 0x67, 0x45, 0x55, 0x22, 0x83,
    0x28, 0x47, 0x34, 0x36, 0x24, 0x53, 0x43, 0x40, 0x48, 0x47, 0x45, 0x56, 0x34, 0x54, 0x22, 0x60,
};

static const int REG_TIMES[128] = {
    -1, 0, -11, -10, -15, -11, -2, -2, 4, 3, 14, 14, 26, 26, 14, 22,
    2, 3, 0, 1, -12, 0, 1, 1, 7, 6, 14, 14, 27, 14, 14, 23,
    5, 6, 3, 4, -1, 3, 4, 4, 10, 9, 14, 14, 26, -5, 14, 23,
    8, 9, 6, 7, 2, 6, 7, 7, 13, 12, 14, 14, 27, -4, 14, 24,
    11, 12, 9, 10, 5, 9, 10, 10, 16, 15, 14, 14, -2, -4, 14, 24,
    14, 15, 12, 13, 8, 12, 13, 13, 19, 18, 14, 14, -2, -36, 14, 24,
    17, 18, 15, 16, 11, 15, 16, 16, 22, 21, 14, 14, 28, -3, 14, 25,
    20, 21, 18, 19, 14, 18, 19, 19, 25, 24, 14, 14, 14, 29, 14, 25,
};
static const int MAX_REG_TIME = 29;
static const int CLOCKS_PER_SAMPLE = 32;
static const int ROM_ADDR = 0xFFC0;

static const int SHIFTS[32] = {13, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 16, 16, 16,
                               0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 11, 11, 11};

enum { ENV_RELEASE = 0, ENV_ATTACK = 1, ENV_DECAY = 2, ENV_SUSTAIN = 3 };

inline int s16(int x) { return (int)(int16_t)(uint16_t)(x & 0xFFFF); }
inline int s8(int x) { return (int)(int8_t)(uint8_t)(x & 0xFF); }

// ── события трассы ──
struct KEv { long smp; int v, srcn, pitch, vl, vr, start, loop; };
struct FEv { int frame, v, srcn, pitch, env, vl, vr, kon, pmon, non; };
struct Ev { bool is_k; KEv k; FEv f; };

struct Trace
{
    std::vector<Ev> events;
    std::map<int, std::vector<int16_t>> streams;   // адрес начала сэмпла -> декодированный BRR
    long total_samples = 0;
};

struct Voice
{
    int buf[24];
    int buf_pos = 0, interp_pos = 0, brr_addr = 0, brr_offset = 1, kon_delay = 0, env_mode = ENV_RELEASE;
    int env = 0, hidden_env = 0, vol0 = 0, vol1 = 0, trace_key = 0, trace_off = 0;
    Voice() { memset(buf, 0, sizeof buf); }
};

class Dsp
{
public:
    uint8_t * ram;
    uint8_t regs[128];
    Voice voices[8];
    int noise = 0x4000;
    int echo_hist[16][2];
    int echo_hist_pos = 0, every_other = 1, kon = 0, new_kon = 0, t_koff = 0;
    int echo_offset = 0, echo_length = 0, phase = 0;
    uint32_t counters[4];
    int sel[32];
    int counter_mask[32];
    int surround_threshold = -0x4000;
    long nsamp = 0;
    Trace * tr = nullptr;
    long max_off_cap = 262144;
    std::map<int, int> max_off;

    static int rate_mask(int rate, int div) { return rate >= div ? rate / div * 8 - 1 : rate - 1; }

    explicit Dsp(uint8_t * r) : ram(r)
    {
        static const int rr[32][2] = {{2, 2}, {2048, 4}, {1536, 3}, {1280, 5}, {1024, 4}, {768, 3}, {640, 5}, {512, 4},
            {384, 3}, {320, 5}, {256, 4}, {192, 3}, {160, 5}, {128, 4}, {96, 3}, {80, 5}, {64, 4}, {48, 3}, {40, 5},
            {32, 4}, {24, 3}, {20, 5}, {16, 4}, {12, 3}, {10, 5}, {8, 4}, {6, 3}, {5, 5}, {4, 4}, {3, 3}, {2, 4}, {1, 4}};
        for (int i = 0; i < 32; i++) counter_mask[i] = rate_mask(rr[i][0], rr[i][1]);
        memset(regs, 0, sizeof regs);
        memset(echo_hist, 0, sizeof echo_hist);
        memset(sel, 0, sizeof sel);
        load(INITIAL_REGS);
    }

    void load(const uint8_t * rg)
    {
        memcpy(regs, rg, 128);
        for (auto & v : voices) { v.brr_offset = 1; v.buf_pos = 0; }
        new_kon = regs[0x4C];
        for (int i = 0; i < 8; i++) update_voice_vol(i * 0x10);
        noise = 0x4000;
        echo_hist_pos = 0;
        every_other = 1;
        echo_offset = 0;
        phase = 0;
        counters[0] = 1; counters[1] = 0; counters[2] = 0xFFFFFFE0u; counters[3] = 0x0B;
        int n = 2;
        for (int i = 1; i < 32; i++)
        {
            sel[i] = n;
            n--;
            if (!n) n = 3;
        }
        sel[0] = 0;
        sel[30] = 2;
    }

    void update_voice_vol(int addr)
    {
        int l = s8(regs[addr]), r = s8(regs[addr + 1]);
        if (l * r < surround_threshold)
        {
            l ^= l >> 7;
            r ^= r >> 7;
        }
        Voice & v = voices[addr >> 4];
        v.vol0 = l;
        v.vol1 = r;
    }

    int read(int addr) { return regs[addr]; }

    void write(int addr, int data)
    {
        regs[addr] = (uint8_t)data;
        int low = addr & 0x0F;
        if (low < 2) update_voice_vol(low ^ addr);
        else if (low == 0x0C)
        {
            if (addr == 0x4C) new_kon = data;
            if (addr == 0x7C) regs[0x7C] = 0;
        }
    }

    void on_brr(int key, int off, const int * s4)
    {
        if (off >= max_off_cap) return;
        auto it = max_off.find(key);
        if (it != max_off.end() && off < it->second) return;
        max_off[key] = off + 4;
        auto & st = tr->streams[key];
        if ((int)st.size() < off + 4) st.resize(off + 4, 0);
        for (int i = 0; i < 4; i++) st[off + i] = (int16_t)s4[i];
    }

    void run(int clock_count)
    {
        int new_phase = phase + clock_count;
        int count = new_phase >> 5;
        phase = new_phase & 31;
        if (!count) return;
        const int dir_ = regs[0x5D] * 0x100;
        const int pmon_reg = regs[0x2D], non_reg = regs[0x3D], eon_reg = regs[0x4D];
        const int slow_gaussian = (pmon_reg >> 1) | non_reg;
        const int noise_rate = regs[0x6C] & 0x1F;
        int mvoll = s8(regs[0x0C]), mvolr = s8(regs[0x1C]);
        
        if (mvoll * mvolr < surround_threshold) mvoll = -mvoll;
        const int flg = regs[0x6C], esa = regs[0x6D];
        const int efb = s8(regs[0x0D]);
        int fir[8];
        for (int i = 0; i < 8; i++) fir[i] = s8(regs[0x0F + i * 0x10]);
        const int noise_mask = counter_mask[noise_rate];
        const int noise_sel = sel[noise_rate];
        while (count)
        {
            count--;
            nsamp++;
            bool report = tr && (nsamp & 127) == 0;
            every_other ^= 1;
            if (every_other)
            {
                new_kon &= ~kon;
                kon = new_kon;
                t_koff = regs[0x5C];
            }
            for (int i = 1; i <= 3; i++)
            {
                uint32_t n = counters[i];
                if (!(n & 7)) n -= 6 - i;
                counters[i] = n - 1;
            }
            if (!(counters[noise_sel] & (uint32_t)noise_mask))
            {
                int fb = (noise << 13) ^ (noise << 14);
                noise = (fb & 0x4000) ^ (noise >> 1);
            }
            const int nz = noise;
            int pmon_input = 0;
            int64_t main_l = 0, main_r = 0, echo_l = 0, echo_r = 0;
            int vbit = 1;
            for (int vi = 0; vi < 8; vi++, vbit <<= 1)
            {
                Voice & v = voices[vi];
                int vr = vi * 0x10;
                if (!v.env && !v.kon_delay && v.env_mode == ENV_RELEASE && !(every_other && (kon & vbit)))
                {
                    regs[vr + 8] = 0;
                    regs[vr + 9] = 0;
                    pmon_input = 0;
                    continue;
                }
                int brr_header = ram[v.brr_addr];
                int kon_delay = v.kon_delay;
                int pitch = (regs[vr + 2] | (regs[vr + 3] << 8)) & 0x3FFF;
                if (pmon_reg & vbit) pitch += ((pmon_input >> 5) * pitch) >> 10;
                int srcn = regs[vr + 4];
                kon_delay--;
                if (kon_delay >= 0)
                {
                    v.kon_delay = kon_delay;
                    if (kon_delay == 4)
                    {
                        v.brr_addr = ram[(dir_ + srcn * 4) & 0xFFFF] | (ram[(dir_ + srcn * 4 + 1) & 0xFFFF] << 8);
                        v.brr_offset = 1;
                        v.buf_pos = 0;
                        brr_header = 0;
                        v.trace_key = v.brr_addr;
                        v.trace_off = 0;
                        if (tr)
                        {
                            Ev e;
                            e.is_k = true;
                            e.k = {nsamp - (vi == 0 ? 1 : 0), vi, srcn, (regs[vr + 2] | (regs[vr + 3] << 8)) & 0x3FFF,
                                   v.vol0, v.vol1, v.brr_addr,
                                   ram[(dir_ + srcn * 4 + 2) & 0xFFFF] | (ram[(dir_ + srcn * 4 + 3) & 0xFFFF] << 8)};
                            tr->events.push_back(e);
                        }
                    }
                    v.env = 0;
                    v.hidden_env = 0;
                    v.interp_pos = (kon_delay & 3) ? 0x4000 : 0;
                    pitch = 0;
                }
                int env = v.env;
                int output = 0;
                regs[vr + 8] = (env >> 4) & 0xFF;
                if (env)
                {
                    int offset = (v.interp_pos >> 3) & 0x1FE;
                    int p0 = v.buf_pos + (v.interp_pos >> 12);
                    int i0 = v.buf[p0], i1 = v.buf[p0 + 1], i2 = v.buf[p0 + 2], i3 = v.buf[p0 + 3];
                    int f0 = GAUSS[offset], f1 = GAUSS[offset + 1];
                    int r1 = GAUSS[510 - offset + 1], r0 = GAUSS[510 - offset];
                    if (!(slow_gaussian & vbit))
                    {
                        output = (f0 * i0 + f1 * i1 + r1 * i2 + r0 * i3) >> 11;
                        output = (output * env) >> 11;
                    }
                    else
                    {
                        output = s16(nz * 2);
                        if (!(non_reg & vbit))
                        {
                            output = (f0 * i0) >> 11;
                            output += (f1 * i1) >> 11;
                            output += (r1 * i2) >> 11;
                            output = s16(output);
                            output += (r0 * i3) >> 11;
                            if (output > 32767) output = 32767;
                            else if (output < -32768) output = -32768;
                            output &= ~1;
                        }
                        output = ((output * env) >> 11) & ~1;
                    }
                    int l = output * v.vol0, r = output * v.vol1;
                    main_l += l;
                    main_r += r;
                    if (eon_reg & vbit) { echo_l += l; echo_r += r; }
                }
                pmon_input = output;
                regs[vr + 9] = (output >> 8) & 0xFF;
                if (report && (env > 0 || v.kon_delay > 0))
                {
                    Ev e;
                    e.is_k = false;
                    e.f = {(int)(nsamp >> 7), vi, srcn, pitch, env, v.vol0, v.vol1, v.kon_delay,
                           (pmon_reg & vbit) ? 1 : 0, (non_reg & vbit) ? 1 : 0};
                    tr->events.push_back(e);
                }

                if ((flg & 0x80) || (brr_header & 3) == 1)
                {
                    v.env_mode = ENV_RELEASE;
                    env = 0;
                }
                if (every_other)
                {
                    if (t_koff & vbit) v.env_mode = ENV_RELEASE;
                    if (kon & vbit)
                    {
                        v.kon_delay = 5;
                        v.env_mode = ENV_ATTACK;
                        regs[0x7C] &= ~vbit;
                    }
                }
                bool skip_brr = false;
                if (!v.kon_delay)
                {
                    if (v.env_mode == ENV_RELEASE)
                    {
                        env -= 8;
                        v.env = env;
                        if (env <= 0)
                        {
                            v.env = 0;
                            skip_brr = true;
                        }
                    }
                    else
                    {
                        int adsr0 = regs[vr + 5];
                        int env_data = regs[vr + 6];
                        int rate;
                        bool exit_env = false;
                        if (adsr0 >= 0x80)
                        {
                            if (v.env_mode > ENV_DECAY)
                            {
                                env -= 1;
                                env -= env >> 8;
                                rate = env_data & 0x1F;
                                v.hidden_env = env;
                                if (!(counters[sel[rate]] & (uint32_t)counter_mask[rate])) v.env = env;
                                exit_env = true;
                            }
                            else if (v.env_mode == ENV_DECAY)
                            {
                                env -= 1;
                                env -= env >> 8;
                                rate = ((adsr0 >> 3) & 0x0E) + 0x10;
                            }
                            else
                            {
                                rate = (adsr0 & 0x0F) * 2 + 1;
                                env += rate < 31 ? 0x20 : 0x400;
                            }
                        }
                        else
                        {
                            env_data = regs[vr + 7];
                            int mode = env_data >> 5;
                            if (mode < 4)
                            {
                                env = env_data * 0x10;
                                rate = 31;
                            }
                            else
                            {
                                rate = env_data & 0x1F;
                                if (mode == 4) env -= 0x20;
                                else if (mode < 6)
                                {
                                    env -= 1;
                                    env -= env >> 8;
                                }
                                else
                                {
                                    env += 0x20;
                                    if (mode > 6 && v.hidden_env >= 0x600) env += 0x8 - 0x20;
                                }
                            }
                        }
                        if (!exit_env)
                        {
                            if ((env >> 8) == (env_data >> 5) && v.env_mode == ENV_DECAY) v.env_mode = ENV_SUSTAIN;
                            v.hidden_env = env;
                            if (env > 0x7FF || env < 0)
                            {
                                env = env < 0 ? 0 : 0x7FF;
                                if (v.env_mode == ENV_ATTACK) v.env_mode = ENV_DECAY;
                            }
                            if (!(counters[sel[rate]] & (uint32_t)counter_mask[rate])) v.env = env;
                        }
                    }
                }
                if (!skip_brr)
                {
                    int old_pos = v.interp_pos;
                    int interp_pos = (old_pos & 0x3FFF) + pitch;
                    if (interp_pos > 0x7FFF) interp_pos = 0x7FFF;
                    v.interp_pos = interp_pos;
                    if (old_pos >= 0x4000)
                    {
                        int ba = v.brr_addr, bo = v.brr_offset;
                        int nybbles = (ram[(ba + bo) & 0xFFFF] << 8) | ram[(ba + bo + 1) & 0xFFFF];
                        bo += 2;
                        if (bo >= 9)
                        {
                            ba = (ba + 9) & 0xFFFF;
                            if (brr_header & 1)
                            {
                                int sp = srcn * 4 + 2;
                                ba = ram[(dir_ + sp) & 0xFFFF] | (ram[(dir_ + sp + 1) & 0xFFFF] << 8);
                                if (!v.kon_delay) regs[0x7C] |= vbit;
                            }
                            v.brr_addr = ba;
                            bo = 1;
                        }
                        v.brr_offset = bo;
                        int scale = brr_header >> 4;
                        int right_shift = SHIFTS[scale], left_shift = SHIFTS[scale + 16];
                        int filt = brr_header & 0x0C;
                        int pos = v.buf_pos;
                        int decoded[4];
                        for (int q = 0; q < 4; q++)
                        {
                            int n16 = ((nybbles & 0xFFFF) ^ 0x8000) - 0x8000;
                            int s = s16(((n16 >> right_shift) & 0xFFFF) << left_shift);
                            int p1 = v.buf[pos + 11];
                            int p2 = v.buf[pos + 10] >> 1;
                            if (filt >= 8)
                            {
                                s += p1;
                                s -= p2;
                                if (filt == 8)
                                {
                                    s += p2 >> 4;
                                    s += (p1 * -3) >> 6;
                                }
                                else
                                {
                                    s += (p1 * -13) >> 7;
                                    s += (p2 * 3) >> 4;
                                }
                            }
                            else if (filt)
                            {
                                s += p1 >> 1;
                                s += (-p1) >> 5;
                            }
                            if (s > 32767) s = 32767;
                            else if (s < -32768) s = -32768;
                            s = s16(s * 2);
                            v.buf[pos] = v.buf[pos + 12] = s;
                            decoded[q] = s;
                            pos++;
                            nybbles <<= 4;
                        }
                        if (tr) on_brr(v.trace_key, v.trace_off, decoded);
                        v.trace_off += 4;
                        if (pos >= 12) pos = 0;
                        v.buf_pos = pos;
                    }
                }
            }
            // эхо
            int ptr = (esa * 0x100 + echo_offset) & 0xFFFF;
            if (!echo_offset) echo_length = (regs[0x7D] & 0x0F) * 0x800;
            echo_offset += 4;
            if (echo_offset >= echo_length) echo_offset = 0;
            int in_l = s16(ram[ptr] | (ram[(ptr + 1) & 0xFFFF] << 8));
            int in_r = s16(ram[(ptr + 2) & 0xFFFF] | (ram[(ptr + 3) & 0xFFFF] << 8));
            int hp = echo_hist_pos + 1;
            if (hp >= 8) hp = 0;
            echo_hist_pos = hp;
            echo_hist[hp][0] = echo_hist[hp + 8][0] = in_l;
            echo_hist[hp][1] = echo_hist[hp + 8][1] = in_r;
            int64_t il = (int64_t)in_l * fir[7], ir = (int64_t)in_r * fir[7];
            for (int i = 0; i < 7; i++)
            {
                il += (int64_t)echo_hist[hp + i + 1][0] * fir[i];
                ir += (int64_t)echo_hist[hp + i + 1][1] * fir[i];
            }
            if (!(flg & 0x20))
            {
                int64_t l = (echo_l >> 7) + ((il * efb) >> 14);
                int64_t r = (echo_r >> 7) + ((ir * efb) >> 14);
                if (l > 32767) l = 32767; else if (l < -32768) l = -32768;
                if (r > 32767) r = 32767; else if (r < -32768) r = -32768;
                ram[ptr] = l & 0xFF;
                ram[(ptr + 1) & 0xFFFF] = (l >> 8) & 0xFF;
                ram[(ptr + 2) & 0xFFFF] = r & 0xFF;
                ram[(ptr + 3) & 0xFFFF] = (r >> 8) & 0xFF;
            }
            // выход не нужен (WAV не пишем), но состояние эхо/RAM должно обновляться как в оригинале
        }
    }
};

struct Timer
{
    long next_time = 1;
    int prescaler = 0, period = 0, divider = 0, enabled = 0, counter = 0;
};

class Spc
{
public:
    std::unique_ptr<uint8_t[]> ram_mem;
    uint8_t * ram;
    uint8_t regs[16], regs_in[16], rom[64], hi_ram[64];
    int rom_enabled = 0;
    int pc, a, x, y, sp;
    int c = 0, nz = 1, i = 0, h = 0, b = 0, dp = 0, v = 0, n = 0;
    Dsp dsp;
    long cyc = 0, dsp_time = CLOCKS_PER_SAMPLE + 1, cyc_target = 0;
    bool halted = false;
    Timer timers[3];
    int cycles[256];

    static bool valid(const std::vector<uint8_t> & d)
    {
        return d.size() >= 0x10180 && !memcmp(d.data(), "SNES-SPC700 Sound File Data", 27);
    }

    explicit Spc(const std::vector<uint8_t> & d) : ram_mem(new uint8_t[0x10000]), ram(ram_mem.get()), dsp(ram)
    {
        memcpy(ram, d.data() + 0x100, 0x10000);
        memset(regs, 0, sizeof regs);
        memset(regs_in, 0, sizeof regs_in);
        memset(rom, 0, sizeof rom);
        memset(hi_ram, 0, sizeof hi_ram);
        rom[0x3E] = 0xFF;
        rom[0x3F] = 0xC0;
        pc = d[0x25] | (d[0x26] << 8);
        a = d[0x27]; x = d[0x28]; y = d[0x29];
        sp = d[0x2B];
        set_psw(d[0x2A]);
        dsp.load(d.data() + 0x10100);
        memcpy(regs, ram + 0xF0, 16);
        memcpy(regs_in, regs, 16);
        regs_in[1] = regs_in[0xA] = regs_in[0xB] = regs_in[0xC] = 0;
        regs_in[0] = 0;
        enable_rom(regs[1] & 0x80);
        for (int k = 0; k < 3; k++)
        {
            timers[k].period = regs[0xA + k] ? regs[0xA + k] : 256;
            timers[k].enabled = (regs[1] >> k) & 1;
            timers[k].counter = regs_in[0xD + k] & 0x0F;
        }
        int t = 0x100;
        int rate = (16 * t + (t >> 1)) / t;
        timers[2].prescaler = rate;
        timers[1].prescaler = rate << 3;
        timers[0].prescaler = rate << 3;
        clear_echo();
        for (int k = 0; k < 128; k++) { cycles[2 * k] = CYCLE_PACKED[k] >> 4; cycles[2 * k + 1] = CYCLE_PACKED[k] & 15; }
    }

    void set_psw(int p)
    {
        c = p & 1;
        nz = (p & 2) ? 0 : 1;
        i = (p >> 2) & 1;
        h = (p >> 3) & 1;
        b = (p >> 4) & 1;
        dp = (p & 0x20) ? 0x100 : 0;
        v = (p >> 6) & 1;
        n = (p >> 7) & 1;
    }
    int get_psw() const
    {
        return c | (nz ? 0 : 2) | (i << 2) | (h << 3) | (b << 4) | (dp ? 0x20 : 0) | (v << 6) | (n << 7);
    }

    void clear_echo()
    {
        const uint8_t * r = dsp.regs;
        if (!(r[0x6C] & 0x20))
        {
            int addr = 0x100 * r[0x6D];
            int end = std::min(0x10000, addr + 0x800 * (r[0x7D] & 0x0F));
            if (end > addr) memset(ram + addr, 0xFF, end - addr);
        }
    }

    void enable_rom(int enable)
    {
        if (rom_enabled != enable)
        {
            rom_enabled = enable;
            if (enable) memcpy(hi_ram, ram + ROM_ADDR, 64);
            memcpy(ram + ROM_ADDR, enable ? rom : hi_ram, 64);
        }
    }

    Timer & run_timer(Timer & t, long time)
    {
        if (time < t.next_time) return t;
        long elapsed = (time - t.next_time) / t.prescaler + 1;
        t.next_time += (long)t.prescaler * elapsed;
        if (t.enabled)
        {
            long remain = ((t.period - t.divider - 1) & 0xFF) + 1;
            long divider = t.divider + elapsed;
            long over = elapsed - remain;
            if (over >= 0)
            {
                long nn = over / t.period;
                t.counter = (int)((t.counter + 1 + nn) & 0x0F);
                divider = over - nn * t.period;
            }
            t.divider = (int)(divider & 0xFF);
        }
        return t;
    }

    void run_dsp(long time, int offset)
    {
        long count = time - offset - dsp_time;
        if (count >= 0)
        {
            long clock_count = (count & ~31L) + 32;
            dsp_time += clock_count;
            dsp.run((int)clock_count);
        }
    }

    int rd(int addr)
    {
        if (addr < 0xF0 || addr >= 0x100) return ram[addr];
        int reg = addr - 0xF0;
        if (reg >= 0xD)
        {
            Timer & t = timers[reg - 0xD];
            if (cyc >= t.next_time) run_timer(t, cyc);
            int res = t.counter;
            t.counter = 0;
            return res;
        }
        int res = regs_in[reg];
        if (reg == 2) res = regs[2];
        else if (reg == 3)
        {
            run_dsp(cyc, REG_TIMES[regs[2] & 0x7F]);
            res = dsp.read(regs[2] & 0x7F);
        }
        return res;
    }

    void wr(int addr, int data)
    {
        ram[addr] = (uint8_t)data;
        if (addr >= 0xF0)
        {
            int reg = addr - 0xF0;
            if (reg < 16)
            {
                regs[reg] = (uint8_t)data;
                if (reg == 3)
                {
                    int aa = regs[2];
                    run_dsp(cyc, aa < 128 ? REG_TIMES[aa] : MAX_REG_TIME);
                    if (aa <= 0x7F) dsp.write(aa, data);
                }
                else if (reg != 2 && (reg < 4 || reg > 7)) write_smp_reg(data, reg);
            }
            else if (addr >= ROM_ADDR)
            {
                int k = addr - ROM_ADDR;
                hi_ram[k] = (uint8_t)data;
                if (rom_enabled) ram[addr] = rom[k];
            }
        }
    }

    void write_smp_reg(int data, int reg)
    {
        long time = cyc;
        if (reg >= 0xA && reg <= 0xC)
        {
            Timer & t = timers[reg - 0xA];
            int period = data ? data : 256;
            if (t.period != period)
            {
                run_timer(t, time);
                t.period = period;
            }
        }
        else if (reg >= 0xD && reg <= 0xF) run_timer(timers[reg - 0xD], time - 1).counter = 0;
        else if (reg == 8 || reg == 9) regs_in[reg] = (uint8_t)data;
        else if (reg == 1)
        {
            if (data & 0x10) { regs_in[4] = 0; regs_in[5] = 0; }
            if (data & 0x20) { regs_in[6] = 0; regs_in[7] = 0; }
            for (int k = 0; k < 3; k++)
            {
                Timer & t = timers[k];
                int en = (data >> k) & 1;
                if (t.enabled != en)
                {
                    run_timer(t, time);
                    t.enabled = en;
                    if (en) { t.divider = 0; t.counter = 0; }
                }
            }
            enable_rom(data & 0x80);
        }
    }

    // ── CPU ──
    int u16(int ad) const { return ram[ad & 0xFFFF] | (ram[(ad + 1) & 0xFFFF] << 8); }
    int op1() const { return ram[(pc + 1) & 0xFFFF]; }
    int op2() const { return ram[(pc + 2) & 0xFFFF]; }
    void setnz(int val) { nz = val & 0xFF; n = (val >> 7) & 1; }
    void setnz16(int val) { nz = val & 0xFFFF; n = (val >> 15) & 1; }
    void push(int val) { ram[0x100 + sp] = (uint8_t)(val & 0xFF); sp = (sp - 1) & 0xFF; }
    int pop() { sp = (sp + 1) & 0xFF; return ram[0x100 + sp]; }

    // режимы: возвращают адрес (или -1 для непосредственного) и длину
    enum { M_DP = 0, M_DPX, M_DPY, M_ABS, M_ABSX, M_ABSY, M_X, M_DPXI, M_DPIY, M_IMM };
    int ea_of(int mode, int & ln)
    {
        switch (mode)
        {
        case M_DP: ln = 2; return op1() + dp;
        case M_DPX: ln = 2; return ((op1() + x) & 0xFF) + dp;
        case M_DPY: ln = 2; return ((op1() + y) & 0xFF) + dp;
        case M_ABS: ln = 3; return u16(pc + 1);
        case M_ABSX: ln = 3; return (u16(pc + 1) + x) & 0xFFFF;
        case M_ABSY: ln = 3; return (u16(pc + 1) + y) & 0xFFFF;
        case M_X: ln = 1; return x + dp;
        case M_DPXI: ln = 2; return u16(((op1() + x) & 0xFF) + dp);
        case M_DPIY: ln = 2; return (u16(op1() + dp) + y) & 0xFFFF;
        default: ln = 2; return -1;
        }
    }
    int fetch(int mode, int & ln)
    {
        int ea = ea_of(mode, ln);
        return ea < 0 ? op1() : rd(ea);
    }

    enum { F_OR, F_AND, F_EOR, F_CMP, F_ADC, F_SBC };
    int alu(int f, int aa, int m)
    {
        switch (f)
        {
        case F_OR: return aa | m;
        case F_AND: return aa & m;
        case F_EOR: return aa ^ m;
        case F_CMP: { int r = aa - m; c = r >= 0; setnz(r & 0xFF); return aa; }
        case F_SBC: m ^= 0xFF; /* fallthrough */
        default:
        {
            int r = aa + m + c;
            v = (~(aa ^ m) & (aa ^ r) & 0x80) ? 1 : 0;
            h = ((aa ^ m ^ r) & 0x10) ? 1 : 0;
            c = (r >> 8) & 1;
            r &= 0xFF;
            setnz(r);
            return r;
        }
        }
    }
    bool logic(int f) const { return f == F_OR || f == F_AND || f == F_EOR; }

    enum { R_ASL, R_ROL, R_LSR, R_ROR, R_INC, R_DEC };
    int rmw(int f, int val)
    {
        switch (f)
        {
        case R_ASL: c = (val >> 7) & 1; return (val << 1) & 0xFF;
        case R_ROL: { int r = ((val << 1) | c) & 0xFF; c = (val >> 7) & 1; return r; }
        case R_LSR: c = val & 1; return val >> 1;
        case R_ROR: { int r = (val >> 1) | (c << 7); c = val & 1; return r; }
        case R_INC: return (val + 1) & 0xFF;
        default: return (val - 1) & 0xFF;
        }
    }

    int & reg_ref(int r) { return r == 0 ? a : r == 1 ? x : r == 2 ? y : sp; }

    void branch_rel(bool cond)
    {
        if (cond) pc = (pc + 2 + s8(op1())) & 0xFFFF;
        else { cyc -= 2; pc = (pc + 2) & 0xFFFF; }
    }
    void branch3(bool cond)
    {
        if (cond) pc = (pc + 3 + s8(op2())) & 0xFFFF;
        else { cyc -= 2; pc = (pc + 3) & 0xFFFF; }
    }

    void exec(int op)
    {
        // группы ALU
        int lo = op & 0x1F;
        int grp = op >> 5;         // 0 or,1 and,2 eor,3 cmp,4 adc,5 sbc
        if (op < 0xC0)
        {
            static const int fmap[6] = {F_OR, F_AND, F_EOR, F_CMP, F_ADC, F_SBC};
            int f = fmap[grp];
            int mode = -1;
            switch (lo)
            {
            case 0x04: mode = M_DP; break;
            case 0x05: mode = M_ABS; break;
            case 0x06: mode = M_X; break;
            case 0x07: mode = M_DPXI; break;
            case 0x08: mode = M_IMM; break;
            case 0x14: mode = M_DPX; break;
            case 0x15: mode = M_ABSX; break;
            case 0x16: mode = M_ABSY; break;
            case 0x17: mode = M_DPIY; break;
            }
            if (mode >= 0)
            {
                int ln;
                int m = fetch(mode, ln);
                int r = alu(f, a, m);
                if (f != F_CMP)
                {
                    a = r;
                    if (logic(f)) setnz(r);
                }
                pc = (pc + ln) & 0xFFFF;
                return;
            }
            if (lo == 0x19)
            {
                int m = rd(y + dp);
                int ea = x + dp;
                int r = alu(f, rd(ea), m);
                if (f != F_CMP) { wr(ea, r); if (logic(f)) setnz(r); }
                pc = (pc + 1) & 0xFFFF;
                return;
            }
            if (lo == 0x09)
            {
                int m = rd(op1() + dp);
                int ea = op2() + dp;
                int r = alu(f, rd(ea), m);
                if (f != F_CMP) { wr(ea, r); if (logic(f)) setnz(r); }
                pc = (pc + 3) & 0xFFFF;
                return;
            }
            if (lo == 0x18)
            {
                int m = op1();
                int ea = op2() + dp;
                int r = alu(f, rd(ea), m);
                if (f != F_CMP) { wr(ea, r); if (logic(f)) setnz(r); }
                pc = (pc + 3) & 0xFFFF;
                return;
            }
        }
        int ln;
        switch (op)
        {
        // CMP X / Y
        case 0xC8: case 0x3E: case 0x1E:
        {
            int m = fetch(op == 0xC8 ? M_IMM : op == 0x3E ? M_DP : M_ABS, ln);
            alu(F_CMP, x, m);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0xAD: case 0x7E: case 0x5E:
        {
            int m = fetch(op == 0xAD ? M_IMM : op == 0x7E ? M_DP : M_ABS, ln);
            alu(F_CMP, y, m);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        // загрузки
        case 0xE4: case 0xE5: case 0xE6: case 0xE7: case 0xE8: case 0xF4: case 0xF5: case 0xF6: case 0xF7:
        {
            int mode = op == 0xE4 ? M_DP : op == 0xE5 ? M_ABS : op == 0xE6 ? M_X : op == 0xE7 ? M_DPXI :
                       op == 0xE8 ? M_IMM : op == 0xF4 ? M_DPX : op == 0xF5 ? M_ABSX : op == 0xF6 ? M_ABSY : M_DPIY;
            int m = fetch(mode, ln);
            a = m; setnz(m);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0xF8: case 0xF9: case 0xE9: case 0xCD:
        {
            int m = fetch(op == 0xF8 ? M_DP : op == 0xF9 ? M_DPY : op == 0xE9 ? M_ABS : M_IMM, ln);
            x = m; setnz(m);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0xEB: case 0xFB: case 0xEC: case 0x8D:
        {
            int m = fetch(op == 0xEB ? M_DP : op == 0xFB ? M_DPX : op == 0xEC ? M_ABS : M_IMM, ln);
            y = m; setnz(m);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        // сохранения
        case 0xC4: case 0xC5: case 0xC6: case 0xC7: case 0xD4: case 0xD5: case 0xD6: case 0xD7:
        {
            int mode = op == 0xC4 ? M_DP : op == 0xC5 ? M_ABS : op == 0xC6 ? M_X : op == 0xC7 ? M_DPXI :
                       op == 0xD4 ? M_DPX : op == 0xD5 ? M_ABSX : op == 0xD6 ? M_ABSY : M_DPIY;
            int ea = ea_of(mode, ln);
            wr(ea, a);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0xD8: case 0xD9: case 0xC9:
        {
            int ea = ea_of(op == 0xD8 ? M_DP : op == 0xD9 ? M_DPY : M_ABS, ln);
            wr(ea, x);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0xCB: case 0xDB: case 0xCC:
        {
            int ea = ea_of(op == 0xCB ? M_DP : op == 0xDB ? M_DPX : M_ABS, ln);
            wr(ea, y);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        // пересылки регистров
        case 0x7D: a = x; setnz(a); pc = (pc + 1) & 0xFFFF; return;
        case 0xDD: a = y; setnz(a); pc = (pc + 1) & 0xFFFF; return;
        case 0x5D: x = a; setnz(x); pc = (pc + 1) & 0xFFFF; return;
        case 0xFD: y = a; setnz(y); pc = (pc + 1) & 0xFFFF; return;
        case 0x9D: x = sp; setnz(x); pc = (pc + 1) & 0xFFFF; return;
        case 0xBD: sp = x; pc = (pc + 1) & 0xFFFF; return;
        case 0xAF: wr(x + dp, a); x = (x + 1) & 0xFF; pc = (pc + 1) & 0xFFFF; return;
        case 0xBF: a = rd(x + dp); x = (x + 1) & 0xFF; setnz(a); pc = (pc + 1) & 0xFFFF; return;
        case 0x8F: wr(op2() + dp, op1()); pc = (pc + 3) & 0xFFFF; return;
        case 0xFA: { int val = rd(op1() + dp); wr(op2() + dp, val); pc = (pc + 3) & 0xFFFF; return; }
        // сдвиги, inc/dec
        case 0x0B: case 0x2B: case 0x4B: case 0x6B: case 0xAB: case 0x8B:
        case 0x0C: case 0x2C: case 0x4C: case 0x6C: case 0xAC: case 0x8C:
        case 0x1B: case 0x3B: case 0x5B: case 0x7B: case 0xBB: case 0x9B:
        {
            int f = rmw_f(op);
            int mode = (op & 0x1F) == 0x0B ? M_DP : (op & 0x1F) == 0x0C ? M_ABS : M_DPX;
            int ea = ea_of(mode, ln);
            int r = rmw(f, rd(ea));
            setnz(r);
            wr(ea, r);
            pc = (pc + ln) & 0xFFFF;
            return;
        }
        case 0x1C: case 0x3C: case 0x5C: case 0x7C: case 0xBC: case 0x9C:
            a = rmw(rmw_f(op), a); setnz(a); pc = (pc + 1) & 0xFFFF; return;
        case 0x3D: x = rmw(R_INC, x); setnz(x); pc = (pc + 1) & 0xFFFF; return;
        case 0x1D: x = rmw(R_DEC, x); setnz(x); pc = (pc + 1) & 0xFFFF; return;
        case 0xFC: y = rmw(R_INC, y); setnz(y); pc = (pc + 1) & 0xFFFF; return;
        case 0xDC: y = rmw(R_DEC, y); setnz(y); pc = (pc + 1) & 0xFFFF; return;
        // 16 бит
        case 0xBA:
        {
            int d = op1();
            int lo_ = d + dp, hi_ = ((d + 1) & 0xFF) + dp;
            a = rd(lo_); y = rd(hi_);
            setnz16(a | (y << 8));
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0xDA:
        {
            int d = op1();
            int lo_ = d + dp, hi_ = ((d + 1) & 0xFF) + dp;
            rd(lo_);
            wr(lo_, a);
            wr(hi_, y);
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0x3A: case 0x1A:
        {
            int d = op1();
            int lo_ = d + dp, hi_ = ((d + 1) & 0xFF) + dp;
            int val = (rd(lo_) | (rd(hi_) << 8)) + (op == 0x3A ? 1 : -1);
            wr(lo_, val & 0xFF);
            wr(hi_, (val >> 8) & 0xFF);
            setnz16(val);
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0x7A: case 0x9A:
        {
            int d = op1();
            int lo_ = d + dp, hi_ = ((d + 1) & 0xFF) + dp;
            int m = rd(lo_) | (rd(hi_) << 8);
            int ya = a | (y << 8);
            int r;
            if (op == 0x7A) r = ya + m;
            else { m ^= 0xFFFF; r = ya + m + 1; }
            v = (~(ya ^ m) & (ya ^ r) & 0x8000) ? 1 : 0;
            h = (((ya ^ m ^ r) >> 8) & 0x10) ? 1 : 0;
            c = (r >> 16) & 1;
            r &= 0xFFFF;
            a = r & 0xFF; y = r >> 8;
            setnz16(r);
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0x5A:
        {
            int d = op1();
            int lo_ = d + dp, hi_ = ((d + 1) & 0xFF) + dp;
            int m = rd(lo_) | (rd(hi_) << 8);
            int ya = a | (y << 8);
            int r = ya - m;
            c = r >= 0;
            setnz16(r & 0xFFFF);
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0xCF:
        {
            int r = y * a;
            a = r & 0xFF; y = r >> 8;
            setnz(y);
            pc = (pc + 1) & 0xFFFF;
            return;
        }
        case 0x9E:
        {
            int ya = a | (y << 8);
            v = y >= x;
            h = (y & 15) >= (x & 15);
            if (y < (x << 1))
            {
                if (x) { a = (ya / x) & 0xFF; y = ya % x; }
                else { a = 0xFF; y = 0xFF & ya; }
            }
            else
            {
                int d = 256 - x;
                int t = ya - (x << 9);
                a = (255 - t / d) & 0xFF;
                y = (x + t % d) & 0xFF;
            }
            setnz(a);
            pc = (pc + 1) & 0xFFFF;
            return;
        }
        case 0x9F: a = ((a << 4) | (a >> 4)) & 0xFF; setnz(a); pc = (pc + 1) & 0xFFFF; return;
        case 0xDF:
        {
            int aa = a;
            if (c || aa > 0x99) { aa += 0x60; c = 1; }
            if (h || (aa & 15) > 9) aa += 6;
            a = aa & 0xFF;
            setnz(a);
            pc = (pc + 1) & 0xFFFF;
            return;
        }
        case 0xBE:
        {
            int aa = a;
            if (!c || aa > 0x99) { aa -= 0x60; c = 0; }
            if (!h || (aa & 15) > 9) aa -= 6;
            a = aa & 0xFF;
            setnz(a);
            pc = (pc + 1) & 0xFFFF;
            return;
        }
        // флаги, стек
        case 0x60: c = 0; pc = (pc + 1) & 0xFFFF; return;
        case 0x80: c = 1; pc = (pc + 1) & 0xFFFF; return;
        case 0xED: c ^= 1; pc = (pc + 1) & 0xFFFF; return;
        case 0xE0: v = 0; h = 0; pc = (pc + 1) & 0xFFFF; return;
        case 0x20: dp = 0; pc = (pc + 1) & 0xFFFF; return;
        case 0x40: dp = 0x100; pc = (pc + 1) & 0xFFFF; return;
        case 0xA0: i = 1; pc = (pc + 1) & 0xFFFF; return;
        case 0xC0: i = 0; pc = (pc + 1) & 0xFFFF; return;
        case 0x00: pc = (pc + 1) & 0xFFFF; return;
        case 0x2D: push(a); pc = (pc + 1) & 0xFFFF; return;
        case 0x4D: push(x); pc = (pc + 1) & 0xFFFF; return;
        case 0x6D: push(y); pc = (pc + 1) & 0xFFFF; return;
        case 0xAE: a = pop(); pc = (pc + 1) & 0xFFFF; return;
        case 0xCE: x = pop(); pc = (pc + 1) & 0xFFFF; return;
        case 0xEE: y = pop(); pc = (pc + 1) & 0xFFFF; return;
        case 0x0D: push(get_psw()); pc = (pc + 1) & 0xFFFF; return;
        case 0x8E: set_psw(pop()); pc = (pc + 1) & 0xFFFF; return;
        // переходы
        case 0x10: branch_rel(!n); return;
        case 0x30: branch_rel(n); return;
        case 0x50: branch_rel(!v); return;
        case 0x70: branch_rel(v); return;
        case 0x90: branch_rel(!c); return;
        case 0xB0: branch_rel(c); return;
        case 0xD0: branch_rel(nz != 0); return;
        case 0xF0: branch_rel(nz == 0); return;
        case 0x2F: pc = (pc + 2 + s8(op1())) & 0xFFFF; return;
        case 0x2E: branch3(a != rd(op1() + dp)); return;
        case 0xDE: branch3(a != rd(((op1() + x) & 0xFF) + dp)); return;
        case 0x6E:
        {
            int ea = op1() + dp;
            int val = (rd(ea) - 1) & 0xFF;
            wr(ea, val);
            branch3(val != 0);
            return;
        }
        case 0xFE:
        {
            y = (y - 1) & 0xFF;
            branch_rel(y != 0);
            return;
        }
        case 0x5F: pc = u16(pc + 1); return;
        case 0x1F: pc = u16(u16(pc + 1) + x); return;
        case 0x3F:
        {
            int ret = (pc + 3) & 0xFFFF;
            pc = u16(pc + 1);
            push(ret >> 8);
            push(ret & 0xFF);
            return;
        }
        case 0x4F:
        {
            int ret = (pc + 2) & 0xFFFF;
            pc = 0xFF00 + op1();
            push(ret >> 8);
            push(ret & 0xFF);
            return;
        }
        case 0x6F: { int l = pop(); int hh = pop(); pc = l | (hh << 8); return; }
        case 0x7F: { set_psw(pop()); int l = pop(); int hh = pop(); pc = l | (hh << 8); return; }
        case 0x0F:
        {
            int ret = (pc + 1) & 0xFFFF;
            push(ret >> 8);
            push(ret & 0xFF);
            push(get_psw());
            b = 1;
            i = 0;
            pc = u16(0xFFDE);
            return;
        }
        case 0x0E:
        {
            int ea = u16(pc + 1);
            int val = rd(ea);
            setnz((a - val) & 0xFF);
            wr(ea, val | a);
            pc = (pc + 3) & 0xFFFF;
            return;
        }
        case 0x4E:
        {
            int ea = u16(pc + 1);
            int val = rd(ea);
            setnz((a - val) & 0xFF);
            wr(ea, val & ~a & 0xFF);
            pc = (pc + 3) & 0xFFFF;
            return;
        }
        case 0x0A: case 0x2A: case 0x4A: case 0x6A: case 0x8A: case 0xAA: case 0xCA: case 0xEA:
        {
            int w = u16(pc + 1);
            int ea = w & 0x1FFF, bit = w >> 13;
            switch (op)
            {
            case 0x0A: c = c | ((rd(ea) >> bit) & 1); break;
            case 0x2A: c = c | (((rd(ea) >> bit) & 1) ^ 1); break;
            case 0x4A: c = c & ((rd(ea) >> bit) & 1); break;
            case 0x6A: c = c & (((rd(ea) >> bit) & 1) ^ 1); break;
            case 0x8A: c = c ^ ((rd(ea) >> bit) & 1); break;
            case 0xAA: c = (rd(ea) >> bit) & 1; break;
            case 0xCA: wr(ea, (rd(ea) & ~(1 << bit) & 0xFF) | (c << bit)); break;
            default: wr(ea, rd(ea) ^ (1 << bit)); break;
            }
            pc = (pc + 3) & 0xFFFF;
            return;
        }
        case 0xEF: case 0xFF:
            halted = true;
            cyc += 1L << 40;
            return;
        default: break;
        }
        // TCALL n, SET1/CLR1/BBS/BBC
        if ((op & 0x0F) == 0x01)
        {
            int nn = op >> 4;
            int ret = (pc + 1) & 0xFFFF;
            pc = u16(0xFFDE - nn * 2);
            push(ret >> 8);
            push(ret & 0xFF);
            return;
        }
        int bit = op >> 5;
        switch (op & 0x1F)
        {
        case 0x03:
        {
            int val = rd(op1() + dp);
            branch3((val >> bit) & 1);
            return;
        }
        case 0x13:
        {
            int val = rd(op1() + dp);
            branch3(!((val >> bit) & 1));
            return;
        }
        case 0x02:
        {
            int ea = op1() + dp;
            wr(ea, rd(ea) | (1 << bit));
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        case 0x12:
        {
            int ea = op1() + dp;
            wr(ea, rd(ea) & ~(1 << bit) & 0xFF);
            pc = (pc + 2) & 0xFFFF;
            return;
        }
        }
        pc = (pc + 1) & 0xFFFF;   // недостижимо: все 256 кодов определены
    }

    static int rmw_f(int op)
    {
        switch (op >> 5)
        {
        case 0: return R_ASL;
        case 1: return R_ROL;
        case 2: return R_LSR;
        case 3: return R_ROR;
        case 5: return R_INC;
        default: return R_DEC;
        }
    }

    void run_until(long end)
    {
        for (;;)
        {
            int op = ram[pc];
            long cc = cyc + cycles[op];
            if (cc > end) break;
            cyc = cc;
            exec(op);
            if (halted) break;
        }
    }

    void play(long nsamples)
    {
        cyc_target += nsamples * CLOCKS_PER_SAMPLE;
        long end = cyc_target;
        run_until(end);
        if (halted) cyc = end;
        for (auto & t : timers) run_timer(t, end);
        run_dsp(end, MAX_REG_TIME);
    }
};

// Эмулирует seconds секунд и возвращает трассу (как spctrace). progress(f) - необязателен.
inline bool trace(const std::vector<uint8_t> & data, double seconds, Trace & out)
{
    if (!Spc::valid(data)) return false;
    std::unique_ptr<Spc> emu(new Spc(data));
    emu->dsp.tr = &out;
    long total = (long)(seconds * 32000);
    long done = 0;
    const long chunk = 4096;
    while (done < total)
    {
        long nn = std::min(chunk, total - done);
        emu->play(nn);
        done += nn;
    }
    out.total_samples = emu->dsp.nsamp;
    return true;
}

} // namespace spcemu
