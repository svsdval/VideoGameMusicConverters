"""
NSF to XM/MIDI Converter v2
Исправлен эмулятор 6502: bankswitch, branches, trampoline.
"""

import struct
import sys
import os
import math
from dataclasses import dataclass, field
from typing import Optional


NTSC_CPU_CLOCK = 1789773
PAL_CPU_CLOCK = 1662607
NTSC_FRAME_RATE = 60.0988
PAL_FRAME_RATE = 50.0070
NOTE_NAMES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-']
SAMPLE_PERIOD = 32


def freq_to_xm_note(freq):
    if freq <= 0 or freq < 20 or freq > 16000: return None
    midi = 69 + 12 * math.log2(freq / 440.0)
    xm = int(round(midi)) - 11
    return xm if 1 <= xm <= 96 else None

def xm_note_to_midi(xm_note): return xm_note + 11

def note_name(xm_note):
    if not xm_note or xm_note < 1 or xm_note > 96: return "---"
    n = xm_note - 1; return f"{NOTE_NAMES[n%12]}{n//12}"


@dataclass
class NSFHeader:
    version: int = 0; total_songs: int = 1; starting_song: int = 1
    load_addr: int = 0x8000; init_addr: int = 0x8000; play_addr: int = 0x8000
    title: str = ""; artist: str = ""; copyright: str = ""
    ntsc_speed: int = 16666; pal_speed: int = 20000
    bankswitch: bytes = b'\x00' * 8
    is_pal: bool = False; is_dual: bool = False; extra_chips: int = 0

@dataclass
class TrackerNote:
    note: int = 0; instrument: int = 0; volume: int = 0
    effect: int = 0; effect_param: int = 0

@dataclass
class NoteEvent:
    frame: int = 0; channel: int = 0; event_type: str = ""
    note: int = 0; instrument: int = 0; volume: int = 64


class NSFParser:
    def __init__(self, filename):
        with open(filename, 'rb') as f: self.raw = f.read()
        if self.raw[:5] != b'NESM\x1a': raise ValueError("Not NSF")
        self.header = self._parse_header()
        self.prg_data = self.raw[0x80:]

    def _parse_header(self):
        d = self.raw; h = NSFHeader()
        h.version = d[0x05]; h.total_songs = d[0x06]; h.starting_song = d[0x07]
        h.load_addr = struct.unpack_from('<H', d, 0x08)[0]
        h.init_addr = struct.unpack_from('<H', d, 0x0A)[0]
        h.play_addr = struct.unpack_from('<H', d, 0x0C)[0]
        h.title = d[0x0E:0x2E].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        h.artist = d[0x2E:0x4E].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        h.copyright = d[0x4E:0x6E].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        h.ntsc_speed = struct.unpack_from('<H', d, 0x6E)[0]
        h.bankswitch = d[0x70:0x78]
        h.pal_speed = struct.unpack_from('<H', d, 0x78)[0]
        h.is_pal = bool(d[0x7A] & 0x01); h.is_dual = bool(d[0x7A] & 0x02)
        h.extra_chips = d[0x7B]
        return h

    @property
    def has_bankswitch(self):
        return any(b != 0 for b in self.header.bankswitch)


class APUState:
    def __init__(self, cpu_clock=NTSC_CPU_CLOCK):
        self.cpu_clock = cpu_clock
        self.regs = bytearray(0x20)
        self.current_note = [-1, -1, -1, -1]
    def write_reg(self, addr, val):
        if 0x4000 <= addr <= 0x401F: self.regs[addr - 0x4000] = val
    def get_pulse_freq(self, ch):
        base = ch * 4
        timer = self.regs[base + 2] | ((self.regs[base + 3] & 0x07) << 8)
        return self.cpu_clock / (16.0 * (timer + 1)) if timer >= 8 else 0
    def get_pulse_volume(self, ch):
        vol = self.regs[ch * 4] & 0x0F
        return max(1, int(64 * vol / 15)) if vol > 0 else 0
    def get_pulse_duty(self, ch):
        return (self.regs[ch * 4] >> 6) & 0x03
    def is_pulse_enabled(self, ch):
        return bool(self.regs[0x15] & (1 << ch))
    def get_triangle_freq(self):
        timer = self.regs[0x0A] | ((self.regs[0x0B] & 0x07) << 8)
        return self.cpu_clock / (32.0 * (timer + 1)) if timer >= 2 else 0
    def is_triangle_enabled(self):
        return bool(self.regs[0x15] & 0x04)
    def get_triangle_volume(self):
        return 48 if (self.regs[0x08] & 0x7F) > 0 else 0
    def get_noise_period(self):
        return self.regs[0x0E] & 0x0F
    def get_noise_volume(self):
        vol = self.regs[0x0C] & 0x0F
        return max(1, int(64 * vol / 15)) if vol > 0 else 0
    def is_noise_enabled(self):
        return bool(self.regs[0x15] & 0x08)
    NOISE_NOTES = [84,72,66,60,54,48,42,36,30,24,18,12,10,8,6,4]
    def get_noise_note(self):
        return self.NOISE_NOTES[min(self.get_noise_period(), 15)]


# ═══════════════════════════════════════════════════════════════
# MOS 6502 — исправленный
# ═══════════════════════════════════════════════════════════════

class CPU6502:
    """Эмулятор MOS 6502 для NSF"""

    # Маркер: PC когда нужно остановиться
    STOP_ADDR = 0x3FF6

    def __init__(self):
        self.a = 0; self.x = 0; self.y = 0; self.sp = 0xFD; self.pc = 0
        self.flag_c = False; self.flag_z = True; self.flag_i = True
        self.flag_d = False; self.flag_v = False; self.flag_n = False
        self.ram = bytearray(0x10000)
        self.apu_writes = []
        self.current_frame = 0
        self.cycles = 0
        # Bankswitch
        self.banks = [0] * 8  # текущие банки для $8000-$FFFF
        self.prg_rom = b''    # полный PRG-ROM

    def get_status(self):
        return (int(self.flag_c) | (int(self.flag_z) << 1) |
                (int(self.flag_i) << 2) | (int(self.flag_d) << 3) |
                (1 << 4) | (1 << 5) |
                (int(self.flag_v) << 6) | (int(self.flag_n) << 7))

    def set_status(self, v):
        self.flag_c = bool(v & 0x01); self.flag_z = bool(v & 0x02)
        self.flag_i = bool(v & 0x04); self.flag_d = bool(v & 0x08)
        self.flag_v = bool(v & 0x40); self.flag_n = bool(v & 0x80)

    def read(self, addr):
        addr &= 0xFFFF
        # APU status read
        if addr == 0x4015:
            return self.ram[addr]
        return self.ram[addr]

    def write(self, addr, val):
        addr &= 0xFFFF; val &= 0xFF
        # APU registers
        if 0x4000 <= addr <= 0x4017:
            self.ram[addr] = val
            self.apu_writes.append((self.current_frame, addr, val))
            return
        # Bankswitch registers ($5FF8-$5FFF)
        if 0x5FF8 <= addr <= 0x5FFF:
            bank_slot = addr - 0x5FF8  # 0-7
            self.banks[bank_slot] = val
            self._apply_bank(bank_slot, val)
            return
        self.ram[addr] = val

    def _apply_bank(self, slot, bank_num):
        """Загружает 4KB банк из PRG-ROM в RAM"""
        src = bank_num * 0x1000
        dst = 0x8000 + slot * 0x1000
        if src + 0x1000 <= len(self.prg_rom):
            for i in range(0x1000):
                self.ram[dst + i] = self.prg_rom[src + i]

    def read16(self, addr):
        return self.read(addr) | (self.read((addr + 1) & 0xFFFF) << 8)

    def push(self, v):
        self.ram[0x100 + self.sp] = v & 0xFF
        self.sp = (self.sp - 1) & 0xFF

    def pop(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.ram[0x100 + self.sp]

    def push16(self, v):
        self.push((v >> 8) & 0xFF); self.push(v & 0xFF)

    def pop16(self):
        lo = self.pop(); return lo | (self.pop() << 8)

    def nz(self, v):
        v &= 0xFF; self.flag_n = bool(v & 0x80); self.flag_z = (v == 0); return v

    def adc(self, val):
        c = int(self.flag_c); r = self.a + val + c
        self.flag_c = r > 0xFF
        self.flag_v = bool((~(self.a ^ val) & (self.a ^ r)) & 0x80)
        self.a = self.nz(r)

    def sbc(self, val):
        self.adc(val ^ 0xFF)

    def cmp(self, a, b):
        r = a - b; self.flag_c = r >= 0; self.nz(r & 0xFF)

    def call_subroutine(self, addr, max_cycles=500000):
        """Вызывает подпрограмму и ждёт возврата"""
        # Устанавливаем return address на STOP_ADDR
        self.push16((self.STOP_ADDR - 1) & 0xFFFF)
        self.pc = addr
        start = self.cycles
        while self.cycles - start < max_cycles:
            if self.pc == self.STOP_ADDR:
                return True  # нормальный возврат
            self.step()
        return False  # таймаут

    def step(self):
        op = self.ram[self.pc]; self.pc = (self.pc + 1) & 0xFFFF
        self.cycles += 2

        # Helpers
        def imm():
            v = self.pc; self.pc = (self.pc + 1) & 0xFFFF; return v
        def zpg():
            v = self.ram[self.pc]; self.pc = (self.pc + 1) & 0xFFFF; return v
        def zpx():
            v = (self.ram[self.pc] + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF; return v
        def zpy():
            v = (self.ram[self.pc] + self.y) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF; return v
        def abso():
            v = self.read16(self.pc); self.pc = (self.pc + 2) & 0xFFFF; return v
        def abx():
            v = (self.read16(self.pc) + self.x) & 0xFFFF; self.pc = (self.pc + 2) & 0xFFFF; return v
        def aby():
            v = (self.read16(self.pc) + self.y) & 0xFFFF; self.pc = (self.pc + 2) & 0xFFFF; return v
        def idx():
            z = (self.ram[self.pc] + self.x) & 0xFF; self.pc = (self.pc + 1) & 0xFFFF
            return self.ram[z] | (self.ram[(z+1) & 0xFF] << 8)
        def idy():
            z = self.ram[self.pc]; self.pc = (self.pc + 1) & 0xFFFF
            base = self.ram[z] | (self.ram[(z+1) & 0xFF] << 8)
            return (base + self.y) & 0xFFFF
        def branch(cond):
            r = self.ram[self.pc]; self.pc = (self.pc + 1) & 0xFFFF
            if cond:
                r = r - 256 if r >= 128 else r
                self.pc = (self.pc + r) & 0xFFFF; self.cycles += 1

        # LDA
        if op == 0xA9: self.a = self.nz(self.read(imm()))
        elif op == 0xA5: self.a = self.nz(self.read(zpg()))
        elif op == 0xB5: self.a = self.nz(self.read(zpx()))
        elif op == 0xAD: self.a = self.nz(self.read(abso()))
        elif op == 0xBD: self.a = self.nz(self.read(abx()))
        elif op == 0xB9: self.a = self.nz(self.read(aby()))
        elif op == 0xA1: self.a = self.nz(self.read(idx()))
        elif op == 0xB1: self.a = self.nz(self.read(idy()))
        # LDX
        elif op == 0xA2: self.x = self.nz(self.read(imm()))
        elif op == 0xA6: self.x = self.nz(self.read(zpg()))
        elif op == 0xB6: self.x = self.nz(self.read(zpy()))
        elif op == 0xAE: self.x = self.nz(self.read(abso()))
        elif op == 0xBE: self.x = self.nz(self.read(aby()))
        # LDY
        elif op == 0xA0: self.y = self.nz(self.read(imm()))
        elif op == 0xA4: self.y = self.nz(self.read(zpg()))
        elif op == 0xB4: self.y = self.nz(self.read(zpx()))
        elif op == 0xAC: self.y = self.nz(self.read(abso()))
        elif op == 0xBC: self.y = self.nz(self.read(abx()))
        # STA
        elif op == 0x85: self.write(zpg(), self.a)
        elif op == 0x95: self.write(zpx(), self.a)
        elif op == 0x8D: self.write(abso(), self.a)
        elif op == 0x9D: self.write(abx(), self.a)
        elif op == 0x99: self.write(aby(), self.a)
        elif op == 0x81: self.write(idx(), self.a)
        elif op == 0x91: self.write(idy(), self.a)
        # STX
        elif op == 0x86: self.write(zpg(), self.x)
        elif op == 0x96: self.write(zpy(), self.x)
        elif op == 0x8E: self.write(abso(), self.x)
        # STY
        elif op == 0x84: self.write(zpg(), self.y)
        elif op == 0x94: self.write(zpx(), self.y)
        elif op == 0x8C: self.write(abso(), self.y)
        # Transfer
        elif op == 0xAA: self.x = self.nz(self.a)
        elif op == 0xA8: self.y = self.nz(self.a)
        elif op == 0x8A: self.a = self.nz(self.x)
        elif op == 0x98: self.a = self.nz(self.y)
        elif op == 0xBA: self.x = self.nz(self.sp)
        elif op == 0x9A: self.sp = self.x
        # Stack
        elif op == 0x48: self.push(self.a)
        elif op == 0x08: self.push(self.get_status())
        elif op == 0x68: self.a = self.nz(self.pop())
        elif op == 0x28: self.set_status(self.pop())
        # ADC
        elif op == 0x69: self.adc(self.read(imm()))
        elif op == 0x65: self.adc(self.read(zpg()))
        elif op == 0x75: self.adc(self.read(zpx()))
        elif op == 0x6D: self.adc(self.read(abso()))
        elif op == 0x7D: self.adc(self.read(abx()))
        elif op == 0x79: self.adc(self.read(aby()))
        elif op == 0x61: self.adc(self.read(idx()))
        elif op == 0x71: self.adc(self.read(idy()))
        # SBC
        elif op == 0xE9: self.sbc(self.read(imm()))
        elif op == 0xE5: self.sbc(self.read(zpg()))
        elif op == 0xF5: self.sbc(self.read(zpx()))
        elif op == 0xED: self.sbc(self.read(abso()))
        elif op == 0xFD: self.sbc(self.read(abx()))
        elif op == 0xF9: self.sbc(self.read(aby()))
        elif op == 0xE1: self.sbc(self.read(idx()))
        elif op == 0xF1: self.sbc(self.read(idy()))
        # AND
        elif op == 0x29: self.a = self.nz(self.a & self.read(imm()))
        elif op == 0x25: self.a = self.nz(self.a & self.read(zpg()))
        elif op == 0x35: self.a = self.nz(self.a & self.read(zpx()))
        elif op == 0x2D: self.a = self.nz(self.a & self.read(abso()))
        elif op == 0x3D: self.a = self.nz(self.a & self.read(abx()))
        elif op == 0x39: self.a = self.nz(self.a & self.read(aby()))
        elif op == 0x21: self.a = self.nz(self.a & self.read(idx()))
        elif op == 0x31: self.a = self.nz(self.a & self.read(idy()))
        # ORA
        elif op == 0x09: self.a = self.nz(self.a | self.read(imm()))
        elif op == 0x05: self.a = self.nz(self.a | self.read(zpg()))
        elif op == 0x15: self.a = self.nz(self.a | self.read(zpx()))
        elif op == 0x0D: self.a = self.nz(self.a | self.read(abso()))
        elif op == 0x1D: self.a = self.nz(self.a | self.read(abx()))
        elif op == 0x19: self.a = self.nz(self.a | self.read(aby()))
        elif op == 0x01: self.a = self.nz(self.a | self.read(idx()))
        elif op == 0x11: self.a = self.nz(self.a | self.read(idy()))
        # EOR
        elif op == 0x49: self.a = self.nz(self.a ^ self.read(imm()))
        elif op == 0x45: self.a = self.nz(self.a ^ self.read(zpg()))
        elif op == 0x55: self.a = self.nz(self.a ^ self.read(zpx()))
        elif op == 0x4D: self.a = self.nz(self.a ^ self.read(abso()))
        elif op == 0x5D: self.a = self.nz(self.a ^ self.read(abx()))
        elif op == 0x59: self.a = self.nz(self.a ^ self.read(aby()))
        elif op == 0x41: self.a = self.nz(self.a ^ self.read(idx()))
        elif op == 0x51: self.a = self.nz(self.a ^ self.read(idy()))
        # CMP
        elif op == 0xC9: self.cmp(self.a, self.read(imm()))
        elif op == 0xC5: self.cmp(self.a, self.read(zpg()))
        elif op == 0xD5: self.cmp(self.a, self.read(zpx()))
        elif op == 0xCD: self.cmp(self.a, self.read(abso()))
        elif op == 0xDD: self.cmp(self.a, self.read(abx()))
        elif op == 0xD9: self.cmp(self.a, self.read(aby()))
        elif op == 0xC1: self.cmp(self.a, self.read(idx()))
        elif op == 0xD1: self.cmp(self.a, self.read(idy()))
        # CPX
        elif op == 0xE0: self.cmp(self.x, self.read(imm()))
        elif op == 0xE4: self.cmp(self.x, self.read(zpg()))
        elif op == 0xEC: self.cmp(self.x, self.read(abso()))
        # CPY
        elif op == 0xC0: self.cmp(self.y, self.read(imm()))
        elif op == 0xC4: self.cmp(self.y, self.read(zpg()))
        elif op == 0xCC: self.cmp(self.y, self.read(abso()))
        # INC mem
        elif op == 0xE6: a=zpg(); self.write(a, self.nz((self.read(a)+1)&0xFF))
        elif op == 0xF6: a=zpx(); self.write(a, self.nz((self.read(a)+1)&0xFF))
        elif op == 0xEE: a=abso(); self.write(a, self.nz((self.read(a)+1)&0xFF))
        elif op == 0xFE: a=abx(); self.write(a, self.nz((self.read(a)+1)&0xFF))
        # DEC mem
        elif op == 0xC6: a=zpg(); self.write(a, self.nz((self.read(a)-1)&0xFF))
        elif op == 0xD6: a=zpx(); self.write(a, self.nz((self.read(a)-1)&0xFF))
        elif op == 0xCE: a=abso(); self.write(a, self.nz((self.read(a)-1)&0xFF))
        elif op == 0xDE: a=abx(); self.write(a, self.nz((self.read(a)-1)&0xFF))
        # INX/INY/DEX/DEY
        elif op == 0xE8: self.x = self.nz((self.x+1)&0xFF)
        elif op == 0xC8: self.y = self.nz((self.y+1)&0xFF)
        elif op == 0xCA: self.x = self.nz((self.x-1)&0xFF)
        elif op == 0x88: self.y = self.nz((self.y-1)&0xFF)
        # ASL
        elif op == 0x0A: c=self.a>>7; self.a=self.nz((self.a<<1)&0xFF); self.flag_c=bool(c)
        elif op == 0x06: a=zpg(); v=self.read(a); c=v>>7; self.write(a,self.nz((v<<1)&0xFF)); self.flag_c=bool(c)
        elif op == 0x16: a=zpx(); v=self.read(a); c=v>>7; self.write(a,self.nz((v<<1)&0xFF)); self.flag_c=bool(c)
        elif op == 0x0E: a=abso(); v=self.read(a); c=v>>7; self.write(a,self.nz((v<<1)&0xFF)); self.flag_c=bool(c)
        elif op == 0x1E: a=abx(); v=self.read(a); c=v>>7; self.write(a,self.nz((v<<1)&0xFF)); self.flag_c=bool(c)
        # LSR
        elif op == 0x4A: c=self.a&1; self.a=self.nz(self.a>>1); self.flag_c=bool(c)
        elif op == 0x46: a=zpg(); v=self.read(a); c=v&1; self.write(a,self.nz(v>>1)); self.flag_c=bool(c)
        elif op == 0x56: a=zpx(); v=self.read(a); c=v&1; self.write(a,self.nz(v>>1)); self.flag_c=bool(c)
        elif op == 0x4E: a=abso(); v=self.read(a); c=v&1; self.write(a,self.nz(v>>1)); self.flag_c=bool(c)
        elif op == 0x5E: a=abx(); v=self.read(a); c=v&1; self.write(a,self.nz(v>>1)); self.flag_c=bool(c)
        # ROL
        elif op == 0x2A: c=int(self.flag_c); nc=self.a>>7; self.a=self.nz(((self.a<<1)|c)&0xFF); self.flag_c=bool(nc)
        elif op == 0x26: a=zpg(); v=self.read(a); c=int(self.flag_c); nc=v>>7; self.write(a,self.nz(((v<<1)|c)&0xFF)); self.flag_c=bool(nc)
        elif op == 0x36: a=zpx(); v=self.read(a); c=int(self.flag_c); nc=v>>7; self.write(a,self.nz(((v<<1)|c)&0xFF)); self.flag_c=bool(nc)
        elif op == 0x2E: a=abso(); v=self.read(a); c=int(self.flag_c); nc=v>>7; self.write(a,self.nz(((v<<1)|c)&0xFF)); self.flag_c=bool(nc)
        elif op == 0x3E: a=abx(); v=self.read(a); c=int(self.flag_c); nc=v>>7; self.write(a,self.nz(((v<<1)|c)&0xFF)); self.flag_c=bool(nc)
        # ROR
        elif op == 0x6A: c=int(self.flag_c); nc=self.a&1; self.a=self.nz((self.a>>1)|(c<<7)); self.flag_c=bool(nc)
        elif op == 0x66: a=zpg(); v=self.read(a); c=int(self.flag_c); nc=v&1; self.write(a,self.nz((v>>1)|(c<<7))); self.flag_c=bool(nc)
        elif op == 0x76: a=zpx(); v=self.read(a); c=int(self.flag_c); nc=v&1; self.write(a,self.nz((v>>1)|(c<<7))); self.flag_c=bool(nc)
        elif op == 0x6E: a=abso(); v=self.read(a); c=int(self.flag_c); nc=v&1; self.write(a,self.nz((v>>1)|(c<<7))); self.flag_c=bool(nc)
        elif op == 0x7E: a=abx(); v=self.read(a); c=int(self.flag_c); nc=v&1; self.write(a,self.nz((v>>1)|(c<<7))); self.flag_c=bool(nc)
        # BIT
        elif op == 0x24: v=self.read(zpg()); self.flag_n=bool(v&0x80); self.flag_v=bool(v&0x40); self.flag_z=(self.a&v)==0
        elif op == 0x2C: v=self.read(abso()); self.flag_n=bool(v&0x80); self.flag_v=bool(v&0x40); self.flag_z=(self.a&v)==0
        # Branches
        elif op == 0x10: branch(not self.flag_n)
        elif op == 0x30: branch(self.flag_n)
        elif op == 0x50: branch(not self.flag_v)
        elif op == 0x70: branch(self.flag_v)
        elif op == 0x90: branch(not self.flag_c)
        elif op == 0xB0: branch(self.flag_c)
        elif op == 0xD0: branch(not self.flag_z)
        elif op == 0xF0: branch(self.flag_z)
        # JMP
        elif op == 0x4C: self.pc = abso()
        elif op == 0x6C:
            a = abso()
            self.pc = self.ram[a] | (self.ram[(a&0xFF00)|((a+1)&0xFF)] << 8)
        # JSR/RTS/RTI
        elif op == 0x20: a=abso(); self.push16((self.pc-1)&0xFFFF); self.pc=a
        elif op == 0x60: self.pc = (self.pop16()+1)&0xFFFF
        elif op == 0x40: self.set_status(self.pop()); self.pc = self.pop16()
        # Flags
        elif op == 0x18: self.flag_c = False
        elif op == 0x38: self.flag_c = True
        elif op == 0x58: self.flag_i = False
        elif op == 0x78: self.flag_i = True
        elif op == 0xB8: self.flag_v = False
        elif op == 0xD8: self.flag_d = False
        elif op == 0xF8: self.flag_d = True
        # NOP
        elif op == 0xEA: pass
        # BRK
        elif op == 0x00:
            self.pc = (self.pc + 1) & 0xFFFF  # skip padding byte
        # Unofficial NOPs
        elif op in (0x1A,0x3A,0x5A,0x7A,0xDA,0xFA): pass
        elif op in (0x04,0x44,0x64): self.pc = (self.pc+1)&0xFFFF
        elif op in (0x0C,0x14,0x34,0x54,0x74,0xD4,0xF4): self.pc = (self.pc+1)&0xFFFF
        elif op in (0x1C,0x3C,0x5C,0x7C,0xDC,0xFC): self.pc = (self.pc+2)&0xFFFF
        elif op == 0x80: self.pc = (self.pc+1)&0xFFFF
        elif op == 0x89: self.pc = (self.pc+1)&0xFFFF
        # LAX (unofficial but common)
        elif op == 0xA7: v=self.read(zpg()); self.a=self.nz(v); self.x=v
        elif op == 0xB7: v=self.read(zpy()); self.a=self.nz(v); self.x=v
        elif op == 0xAF: v=self.read(abso()); self.a=self.nz(v); self.x=v
        elif op == 0xBF: v=self.read(aby()); self.a=self.nz(v); self.x=v
        elif op == 0xA3: v=self.read(idx()); self.a=self.nz(v); self.x=v
        elif op == 0xB3: v=self.read(idy()); self.a=self.nz(v); self.x=v
        # SAX (unofficial)
        elif op == 0x87: self.write(zpg(), self.a & self.x)
        elif op == 0x97: self.write(zpy(), self.a & self.x)
        elif op == 0x8F: self.write(abso(), self.a & self.x)
        elif op == 0x83: self.write(idx(), self.a & self.x)
        # DCP (unofficial)
        elif op == 0xC7: a=zpg(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xD7: a=zpx(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xCF: a=abso(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xDF: a=abx(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xDB: a=aby(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xC3: a=idx(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        elif op == 0xD3: a=idy(); v=(self.read(a)-1)&0xFF; self.write(a,v); self.cmp(self.a,v)
        # ISB/ISC (unofficial)
        elif op == 0xE7: a=zpg(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xF7: a=zpx(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xEF: a=abso(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xFF: a=abx(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xFB: a=aby(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xE3: a=idx(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        elif op == 0xF3: a=idy(); v=(self.read(a)+1)&0xFF; self.write(a,v); self.sbc(v)
        # SLO (unofficial)
        elif op == 0x07: a=zpg(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x17: a=zpx(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x0F: a=abso(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x1F: a=abx(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x1B: a=aby(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x03: a=idx(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        elif op == 0x13: a=idy(); v=self.read(a); self.flag_c=bool(v&0x80); v=(v<<1)&0xFF; self.write(a,v); self.a=self.nz(self.a|v)
        # RLA (unofficial)
        elif op == 0x27: a=zpg(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x37: a=zpx(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x2F: a=abso(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x3F: a=abx(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x3B: a=aby(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x23: a=idx(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        elif op == 0x33: a=idy(); v=self.read(a); c=int(self.flag_c); self.flag_c=bool(v&0x80); v=((v<<1)|c)&0xFF; self.write(a,v); self.a=self.nz(self.a&v)
        # RRA (unofficial)
        elif op == 0x67: a=zpg(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x77: a=zpx(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x6F: a=abso(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x7F: a=abx(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x7B: a=aby(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x63: a=idx(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        elif op == 0x73: a=idy(); v=self.read(a); c=int(self.flag_c); nc=v&1; v=(v>>1)|(c<<7); self.write(a,v); self.flag_c=bool(nc); self.adc(v)
        # SRE (unofficial)
        elif op == 0x47: a=zpg(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x57: a=zpx(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x4F: a=abso(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x5F: a=abx(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x5B: a=aby(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x43: a=idx(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        elif op == 0x53: a=idy(); v=self.read(a); self.flag_c=bool(v&1); v=v>>1; self.write(a,v); self.a=self.nz(self.a^v)
        else:
            pass  # unknown


# ═══════════════════════════════════════════════════════════════
# NSF Runner — исправленный
# ═══════════════════════════════════════════════════════════════

class NSFRunner:
    def __init__(self, nsf: NSFParser):
        self.nsf = nsf
        self.cpu = CPU6502()
        self._load_prg()

    def _load_prg(self):
        h = self.nsf.header
        self.cpu.prg_rom = self.nsf.prg_data

        # Инициализируем RAM: $0000-$07FF = 0, $4000-$4013 = 0
        for i in range(0x800):
            self.cpu.ram[i] = 0
        for i in range(0x4000, 0x4014):
            self.cpu.ram[i] = 0
        self.cpu.ram[0x4015] = 0x00
        self.cpu.ram[0x4017] = 0x40  # frame counter

        if self.nsf.has_bankswitch:
            # Инициализируем банки из заголовка
            for slot in range(8):
                bank_num = h.bankswitch[slot]
                self.cpu.banks[slot] = bank_num
                self.cpu._apply_bank(slot, bank_num)
        else:
            # Без bankswitch: загружаем PRG в load_addr
            load = h.load_addr
            for i, b in enumerate(self.nsf.prg_data):
                addr = load + i
                if addr < 0x10000:
                    self.cpu.ram[addr] = b

        # Ставим STOP_ADDR = JMP $3FF6 (бесконечный цикл)
        self.cpu.ram[0x3FF6] = 0x4C  # JMP
        self.cpu.ram[0x3FF7] = 0xF6
        self.cpu.ram[0x3FF8] = 0x3F

    def init_song(self, song_num):
        h = self.nsf.header
        self.cpu.a = song_num
        self.cpu.x = 1 if h.is_pal else 0
        self.cpu.sp = 0xFD
        self.cpu.flag_i = True

        # Сбрасываем APU
        for i in range(0x4000, 0x4014):
            self.cpu.ram[i] = 0
        self.cpu.ram[0x4015] = 0x0F  # enable all channels

        # Переинициализируем банки
        if self.nsf.has_bankswitch:
            for slot in range(8):
                bank_num = h.bankswitch[slot]
                self.cpu._apply_bank(slot, bank_num)

        ok = self.cpu.call_subroutine(h.init_addr, max_cycles=1000000)
        return ok

    def run_frames(self, num_frames):
        h = self.nsf.header
        self.cpu.apu_writes = []

        for frame in range(num_frames):
            self.cpu.current_frame = frame
            self.cpu.call_subroutine(h.play_addr, max_cycles=50000)

        return self.cpu.apu_writes


# Остальные классы (APUAnalyzer, NESSampleGenerator, MIDIWriter, XMWriter)
# остаются без изменений из предыдущей версии.
# Копируем их сюда:

class APUAnalyzer:
    def __init__(self, apu_writes, cpu_clock=NTSC_CPU_CLOCK):
        self.apu_writes = apu_writes; self.cpu_clock = cpu_clock
    def analyze(self):
        apu = APUState(self.cpu_clock); events = []
        apu.regs[0x15] = 0x0F  # ← ЭТА СТРОКА РЕШАЕТ ПРОБЛЕМУ
        prev_note = [-1,-1,-1,-1]; prev_vol = [0,0,0,0]
        last_frame = 0
        for frame, addr, val in self.apu_writes:
            apu.write_reg(addr, val); last_frame = frame
            if 0x4000<=addr<=0x4003: self._ck_pulse(apu,0,frame,events,prev_note,prev_vol)
            elif 0x4004<=addr<=0x4007: self._ck_pulse(apu,1,frame,events,prev_note,prev_vol)
            elif 0x4008<=addr<=0x400B: self._ck_tri(apu,frame,events,prev_note,prev_vol)
            elif 0x400C<=addr<=0x400F: self._ck_noise(apu,frame,events,prev_note,prev_vol)
            elif addr==0x4015:
                for c in range(2): self._ck_pulse(apu,c,frame,events,prev_note,prev_vol)
                self._ck_tri(apu,frame,events,prev_note,prev_vol)
                self._ck_noise(apu,frame,events,prev_note,prev_vol)
        for ch in range(4):
            if prev_note[ch]>=0:
                events.append(NoteEvent(frame=last_frame,channel=ch,event_type='note_off',note=97))
        return events
    def _ck_pulse(self,apu,ch,frame,events,pn,pv):
        if not apu.is_pulse_enabled(ch):
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
            return
        freq=apu.get_pulse_freq(ch); vol=apu.get_pulse_volume(ch)
        note=freq_to_xm_note(freq) if freq>0 else None; duty=apu.get_pulse_duty(ch); inst=1+duty
        if vol==0 or note is None:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
        elif note!=pn[ch]:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97))
            events.append(NoteEvent(frame=frame,channel=ch,event_type='note_on',note=note,instrument=inst,volume=vol)); pn[ch]=note
    def _ck_tri(self,apu,frame,events,pn,pv):
        ch=2
        if not apu.is_triangle_enabled():
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
            return
        freq=apu.get_triangle_freq(); vol=apu.get_triangle_volume()
        note=freq_to_xm_note(freq) if freq>0 else None
        if vol==0 or note is None:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
        elif note!=pn[ch]:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97))
            events.append(NoteEvent(frame=frame,channel=ch,event_type='note_on',note=note,instrument=5,volume=vol)); pn[ch]=note
    def _ck_noise(self,apu,frame,events,pn,pv):
        ch=3
        if not apu.is_noise_enabled():
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
            return
        vol=apu.get_noise_volume(); note=apu.get_noise_note()
        if vol==0:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97)); pn[ch]=-1
        elif note!=pn[ch] or vol!=pv[ch]:
            if pn[ch]>=0: events.append(NoteEvent(frame=frame,channel=ch,event_type='note_off',note=97))
            xn=max(1,min(96,note))
            events.append(NoteEvent(frame=frame,channel=ch,event_type='note_on',note=xn,instrument=6,volume=vol)); pn[ch]=note; pv[ch]=vol


class NESSampleGenerator:
    P=SAMPLE_PERIOD
    @classmethod
    def pulse_12(cls):
        p=cls.P; s=[]
        for i in range(p):
            val=0.0; ph=i/p
            for h in range(1,20): c=(2.0/(h*math.pi))*math.sin(math.pi*h*0.125); val+=c*math.sin(2*math.pi*h*ph)
            s.append(int(max(-1,min(1,val*1.5))*24000))
        return s
    @classmethod
    def pulse_25(cls):
        p=cls.P; s=[]
        for i in range(p):
            val=0.0; ph=i/p
            for h in range(1,20): c=(2.0/(h*math.pi))*math.sin(math.pi*h*0.25); val+=c*math.sin(2*math.pi*h*ph)
            s.append(int(max(-1,min(1,val*1.3))*24000))
        return s
    @classmethod
    def pulse_50(cls):
        p=cls.P; s=[]
        for i in range(p):
            val=0.0; ph=i/p
            for h in range(1,20,2): val+=math.sin(2*math.pi*h*ph)/h
            s.append(int(max(-1,min(1,val*1.2))*24000))
        return s
    @classmethod
    def pulse_75(cls): return [-v for v in cls.pulse_25()]
    @classmethod
    def triangle(cls):
        p=cls.P; steps=list(range(0,16))+list(range(15,-1,-1)); s=[]
        for i in range(p):
            idx=int(i/p*32)%32; val=(steps[idx]-7.5)/7.5; s.append(int(val*24000))
        return s
    @staticmethod
    def noise(length=4096):
        s=[]; lfsr=1
        for _ in range(length):
            bit=((lfsr>>0)^(lfsr>>1))&1; lfsr=(lfsr>>1)|(bit<<14)
            s.append(24000 if lfsr&1 else -24000)
        return s


class MIDIWriter:
    GM={0:80,1:80,2:80,3:80,4:74,5:115}
    def __init__(self): self.tracks=[]; self.tpqn=480; self.bpm=120
    def build(self,events,bpm=120,transpose=0,octave=0,title="",frame_rate=60.0):
        ts=transpose+octave*12; self.bpm=bpm; bps=bpm/60.0; spt=1.0/(bps*self.tpqn)
        f2t=(1.0/frame_rate)/spt; chs={}
        for ev in events: chs.setdefault(ev.channel,[]).append(ev)
        self.tracks=[]; t0=[]; usec=int(60_000_000/bpm)
        t0.append((0,bytes([0xFF,0x51,0x03,(usec>>16)&0xFF,(usec>>8)&0xFF,usec&0xFF])))
        if title: td=title.encode('ascii',errors='replace')[:127]; t0.append((0,bytes([0xFF,0x01,len(td)])+td))
        self.tracks.append(t0)
        cn=['Pulse 1','Pulse 2','Triangle','Noise']
        for ci,ch in enumerate(sorted(chs.keys())):
            if ci>=15: break
            mc=ci if ci<9 else ci+1; trk=[]
            trk.append((0,bytes([0xC0|mc,self.GM.get(ch,0)])))
            n=(cn[ch] if ch<len(cn) else f"Ch {ch}").encode()[:127]
            trk.append((0,bytes([0xFF,0x03,len(n)])+n))
            act={}
            for ev in chs[ch]:
                mt=int(ev.frame*f2t)
                if ev.event_type=='note_on':
                    mn=max(0,min(127,xm_note_to_midi(ev.note)+ts)); vel=max(1,min(127,ev.volume*2))
                    if ch in act: pn,_=act[ch]; trk.append((mt,bytes([0x80|mc,pn,0])))
                    trk.append((mt,bytes([0x90|mc,mn,vel]))); act[ch]=(mn,mt)
                elif ev.event_type=='note_off':
                    if ch in act: pn,_=act[ch]; trk.append((mt,bytes([0x80|mc,pn,0]))); del act[ch]
            for pn,pt in act.values(): trk.append((pt+self.tpqn,bytes([0x80|mc,pn,0])))
            self.tracks.append(trk)
    def write(self,fn):
        with open(fn,'wb') as f:
            f.write(b'MThd'); f.write(struct.pack('>I',6)); f.write(struct.pack('>HHH',1,len(self.tracks),self.tpqn))
            for trk in self.tracks:
                td=self._enc(trk); f.write(b'MTrk'); f.write(struct.pack('>I',len(td))); f.write(td)
    def _enc(self,evts):
        evts.sort(key=lambda x:x[0]); d=bytearray(); prev=0
        for at,ed in evts: d.extend(self._vlq(max(0,at-prev))); d.extend(ed); prev=at
        d.extend(self._vlq(0)); d.extend(b'\xFF\x2F\x00'); return bytes(d)
    @staticmethod
    def _vlq(v):
        if v<0: v=0
        r=[v&0x7F]; v>>=7
        while v: r.append((v&0x7F)|0x80); v>>=7
        r.reverse(); return bytes(r)


class XMWriter:
    def __init__(self):
        self.title=""; self.num_channels=4; self.bpm=150; self.speed=1
        self.patterns=[]; self.instruments=[]; self.order=[]
    def build(self,events,title="",bpm=150,speed=1,transpose=0,octave=0,finetune=0,compact=1):
        self.title=title[:20]; self.bpm=bpm; self.speed=speed; self.num_channels=4
        tt=transpose+octave*12; self._build_inst(tt,finetune)
        if compact>1:
            events=[NoteEvent(frame=e.frame//compact,channel=e.channel,event_type=e.event_type,
                note=e.note,instrument=e.instrument,volume=e.volume) for e in events]
            self.bpm=max(32,bpm//compact)
        self._build_pat(events)
    def _build_inst(self,transpose=0,finetune=0):
        gen=NESSampleGenerator; rel=max(-128,min(127,transpose)); ft=max(-128,min(127,finetune))
        def mk(name,pcm,loop=True):
            return {'name':name[:22],'samples':[{'name':name[:22],'data':pcm,'length':len(pcm),
                'loop_start':0,'loop_length':len(pcm) if loop else 0,'loop_type':1 if loop else 0,
                'volume':64,'finetune':ft,'panning':128,'relative_note':rel,'bits':16}]}
        self.instruments=[mk("Pulse 12.5%",gen.pulse_12()),mk("Pulse 25%",gen.pulse_25()),
            mk("Pulse 50%",gen.pulse_50()),mk("Pulse 75%",gen.pulse_75()),
            mk("Triangle",gen.triangle()),mk("Noise",gen.noise(),loop=False)]
    def _build_pat(self,events):
        rpp=64
        if not events: self.patterns=[[[TrackerNote() for _ in range(4)] for _ in range(rpp)]]; self.order=[0]; return
        mx=max(e.frame for e in events); np_=max(1,min((mx+rpp)//rpp,256))
        self.patterns=[[[TrackerNote() for _ in range(4)] for _ in range(rpp)] for _ in range(np_)]
        seen={}
        for ev in events:
            ch=ev.channel
            if ch>=4: continue
            pi=ev.frame//rpp; ri=ev.frame%rpp
            if pi>=len(self.patterns): continue
            nd=self.patterns[pi][ri][ch]; key=(ev.frame,ch)
            if ev.event_type=='note_on':
                nd.note=max(1,min(96,ev.note)); nd.instrument=min(ev.instrument,len(self.instruments))
                nd.volume=0x10+min(0x40,max(0,ev.volume)); seen[key]='on'
            elif ev.event_type=='note_off':
                if key not in seen or seen[key]!='on':
                    if nd.note==0: nd.note=97
        self.order=list(range(np_))
    def write(self,fn):
        with open(fn,'wb') as f:
            self._wh(f)
            for p in self.patterns: self._wp(f,p)
            for i in self.instruments[:128]: self._wi(f,i)
    def _wh(self,f):
        f.write(b'Extended Module: '); f.write(self.title.encode('ascii','replace')[:20].ljust(20,b'\x00'))
        f.write(b'\x1a'); f.write(b'NSF2XM Converter    ')
        f.write(struct.pack('<H',0x0104)); f.write(struct.pack('<I',276))
        f.write(struct.pack('<H',len(self.order))); f.write(struct.pack('<H',0))
        f.write(struct.pack('<H',self.num_channels)); f.write(struct.pack('<H',len(self.patterns)))
        f.write(struct.pack('<H',min(len(self.instruments),128)))
        f.write(struct.pack('<H',1)); f.write(struct.pack('<H',self.speed)); f.write(struct.pack('<H',self.bpm))
        ot=bytearray(256)
        for i,o in enumerate(self.order[:256]): ot[i]=o
        f.write(ot)
    def _wp(self,f,pat):
        pk=bytearray()
        for row in pat:
            for ch in range(self.num_channels):
                nd=row[ch] if ch<len(row) else TrackerNote()
                hn=nd.note>0;hi=nd.instrument>0;hv=nd.volume>0;hf=nd.effect>0;hp=nd.effect_param>0
                if not(hn or hi or hv or hf or hp): pk.append(0x80)
                else:
                    pb=0x80
                    if hn:pb|=1
                    if hi:pb|=2
                    if hv:pb|=4
                    if hf:pb|=8
                    if hp:pb|=16
                    pk.append(pb)
                    if hn:pk.append(nd.note&0xFF)
                    if hi:pk.append(nd.instrument&0xFF)
                    if hv:pk.append(nd.volume&0xFF)
                    if hf:pk.append(nd.effect&0xFF)
                    if hp:pk.append(nd.effect_param&0xFF)
        f.write(struct.pack('<I',9)); f.write(struct.pack('<B',0))
        f.write(struct.pack('<H',len(pat))); f.write(struct.pack('<H',len(pk))); f.write(pk)
    def _wi(self,f,inst):
        samples=inst.get('samples',[])
        if not samples:
            f.write(struct.pack('<I',29)); f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
            f.write(struct.pack('<BH',0,0)); return
        ihs=263; f.write(struct.pack('<I',ihs))
        f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
        f.write(struct.pack('<B',0)); f.write(struct.pack('<H',len(samples))); f.write(struct.pack('<I',40))
        f.write(bytearray(96))
        ve=bytearray(48); struct.pack_into('<HH',ve,0,0,64); struct.pack_into('<HH',ve,4,100,64); f.write(ve)
        pe=bytearray(48); struct.pack_into('<HH',pe,0,0,32); struct.pack_into('<HH',pe,4,100,32); f.write(pe)
        f.write(bytes([2,2,0,0,1,0,0,1,1,0,0,0,0,0])); f.write(struct.pack('<H',0x800))
        rem=ihs-(4+22+1+2+4+96+48+48+14+2)
        if rem>0: f.write(b'\x00'*rem)
        for s in samples: self._wsh(f,s)
        for s in samples: self._wsd(f,s)
    def _wsh(self,f,s):
        bits=s.get('bits',16); bps=2 if bits==16 else 1
        f.write(struct.pack('<I',len(s['data'])*bps)); f.write(struct.pack('<I',s['loop_start']*bps))
        f.write(struct.pack('<I',s['loop_length']*bps)); f.write(struct.pack('<B',s.get('volume',64)))
        f.write(struct.pack('<b',max(-128,min(127,s.get('finetune',0)))))
        tb=s.get('loop_type',0)&3
        if bits==16: tb|=0x10
        f.write(struct.pack('<B',tb)); f.write(struct.pack('<B',s.get('panning',128)))
        f.write(struct.pack('<b',max(-128,min(127,s.get('relative_note',0)))))
        f.write(struct.pack('<B',0)); f.write(s.get('name','').encode('ascii','replace')[:22].ljust(22,b'\x00'))
    def _wsd(self,f,s):
        prev=0
        for v in s['data']:
            v=max(-32768,min(32767,v)); d=((v-prev)+32768)%65536-32768
            f.write(struct.pack('<h',d)); prev=v


def convert_nsf_track(nsf, song_num, output_base, duration=120.0,
                      transpose=0, octave=0, finetune=0,
                      bpm_override=None, speed_override=None,
                      compact=1, output_format='xm', midi_file=None):
    h = nsf.header
    frame_rate = PAL_FRAME_RATE if h.is_pal else NTSC_FRAME_RATE
    cpu_clock = PAL_CPU_CLOCK if h.is_pal else NTSC_CPU_CLOCK
    num_frames = int(duration * frame_rate)

    print(f"\n  Трек {song_num + 1}/{h.total_songs}")

    runner = NSFRunner(nsf)
    ok = runner.init_song(song_num)
    print(f"  INIT: {'OK' if ok else 'timeout'}")

    apu_writes = runner.run_frames(num_frames)
    print(f"  PLAY: {num_frames} фреймов, APU записей: {len(apu_writes)}")

    if not apu_writes:
        print(f"  Пропуск — нет APU данных")
        return

    analyzer = APUAnalyzer(apu_writes, cpu_clock)
    events = analyzer.analyze()
    note_ons = [e for e in events if e.event_type == 'note_on']
    print(f"  Нот: {len(note_ons)}")

    if not note_ons:
        print(f"  Пропуск — нет нот")
        return

    bpm = bpm_override or max(32, min(255, int(frame_rate * 2.5)))
    speed = speed_override or 1

    ch_names = ['Pulse1', 'Pulse2', 'Tri', 'Noise']
    print(f"  Первые 10:")
    for e in note_ons[:10]:
        cn = ch_names[e.channel] if e.channel < len(ch_names) else f"ch{e.channel}"
        print(f"    frame={e.frame:>5d} {cn:>6s} {note_name(e.note):>4s} vol={e.volume}")

    if output_format in ('xm', 'both'):
        xf = output_base + '.xm'
        w = XMWriter()
        w.build(events, title=h.title or f"Track {song_num+1}", bpm=bpm, speed=speed,
                transpose=transpose, octave=octave, finetune=finetune, compact=compact)
        w.write(xf)
        print(f"  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {len(w.patterns)} pat)")

    if output_format in ('midi', 'both') or midi_file:
        mf = midi_file or (output_base + '.mid')
        mw = MIDIWriter()
        mw.build(events, bpm=bpm, transpose=transpose, octave=octave,
                 title=h.title or f"Track {song_num+1}", frame_rate=frame_rate)
        mw.write(mf)
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")


def convert_nsf(input_file, output_file=None, duration=120.0,
                transpose=0, octave=0, finetune=0,
                bpm_override=None, speed_override=None,
                compact=1, output_format='xm', midi_file=None,
                track=None, all_tracks=False):
    print(f"Конвертация: {input_file}")
    nsf = NSFParser(input_file); h = nsf.header
    print(f"  Название: {h.title}")
    print(f"  Артист: {h.artist}")
    print(f"  Треков: {h.total_songs}, Регион: {'PAL' if h.is_pal else 'NTSC'}")
    print(f"  Load: ${h.load_addr:04X}, Init: ${h.init_addr:04X}, Play: ${h.play_addr:04X}")
    print(f"  Bankswitch: {'Да' if nsf.has_bankswitch else 'Нет'} {list(h.bankswitch)}")

    base = os.path.splitext(input_file)[0]
    if all_tracks: tracks = range(h.total_songs)
    elif track is not None:
        t = track - 1
        if t < 0 or t >= h.total_songs: print(f"  Трек {track} не существует"); return
        tracks = [t]
    else: tracks = [h.starting_song - 1]

    for sn in tracks:
        ob = f"{base}_track{sn+1:02d}" if (all_tracks and h.total_songs > 1) else (os.path.splitext(output_file)[0] if output_file else base)
        convert_nsf_track(nsf, sn, ob, duration=duration, transpose=transpose,
                          octave=octave, finetune=finetune, bpm_override=bpm_override,
                          speed_override=speed_override, compact=compact,
                          output_format=output_format,
                          midi_file=midi_file if not all_tracks else None)
    print(f"\nГотово!")


def main():
    import argparse
    ap = argparse.ArgumentParser(description='NSF to XM/MIDI Converter v2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python nsf2xm.py game.nsf
  python nsf2xm.py game.nsf --track 3
  python nsf2xm.py game.nsf --all-tracks
  python nsf2xm.py game.nsf --all-tracks --midi
  python nsf2xm.py game.nsf --midi-only --track 5
  python nsf2xm.py game.nsf --octave 1 --compact 2
        """)
    ap.add_argument('input', nargs='+')
    ap.add_argument('-o', '--output')
    ap.add_argument('--duration', type=float, default=120.0)
    ap.add_argument('--track', type=int, default=None, help='Номер трека (1-N)')
    ap.add_argument('--all-tracks', action='store_true')
    ap.add_argument('--octave', type=int, default=0)
    ap.add_argument('--transpose', type=int, default=0)
    ap.add_argument('--finetune', type=int, default=0)
    ap.add_argument('--bpm', type=int, default=None)
    ap.add_argument('--speed', type=int, default=None)
    ap.add_argument('--compact', type=int, default=1)
    ap.add_argument('--midi', action='store_true')
    ap.add_argument('--midi-only', action='store_true')
    ap.add_argument('--midi-file', type=str, default=None)
    args = ap.parse_args()
    if args.output and len(args.input) > 1: print("--output: one file"); sys.exit(1)
    fmt = 'midi' if args.midi_only else ('both' if args.midi or args.midi_file else 'xm')
    for inp in args.input:
        if not os.path.exists(inp): print(f"Not found: {inp}"); continue
        try:
            convert_nsf(inp, args.output, args.duration, transpose=args.transpose,
                        octave=args.octave, finetune=args.finetune,
                        bpm_override=args.bpm, speed_override=args.speed,
                        compact=args.compact, output_format=fmt,
                        midi_file=args.midi_file, track=args.track, all_tracks=args.all_tracks)
        except Exception as e:
            import traceback; print(f"Error: {e}"); traceback.print_exc()

if __name__ == '__main__': main()