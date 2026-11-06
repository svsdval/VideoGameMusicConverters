// Встроенный экспорт SID -> XM / MIDI на C++ (порт sid2xm-midi-wav.py):
// 6502 исполняет init/play, регистры SID снимаются покадрово, из них строятся ноты.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

namespace sidexp {

constexpr int PAL_CLOCK = 985248, NTSC_CLOCK = 1022727;
enum Mode { MODE_TRI = 0, MODE_SAW, MODE_PULSE, MODE_NOISE, MODE_NONE = -1 };
enum EvT { NOTE_ON, NOTE_OFF };

struct Header
{
    std::string name, author, released;
    int version = 2, data_offset = 0x7C, load_addr = 0, init_addr = 0, play_addr = 0;
    int num_songs = 1, start_song = 1;
    uint32_t speed = 0;
    int flags = 0;
    bool is_pal = true;
    int second_sid = 0, third_sid = 0;
};

inline std::string pstr(const std::vector<uint8_t> & d, size_t a)
{
    std::string s;
    for (size_t i = a; i < a + 32 && i < d.size() && d[i]; i++) s += (char)d[i];
    while (!s.empty() && s.back() == ' ') s.pop_back();
    while (!s.empty() && s.front() == ' ') s.erase(0, 1);
    return s;
}

inline bool parse_sid(const std::vector<uint8_t> & d, Header & h, std::vector<uint8_t> & prg, int & load_addr)
{
    if (d.size() < 0x7C || (memcmp(d.data(), "PSID", 4) && memcmp(d.data(), "RSID", 4))) return false;
    auto be16 = [&](size_t o) { return (d[o] << 8) | d[o + 1]; };
    h.version = be16(4);
    h.data_offset = be16(6);
    h.load_addr = be16(8); h.init_addr = be16(10); h.play_addr = be16(12);
    h.num_songs = be16(14) ? be16(14) : 1;
    h.start_song = be16(16) ? be16(16) : 1;
    h.speed = ((uint32_t)be16(18) << 16) | be16(20);
    h.name = pstr(d, 22); h.author = pstr(d, 54); h.released = pstr(d, 86);
    if (h.version >= 2 && d.size() >= 0x76 + 8)
    {
        h.flags = be16(0x76);
        int clock_bits = (h.flags >> 2) & 3;
        h.is_pal = clock_bits != 2;
        if (h.version >= 3 && d.size() > 0x7A) h.second_sid = d[0x7A];
        if (h.version >= 4 && d.size() > 0x7B) h.third_sid = d[0x7B];
    }
    if ((size_t)h.data_offset > d.size()) return false;
    prg.assign(d.begin() + h.data_offset, d.end());
    if (h.load_addr == 0)
    {
        if (prg.size() < 2) return false;
        load_addr = prg[0] | (prg[1] << 8);
        prg.erase(prg.begin(), prg.begin() + 2);
    }
    else load_addr = h.load_addr;
    return true;
}

// ── 6502 (порт CPU6502: такты считаются как 2 на опкод и +1 за взятый переход) ──
class CPU
{
public:
    static constexpr int STOP_ADDR = 0xFFF0;
    int a = 0, x = 0, y = 0, sp = 0xFD, pc = 0;
    bool fc = false, fz = true, fi = true, fd = false, fv = false, fn = false;
    std::vector<uint8_t> ram;
    int64_t cycles = 0;

    CPU() : ram(0x10000, 0) {}

    int status() const
    {
        return (int)fc | ((int)fz << 1) | ((int)fi << 2) | ((int)fd << 3) | (1 << 4) | (1 << 5) | ((int)fv << 6) | ((int)fn << 7);
    }
    void set_status(int v) { fc = v & 1; fz = v & 2; fi = v & 4; fd = v & 8; fv = v & 0x40; fn = v & 0x80; }
    int read(int addr)
    {
        addr &= 0xFFFF;
        if (addr == 0xD012) return ((cycles / 63) % 312) & 0xFF;
        if (addr == 0xD011) return ((cycles / 63) % 312) > 255 ? 0x80 : 0;
        if (addr == 0xD41B) return (int)((cycles * 13) & 0xFF);
        if (addr == 0xD41C) return 0xFF;
        return ram[addr];
    }
    void write(int addr, int val) { ram[addr & 0xFFFF] = val & 0xFF; }
    int read16(int addr) { return read(addr) | (read((addr + 1) & 0xFFFF) << 8); }
    void push(int v) { ram[0x100 + sp] = v & 0xFF; sp = (sp - 1) & 0xFF; }
    int pop() { sp = (sp + 1) & 0xFF; return ram[0x100 + sp]; }
    void push16(int v) { push((v >> 8) & 0xFF); push(v & 0xFF); }
    int pop16() { int lo = pop(); return lo | (pop() << 8); }
    int nz(int v) { v &= 0xFF; fn = v & 0x80; fz = v == 0; return v; }
    void adc(int val)
    {
        int c = fc, r = a + val + c;
        fc = r > 0xFF;
        fv = ((~(a ^ val) & (a ^ r)) & 0x80) != 0;
        a = nz(r);
    }
    void sbc(int val) { adc(val ^ 0xFF); }
    void cmp(int p, int q) { int r = p - q; fc = r >= 0; nz(r & 0xFF); }

    bool call_subroutine(int addr, int64_t max_cycles = 200000)
    {
        push16((STOP_ADDR - 1) & 0xFFFF);
        pc = addr;
        int64_t start = cycles;
        while (cycles - start < max_cycles)
        {
            if (pc == STOP_ADDR) return true;
            step();
        }
        return false;
    }

    void step()
    {
        int op = ram[pc];
        pc = (pc + 1) & 0xFFFF;
        cycles += 2;
        auto imm = [&]() { int v = pc; pc = (pc + 1) & 0xFFFF; return v; };
        auto zpg = [&]() { int v = ram[pc]; pc = (pc + 1) & 0xFFFF; return v; };
        auto zpx = [&]() { int v = (ram[pc] + x) & 0xFF; pc = (pc + 1) & 0xFFFF; return v; };
        auto zpy = [&]() { int v = (ram[pc] + y) & 0xFF; pc = (pc + 1) & 0xFFFF; return v; };
        auto abso = [&]() { int v = read16(pc); pc = (pc + 2) & 0xFFFF; return v; };
        auto abx = [&]() { int v = (read16(pc) + x) & 0xFFFF; pc = (pc + 2) & 0xFFFF; return v; };
        auto aby = [&]() { int v = (read16(pc) + y) & 0xFFFF; pc = (pc + 2) & 0xFFFF; return v; };
        auto idx = [&]() {
            int z = (ram[pc] + x) & 0xFF; pc = (pc + 1) & 0xFFFF;
            return ram[z] | (ram[(z + 1) & 0xFF] << 8);
        };
        auto idy = [&]() {
            int z = ram[pc]; pc = (pc + 1) & 0xFFFF;
            int base = ram[z] | (ram[(z + 1) & 0xFF] << 8);
            return (base + y) & 0xFFFF;
        };
        auto branch = [&](bool cond) {
            int r = ram[pc]; pc = (pc + 1) & 0xFFFF;
            if (cond) { r = r >= 128 ? r - 256 : r; pc = (pc + r) & 0xFFFF; cycles += 1; }
        };
        // группы режимов для однотипных операций
        using AF = int (*)();
        (void)sizeof(AF);
        auto addr_of = [&](int m) -> int {   // 0 imm,1 zpg,2 zpx,3 zpy,4 abs,5 abx,6 aby,7 idx,8 idy
            switch (m) {
            case 0: return imm(); case 1: return zpg(); case 2: return zpx(); case 3: return zpy();
            case 4: return abso(); case 5: return abx(); case 6: return aby(); case 7: return idx();
            default: return idy();
            }
        };
        // ALU-блоки (opcode, режим)
        static const int M01[8] = {7, 1, 0, 4, 8, 2, 6, 5};
        if ((op & 3) == 1 && op != 0x89 && !(op == 0x81 || op == 0x85 || op == 0x8D || op == 0x91 || op == 0x95 || op == 0x99 || op == 0x9D || false))
        {
            int aaa = op >> 5, m = M01[(op >> 2) & 7];
            // в Python STA/LDA/... заданы только для существующих режимов; $89 и STA imm - отсутствуют
            if (aaa == 5) { a = nz(read(addr_of(m))); return; }                 // LDA
            if (aaa == 0) { a = nz(a | read(addr_of(m))); return; }             // ORA
            if (aaa == 1) { a = nz(a & read(addr_of(m))); return; }             // AND
            if (aaa == 2) { a = nz(a ^ read(addr_of(m))); return; }             // EOR
            if (aaa == 3) { adc(read(addr_of(m))); return; }                    // ADC
            if (aaa == 6) { cmp(a, read(addr_of(m))); return; }                 // CMP
            if (aaa == 7) { sbc(read(addr_of(m))); return; }                    // SBC
        }
        switch (op)
        {
        case 0xA2: x = nz(read(imm())); break; case 0xA6: x = nz(read(zpg())); break;
        case 0xB6: x = nz(read(zpy())); break; case 0xAE: x = nz(read(abso())); break;
        case 0xBE: x = nz(read(aby())); break;
        case 0xA0: y = nz(read(imm())); break; case 0xA4: y = nz(read(zpg())); break;
        case 0xB4: y = nz(read(zpx())); break; case 0xAC: y = nz(read(abso())); break;
        case 0xBC: y = nz(read(abx())); break;
        case 0x85: write(zpg(), a); break; case 0x95: write(zpx(), a); break; case 0x8D: write(abso(), a); break;
        case 0x9D: write(abx(), a); break; case 0x99: write(aby(), a); break; case 0x81: write(idx(), a); break;
        case 0x91: write(idy(), a); break;
        case 0x86: write(zpg(), x); break; case 0x96: write(zpy(), x); break; case 0x8E: write(abso(), x); break;
        case 0x84: write(zpg(), y); break; case 0x94: write(zpx(), y); break; case 0x8C: write(abso(), y); break;
        case 0xAA: x = nz(a); break; case 0xA8: y = nz(a); break; case 0x8A: a = nz(x); break; case 0x98: a = nz(y); break;
        case 0xBA: x = nz(sp); break; case 0x9A: sp = x; break;
        case 0x48: push(a); break; case 0x08: push(status()); break;
        case 0x68: a = nz(pop()); break; case 0x28: set_status(pop()); break;
        case 0xE0: cmp(x, read(imm())); break; case 0xE4: cmp(x, read(zpg())); break; case 0xEC: cmp(x, read(abso())); break;
        case 0xC0: cmp(y, read(imm())); break; case 0xC4: cmp(y, read(zpg())); break; case 0xCC: cmp(y, read(abso())); break;
        case 0xE6: { int ad = zpg(); write(ad, nz((read(ad) + 1) & 0xFF)); break; }
        case 0xF6: { int ad = zpx(); write(ad, nz((read(ad) + 1) & 0xFF)); break; }
        case 0xEE: { int ad = abso(); write(ad, nz((read(ad) + 1) & 0xFF)); break; }
        case 0xFE: { int ad = abx(); write(ad, nz((read(ad) + 1) & 0xFF)); break; }
        case 0xC6: { int ad = zpg(); write(ad, nz((read(ad) - 1) & 0xFF)); break; }
        case 0xD6: { int ad = zpx(); write(ad, nz((read(ad) - 1) & 0xFF)); break; }
        case 0xCE: { int ad = abso(); write(ad, nz((read(ad) - 1) & 0xFF)); break; }
        case 0xDE: { int ad = abx(); write(ad, nz((read(ad) - 1) & 0xFF)); break; }
        case 0xE8: x = nz((x + 1) & 0xFF); break; case 0xC8: y = nz((y + 1) & 0xFF); break;
        case 0xCA: x = nz((x - 1) & 0xFF); break; case 0x88: y = nz((y - 1) & 0xFF); break;
        case 0x0A: { int c = a >> 7; a = nz((a << 1) & 0xFF); fc = c; break; }
        case 0x06: case 0x16: case 0x0E: case 0x1E:
        {
            int ad = op == 0x06 ? zpg() : op == 0x16 ? zpx() : op == 0x0E ? abso() : abx();
            int v = read(ad), c = v >> 7;
            write(ad, nz((v << 1) & 0xFF)); fc = c; break;
        }
        case 0x4A: { int c = a & 1; a = nz(a >> 1); fc = c; break; }
        case 0x46: case 0x56: case 0x4E: case 0x5E:
        {
            int ad = op == 0x46 ? zpg() : op == 0x56 ? zpx() : op == 0x4E ? abso() : abx();
            int v = read(ad), c = v & 1;
            write(ad, nz(v >> 1)); fc = c; break;
        }
        case 0x2A: { int c = fc, nc = a >> 7; a = nz(((a << 1) | c) & 0xFF); fc = nc; break; }
        case 0x26: case 0x36: case 0x2E: case 0x3E:
        {
            int ad = op == 0x26 ? zpg() : op == 0x36 ? zpx() : op == 0x2E ? abso() : abx();
            int v = read(ad), c = fc, nc = v >> 7;
            write(ad, nz(((v << 1) | c) & 0xFF)); fc = nc; break;
        }
        case 0x6A: { int c = fc, nc = a & 1; a = nz((a >> 1) | (c << 7)); fc = nc; break; }
        case 0x66: case 0x76: case 0x6E: case 0x7E:
        {
            int ad = op == 0x66 ? zpg() : op == 0x76 ? zpx() : op == 0x6E ? abso() : abx();
            int v = read(ad), c = fc, nc = v & 1;
            write(ad, nz((v >> 1) | (c << 7))); fc = nc; break;
        }
        case 0x24: { int v = read(zpg()); fn = v & 0x80; fv = v & 0x40; fz = (a & v) == 0; break; }
        case 0x2C: { int v = read(abso()); fn = v & 0x80; fv = v & 0x40; fz = (a & v) == 0; break; }
        case 0x10: branch(!fn); break; case 0x30: branch(fn); break;
        case 0x50: branch(!fv); break; case 0x70: branch(fv); break;
        case 0x90: branch(!fc); break; case 0xB0: branch(fc); break;
        case 0xD0: branch(!fz); break; case 0xF0: branch(fz); break;
        case 0x4C: pc = abso(); break;
        case 0x6C: { int ad = abso(); pc = ram[ad] | (ram[(ad & 0xFF00) | ((ad + 1) & 0xFF)] << 8); break; }
        case 0x20: { int ad = abso(); push16((pc - 1) & 0xFFFF); pc = ad; break; }
        case 0x60: pc = (pop16() + 1) & 0xFFFF; break;
        case 0x40: set_status(pop()); pc = pop16(); break;
        case 0x18: fc = false; break; case 0x38: fc = true; break;
        case 0x58: fi = false; break; case 0x78: fi = true; break;
        case 0xB8: fv = false; break; case 0xD8: fd = false; break; case 0xF8: fd = true; break;
        case 0xEA: break;
        case 0x00: pc = (pc + 1) & 0xFFFF; break;
        case 0x1A: case 0x3A: case 0x5A: case 0x7A: case 0xDA: case 0xFA: break;
        case 0x04: case 0x44: case 0x64: pc = (pc + 1) & 0xFFFF; break;
        case 0x0C: case 0x14: case 0x34: case 0x54: case 0x74: case 0xD4: case 0xF4: pc = (pc + 1) & 0xFFFF; break;
        case 0x1C: case 0x3C: case 0x5C: case 0x7C: case 0xDC: case 0xFC: pc = (pc + 2) & 0xFFFF; break;
        case 0x80: case 0x89: pc = (pc + 1) & 0xFFFF; break;
        // LAX
        case 0xA7: { int v = read(zpg()); a = nz(v); x = v; break; }
        case 0xB7: { int v = read(zpy()); a = nz(v); x = v; break; }
        case 0xAF: { int v = read(abso()); a = nz(v); x = v; break; }
        case 0xBF: { int v = read(aby()); a = nz(v); x = v; break; }
        case 0xA3: { int v = read(idx()); a = nz(v); x = v; break; }
        case 0xB3: { int v = read(idy()); a = nz(v); x = v; break; }
        // SAX
        case 0x87: write(zpg(), a & x); break; case 0x97: write(zpy(), a & x); break;
        case 0x8F: write(abso(), a & x); break; case 0x83: write(idx(), a & x); break;
        // DCP
        case 0xC7: case 0xD7: case 0xCF: case 0xDF: case 0xDB: case 0xC3: case 0xD3:
        {
            int ad = op == 0xC7 ? zpg() : op == 0xD7 ? zpx() : op == 0xCF ? abso() : op == 0xDF ? abx() :
                     op == 0xDB ? aby() : op == 0xC3 ? idx() : idy();
            int v = (read(ad) - 1) & 0xFF; write(ad, v); cmp(a, v); break;
        }
        // ISB
        case 0xE7: case 0xF7: case 0xEF: case 0xFF: case 0xFB: case 0xE3: case 0xF3:
        {
            int ad = op == 0xE7 ? zpg() : op == 0xF7 ? zpx() : op == 0xEF ? abso() : op == 0xFF ? abx() :
                     op == 0xFB ? aby() : op == 0xE3 ? idx() : idy();
            int v = (read(ad) + 1) & 0xFF; write(ad, v); sbc(v); break;
        }
        // SLO
        case 0x07: case 0x17: case 0x0F: case 0x1F: case 0x1B: case 0x03: case 0x13:
        {
            int ad = op == 0x07 ? zpg() : op == 0x17 ? zpx() : op == 0x0F ? abso() : op == 0x1F ? abx() :
                     op == 0x1B ? aby() : op == 0x03 ? idx() : idy();
            int v = read(ad); fc = v & 0x80; v = (v << 1) & 0xFF; write(ad, v); a = nz(a | v); break;
        }
        // RLA
        case 0x27: case 0x37: case 0x2F: case 0x3F: case 0x3B: case 0x23: case 0x33:
        {
            int ad = op == 0x27 ? zpg() : op == 0x37 ? zpx() : op == 0x2F ? abso() : op == 0x3F ? abx() :
                     op == 0x3B ? aby() : op == 0x23 ? idx() : idy();
            int v = read(ad), c = fc; fc = v & 0x80; v = ((v << 1) | c) & 0xFF; write(ad, v); a = nz(a & v); break;
        }
        // RRA
        case 0x67: case 0x77: case 0x6F: case 0x7F: case 0x7B: case 0x63: case 0x73:
        {
            int ad = op == 0x67 ? zpg() : op == 0x77 ? zpx() : op == 0x6F ? abso() : op == 0x7F ? abx() :
                     op == 0x7B ? aby() : op == 0x63 ? idx() : idy();
            int v = read(ad), c = fc, nc = v & 1; v = (v >> 1) | (c << 7); write(ad, v); fc = nc; adc(v); break;
        }
        // SRE
        case 0x47: case 0x57: case 0x4F: case 0x5F: case 0x5B: case 0x43: case 0x53:
        {
            int ad = op == 0x47 ? zpg() : op == 0x57 ? zpx() : op == 0x4F ? abso() : op == 0x5F ? abx() :
                     op == 0x5B ? aby() : op == 0x43 ? idx() : idy();
            int v = read(ad); fc = v & 1; v >>= 1; write(ad, v); a = nz(a ^ v); break;
        }
        default: break;   // неизвестный опкод - игнорируется
        }
    }
};

struct Runner
{
    Header h;
    CPU cpu;
    std::vector<int> sid_bases;
    int num_chips = 1;

    Runner(const Header & hd, const std::vector<uint8_t> & prg, int load_addr) : h(hd)
    {
        cpu.ram[0] = 0x2F; cpu.ram[1] = 0x37;
        for (size_t i = 0; i < prg.size(); i++)
        {
            size_t ad = (size_t)load_addr + i;
            if (ad < 0x10000) cpu.ram[ad] = prg[i];
        }
        int s = CPU::STOP_ADDR;
        cpu.ram[s] = 0x4C; cpu.ram[s + 1] = s & 0xFF; cpu.ram[s + 2] = (s >> 8) & 0xFF;
        sid_bases.push_back(0xD400);
        for (int raw : {hd.second_sid, hd.third_sid})
            if (raw)
            {
                int ad = 0xD000 | ((raw & 0xFF) << 4);
                if (std::find(sid_bases.begin(), sid_bases.end(), ad) == sid_bases.end()) sid_bases.push_back(ad);
            }
        num_chips = (int)sid_bases.size();
    }
    bool init_song(int idx0)
    {
        cpu.a = idx0 & 0xFF; cpu.x = 0; cpu.y = 0; cpu.sp = 0xFD; cpu.fi = false;
        return cpu.call_subroutine(h.init_addr, 2000000);
    }
    // frames[frame][chip] = 25 байт
    std::vector<std::vector<std::array<uint8_t, 25>>> simulate(int num_frames)
    {
        std::vector<std::vector<std::array<uint8_t, 25>>> out;
        for (int f = 0; f < num_frames; f++)
        {
            if (h.play_addr) cpu.call_subroutine(h.play_addr, 300000);
            else
            {
                int vec = cpu.read16(0xFFFE);
                if (vec && vec != CPU::STOP_ADDR) cpu.call_subroutine(vec, 300000);
            }
            std::vector<std::array<uint8_t, 25>> snap;
            for (int b : sid_bases)
            {
                std::array<uint8_t, 25> r;
                for (int i = 0; i < 25; i++) r[i] = cpu.ram[b + i];
                snap.push_back(r);
            }
            out.push_back(std::move(snap));
        }
        return out;
    }
};

struct NoteEvent { int frame = 0, channel = 0, voice = 0, mode = MODE_NONE; EvT type = NOTE_OFF; int note = 0, instrument = 1, volume = 64; };
using ChannelMap = std::map<std::pair<int, int>, int>;

inline ChannelMap preset(int id, int num_chips)
{
    int nv = num_chips * 3;
    ChannelMap m;
    for (int v = 0; v < nv; v++)
        for (int mo = 0; mo < 4; mo++)
        {
            if (id == 1) m[{v, mo}] = v * 4 + mo;                       // split
            else if (id == 2) m[{v, mo}] = (mo == MODE_NOISE) ? nv : v;  // compact
            else m[{v, mo}] = v;                                         // default
        }
    return m;
}

inline int freq_to_xm_note(double freq)
{
    if (freq < 15 || freq > 20000) return 0;
    int xm = (int)std::nearbyint(69 + 12 * std::log2(freq / 440.0)) - 11;
    return (xm >= 1 && xm <= 96) ? xm : 0;
}

inline std::vector<NoteEvent> extract(const std::vector<std::vector<std::array<uint8_t, 25>>> & frames,
                                      const ChannelMap & cmap, double clock, int num_chips)
{
    std::vector<NoteEvent> ev;
    int mx = 0;
    for (auto & kv : cmap) mx = std::max(mx, kv.second + 1);
    int nv = num_chips * 3;
    std::vector<int> pn(mx, -1), pi(mx, 0), pax(nv, -1), pmode(nv, MODE_NONE);
    int last_frame = 0;
    auto gx = [&](int v, int m) {
        auto it = cmap.find({v, m});
        if (it != cmap.end()) return it->second;
        for (int f : {MODE_PULSE, MODE_SAW, MODE_TRI, MODE_NOISE})
        {
            auto i2 = cmap.find({v, f});
            if (i2 != cmap.end()) return i2->second;
        }
        return v % std::max(1, mx);
    };
    auto push = [&](int frame, int ch, int voice, int mode, EvT t, int note, int inst) {
        NoteEvent e; e.frame = frame; e.channel = ch; e.voice = voice; e.mode = mode; e.type = t; e.note = note;
        e.instrument = inst; e.volume = 64;
        ev.push_back(e);
    };
    for (size_t frame = 0; frame < frames.size(); frame++)
    {
        last_frame = (int)frame;
        for (int gv = 0; gv < nv; gv++)
        {
            int chip = gv / 3, v = gv % 3;
            const auto & regs = frames[frame][chip];
            int b = v * 7;
            int freq = regs[b] | (regs[b + 1] << 8), ctrl = regs[b + 4];
            bool gate = ctrl & 1;
            int wf = (ctrl >> 4) & 0xF;
            int note = 0, mode = MODE_NONE, inst = 0;
            if (gate && wf && freq > 0)
            {
                note = freq_to_xm_note(freq * clock / 16777216.0);
                if (wf & 0x4) { mode = MODE_PULSE; inst = 3; }
                else if (wf & 0x2) { mode = MODE_SAW; inst = 2; }
                else if (wf & 0x1) { mode = MODE_TRI; inst = 1; }
                else if (wf & 0x8) { mode = MODE_NOISE; inst = 4; }
            }
            if (!note || mode == MODE_NONE)
            {
                int ox = pax[gv];
                if (ox >= 0 && pn[ox] >= 0)
                {
                    push((int)frame, ox, gv, pmode[gv] == MODE_NONE ? MODE_NONE : pmode[gv], NOTE_OFF, 97, 1);
                    pn[ox] = -1;
                }
                pmode[gv] = MODE_NONE; pax[gv] = -1;
                continue;
            }
            int xc = gx(gv, mode);
            int ox = pax[gv];
            if (ox >= 0 && ox != xc && pn[ox] >= 0)
            {
                push((int)frame, ox, gv, pmode[gv], NOTE_OFF, 97, 1);
                pn[ox] = -1;
            }
            pmode[gv] = mode; pax[gv] = xc;
            if (pn[xc] < 0 || note != pn[xc] || inst != pi[xc])
            {
                if (pn[xc] >= 0) push((int)frame, xc, gv, mode, NOTE_OFF, 97, 1);
                push((int)frame, xc, gv, mode, NOTE_ON, note, inst);
                pn[xc] = note; pi[xc] = inst;
            }
        }
    }
    for (int xc = 0; xc < mx; xc++)
        if (pn[xc] >= 0)
        {
            NoteEvent e; e.frame = last_frame; e.channel = xc; e.type = NOTE_OFF; e.note = 97;
            ev.push_back(e);
        }
    return ev;
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
    if (v < 0) v = 0;
    std::vector<uint8_t> r = {(uint8_t)(v & 0x7F)};
    v >>= 7;
    while (v) { r.push_back((uint8_t)((v & 0x7F) | 0x80)); v >>= 7; }
    std::reverse(r.begin(), r.end());
    b.raw(r);
}

struct SInst { std::string name; std::vector<int> data; int loop_len; int loop_type; };

inline std::vector<SInst> make_insts()
{
    const int P = 32;
    std::vector<int> tri, saw, pul, noi;
    for (int i = 0; i < P; i++) tri.push_back((int)((4 * std::fabs((double)i / P - 0.5) - 1) * 24000));
    for (int i = 0; i < P; i++) saw.push_back((int)((((double)i / P) * 2 - 1) * 24000));
    int n = (int)(P * 0.5);
    for (int i = 0; i < P; i++) pul.push_back(i < n ? 24000 : -24000);
    int l = 0x7FFFFF;
    for (int i = 0; i < 4096; i++)
    {
        int bit = ((l >> 22) ^ (l >> 17)) & 1;
        l = ((l << 1) | bit) & 0x7FFFFF;
        noi.push_back((l & 1) ? 24000 : -24000);
    }
    return {{"SID Triangle", tri, (int)tri.size(), 1}, {"SID Sawtooth", saw, (int)saw.size(), 1},
            {"SID Pulse", pul, (int)pul.size(), 1}, {"SID Noise", noi, 0, 0}};
}

struct TN { int note = 0, instrument = 0, volume = 0; };

inline bool write_xm(const std::string & fn, const std::vector<NoteEvent> & events, const std::string & title,
                     int bpm, int speed, double fr, int nc)
{
    if (nc % 2) nc++;
    nc = std::max(4, std::min(32, nc));
    auto insts = make_insts();
    double rps = speed * 2.5 / bpm;
    int fpr = std::max(1, (int)(fr * rps));
    std::map<std::pair<int, int>, TN> rd;
    for (auto & e : events)
    {
        int row = e.frame / fpr, ch = e.channel;
        if (ch >= nc) continue;
        auto k = std::make_pair(row, ch);
        if (e.type == NOTE_ON)
            rd[k] = {std::max(1, std::min(96, e.note)), std::min<int>(e.instrument, insts.size()), 0x10 + std::min(0x40, std::max(0, e.volume))};
        else if (!rd.count(k)) rd[k] = {97, 0, 0};
    }
    int mr = 0;
    for (auto & kv : rd) mr = std::max(mr, kv.first.first);
    mr += 1;
    const int rpp = 64;
    int np = std::max(1, std::min(256, (mr + rpp - 1) / rpp));
    Buf b;
    b.str("Extended Module: ", 17, 0);
    b.str(title, 20, 0);
    b.u8(0x1a);
    b.str("SID2XM Converter    ", 20, ' ');
    b.le16(0x0104); b.le32(276); b.le16(np); b.le16(0); b.le16(nc); b.le16(np); b.le16((int)std::min<size_t>(insts.size(), 128));
    b.le16(1); b.le16(speed); b.le16(bpm);
    for (int i = 0; i < 256; i++) b.u8(i < np ? i : 0);
    for (int pi = 0; pi < np; pi++)
    {
        Buf pk;
        for (int ri = 0; ri < rpp; ri++)
            for (int ch = 0; ch < nc; ch++)
            {
                auto it = rd.find({pi * rpp + ri, ch});
                TN nd = it != rd.end() ? it->second : TN();
                bool hn = nd.note > 0, hi = nd.instrument > 0, hv = nd.volume > 0;
                if (!(hn || hi || hv)) { pk.u8(0x80); continue; }
                pk.u8(0x80 | (hn ? 1 : 0) | (hi ? 2 : 0) | (hv ? 4 : 0));
                if (hn) pk.u8(nd.note & 0xFF);
                if (hi) pk.u8(nd.instrument & 0xFF);
                if (hv) pk.u8(nd.volume & 0xFF);
            }
        b.le32(9); b.u8(0); b.le16(rpp); b.le16((int)pk.d.size()); b.raw(pk.d);
    }
    for (size_t k = 0; k < insts.size() && k < 128; k++)
    {
        auto & in = insts[k];
        const int ihs = 263;
        b.le32(ihs); b.str(in.name, 22, 0);
        b.u8(0); b.le16(1); b.le32(40);
        for (int i = 0; i < 96; i++) b.u8(0);
        { uint8_t ve[48] = {0}; ve[2] = 64; ve[4] = 100; ve[6] = 64; b.raw({ve, ve + 48}); }
        { uint8_t pe[48] = {0}; pe[2] = 32; pe[4] = 100; pe[6] = 32; b.raw({pe, pe + 48}); }
        b.raw({2, 2, 0, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0});
        b.le16(0x800);
        for (int i = 0; i < ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2); i++) b.u8(0);
        b.le32((uint32_t)in.data.size() * 2); b.le32(0); b.le32((uint32_t)in.loop_len * 2);
        b.u8(64); b.u8(0);
        b.u8((in.loop_type & 3) | 0x10); b.u8(128); b.u8(0); b.u8(0);
        b.str(in.name, 22, 0);
        int prev = 0;
        for (int v : in.data)
        {
            v = std::max(-32768, std::min(32767, v));
            int d = (((v - prev) + 32768) % 65536 + 65536) % 65536 - 32768;
            b.le16(d & 0xFFFF);
            prev = v;
        }
    }
    return b.save(fn);
}

inline bool write_midi(const std::string & fn, const std::vector<NoteEvent> & events, int bpm, double fr,
                       const std::string & title)
{
    const int tpqn = 480;
    double tpf = tpqn * (double)bpm / (60.0 * fr);
    int usec = (int)(60000000 / bpm);
    std::map<int, std::vector<const NoteEvent *>> chs;
    for (auto & e : events) chs[e.channel].push_back(&e);
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
    static const int gm[4] = {81, 82, 80, 127};
    int ci = 0;
    for (auto & kv : chs)
    {
        if (ci >= 15) break;
        int mc = ci < 9 ? ci : ci + 1;
        std::vector<TEv> trk;
        trk.push_back({0, {(uint8_t)(0xC0 | mc), (uint8_t)gm[ci % 4]}});
        bool active = false;
        int act_note = 0, act_t = 0;
        for (auto * e : kv.second)
        {
            int mt = (int)(e->frame * tpf);
            if (e->type == NOTE_ON)
            {
                int mn = std::max(0, std::min(127, e->note + 11)), vel = std::max(1, std::min(127, e->volume * 2));
                if (active) trk.push_back({mt, {(uint8_t)(0x80 | mc), (uint8_t)act_note, 0}});
                trk.push_back({mt, {(uint8_t)(0x90 | mc), (uint8_t)mn, (uint8_t)vel}});
                active = true; act_note = mn; act_t = mt;
            }
            else if (active)
            {
                trk.push_back({mt, {(uint8_t)(0x80 | mc), (uint8_t)act_note, 0}});
                active = false;
            }
        }
        if (active) trk.push_back({act_t + tpqn, {(uint8_t)(0x80 | mc), (uint8_t)act_note, 0}});
        tracks.push_back(trk);
        ci++;
    }
    Buf f;
    f.str("MThd", 4, 0); f.be32(6); f.be16(1); f.be16((int)tracks.size()); f.be16(tpqn);
    for (auto & t : tracks)
    {
        std::stable_sort(t.begin(), t.end(), [](const TEv & a, const TEv & b) { return a.first < b.first; });
        Buf d;
        int prev = 0;
        for (auto & e : t) { vlq(d, std::max(0, e.first - prev)); d.raw(e.second); prev = e.first; }
        vlq(d, 0); d.raw({0xFF, 0x2F, 0x00});
        f.str("MTrk", 4, 0); f.be32((uint32_t)d.d.size()); f.raw(d.d);
    }
    return f.save(fn);
}

// song: с 1 (0 = стартовый). seconds: сколько эмулировать.
inline std::string export_sid(const std::string & path, int song, const std::string & out_base, bool want_xm,
                              bool want_midi, double seconds, int layout)
{
    FILE * f = fopen(path.c_str(), "rb");
    if (!f) return "cannot read file";
    std::vector<uint8_t> data;
    uint8_t buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) data.insert(data.end(), buf, buf + n);
    fclose(f);
    Header h;
    std::vector<uint8_t> prg;
    int load_addr;
    if (!parse_sid(data, h, prg, load_addr)) return "not a PSID/RSID file";
    double clock = h.is_pal ? PAL_CLOCK : NTSC_CLOCK;
    int speed_bit = h.speed & 1;
    double frame_rate = speed_bit ? 60.0 : (h.is_pal ? 50.0 : 60.0);
    int song_idx = song > 0 ? song - 1 : h.start_song - 1;
    Runner runner(h, prg, load_addr);
    ChannelMap cmap = preset(layout, runner.num_chips);
    int num_xm = 0;
    for (auto & kv : cmap) num_xm = std::max(num_xm, kv.second + 1);
    runner.init_song(song_idx);
    int num_frames = std::max(1, (int)(seconds * frame_rate));
    auto frames = runner.simulate(num_frames);
    auto ev = extract(frames, cmap, clock, runner.num_chips);
    int note_ons = 0;
    for (auto & e : ev) if (e.type == NOTE_ON) note_ons++;
    if (!note_ons) return "no notes found";
    if (want_xm && !write_xm(out_base + ".xm", ev, h.name, 125, 6, frame_rate, num_xm)) return "cannot write " + out_base + ".xm";
    if (want_midi && !write_midi(out_base + ".mid", ev, 125, frame_rate, h.name)) return "cannot write " + out_base + ".mid";
    return "";
}

} // namespace sidexp
