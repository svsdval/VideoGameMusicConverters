#!/usr/bin/env python3
"""
spc_pyemu - эмулятор SNES SPC700 + S-DSP на чистом Python (без numpy в самом ядре).

Это порт Game_Music_Emu (Spc_Dsp.cpp, Snes_Spc.cpp, Spc_Cpu.*) с теми же хуками, что в
spc_tracer/spctrace.patch, поэтому он выдаёт ровно те же файлы, что и нативный spctrace:
    prefix.trace   K/F-строки (старт голоса и состояние голосов каждые 128 отсчётов)
    prefix.pcm     декодированный BRR
    out.raw        (необязательно) вывод DSP 32 кГц стерео s16 (как у GME: усиление 1.4)

Скорость: порядка 0.3-1 секунды эмуляции за реальную секунду на современном ПК - нативный
spctrace быстрее в сотни раз, но этот модуль не требует ни компилятора, ни cmake, ни git.

    python3 spc_pyemu.py in.spc out_prefix seconds [out.raw]
"""
import struct
import sys

GAUSS = (
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
)

INITIAL_REGS = bytes((
    0x45, 0x8B, 0x5A, 0x9A, 0xE4, 0x82, 0x1B, 0x78, 0x00, 0x00, 0xAA, 0x96, 0x89, 0x0E, 0xE0, 0x80,
    0x2A, 0x49, 0x3D, 0xBA, 0x14, 0xA0, 0xAC, 0xC5, 0x00, 0x00, 0x51, 0xBB, 0x9C, 0x4E, 0x7B, 0xFF,
    0xF4, 0xFD, 0x57, 0x32, 0x37, 0xD9, 0x42, 0x22, 0x00, 0x00, 0x5B, 0x3C, 0x9F, 0x1B, 0x87, 0x9A,
    0x6F, 0x27, 0xAF, 0x7B, 0xE5, 0x68, 0x0A, 0xD9, 0x00, 0x00, 0x9A, 0xC5, 0x9C, 0x4E, 0x7B, 0xFF,
    0xEA, 0x21, 0x78, 0x4F, 0xDD, 0xED, 0x24, 0x14, 0x00, 0x00, 0x77, 0xB1, 0xD1, 0x36, 0xC1, 0x67,
    0x52, 0x57, 0x46, 0x3D, 0x59, 0xF4, 0x87, 0xA4, 0x00, 0x00, 0x7E, 0x44, 0x9C, 0x4E, 0x7B, 0xFF,
    0x75, 0xF5, 0x06, 0x97, 0x10, 0xC3, 0x24, 0xBB, 0x00, 0x00, 0x7B, 0x7A, 0xE0, 0x60, 0x12, 0x0F,
    0xF7, 0x74, 0x1C, 0xE5, 0x39, 0x3D, 0x73, 0xC1, 0x00, 0x00, 0x7A, 0xB3, 0xFF, 0x4E, 0x7B, 0xFF,
))

CLOCKS_PER_SAMPLE = 32
ROM_ADDR = 0xFFC0

# циклы инструкций (как в Snes_Spc.cpp; для условных переходов - с выполненным переходом)
_CYCLE_PACKED = (
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x54, 0x68, 0x48, 0x47, 0x45, 0x56, 0x55, 0x65, 0x22, 0x46,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x54, 0x74, 0x48, 0x47, 0x45, 0x56, 0x55, 0x65, 0x22, 0x38,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x44, 0x54, 0x66, 0x48, 0x47, 0x45, 0x56, 0x55, 0x45, 0x22, 0x43,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x44, 0x54, 0x75, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0x36,
    0x28, 0x47, 0x34, 0x36, 0x26, 0x54, 0x52, 0x45, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0xC5,
    0x38, 0x47, 0x34, 0x36, 0x26, 0x44, 0x52, 0x44, 0x48, 0x47, 0x45, 0x56, 0x55, 0x55, 0x22, 0x34,
    0x38, 0x47, 0x45, 0x47, 0x25, 0x64, 0x52, 0x49, 0x48, 0x47, 0x56, 0x67, 0x45, 0x55, 0x22, 0x83,
    0x28, 0x47, 0x34, 0x36, 0x24, 0x53, 0x43, 0x40, 0x48, 0x47, 0x45, 0x56, 0x34, 0x54, 0x22, 0x60,
)
CYCLES = []
for _n in _CYCLE_PACKED:
    CYCLES += [_n >> 4, _n & 15]

REG_TIMES = [
    -1, 0, -11, -10, -15, -11, -2, -2, 4, 3, 14, 14, 26, 26, 14, 22,
    2, 3, 0, 1, -12, 0, 1, 1, 7, 6, 14, 14, 27, 14, 14, 23,
    5, 6, 3, 4, -1, 3, 4, 4, 10, 9, 14, 14, 26, -5, 14, 23,
    8, 9, 6, 7, 2, 6, 7, 7, 13, 12, 14, 14, 27, -4, 14, 24,
    11, 12, 9, 10, 5, 9, 10, 10, 16, 15, 14, 14, -2, -4, 14, 24,
    14, 15, 12, 13, 8, 12, 13, 13, 19, 18, 14, 14, -2, -36, 14, 24,
    17, 18, 15, 16, 11, 15, 16, 16, 22, 21, 14, 14, 28, -3, 14, 25,
    20, 21, 18, 19, 14, 18, 19, 19, 25, 24, 14, 14, 14, 29, 14, 25,
] + [29] * 128
MAX_REG_TIME = 29


def _rate(rate, div):
    return rate // div * 8 - 1 if rate >= div else rate - 1


COUNTER_MASK = [
    _rate(2, 2), _rate(2048, 4), _rate(1536, 3), _rate(1280, 5), _rate(1024, 4), _rate(768, 3),
    _rate(640, 5), _rate(512, 4), _rate(384, 3), _rate(320, 5), _rate(256, 4), _rate(192, 3),
    _rate(160, 5), _rate(128, 4), _rate(96, 3), _rate(80, 5), _rate(64, 4), _rate(48, 3),
    _rate(40, 5), _rate(32, 4), _rate(24, 3), _rate(20, 5), _rate(16, 4), _rate(12, 3),
    _rate(10, 5), _rate(8, 4), _rate(6, 3), _rate(5, 5), _rate(4, 4), _rate(3, 3),
    _rate(2, 4), _rate(1, 4),
]

SHIFTS = (13, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 12, 16, 16, 16,
          0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 11, 11, 11)

ENV_RELEASE, ENV_ATTACK, ENV_DECAY, ENV_SUSTAIN = 0, 1, 2, 3


def s16(x):
    return ((x + 0x8000) & 0xFFFF) - 0x8000


def s8(x):
    return ((x + 0x80) & 0xFF) - 0x80


class Voice:
    __slots__ = ('buf', 'buf_pos', 'interp_pos', 'brr_addr', 'brr_offset', 'kon_delay', 'env_mode',
                 'env', 'hidden_env', 'vol0', 'vol1', 'trace_key', 'trace_off')

    def __init__(self):
        self.buf = [0] * 24
        self.buf_pos = 0
        self.interp_pos = 0
        self.brr_addr = 0
        self.brr_offset = 1
        self.kon_delay = 0
        self.env_mode = ENV_RELEASE
        self.env = 0
        self.hidden_env = 0
        self.vol0 = 0
        self.vol1 = 0
        self.trace_key = 0
        self.trace_off = 0


class Dsp:
    def __init__(self, ram):
        self.ram = ram
        self.regs = bytearray(128)
        self.voices = [Voice() for _ in range(8)]
        self.noise = 0x4000
        self.echo_hist = [[0, 0] for _ in range(16)]
        self.echo_hist_pos = 0
        self.every_other = 1
        self.kon = 0
        self.new_kon = 0
        self.t_koff = 0
        self.echo_offset = 0
        self.echo_length = 0
        self.phase = 0
        self.counters = [0, 0, 0, 0]
        self.sel = [0] * 32
        self.surround_threshold = -0x4000
        self.out = []              # плоский список l, r, l, r ...
        self.nsamp = 0
        self.on_kon = None
        self.on_voice = None
        self.on_brr = None
        self.load(INITIAL_REGS)

    def load(self, regs):
        self.regs[:] = regs[:128]
        for v in self.voices:
            v.brr_offset = 1
            v.buf_pos = 0
        self.new_kon = self.regs[0x4C]
        for i in range(8):
            self.update_voice_vol(i * 0x10)
        self.noise = 0x4000
        self.echo_hist_pos = 0
        self.every_other = 1
        self.echo_offset = 0
        self.phase = 0
        self.counters = [1, 0, 0xFFFFFFE0, 0x0B]
        n = 2
        for i in range(1, 32):
            self.sel[i] = n
            n -= 1
            if not n:
                n = 3
        self.sel[0] = 0
        self.sel[30] = 2

    def update_voice_vol(self, addr):
        l = s8(self.regs[addr])
        r = s8(self.regs[addr + 1])
        if l * r < self.surround_threshold:
            l ^= l >> 7
            r ^= r >> 7
        v = self.voices[addr >> 4]
        v.vol0 = l
        v.vol1 = r

    def read(self, addr):
        return self.regs[addr]

    def write(self, addr, data):
        self.regs[addr] = data
        low = addr & 0x0F
        if low < 2:
            self.update_voice_vol(low ^ addr)
        elif low == 0x0C:
            if addr == 0x4C:
                self.new_kon = data
            if addr == 0x7C:
                self.regs[0x7C] = 0

    def run(self, clock_count):
        new_phase = self.phase + clock_count
        count = new_phase >> 5
        self.phase = new_phase & 31
        if not count:
            return
        ram = self.ram
        regs = self.regs
        voices = self.voices
        counters = self.counters
        sel = self.sel
        out = self.out
        gauss = GAUSS
        on_voice = self.on_voice
        on_kon = self.on_kon
        on_brr = self.on_brr
        dir_ = regs[0x5D] * 0x100
        pmon_reg = regs[0x2D]
        non_reg = regs[0x3D]
        eon_reg = regs[0x4D]
        slow_gaussian = (pmon_reg >> 1) | non_reg
        noise_rate = regs[0x6C] & 0x1F
        mvoll = s8(regs[0x0C])
        mvolr = s8(regs[0x1C])
        evoll = s8(regs[0x2C])
        evolr = s8(regs[0x3C])
        if mvoll * mvolr < self.surround_threshold:
            mvoll = -mvoll
        flg = regs[0x6C]
        esa = regs[0x6D]
        efb = s8(regs[0x0D])
        fir = [s8(regs[0x0F + i * 0x10]) for i in range(8)]
        noise_mask = COUNTER_MASK[noise_rate]
        noise_sel = sel[noise_rate]
        while count:
            count -= 1
            self.nsamp += 1
            report = on_voice is not None and (self.nsamp & 127) == 0
            self.every_other ^= 1
            if self.every_other:
                self.new_kon &= ~self.kon
                self.kon = self.new_kon
                self.t_koff = regs[0x5C]
            # счётчики
            for i in (1, 2, 3):
                n = counters[i]
                if not (n & 7):
                    n -= 6 - i
                counters[i] = (n - 1) & 0xFFFFFFFF
            if not (counters[noise_sel] & noise_mask):
                fb = (self.noise << 13) ^ (self.noise << 14)
                self.noise = (fb & 0x4000) ^ (self.noise >> 1)
            noise = self.noise
            pmon_input = 0
            main_l = main_r = echo_l = echo_r = 0
            vbit = 1
            for vi in range(8):
                v = voices[vi]
                vr = vi * 0x10
                if (not v.env and not v.kon_delay and v.env_mode == ENV_RELEASE and
                        not (self.every_other and (self.kon & vbit))):
                    # молчащий голос без KON: ничего не происходит
                    regs[vr + 8] = 0
                    regs[vr + 9] = 0
                    pmon_input = 0
                    vbit <<= 1
                    continue
                brr_header = ram[v.brr_addr]
                kon_delay = v.kon_delay
                pitch = (regs[vr + 2] | (regs[vr + 3] << 8)) & 0x3FFF
                if pmon_reg & vbit:
                    pitch += ((pmon_input >> 5) * pitch) >> 10
                srcn = regs[vr + 4]
                kon_delay -= 1
                if kon_delay >= 0:
                    v.kon_delay = kon_delay
                    if kon_delay == 4:
                        v.brr_addr = ram[dir_ + srcn * 4] | (ram[dir_ + srcn * 4 + 1] << 8)
                        v.brr_offset = 1
                        v.buf_pos = 0
                        brr_header = 0
                        v.trace_key = v.brr_addr
                        v.trace_off = 0
                        if on_kon:
                            on_kon(vi, srcn, (regs[vr + 2] | (regs[vr + 3] << 8)) & 0x3FFF,
                                   v.vol0, v.vol1, v.brr_addr,
                                   ram[dir_ + srcn * 4 + 2] | (ram[dir_ + srcn * 4 + 3] << 8))
                    v.env = 0
                    v.hidden_env = 0
                    v.interp_pos = 0x4000 if (kon_delay & 3) else 0
                    pitch = 0
                env = v.env
                output = 0
                regs[vr + 8] = (env >> 4) & 0xFF
                if env:
                    offset = (v.interp_pos >> 3) & 0x1FE
                    b = v.buf
                    p0 = v.buf_pos + (v.interp_pos >> 12)
                    i0 = b[p0]
                    i1 = b[p0 + 1]
                    i2 = b[p0 + 2]
                    i3 = b[p0 + 3]
                    f0 = gauss[offset]
                    f1 = gauss[offset + 1]
                    r1 = gauss[510 - offset + 1]
                    r0 = gauss[510 - offset]
                    if not (slow_gaussian & vbit):
                        output = (f0 * i0 + f1 * i1 + r1 * i2 + r0 * i3) >> 11
                        output = (output * env) >> 11
                    else:
                        output = s16(noise * 2)
                        if not (non_reg & vbit):
                            output = (f0 * i0) >> 11
                            output += (f1 * i1) >> 11
                            output += (r1 * i2) >> 11
                            output = s16(output)
                            output += (r0 * i3) >> 11
                            if output > 32767:
                                output = 32767
                            elif output < -32768:
                                output = -32768
                            output &= ~1
                        output = ((output * env) >> 11) & ~1
                    l = output * v.vol0
                    r = output * v.vol1
                    main_l += l
                    main_r += r
                    if eon_reg & vbit:
                        echo_l += l
                        echo_r += r
                pmon_input = output
                regs[vr + 9] = (output >> 8) & 0xFF
                if report and (env > 0 or v.kon_delay > 0):
                    on_voice(vi, env, v.kon_delay, srcn, pitch, v.vol0, v.vol1,
                             1 if (pmon_reg & vbit) else 0, 1 if (non_reg & vbit) else 0)

                if (flg & 0x80) or (brr_header & 3) == 1:
                    v.env_mode = ENV_RELEASE
                    env = 0
                if self.every_other:
                    if self.t_koff & vbit:
                        v.env_mode = ENV_RELEASE
                    if self.kon & vbit:
                        v.kon_delay = 5
                        v.env_mode = ENV_ATTACK
                        regs[0x7C] &= ~vbit
                # огибающая
                skip_brr = False
                run_brr = True
                if not v.kon_delay:
                    if v.env_mode == ENV_RELEASE:
                        env -= 8
                        v.env = env
                        if env <= 0:
                            v.env = 0
                            skip_brr = True
                    else:
                        adsr0 = regs[vr + 5]
                        env_data = regs[vr + 6]
                        exit_env = False
                        if adsr0 >= 0x80:
                            if v.env_mode > ENV_DECAY:
                                env -= 1
                                env -= env >> 8
                                rate = env_data & 0x1F
                                v.hidden_env = env
                                if not (counters[sel[rate]] & COUNTER_MASK[rate]):
                                    v.env = env
                                exit_env = True
                            elif v.env_mode == ENV_DECAY:
                                env -= 1
                                env -= env >> 8
                                rate = ((adsr0 >> 3) & 0x0E) + 0x10
                            else:
                                rate = (adsr0 & 0x0F) * 2 + 1
                                env += 0x20 if rate < 31 else 0x400
                        else:
                            env_data = regs[vr + 7]
                            mode = env_data >> 5
                            if mode < 4:
                                env = env_data * 0x10
                                rate = 31
                            else:
                                rate = env_data & 0x1F
                                if mode == 4:
                                    env -= 0x20
                                elif mode < 6:
                                    env -= 1
                                    env -= env >> 8
                                else:
                                    env += 0x20
                                    if mode > 6 and v.hidden_env >= 0x600:
                                        env += 0x8 - 0x20
                        if not exit_env:
                            if (env >> 8) == (env_data >> 5) and v.env_mode == ENV_DECAY:
                                v.env_mode = ENV_SUSTAIN
                            v.hidden_env = env
                            if env > 0x7FF or env < 0:
                                env = 0 if env < 0 else 0x7FF
                                if v.env_mode == ENV_ATTACK:
                                    v.env_mode = ENV_DECAY
                            if not (counters[sel[rate]] & COUNTER_MASK[rate]):
                                v.env = env
                if not skip_brr:
                    old_pos = v.interp_pos
                    interp_pos = (old_pos & 0x3FFF) + pitch
                    if interp_pos > 0x7FFF:
                        interp_pos = 0x7FFF
                    v.interp_pos = interp_pos
                    if old_pos >= 0x4000:
                        ba = v.brr_addr
                        bo = v.brr_offset
                        nybbles = (ram[(ba + bo) & 0xFFFF] << 8) | ram[(ba + bo + 1) & 0xFFFF]
                        bo += 2
                        if bo >= 9:
                            ba = (ba + 9) & 0xFFFF
                            if brr_header & 1:
                                sp = ((srcn * 4) + 2)
                                ba = ram[dir_ + sp] | (ram[dir_ + sp + 1] << 8)
                                if not v.kon_delay:
                                    regs[0x7C] |= vbit
                            v.brr_addr = ba
                            bo = 1
                        v.brr_offset = bo
                        scale = brr_header >> 4
                        right_shift = SHIFTS[scale]
                        left_shift = SHIFTS[scale + 16]
                        filt = brr_header & 0x0C
                        b = v.buf
                        pos = v.buf_pos
                        decoded = []
                        for _ in range(4):
                            n16 = ((nybbles & 0xFFFF) ^ 0x8000) - 0x8000
                            s = ((((n16 >> right_shift) & 0xFFFF) << left_shift) + 0x8000 & 0xFFFF) - 0x8000
                            p1 = b[pos + 11]
                            p2 = b[pos + 10] >> 1
                            if filt >= 8:
                                s += p1
                                s -= p2
                                if filt == 8:
                                    s += p2 >> 4
                                    s += (p1 * -3) >> 6
                                else:
                                    s += (p1 * -13) >> 7
                                    s += (p2 * 3) >> 4
                            elif filt:
                                s += p1 >> 1
                                s += (-p1) >> 5
                            if s > 32767:
                                s = 32767
                            elif s < -32768:
                                s = -32768
                            s = ((s * 2 + 0x8000) & 0xFFFF) - 0x8000
                            b[pos] = b[pos + 12] = s
                            decoded.append(s)
                            pos += 1
                            nybbles <<= 4
                        if on_brr:
                            on_brr(vi, v.trace_key, v.trace_off, decoded)
                        v.trace_off += 4
                        if pos >= 12:
                            pos = 0
                        v.buf_pos = pos
                vbit <<= 1
            # эхо
            echo_offset = self.echo_offset
            ptr = (esa * 0x100 + echo_offset) & 0xFFFF
            if not echo_offset:
                self.echo_length = (regs[0x7D] & 0x0F) * 0x800
            echo_offset += 4
            if echo_offset >= self.echo_length:
                echo_offset = 0
            self.echo_offset = echo_offset
            in_l = s16(ram[ptr] | (ram[(ptr + 1) & 0xFFFF] << 8))
            in_r = s16(ram[(ptr + 2) & 0xFFFF] | (ram[(ptr + 3) & 0xFFFF] << 8))
            hp = self.echo_hist_pos + 1
            if hp >= 8:
                hp = 0
            self.echo_hist_pos = hp
            eh = self.echo_hist
            eh[hp][0] = eh[hp + 8][0] = in_l
            eh[hp][1] = eh[hp + 8][1] = in_r
            in_l = in_l * fir[7]
            in_r = in_r * fir[7]
            for i in range(7):
                e = eh[hp + i + 1]
                in_l += e[0] * fir[i]
                in_r += e[1] * fir[i]
            if not (flg & 0x20):
                l = (echo_l >> 7) + ((in_l * efb) >> 14)
                r = (echo_r >> 7) + ((in_r * efb) >> 14)
                if l > 32767:
                    l = 32767
                elif l < -32768:
                    l = -32768
                if r > 32767:
                    r = 32767
                elif r < -32768:
                    r = -32768
                ram[ptr] = l & 0xFF
                ram[(ptr + 1) & 0xFFFF] = (l >> 8) & 0xFF
                ram[(ptr + 2) & 0xFFFF] = r & 0xFF
                ram[(ptr + 3) & 0xFFFF] = (r >> 8) & 0xFF
            l = (main_l * mvoll + in_l * evoll) >> 14
            r = (main_r * mvolr + in_r * evolr) >> 14
            if l > 32767:
                l = 32767
            elif l < -32768:
                l = -32768
            if r > 32767:
                r = 32767
            elif r < -32768:
                r = -32768
            if flg & 0x40:
                l = r = 0
            out.append(l)
            out.append(r)


class Timer:
    __slots__ = ('next_time', 'prescaler', 'period', 'divider', 'enabled', 'counter')


class Spc:
    def __init__(self, data):
        if data[:27] != b'SNES-SPC700 Sound File Data' or len(data) < 0x10180:
            raise ValueError('не SPC файл')
        self.ram = bytearray(data[0x100:0x10100])
        self.regs = bytearray(16)          # REGS
        self.regs_in = bytearray(16)       # REGS_IN
        self.rom = bytearray(64)
        self.rom[0x3E] = 0xFF
        self.rom[0x3F] = 0xC0
        self.hi_ram = bytearray(64)
        self.rom_enabled = 0
        self.pc = data[0x25] | (data[0x26] << 8)
        self.a, self.x, self.y = data[0x27], data[0x28], data[0x29]
        self.sp = data[0x2B]
        self.set_psw(data[0x2A])
        self.dsp = Dsp(self.ram)
        self.dsp.load(data[0x10100:0x10180])
        # ram_loaded
        self.regs[:] = self.ram[0xF0:0x100]
        self.regs_in[:] = self.regs
        for r in (1, 0xA, 0xB, 0xC):
            self.regs_in[r] = 0
        self.regs_in[0] = 0
        # reset_time_regs
        self.cyc = 0
        self.dsp_time = CLOCKS_PER_SAMPLE + 1
        self.halted = False
        self.timers = []
        for i in range(3):
            t = Timer()
            t.next_time = 1
            t.divider = 0
            self.timers.append(t)
        self.enable_rom(self.regs[1] & 0x80)
        for i, t in enumerate(self.timers):
            t.period = self.regs[0xA + i] or 256
            t.enabled = (self.regs[1] >> i) & 1
            t.counter = self.regs_in[0xD + i] & 0x0F
        t = 0x100
        rate = (16 * t + (t >> 1)) // t
        self.timers[2].prescaler = rate
        self.timers[1].prescaler = rate << 3
        self.timers[0].prescaler = rate << 3
        self.clear_echo()
        self.ops = self._build_ops()

    # ── флаги ──
    def set_psw(self, p):
        self.c = p & 1
        self.nz = 0 if (p & 2) else 1          # nz == 0 <=> флаг Z
        self.i = (p >> 2) & 1
        self.h = (p >> 3) & 1
        self.b = (p >> 4) & 1
        self.dp = 0x100 if (p & 0x20) else 0
        self.v = (p >> 6) & 1
        self.n = (p >> 7) & 1

    def get_psw(self):
        return (self.c | (0 if self.nz else 2) | (self.i << 2) | (self.h << 3) | (self.b << 4) |
                (0x20 if self.dp else 0) | (self.v << 6) | (self.n << 7))

    def clear_echo(self):
        r = self.dsp.regs
        if not (r[0x6C] & 0x20):
            addr = 0x100 * r[0x6D]
            end = min(0x10000, addr + 0x800 * (r[0x7D] & 0x0F))
            if end > addr:
                self.ram[addr:end] = b'\xFF' * (end - addr)

    def enable_rom(self, enable):
        if self.rom_enabled != enable:
            self.rom_enabled = enable
            if enable:
                self.hi_ram[:] = self.ram[ROM_ADDR:ROM_ADDR + 64]
            self.ram[ROM_ADDR:ROM_ADDR + 64] = self.rom if enable else self.hi_ram

    # ── таймеры ──
    def run_timer(self, t, time):
        if time < t.next_time:
            return t
        elapsed = (time - t.next_time) // t.prescaler + 1
        t.next_time += t.prescaler * elapsed
        if t.enabled:
            remain = ((t.period - t.divider - 1) & 0xFF) + 1
            divider = t.divider + elapsed
            over = elapsed - remain
            if over >= 0:
                n = over // t.period
                t.counter = (t.counter + 1 + n) & 0x0F
                divider = over - n * t.period
            t.divider = divider & 0xFF
        return t

    # ── DSP ──
    def run_dsp(self, time, offset):
        count = time - offset - self.dsp_time
        if count >= 0:
            clock_count = (count & ~31) + 32
            self.dsp_time += clock_count
            self.dsp.run(clock_count)

    def rd(self, addr):
        if addr < 0xF0 or addr >= 0x100:
            return self.ram[addr]
        reg = addr - 0xF0
        if reg >= 0xD:
            t = self.timers[reg - 0xD]
            if self.cyc >= t.next_time:
                self.run_timer(t, self.cyc)
            res = t.counter
            t.counter = 0
            return res
        res = self.regs_in[reg]
        if reg == 2:
            res = self.regs[2]
        elif reg == 3:
            self.run_dsp(self.cyc, REG_TIMES[self.regs[2] & 0x7F])
            res = self.dsp.read(self.regs[2] & 0x7F)
        return res

    def wr(self, addr, data):
        self.ram[addr] = data
        if addr >= 0xF0:
            reg = addr - 0xF0
            if reg < 16:
                self.regs[reg] = data
                if reg == 3:
                    a = self.regs[2]
                    self.run_dsp(self.cyc, REG_TIMES[a])
                    if a <= 0x7F:
                        self.dsp.write(a, data)
                elif reg != 2 and (reg < 4 or reg > 7):
                    self._write_smp_reg(data, reg)
            elif addr >= ROM_ADDR:
                i = addr - ROM_ADDR
                self.hi_ram[i] = data
                if self.rom_enabled:
                    self.ram[addr] = self.rom[i]

    def _write_smp_reg(self, data, reg):
        time = self.cyc
        if 0xA <= reg <= 0xC:
            t = self.timers[reg - 0xA]
            period = data or 256
            if t.period != period:
                self.run_timer(t, time)
                t.period = period
        elif 0xD <= reg <= 0xF:
            self.run_timer(self.timers[reg - 0xD], time - 1).counter = 0
        elif reg in (8, 9):
            self.regs_in[reg] = data
        elif reg == 1:
            if data & 0x10:
                self.regs_in[4] = 0
                self.regs_in[5] = 0
            if data & 0x20:
                self.regs_in[6] = 0
                self.regs_in[7] = 0
            for i, t in enumerate(self.timers):
                en = (data >> i) & 1
                if t.enabled != en:
                    self.run_timer(t, time)
                    t.enabled = en
                    if en:
                        t.divider = 0
                        t.counter = 0
            self.enable_rom(data & 0x80)

    # ── CPU ──
    def _build_ops(self):
        S = self
        ops = [None] * 256
        ram = self.ram
        rd = self.rd
        wr = self.wr

        def u16(a):
            return ram[a & 0xFFFF] | (ram[(a + 1) & 0xFFFF] << 8)

        def setnz(v):
            S.nz = v & 0xFF
            S.n = (v >> 7) & 1

        def setnz16(v):
            S.nz = v & 0xFFFF
            S.n = (v >> 15) & 1

        def push(v):
            ram[0x100 + S.sp] = v & 0xFF
            S.sp = (S.sp - 1) & 0xFF

        def pop():
            S.sp = (S.sp + 1) & 0xFF
            return ram[0x100 + S.sp]

        # режимы адресации: возвращают (адрес, длина команды)
        def m_dp():
            return ram[(S.pc + 1) & 0xFFFF] + S.dp, 2

        def m_dpx():
            return ((ram[(S.pc + 1) & 0xFFFF] + S.x) & 0xFF) + S.dp, 2

        def m_dpy():
            return ((ram[(S.pc + 1) & 0xFFFF] + S.y) & 0xFF) + S.dp, 2

        def m_abs():
            return u16(S.pc + 1), 3

        def m_absx():
            return (u16(S.pc + 1) + S.x) & 0xFFFF, 3

        def m_absy():
            return (u16(S.pc + 1) + S.y) & 0xFFFF, 3

        def m_x():
            return S.x + S.dp, 1

        def m_dpx_ind():
            return u16(((ram[(S.pc + 1) & 0xFFFF] + S.x) & 0xFF) + S.dp), 2

        def m_dp_ind_y():
            return (u16(ram[(S.pc + 1) & 0xFFFF] + S.dp) + S.y) & 0xFFFF, 2

        def m_imm():
            return None, 2

        MODES = {0x04: m_dp, 0x05: m_abs, 0x06: m_x, 0x07: m_dpx_ind, 0x08: m_imm,
                 0x14: m_dpx, 0x15: m_absx, 0x16: m_absy, 0x17: m_dp_ind_y}

        # ── ALU ──
        def do_or(a, m):
            return a | m

        def do_and(a, m):
            return a & m

        def do_eor(a, m):
            return a ^ m

        def adc(a, m):
            r = a + m + S.c
            S.v = 1 if (~(a ^ m) & (a ^ r) & 0x80) else 0
            S.h = 1 if ((a ^ m ^ r) & 0x10) else 0
            S.c = (r >> 8) & 1
            r &= 0xFF
            setnz(r)
            return r

        def sbc(a, m):
            return adc(a, m ^ 0xFF)

        def cmp_(a, m):
            r = a - m
            S.c = 1 if r >= 0 else 0
            setnz(r & 0xFF)
            return a

        def make_alu(base, f, store=True):
            for lo, mode in MODES.items():
                def h(f=f, mode=mode, store=store):
                    ea, ln = mode()
                    m = ram[(S.pc + 1) & 0xFFFF] if ea is None else rd(ea)
                    r = f(S.a, m)
                    if store:
                        S.a = r
                        if f in (do_or, do_and, do_eor):
                            setnz(r)
                    S.pc = (S.pc + ln) & 0xFFFF
                ops[base + lo] = h
            # (X),(Y)
            def hxy(f=f, store=store):
                m = rd(S.y + S.dp)
                ea = S.x + S.dp
                r = f(rd(ea), m)
                if store:
                    wr(ea, r)
                    if f in (do_or, do_and, do_eor):
                        setnz(r)
                S.pc = (S.pc + 1) & 0xFFFF
            ops[base + 0x19] = hxy

            def hdd(f=f, store=store):
                m = rd(ram[(S.pc + 1) & 0xFFFF] + S.dp)
                ea = ram[(S.pc + 2) & 0xFFFF] + S.dp
                r = f(rd(ea), m)
                if store:
                    wr(ea, r)
                    if f in (do_or, do_and, do_eor):
                        setnz(r)
                S.pc = (S.pc + 3) & 0xFFFF
            ops[base + 0x09] = hdd

            def hdi(f=f, store=store):
                m = ram[(S.pc + 1) & 0xFFFF]
                ea = ram[(S.pc + 2) & 0xFFFF] + S.dp
                r = f(rd(ea), m)
                if store:
                    wr(ea, r)
                    if f in (do_or, do_and, do_eor):
                        setnz(r)
                S.pc = (S.pc + 3) & 0xFFFF
            ops[base + 0x18] = hdi

        make_alu(0x00, do_or)
        make_alu(0x20, do_and)
        make_alu(0x40, do_eor)
        make_alu(0x60, cmp_, store=False)
        make_alu(0x80, adc)
        make_alu(0xA0, sbc)

        # CMP X / Y
        def make_cmp(reg, modes):
            for op, mode in modes.items():
                def h(mode=mode, reg=reg):
                    ea, ln = mode()
                    m = ram[(S.pc + 1) & 0xFFFF] if ea is None else rd(ea)
                    cmp_(getattr(S, reg), m)
                    S.pc = (S.pc + ln) & 0xFFFF
                ops[op] = h
        make_cmp('x', {0xC8: m_imm, 0x3E: m_dp, 0x1E: m_abs})
        make_cmp('y', {0xAD: m_imm, 0x7E: m_dp, 0x5E: m_abs})

        # ── MOV ──
        def make_load(reg, modes):
            for op, mode in modes.items():
                def h(mode=mode, reg=reg):
                    ea, ln = mode()
                    m = ram[(S.pc + 1) & 0xFFFF] if ea is None else rd(ea)
                    setattr(S, reg, m)
                    setnz(m)
                    S.pc = (S.pc + ln) & 0xFFFF
                ops[op] = h
        make_load('a', {0xE4: m_dp, 0xE5: m_abs, 0xE6: m_x, 0xE7: m_dpx_ind, 0xE8: m_imm,
                        0xF4: m_dpx, 0xF5: m_absx, 0xF6: m_absy, 0xF7: m_dp_ind_y})
        make_load('x', {0xF8: m_dp, 0xF9: m_dpy, 0xE9: m_abs, 0xCD: m_imm})
        make_load('y', {0xEB: m_dp, 0xFB: m_dpx, 0xEC: m_abs, 0x8D: m_imm})

        def make_store(reg, modes):
            for op, mode in modes.items():
                def h(mode=mode, reg=reg):
                    ea, ln = mode()
                    wr(ea, getattr(S, reg))
                    S.pc = (S.pc + ln) & 0xFFFF
                ops[op] = h
        make_store('a', {0xC4: m_dp, 0xC5: m_abs, 0xC6: m_x, 0xC7: m_dpx_ind, 0xD4: m_dpx,
                         0xD5: m_absx, 0xD6: m_absy, 0xD7: m_dp_ind_y})
        make_store('x', {0xD8: m_dp, 0xD9: m_dpy, 0xC9: m_abs})
        make_store('y', {0xCB: m_dp, 0xDB: m_dpx, 0xCC: m_abs})

        def reg_move(op, src, dst, flags=True):
            def h():
                v = getattr(S, src)
                setattr(S, dst, v)
                if flags:
                    setnz(v)
                S.pc = (S.pc + 1) & 0xFFFF
            ops[op] = h
        reg_move(0x7D, 'x', 'a')
        reg_move(0xDD, 'y', 'a')
        reg_move(0x5D, 'a', 'x')
        reg_move(0xFD, 'a', 'y')
        reg_move(0x9D, 'sp', 'x')
        reg_move(0xBD, 'x', 'sp', False)

        def mov_xinc_a():
            wr(S.x + S.dp, S.a)
            S.x = (S.x + 1) & 0xFF
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0xAF] = mov_xinc_a

        def mov_a_xinc():
            S.a = rd(S.x + S.dp)
            S.x = (S.x + 1) & 0xFF
            setnz(S.a)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0xBF] = mov_a_xinc

        def mov_d_imm():
            wr(ram[(S.pc + 2) & 0xFFFF] + S.dp, ram[(S.pc + 1) & 0xFFFF])
            S.pc = (S.pc + 3) & 0xFFFF
        ops[0x8F] = mov_d_imm

        def mov_dd():
            v = rd(ram[(S.pc + 1) & 0xFFFF] + S.dp)
            wr(ram[(S.pc + 2) & 0xFFFF] + S.dp, v)
            S.pc = (S.pc + 3) & 0xFFFF
        ops[0xFA] = mov_dd

        # ── сдвиги, inc/dec ──
        def asl(v):
            S.c = (v >> 7) & 1
            return (v << 1) & 0xFF

        def lsr(v):
            S.c = v & 1
            return v >> 1

        def rol(v):
            r = ((v << 1) | S.c) & 0xFF
            S.c = (v >> 7) & 1
            return r

        def ror(v):
            r = (v >> 1) | (S.c << 7)
            S.c = v & 1
            return r

        def inc(v):
            return (v + 1) & 0xFF

        def dec(v):
            return (v - 1) & 0xFF

        def make_rmw(f, base_dp, base_abs, base_dpx, op_a, op_x=None, op_y=None):
            def hmem(mode):
                def h():
                    ea, ln = mode()
                    r = f(rd(ea))
                    setnz(r)
                    wr(ea, r)
                    S.pc = (S.pc + ln) & 0xFFFF
                return h
            ops[base_dp] = hmem(m_dp)
            ops[base_abs] = hmem(m_abs)
            ops[base_dpx] = hmem(m_dpx)

            def ha():
                S.a = f(S.a)
                setnz(S.a)
                S.pc = (S.pc + 1) & 0xFFFF
            ops[op_a] = ha
            if op_x:
                def hx():
                    S.x = f(S.x)
                    setnz(S.x)
                    S.pc = (S.pc + 1) & 0xFFFF
                ops[op_x] = hx
            if op_y:
                def hy():
                    S.y = f(S.y)
                    setnz(S.y)
                    S.pc = (S.pc + 1) & 0xFFFF
                ops[op_y] = hy
        make_rmw(asl, 0x0B, 0x0C, 0x1B, 0x1C)
        make_rmw(rol, 0x2B, 0x2C, 0x3B, 0x3C)
        make_rmw(lsr, 0x4B, 0x4C, 0x5B, 0x5C)
        make_rmw(ror, 0x6B, 0x6C, 0x7B, 0x7C)
        make_rmw(inc, 0xAB, 0xAC, 0xBB, 0xBC, 0x3D, 0xFC)
        make_rmw(dec, 0x8B, 0x8C, 0x9B, 0x9C, 0x1D, 0xDC)

        # ── 16 бит ──
        def dp16(d):
            return d + S.dp, ((d + 1) & 0xFF) + S.dp

        def movw_ya_d():
            lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
            S.a = rd(lo)
            S.y = rd(hi)
            setnz16(S.a | (S.y << 8))
            S.pc = (S.pc + 2) & 0xFFFF
        ops[0xBA] = movw_ya_d

        def movw_d_ya():
            lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
            rd(lo)
            wr(lo, S.a)
            wr(hi, S.y)
            S.pc = (S.pc + 2) & 0xFFFF
        ops[0xDA] = movw_d_ya

        def make_incw(delta, op):
            def h():
                lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
                v = (rd(lo) | (rd(hi) << 8)) + delta
                wr(lo, v & 0xFF)
                wr(hi, (v >> 8) & 0xFF)
                setnz16(v)
                S.pc = (S.pc + 2) & 0xFFFF
            ops[op] = h
        make_incw(1, 0x3A)
        make_incw(-1, 0x1A)

        def addw():
            lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
            m = rd(lo) | (rd(hi) << 8)
            ya = S.a | (S.y << 8)
            r = ya + m
            S.v = 1 if (~(ya ^ m) & (ya ^ r) & 0x8000) else 0
            S.h = 1 if (((ya ^ m ^ r) >> 8) & 0x10) else 0
            S.c = (r >> 16) & 1
            r &= 0xFFFF
            S.a, S.y = r & 0xFF, r >> 8
            setnz16(r)
            S.pc = (S.pc + 2) & 0xFFFF
        ops[0x7A] = addw

        def subw():
            lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
            m = (rd(lo) | (rd(hi) << 8)) ^ 0xFFFF
            ya = S.a | (S.y << 8)
            r = ya + m + 1
            S.v = 1 if (~(ya ^ m) & (ya ^ r) & 0x8000) else 0
            S.h = 1 if (((ya ^ m ^ r) >> 8) & 0x10) else 0
            S.c = (r >> 16) & 1
            r &= 0xFFFF
            S.a, S.y = r & 0xFF, r >> 8
            setnz16(r)
            S.pc = (S.pc + 2) & 0xFFFF
        ops[0x9A] = subw

        def cmpw():
            lo, hi = dp16(ram[(S.pc + 1) & 0xFFFF])
            m = rd(lo) | (rd(hi) << 8)
            ya = S.a | (S.y << 8)
            r = ya - m
            S.c = 1 if r >= 0 else 0
            setnz16(r & 0xFFFF)
            S.pc = (S.pc + 2) & 0xFFFF
        ops[0x5A] = cmpw

        def mul():
            r = S.y * S.a
            S.a = r & 0xFF
            S.y = r >> 8
            setnz(S.y)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0xCF] = mul

        def div():
            ya = S.a | (S.y << 8)
            x = S.x
            S.v = 1 if S.y >= x else 0
            S.h = 1 if (S.y & 15) >= (x & 15) else 0
            if S.y < (x << 1):
                if x:
                    S.a = (ya // x) & 0xFF
                    S.y = ya % x
                else:
                    S.a = 0xFF
                    S.y = 0xFF & ya
            else:
                d = 256 - x
                t = ya - (x << 9)
                S.a = (255 - t // d) & 0xFF
                S.y = (x + t % d) & 0xFF
            setnz(S.a)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0x9E] = div

        def xcn():
            S.a = ((S.a << 4) | (S.a >> 4)) & 0xFF
            setnz(S.a)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0x9F] = xcn

        def daa():
            a = S.a
            if S.c or a > 0x99:
                a += 0x60
                S.c = 1
            if S.h or (a & 15) > 9:
                a += 6
            S.a = a & 0xFF
            setnz(S.a)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0xDF] = daa

        def das():
            a = S.a
            if (not S.c) or a > 0x99:
                a -= 0x60
                S.c = 0
            if (not S.h) or (a & 15) > 9:
                a -= 6
            S.a = a & 0xFF
            setnz(S.a)
            S.pc = (S.pc + 1) & 0xFFFF
        ops[0xBE] = das

        # ── флаги/стек ──
        def simple(op, fn, ln=1):
            def h():
                fn()
                S.pc = (S.pc + ln) & 0xFFFF
            ops[op] = h

        def set_(attr, val):
            def f():
                setattr(S, attr, val)
            return f

        def setdp(val):
            def f():
                S.dp = val
            return f

        def clrv():
            S.v = 0
            S.h = 0

        def notc():
            S.c ^= 1
        simple(0x60, set_('c', 0))
        simple(0x80, set_('c', 1))
        simple(0xED, notc)
        simple(0xE0, clrv)
        simple(0x20, setdp(0))
        simple(0x40, setdp(0x100))
        simple(0xA0, set_('i', 1))
        simple(0xC0, set_('i', 0))
        simple(0x00, lambda: None)

        def make_push(op, reg):
            def f():
                push(getattr(S, reg))
            simple(op, f)

        def make_pop(op, reg):
            def f():
                setattr(S, reg, pop())
            simple(op, f)
        make_push(0x2D, 'a')
        make_push(0x4D, 'x')
        make_push(0x6D, 'y')
        make_pop(0xAE, 'a')
        make_pop(0xCE, 'x')
        make_pop(0xEE, 'y')
        simple(0x0D, lambda: push(S.get_psw()))
        simple(0x8E, lambda: S.set_psw(pop()))

        # ── переходы ──
        def branch(op, cond):
            def h():
                if cond():
                    S.pc = (S.pc + 2 + s8(ram[(S.pc + 1) & 0xFFFF])) & 0xFFFF
                else:
                    S.cyc -= 2
                    S.pc = (S.pc + 2) & 0xFFFF
            ops[op] = h
        branch(0x10, lambda: not S.n)
        branch(0x30, lambda: S.n)
        branch(0x50, lambda: not S.v)
        branch(0x70, lambda: S.v)
        branch(0x90, lambda: not S.c)
        branch(0xB0, lambda: S.c)
        branch(0xD0, lambda: S.nz)
        branch(0xF0, lambda: not S.nz)

        def bra():
            S.pc = (S.pc + 2 + s8(ram[(S.pc + 1) & 0xFFFF])) & 0xFFFF
        ops[0x2F] = bra

        for bit in range(8):
            def bbs(bit=bit):
                v = rd(ram[(S.pc + 1) & 0xFFFF] + S.dp)
                if (v >> bit) & 1:
                    S.pc = (S.pc + 3 + s8(ram[(S.pc + 2) & 0xFFFF])) & 0xFFFF
                else:
                    S.cyc -= 2
                    S.pc = (S.pc + 3) & 0xFFFF

            def bbc(bit=bit):
                v = rd(ram[(S.pc + 1) & 0xFFFF] + S.dp)
                if not ((v >> bit) & 1):
                    S.pc = (S.pc + 3 + s8(ram[(S.pc + 2) & 0xFFFF])) & 0xFFFF
                else:
                    S.cyc -= 2
                    S.pc = (S.pc + 3) & 0xFFFF

            def set1(bit=bit):
                ea = ram[(S.pc + 1) & 0xFFFF] + S.dp
                wr(ea, rd(ea) | (1 << bit))
                S.pc = (S.pc + 2) & 0xFFFF

            def clr1(bit=bit):
                ea = ram[(S.pc + 1) & 0xFFFF] + S.dp
                wr(ea, rd(ea) & ~(1 << bit) & 0xFF)
                S.pc = (S.pc + 2) & 0xFFFF
            ops[0x03 + bit * 0x20] = bbs
            ops[0x13 + bit * 0x20] = bbc
            ops[0x02 + bit * 0x20] = set1
            ops[0x12 + bit * 0x20] = clr1

        def cbne_d():
            if S.a != rd(ram[(S.pc + 1) & 0xFFFF] + S.dp):
                S.pc = (S.pc + 3 + s8(ram[(S.pc + 2) & 0xFFFF])) & 0xFFFF
            else:
                S.cyc -= 2
                S.pc = (S.pc + 3) & 0xFFFF
        ops[0x2E] = cbne_d

        def cbne_dx():
            if S.a != rd(((ram[(S.pc + 1) & 0xFFFF] + S.x) & 0xFF) + S.dp):
                S.pc = (S.pc + 3 + s8(ram[(S.pc + 2) & 0xFFFF])) & 0xFFFF
            else:
                S.cyc -= 2
                S.pc = (S.pc + 3) & 0xFFFF
        ops[0xDE] = cbne_dx

        def dbnz_d():
            ea = ram[(S.pc + 1) & 0xFFFF] + S.dp
            v = (rd(ea) - 1) & 0xFF
            wr(ea, v)
            if v:
                S.pc = (S.pc + 3 + s8(ram[(S.pc + 2) & 0xFFFF])) & 0xFFFF
            else:
                S.cyc -= 2
                S.pc = (S.pc + 3) & 0xFFFF
        ops[0x6E] = dbnz_d

        def dbnz_y():
            S.y = (S.y - 1) & 0xFF
            if S.y:
                S.pc = (S.pc + 2 + s8(ram[(S.pc + 1) & 0xFFFF])) & 0xFFFF
            else:
                S.cyc -= 2
                S.pc = (S.pc + 2) & 0xFFFF
        ops[0xFE] = dbnz_y

        def jmp_abs():
            S.pc = u16(S.pc + 1)
        ops[0x5F] = jmp_abs

        def jmp_absx():
            S.pc = u16(u16(S.pc + 1) + S.x)
        ops[0x1F] = jmp_absx

        def call():
            ret = (S.pc + 3) & 0xFFFF
            S.pc = u16(S.pc + 1)
            push(ret >> 8)
            push(ret & 0xFF)
        ops[0x3F] = call

        def pcall():
            ret = (S.pc + 2) & 0xFFFF
            S.pc = 0xFF00 + ram[(S.pc + 1) & 0xFFFF]
            push(ret >> 8)
            push(ret & 0xFF)
        ops[0x4F] = pcall

        for n in range(16):
            def tcall(n=n):
                ret = (S.pc + 1) & 0xFFFF
                S.pc = u16(0xFFDE - n * 2)
                push(ret >> 8)
                push(ret & 0xFF)
            ops[0x01 + n * 0x10] = tcall

        def ret():
            lo = pop()
            hi = pop()
            S.pc = lo | (hi << 8)
        ops[0x6F] = ret

        def reti():
            S.set_psw(pop())
            lo = pop()
            hi = pop()
            S.pc = lo | (hi << 8)
        ops[0x7F] = reti

        def brk():
            ret_ = (S.pc + 1) & 0xFFFF
            push(ret_ >> 8)
            push(ret_ & 0xFF)
            push(S.get_psw())
            S.b = 1
            S.i = 0
            S.pc = u16(0xFFDE)
        ops[0x0F] = brk

        def tset1():
            ea = u16(S.pc + 1)
            v = rd(ea)
            setnz((S.a - v) & 0xFF)
            wr(ea, v | S.a)
            S.pc = (S.pc + 3) & 0xFFFF
        ops[0x0E] = tset1

        def tclr1():
            ea = u16(S.pc + 1)
            v = rd(ea)
            setnz((S.a - v) & 0xFF)
            wr(ea, v & ~S.a & 0xFF)
            S.pc = (S.pc + 3) & 0xFFFF
        ops[0x4E] = tclr1

        # ── битовые операции с C ──
        def mem_bit():
            w = u16(S.pc + 1)
            return w & 0x1FFF, w >> 13

        def make_bit(op, fn):
            def h():
                ea, bit = mem_bit()
                fn(ea, bit)
                S.pc = (S.pc + 3) & 0xFFFF
            ops[op] = h
        make_bit(0x0A, lambda ea, b: setattr(S, 'c', S.c | ((rd(ea) >> b) & 1)))
        make_bit(0x2A, lambda ea, b: setattr(S, 'c', S.c | (((rd(ea) >> b) & 1) ^ 1)))
        make_bit(0x4A, lambda ea, b: setattr(S, 'c', S.c & ((rd(ea) >> b) & 1)))
        make_bit(0x6A, lambda ea, b: setattr(S, 'c', S.c & (((rd(ea) >> b) & 1) ^ 1)))
        make_bit(0x8A, lambda ea, b: setattr(S, 'c', S.c ^ ((rd(ea) >> b) & 1)))
        make_bit(0xAA, lambda ea, b: setattr(S, 'c', (rd(ea) >> b) & 1))
        make_bit(0xCA, lambda ea, b: wr(ea, (rd(ea) & ~(1 << b) & 0xFF) | (S.c << b)))
        make_bit(0xEA, lambda ea, b: wr(ea, rd(ea) ^ (1 << b)))

        def stop():
            S.halted = True
            S.cyc += 1 << 40
        ops[0xEF] = stop
        ops[0xFF] = stop
        return ops

    def run_until(self, end):
        """Выполняет инструкции, пока они укладываются в end (абсолютные такты)."""
        ram = self.ram
        ops = self.ops
        cycles = CYCLES
        while True:
            pc = self.pc
            op = ram[pc]
            c = self.cyc + cycles[op]
            if c > end:
                break
            self.cyc = c
            ops[op]()
            if self.halted:
                break

    def play(self, nsamples):
        """Эмулирует nsamples отсчётов DSP (32 кГц)."""
        end = self.cyc_target = getattr(self, 'cyc_target', 0) + nsamples * CLOCKS_PER_SAMPLE
        self.run_until(end)
        if self.halted:
            self.cyc = end
        for t in self.timers:
            self.run_timer(t, end)
        self.run_dsp(end, MAX_REG_TIME)


# ═══════════════════════════════════════════════════════════════
# Фильтр GME и трассировщик
# ═══════════════════════════════════════════════════════════════

def gme_output(samples, total):
    """Как у GME без точного режима: усиление 1.4, 4 отсчёта тишины в начале."""
    g = int(0x100 * 1.4)
    out = [0] * 8
    out.extend(max(-32768, min(32767, (x * g) >> 8)) for x in samples)
    return out[:total * 2]


def trace(spc_path, prefix, seconds, raw_path=None, progress=None):
    with open(spc_path, 'rb') as f:
        data = f.read()
    emu = Spc(data)
    ftrace = open(prefix + '.trace', 'w')
    fpcm = open(prefix + '.pcm', 'wb')
    ftrace.write('# rate 32000\n')
    state = {'nsamp': 0}
    max_off = {}
    cap = 262144

    def on_kon(v, srcn, pitch, vl, vr, start, loop):
        ftrace.write('K %d %d %d %d %d %d %d %d\n' % (emu.dsp.nsamp - (1 if v == 0 else 0), v, srcn, pitch, vl, vr, start, loop))

    def on_voice(v, env, kon, srcn, pitch, vl, vr, pmon, non):
        n = emu.dsp.nsamp
        ftrace.write('F %d %d %d %d %d %d %d %d %d %d\n' % (n >> 7, v, srcn, pitch, env, vl, vr, kon, pmon, non))

    def on_brr(v, key, off, s4):
        if off >= cap:
            return
        mo = max_off.get(key)
        if mo is not None and off < mo:
            return
        max_off[key] = off + 4
        fpcm.write(struct.pack('<ii4h', key, off, *s4))

    emu.dsp.on_kon = on_kon
    emu.dsp.on_voice = on_voice
    emu.dsp.on_brr = on_brr
    total = int(seconds * 32000)
    done = 0
    raw = open(raw_path, 'wb') if raw_path else None
    chunk = 4096
    pending = []
    while done < total:
        n = min(chunk, total - done)
        emu.play(n)
        done += n
        out = emu.dsp.out
        if raw is not None:
            pending.extend(out)
        out.clear()
        if progress and (done // chunk) % 40 == 0:
            progress(done / total)
    ftrace.write('# end %d\n' % emu.dsp.nsamp)
    ftrace.close()
    fpcm.close()
    if raw is not None:
        filt = gme_output(pending, total)
        raw.write(struct.pack('<%dh' % len(filt), *filt))
        raw.close()


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    trace(sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else None)
