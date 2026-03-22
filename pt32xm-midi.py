"""
PT3 to XM/MIDI Converter v6
Based on pt3player.c by Bulba/Volutar
With channel mapping and --raw-notes mode
"""

import struct, sys, os, math
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

NOTE_NAMES = ['C-','C#','D-','D#','E-','F-',
              'F#','G-','G#','A-','A#','B-']

PT3NoteTable_PT_34_35 = [
    0xC22,0xB73,0xACF,0xA33,0x9A1,0x917,0x894,0x819,0x7A4,0x737,0x6CF,0x66D,
    0x611,0x5BA,0x567,0x51A,0x4D0,0x48B,0x44A,0x40C,0x3D2,0x39B,0x367,0x337,
    0x308,0x2DD,0x2B4,0x28D,0x268,0x246,0x225,0x206,0x1E9,0x1CE,0x1B4,0x19B,
    0x184,0x16E,0x15A,0x146,0x134,0x123,0x112,0x103,0x0F5,0x0E7,0x0DA,0x0CE,
    0x0C2,0x0B7,0x0AD,0x0A3,0x09A,0x091,0x089,0x082,0x07A,0x073,0x06D,0x067,
    0x061,0x05C,0x056,0x052,0x04D,0x049,0x045,0x041,0x03D,0x03A,0x036,0x033,
    0x031,0x02E,0x02B,0x029,0x027,0x024,0x022,0x020,0x01F,0x01D,0x01B,0x01A,
    0x018,0x017,0x016,0x014,0x013,0x012,0x011,0x010,0x00F,0x00E,0x00D,0x00C]

PT3NoteTable_ASM_34_35 = [
    0xD10,0xC55,0xBA4,0xAFC,0xA5F,0x9CA,0x93D,0x8B8,0x83B,0x7C5,0x755,0x6EC,
    0x688,0x62A,0x5D2,0x57E,0x52F,0x4E5,0x49E,0x45C,0x41D,0x3E2,0x3AB,0x376,
    0x344,0x315,0x2E9,0x2BF,0x298,0x272,0x24F,0x22E,0x20F,0x1F1,0x1D5,0x1BB,
    0x1A2,0x18B,0x174,0x160,0x14C,0x139,0x128,0x117,0x107,0x0F9,0x0EB,0x0DD,
    0x0D1,0x0C5,0x0BA,0x0B0,0x0A6,0x09D,0x094,0x08C,0x084,0x07C,0x075,0x06F,
    0x069,0x063,0x05D,0x058,0x053,0x04E,0x04A,0x046,0x042,0x03E,0x03B,0x037,
    0x034,0x031,0x02F,0x02C,0x029,0x027,0x025,0x023,0x021,0x01F,0x01D,0x01C,
    0x01A,0x019,0x017,0x016,0x015,0x014,0x012,0x011,0x010,0x00F,0x00E,0x00D]

PT3NoteTable_ST = [
    0xEF8,0xE10,0xD60,0xC80,0xBD8,0xB28,0xA88,0x9F0,0x960,0x8E0,0x858,0x7E0,
    0x77C,0x708,0x6B0,0x640,0x5EC,0x594,0x544,0x4F8,0x4B0,0x470,0x42C,0x3FD,
    0x3BE,0x384,0x358,0x320,0x2F6,0x2CA,0x2A2,0x27C,0x258,0x238,0x216,0x1F8,
    0x1DF,0x1C2,0x1AC,0x190,0x17B,0x165,0x151,0x13E,0x12C,0x11C,0x10A,0x0FC,
    0x0EF,0x0E1,0x0D6,0x0C8,0x0BD,0x0B2,0x0A8,0x09F,0x096,0x08E,0x085,0x07E,
    0x077,0x070,0x06B,0x064,0x05E,0x059,0x054,0x04F,0x04B,0x047,0x042,0x03F,
    0x03B,0x038,0x035,0x032,0x02F,0x02C,0x02A,0x027,0x025,0x023,0x021,0x01F,
    0x01D,0x01C,0x01A,0x019,0x017,0x016,0x015,0x013,0x012,0x011,0x010,0x00F]

PT3NoteTable_REAL_34_35 = [
    0xCDA,0xC22,0xB73,0xACF,0xA33,0x9A1,0x917,0x894,0x819,0x7A4,0x737,0x6CF,
    0x66D,0x611,0x5BA,0x567,0x51A,0x4D0,0x48B,0x44A,0x40C,0x3D2,0x39B,0x367,
    0x337,0x308,0x2DD,0x2B4,0x28D,0x268,0x246,0x225,0x206,0x1E9,0x1CE,0x1B4,
    0x19B,0x184,0x16E,0x15A,0x146,0x134,0x123,0x112,0x103,0x0F5,0x0E7,0x0DA,
    0x0CE,0x0C2,0x0B7,0x0AD,0x0A3,0x09A,0x091,0x089,0x082,0x07A,0x073,0x06D,
    0x067,0x061,0x05C,0x056,0x052,0x04D,0x049,0x045,0x041,0x03D,0x03A,0x036,
    0x033,0x031,0x02E,0x02B,0x029,0x027,0x024,0x022,0x020,0x01F,0x01D,0x01B,
    0x01A,0x018,0x017,0x016,0x014,0x013,0x012,0x011,0x010,0x00F,0x00E,0x00D]

def note_name_xm(n):
    if 1<=n<=96: v=n-1; return f"{NOTE_NAMES[v%12]}{v//12}"
    return "---"

def xm_note_to_midi(n): return n+11

# ═══════════════════════════════════════════════════════════════
# AY Modes & Channel mapping
# ═══════════════════════════════════════════════════════════════

MODE_TONE='tone'; MODE_NOISE='noise'; MODE_BUZZER='buzzer'
MODE_MIXED='mixed'; MODE_ENV_TONE='env_tone'
ALL_MODES=[MODE_TONE,MODE_NOISE,MODE_BUZZER,MODE_MIXED,MODE_ENV_TONE]

CHANNEL_PRESETS = {
    'default': {
        (0,MODE_TONE):0,(0,MODE_MIXED):0,(0,MODE_ENV_TONE):0,
        (0,MODE_BUZZER):3,(0,MODE_NOISE):6,
        (1,MODE_TONE):1,(1,MODE_MIXED):1,(1,MODE_ENV_TONE):1,
        (1,MODE_BUZZER):4,(1,MODE_NOISE):7,
        (2,MODE_TONE):2,(2,MODE_MIXED):2,(2,MODE_ENV_TONE):2,
        (2,MODE_BUZZER):5,(2,MODE_NOISE):8,
    },
    'compact': {
        (0,MODE_TONE):0,(0,MODE_MIXED):0,(0,MODE_ENV_TONE):0,
        (0,MODE_BUZZER):3,(0,MODE_NOISE):0,
        (1,MODE_TONE):1,(1,MODE_MIXED):1,(1,MODE_ENV_TONE):1,
        (1,MODE_BUZZER):3,(1,MODE_NOISE):1,
        (2,MODE_TONE):2,(2,MODE_MIXED):2,(2,MODE_ENV_TONE):2,
        (2,MODE_BUZZER):3,(2,MODE_NOISE):2,
    },
    'split-all': {
        (0,MODE_TONE):0,(0,MODE_MIXED):3,(0,MODE_ENV_TONE):6,
        (0,MODE_BUZZER):9,(0,MODE_NOISE):12,
        (1,MODE_TONE):1,(1,MODE_MIXED):4,(1,MODE_ENV_TONE):7,
        (1,MODE_BUZZER):10,(1,MODE_NOISE):13,
        (2,MODE_TONE):2,(2,MODE_MIXED):5,(2,MODE_ENV_TONE):8,
        (2,MODE_BUZZER):11,(2,MODE_NOISE):14,
    },
    'minimal': {
        (0,MODE_TONE):0,(0,MODE_MIXED):0,(0,MODE_ENV_TONE):0,
        (0,MODE_BUZZER):0,(0,MODE_NOISE):0,
        (1,MODE_TONE):1,(1,MODE_MIXED):1,(1,MODE_ENV_TONE):1,
        (1,MODE_BUZZER):1,(1,MODE_NOISE):1,
        (2,MODE_TONE):2,(2,MODE_MIXED):2,(2,MODE_ENV_TONE):2,
        (2,MODE_BUZZER):2,(2,MODE_NOISE):2,
    },
    'buzzer-split': {
        (0,MODE_TONE):0,(0,MODE_MIXED):0,(0,MODE_ENV_TONE):0,
        (0,MODE_BUZZER):3,(0,MODE_NOISE):6,
        (1,MODE_TONE):1,(1,MODE_MIXED):1,(1,MODE_ENV_TONE):1,
        (1,MODE_BUZZER):4,(1,MODE_NOISE):6,
        (2,MODE_TONE):2,(2,MODE_MIXED):2,(2,MODE_ENV_TONE):2,
        (2,MODE_BUZZER):5,(2,MODE_NOISE):6,
    },
}

MODE_INSTRUMENTS={MODE_TONE:1,MODE_MIXED:4,MODE_ENV_TONE:2,MODE_BUZZER:3,MODE_NOISE:5}
MODE_MIDI_GM={MODE_TONE:80,MODE_MIXED:81,MODE_ENV_TONE:84,MODE_BUZZER:87,MODE_NOISE:119}

def get_channel_name(ay_ch,mode):
    cn=['A','B','C'][ay_ch] if ay_ch<3 else str(ay_ch)
    mn={MODE_TONE:'Tone',MODE_NOISE:'Noise',MODE_BUZZER:'Buzz',
        MODE_MIXED:'T+N',MODE_ENV_TONE:'T+E'}
    return f"{cn}-{mn.get(mode,mode)}"

def parse_channel_map(spec):
    if spec in CHANNEL_PRESETS: return CHANNEL_PRESETS[spec]
    ma={'t':MODE_TONE,'tone':MODE_TONE,'n':MODE_NOISE,'noise':MODE_NOISE,
        'b':MODE_BUZZER,'buzz':MODE_BUZZER,'buzzer':MODE_BUZZER,
        'm':MODE_MIXED,'mix':MODE_MIXED,'mixed':MODE_MIXED,
        'e':MODE_ENV_TONE,'env':MODE_ENV_TONE,'env_tone':MODE_ENV_TONE}
    ca={'a':0,'b':1,'c':2,'0':0,'1':1,'2':2}
    result={}
    for part in spec.split(','):
        part=part.strip()
        if not part: continue
        if '=' not in part or ':' not in part:
            raise ValueError(f"Bad: '{part}'")
        left,right=part.split('=',1)
        ay_str,mode_str=left.split(':',1)
        ay_ch=ca.get(ay_str.strip().lower())
        if ay_ch is None: raise ValueError(f"Unknown AY ch: '{ay_str}'")
        mode=ma.get(mode_str.strip().lower())
        if mode is None: raise ValueError(f"Unknown mode: '{mode_str}'")
        result[(ay_ch,mode)]=int(right.strip())
    for ay_ch in range(3):
        for mode in ALL_MODES:
            if(ay_ch,mode) not in result:
                if(ay_ch,MODE_TONE) in result: result[(ay_ch,mode)]=result[(ay_ch,MODE_TONE)]
                else: result[(ay_ch,mode)]=ay_ch
    return result

# ═══════════════════════════════════════════════════════════════
# PT3 Player
# ═══════════════════════════════════════════════════════════════

@dataclass
class ChannelState:
    address:int=0;orn_ptr:int=0;smp_ptr:int=0;tone:int=0
    loop_orn:int=0;orn_len:int=0;pos_orn:int=0
    loop_smp:int=0;smp_len:int=0;pos_smp:int=0
    volume:int=15;notes_to_skip:int=1;note:int=0
    slide_to_note:int=0;amplitude:int=0
    env_enabled:bool=False;enabled:bool=False;simple_gliss:bool=True
    cur_amp_slide:int=0;cur_noise_slide:int=0;cur_env_slide:int=0
    ton_slide_count:int=0;cur_onoff:int=0
    onoff_delay:int=0;offon_delay:int=0
    ton_slide_delay:int=0;cur_ton_sliding:int=0
    ton_accum:int=0;ton_slide_step:int=0;ton_delta:int=0
    skip_counter:int=1
    # --- для raw mode ---
    note_changed:bool=False   # нота изменилась в этом тике
    just_disabled:bool=False  # канал выключен (C0) в этом тике

@dataclass
class PT3State:
    env_base:int=0;cur_env_slide:int=0;env_slide_add:int=0
    cur_env_delay:int=0;env_delay:int=0;noise_base:int=0
    delay:int=6;add_to_noise:int=0;delay_counter:int=1
    current_position:int=0

class PT3Player:
    def __init__(self,data):
        self.d=bytearray(data);self.version=6;self.tone_table_id=0
        self.channels=[ChannelState() for _ in range(3)]
        self.state=PT3State();self.ay=[0]*14
        self.temp_mixer=0;self.add_to_env=0
        self._parse_header();self._init_playback()

    def _b(self,p): return self.d[p] if p<len(self.d) else 0
    def _w(self,p): return(self.d[p]|(self.d[p+1]<<8))if p+1<len(self.d) else 0
    def _sw(self,p): v=self._w(p);return v-65536 if v>=32768 else v

    def _parse_header(self):
        d=self.d;v=self._b(0x0D)
        self.version=v-0x30 if 0x30<=v<=0x39 else 6
        self.title=bytes(d[0x1E:0x3E]).decode('ascii','replace').rstrip('\x00 ')
        self.author=bytes(d[0x42:0x62]).decode('ascii','replace').rstrip('\x00 ')
        self.tone_table_id=self._b(0x63)
        self.initial_delay=self._b(0x64)
        if self.initial_delay==0:self.initial_delay=6
        i=0
        while i<65535-201:
            if self._b(0xC9+i)==255:break
            i+=1
        self.num_positions=min(i,self._b(0x65))if self._b(0x65)>0 else i
        self.loop_position=self._b(0x66)
        self.pat_ptr=self._w(0x67)
        self.positions=[self._b(0xC9+i)for i in range(self.num_positions)]
        self.smp_ptrs=[self._w(0x69+i*2)for i in range(32)]
        self.orn_ptrs=[self._w(0xA9+i*2)for i in range(16)]

    def get_note_freq(self,j):
        j=max(0,min(95,j));tt=self.tone_table_id
        if tt==0:return PT3NoteTable_PT_34_35[j]
        elif tt==1:return PT3NoteTable_ST[j]
        elif tt==2:return PT3NoteTable_ASM_34_35[j]
        else:return PT3NoteTable_REAL_34_35[j]

    def _init_playback(self):
        st=self.state;st.delay=self.initial_delay;st.delay_counter=1
        st.noise_base=0;st.add_to_noise=0;st.cur_env_slide=0
        st.cur_env_delay=0;st.env_base=0;st.current_position=0
        if not self.positions:return
        i=self.positions[0]
        for abc in range(3):
            ch=self.channels[abc]
            ch.address=self._w(self.pat_ptr+(i+abc)*2)
            ch.orn_ptr=self.orn_ptrs[0]
            ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1);ch.orn_ptr+=2
            ch.smp_ptr=self.smp_ptrs[1]
            ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1);ch.smp_ptr+=2
            ch.volume=15;ch.skip_counter=1;ch.notes_to_skip=1
            ch.enabled=False;ch.env_enabled=False;ch.note=0;ch.tone=0

    def pattern_interpreter(self,abc):
        ch=self.channels[abc];st=self.state
        pr_note=ch.note;pr_sliding=ch.cur_ton_sliding
        effect_flags=[];quit=False
        ch.note_changed=False;ch.just_disabled=False
        while not quit and ch.address<len(self.d):
            op=self._b(ch.address)
            if 0xF0<=op<=0xFF:
                orn_idx=op-0xF0
                ch.orn_ptr=self.orn_ptrs[orn_idx]
                ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                ch.orn_ptr+=2;ch.pos_orn=0;ch.address+=1
                smp_idx=self._b(ch.address)//2
                ch.smp_ptr=self.smp_ptrs[smp_idx]
                ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1)
                ch.smp_ptr+=2;ch.env_enabled=False
            elif 0xD1<=op<=0xEF:
                if op<=0xDF:
                    smp_idx=op-0xD0;ch.smp_ptr=self.smp_ptrs[smp_idx]
                    ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1);ch.smp_ptr+=2
                else:
                    orn_idx=op-0xE0;ch.orn_ptr=self.orn_ptrs[orn_idx]
                    ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                    ch.orn_ptr+=2;ch.pos_orn=0
            elif op==0xD0:quit=True
            elif 0xC1<=op<=0xCF:ch.volume=op-0xC0
            elif op==0xC0:
                ch.pos_smp=0;ch.cur_amp_slide=0;ch.cur_noise_slide=0
                ch.cur_env_slide=0;ch.pos_orn=0;ch.ton_slide_count=0
                ch.cur_ton_sliding=0;ch.ton_accum=0;ch.cur_onoff=0
                ch.enabled=False;ch.just_disabled=True;quit=True
            elif 0xB2<=op<=0xBF:
                ch.env_enabled=True;self.ay[13]=op-0xB1
                ch.address+=1;hi=self._b(ch.address)
                ch.address+=1;lo=self._b(ch.address)
                st.env_base=(hi<<8)|lo;ch.pos_orn=0
                st.cur_env_slide=0;st.cur_env_delay=0
            elif op==0xB1:ch.address+=1;ch.notes_to_skip=self._b(ch.address)
            elif op==0xB0:ch.env_enabled=False;ch.pos_orn=0
            elif 0x50<=op<=0xAF:
                ch.note=op-0x50;ch.pos_smp=0;ch.cur_amp_slide=0
                ch.cur_noise_slide=0;ch.cur_env_slide=0;ch.pos_orn=0
                ch.ton_slide_count=0;ch.cur_ton_sliding=0;ch.ton_accum=0
                ch.cur_onoff=0;ch.enabled=True
                ch.note_changed=True;quit=True
            elif 0x40<=op<=0x4F:
                orn_idx=op-0x40;ch.orn_ptr=self.orn_ptrs[orn_idx]
                ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                ch.orn_ptr+=2;ch.pos_orn=0
            elif 0x20<=op<=0x3F:st.noise_base=op-0x20
            elif 0x11<=op<=0x1F:
                self.ay[13]=op-0x10;ch.address+=1;hi=self._b(ch.address)
                ch.address+=1;lo=self._b(ch.address)
                st.env_base=(hi<<8)|lo;st.cur_env_slide=0;st.cur_env_delay=0
                ch.env_enabled=True;ch.address+=1
                smp_idx=self._b(ch.address)//2;ch.smp_ptr=self.smp_ptrs[smp_idx]
                ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1)
                ch.smp_ptr+=2;ch.pos_orn=0
            elif op==0x10:
                ch.env_enabled=False;ch.address+=1
                smp_idx=self._b(ch.address)//2;ch.smp_ptr=self.smp_ptrs[smp_idx]
                ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1)
                ch.smp_ptr+=2;ch.pos_orn=0
            elif 1<=op<=9:effect_flags.append(op)
            ch.address+=1
        for ef in reversed(effect_flags):
            if ef==1:
                ch.ton_slide_delay=self._b(ch.address);ch.address+=1
                ch.ton_slide_count=ch.ton_slide_delay
                ch.ton_slide_step=self._sw(ch.address);ch.address+=2
                ch.simple_gliss=True;ch.cur_onoff=0
            elif ef==2:
                ch.simple_gliss=False;ch.cur_onoff=0
                ch.ton_slide_delay=self._b(ch.address);ch.address+=1
                ch.ton_slide_count=ch.ton_slide_delay
                ch.ton_slide_step=abs(self._sw(ch.address+2))
                ch.ton_delta=self.get_note_freq(ch.note)-self.get_note_freq(pr_note)
                ch.slide_to_note=ch.note;ch.note=pr_note
                if self.version>=6:ch.cur_ton_sliding=pr_sliding
                if ch.ton_delta-ch.cur_ton_sliding<0:ch.ton_slide_step=-ch.ton_slide_step
                ch.address+=4
            elif ef==3:ch.pos_smp=self._b(ch.address);ch.address+=1
            elif ef==4:ch.pos_orn=self._b(ch.address);ch.address+=1
            elif ef==5:
                ch.onoff_delay=self._b(ch.address);ch.address+=1
                ch.offon_delay=self._b(ch.address);ch.address+=1
                ch.cur_onoff=ch.onoff_delay;ch.ton_slide_count=0;ch.cur_ton_sliding=0
            elif ef==8:
                st.env_delay=self._b(ch.address);ch.address+=1
                st.cur_env_delay=st.env_delay
                st.env_slide_add=self._sw(ch.address);ch.address+=2
            elif ef==9:self.state.delay=self._b(ch.address);ch.address+=1
        ch.skip_counter=ch.notes_to_skip

    def change_registers(self,abc):
        ch=self.channels[abc];st=self.state
        if ch.enabled:
            smp_base=ch.smp_ptr+ch.pos_smp*4
            b0=self._b(smp_base);b1=self._b(smp_base+1)
            ch.tone=self._w(smp_base+2)+ch.ton_accum
            if b1&0x40:ch.ton_accum=ch.tone
            orn_val=self._b(ch.orn_ptr+ch.pos_orn)
            if orn_val>=128:orn_val-=256
            j=ch.note+orn_val
            if j<0:j=0
            elif j>95:j=95
            w=self.get_note_freq(j)
            ch.tone=(ch.tone+ch.cur_ton_sliding+w)&0xFFF
            if ch.ton_slide_count>0:
                ch.ton_slide_count-=1
                if ch.ton_slide_count==0:
                    ch.cur_ton_sliding+=ch.ton_slide_step
                    ch.ton_slide_count=ch.ton_slide_delay
                    if not ch.simple_gliss:
                        if((ch.ton_slide_step<0 and ch.cur_ton_sliding<=ch.ton_delta)or
                           (ch.ton_slide_step>=0 and ch.cur_ton_sliding>=ch.ton_delta)):
                            ch.note=ch.slide_to_note;ch.ton_slide_count=0;ch.cur_ton_sliding=0
            ch.amplitude=b1&0x0F
            if b0&0x80:
                if b0&0x40:
                    if ch.cur_amp_slide<15:ch.cur_amp_slide+=1
                else:
                    if ch.cur_amp_slide>-15:ch.cur_amp_slide-=1
            ch.amplitude+=ch.cur_amp_slide
            if ch.amplitude<0:ch.amplitude=0
            elif ch.amplitude>15:ch.amplitude=15
            ch.amplitude=(ch.volume*ch.amplitude+8)//15
            if not(b0&1)and ch.env_enabled:ch.amplitude|=0x10
            if b1&0x80:
                if b0&0x20:j2=((b0>>1)|0xF0)+ch.cur_env_slide
                else:j2=((b0>>1)&0x0F)+ch.cur_env_slide
                if b1&0x20:ch.cur_env_slide=j2
                self.add_to_env+=j2
            else:
                st.add_to_noise=((b0>>1)&0xFF)+ch.cur_noise_slide
                if b1&0x20:ch.cur_noise_slide=st.add_to_noise
            self.temp_mixer|=((b1>>1)&0x48)
            ch.pos_smp+=1
            if ch.pos_smp>=ch.smp_len:ch.pos_smp=ch.loop_smp
            ch.pos_orn+=1
            if ch.pos_orn>=ch.orn_len:ch.pos_orn=ch.loop_orn
        else:ch.amplitude=0
        self.temp_mixer>>=1
        if ch.cur_onoff>0:
            ch.cur_onoff-=1
            if ch.cur_onoff==0:
                ch.enabled=not ch.enabled
                ch.cur_onoff=ch.onoff_delay if ch.enabled else ch.offon_delay

    def play_tick(self):
        st=self.state;self.ay[13]=0xFF
        # Сбрасываем флаги
        for abc in range(3):
            self.channels[abc].note_changed=False
            self.channels[abc].just_disabled=False
        st.delay_counter-=1
        if st.delay_counter==0:
            self.channels[0].skip_counter-=1
            if self.channels[0].skip_counter==0:
                if self._b(self.channels[0].address)==0:
                    st.current_position+=1
                    if st.current_position>=self.num_positions:
                        st.current_position=self.loop_position
                    i=self.positions[st.current_position]
                    for abc in range(3):
                        self.channels[abc].address=self._w(self.pat_ptr+(i+abc)*2)
                    st.noise_base=0
                self.pattern_interpreter(0)
            for abc in range(1,3):
                self.channels[abc].skip_counter-=1
                if self.channels[abc].skip_counter==0:
                    self.pattern_interpreter(abc)
            st.delay_counter=st.delay
        self.add_to_env=0;self.temp_mixer=0
        self.change_registers(0);self.change_registers(1);self.change_registers(2)
        self.ay[0]=self.channels[0].tone&0xFF;self.ay[1]=self.channels[0].tone>>8
        self.ay[2]=self.channels[1].tone&0xFF;self.ay[3]=self.channels[1].tone>>8
        self.ay[4]=self.channels[2].tone&0xFF;self.ay[5]=self.channels[2].tone>>8
        self.ay[6]=(st.noise_base+st.add_to_noise)&0x1F
        self.ay[7]=self.temp_mixer
        self.ay[8]=self.channels[0].amplitude;self.ay[9]=self.channels[1].amplitude
        self.ay[10]=self.channels[2].amplitude
        env=(st.env_base+self.add_to_env+st.cur_env_slide)&0xFFFF
        self.ay[11]=env&0xFF;self.ay[12]=env>>8
        if st.cur_env_delay>0:
            st.cur_env_delay-=1
            if st.cur_env_delay==0:
                st.cur_env_delay=st.env_delay;st.cur_env_slide+=st.env_slide_add
        return list(self.ay)

# ═══════════════════════════════════════════════════════════════
# Note extraction
# ═══════════════════════════════════════════════════════════════

CLOCK=1773400;REPEATING_ENV={0x08,0x0A,0x0C,0x0E}
def ay_freq(p): return CLOCK/(16.0*p) if p>0 else 0
def env_freq(p): return CLOCK/(256.0*p) if p>0 else 0
def freq_to_xm(freq):
    if freq<15 or freq>20000:return None
    midi=69+12*math.log2(freq/440.0);xm=int(round(midi))-11
    return xm if 1<=xm<=96 else None

@dataclass
class NoteEvent:
    frame:int=0;channel:int=0;ay_channel:int=0
    mode:str="";event_type:str=""
    note:int=0;instrument:int=1;volume:int=64

def extract_notes_raw(player, max_frames=50000):
    """
    RAW mode: берём ноты напрямую из PT3 player state.
    ch.note = номер ноты как записан в паттерне (0-95)
    ch.enabled = канал включён
    ch.note_changed = в этом тике была новая нота
    ch.just_disabled = в этом тике канал выключен (C0)
    ch.volume = громкость (0-15)
    
    Без орнаментов, без сэмплов, без арпеджио.
    3 канала, прямое соответствие PT3.
    """
    events=[]
    prev_note=[-1,-1,-1]
    prev_vol=[0,0,0]

    for frame in range(max_frames):
        player.play_tick()
        for ay_ch in range(3):
            ch=player.channels[ay_ch]
            if ch.just_disabled:
                if prev_note[ay_ch]>=0:
                    events.append(NoteEvent(frame,ay_ch,ay_ch,'','note_off',97))
                    prev_note[ay_ch]=-1;prev_vol[ay_ch]=0
            elif ch.note_changed and ch.enabled:
                if prev_note[ay_ch]>=0:
                    events.append(NoteEvent(frame,ay_ch,ay_ch,'','note_off',97))
                xm_note=ch.note+1  # PT3 0-95 → XM 1-96
                xm_vol=max(1,int(64*ch.volume/15))
                events.append(NoteEvent(frame,ay_ch,ay_ch,MODE_TONE,
                    'note_on',max(1,min(96,xm_note)),1,xm_vol))
                prev_note[ay_ch]=ch.note;prev_vol[ay_ch]=xm_vol
        # Stop condition
        if(player.state.current_position>=player.num_positions-1 and
           player.state.delay_counter==1 and
           player.channels[0].skip_counter==1 and frame>100):
            if player._b(player.channels[0].address)==0:break
    for ay_ch in range(3):
        if prev_note[ay_ch]>=0:
            events.append(NoteEvent(frame,ay_ch,ay_ch,'','note_off',97))
    return events


def extract_notes_ay(player, channel_map, max_frames=50000):
    """AY mode: анализ AY регистров с маппингом по режимам."""
    events=[]
    max_xm=max(channel_map.values())+1
    prev_note=[-1]*max_xm;prev_vol=[0]*max_xm
    prev_inst=[0]*max_xm;prev_silent=[True]*max_xm
    prev_ay_mode=[None]*3;prev_ay_xm=[-1]*3

    def get_xm(ay_ch,mode):
        key=(ay_ch,mode)
        if key in channel_map:return channel_map[key]
        for fb in[MODE_TONE,MODE_MIXED,MODE_BUZZER]:
            if(ay_ch,fb) in channel_map:return channel_map[(ay_ch,fb)]
        return ay_ch

    for frame in range(max_frames):
        regs=player.play_tick();mixer=regs[7]
        for ay_ch in range(3):
            period=regs[ay_ch*2]|((regs[ay_ch*2+1]&0x0F)<<8)
            vol_reg=regs[8+ay_ch];use_env=bool(vol_reg&0x10)
            volume=vol_reg&0x0F
            tone_off=bool(mixer&(1<<ay_ch));noise_off=bool(mixer&(1<<(ay_ch+3)))
            env_period=regs[11]|(regs[12]<<8)
            env_shape=regs[13] if regs[13]!=0xFF else -1
            note=None;eff_vol=volume;mode=None;inst=1
            if use_env and tone_off and noise_off:
                if env_shape in REPEATING_ENV and env_period>0:
                    note=freq_to_xm(env_freq(env_period));eff_vol=15
                    mode=MODE_BUZZER;inst=MODE_INSTRUMENTS[mode]
            elif use_env and not tone_off:
                if period>0:
                    note=freq_to_xm(ay_freq(period));eff_vol=15
                    mode=MODE_ENV_TONE;inst=MODE_INSTRUMENTS[mode]
            elif volume>0:
                if not tone_off and period>0:
                    note=freq_to_xm(ay_freq(period))
                    mode=MODE_MIXED if not noise_off else MODE_TONE
                    inst=MODE_INSTRUMENTS[mode]
                elif not noise_off:
                    np=regs[6]&0x1F;note=max(1,min(96,72-np))
                    mode=MODE_NOISE;inst=MODE_INSTRUMENTS[mode]
            if note is None or eff_vol==0 or mode is None:
                old_xm=prev_ay_xm[ay_ch]
                if old_xm>=0 and prev_note[old_xm]>=0:
                    events.append(NoteEvent(frame,old_xm,ay_ch,prev_ay_mode[ay_ch] or '','note_off',97))
                    prev_note[old_xm]=-1;prev_vol[old_xm]=0;prev_inst[old_xm]=0;prev_silent[old_xm]=True
                prev_ay_mode[ay_ch]=None;prev_ay_xm[ay_ch]=-1;continue
            xm_ch=get_xm(ay_ch,mode);xm_vol=max(1,int(64*eff_vol/15))
            old_xm=prev_ay_xm[ay_ch]
            if old_xm>=0 and old_xm!=xm_ch:
                if prev_note[old_xm]>=0:
                    events.append(NoteEvent(frame,old_xm,ay_ch,prev_ay_mode[ay_ch] or '','note_off',97))
                    prev_note[old_xm]=-1;prev_vol[old_xm]=0;prev_inst[old_xm]=0;prev_silent[old_xm]=True
            prev_ay_mode[ay_ch]=mode;prev_ay_xm[ay_ch]=xm_ch
            need_new=False
            if prev_note[xm_ch]<0:need_new=True
            elif note!=prev_note[xm_ch]:need_new=True
            elif inst!=prev_inst[xm_ch]:need_new=True
            elif prev_silent[xm_ch]:need_new=True
            elif xm_vol>prev_vol[xm_ch] and(xm_vol-prev_vol[xm_ch])>=16:need_new=True
            if need_new:
                if prev_note[xm_ch]>=0:
                    events.append(NoteEvent(frame,xm_ch,ay_ch,mode,'note_off',97))
                events.append(NoteEvent(frame,xm_ch,ay_ch,mode,'note_on',note,inst,xm_vol))
                prev_note[xm_ch]=note;prev_vol[xm_ch]=xm_vol;prev_inst[xm_ch]=inst
            prev_silent[xm_ch]=False
        if(player.state.current_position>=player.num_positions-1 and
           player.state.delay_counter==1 and player.channels[0].skip_counter==1 and frame>100):
            if player._b(player.channels[0].address)==0:break
    for xm_ch in range(max_xm):
        if prev_note[xm_ch]>=0:
            events.append(NoteEvent(frame,xm_ch,event_type='note_off',note=97))
    return events

# ═══════════════════════════════════════════════════════════════
# XM Writer
# ═══════════════════════════════════════════════════════════════

@dataclass
class TN:
    note:int=0;instrument:int=0;volume:int=0;effect:int=0;effect_param:int=0

class AYGen:
    P=32
    @classmethod
    def sq(cls):
        return[int(max(-1,min(1,sum(math.sin(2*math.pi*h*i/cls.P)/h for h in range(1,20,2))*1.2))*24000)for i in range(cls.P)]
    @classmethod
    def buzz(cls):
        p=cls.P*2;return[int(max(-1,min(1,math.sin(2*math.pi*i/p)*.5+math.sin(6*math.pi*i/p)*.3+math.sin(10*math.pi*i/p)*.2))*24000)for i in range(p)]
    @classmethod
    def lead(cls):
        return[int(max(-1,min(1,sum(math.sin(2*math.pi*i/cls.P*h)*(1./h**.8)for h in range(1,12))*.5))*24000)for i in range(cls.P)]
    @classmethod
    def nt(cls):
        import random;random.seed(42)
        return[int((math.sin(2*math.pi*i/cls.P)*.5+random.gauss(0,.3))*24000)for i in range(cls.P)]
    @staticmethod
    def noise(n=4096):
        s,l=[],1
        for _ in range(n):b=((l>>0)^(l>>1))&1;l=(l>>1)|(b<<14);s.append(24000 if l&1 else-24000)
        return s

class XMWriter:
    def build_and_write(self,fn,events,title="",bpm=125,speed=3,frame_rate=50,nc=6):
        if nc%2:nc+=1
        nc=max(4,min(32,nc))
        insts=self._mk_insts();rps=speed*2.5/bpm;fpr=max(1,int(frame_rate*rps))
        rows_data={}
        for ev in events:
            if ev.event_type not in('note_on','note_off'):continue
            row=ev.frame//fpr;ch=ev.channel
            if ch>=nc:continue
            key=(row,ch)
            if ev.event_type=='note_on':
                rows_data[key]=TN(max(1,min(96,ev.note)),min(ev.instrument,len(insts)),
                    0x10+min(0x40,max(0,ev.volume)))
            elif ev.event_type=='note_off':
                if key not in rows_data:rows_data[key]=TN(97)
        max_row=max((k[0]for k in rows_data),default=0)+1
        rpp=64;npat=max(1,min(256,(max_row+rpp-1)//rpp))
        patterns=[]
        for pi in range(npat):
            pat=[]
            for ri in range(rpp):
                row=[TN()for _ in range(nc)]
                for ch in range(nc):
                    key=(pi*rpp+ri,ch)
                    if key in rows_data:row[ch]=rows_data[key]
                pat.append(row)
            patterns.append(pat)
        self._write_xm(fn,title,nc,bpm,speed,patterns,insts,list(range(npat)))

    def _mk_insts(self):
        g=AYGen
        def mk(name,pcm,loop=True,per=1):
            os_=int(round(12*math.log2(per)))if per>1 else 0
            return{'name':name[:22],'samples':[{'name':name[:22],'data':pcm,
                'length':len(pcm),'loop_start':0,
                'loop_length':len(pcm)if loop else 0,
                'loop_type':1 if loop else 0,'volume':64,'finetune':0,
                'panning':128,'relative_note':os_,'bits':16}]}
        return[mk("AY Tone",g.sq()),mk("AY Env+Tone",g.lead()),
               mk("AY Buzzer",g.buzz(),per=2),mk("AY Tone+Noise",g.nt()),
               mk("AY Noise",g.noise(),loop=False)]

    def _write_xm(self,fn,title,nc,bpm,speed,patterns,insts,order):
        with open(fn,'wb') as f:
            f.write(b'Extended Module: ')
            f.write(title.encode('ascii','replace')[:20].ljust(20,b'\x00'))
            f.write(b'\x1a');f.write(b'PT32XM Converter    ')
            f.write(struct.pack('<H',0x0104));f.write(struct.pack('<I',276))
            f.write(struct.pack('<H',len(order)));f.write(struct.pack('<H',0))
            f.write(struct.pack('<H',nc));f.write(struct.pack('<H',len(patterns)))
            f.write(struct.pack('<H',min(len(insts),128)));f.write(struct.pack('<H',1))
            f.write(struct.pack('<H',speed));f.write(struct.pack('<H',bpm))
            ot=bytearray(256)
            for i,o in enumerate(order[:256]):ot[i]=o
            f.write(ot)
            for pat in patterns:
                pk=bytearray()
                for row in pat:
                    for ch in range(nc):
                        nd=row[ch]if ch<len(row)else TN()
                        hn=nd.note>0;hi=nd.instrument>0;hv=nd.volume>0
                        hf=nd.effect>0;hp=nd.effect_param>0
                        if not(hn or hi or hv or hf or hp):pk.append(0x80)
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
                f.write(struct.pack('<I',9));f.write(struct.pack('<B',0))
                f.write(struct.pack('<H',len(pat)));f.write(struct.pack('<H',len(pk)));f.write(pk)
            for inst in insts[:128]:
                samples=inst.get('samples',[])
                if not samples:
                    f.write(struct.pack('<I',29))
                    f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
                    f.write(struct.pack('<BH',0,0));continue
                ihs=263;f.write(struct.pack('<I',ihs))
                f.write(inst['name'].encode('ascii','replace')[:22].ljust(22,b'\x00'))
                f.write(struct.pack('<B',0));f.write(struct.pack('<H',len(samples)))
                f.write(struct.pack('<I',40));f.write(bytearray(96))
                ve=bytearray(48);struct.pack_into('<HH',ve,0,0,64);struct.pack_into('<HH',ve,4,100,64);f.write(ve)
                pe=bytearray(48);struct.pack_into('<HH',pe,0,0,32);struct.pack_into('<HH',pe,4,100,32);f.write(pe)
                f.write(bytes([2,2,0,0,1,0,0,1,1,0,0,0,0,0]));f.write(struct.pack('<H',0x800))
                rem=ihs-(4+22+1+2+4+96+48+48+14+2)
                if rem>0:f.write(b'\x00'*rem)
                for s in samples:
                    bps=2 if s.get('bits',16)==16 else 1
                    f.write(struct.pack('<I',len(s['data'])*bps))
                    f.write(struct.pack('<I',s['loop_start']*bps))
                    f.write(struct.pack('<I',s['loop_length']*bps))
                    f.write(struct.pack('<B',s.get('volume',64)))
                    f.write(struct.pack('<b',max(-128,min(127,s.get('finetune',0)))))
                    tb=s.get('loop_type',0)&3
                    if s.get('bits',16)==16:tb|=0x10
                    f.write(struct.pack('<B',tb));f.write(struct.pack('<B',s.get('panning',128)))
                    f.write(struct.pack('<b',max(-128,min(127,s.get('relative_note',0)))))
                    f.write(struct.pack('<B',0))
                    f.write(s.get('name','').encode('ascii','replace')[:22].ljust(22,b'\x00'))
                for s in samples:
                    prev=0
                    for v in s['data']:
                        v=max(-32768,min(32767,v));d_=(((v-prev)+32768)%65536-32768)
                        f.write(struct.pack('<h',d_));prev=v

# ═══════════════════════════════════════════════════════════════
# MIDI Writer
# ═══════════════════════════════════════════════════════════════

class MIDIWriter:
    def __init__(self):self.tpqn=480
    @staticmethod
    def _vlq(v):
        if v<0:v=0
        r=[v&0x7F];v>>=7
        while v:r.append((v&0x7F)|0x80);v>>=7
        r.reverse();return bytes(r)
    def write(self,fn,events,bpm=125,speed=3,frame_rate=50,title=""):
        tpqn=self.tpqn;tpf=tpqn*bpm/(60.0*frame_rate);usec=int(60_000_000/bpm)
        chs={}
        for ev in events:chs.setdefault(ev.channel,[]).append(ev)
        tracks=[];t0=[(0,bytes([0xFF,0x51,0x03,(usec>>16)&0xFF,(usec>>8)&0xFF,usec&0xFF]))]
        if title:td=title.encode('ascii',errors='replace')[:127];t0.append((0,bytes([0xFF,0x01,len(td)])+td))
        tracks.append(t0)
        gm_map={MODE_TONE:80,MODE_MIXED:81,MODE_ENV_TONE:84,MODE_BUZZER:87,MODE_NOISE:119}
        for ci,ch in enumerate(sorted(chs.keys())):
            if ci>=15:break
            mc=ci if ci<9 else ci+1
            modes=set(ev.mode for ev in chs[ch] if ev.mode)
            primary=modes.pop()if len(modes)==1 else MODE_TONE
            gm=gm_map.get(primary,80)
            trk=[(0,bytes([0xC0|mc,gm]))]
            nm=f"Ch{'ABC'[ch]if ch<3 else ch}".encode()[:127]
            trk.append((0,bytes([0xFF,0x03,len(nm)])+nm))
            act={}
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
            f.write(b'MThd');f.write(struct.pack('>I',6));f.write(struct.pack('>HHH',1,len(tracks),tpqn))
            for t in tracks:
                t.sort(key=lambda x:x[0]);d=bytearray();prev=0
                for at,ed in t:d.extend(self._vlq(max(0,at-prev)));d.extend(ed);prev=at
                d.extend(self._vlq(0));d.extend(b'\xFF\x2F\x00')
                f.write(b'MTrk');f.write(struct.pack('>I',len(d)));f.write(d)

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def convert_pt3(input_file, output_file=None, transpose=0, octave=0,
                finetune=0, fmt='xm', midi_file=None,
                channel_map_spec='default', raw_notes=False):
    base=os.path.splitext(input_file)[0]
    if not output_file:output_file=base+'.xm'
    with open(input_file,'rb') as f:data=f.read()

    channel_map=parse_channel_map(channel_map_spec)
    num_xm_ay=max(channel_map.values())+1

    print(f"Конвертация: {input_file}")
    player=PT3Player(data)
    print(f"  Title: {player.title}")
    print(f"  Author: {player.author}")
    print(f"  Version: {player.version}, ToneTable: {player.tone_table_id}")
    print(f"  Speed: {player.initial_delay}, Positions: {player.num_positions}")
    print(f"  Loop: {player.loop_position}")

    mode_str = "RAW (original PT3 notes)" if raw_notes else f"AY register analysis ({channel_map_spec})"
    print(f"  Mode: {mode_str}")

    if not raw_notes:
        print(f"  Channel mapping ({channel_map_spec}):")
        used=set()
        for(ay,mode),xm in sorted(channel_map.items()):
            print(f"    {get_channel_name(ay,mode):>8s} → XM ch {xm}");used.add(xm)
        print(f"  XM channels: {num_xm_ay}")

    total_rows=player.num_positions*64
    max_frames=total_rows*player.initial_delay+1000

    if raw_notes:
        num_xm=4  # 3 каналов + 1 запас
        print(f"\n  Running PT3 player (raw notes, {max_frames} max frames)...")
        events=extract_notes_raw(player,max_frames)
    else:
        num_xm=num_xm_ay
        print(f"\n  Running PT3 player (AY analysis, {max_frames} max frames)...")
        events=extract_notes_ay(player,channel_map,max_frames)

    note_ons=[e for e in events if e.event_type=='note_on']
    note_offs=[e for e in events if e.event_type=='note_off']
    print(f"  Notes: {len(note_ons)}, Note-off: {len(note_offs)}")

    if raw_notes:
        for ch in range(3):
            cn=[e for e in note_ons if e.channel==ch]
            if cn:
                ns=set(e.note for e in cn)
                print(f"    Ch {'ABC'[ch]}: {len(cn)} notes, "
                      f"{note_name_xm(min(ns))}-{note_name_xm(max(ns))}")
    else:
        xm_stats={}
        for e in note_ons:
            if e.channel not in xm_stats:
                xm_stats[e.channel]={'n':0,'min':999,'max':0,'modes':set()}
            xm_stats[e.channel]['n']+=1
            xm_stats[e.channel]['min']=min(xm_stats[e.channel]['min'],e.note)
            xm_stats[e.channel]['max']=max(xm_stats[e.channel]['max'],e.note)
            if e.mode:xm_stats[e.channel]['modes'].add(e.mode)
        for xm_ch in sorted(xm_stats):
            s=xm_stats[xm_ch];ms='/'.join(sorted(s['modes']))
            print(f"    XM ch {xm_ch}: {s['n']} notes, "
                  f"{note_name_xm(s['min'])}-{note_name_xm(s['max'])}, {ms}")

    if not note_ons:print("  No notes!");return

    print(f"\n  First 25 notes:")
    for e in note_ons[:25]:
        t=e.frame/50.0
        if raw_notes:
            print(f"    f={e.frame:5d} ({t:6.2f}s) ch={'ABC'[e.channel]} "
                  f"{note_name_xm(e.note):>4s} v={e.volume}")
        else:
            print(f"    f={e.frame:5d} ({t:6.2f}s) xm={e.channel} "
                  f"ay={'ABC'[e.ay_channel]} {e.mode:>7s} "
                  f"{note_name_xm(e.note):>4s} i={e.instrument} v={e.volume}")

    bpm=125;speed=player.initial_delay

    if fmt in('xm','both'):
        xf=output_file if output_file.endswith('.xm') else base+'.xm'
        xm=XMWriter();xm.build_and_write(xf,events,player.title,bpm,speed,50,num_xm)
        print(f"\n  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {num_xm} ch)")

    if fmt in('midi','both') or midi_file:
        mf=midi_file or base+'.mid'
        mw=MIDIWriter();mw.write(mf,events,bpm,speed,50,player.title)
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")

    dur=max(e.frame for e in events)/50.0
    print(f"\n  Total: {len(note_ons)} notes, {dur:.1f}s")


def main():
    import argparse
    preset_help="\n".join(f"    {n}: {max(m.values())+1} ch" for n,m in CHANNEL_PRESETS.items())
    ap=argparse.ArgumentParser(
        description='PT3→XM/MIDI v6 (raw notes + channel mapping)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Note extraction modes:
  --raw-notes    Original PT3 notes as-is, no ornaments/arpeggio.
                 3 channels (A, B, C), direct from pattern data.
                 Best for clean MIDI export.

  (default)      AY register analysis with mode detection.
                 Detects tone/buzzer/noise/envelope modes.
                 Uses --channel-map for XM channel assignment.

Channel map presets (--channel-map):
{preset_help}

Custom: "AY_CH:MODE=XM_CH,..."  (e.g. "A:tone=0,A:buzz=3")

Examples:
  python pt32xm.py music.pt3 --raw-notes --midi
  python pt32xm.py music.pt3 --channel-map default
  python pt32xm.py music.pt3 --raw-notes -o clean.xm
  python pt32xm.py music.pt3 --channel-map compact --midi
""")
    ap.add_argument('input',nargs='*')
    ap.add_argument('-o','--output')
    ap.add_argument('--octave',type=int,default=0)
    ap.add_argument('--transpose',type=int,default=0)
    ap.add_argument('--finetune',type=int,default=0)
    ap.add_argument('--midi',action='store_true')
    ap.add_argument('--midi-only',action='store_true')
    ap.add_argument('--midi-file',type=str,default=None)
    ap.add_argument('--channel-map',type=str,default='default')
    ap.add_argument('--raw-notes',action='store_true',
        help='Extract original PT3 notes without ornaments/arpeggio (3 channels)')
    ap.add_argument('--list-presets',action='store_true')
    args=ap.parse_args()

    if args.list_presets:
        print("Available channel mapping presets:\n")
        for name,mapping in CHANNEL_PRESETS.items():
            nc=max(mapping.values())+1
            print(f"  {name} ({nc} XM channels):")
            for(ay,mode),xm in sorted(mapping.items()):
                print(f"    {get_channel_name(ay,mode):>10s} → XM {xm}")
            print()
        sys.exit(0)

    if not args.input:ap.print_help();sys.exit(1)
    if args.output and len(args.input)>1:print("--output: single file only");sys.exit(1)
    try:channel_map=parse_channel_map(args.channel_map)
    except ValueError as e:print(f"Channel map error: {e}");sys.exit(1)
    fmt='midi' if args.midi_only else('both' if args.midi or args.midi_file else 'xm')

    for inp in args.input:
        if not os.path.exists(inp):print(f"Not found: {inp}");continue
        try:
            convert_pt3(inp,args.output,args.transpose,args.octave,
                        args.finetune,fmt,args.midi_file,args.channel_map,
                        args.raw_notes)
        except Exception as e:
            import traceback;print(f"Error: {e}");traceback.print_exc()
    print("\nГотово!")

if __name__=='__main__':main()