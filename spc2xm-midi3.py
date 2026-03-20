"""
SPC to XM/MIDI Converter v9
Добавлена конвертация в MIDI формат.
Требуется: pip install midiutil (только для MIDI экспорта)
"""

import struct
import sys
import os
import math
from dataclasses import dataclass, field
from typing import Optional


class DSPAddr:
    VOL_L=0x00;VOL_R=0x01;PITCH_L=0x02;PITCH_H=0x03
    SRCN=0x04;ADSR1=0x05;ADSR2=0x06;GAIN=0x07
    ENVX=0x08;OUTX=0x09;KON=0x4C;KOFF=0x5C
    FLG=0x6C;ENDX=0x7C;DIR=0x5D


def pitch_to_xm_note(pitch):
    if pitch<=0 or pitch>0x3FFF: return None
    ref_pitch=0x03A2; ref_note=58
    ratio=pitch/ref_pitch
    if ratio<=0: return None
    semitones=12.0*math.log2(ratio)
    xm_note=ref_note+int(round(semitones))
    return xm_note if 1<=xm_note<=96 else None


def xm_note_to_midi(xm_note):
    """XM note (1-96) -> MIDI note (0-127). XM C-0=1 -> MIDI C0=12"""
    return xm_note + 11


@dataclass
class SPCHeader:
    has_id666: bool = False
    title: str = ""
    game: str = ""
    pc: int = 0; a: int = 0; x: int = 0; y: int = 0
    psw: int = 0; sp: int = 0
    # Новые поля для длительности
    duration_sec: int = 0       # длина трека в секундах (из ID666)
    fade_ms: int = 0            # fade out в миллисекундах
    artist: str = ""



@dataclass
class BRRSample:
    source_index:int=0;start_address:int=0;loop_address:int=0
    has_loop:bool=False;pcm_data:list=field(default_factory=list);name:str=""

@dataclass
class TrackerNote:
    note:int=0;instrument:int=0;volume:int=0;effect:int=0;effect_param:int=0

@dataclass
class NoteEvent:
    tick:int=0;channel:int=0;event_type:str=""
    note:int=0;instrument:int=0;volume:int=64;pitch:int=0


class BRRDecoder:
    @staticmethod
    def decode_sample(ram,start_addr,max_blocks=4096):
        pcm=[];p1=p2=0;addr=start_addr;has_loop=False
        for _ in range(max_blocks):
            if addr+9>len(ram): break
            hdr=ram[addr];shift=(hdr>>4)&0xF;filt=(hdr>>2)&3
            lf=bool(hdr&2);ef=bool(hdr&1)
            for bi in range(1,9):
                b=ram[addr+bi]
                for nib in range(2):
                    s=((b>>4)&0xF) if nib==0 else (b&0xF)
                    if s>=8: s-=16
                    if shift<=12: s=(s<<shift)>>1
                    else: s=(-1 if s<0 else 0)>>1
                    if filt==1: s+=p1+(-p1>>4)
                    elif filt==2: s+=(p1<<1)+(-(p1+(p1<<1))>>5)-p2+(p2>>4)
                    elif filt==3: s+=(p1<<1)+(-(p1+(p1<<2)+(p1<<3))>>6)-p2+((p2+(p2<<1))>>4)
                    s=max(-32768,min(32767,s));pcm.append(s);p2=p1;p1=s
            if ef:
                if lf: has_loop=True
                break
            addr+=9
        return pcm or [0]*16, has_loop


class SPC700Emulator:
    def __init__(self,ram,dsp_regs,header):
        self.ram=bytearray(ram);self.dsp=bytearray(dsp_regs)
        self.pc=header.pc;self.a=header.a;self.x=header.x
        self.y=header.y;self.sp=header.sp;self._unpack_psw(header.psw)
        self.timer_target=[self.ram[0xFA],self.ram[0xFB],self.ram[0xFC]]
        self.timer_counter=[0,0,0];self.timer_out=[0,0,0]
        flg=self.ram[0xF1]
        self.timer_enabled=[bool(flg&1),bool(flg&2),bool(flg&4)]
        self.dsp_writes=[];self.cycles=0;self.total_cycles=0
        self.ports_in=[0]*4;self.ports_out=[0]*4;self.timer_read_log=[]

    def _unpack_psw(self,p):
        self.flag_c=bool(p&1);self.flag_z=bool(p&2);self.flag_i=bool(p&4)
        self.flag_h=bool(p&8);self.flag_b=bool(p&16);self.flag_p=bool(p&32)
        self.flag_v=bool(p&64);self.flag_n=bool(p&128)
    def _pack_psw(self):
        return int(self.flag_c)|(int(self.flag_z)<<1)|(int(self.flag_i)<<2)|\
               (int(self.flag_h)<<3)|(int(self.flag_b)<<4)|(int(self.flag_p)<<5)|\
               (int(self.flag_v)<<6)|(int(self.flag_n)<<7)
    @property
    def dp_base(self): return 0x100 if self.flag_p else 0
    def read(self,addr):
        addr&=0xFFFF
        if addr==0xF2: return self.ram[0xF2]
        if addr==0xF3: return self.dsp[self.ram[0xF2]&0x7F]
        if 0xF4<=addr<=0xF7: return self.ports_in[addr-0xF4]
        if addr==0xFD:
            v=self.timer_out[0];self.timer_out[0]=0
            if v: self.timer_read_log.append((self.total_cycles,0))
            return v&0xF
        if addr==0xFE:
            v=self.timer_out[1];self.timer_out[1]=0
            if v: self.timer_read_log.append((self.total_cycles,1))
            return v&0xF
        if addr==0xFF:
            v=self.timer_out[2];self.timer_out[2]=0
            if v: self.timer_read_log.append((self.total_cycles,2))
            return v&0xF
        return self.ram[addr]
    def write(self,addr,val):
        addr&=0xFFFF;val&=0xFF
        if addr==0xF1:
            for i in range(3):
                en=bool(val&(1<<i))
                if not en: self.timer_counter[i]=0;self.timer_out[i]=0
                self.timer_enabled[i]=en
            return
        if addr==0xF2: self.ram[0xF2]=val; return
        if addr==0xF3:
            da=self.ram[0xF2]&0x7F;self.dsp[da]=val
            self.dsp_writes.append((self.total_cycles,da,val)); return
        if 0xF4<=addr<=0xF7: self.ports_out[addr-0xF4]=val; return
        if addr==0xFA: self.timer_target[0]=val; return
        if addr==0xFB: self.timer_target[1]=val; return
        if addr==0xFC: self.timer_target[2]=val; return
        if 0xFD<=addr<=0xFF: return
        self.ram[addr]=val
    def read16(self,a): return self.read(a)|(self.read((a+1)&0xFFFF)<<8)
    def write16(self,a,v): self.write(a,v&0xFF);self.write((a+1)&0xFFFF,(v>>8)&0xFF)
    def fetch(self): v=self.read(self.pc);self.pc=(self.pc+1)&0xFFFF; return v
    def fetch16(self): lo=self.fetch(); return lo|(self.fetch()<<8)
    def push(self,v): self.write(0x100+self.sp,v&0xFF);self.sp=(self.sp-1)&0xFF
    def pop(self): self.sp=(self.sp+1)&0xFF; return self.read(0x100+self.sp)
    def push16(self,v): self.push((v>>8)&0xFF);self.push(v&0xFF)
    def pop16(self): lo=self.pop(); return lo|(self.pop()<<8)
    def dp(self,o=0): return (self.dp_base+o)&0xFFFF
    def _nz(self,v): v&=0xFF;self.flag_n=bool(v&0x80);self.flag_z=v==0; return v
    def _nz16(self,v): v&=0xFFFF;self.flag_n=bool(v&0x8000);self.flag_z=v==0; return v
    def _adc(self,a,b):
        c=int(self.flag_c);r=a+b+c;self.flag_c=r>0xFF
        self.flag_h=bool((a^b^r)&0x10);self.flag_v=bool((~(a^b)&(a^r))&0x80)
        return self._nz(r)
    def _sbc(self,a,b):
        c=int(self.flag_c);r=a-b-(1-c);self.flag_c=r>=0
        self.flag_h=not bool((a^b^r)&0x10);self.flag_v=bool(((a^b)&(a^r))&0x80)
        return self._nz(r&0xFF)
    def _cmp(self,a,b): r=a-b;self.flag_c=r>=0;self._nz(r&0xFF)
    def tick_timers(self,cyc):
        self.cycles+=cyc
        while self.cycles>=128:
            self.cycles-=128
            for t in range(2):
                if self.timer_enabled[t]:
                    self.timer_counter[t]+=1
                    tgt=self.timer_target[t] or 256
                    if self.timer_counter[t]>=tgt:
                        self.timer_counter[t]=0;self.timer_out[t]=(self.timer_out[t]+1)&0xF
            if self.timer_enabled[2]:
                self.timer_counter[2]+=1
                tgt2=self.timer_target[2] or 256
                if self.timer_counter[2]>=max(1,tgt2>>3):
                    self.timer_counter[2]=0;self.timer_out[2]=(self.timer_out[2]+1)&0xF
    def run(self,max_cycles):
        start=len(self.dsp_writes);target=self.total_cycles+max_cycles
        safety=0;limit=max_cycles*2
        while self.total_cycles<target and safety<limit:
            cyc=self.step();self.total_cycles+=cyc;self.tick_timers(cyc);safety+=1
        return self.dsp_writes[start:]
    def step(self):
        op=self.fetch()
        if op==0xE8: self.a=self._nz(self.fetch()); return 2
        if op==0xCD: self.x=self._nz(self.fetch()); return 2
        if op==0x8D: self.y=self._nz(self.fetch()); return 2
        if op==0x7D: self.a=self._nz(self.x); return 2
        if op==0xDD: self.a=self._nz(self.y); return 2
        if op==0x5D: self.x=self._nz(self.a); return 2
        if op==0xFD: self.y=self._nz(self.a); return 2
        if op==0x9D: self.x=self._nz(self.sp); return 2
        if op==0xBD: self.sp=self.x; return 2
        if op==0xE4: d=self.fetch();self.a=self._nz(self.read(self.dp(d))); return 3
        if op==0xF4: d=self.fetch();self.a=self._nz(self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0xE5: self.a=self._nz(self.read(self.fetch16())); return 4
        if op==0xF5: a=self.fetch16();self.a=self._nz(self.read((a+self.x)&0xFFFF)); return 5
        if op==0xF6: a=self.fetch16();self.a=self._nz(self.read((a+self.y)&0xFFFF)); return 5
        if op==0xE6: self.a=self._nz(self.read(self.dp(self.x))); return 3
        if op==0xBF: self.a=self._nz(self.read(self.dp(self.x)));self.x=(self.x+1)&0xFF; return 4
        if op==0xE7: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._nz(self.read(p)); return 6
        if op==0xF7: d=self.fetch();p=self.read16(self.dp(d));self.a=self._nz(self.read((p+self.y)&0xFFFF)); return 6
        if op==0xF8: d=self.fetch();self.x=self._nz(self.read(self.dp(d))); return 3
        if op==0xF9: d=self.fetch();self.x=self._nz(self.read(self.dp((d+self.y)&0xFF))); return 4
        if op==0xE9: self.x=self._nz(self.read(self.fetch16())); return 4
        if op==0xEB: d=self.fetch();self.y=self._nz(self.read(self.dp(d))); return 3
        if op==0xFB: d=self.fetch();self.y=self._nz(self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0xEC: self.y=self._nz(self.read(self.fetch16())); return 4
        if op==0xC4: d=self.fetch();self.write(self.dp(d),self.a); return 4
        if op==0xD4: d=self.fetch();self.write(self.dp((d+self.x)&0xFF),self.a); return 5
        if op==0xC5: self.write(self.fetch16(),self.a); return 5
        if op==0xD5: a=self.fetch16();self.write((a+self.x)&0xFFFF,self.a); return 6
        if op==0xD6: a=self.fetch16();self.write((a+self.y)&0xFFFF,self.a); return 6
        if op==0xC6: self.write(self.dp(self.x),self.a); return 4
        if op==0xAF: self.write(self.dp(self.x),self.a);self.x=(self.x+1)&0xFF; return 4
        if op==0xC7: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.write(p,self.a); return 7
        if op==0xD7: d=self.fetch();p=self.read16(self.dp(d));self.write((p+self.y)&0xFFFF,self.a); return 7
        if op==0xD8: d=self.fetch();self.write(self.dp(d),self.x); return 4
        if op==0xD9: d=self.fetch();self.write(self.dp((d+self.y)&0xFF),self.x); return 5
        if op==0xC9: self.write(self.fetch16(),self.x); return 5
        if op==0xCB: d=self.fetch();self.write(self.dp(d),self.y); return 4
        if op==0xDB: d=self.fetch();self.write(self.dp((d+self.x)&0xFF),self.y); return 5
        if op==0xCC: self.write(self.fetch16(),self.y); return 5
        if op==0x8F: i=self.fetch();d=self.fetch();self.write(self.dp(d),i); return 5
        if op==0xFA: s=self.fetch();d=self.fetch();self.write(self.dp(d),self.read(self.dp(s))); return 5
        if op==0xBA:
            d=self.fetch();self.a=self.read(self.dp(d));self.y=self.read(self.dp((d+1)&0xFF))
            self._nz16(self.a|(self.y<<8)); return 5
        if op==0xDA:
            d=self.fetch();self.write(self.dp(d),self.a);self.write(self.dp((d+1)&0xFF),self.y); return 4
        if op==0x88: self.a=self._adc(self.a,self.fetch()); return 2
        if op==0x84: d=self.fetch();self.a=self._adc(self.a,self.read(self.dp(d))); return 3
        if op==0x94: d=self.fetch();self.a=self._adc(self.a,self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0x85: self.a=self._adc(self.a,self.read(self.fetch16())); return 4
        if op==0x95: a=self.fetch16();self.a=self._adc(self.a,self.read((a+self.x)&0xFFFF)); return 5
        if op==0x96: a=self.fetch16();self.a=self._adc(self.a,self.read((a+self.y)&0xFFFF)); return 5
        if op==0x87: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._adc(self.a,self.read(p)); return 6
        if op==0x97: d=self.fetch();p=self.read16(self.dp(d));self.a=self._adc(self.a,self.read((p+self.y)&0xFFFF)); return 6
        if op==0x99: a=self.read(self.dp(self.x));b=self.read(self.dp(self.y));self.write(self.dp(self.x),self._adc(a,b)); return 5
        if op==0x89: s=self.fetch();d=self.fetch();self.write(self.dp(d),self._adc(self.read(self.dp(d)),self.read(self.dp(s)))); return 6
        if op==0x98: i=self.fetch();d=self.fetch();self.write(self.dp(d),self._adc(self.read(self.dp(d)),i)); return 5
        if op==0xA8: self.a=self._sbc(self.a,self.fetch()); return 2
        if op==0xA4: d=self.fetch();self.a=self._sbc(self.a,self.read(self.dp(d))); return 3
        if op==0xB4: d=self.fetch();self.a=self._sbc(self.a,self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0xA5: self.a=self._sbc(self.a,self.read(self.fetch16())); return 4
        if op==0xB5: a=self.fetch16();self.a=self._sbc(self.a,self.read((a+self.x)&0xFFFF)); return 5
        if op==0xB6: a=self.fetch16();self.a=self._sbc(self.a,self.read((a+self.y)&0xFFFF)); return 5
        if op==0xA7: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._sbc(self.a,self.read(p)); return 6
        if op==0xB7: d=self.fetch();p=self.read16(self.dp(d));self.a=self._sbc(self.a,self.read((p+self.y)&0xFFFF)); return 6
        if op==0xB9: a=self.read(self.dp(self.x));b=self.read(self.dp(self.y));self.write(self.dp(self.x),self._sbc(a,b)); return 5
        if op==0xA9: s=self.fetch();d=self.fetch();self.write(self.dp(d),self._sbc(self.read(self.dp(d)),self.read(self.dp(s)))); return 6
        if op==0xB8: i=self.fetch();d=self.fetch();self.write(self.dp(d),self._sbc(self.read(self.dp(d)),i)); return 5
        if op==0x68: self._cmp(self.a,self.fetch()); return 2
        if op==0x64: d=self.fetch();self._cmp(self.a,self.read(self.dp(d))); return 3
        if op==0x74: d=self.fetch();self._cmp(self.a,self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0x65: self._cmp(self.a,self.read(self.fetch16())); return 4
        if op==0x75: a=self.fetch16();self._cmp(self.a,self.read((a+self.x)&0xFFFF)); return 5
        if op==0x76: a=self.fetch16();self._cmp(self.a,self.read((a+self.y)&0xFFFF)); return 5
        if op==0x67: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self._cmp(self.a,self.read(p)); return 6
        if op==0x77: d=self.fetch();p=self.read16(self.dp(d));self._cmp(self.a,self.read((p+self.y)&0xFFFF)); return 6
        if op==0xC8: self._cmp(self.x,self.fetch()); return 2
        if op==0x3E: d=self.fetch();self._cmp(self.x,self.read(self.dp(d))); return 3
        if op==0x1E: self._cmp(self.x,self.read(self.fetch16())); return 4
        if op==0xAD: self._cmp(self.y,self.fetch()); return 2
        if op==0x7E: d=self.fetch();self._cmp(self.y,self.read(self.dp(d))); return 3
        if op==0x5E: self._cmp(self.y,self.read(self.fetch16())); return 4
        if op==0x69: s=self.fetch();d=self.fetch();self._cmp(self.read(self.dp(d)),self.read(self.dp(s))); return 6
        if op==0x78: i=self.fetch();d=self.fetch();self._cmp(self.read(self.dp(d)),i); return 5
        if op==0x28: self.a=self._nz(self.a&self.fetch()); return 2
        if op==0x24: d=self.fetch();self.a=self._nz(self.a&self.read(self.dp(d))); return 3
        if op==0x34: d=self.fetch();self.a=self._nz(self.a&self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0x25: self.a=self._nz(self.a&self.read(self.fetch16())); return 4
        if op==0x35: a=self.fetch16();self.a=self._nz(self.a&self.read((a+self.x)&0xFFFF)); return 5
        if op==0x36: a=self.fetch16();self.a=self._nz(self.a&self.read((a+self.y)&0xFFFF)); return 5
        if op==0x27: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._nz(self.a&self.read(p)); return 6
        if op==0x37: d=self.fetch();p=self.read16(self.dp(d));self.a=self._nz(self.a&self.read((p+self.y)&0xFFFF)); return 6
        if op==0x29: s=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))&self.read(self.dp(s)))); return 6
        if op==0x38: i=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))&i)); return 5
        if op==0x08: self.a=self._nz(self.a|self.fetch()); return 2
        if op==0x04: d=self.fetch();self.a=self._nz(self.a|self.read(self.dp(d))); return 3
        if op==0x14: d=self.fetch();self.a=self._nz(self.a|self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0x05: self.a=self._nz(self.a|self.read(self.fetch16())); return 4
        if op==0x15: a=self.fetch16();self.a=self._nz(self.a|self.read((a+self.x)&0xFFFF)); return 5
        if op==0x16: a=self.fetch16();self.a=self._nz(self.a|self.read((a+self.y)&0xFFFF)); return 5
        if op==0x07: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._nz(self.a|self.read(p)); return 6
        if op==0x17: d=self.fetch();p=self.read16(self.dp(d));self.a=self._nz(self.a|self.read((p+self.y)&0xFFFF)); return 6
        if op==0x09: s=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))|self.read(self.dp(s)))); return 6
        if op==0x18: i=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))|i)); return 5
        if op==0x48: self.a=self._nz(self.a^self.fetch()); return 2
        if op==0x44: d=self.fetch();self.a=self._nz(self.a^self.read(self.dp(d))); return 3
        if op==0x54: d=self.fetch();self.a=self._nz(self.a^self.read(self.dp((d+self.x)&0xFF))); return 4
        if op==0x45: self.a=self._nz(self.a^self.read(self.fetch16())); return 4
        if op==0x55: a=self.fetch16();self.a=self._nz(self.a^self.read((a+self.x)&0xFFFF)); return 5
        if op==0x56: a=self.fetch16();self.a=self._nz(self.a^self.read((a+self.y)&0xFFFF)); return 5
        if op==0x47: d=self.fetch();p=self.read16(self.dp((d+self.x)&0xFF));self.a=self._nz(self.a^self.read(p)); return 6
        if op==0x57: d=self.fetch();p=self.read16(self.dp(d));self.a=self._nz(self.a^self.read((p+self.y)&0xFFFF)); return 6
        if op==0x49: s=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))^self.read(self.dp(s)))); return 6
        if op==0x58: i=self.fetch();d=self.fetch();self.write(self.dp(d),self._nz(self.read(self.dp(d))^i)); return 5
        if op==0xBC: self.a=self._nz((self.a+1)&0xFF); return 2
        if op==0x3D: self.x=self._nz((self.x+1)&0xFF); return 2
        if op==0xFC: self.y=self._nz((self.y+1)&0xFF); return 2
        if op==0xAB: d=self.fetch();a=self.dp(d);self.write(a,self._nz((self.read(a)+1)&0xFF)); return 4
        if op==0xBB: d=self.fetch();a=self.dp((d+self.x)&0xFF);self.write(a,self._nz((self.read(a)+1)&0xFF)); return 5
        if op==0xAC: a=self.fetch16();self.write(a,self._nz((self.read(a)+1)&0xFF)); return 5
        if op==0x9C: self.a=self._nz((self.a-1)&0xFF); return 2
        if op==0x1D: self.x=self._nz((self.x-1)&0xFF); return 2
        if op==0xDC: self.y=self._nz((self.y-1)&0xFF); return 2
        if op==0x8B: d=self.fetch();a=self.dp(d);self.write(a,self._nz((self.read(a)-1)&0xFF)); return 4
        if op==0x9B: d=self.fetch();a=self.dp((d+self.x)&0xFF);self.write(a,self._nz((self.read(a)-1)&0xFF)); return 5
        if op==0x8C: a=self.fetch16();self.write(a,self._nz((self.read(a)-1)&0xFF)); return 5
        if op==0x1C: c=(self.a>>7)&1;self.a=self._nz((self.a<<1)&0xFF);self.flag_c=bool(c); return 2
        if op==0x0B: d=self.fetch();a=self.dp(d);v=self.read(a);c=(v>>7)&1;self.write(a,self._nz((v<<1)&0xFF));self.flag_c=bool(c); return 4
        if op==0x1B: d=self.fetch();a=self.dp((d+self.x)&0xFF);v=self.read(a);c=(v>>7)&1;self.write(a,self._nz((v<<1)&0xFF));self.flag_c=bool(c); return 5
        if op==0x0C: a=self.fetch16();v=self.read(a);c=(v>>7)&1;self.write(a,self._nz((v<<1)&0xFF));self.flag_c=bool(c); return 5
        if op==0x5C: c=self.a&1;self.a=self._nz(self.a>>1);self.flag_c=bool(c); return 2
        if op==0x4B: d=self.fetch();a=self.dp(d);v=self.read(a);c=v&1;self.write(a,self._nz(v>>1));self.flag_c=bool(c); return 4
        if op==0x5B: d=self.fetch();a=self.dp((d+self.x)&0xFF);v=self.read(a);c=v&1;self.write(a,self._nz(v>>1));self.flag_c=bool(c); return 5
        if op==0x4C: a=self.fetch16();v=self.read(a);c=v&1;self.write(a,self._nz(v>>1));self.flag_c=bool(c); return 5
        if op==0x3C: c=int(self.flag_c);nc=(self.a>>7)&1;self.a=self._nz(((self.a<<1)|c)&0xFF);self.flag_c=bool(nc); return 2
        if op==0x2B: d=self.fetch();a=self.dp(d);v=self.read(a);c=int(self.flag_c);nc=(v>>7)&1;self.write(a,self._nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc); return 4
        if op==0x3B: d=self.fetch();a=self.dp((d+self.x)&0xFF);v=self.read(a);c=int(self.flag_c);nc=(v>>7)&1;self.write(a,self._nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc); return 5
        if op==0x2C: a=self.fetch16();v=self.read(a);c=int(self.flag_c);nc=(v>>7)&1;self.write(a,self._nz(((v<<1)|c)&0xFF));self.flag_c=bool(nc); return 5
        if op==0x7C: c=int(self.flag_c);nc=self.a&1;self.a=self._nz((self.a>>1)|(c<<7));self.flag_c=bool(nc); return 2
        if op==0x6B: d=self.fetch();a=self.dp(d);v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self._nz((v>>1)|(c<<7)));self.flag_c=bool(nc); return 4
        if op==0x7B: d=self.fetch();a=self.dp((d+self.x)&0xFF);v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self._nz((v>>1)|(c<<7)));self.flag_c=bool(nc); return 5
        if op==0x6C: a=self.fetch16();v=self.read(a);c=int(self.flag_c);nc=v&1;self.write(a,self._nz((v>>1)|(c<<7)));self.flag_c=bool(nc); return 5
        if op==0x2F: r=self.fetch();r=r-256 if r>=128 else r;self.pc=(self.pc+r)&0xFFFF; return 4
        for bop,cfn in [(0xF0,lambda:self.flag_z),(0xD0,lambda:not self.flag_z),(0xB0,lambda:self.flag_c),(0x90,lambda:not self.flag_c),(0x70,lambda:self.flag_v),(0x50,lambda:not self.flag_v),(0x30,lambda:self.flag_n),(0x10,lambda:not self.flag_n)]:
            if op==bop:
                r=self.fetch();r=r-256 if r>=128 else r;t=cfn()
                if t: self.pc=(self.pc+r)&0xFFFF
                return 4 if t else 2
        if op==0x2E:
            d=self.fetch();r=self.fetch();r=r-256 if r>=128 else r
            if self.a!=self.read(self.dp(d)): self.pc=(self.pc+r)&0xFFFF
            return 5
        if op==0xDE:
            d=self.fetch();r=self.fetch();r=r-256 if r>=128 else r
            if self.a!=self.read(self.dp((d+self.x)&0xFF)): self.pc=(self.pc+r)&0xFFFF
            return 6
        if op==0x6E:
            d=self.fetch();r=self.fetch();r=r-256 if r>=128 else r
            a=self.dp(d);v=(self.read(a)-1)&0xFF;self.write(a,v)
            if v: self.pc=(self.pc+r)&0xFFFF
            return 5
        if op==0xFE:
            r=self.fetch();r=r-256 if r>=128 else r;self.y=(self.y-1)&0xFF
            if self.y: self.pc=(self.pc+r)&0xFFFF
            return 4
        if op==0x5F: self.pc=self.fetch16(); return 3
        if op==0x1F: a=self.fetch16();self.pc=self.read16((a+self.x)&0xFFFF); return 6
        if op==0x3F: a=self.fetch16();self.push16(self.pc);self.pc=a; return 8
        if op==0x6F: self.pc=self.pop16(); return 5
        if op==0x7F: self._unpack_psw(self.pop());self.pc=self.pop16(); return 6
        if op==0x4F: n=self.fetch();self.push16(self.pc);self.pc=0xFF00|n; return 6
        if (op&0x0F)==0x01: n=(op>>4)&0xF;self.push16(self.pc);self.pc=self.read16(0xFFDE-n*2); return 8
        if op==0x60: self.flag_c=False; return 2
        if op==0x80: self.flag_c=True; return 2
        if op==0xED: self.flag_c=not self.flag_c; return 3
        if op==0xE0: self.flag_v=False;self.flag_h=False; return 2
        if op==0x20: self.flag_p=False; return 2
        if op==0x40: self.flag_p=True; return 2
        if op==0xA0: self.flag_i=True; return 3
        if op==0xC0: self.flag_i=False; return 3
        if (op&0x0F)==0x02: bit=(op>>5)&7;d=self.fetch();a=self.dp(d);self.write(a,self.read(a)|(1<<bit)); return 4
        if (op&0x0F)==0x12: bit=(op>>5)&7;d=self.fetch();a=self.dp(d);self.write(a,self.read(a)&~(1<<bit)); return 4
        if (op&0x0F)==0x03:
            bit=(op>>5)&7;d=self.fetch();r=self.fetch();r=r-256 if r>=128 else r
            if self.read(self.dp(d))&(1<<bit): self.pc=(self.pc+r)&0xFFFF
            return 5
        if (op&0x0F)==0x13:
            bit=(op>>5)&7;d=self.fetch();r=self.fetch();r=r-256 if r>=128 else r
            if not(self.read(self.dp(d))&(1<<bit)): self.pc=(self.pc+r)&0xFFFF
            return 5
        if op==0x7A:
            d=self.fetch();mem=self.read(self.dp(d))|(self.read(self.dp((d+1)&0xFF))<<8)
            ya=self.a|(self.y<<8);r=ya+mem;self.flag_c=r>0xFFFF;r&=0xFFFF
            self.a=r&0xFF;self.y=(r>>8)&0xFF;self._nz16(r); return 5
        if op==0x9A:
            d=self.fetch();mem=self.read(self.dp(d))|(self.read(self.dp((d+1)&0xFF))<<8)
            ya=self.a|(self.y<<8);r=ya-mem;self.flag_c=r>=0;r&=0xFFFF
            self.a=r&0xFF;self.y=(r>>8)&0xFF;self._nz16(r); return 5
        if op==0x5A:
            d=self.fetch();mem=self.read(self.dp(d))|(self.read(self.dp((d+1)&0xFF))<<8)
            ya=self.a|(self.y<<8);r=ya-mem;self.flag_c=r>=0;self._nz16(r&0xFFFF); return 4
        if op==0x3A:
            d=self.fetch();v=self.read(self.dp(d))|(self.read(self.dp((d+1)&0xFF))<<8);v=(v+1)&0xFFFF
            self.write(self.dp(d),v&0xFF);self.write(self.dp((d+1)&0xFF),(v>>8)&0xFF);self._nz16(v); return 6
        if op==0x1A:
            d=self.fetch();v=self.read(self.dp(d))|(self.read(self.dp((d+1)&0xFF))<<8);v=(v-1)&0xFFFF
            self.write(self.dp(d),v&0xFF);self.write(self.dp((d+1)&0xFF),(v>>8)&0xFF);self._nz16(v); return 6
        if op==0xCF: ya=self.y*self.a;self.a=ya&0xFF;self.y=(ya>>8)&0xFF;self._nz(self.y); return 9
        if op==0x9E:
            ya=self.a|(self.y<<8)
            if self.x: self.a=(ya//self.x)&0xFF;self.y=(ya%self.x)&0xFF
            else: self.a=0xFF;self.y=0xFF
            self._nz(self.a); return 12
        if op==0xDF:
            if self.flag_c or self.a>0x99: self.a=(self.a+0x60)&0xFF;self.flag_c=True
            if self.flag_h or (self.a&0xF)>9: self.a=(self.a+6)&0xFF
            self._nz(self.a); return 3
        if op==0xBE:
            if not self.flag_c or self.a>0x99: self.a=(self.a-0x60)&0xFF;self.flag_c=False
            if not self.flag_h or (self.a&0xF)>9: self.a=(self.a-6)&0xFF
            self._nz(self.a); return 3
        if op==0x9F: self.a=((self.a>>4)|(self.a<<4))&0xFF;self._nz(self.a); return 5
        if op==0x4E: a=self.fetch16();v=self.read(a);self._nz(self.a-v);self.write(a,v&~self.a); return 6
        if op==0x0E: a=self.fetch16();v=self.read(a);self._nz(self.a-v);self.write(a,v|self.a); return 6
        if op in (0x4A,0x6A,0x0A,0x2A,0x8A):
            a=self.fetch16();bit=(a>>13)&7;a&=0x1FFF;bv=bool(self.read(a)&(1<<bit))
            if op==0x4A: self.flag_c=self.flag_c and bv
            elif op==0x6A: self.flag_c=self.flag_c and not bv
            elif op==0x0A: self.flag_c=self.flag_c or bv
            elif op==0x2A: self.flag_c=self.flag_c or not bv
            elif op==0x8A: self.flag_c=self.flag_c!=bv
            return 5
        if op==0xEA: a=self.fetch16();bit=(a>>13)&7;a&=0x1FFF;self.write(a,self.read(a)^(1<<bit)); return 5
        if op==0xCA:
            a=self.fetch16();bit=(a>>13)&7;a&=0x1FFF;v=self.read(a)
            self.write(a,(v|(1<<bit)) if self.flag_c else (v&~(1<<bit))); return 6
        if op==0xAA: a=self.fetch16();bit=(a>>13)&7;a&=0x1FFF;self.flag_c=bool(self.read(a)&(1<<bit)); return 4
        if op==0x2D: self.push(self.a); return 4
        if op==0x4D: self.push(self.x); return 4
        if op==0x6D: self.push(self.y); return 4
        if op==0x0D: self.push(self._pack_psw()); return 4
        if op==0xAE: self.a=self.pop(); return 4
        if op==0xCE: self.x=self.pop(); return 4
        if op==0xEE: self.y=self.pop(); return 4
        if op==0x8E: self._unpack_psw(self.pop()); return 4
        if op==0x00: return 2
        if op==0xEF: self.pc=(self.pc-1)&0xFFFF; return 3
        if op==0xFF: self.pc=(self.pc-1)&0xFFFF; return 3
        return 2


class TempoDetector:
    @staticmethod
    def detect_tick_interval(kon_cycles,cpu_clock=1024000.0):
        if len(kon_cycles)<3: return cpu_clock/60.0
        deltas=sorted(kon_cycles[i]-kon_cycles[i-1] for i in range(1,len(kon_cycles)) if kon_cycles[i]>kon_cycles[i-1])
        if not deltas: return cpu_clock/60.0
        mn=cpu_clock/500.0;mx=cpu_clock/5.0
        filtered=[d for d in deltas if mn<d<mx] or deltas
        candidate=min(filtered);best_gcd=candidate;best_score=0
        for div in range(1,5):
            test=candidate/div
            if test<mn: break
            score=sum(1 for v in filtered if min(v%test,test-v%test)/test<0.15)
            if score>best_score: best_score=score;best_gcd=test
        return best_gcd
    @staticmethod
    def detect_from_timer_reads(timer_reads,timer_targets,cpu_clock=1024000.0):
        if not timer_reads: return None
        by_timer={0:[],1:[],2:[]}
        for c,t in timer_reads: by_timer[t].append(c)
        most_used=max(by_timer.keys(),key=lambda t:len(by_timer[t]))
        reads=by_timer[most_used]
        if len(reads)<3: return None
        intervals=sorted(reads[i]-reads[i-1] for i in range(1,min(len(reads),200)) if reads[i]>reads[i-1])
        return intervals[len(intervals)//2] if intervals else None
    @staticmethod
    def compute_bpm_speed(tick_cycles,cpu_clock=1024000.0):
        tick_seconds=tick_cycles/cpu_clock
        if tick_seconds<=0: return 150,6
        for speed in [1,2,3,4,6]:
            bpm=2.5/(tick_seconds*speed);bi=int(round(bpm))
            if 32<=bi<=255: return bi,speed
        return max(32,min(255,int(round(2.5/tick_seconds)))),1


class DSPAnalyzer:
    def __init__(self,dsp_writes,initial_dsp,cycles_per_row):
        self.dsp_writes=dsp_writes;self.dsp=bytearray(initial_dsp)
        self.cycles_per_row=cycles_per_row
    def analyze(self):
        events=[];voice_on=[False]*8
        for cycle,addr,val in self.dsp_writes:
            self.dsp[addr&0x7F]=val
            row=int(round(cycle/self.cycles_per_row))
            if addr==DSPAddr.KON and val:
                for v in range(8):
                    if not(val&(1<<v)): continue
                    pl=self.dsp[v*0x10+2];ph=self.dsp[v*0x10+3]&0x3F
                    pitch=pl|(ph<<8);source=self.dsp[v*0x10+4]
                    vl=self.dsp[v*0x10];vr=self.dsp[v*0x10+1]
                    if vl>127: vl=256-vl
                    if vr>127: vr=256-vr
                    volume=min(64,max(vl,vr))
                    note=pitch_to_xm_note(pitch)
                    if note and 1<=note<=96:
                        if voice_on[v]:
                            events.append(NoteEvent(tick=row,channel=v,event_type='note_off',note=97))
                        events.append(NoteEvent(tick=row,channel=v,event_type='note_on',
                            note=note,instrument=source+1,volume=max(1,volume),pitch=pitch))
                        voice_on[v]=True
            elif addr==DSPAddr.KOFF and val:
                for v in range(8):
                    if (val&(1<<v)) and voice_on[v]:
                        events.append(NoteEvent(tick=row,channel=v,event_type='note_off',note=97))
                        voice_on[v]=False
        return self._deduplicate(events)
    @staticmethod
    def _deduplicate(events):
        seen={};result=[]
        for ev in events:
            key=(ev.tick,ev.channel)
            if ev.event_type=='note_on':
                result=[e for e in result if not(e.tick==ev.tick and e.channel==ev.channel and e.event_type=='note_on')]
                seen[key]=ev;result.append(ev)
            elif ev.event_type=='note_off':
                if key not in seen or seen[key].event_type!='note_on':
                    result.append(ev);seen[key]=ev
        return result


# ═══════════════════════════════════════════════════════════════
# MIDI Writer — без внешних зависимостей
# ═══════════════════════════════════════════════════════════════

class MIDIWriter:
    """Запись MIDI файла (формат 1) без внешних библиотек"""

    # GM инструменты для маппинга SPC источников
    GM_PROGRAMS = [
        0,   # Piano
        25,  # Steel Guitar
        33,  # Finger Bass
        48,  # String Ensemble
        80,  # Square Lead
        81,  # Sawtooth Lead
        73,  # Flute
        56,  # Trumpet
    ]

    def __init__(self):
        self.tracks = []

    def build(self, events, tick_cycles, cpu_clock=1024000.0,
              bpm=120, transpose=0, octave=0, title=""):
        """
        events: список NoteEvent
        tick_cycles: CPU циклов на одну строку (row)
        """
        total_shift = transpose + octave * 12
        row_seconds = tick_cycles / cpu_clock

        # MIDI ticks per quarter note
        tpqn = 480

        # Beats per second
        bps = bpm / 60.0

        # Seconds per tick
        spt = 1.0 / (bps * tpqn)

        # Row to MIDI ticks
        row_to_ticks = row_seconds / spt

        # Собираем ноты по каналам
        channels = {}
        for ev in events:
            if ev.channel not in channels:
                channels[ev.channel] = []
            channels[ev.channel].append(ev)

        self.tracks = []
        self.tpqn = tpqn
        self.bpm = bpm
        self.title = title

        # Track 0: tempo track
        tempo_track = []
        # Set tempo (microseconds per quarter note)
        usec_per_qn = int(60_000_000 / bpm)
        tempo_track.append((0, self._tempo_event(usec_per_qn)))
        if title:
            tempo_track.append((0, self._text_event(title)))
        self.tracks.append(tempo_track)

        # One track per channel
        for ch_idx, ch in enumerate(sorted(channels.keys())):
            if ch_idx >= 15:
                break  # MIDI max 16 channels (skip 9=drums)

            midi_ch = ch_idx if ch_idx < 9 else ch_idx + 1  # skip drum channel 9
            track_events = []

            # Program change
            gm_prog = self.GM_PROGRAMS[ch_idx % len(self.GM_PROGRAMS)]
            track_events.append((0, bytes([0xC0 | midi_ch, gm_prog])))

            # Track name
            track_events.append((0, self._track_name_event(f"Channel {ch}")))

            # Convert events
            note_on_map = {}  # track active notes for duration calculation

            for ev in channels[ch]:
                midi_tick = int(ev.tick * row_to_ticks)

                if ev.event_type == 'note_on':
                    midi_note = xm_note_to_midi(ev.note) + total_shift
                    midi_note = max(0, min(127, midi_note))
                    velocity = max(1, min(127, ev.volume * 2))

                    # Close previous note on same channel if any
                    if ch in note_on_map:
                        prev_note, prev_tick = note_on_map[ch]
                        track_events.append((midi_tick, bytes([0x80 | midi_ch, prev_note, 0])))

                    track_events.append((midi_tick, bytes([0x90 | midi_ch, midi_note, velocity])))
                    note_on_map[ch] = (midi_note, midi_tick)

                elif ev.event_type == 'note_off':
                    if ch in note_on_map:
                        prev_note, _ = note_on_map[ch]
                        track_events.append((midi_tick, bytes([0x80 | midi_ch, prev_note, 0])))
                        del note_on_map[ch]

            # Close any remaining notes
            if ch in note_on_map:
                prev_note, prev_tick = note_on_map[ch]
                last_tick = prev_tick + int(row_to_ticks * 4)
                track_events.append((last_tick, bytes([0x80 | midi_ch, prev_note, 0])))

            self.tracks.append(track_events)

    def write(self, filename):
        with open(filename, 'wb') as f:
            # Header
            num_tracks = len(self.tracks)
            f.write(b'MThd')
            f.write(struct.pack('>I', 6))       # header length
            f.write(struct.pack('>H', 1))       # format 1
            f.write(struct.pack('>H', num_tracks))
            f.write(struct.pack('>H', self.tpqn))

            # Tracks
            for track_events in self.tracks:
                track_data = self._encode_track(track_events)
                f.write(b'MTrk')
                f.write(struct.pack('>I', len(track_data)))
                f.write(track_data)

    def _encode_track(self, events):
        # Sort by absolute tick
        events.sort(key=lambda x: x[0])

        data = bytearray()
        prev_tick = 0

        for abs_tick, event_data in events:
            delta = max(0, abs_tick - prev_tick)
            data.extend(self._encode_vlq(delta))
            data.extend(event_data)
            prev_tick = abs_tick

        # End of track
        data.extend(self._encode_vlq(0))
        data.extend(b'\xFF\x2F\x00')

        return bytes(data)

    @staticmethod
    def _encode_vlq(value):
        """Encode variable-length quantity"""
        if value < 0:
            value = 0
        result = []
        result.append(value & 0x7F)
        value >>= 7
        while value:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.reverse()
        return bytes(result)

    @staticmethod
    def _tempo_event(usec_per_qn):
        return bytes([0xFF, 0x51, 0x03,
                      (usec_per_qn >> 16) & 0xFF,
                      (usec_per_qn >> 8) & 0xFF,
                      usec_per_qn & 0xFF])

    @staticmethod
    def _text_event(text):
        data = text.encode('ascii', errors='replace')[:127]
        return bytes([0xFF, 0x01, len(data)]) + data

    @staticmethod
    def _track_name_event(name):
        data = name.encode('ascii', errors='replace')[:127]
        return bytes([0xFF, 0x03, len(data)]) + data


# ═══════════════════════════════════════════════════════════════
# XM Writer (из v8, без изменений кроме compact)
# ═══════════════════════════════════════════════════════════════

def detect_sample_pitch(pcm_data, sample_rate=32000):
    """
    Определение базовой частоты BRR-сэмпла через автокорреляцию.
    Возвращает частоту в Hz или None если не удалось определить.
    
    BRR-сэмплы воспроизводятся SNES DSP с частотой ~32000 Hz
    при pitch=0x1000.
    """
    if not pcm_data or len(pcm_data) < 32:
        return None
    
    # Берём начальный участок (до 2048 сэмплов)
    data = pcm_data[:min(len(pcm_data), 2048)]
    n = len(data)
    
    # Минимальный и максимальный период для поиска
    # Частоты от ~50 Hz до ~4000 Hz
    min_lag = max(8, sample_rate // 4000)   # ~4000 Hz
    max_lag = min(n // 2, sample_rate // 50) # ~50 Hz
    
    if min_lag >= max_lag:
        return None
    
    # Нормализованная автокорреляция
    best_lag = 0
    best_corr = -1.0
    
    # Энергия сигнала
    energy = sum(s * s for s in data[:max_lag])
    if energy < 1:
        return None
    
    for lag in range(min_lag, max_lag):
        corr = 0.0
        energy_lag = 0.0
        for i in range(n - lag):
            corr += data[i] * data[i + lag]
            energy_lag += data[i + lag] * data[i + lag]
        
        if energy_lag < 1:
            continue
        
        # Нормализация
        norm_corr = corr / math.sqrt(energy * energy_lag)
        
        if norm_corr > best_corr:
            best_corr = norm_corr
            best_lag = lag
    
    # Порог: корреляция должна быть достаточно высокой
    if best_corr < 0.5 or best_lag <= 0:
        return None
    
    freq = sample_rate / best_lag
    return freq


def compute_sample_tuning(pcm_data, sample_rate=32000):
    """
    Вычисляет relative_note и finetune для сэмпла,
    чтобы нота C-4 в XM воспроизводила правильную частоту.
    
    XM ожидает что сэмпл с relative_note=0, finetune=0
    при нотe C-4 воспроизводится с sample rate 8363 Hz.
    Один период волны = один цикл.
    
    Для BRR-сэмплов SNES:
    - DSP воспроизводит с rate = pitch * 32000 / 4096
    - При pitch = 0x03A2 (A-4) это ~7260 Hz playback rate
    - Если сэмпл содержит один период 440Hz волны в N сэмплах,
      то N = 32000 / 440 ≈ 72.7 сэмпла
    
    Для XM:
    - При C-4, sample rate 8363, период C-4 (261.63Hz) = 8363/261.63 ≈ 32 сэмпла
    - relative_note сдвигает на полутоны
    - finetune сдвигает на 1/128 полутона
    
    Возвращает (relative_note, finetune)
    """
    freq = detect_sample_pitch(pcm_data, sample_rate)
    
    if freq is None or freq <= 0:
        return 0, 0
    
    # Период волны в сэмплах при sample_rate
    period_samples = sample_rate / freq
    
    # В XM: C-4 при rate 8363 Hz и relative_note=0
    # Частота ноты C-4 = 8363 / period_samples_in_xm
    # Мы хотим чтобы в XM нота соответствовала реальному звуку
    
    # Реальная нота этого сэмпла при воспроизведении 1:1 (pitch=0x1000 в SNES):
    # freq_at_native = sample_rate / period_samples = freq
    
    # В XM, C-4 = 261.63 Hz, при sample rate 8363
    # Фактическая частота сэмпла с периодом period_samples при XM rate 8363:
    xm_freq_at_c4 = 8363.0 / period_samples
    
    # Разница в полутонах между тем что XM воспроизведёт и тем что должно быть
    # Целевая нота: чтобы при pitch 0x03A2 (A-4, 440Hz) в SNES
    # мы слышали правильную частоту
    # 
    # SNES pitch 0x03A2 → playback rate = 0x03A2 * 32000 / 4096 ≈ 7259 Hz
    # При этом rate, частота звука = playback_rate / period_samples
    # = 7259 / (32000/freq) = 7259 * freq / 32000
    #
    # В XM мы маппим pitch 0x03A2 → note A-4 (note 58)
    # XM воспроизводит A-4 с rate = 8363 * 2^((58-49)/12) ≈ 8363 * 1.6818 ≈ 14062
    # (это internal rate, не sample rate)
    # Частота звука в XM = xm_rate / period_in_xm_samples
    
    # Проще: определяем на сколько полутонов сэмпл "расстроен"
    # относительно стандартной частоты
    
    # Если бы это был идеальный сэмпл с периодом ровно для C-4:
    # period_ideal = sample_rate / 261.63
    # Наш сэмпл: period_actual = sample_rate / freq
    # Разница: freq / 261.63 = соотношение
    # Полутоны: 12 * log2(freq / 261.63)
    
    c4_freq = 261.63
    semitones_off = 12.0 * math.log2(freq / c4_freq)
    
    # relative_note компенсирует целые полутоны
    rel_note = -int(round(semitones_off))
    
    # finetune компенсирует остаток
    remainder = semitones_off + rel_note  # остаток в полутонах
    fine = int(round(remainder * 128))    # 128 = 1 полутон
    fine = max(-128, min(127, fine))
    
    return rel_note, fine


# ═══════════════════════════════════════════════════════════════
# Обновлённый XMWriter._build_instruments
# ═══════════════════════════════════════════════════════════════

class XMWriter:
    def __init__(self):
        self.title="";self.num_channels=8;self.bpm=150;self.speed=6
        self.patterns=[];self.instruments=[];self.order=[]
        self.source_to_instrument={}

    def build(self, events, brr_samples, title="", bpm=150, speed=6,
              transpose=0, octave=0, finetune=0, clean_samples=False, compact=1):
        self.title = title[:20]
        self.speed = speed
        total_transpose = transpose + octave * 12

        if clean_samples:
            used_sources = set(ev.instrument - 1 for ev in events if ev.event_type == 'note_on')
            brr_samples = [s for s in brr_samples if s.source_index in used_sources]

        if compact > 1:
            events = self._compact_events(events, compact)
            bpm = max(32, bpm // compact)

        self.bpm = bpm
        self._build_instruments(brr_samples, total_transpose, finetune)
        self._build_patterns(events)

    def _build_instruments(self, brr_samples, transpose=0, finetune_override=0):
        self.instruments = []
        self.source_to_instrument = {}

        for i, brr in enumerate(brr_samples):
            pcm = brr.pcm_data or [0] * 16
            mx = max(abs(s) for s in pcm) or 1
            norm = [max(-32768, min(32767, int(s * 32767 / mx))) for s in pcm]

            # Loop
            ls = 0
            ll = 0
            if brr.has_loop and len(norm) > 16:
                bb = max(0, (brr.loop_address - brr.start_address) // 9)
                ls = bb * 16
                if ls >= len(norm):
                    ls = 0
                ll = len(norm) - ls
                if ll <= 0:
                    ls = 0
                    ll = 0

            # Автоматическая коррекция тональности для каждого сэмпла
            auto_rel, auto_fine = compute_sample_tuning(pcm)

            # Добавляем пользовательский transpose
            total_rel = auto_rel + transpose
            total_rel = max(-128, min(127, total_rel))

            # Finetune: автоматический + пользовательский override
            if finetune_override != 0:
                total_fine = finetune_override
            else:
                total_fine = auto_fine
            total_fine = max(-128, min(127, total_fine))

            self.instruments.append({
                'name': brr.name[:22],
                'samples': [{
                    'name': brr.name[:22],
                    'data': norm,
                    'length': len(norm),
                    'loop_start': ls,
                    'loop_length': ll,
                    'loop_type': 1 if ll > 0 else 0,
                    'volume': 64,
                    'finetune': total_fine,
                    'panning': 128,
                    'relative_note': total_rel,
                    'bits': 16,
                }],
                'source_index': brr.source_index,
                '_auto_tuning': (auto_rel, auto_fine),  # для отладки
            })
            self.source_to_instrument[brr.source_index] = i + 1

    # ... остальные методы без изменений (compact, patterns, write, etc.)
    # Копируются из предыдущей версии как есть.

    @staticmethod
    def _compact_events(events, factor):
        if factor <= 1: return events
        compacted = [NoteEvent(tick=ev.tick // factor, channel=ev.channel,
                               event_type=ev.event_type, note=ev.note,
                               instrument=ev.instrument, volume=ev.volume,
                               pitch=ev.pitch) for ev in events]
        occupied = {}; resolved = []
        for ev in compacted:
            row = ev.tick
            if ev.event_type == 'note_on':
                while (row, ev.channel) in occupied:
                    row += 1
                ev.tick = row
                occupied[(row, ev.channel)] = True
            elif ev.event_type == 'note_off':
                if (row, ev.channel) in occupied:
                    row += 1
                ev.tick = row
            resolved.append(ev)
        return resolved

    def _build_patterns(self, events):
        rpp = 64
        if not events:
            self.patterns = [[[TrackerNote() for _ in range(8)] for _ in range(rpp)]]
            self.order = [0]
            return
        max_tick = max(e.tick for e in events)
        np_ = max(1, min((max_tick + rpp) // rpp, 256))
        self.patterns = [[[TrackerNote() for _ in range(8)] for _ in range(rpp)] for _ in range(np_)]
        for ev in events:
            if ev.channel >= 8: continue
            pi = ev.tick // rpp
            ri = ev.tick % rpp
            if pi >= len(self.patterns): continue
            nd = self.patterns[pi][ri][ev.channel]
            if ev.event_type == 'note_on':
                nd.note = max(1, min(96, ev.note))
                inst = self.source_to_instrument.get(ev.instrument - 1, 0)
                if inst == 0 and self.instruments: inst = 1
                nd.instrument = min(inst, 128)
                nd.volume = 0x10 + min(0x40, max(0, ev.volume))
            elif ev.event_type == 'note_off':
                if nd.note == 0:
                    nd.note = 97
        self.order = list(range(np_))

    def write(self, filename):
        with open(filename, 'wb') as f:
            self._wh(f)
            for p in self.patterns: self._wp(f, p)
            for inst in self.instruments[:128]: self._wi(f, inst)

    def _wh(self, f):
        f.write(b'Extended Module: ')
        f.write(self.title.encode('ascii','replace')[:20].ljust(20, b'\x00'))
        f.write(b'\x1a')
        f.write(b'SPC2XM Converter    ')
        f.write(struct.pack('<H', 0x0104))
        f.write(struct.pack('<I', 276))
        f.write(struct.pack('<H', len(self.order)))
        f.write(struct.pack('<H', 0))
        f.write(struct.pack('<H', 8))
        f.write(struct.pack('<H', len(self.patterns)))
        f.write(struct.pack('<H', min(len(self.instruments), 128)))
        f.write(struct.pack('<H', 1))
        f.write(struct.pack('<H', self.speed))
        f.write(struct.pack('<H', self.bpm))
        ot = bytearray(256)
        for i, o in enumerate(self.order[:256]): ot[i] = o
        f.write(ot)

    def _wp(self, f, pattern):
        packed = bytearray()
        for row in pattern:
            for ch in range(8):
                nd = row[ch] if ch < len(row) else TrackerNote()
                hn=nd.note>0;hi=nd.instrument>0;hv=nd.volume>0;hf=nd.effect>0;hp=nd.effect_param>0
                if not(hn or hi or hv or hf or hp):
                    packed.append(0x80)
                else:
                    pb=0x80
                    if hn:pb|=1
                    if hi:pb|=2
                    if hv:pb|=4
                    if hf:pb|=8
                    if hp:pb|=16
                    packed.append(pb)
                    if hn:packed.append(nd.note&0xFF)
                    if hi:packed.append(nd.instrument&0xFF)
                    if hv:packed.append(nd.volume&0xFF)
                    if hf:packed.append(nd.effect&0xFF)
                    if hp:packed.append(nd.effect_param&0xFF)
        f.write(struct.pack('<I', 9))
        f.write(struct.pack('<B', 0))
        f.write(struct.pack('<H', len(pattern)))
        f.write(struct.pack('<H', len(packed)))
        f.write(packed)

    def _wi(self, f, inst):
        samples = inst.get('samples', [])
        if not samples:
            f.write(struct.pack('<I', 29))
            f.write(inst['name'].encode('ascii','replace')[:22].ljust(22, b'\x00'))
            f.write(struct.pack('<BH', 0, 0))
            return
        ihs = 263
        f.write(struct.pack('<I', ihs))
        f.write(inst['name'].encode('ascii','replace')[:22].ljust(22, b'\x00'))
        f.write(struct.pack('<B', 0))
        f.write(struct.pack('<H', len(samples)))
        f.write(struct.pack('<I', 40))
        f.write(bytearray(96))
        ve = bytearray(48)
        struct.pack_into('<HH', ve, 0, 0, 64)
        struct.pack_into('<HH', ve, 4, 100, 64)
        f.write(ve)
        pe = bytearray(48)
        struct.pack_into('<HH', pe, 0, 0, 32)
        struct.pack_into('<HH', pe, 4, 100, 32)
        f.write(pe)
        f.write(bytes([2,2,0,0,1,0,0,1,1,0,0,0,0,0]))
        f.write(struct.pack('<H', 0x800))
        rem = ihs - (4+22+1+2+4+96+48+48+14+2)
        if rem > 0:
            f.write(b'\x00' * rem)
        for s in samples:
            self._wsh(f, s)
        for s in samples:
            self._wsd(f, s)

    def _wsh(self, f, s):
        bits = s.get('bits', 16)
        bps = 2 if bits == 16 else 1
        f.write(struct.pack('<I', len(s['data']) * bps))
        f.write(struct.pack('<I', s['loop_start'] * bps))
        f.write(struct.pack('<I', s['loop_length'] * bps))
        f.write(struct.pack('<B', s.get('volume', 64)))
        f.write(struct.pack('<b', max(-128, min(127, s.get('finetune', 0)))))
        tb = s.get('loop_type', 0) & 3
        if bits == 16: tb |= 0x10
        f.write(struct.pack('<B', tb))
        f.write(struct.pack('<B', s.get('panning', 128)))
        f.write(struct.pack('<b', max(-128, min(127, s.get('relative_note', 0)))))
        f.write(struct.pack('<B', 0))
        f.write(s.get('name','').encode('ascii','replace')[:22].ljust(22, b'\x00'))

    def _wsd(self, f, s):
        prev = 0
        for v in s['data']:
            v = max(-32768, min(32767, v))
            d = ((v - prev) + 32768) % 65536 - 32768
            f.write(struct.pack('<h', d))
            prev = v


# ═══════════════════════════════════════════════════════════════
# SPC Parser
# ═══════════════════════════════════════════════════════════════
class SPCParser:
    def __init__(self, filename):
        with open(filename, 'rb') as f:
            data = f.read()
        if len(data) < 0x10200:
            raise ValueError("Too small")
        if b'SNES-SPC700' not in data[:33]:
            raise ValueError("Not SPC")

        self.header = SPCHeader()
        self.header.has_id666 = data[0x23] == 0x1A
        self.header.pc = struct.unpack_from('<H', data, 0x25)[0]
        self.header.a = data[0x27]
        self.header.x = data[0x28]
        self.header.y = data[0x29]
        self.header.psw = data[0x2A]
        self.header.sp = data[0x2B]

        if self.header.has_id666:
            # Заголовок ID666 бывает в двух форматах: текстовом и бинарном
            # Определяем формат по содержимому
            self._parse_id666(data)

        self.ram = bytearray(data[0x100:0x10100])
        self.dsp_regs = bytearray(data[0x10100:0x10180])

    def _parse_id666(self, data):
        """Парсинг ID666 тегов — поддержка текстового и бинарного форматов"""

        # Текстовые поля (всегда одинаковые)
        self.header.title = data[0x2E:0x4E].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        self.header.game = data[0x4E:0x6E].split(b'\x00')[0].decode('ascii', errors='replace').strip()

        # Определяем формат: текстовый или бинарный
        # Эвристика: если байт 0xD2 содержит дату в формате "xx/xx/xxxx" — текстовый
        # Если байты 0xA9-0xAB похожи на число секунд — бинарный
        # Простой способ: пробуем оба и выбираем осмысленный

        # Попытка 1: текстовый формат (наиболее распространённый)
        dumper_text = data[0x6E:0x7E].split(b'\x00')[0].decode('ascii', errors='replace').strip()

        # Дата дампа: 0x9E-0xA8 (11 байт, текст "mm/dd/yyyy")
        # Длительность: 0xA9-0xAB (3 байта, текст секунд, напр. "180")
        # Fade: 0xAC-0xB0 (5 байт, текст мс, напр. "10000")

        dur_bytes = data[0xA9:0xAC]
        fade_bytes = data[0xAC:0xB1]

        # Пробуем текстовый формат
        dur_text = dur_bytes.split(b'\x00')[0].decode('ascii', errors='replace').strip()
        fade_text = fade_bytes.split(b'\x00')[0].decode('ascii', errors='replace').strip()

        duration = 0
        fade = 0

        # Текстовый формат
        try:
            if dur_text and dur_text.isdigit():
                duration = int(dur_text)
        except (ValueError, OverflowError):
            pass

        try:
            if fade_text and fade_text.isdigit():
                fade = int(fade_text)
        except (ValueError, OverflowError):
            pass

        # Если текстовый не дал результата, пробуем бинарный
        if duration == 0:
            # Бинарный формат:
            # Длительность: 0xA9-0xAB — 3 байта little-endian (секунды)
            # Fade: 0xAC-0xAF — 4 байта little-endian (мс)
            try:
                duration = dur_bytes[0] | (dur_bytes[1] << 8) | (dur_bytes[2] << 16)
                if duration > 86400:  # больше суток — явно мусор
                    duration = 0
            except (IndexError, OverflowError):
                duration = 0

            try:
                fade = fade_bytes[0] | (fade_bytes[1] << 8) | \
                       (fade_bytes[2] << 16) | (fade_bytes[3] << 24)
                if fade > 600000:  # больше 10 минут — мусор
                    fade = 0
            except (IndexError, OverflowError):
                fade = 0

        # Также пробуем поле артиста
        # В текстовом формате: 0xB1-0xD0 (32 байта)
        try:
            self.header.artist = data[0xB1:0xD1].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        except:
            self.header.artist = ""

        self.header.duration_sec = duration
        self.header.fade_ms = fade

    def v_reg(self, v, r):
        a = v * 0x10 + r
        return self.dsp_regs[a] if a < 128 else 0

    def g_reg(self, r):
        return self.dsp_regs[r] if r < 128 else 0

    def extract_samples(self):
        dp = self.g_reg(DSPAddr.DIR) << 8
        samples = []
        used = set()
        seen = set()
        for v in range(8):
            used.add(self.v_reg(v, DSPAddr.SRCN))
        for src in range(256):
            entry = dp + src * 4
            if entry + 4 > len(self.ram):
                continue
            start = struct.unpack_from('<H', self.ram, entry)[0]
            loop = struct.unpack_from('<H', self.ram, entry + 2)[0]
            if start == 0 and src not in used:
                continue
            if start >= len(self.ram) or start + 9 > len(self.ram):
                continue
            if start in seen and src not in used:
                continue
            seen.add(start)
            pcm, hl = BRRDecoder.decode_sample(self.ram, start)
            if max(abs(s) for s in pcm) < 8:
                continue
            samples.append(BRRSample(src, start, loop, hl, pcm, f"Sample_{src:02X}"))
        return samples

    def get_duration(self):
        """Возвращает рекомендуемую длительность эмуляции в секундах"""
        dur = self.header.duration_sec
        fade = self.header.fade_ms / 1000.0 if self.header.fade_ms else 0

        if dur > 0:
            # Длительность трека + fade
            return dur + fade
        else:
            # Нет информации — дефолт
            return 0


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_spc(input_file, output_file=None, duration_override=None,
                transpose=0, octave=0, finetune=0,
                bpm_override=None, speed_override=None,
                clean_samples=False, compact=1,
                output_format='xm', midi_file=None):

    base = os.path.splitext(input_file)[0]
    if not output_file:
        output_file = base + '.xm'

    print(f"Конвертация: {input_file}")
    parser = SPCParser(input_file)
    print(f"  Название: {parser.header.title}")
    print(f"  Игра: {parser.header.game}")
    if parser.header.artist:
        print(f"  Артист: {parser.header.artist}")

    # Определяем длительность
    spc_duration = parser.get_duration()

    if duration_override is not None:
        duration = duration_override
        dur_source = "пользователь"
    elif spc_duration > 0:
        duration = spc_duration
        dur_source = "ID666"
    else:
        duration = 120.0
        dur_source = "по умолчанию"

    dur_str = f"{parser.header.duration_sec}с" if parser.header.duration_sec else "нет"
    fade_str = f"{parser.header.fade_ms}мс" if parser.header.fade_ms else "нет"
    print(f"  ID666 длительность: {dur_str}, fade: {fade_str}")
    print(f"  Эмуляция: {duration:.1f} сек ({dur_source})")

    total_transpose = transpose + octave * 12
    if total_transpose:
        print(f"  Transpose: {total_transpose:+d} полутонов")
    if clean_samples:
        print(f"  Очистка сэмплов: ВКЛ")
    if compact > 1:
        print(f"  Compact: x{compact}")

    names_l = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-']
    kon = parser.g_reg(DSPAddr.KON)
    print(f"\n  Голоса DSP:")
    for v in range(8):
        pitch = parser.v_reg(v, DSPAddr.PITCH_L) | ((parser.v_reg(v, DSPAddr.PITCH_H) & 0x3F) << 8)
        src = parser.v_reg(v, DSPAddr.SRCN)
        note = pitch_to_xm_note(pitch)
        ns = ""
        if note:
            n = note - 1
            ns = f"{names_l[n % 12]}{n // 12}"
        on = "ON " if kon & (1 << v) else "off"
        print(f"    Voice {v}: src={src:02X} pitch=0x{pitch:04X} key={on} {ns}")

    brr_samples = parser.extract_samples()
    print(f"\n  BRR-сэмплов: {len(brr_samples)}")

    cpu_clock = 1024000.0
    emu_cycles = int(cpu_clock * duration)
    print(f"  Эмуляция SPC700...")
    emu = SPC700Emulator(parser.ram, parser.dsp_regs, parser.header)
    all_writes = emu.run(emu_cycles)
    kon_n = sum(1 for _, a, v in all_writes if a == DSPAddr.KON and v)
    print(f"  DSP: {len(all_writes)} записей, KON: {kon_n}")

    timer_targets = [parser.ram[0xFA], parser.ram[0xFB], parser.ram[0xFC]]
    tick_t = TempoDetector.detect_from_timer_reads(emu.timer_read_log, timer_targets, cpu_clock)
    kon_cycles = [c for c, a, v in all_writes if a == DSPAddr.KON and v]
    tick_k = TempoDetector.detect_tick_interval(kon_cycles, cpu_clock)
    tick_cycles = tick_t if tick_t and tick_t > 100 else tick_k

    bpm, speed = TempoDetector.compute_bpm_speed(tick_cycles, cpu_clock)
    if bpm_override:
        bpm = bpm_override
    if speed_override:
        speed = speed_override
    print(f"  Tempo: BPM={bpm} Speed={speed}")

    analyzer = DSPAnalyzer(all_writes, parser.dsp_regs, tick_cycles)
    events = analyzer.analyze()
    note_ons = [e for e in events if e.event_type == 'note_on']
    print(f"\n  Нот: {len(note_ons)}")

    if note_ons:
        print(f"  Первые 10:")
        for e in note_ons[:10]:
            n = e.note - 1
            ns = f"{names_l[n % 12]}{n // 12}"
            print(f"    row={e.tick:>5d} ch={e.channel} {ns:>4s} inst={e.instrument} vol={e.volume}")

    # XM output
    if output_format in ('xm', 'both'):
        xm_file = output_file if output_file.endswith('.xm') else base + '.xm'
        writer = XMWriter()
        writer.build(events, brr_samples, title=parser.header.title or "SPC Convert",
                     bpm=bpm, speed=speed, transpose=transpose, octave=octave,
                     finetune=finetune, clean_samples=clean_samples, compact=compact)

    # Показываем автоматический тюнинг инструментов
    if writer.instruments:
        tuned = [(inst['name'], inst.get('_auto_tuning', (0,0))) 
                 for inst in writer.instruments if inst.get('_auto_tuning', (0,0)) != (0,0)]
        if tuned:
            print(f"\n  Авто-тюнинг сэмплов:")
            for name, (rel, fine) in tuned[:15]:
                sign_r = '+' if rel > 0 else ''
                sign_f = '+' if fine > 0 else ''
                print(f"    {name}: relative_note={sign_r}{rel}, finetune={sign_f}{fine}")


        writer.write(xm_file)
        xm_size = os.path.getsize(xm_file)
        print(f"\n  XM: {xm_file} ({xm_size / 1024:.1f} KB)")
        print(f"    Паттернов: {len(writer.patterns)}, Инструментов: {len(writer.instruments)}")

    # MIDI output
    if output_format in ('midi', 'both') or midi_file:
        mid_file = midi_file or (base + '.mid')
        midi_writer = MIDIWriter()
        midi_writer.build(events, tick_cycles, cpu_clock, bpm=bpm,
                          transpose=transpose, octave=octave,
                          title=parser.header.title or "SPC Convert")
        midi_writer.write(mid_file)
        mid_size = os.path.getsize(mid_file)
        used_ch = len(set(e.channel for e in note_ons))
        print(f"\n  MIDI: {mid_file} ({mid_size / 1024:.1f} KB)")
        print(f"    Треков: {len(midi_writer.tracks)}, Каналов: {used_ch}")

    print(f"\n  Итого: {len(note_ons)} нот, {duration:.1f} сек")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='SPC to XM/MIDI Converter v9',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python spc2xm.py music.spc                          # авто-длительность из ID666
  python spc2xm.py music.spc --duration 60            # принудительно 60 сек
  python spc2xm.py music.spc --midi                   # XM + MIDI
  python spc2xm.py music.spc --midi-only              # только MIDI
  python spc2xm.py music.spc --clean-samples --compact 4
  python spc2xm.py *.spc --midi --clean-samples

Длительность трека:
  По умолчанию берётся из ID666 тегов SPC файла (если есть).
  Если ID666 не содержит длительности — используется 120 сек.
  --duration N переопределяет длительность вручную.
        """)
    ap.add_argument('input', nargs='+', help='Входные SPC файлы')
    ap.add_argument('-o', '--output', help='Выходной XM файл')
    ap.add_argument('--duration', type=float, default=None,
                    help='Длительность эмуляции в секундах '
                         '(по умолчанию: из ID666 тегов или 120)')
    ap.add_argument('--octave', type=int, default=0,
                    help='Сдвиг октав: +1 = выше, -1 = ниже')
    ap.add_argument('--transpose', type=int, default=0,
                    help='Сдвиг полутонов')
    ap.add_argument('--finetune', type=int, default=0,
                    help='Точная подстройка -128..127')
    ap.add_argument('--bpm', type=int, default=None,
                    help='Принудительный BPM')
    ap.add_argument('--speed', type=int, default=None,
                    help='Принудительный XM Speed')
    ap.add_argument('--clean-samples', action='store_true',
                    help='Удалить неиспользуемые сэмплы')
    ap.add_argument('--compact', type=int, default=1,
                    help='Сжатие паттернов (2, 4, 8...)')
    ap.add_argument('--midi', action='store_true',
                    help='Дополнительно сохранить MIDI')
    ap.add_argument('--midi-only', action='store_true',
                    help='Только MIDI (без XM)')
    ap.add_argument('--midi-file', type=str, default=None,
                    help='Имя MIDI файла')

    args = ap.parse_args()
    if args.output and len(args.input) > 1:
        print("--output: только для одного файла")
        sys.exit(1)
    if args.compact < 1:
        print("--compact >= 1")
        sys.exit(1)

    if args.midi_only:
        fmt = 'midi'
    elif args.midi or args.midi_file:
        fmt = 'both'
    else:
        fmt = 'xm'

    for inp in args.input:
        if not os.path.exists(inp):
            print(f"Не найден: {inp}")
            continue
        try:
            convert_spc(inp, args.output,
                        duration_override=args.duration,
                        transpose=args.transpose, octave=args.octave,
                        finetune=args.finetune,
                        bpm_override=args.bpm, speed_override=args.speed,
                        clean_samples=args.clean_samples,
                        compact=args.compact,
                        output_format=fmt,
                        midi_file=args.midi_file)
        except Exception as e:
            import traceback
            print(f"Ошибка: {e}")
            traceback.print_exc()

    print("\nГотово!")


if __name__=='__main__': main()