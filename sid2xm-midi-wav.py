"""
SID (Commodore 64 / MOS 6581-8580) to XM/MIDI/WAV Converter v1
6502 CPU emulator drives the tune's init/play routines, register writes
to $D400-$D418 are captured per frame and fed to a simplified SID
chip model (3 oscillators + ADSR envelopes + one state-variable filter).
Only the primary SID chip is emulated (multi-SID / stereo SID tunes
will only convert the first chip's voices).
"""

import struct, sys, os, math
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

NOTE_NAMES = ['C-','C#','D-','D#','E-','F-',
              'F#','G-','G#','A-','A#','B-']

def note_name_xm(n):
    if 1<=n<=96:v=n-1;return f"{NOTE_NAMES[v%12]}{v//12}"
    return "---"
def xm_note_to_midi(n): return n+11
def freq_to_xm_note(freq):
    if freq<15 or freq>20000:return None
    midi=69+12*math.log2(freq/440.0);xm=int(round(midi))-11
    return xm if 1<=xm<=96 else None

# ═══════════════════════════════════════════════════════════════
# PSID/RSID header parsing
# ═══════════════════════════════════════════════════════════════

PAL_CLOCK=985248;NTSC_CLOCK=1022727

@dataclass
class PSIDHeader:
    magic:str='PSID';version:int=2;data_offset:int=0x7C
    load_addr:int=0;init_addr:int=0;play_addr:int=0
    num_songs:int=1;start_song:int=1;speed:int=0
    name:str='';author:str='';released:str=''
    flags:int=0;is_pal:bool=True;sid_model:str='6581'
    second_sid:int=0;third_sid:int=0

def parse_sid(data):
    if data[:4] not in (b'PSID',b'RSID'):
        raise ValueError("Не PSID/RSID файл")
    h=PSIDHeader();h.magic=data[:4].decode('ascii')
    h.version=struct.unpack_from('>H',data,4)[0]
    h.data_offset=struct.unpack_from('>H',data,6)[0]
    h.load_addr=struct.unpack_from('>H',data,8)[0]
    h.init_addr=struct.unpack_from('>H',data,10)[0]
    h.play_addr=struct.unpack_from('>H',data,12)[0]
    h.num_songs=struct.unpack_from('>H',data,14)[0] or 1
    h.start_song=struct.unpack_from('>H',data,16)[0] or 1
    h.speed=struct.unpack_from('>I',data,18)[0]
    h.name=data[22:54].split(b'\x00')[0].decode('latin1','replace').strip()
    h.author=data[54:86].split(b'\x00')[0].decode('latin1','replace').strip()
    h.released=data[86:118].split(b'\x00')[0].decode('latin1','replace').strip()
    if h.version>=2 and len(data)>=0x76+8:
        h.flags=struct.unpack_from('>H',data,0x76)[0]
        clock_bits=(h.flags>>2)&3
        h.is_pal = clock_bits!=2
        model_bits=(h.flags>>4)&3
        h.sid_model='8580' if model_bits==2 else '6581'
        if h.version>=3 and len(data)>0x7A:h.second_sid=data[0x7A]
        if h.version>=4 and len(data)>0x7B:h.third_sid=data[0x7B]
    prg=data[h.data_offset:]
    if h.load_addr==0:
        load_addr=prg[0]|(prg[1]<<8);prg=prg[2:]
    else:
        load_addr=h.load_addr
    return h,prg,load_addr

# ═══════════════════════════════════════════════════════════════
# Waveform modes & channel mapping (analogous to AY tone/noise split)
# ═══════════════════════════════════════════════════════════════
MODE_TRI='tri';MODE_SAW='saw';MODE_PULSE='pulse';MODE_NOISE='noise'
ALL_MODES=[MODE_TRI,MODE_SAW,MODE_PULSE,MODE_NOISE]
MODE_INSTRUMENTS={MODE_TRI:1,MODE_SAW:2,MODE_PULSE:3,MODE_NOISE:4}

# Voice indices are global: chip*3+voice (0-2 = chip 1, 3-5 = chip 2, 6-8 = chip 3)
def build_presets(num_chips=1):
    nv=num_chips*3
    return{
        'default':{(v,m):v for v in range(nv) for m in ALL_MODES},
        'split':{(v,m):v*4+i for v in range(nv) for i,m in enumerate(ALL_MODES)},
        'compact':{**{(v,m):v for v in range(nv) for m in(MODE_TRI,MODE_SAW,MODE_PULSE)},
                   **{(v,MODE_NOISE):nv for v in range(nv)}},
    }
CHANNEL_PRESETS=build_presets(1)  # kept for simple single-chip lookups

def get_channel_name(voice,mode,num_chips=1):
    mn={MODE_TRI:'Tri',MODE_SAW:'Saw',MODE_PULSE:'Pulse',MODE_NOISE:'Noise'}
    if num_chips>1:
        chip,v=divmod(voice,3)
        return f"S{chip+1}V{v+1}-{mn.get(mode,mode)}"
    return f"V{voice+1}-{mn.get(mode,mode)}"

def _parse_voice_token(tok,num_chips):
    tok=tok.strip().lower()
    if tok.isdigit():
        v=int(tok)
        if 0<=v<num_chips*3:return v
        raise ValueError(f"Voice index out of range: '{tok}'")
    import re
    mm=re.match(r's?(\d+)[.:]?([abc123])$',tok)
    if mm:
        chip=int(mm.group(1))-1
        vmap={'a':0,'b':1,'c':2,'1':0,'2':1,'3':2}
        if not(0<=chip<num_chips):raise ValueError(f"Unknown SID chip: '{tok}'")
        return chip*3+vmap[mm.group(2)]
    vmap={'a':0,'b':1,'c':2}
    if tok in vmap:return vmap[tok]
    raise ValueError(f"Unknown voice: '{tok}'")

def parse_channel_map(spec,num_chips=1):
    presets=build_presets(num_chips)
    if spec in presets:return presets[spec]
    ma={'t':MODE_TRI,'tri':MODE_TRI,'triangle':MODE_TRI,
        's':MODE_SAW,'saw':MODE_SAW,'sawtooth':MODE_SAW,
        'p':MODE_PULSE,'pulse':MODE_PULSE,'sq':MODE_PULSE,
        'n':MODE_NOISE,'noise':MODE_NOISE}
    result={}
    for part in spec.split(','):
        part=part.strip()
        if not part:continue
        if'='not in part or':'not in part:raise ValueError(f"Bad: '{part}'")
        left,right=part.split('=',1);v_str,m_str=left.split(':',1)
        vch=_parse_voice_token(v_str,num_chips);mode=ma.get(m_str.strip().lower())
        if mode is None:raise ValueError(f"Unknown mode: '{m_str}'")
        result[(vch,mode)]=int(right.strip())
    for vch in range(num_chips*3):
        for mode in ALL_MODES:
            if(vch,mode)not in result:
                result[(vch,mode)]=result.get((vch,MODE_TRI),vch)
    return result

# ═══════════════════════════════════════════════════════════════
# MOS 6502 CPU emulator (C64 flat memory, SID/CIA/VIC register stubs)
# ═══════════════════════════════════════════════════════════════

class CPU6502:
    STOP_ADDR=0xFFF0

    def __init__(self):
        self.a=0;self.x=0;self.y=0;self.sp=0xFD;self.pc=0
        self.flag_c=False;self.flag_z=True;self.flag_i=True
        self.flag_d=False;self.flag_v=False;self.flag_n=False
        self.ram=bytearray(0x10000)
        self.cycles=0

    def get_status(self):
        return (int(self.flag_c)|(int(self.flag_z)<<1)|
                (int(self.flag_i)<<2)|(int(self.flag_d)<<3)|
                (1<<4)|(1<<5)|
                (int(self.flag_v)<<6)|(int(self.flag_n)<<7))

    def set_status(self,v):
        self.flag_c=bool(v&0x01);self.flag_z=bool(v&0x02)
        self.flag_i=bool(v&0x04);self.flag_d=bool(v&0x08)
        self.flag_v=bool(v&0x40);self.flag_n=bool(v&0x80)

    def read(self,addr):
        addr&=0xFFFF
        if addr==0xD012:return(self.cycles//63)%312&0xFF
        if addr==0xD011:return 0x80 if((self.cycles//63)%312)>255 else 0
        if addr==0xD41B:return(self.cycles*13)&0xFF
        if addr==0xD41C:return 0xFF
        return self.ram[addr]

    def write(self,addr,val):
        self.ram[addr&0xFFFF]=val&0xFF

    def read16(self,addr):
        return self.read(addr)|(self.read((addr+1)&0xFFFF)<<8)

    def push(self,v):
        self.ram[0x100+self.sp]=v&0xFF;self.sp=(self.sp-1)&0xFF
    def pop(self):
        self.sp=(self.sp+1)&0xFF;return self.ram[0x100+self.sp]
    def push16(self,v):
        self.push((v>>8)&0xFF);self.push(v&0xFF)
    def pop16(self):
        lo=self.pop();return lo|(self.pop()<<8)

    def nz(self,v):
        v&=0xFF;self.flag_n=bool(v&0x80);self.flag_z=(v==0);return v

    def adc(self,val):
        c=int(self.flag_c);r=self.a+val+c
        self.flag_c=r>0xFF
        self.flag_v=bool((~(self.a^val)&(self.a^r))&0x80)
        self.a=self.nz(r)
    def sbc(self,val):self.adc(val^0xFF)
    def cmp(self,a,b):
        r=a-b;self.flag_c=r>=0;self.nz(r&0xFF)

    def call_subroutine(self,addr,max_cycles=200000):
        self.push16((self.STOP_ADDR-1)&0xFFFF)
        self.pc=addr
        start=self.cycles
        while self.cycles-start<max_cycles:
            if self.pc==self.STOP_ADDR:return True
            self.step()
        return False

    def step(self):
        op=self.ram[self.pc];self.pc=(self.pc+1)&0xFFFF
        self.cycles+=2

        def imm():
            v=self.pc;self.pc=(self.pc+1)&0xFFFF;return v
        def zpg():
            v=self.ram[self.pc];self.pc=(self.pc+1)&0xFFFF;return v
        def zpx():
            v=(self.ram[self.pc]+self.x)&0xFF;self.pc=(self.pc+1)&0xFFFF;return v
        def zpy():
            v=(self.ram[self.pc]+self.y)&0xFF;self.pc=(self.pc+1)&0xFFFF;return v
        def abso():
            v=self.read16(self.pc);self.pc=(self.pc+2)&0xFFFF;return v
        def abx():
            v=(self.read16(self.pc)+self.x)&0xFFFF;self.pc=(self.pc+2)&0xFFFF;return v
        def aby():
            v=(self.read16(self.pc)+self.y)&0xFFFF;self.pc=(self.pc+2)&0xFFFF;return v
        def idx():
            z=(self.ram[self.pc]+self.x)&0xFF;self.pc=(self.pc+1)&0xFFFF
            return self.ram[z]|(self.ram[(z+1)&0xFF]<<8)
        def idy():
            z=self.ram[self.pc];self.pc=(self.pc+1)&0xFFFF
            base=self.ram[z]|(self.ram[(z+1)&0xFF]<<8)
            return(base+self.y)&0xFFFF
        def branch(cond):
            r=self.ram[self.pc];self.pc=(self.pc+1)&0xFFFF
            if cond:
                r=r-256 if r>=128 else r
                self.pc=(self.pc+r)&0xFFFF;self.cycles+=1

        if op==0xA9:self.a=self.nz(self.read(imm()))
        elif op==0xA5:self.a=self.nz(self.read(zpg()))
        elif op==0xB5:self.a=self.nz(self.read(zpx()))
        elif op==0xAD:self.a=self.nz(self.read(abso()))
        elif op==0xBD:self.a=self.nz(self.read(abx()))
        elif op==0xB9:self.a=self.nz(self.read(aby()))
        elif op==0xA1:self.a=self.nz(self.read(idx()))
        elif op==0xB1:self.a=self.nz(self.read(idy()))
        elif op==0xA2:self.x=self.nz(self.read(imm()))
        elif op==0xA6:self.x=self.nz(self.read(zpg()))
        elif op==0xB6:self.x=self.nz(self.read(zpy()))
        elif op==0xAE:self.x=self.nz(self.read(abso()))
        elif op==0xBE:self.x=self.nz(self.read(aby()))
        elif op==0xA0:self.y=self.nz(self.read(imm()))
        elif op==0xA4:self.y=self.nz(self.read(zpg()))
        elif op==0xB4:self.y=self.nz(self.read(zpx()))
        elif op==0xAC:self.y=self.nz(self.read(abso()))
        elif op==0xBC:self.y=self.nz(self.read(abx()))
        elif op==0x85:self.write(zpg(),self.a)
        elif op==0x95:self.write(zpx(),self.a)
        elif op==0x8D:self.write(abso(),self.a)
        elif op==0x9D:self.write(abx(),self.a)
        elif op==0x99:self.write(aby(),self.a)
        elif op==0x81:self.write(idx(),self.a)
        elif op==0x91:self.write(idy(),self.a)
        elif op==0x86:self.write(zpg(),self.x)
        elif op==0x96:self.write(zpy(),self.x)
        elif op==0x8E:self.write(abso(),self.x)
        elif op==0x84:self.write(zpg(),self.y)
        elif op==0x94:self.write(zpx(),self.y)
        elif op==0x8C:self.write(abso(),self.y)
        elif op==0xAA:self.x=self.nz(self.a)
        elif op==0xA8:self.y=self.nz(self.a)
        elif op==0x8A:self.a=self.nz(self.x)
        elif op==0x98:self.a=self.nz(self.y)
        elif op==0xBA:self.x=self.nz(self.sp)
        elif op==0x9A:self.sp=self.x
        elif op==0x48:self.push(self.a)
        elif op==0x08:self.push(self.get_status())
        elif op==0x68:self.a=self.nz(self.pop())
        elif op==0x28:self.set_status(self.pop())
        elif op==0x69:self.adc(self.read(imm()))
        elif op==0x65:self.adc(self.read(zpg()))
        elif op==0x75:self.adc(self.read(zpx()))
        elif op==0x6D:self.adc(self.read(abso()))
        elif op==0x7D:self.adc(self.read(abx()))
        elif op==0x79:self.adc(self.read(aby()))
        elif op==0x61:self.adc(self.read(idx()))
        elif op==0x71:self.adc(self.read(idy()))
        elif op==0xE9:self.sbc(self.read(imm()))
        elif op==0xE5:self.sbc(self.read(zpg()))
        elif op==0xF5:self.sbc(self.read(zpx()))
        elif op==0xED:self.sbc(self.read(abso()))
        elif op==0xFD:self.sbc(self.read(abx()))
        elif op==0xF9:self.sbc(self.read(aby()))
        elif op==0xE1:self.sbc(self.read(idx()))
        elif op==0xF1:self.sbc(self.read(idy()))
        elif op==0x29:self.a=self.nz(self.a&self.read(imm()))
        elif op==0x25:self.a=self.nz(self.a&self.read(zpg()))
        elif op==0x35:self.a=self.nz(self.a&self.read(zpx()))
        elif op==0x2D:self.a=self.nz(self.a&self.read(abso()))
        elif op==0x3D:self.a=self.nz(self.a&self.read(abx()))
        elif op==0x39:self.a=self.nz(self.a&self.read(aby()))
        elif op==0x21:self.a=self.nz(self.a&self.read(idx()))
        elif op==0x31:self.a=self.nz(self.a&self.read(idy()))
        elif op==0x09:self.a=self.nz(self.a|self.read(imm()))
        elif op==0x05:self.a=self.nz(self.a|self.read(zpg()))
        elif op==0x15:self.a=self.nz(self.a|self.read(zpx()))
        elif op==0x0D:self.a=self.nz(self.a|self.read(abso()))
        elif op==0x1D:self.a=self.nz(self.a|self.read(abx()))
        elif op==0x19:self.a=self.nz(self.a|self.read(aby()))
        elif op==0x01:self.a=self.nz(self.a|self.read(idx()))
        elif op==0x11:self.a=self.nz(self.a|self.read(idy()))
        elif op==0x49:self.a=self.nz(self.a^self.read(imm()))
        elif op==0x45:self.a=self.nz(self.a^self.read(zpg()))
        elif op==0x55:self.a=self.nz(self.a^self.read(zpx()))
        elif op==0x4D:self.a=self.nz(self.a^self.read(abso()))
        elif op==0x5D:self.a=self.nz(self.a^self.read(abx()))
        elif op==0x59:self.a=self.nz(self.a^self.read(aby()))
        elif op==0x41:self.a=self.nz(self.a^self.read(idx()))
        elif op==0x51:self.a=self.nz(self.a^self.read(idy()))
        elif op==0xC9:self.cmp(self.a,self.read(imm()))
        elif op==0xC5:self.cmp(self.a,self.read(zpg()))
        elif op==0xD5:self.cmp(self.a,self.read(zpx()))
        elif op==0xCD:self.cmp(self.a,self.read(abso()))
        elif op==0xDD:self.cmp(self.a,self.read(abx()))
        elif op==0xD9:self.cmp(self.a,self.read(aby()))
        elif op==0xC1:self.cmp(self.a,self.read(idx()))
        elif op==0xD1:self.cmp(self.a,self.read(idy()))
        elif op==0xE0:self.cmp(self.x,self.read(imm()))
        elif op==0xE4:self.cmp(self.x,self.read(zpg()))
        elif op==0xEC:self.cmp(self.x,self.read(abso()))
        elif op==0xC0:self.cmp(self.y,self.read(imm()))
        elif op==0xC4:self.cmp(self.y,self.read(zpg()))
        elif op==0xCC:self.cmp(self.y,self.read(abso()))
        elif op==0xE6:a=zpg();self.write(a,self.nz((self.read(a)+1)&0xFF))
        elif op==0xF6:a=zpx();self.write(a,self.nz((self.read(a)+1)&0xFF))
        elif op==0xEE:a=abso();self.write(a,self.nz((self.read(a)+1)&0xFF))
        elif op==0xFE:a=abx();self.write(a,self.nz((self.read(a)+1)&0xFF))
        elif op==0xC6:a=zpg();self.write(a,self.nz((self.read(a)-1)&0xFF))
        elif op==0xD6:a=zpx();self.write(a,self.nz((self.read(a)-1)&0xFF))
        elif op==0xCE:a=abso();self.write(a,self.nz((self.read(a)-1)&0xFF))
        elif op==0xDE:a=abx();self.write(a,self.nz((self.read(a)-1)&0xFF))
        elif op==0xE8:self.x=self.nz((self.x+1)&0xFF)
        elif op==0xC8:self.y=self.nz((self.y+1)&0xFF)
        elif op==0xCA:self.x=self.nz((self.x-1)&0xFF)
        elif op==0x88:self.y=self.nz((self.y-1)&0xFF)
        elif op==0x0A:c=self.a>>7;self.a=self.nz((self.a<<1)&0xFF);self.flag_c=bool(c)
        elif op==0x06:a=zpg();v=self.read(a);c=v>>7;self.write(a,self.nz((v<<1)&0xFF));self.flag_c=bool(c)
        elif op==0x16:a=zpx();v=self.read(a);c=v>>7;self.write(a,self.nz((v<<1)&0xFF));self.flag_c=bool(c)
        elif op==0x0E:a=abso();v=self.read(a);c=v>>7;self.write(a,self.nz((v<<1)&0xFF));self.flag_c=bool(c)
        elif op==0x1E:a=abx();v=self.read(a);c=v>>7;self.write(a,self.nz((v<<1)&0xFF));self.flag_c=bool(c)
        elif op==0x4A:c=self.a&1;self.a=self.nz(self.a>>1);self.flag_c=bool(c)
        elif op==0x46:a=zpg();v=self.read(a);c=v&1;self.write(a,self.nz(v>>1));self.flag_c=bool(c)
        elif op==0x56:a=zpx();v=self.read(a);c=v&1;self.write(a,self.nz(v>>1));self.flag_c=bool(c)
        elif op==0x4E:a=abso();v=self.read(a);c=v&1;self.write(a,self.nz(v>>1));self.flag_c=bool(c)
        elif op==0x5E:a=abx();v=self.read(a);c=v&1;self.write(a,self.nz(v>>1));self.flag_c=bool(c)
        elif op==0x2A:c=int(self.flag_c);nc=self.a>>7;self.a=self.nz(((self.a<<1)|c)&0xFF);self.flag_c=bool(nc)
        elif op==0x26:a=zpg();v=self.read(a);c=int(self.flag_c);nc=v>>7;self.write(a,self.nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc)
        elif op==0x36:a=zpx();v=self.read(a);c=int(self.flag_c);nc=v>>7;self.write(a,self.nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc)
        elif op==0x2E:a=abso();v=self.read(a);c=int(self.flag_c);nc=v>>7;self.write(a,self.nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc)
        elif op==0x3E:a=abx();v=self.read(a);c=int(self.flag_c);nc=v>>7;self.write(a,self.nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc)
        elif op==0x6A:c=int(self.flag_c);nc=self.a&1;self.a=self.nz((self.a>>1)|(c<<7));self.flag_c=bool(nc)
        elif op==0x66:a=zpg();v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self.nz((v>>1)|(c<<7)));self.flag_c=bool(nc)
        elif op==0x76:a=zpx();v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self.nz((v>>1)|(c<<7)));self.flag_c=bool(nc)
        elif op==0x6E:a=abso();v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self.nz((v>>1)|(c<<7)));self.flag_c=bool(nc)
        elif op==0x7E:a=abx();v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self.nz((v>>1)|(c<<7)));self.flag_c=bool(nc)
        elif op==0x24:v=self.read(zpg());self.flag_n=bool(v&0x80);self.flag_v=bool(v&0x40);self.flag_z=(self.a&v)==0
        elif op==0x2C:v=self.read(abso());self.flag_n=bool(v&0x80);self.flag_v=bool(v&0x40);self.flag_z=(self.a&v)==0
        elif op==0x10:branch(not self.flag_n)
        elif op==0x30:branch(self.flag_n)
        elif op==0x50:branch(not self.flag_v)
        elif op==0x70:branch(self.flag_v)
        elif op==0x90:branch(not self.flag_c)
        elif op==0xB0:branch(self.flag_c)
        elif op==0xD0:branch(not self.flag_z)
        elif op==0xF0:branch(self.flag_z)
        elif op==0x4C:self.pc=abso()
        elif op==0x6C:
            a=abso();self.pc=self.ram[a]|(self.ram[(a&0xFF00)|((a+1)&0xFF)]<<8)
        elif op==0x20:a=abso();self.push16((self.pc-1)&0xFFFF);self.pc=a
        elif op==0x60:self.pc=(self.pop16()+1)&0xFFFF
        elif op==0x40:self.set_status(self.pop());self.pc=self.pop16()
        elif op==0x18:self.flag_c=False
        elif op==0x38:self.flag_c=True
        elif op==0x58:self.flag_i=False
        elif op==0x78:self.flag_i=True
        elif op==0xB8:self.flag_v=False
        elif op==0xD8:self.flag_d=False
        elif op==0xF8:self.flag_d=True
        elif op==0xEA:pass
        elif op==0x00:self.pc=(self.pc+1)&0xFFFF
        elif op in(0x1A,0x3A,0x5A,0x7A,0xDA,0xFA):pass
        elif op in(0x04,0x44,0x64):self.pc=(self.pc+1)&0xFFFF
        elif op in(0x0C,0x14,0x34,0x54,0x74,0xD4,0xF4):self.pc=(self.pc+1)&0xFFFF
        elif op in(0x1C,0x3C,0x5C,0x7C,0xDC,0xFC):self.pc=(self.pc+2)&0xFFFF
        elif op==0x80:self.pc=(self.pc+1)&0xFFFF
        elif op==0x89:self.pc=(self.pc+1)&0xFFFF
        elif op==0xA7:v=self.read(zpg());self.a=self.nz(v);self.x=v
        elif op==0xB7:v=self.read(zpy());self.a=self.nz(v);self.x=v
        elif op==0xAF:v=self.read(abso());self.a=self.nz(v);self.x=v
        elif op==0xBF:v=self.read(aby());self.a=self.nz(v);self.x=v
        elif op==0xA3:v=self.read(idx());self.a=self.nz(v);self.x=v
        elif op==0xB3:v=self.read(idy());self.a=self.nz(v);self.x=v
        elif op==0x87:self.write(zpg(),self.a&self.x)
        elif op==0x97:self.write(zpy(),self.a&self.x)
        elif op==0x8F:self.write(abso(),self.a&self.x)
        elif op==0x83:self.write(idx(),self.a&self.x)
        elif op==0xC7:a=zpg();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xD7:a=zpx();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xCF:a=abso();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xDF:a=abx();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xDB:a=aby();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xC3:a=idx();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xD3:a=idy();v=(self.read(a)-1)&0xFF;self.write(a,v);self.cmp(self.a,v)
        elif op==0xE7:a=zpg();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xF7:a=zpx();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xEF:a=abso();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xFF:a=abx();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xFB:a=aby();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xE3:a=idx();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0xF3:a=idy();v=(self.read(a)+1)&0xFF;self.write(a,v);self.sbc(v)
        elif op==0x07:a=zpg();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x17:a=zpx();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x0F:a=abso();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x1F:a=abx();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x1B:a=aby();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x03:a=idx();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x13:a=idy();v=self.read(a);self.flag_c=bool(v&0x80);v=(v<<1)&0xFF;self.write(a,v);self.a=self.nz(self.a|v)
        elif op==0x27:a=zpg();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x37:a=zpx();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x2F:a=abso();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x3F:a=abx();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x3B:a=aby();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x23:a=idx();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x33:a=idy();v=self.read(a);c=int(self.flag_c);self.flag_c=bool(v&0x80);v=((v<<1)|c)&0xFF;self.write(a,v);self.a=self.nz(self.a&v)
        elif op==0x67:a=zpg();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x77:a=zpx();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x6F:a=abso();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x7F:a=abx();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x7B:a=aby();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x63:a=idx();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x73:a=idy();v=self.read(a);c=int(self.flag_c);nc=v&1;v=(v>>1)|(c<<7);self.write(a,v);self.flag_c=bool(nc);self.adc(v)
        elif op==0x47:a=zpg();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x57:a=zpx();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x4F:a=abso();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x5F:a=abx();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x5B:a=aby();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x43:a=idx();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        elif op==0x53:a=idy();v=self.read(a);self.flag_c=bool(v&1);v=v>>1;self.write(a,v);self.a=self.nz(self.a^v)
        else:pass  # unknown/illegal opcode - ignore


# ═══════════════════════════════════════════════════════════════
# SID tune runner
# ═══════════════════════════════════════════════════════════════

class SIDRunner:
    def __init__(self,header,prg,load_addr,num_chips_override=None):
        self.h=header;self.cpu=CPU6502()
        self.cpu.ram[0]=0x2F;self.cpu.ram[1]=0x37  # default 6510 port state
        for i,b in enumerate(prg):
            addr=load_addr+i
            if addr<0x10000:self.cpu.ram[addr]=b
        # self-referential JMP trampoline used as a subroutine "return" marker
        s=CPU6502.STOP_ADDR
        self.cpu.ram[s]=0x4C;self.cpu.ram[s+1]=s&0xFF;self.cpu.ram[s+2]=(s>>8)&0xFF

        self.sid_bases=[0xD400]
        for raw in(header.second_sid,header.third_sid):
            if raw:
                addr=0xD000|((raw&0xFF)<<4)
                if addr not in self.sid_bases:self.sid_bases.append(addr)
        if num_chips_override is not None:
            if num_chips_override<len(self.sid_bases):
                self.sid_bases=self.sid_bases[:max(1,num_chips_override)]
            while len(self.sid_bases)<num_chips_override:
                # header didn't declare enough chips - guess the common $D420/$D440 layout
                guess=0xD400+0x20*len(self.sid_bases)
                self.sid_bases.append(guess)
        self.num_chips=len(self.sid_bases)

    def init_song(self,song_index0):
        self.cpu.a=song_index0&0xFF;self.cpu.x=0;self.cpu.y=0
        self.cpu.sp=0xFD;self.cpu.flag_i=False
        return self.cpu.call_subroutine(self.h.init_addr,max_cycles=2_000_000)

    def simulate(self,num_frames):
        """Runs play (or IRQ vector fallback) once per frame; returns, for
        every frame, a tuple of 25-byte register snapshots (one per SID
        chip, $xx00-$xx18) in self.sid_bases order."""
        play=self.h.play_addr;out=[]
        for frame in range(num_frames):
            if play:
                self.cpu.call_subroutine(play,max_cycles=300000)
            else:
                vec=self.cpu.read16(0xFFFE)
                if vec and vec!=CPU6502.STOP_ADDR:
                    self.cpu.call_subroutine(vec,max_cycles=300000)
            out.append(tuple(bytes(self.cpu.ram[b:b+0x19])for b in self.sid_bases))
        return out

# ═══════════════════════════════════════════════════════════════
# SID chip model (approximate — for WAV rendering)
# ═══════════════════════════════════════════════════════════════

ATTACK_MS=[2,8,16,24,38,56,68,80,100,250,500,800,1000,3000,5000,8000]
DR_MS=[6,24,48,72,114,168,204,240,300,750,1500,2400,3000,9000,15000,24000]

class SIDVoice:
    def __init__(self):
        self.freq=0;self.pw=0;self.control=0
        self.attack=0;self.decay=0;self.sustain=0;self.release=0
        self.acc=0;self.lfsr=0x7FFFFF
        self.env_state='release';self.env_level=0.0;self.gate_prev=0

    def set_control(self,v):
        gate=v&1
        if gate and not self.gate_prev:self.env_state='attack'
        elif not gate and self.gate_prev:self.env_state='release'
        self.gate_prev=gate;self.control=v

    def step_env(self,dt_ms):
        st=self.env_state
        if st=='sustain':
            self.env_level=self.sustain*17.0;return
        target=self.sustain*17.0 if st=='decay' else(255.0 if st=='attack' else 0.0)
        rate_ms=ATTACK_MS[self.attack]if st=='attack' else DR_MS[self.decay if st=='decay' else self.release]
        step=255.0/max(1,rate_ms)*dt_ms
        if st=='attack':
            self.env_level=min(255.0,self.env_level+step)
            if self.env_level>=255.0:self.env_state='decay'
        else:
            self.env_level=max(target,self.env_level-step)
            if st=='decay' and self.env_level<=target:self.env_state='sustain'

    def gen_wave(self):
        wf=(self.control>>4)&0xF
        if self.control&0x08:self.acc=0  # test bit holds accumulator at 0
        acc12=(self.acc>>12)&0xFFF
        parts=[]
        if wf&0x1:
            msb=(self.acc>>23)&1
            tri_acc=(self.acc^(0xFFFFFF if msb else 0))&0xFFFFFF
            parts.append((tri_acc>>11)&0xFFF)
        if wf&0x2:parts.append(acc12)
        if wf&0x4:parts.append(0xFFF if acc12>=self.pw else 0x000)
        if wf&0x8:parts.append((self.lfsr>>11)&0xFFF)
        if not parts:return 2048
        out=parts[0]
        for p in parts[1:]:out&=p
        return out

class SVFilter:
    """Chamberlin state-variable filter approximating the SID's analog filter."""
    def __init__(self,sr):
        self.sr=sr;self.low=0.0;self.band=0.0
    def process(self,x,cutoff_hz,q):
        f=2*math.sin(math.pi*min(cutoff_hz,self.sr*0.45)/self.sr)
        high=x-self.low-q*self.band
        self.band+=f*high;self.low+=f*self.band
        return self.low,self.band,high

class SIDEmulator:
    def __init__(self,clock_rate=PAL_CLOCK,sample_rate=44100,sid_model='6581'):
        self.clock=clock_rate;self.sr=sample_rate;self.sid_model=sid_model
        self.voices=[SIDVoice()for _ in range(3)]
        self.reg=bytearray(25)
        self.filter=SVFilter(sample_rate)
        self.step=clock_rate/sample_rate

    def set_registers(self,regs):
        self.reg[:len(regs)]=regs
        for i,v in enumerate(self.voices):
            b=i*7
            v.freq=self.reg[b]|(self.reg[b+1]<<8)
            v.pw=(self.reg[b+2]|(self.reg[b+3]<<8))&0xFFF
            v.set_control(self.reg[b+4])
            v.attack=(self.reg[b+5]>>4)&0xF;v.decay=self.reg[b+5]&0xF
            v.sustain=(self.reg[b+6]>>4)&0xF;v.release=self.reg[b+6]&0xF

    def render_sample(self):
        dt_ms=1000.0/self.sr
        fc=((self.reg[22]<<3)|(self.reg[21]&0x07))&0x7FF
        cutoff_hz=30+((fc/2047.0)**1.5)*10000
        resonance=(self.reg[23]>>4)&0xF
        q=1.0/(0.707+resonance/15.0*1.5)
        mode=(self.reg[24]>>4)&0x7
        vol=self.reg[24]&0x0F
        filt_sum=0.0;unfilt_sum=0.0
        for i,v in enumerate(self.voices):
            old_acc=v.acc
            v.acc=(v.acc+int(v.freq*self.step))&0xFFFFFF
            shifts=(v.acc>>19)-(old_acc>>19)
            if shifts<0:shifts+=32
            for _ in range(min(shifts,32)):
                bit=((v.lfsr>>22)^(v.lfsr>>17))&1
                v.lfsr=((v.lfsr<<1)|bit)&0x7FFFFF
            v.step_env(dt_ms)
            raw=v.gen_wave()
            amp=(raw-2048)/2048.0*(v.env_level/255.0)
            if self.reg[23]&(1<<i):filt_sum+=amp
            else:unfilt_sum+=amp
        filt_out=0.0
        if mode:
            low,band,high=self.filter.process(filt_sum,cutoff_hz,q)
            if mode&1:filt_out+=low
            if mode&2:filt_out+=band
            if mode&4:filt_out+=high
        else:
            filt_out=filt_sum
        mixed=(unfilt_sum+filt_out)*(vol/15.0)
        return mixed,mixed

# ═══════════════════════════════════════════════════════════════
# WAV renderer
# ═══════════════════════════════════════════════════════════════

def render_wav(frames_regs,filename,sample_rate=44100,clock_rate=PAL_CLOCK,
               sid_model='6581',frame_rate=50.0,volume=0.8,num_chips=1,stereo_spread=True):
    samples_per_frame=int(round(sample_rate/frame_rate))
    sids=[SIDEmulator(clock_rate,sample_rate,sid_model)for _ in range(num_chips)]
    if num_chips<=1 or not stereo_spread:
        pans=[(0.5,0.5)]*num_chips
    elif num_chips==2:
        pans=[(0.75,0.25),(0.25,0.75)]
    else:
        pans=[(0.75,0.25),(0.25,0.75),(0.5,0.5)]+[(0.5,0.5)]*(num_chips-3)
    pcm_l=[];pcm_r=[];total=0
    print(f"  Rendering WAV: {sample_rate}Hz, SID {sid_model} x{num_chips}...")
    for i,snap in enumerate(frames_regs):
        for c in range(num_chips):sids[c].set_registers(snap[c])
        for _ in range(samples_per_frame):
            l=0.0;r=0.0
            for c in range(num_chips):
                mono,_=sids[c].render_sample();pl,pr=pans[c]
                l+=mono*pl;r+=mono*pr
            pcm_l.append(l);pcm_r.append(r);total+=1
        if i%500==0 and i>0:
            print(f"    frame {i}/{len(frames_regs)} ({total/sample_rate:.1f}s)...",end='\r')
    print(f"    Rendered {total} samples ({total/sample_rate:.1f}s)        ")
    max_val=max((abs(v)for v in pcm_l+pcm_r),default=0)or 1
    scale=volume*32767/max_val;nch=2
    with open(filename,'wb') as f:
        data_size=total*nch*2
        f.write(b'RIFF');f.write(struct.pack('<I',36+data_size));f.write(b'WAVE')
        f.write(b'fmt ');f.write(struct.pack('<I',16))
        f.write(struct.pack('<H',1));f.write(struct.pack('<H',nch))
        f.write(struct.pack('<I',sample_rate))
        f.write(struct.pack('<I',sample_rate*nch*2))
        f.write(struct.pack('<H',nch*2));f.write(struct.pack('<H',16))
        f.write(b'data');f.write(struct.pack('<I',data_size))
        for i in range(total):
            sl=max(-32768,min(32767,int(pcm_l[i]*scale)))
            sr=max(-32768,min(32767,int(pcm_r[i]*scale)))
            f.write(struct.pack('<hh',sl,sr))
    return total/sample_rate

# ═══════════════════════════════════════════════════════════════
# Note extraction (raw & waveform-aware modes)
# ═══════════════════════════════════════════════════════════════

@dataclass
class NoteEvent:
    frame:int=0;channel:int=0;voice:int=0;mode:str=""
    event_type:str="";note:int=0;instrument:int=1;volume:int=64

def extract_notes_raw(frames_regs,clock,num_chips=1):
    events=[];nv=num_chips*3;pn=[-1]*nv;last_frame=0
    for frame,snap in enumerate(frames_regs):
        last_frame=frame
        for gv in range(nv):
            chip,v=divmod(gv,3);regs=snap[chip];b=v*7
            freq=regs[b]|(regs[b+1]<<8);ctrl=regs[b+4]
            gate=bool(ctrl&1);wf=(ctrl>>4)&0xF
            note=freq_to_xm_note(freq*clock/16777216.0)if(gate and wf and freq>0)else None
            if note is None:
                if pn[gv]>=0:
                    events.append(NoteEvent(frame,gv,gv,'','note_off',97));pn[gv]=-1
            elif note!=pn[gv]:
                if pn[gv]>=0:events.append(NoteEvent(frame,gv,gv,'','note_off',97))
                events.append(NoteEvent(frame,gv,gv,MODE_TRI,'note_on',note,1,64))
                pn[gv]=note
    for gv in range(nv):
        if pn[gv]>=0:events.append(NoteEvent(last_frame,gv,gv,'','note_off',97))
    return events

def extract_notes_sid(frames_regs,channel_map,clock,num_chips=1):
    events=[];mx=max(channel_map.values())+1;nv=num_chips*3
    pn=[-1]*mx;pi=[0]*mx;pmode=[None]*nv;pax=[-1]*nv;last_frame=0
    def gx(v,m):
        k=(v,m)
        if k in channel_map:return channel_map[k]
        for f in(MODE_PULSE,MODE_SAW,MODE_TRI,MODE_NOISE):
            if(v,f)in channel_map:return channel_map[(v,f)]
        return v%max(1,mx)
    for frame,snap in enumerate(frames_regs):
        last_frame=frame
        for gv in range(nv):
            chip,v=divmod(gv,3);regs=snap[chip];b=v*7
            freq=regs[b]|(regs[b+1]<<8);ctrl=regs[b+4]
            gate=bool(ctrl&1);wf=(ctrl>>4)&0xF
            note=None;mode=None;inst=0
            if gate and wf and freq>0:
                note=freq_to_xm_note(freq*clock/16777216.0)
                if wf&0x4:mode,inst=MODE_PULSE,3
                elif wf&0x2:mode,inst=MODE_SAW,2
                elif wf&0x1:mode,inst=MODE_TRI,1
                elif wf&0x8:mode,inst=MODE_NOISE,4
            if note is None or mode is None:
                ox=pax[gv]
                if ox>=0 and pn[ox]>=0:
                    events.append(NoteEvent(frame,ox,gv,pmode[gv]or'','note_off',97));pn[ox]=-1
                pmode[gv]=None;pax[gv]=-1;continue
            xc=gx(gv,mode)
            ox=pax[gv]
            if ox>=0 and ox!=xc and pn[ox]>=0:
                events.append(NoteEvent(frame,ox,gv,pmode[gv]or'','note_off',97));pn[ox]=-1
            pmode[gv]=mode;pax[gv]=xc
            if pn[xc]<0 or note!=pn[xc] or inst!=pi[xc]:
                if pn[xc]>=0:events.append(NoteEvent(frame,xc,gv,mode,'note_off',97))
                events.append(NoteEvent(frame,xc,gv,mode,'note_on',note,inst,64))
                pn[xc]=note;pi[xc]=inst
    for xc in range(mx):
        if pn[xc]>=0:events.append(NoteEvent(last_frame,xc,event_type='note_off',note=97))
    return events

# ═══════════════════════════════════════════════════════════════
# XM & MIDI writers
# ═══════════════════════════════════════════════════════════════

@dataclass
class TN:
    note:int=0;instrument:int=0;volume:int=0;effect:int=0;effect_param:int=0

class SIDGen:
    P=32
    @classmethod
    def triangle(c):return[int((4*abs(i/c.P-0.5)-1)*24000)for i in range(c.P)]
    @classmethod
    def sawtooth(c):return[int(((i/c.P)*2-1)*24000)for i in range(c.P)]
    @classmethod
    def pulse(c,duty=0.5):
        n=int(c.P*duty);return[24000 if i<n else -24000 for i in range(c.P)]
    @staticmethod
    def noise(n=4096):
        s=[];l=0x7FFFFF
        for _ in range(n):
            bit=((l>>22)^(l>>17))&1;l=((l<<1)|bit)&0x7FFFFF
            s.append(24000 if(l&1)else -24000)
        return s

class XMWriter:
    def build_and_write(self,fn,events,title="",bpm=125,speed=6,fr=50,nc=6):
        if nc%2:nc+=1
        nc=max(4,min(32,nc));insts=self._mi();rps=speed*2.5/bpm;fpr=max(1,int(fr*rps))
        rd={}
        for ev in events:
            if ev.event_type not in('note_on','note_off'):continue
            row=ev.frame//fpr;ch=ev.channel
            if ch>=nc:continue
            k=(row,ch)
            if ev.event_type=='note_on':rd[k]=TN(max(1,min(96,ev.note)),min(ev.instrument,len(insts)),0x10+min(0x40,max(0,ev.volume)))
            elif ev.event_type=='note_off' and k not in rd:rd[k]=TN(97)
        mr=max((k[0]for k in rd),default=0)+1;rpp=64;np=max(1,min(256,(mr+rpp-1)//rpp))
        pats=[]
        for pi in range(np):
            pat=[]
            for ri in range(rpp):
                row=[TN()for _ in range(nc)]
                for ch in range(nc):
                    k=(pi*rpp+ri,ch)
                    if k in rd:row[ch]=rd[k]
                pat.append(row)
            pats.append(pat)
        self._wx(fn,title,nc,bpm,speed,pats,insts,list(range(np)))
    def _mi(self):
        g=SIDGen
        def mk(n,p,l=True):
            return{'name':n[:22],'samples':[{'name':n[:22],'data':p,'length':len(p),'loop_start':0,'loop_length':len(p)if l else 0,'loop_type':1 if l else 0,'volume':64,'finetune':0,'panning':128,'relative_note':0,'bits':16}]}
        return[mk("SID Triangle",g.triangle()),mk("SID Sawtooth",g.sawtooth()),
               mk("SID Pulse",g.pulse()),mk("SID Noise",g.noise(),l=False)]
    def _wx(self,fn,title,nc,bpm,speed,pats,insts,order):
        with open(fn,'wb') as f:
            f.write(b'Extended Module: ');f.write(title.encode('ascii','replace')[:20].ljust(20,b'\x00'))
            f.write(b'\x1a');f.write(b'SID2XM Converter    ')
            f.write(struct.pack('<HIHHHHHHH',0x0104,276,len(order),0,nc,len(pats),min(len(insts),128),1,speed)+struct.pack('<H',bpm))
            ot=bytearray(256)
            for i,o in enumerate(order[:256]):ot[i]=o
            f.write(ot)
            for pat in pats:
                pk=bytearray()
                for row in pat:
                    for ch in range(nc):
                        nd=row[ch]if ch<len(row)else TN()
                        hn=nd.note>0;hi=nd.instrument>0;hv=nd.volume>0;hf=nd.effect>0;hp=nd.effect_param>0
                        if not(hn or hi or hv or hf or hp):pk.append(0x80)
                        else:
                            pb=0x80|(1 if hn else 0)|(2 if hi else 0)|(4 if hv else 0)|(8 if hf else 0)|(16 if hp else 0)
                            pk.append(pb)
                            if hn:pk.append(nd.note&0xFF)
                            if hi:pk.append(nd.instrument&0xFF)
                            if hv:pk.append(nd.volume&0xFF)
                            if hf:pk.append(nd.effect&0xFF)
                            if hp:pk.append(nd.effect_param&0xFF)
                f.write(struct.pack('<IBH',9,0,len(pat))+struct.pack('<H',len(pk))+pk)
            for inst in insts[:128]:
                ss=inst.get('samples',[])
                if not ss:f.write(struct.pack('<I',29)+inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00')+struct.pack('<BH',0,0));continue
                ihs=263;f.write(struct.pack('<I',ihs)+inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
                f.write(struct.pack('<BHI',0,len(ss),40)+bytearray(96))
                ve=bytearray(48);struct.pack_into('<HH',ve,0,0,64);struct.pack_into('<HH',ve,4,100,64);f.write(ve)
                pe=bytearray(48);struct.pack_into('<HH',pe,0,0,32);struct.pack_into('<HH',pe,4,100,32);f.write(pe)
                f.write(bytes([2,2,0,0,1,0,0,1,1,0,0,0,0,0])+struct.pack('<H',0x800))
                rem=ihs-(4+22+1+2+4+96+48+48+14+2)
                if rem>0:f.write(b'\x00'*rem)
                for s in ss:
                    bps=2 if s.get('bits',16)==16 else 1
                    f.write(struct.pack('<III',len(s['data'])*bps,s['loop_start']*bps,s['loop_length']*bps))
                    f.write(struct.pack('<Bb',s.get('volume',64),max(-128,min(127,s.get('finetune',0)))))
                    tb=s.get('loop_type',0)&3
                    if s.get('bits',16)==16:tb|=0x10
                    f.write(struct.pack('<BBbB',tb,s.get('panning',128),max(-128,min(127,s.get('relative_note',0))),0))
                    f.write(s.get('name','').encode('ascii','replace')[:22].ljust(22,b'\x00'))
                for s in ss:
                    prev=0
                    for v in s['data']:v=max(-32768,min(32767,v));d_=(((v-prev)+32768)%65536-32768);f.write(struct.pack('<h',d_));prev=v

class MIDIWriter:
    def __init__(self):self.tpqn=480
    @staticmethod
    def _vlq(v):
        if v<0:v=0
        r=[v&0x7F];v>>=7
        while v:r.append((v&0x7F)|0x80);v>>=7
        r.reverse();return bytes(r)
    def write(self,fn,events,bpm=125,speed=6,fr=50,title=""):
        tpqn=self.tpqn;tpf=tpqn*bpm/(60.0*fr);usec=int(60_000_000/bpm)
        chs={}
        for ev in events:chs.setdefault(ev.channel,[]).append(ev)
        tracks=[];t0=[(0,bytes([0xFF,0x51,0x03,(usec>>16)&0xFF,(usec>>8)&0xFF,usec&0xFF]))]
        if title:td=title.encode('ascii',errors='replace')[:127];t0.append((0,bytes([0xFF,0x01,len(td)])+td))
        tracks.append(t0);gm=[81,82,80,127]
        for ci,ch in enumerate(sorted(chs.keys())):
            if ci>=15:break
            mc=ci if ci<9 else ci+1;trk=[(0,bytes([0xC0|mc,gm[ci%4]]))];act={}
            for ev in chs[ch]:
                mt=int(ev.frame*tpf)
                if ev.event_type=='note_on':
                    mn=max(0,min(127,xm_note_to_midi(ev.note)));vel=max(1,min(127,ev.volume*2))
                    if ch in act:pn,_=act[ch];trk.append((mt,bytes([0x80|mc,pn,0])))
                    trk.append((mt,bytes([0x90|mc,mn,vel])));act[ch]=(mn,mt)
                elif ev.event_type=='note_off':
                    if ch in act:pn,_=act[ch];trk.append((mt,bytes([0x80|mc,pn,0])));del act[ch]
            for pn,pt in act.values():trk.append((pt+tpqn,bytes([0x80|mc,pn,0])))
            tracks.append(trk)
        with open(fn,'wb') as f:
            f.write(b'MThd'+struct.pack('>I',6)+struct.pack('>HHH',1,len(tracks),tpqn))
            for t in tracks:
                t.sort(key=lambda x:x[0]);d=bytearray();prev=0
                for at,ed in t:d.extend(self._vlq(max(0,at-prev)));d.extend(ed);prev=at
                d.extend(self._vlq(0));d.extend(b'\xFF\x2F\x00')
                f.write(b'MTrk'+struct.pack('>I',len(d))+d)

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_sid(input_file,output_file=None,fmt='xm',midi_file=None,wav_file=None,
                channel_map_spec='default',raw_notes=False,sample_rate=44100,
                wav_volume=0.8,song=None,seconds=60.0,clock_override=None,
                sid_model_override=None,sids_override=None,mono=False):
    base=os.path.splitext(input_file)[0]
    if not output_file:output_file=base+'.xm'
    with open(input_file,'rb') as f:data=f.read()
    header,prg,load_addr=parse_sid(data)

    is_pal = header.is_pal if clock_override is None else(clock_override=='pal')
    clock = PAL_CLOCK if is_pal else NTSC_CLOCK
    sid_model = sid_model_override or header.sid_model
    speed_bit=(header.speed>>0)&1  # bit for song 1; good enough approximation for all subtunes
    frame_rate = 60.0 if speed_bit else(50.0 if is_pal else 60.0)

    print(f"Конвертация: {input_file}")
    print(f"  Title: {header.name}")
    print(f"  Author: {header.author}")
    print(f"  Released: {header.released}")
    print(f"  Version: {header.version}, Load: ${load_addr:04X}, Init: ${header.init_addr:04X}, Play: ${header.play_addr:04X}")
    print(f"  Songs: {header.num_songs}, Start: {header.start_song}, {'PAL' if is_pal else 'NTSC'}, SID: {sid_model}, {frame_rate:.0f}Hz")

    song_idx=(song-1)if song else(header.start_song-1)
    runner=SIDRunner(header,prg,load_addr,sids_override)
    num_chips=runner.num_chips
    if num_chips>1:
        addrs=', '.join(f"${b:04X}" for b in runner.sid_bases)
        print(f"  Multi-SID: {num_chips} чипа ({addrs}), voices 1-{num_chips*3}")
    channel_map=parse_channel_map(channel_map_spec,num_chips)
    num_xm_ch=max(channel_map.values())+1

    ok=runner.init_song(song_idx)
    if not ok:print("  Предупреждение: init routine не завершилась штатно (timeout)")

    num_frames=max(1,int(seconds*frame_rate))
    print(f"  Рендеринг {num_frames} фреймов ({seconds:.0f}s)...")
    frames_regs=runner.simulate(num_frames)

    if fmt in('wav','all')or wav_file:
        wf=wav_file or base+'.wav'
        dur=render_wav(frames_regs,wf,sample_rate,clock,sid_model,frame_rate,wav_volume,
                        num_chips,not mono)
        print(f"  WAV: {wf} ({os.path.getsize(wf)/1024:.1f} KB, {dur:.1f}s)")
        if fmt=='wav' and not midi_file:
            print(f"\nГотово!")
            return

    if raw_notes:
        num_xm=num_chips*3;mode_str="RAW"
        events=extract_notes_raw(frames_regs,clock,num_chips)
    else:
        num_xm=num_xm_ch;mode_str=f"Waveform ({channel_map_spec})"
        events=extract_notes_sid(frames_regs,channel_map,clock,num_chips)

    note_ons=[e for e in events if e.event_type=='note_on']
    print(f"  Mode: {mode_str}, Notes: {len(note_ons)}")
    if not note_ons and fmt not in('wav',):
        print("  No notes!");return

    bpm=125;speed=6
    xm_nc=max(4,min(32,num_xm+(num_xm%2)))
    if xm_nc<num_xm:
        print(f"  Внимание: {num_xm} XM-каналов не влезает в лимит 32, часть голосов будет потеряна")

    if fmt in('xm','both','all'):
        xf=output_file if output_file.endswith('.xm')else base+'.xm'
        XMWriter().build_and_write(xf,events,header.name,bpm,speed,frame_rate,num_xm)
        print(f"  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {xm_nc} ch)")

    if fmt in('midi','both','all')or midi_file:
        mf=midi_file or base+'.mid'
        MIDIWriter().write(mf,events,bpm,speed,frame_rate,header.name)
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")

    if note_ons:
        dur=max(e.frame for e in events)/frame_rate
        print(f"\n  Total: {len(note_ons)} notes, {dur:.1f}s")


def main():
    import argparse
    ap=argparse.ArgumentParser(
        description='SID→XM/MIDI/WAV Converter v1',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output formats:
  (default)     XM only
  --midi        XM + MIDI
  --midi-only   MIDI only
  --wav         WAV only (SID emulation)
  --all         XM + MIDI + WAV

SID options:
  --song N        Subtune number, 1-based (default: header's start song)
  --seconds N      Render duration in seconds (default 60 — SID files don't store length)
  --clock pal|ntsc Override auto-detected video clock
  --sid-model 6581|8580  Override auto-detected chip model
  --sids N         Force number of SID chips to emulate (default: auto-detect from header)
  --mono           Sum multi-SID chips to mono instead of stereo-spreading them

Note modes:
  --raw-notes   Ignore waveform, one channel per voice (3 x number of SID chips)
  (default)     Waveform-aware split with --channel-map

Multi-SID:
  Voices are numbered globally across chips: chip1=voices 1-3, chip2=voices 4-6, ...
  --channel-map accepts 's<chip><voice>=<ch>' tokens, e.g. s1a=0,s2c=5, or plain
  global voice indices 0-8.

Examples:
  python sid2xm-midi-wav.py music.sid --wav
  python sid2xm-midi-wav.py music.sid --all --seconds 120
  python sid2xm-midi-wav.py music.sid --channel-map split --midi
  python sid2xm-midi-wav.py music.sid --song 2 --wav
  python sid2xm-midi-wav.py 3sid_tune.sid --all --sids 3
""")
    ap.add_argument('input',nargs='*')
    ap.add_argument('-o','--output')
    ap.add_argument('--midi',action='store_true')
    ap.add_argument('--midi-only',action='store_true')
    ap.add_argument('--midi-file',type=str,default=None)
    ap.add_argument('--wav',action='store_true',help='Render WAV')
    ap.add_argument('--wav-file',type=str,default=None)
    ap.add_argument('--all',action='store_true',help='Output XM+MIDI+WAV')
    ap.add_argument('--sample-rate',type=int,default=44100)
    ap.add_argument('--wav-volume',type=float,default=0.8)
    ap.add_argument('--channel-map',type=str,default='default')
    ap.add_argument('--raw-notes',action='store_true')
    ap.add_argument('--list-presets',action='store_true')
    ap.add_argument('--song',type=int,default=None)
    ap.add_argument('--seconds',type=float,default=60.0)
    ap.add_argument('--clock',type=str,choices=['pal','ntsc'],default=None)
    ap.add_argument('--sid-model',type=str,choices=['6581','8580'],default=None)
    ap.add_argument('--sids',type=int,default=None,help='Force number of SID chips (1-3)')
    ap.add_argument('--mono',action='store_true',help='Mix multi-SID chips to mono')
    args=ap.parse_args()

    if args.list_presets:
        nchips=args.sids or 3
        for name,m in build_presets(nchips).items():
            nc=max(m.values())+1;print(f"  {name} ({nc} ch, {nchips} SID chip(s)):")
            for(v,mode),xm in sorted(m.items()):print(f"    {get_channel_name(v,mode,nchips):>10s} → XM {xm}")
            print()
        sys.exit(0)

    if not args.input:ap.print_help();sys.exit(1)
    if args.output and len(args.input)>1:print("--output: single file only");sys.exit(1)

    if args.all:fmt='all'
    elif args.wav and not args.midi and not args.midi_only:fmt='wav'
    elif args.midi_only:fmt='midi'
    elif args.midi:fmt='both'
    else:fmt='xm'

    for inp in args.input:
        if not os.path.exists(inp):print(f"Not found: {inp}");continue
        try:
            convert_sid(inp,args.output,fmt,args.midi_file,args.wav_file,
                        args.channel_map,args.raw_notes,args.sample_rate,
                        args.wav_volume,args.song,args.seconds,args.clock,
                        args.sid_model,args.sids,args.mono)
        except Exception as e:
            import traceback;print(f"Error: {e}");traceback.print_exc()
    print("\nГотово!")

if __name__=='__main__':main()
