"""
PT3 to XM/MIDI/WAV Converter v7
Based on pt3player.c by Bulba/Volutar
With channel mapping, --raw-notes mode, and WAV rendering
"""

import struct, sys, os, math, array
import numpy as np
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
    if 1<=n<=96:v=n-1;return f"{NOTE_NAMES[v%12]}{v//12}"
    return "---"
def xm_note_to_midi(n): return n+11

# ═══════════════════════════════════════════════════════════════
# AY Modes & Channel mapping
# ═══════════════════════════════════════════════════════════════
MODE_TONE='tone';MODE_NOISE='noise';MODE_BUZZER='buzzer'
MODE_MIXED='mixed';MODE_ENV_TONE='env_tone'
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

def get_channel_name(ay,mode):
    cn=['A','B','C'][ay]if ay<3 else str(ay)
    mn={MODE_TONE:'Tone',MODE_NOISE:'Noise',MODE_BUZZER:'Buzz',MODE_MIXED:'T+N',MODE_ENV_TONE:'T+E'}
    return f"{cn}-{mn.get(mode,mode)}"

def parse_channel_map(spec):
    if spec in CHANNEL_PRESETS:return CHANNEL_PRESETS[spec]
    ma={'t':MODE_TONE,'tone':MODE_TONE,'n':MODE_NOISE,'noise':MODE_NOISE,
        'b':MODE_BUZZER,'buzz':MODE_BUZZER,'buzzer':MODE_BUZZER,
        'm':MODE_MIXED,'mix':MODE_MIXED,'mixed':MODE_MIXED,
        'e':MODE_ENV_TONE,'env':MODE_ENV_TONE,'env_tone':MODE_ENV_TONE}
    ca={'a':0,'b':1,'c':2,'0':0,'1':1,'2':2}
    result={}
    for part in spec.split(','):
        part=part.strip()
        if not part:continue
        if'='not in part or':'not in part:raise ValueError(f"Bad: '{part}'")
        left,right=part.split('=',1);ay_str,mode_str=left.split(':',1)
        ay_ch=ca.get(ay_str.strip().lower());mode=ma.get(mode_str.strip().lower())
        if ay_ch is None:raise ValueError(f"Unknown AY ch: '{ay_str}'")
        if mode is None:raise ValueError(f"Unknown mode: '{mode_str}'")
        result[(ay_ch,mode)]=int(right.strip())
    for ay_ch in range(3):
        for mode in ALL_MODES:
            if(ay_ch,mode)not in result:
                result[(ay_ch,mode)]=result.get((ay_ch,MODE_TONE),ay_ch)
    return result

# ═══════════════════════════════════════════════════════════════
# AY-3-8910 Emulator (based on ayumi.c)
# ═══════════════════════════════════════════════════════════════

AY_DAC = [
    0.0,0.0,0.00999465934234,0.00999465934234,
    0.0144502937362,0.0144502937362,0.0210574502174,0.0210574502174,
    0.0307011520562,0.0307011520562,0.0455481803616,0.0455481803616,
    0.0644998855573,0.0644998855573,0.107362478065,0.107362478065,
    0.126588845655,0.126588845655,0.20498970016,0.20498970016,
    0.292210269322,0.292210269322,0.372838941024,0.372838941024,
    0.492530708782,0.492530708782,0.635324635691,0.635324635691,
    0.805584802014,0.805584802014,1.0,1.0]

YM_DAC = [
    0.0,0.0,0.00465400167849,0.00772106507973,
    0.0109559777218,0.0139620050355,0.0169985503929,0.0200198367285,
    0.024368657969,0.029694056611,0.0350652323186,0.0403906309606,
    0.0485389486534,0.0583352407111,0.0680552376593,0.0777752346075,
    0.0925154497597,0.111085679408,0.129747463188,0.148485542077,
    0.17666895552,0.211551079576,0.246387426566,0.281101701381,
    0.333730067903,0.400427252613,0.467383840696,0.53443198291,
    0.635172045472,0.75800717174,0.879926756695,1.0]

def _lfsr_sequence():
    """One full period of the AY noise LFSR (17 bit, taps 0 and 3), output bit = bit 0."""
    n = 1
    out = bytearray(131071)
    for i in range(131071):
        bit = (n ^ (n >> 3)) & 1
        n = (n >> 1) | (bit << 16)
        out[i] = n & 1
    return out

_LFSR = None

def get_lfsr():
    global _LFSR
    if _LFSR is None:
        _LFSR = np.frombuffer(_lfsr_sequence(), dtype=np.uint8)
    return _LFSR

def env_level(shape, s):
    """Envelope level 0..31 after s envelope steps since the shape was written (vectorised)."""
    seg = s // 32
    pos = s % 32
    if not shape & 8:
        up = bool(shape & 4)
        return np.where(seg == 0, pos if up else 31 - pos, 0)
    up = bool(shape & 4)
    alt = bool(shape & 2)
    hold = bool(shape & 1)
    if hold:
        final = 31 if (up != alt) else 0
        first = pos if up else 31 - pos
        return np.where(seg == 0, first, final)
    up_seg = np.logical_xor(up, np.logical_and(alt, (seg & 1) == 1))
    return np.where(up_seg, pos, 31 - pos)


class AYEmulator:
    """AY-3-8910 / YM2149 emulator (numpy, one PT3 frame at a time).

    The chip is stepped at clock/8 (tone and noise dividers are exact), the
    result is averaged over every output sample (box filter).
    """
    # (left, right) gains of channels A, B, C
    PAN = {'abc': ((0.95, 0.27), (0.65, 0.62), (0.62, 1.03)),   # fitted to zxtune123 (ABC)
           'mono': ((0.7, 0.7), (0.7, 0.7), (0.7, 0.7))}

    def __init__(self, clock_rate=1773400, sample_rate=44100, is_ym=False, fps=50.0,
                 layout='abc'):
        get_lfsr()
        self.clock = clock_rate
        self.sr = sample_rate
        self.dac = np.array(YM_DAC if is_ym else AY_DAC)
        self.tick_rate = clock_rate / 8.0
        self.ticks_per_frame = self.tick_rate / fps
        self.samples_per_frame = sample_rate / fps
        self.pan = np.array(self.PAN[layout])        # 3 x 2
        self.tcnt = [0, 0, 0]
        self.tout = [0, 0, 0]
        self.ncnt = 0
        self.nidx = 0
        self.ecnt = 0
        self.estep = 0
        self.eshape = 0
        self.tick_pos = 0.0           # fractional tick position carried between frames
        self.sample_pos = 0.0
        self.regs = [0] * 14
        self.chan_rms = [0.0, 0.0, 0.0]   # AC rms of each channel in the last frame

    def set_registers(self, regs):
        self.regs = list(regs)
        if regs[13] != 0xFF:
            self.eshape = regs[13] & 0x0F
            self.ecnt = 0
            self.estep = 0

    def render_frame(self):
        """Render one frame with the current registers -> (n, 2) float array."""
        r = self.regs
        t0 = self.tick_pos
        t1 = t0 + self.ticks_per_frame
        n = int(t1) - int(t0)
        self.tick_pos = t1
        k = np.arange(1, n + 1, dtype=np.int64)
        mixer = r[7]
        noise_p = max(1, r[6] & 0x1F) * 2
        c = self.ncnt + k
        shifts = c // noise_p
        noise = _LFSR[(self.nidx + shifts) % len(_LFSR)]
        self.nidx = int((self.nidx + (self.ncnt + n) // noise_p) % len(_LFSR))
        self.ncnt = int((self.ncnt + n) % noise_p)
        ep = max(1, (r[12] << 8) | r[11])
        steps = (self.ecnt + k) // ep
        env = env_level(self.eshape, self.estep + steps)
        tot = self.ecnt + n
        self.estep = min(self.estep + tot // ep, 1 << 20)
        self.ecnt = int(tot % ep)
        out = np.zeros((n, 2))
        for i in range(3):
            p = max(1, ((r[i * 2 + 1] & 0x0F) << 8) | r[i * 2])
            cnt = min(self.tcnt[i], p - 1)
            cc = cnt + k
            tone = np.bitwise_xor(self.tout[i], (cc // p) & 1)
            self.tout[i] = int(tone[-1]) if n else self.tout[i]
            self.tcnt[i] = int(cc[-1] % p) if n else cnt
            t_dis = (mixer >> i) & 1
            n_dis = (mixer >> (i + 3)) & 1
            gate = np.logical_or(tone, t_dis) & np.logical_or(noise, n_dis)
            va = r[8 + i]
            if va & 0x10:
                lvl = env
            else:
                lvl = (va & 0x0F) * 2 + 1
            v = self.dac[np.where(gate, lvl, 0)] if va & 0x10 else self.dac[lvl] * gate
            self.chan_rms[i] = float(v.std()) if n else 0.0
            out += v[:, None] * self.pan[i][None, :]
        # box-average ticks into output samples
        s0 = self.sample_pos
        s1 = s0 + self.samples_per_frame
        ns = int(s1) - int(s0)
        self.sample_pos = s1
        cs = np.concatenate(([[0.0, 0.0]], np.cumsum(out, axis=0)))
        edges = np.round(np.arange(ns + 1) * (n / max(1, ns))).astype(np.int64)
        edges = np.clip(edges, 0, n)
        cnts = np.maximum(1, np.diff(edges))[:, None]
        return (cs[edges[1:]] - cs[edges[:-1]]) / cnts


# ═══════════════════════════════════════════════════════════════
# PT3 Player (faithful port of pt3player.c)
# ═══════════════════════════════════════════════════════════════

# PT 3.3/3.4 volume table: amp*step>>8 (steps fitted against zxtune123 register dumps; v<5 extrapolated)
VT34_STEP = [0,18,36,54,72,94,111,132,145,161,174,194,207,225,247,256]

@dataclass
class ChannelState:
    address:int=0;orn_ptr:int=0;smp_ptr:int=0;tone:int=0
    loop_orn:int=0;orn_len:int=0;pos_orn:int=0
    loop_smp:int=0;smp_len:int=0;pos_smp:int=0
    volume:int=15;notes_to_skip:int=1;note:int=0
    slide_to_note:int=0;amplitude:int=0
    env_enabled:bool=False;enabled:bool=False;simple_gliss:bool=True
    cur_amp_slide:int=0;cur_noise_slide:int=0;cur_env_slide:int=0
    ton_slide_count:int=0;cur_onoff:int=0;onoff_delay:int=0;offon_delay:int=0
    ton_slide_delay:int=0;cur_ton_sliding:int=0;ton_accum:int=0
    ton_slide_step:int=0;ton_delta:int=0;skip_counter:int=1
    note_changed:bool=False;just_disabled:bool=False

@dataclass
class PT3State:
    env_base:int=0;cur_env_slide:int=0;env_slide_add:int=0
    cur_env_delay:int=0;env_delay:int=0;noise_base:int=0
    delay:int=6;add_to_noise:int=0;delay_counter:int=1;current_position:int=0

class PT3Player:
    def __init__(self,data):
        self.d=bytearray(data);self.version=6;self.tone_table_id=0
        self.channels=[ChannelState()for _ in range(3)]
        self.state=PT3State();self.ay=[0]*14;self.temp_mixer=0;self.add_to_env=0
        self._parse_header();self._init_playback()
    def _b(self,p):return self.d[p]if p<len(self.d)else 0
    def _w(self,p):return(self.d[p]|(self.d[p+1]<<8))if p+1<len(self.d)else 0
    def _sw(self,p):v=self._w(p);return v-65536 if v>=32768 else v
    def _parse_header(self):
        d=self.d;v=self._b(0x0D)
        self.version=v-0x30 if 0x30<=v<=0x39 else 6
        self.title=bytes(d[0x1E:0x3E]).decode('ascii','replace').rstrip('\x00 ')
        self.author=bytes(d[0x42:0x62]).decode('ascii','replace').rstrip('\x00 ')
        self.tone_table_id=self._b(0x63);self.initial_delay=self._b(0x64)
        if self.initial_delay==0:self.initial_delay=6
        i=0
        while i<65535-201:
            if self._b(0xC9+i)==255:break
            i+=1
        self.num_positions=min(i,self._b(0x65))if self._b(0x65)>0 else i
        self.loop_position=self._b(0x66);self.pat_ptr=self._w(0x67)
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
            ch=self.channels[abc];ch.address=self._w(self.pat_ptr+(i+abc)*2)
            ch.orn_ptr=self.orn_ptrs[0];ch.loop_orn=self._b(ch.orn_ptr)
            ch.orn_len=self._b(ch.orn_ptr+1);ch.orn_ptr+=2
            ch.smp_ptr=self.smp_ptrs[1];ch.loop_smp=self._b(ch.smp_ptr)
            ch.smp_len=self._b(ch.smp_ptr+1);ch.smp_ptr+=2
            ch.volume=15;ch.skip_counter=1;ch.notes_to_skip=1
            ch.enabled=False;ch.env_enabled=False;ch.note=0;ch.tone=0
    def pattern_interpreter(self,abc):
        ch=self.channels[abc];st=self.state
        pr_note=ch.note;pr_sliding=ch.cur_ton_sliding
        ef=[];quit=False;ch.note_changed=False;ch.just_disabled=False
        while not quit and ch.address<len(self.d):
            op=self._b(ch.address)
            if 0xF0<=op<=0xFF:
                oi=op-0xF0;ch.orn_ptr=self.orn_ptrs[oi]
                ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                ch.orn_ptr+=2;ch.pos_orn=0;ch.address+=1
                si=self._b(ch.address)//2;ch.smp_ptr=self.smp_ptrs[si]
                ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1)
                ch.smp_ptr+=2;ch.env_enabled=False
            elif 0xD1<=op<=0xEF:
                if op<=0xDF:
                    si=op-0xD0;ch.smp_ptr=self.smp_ptrs[si]
                    ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1);ch.smp_ptr+=2
                else:
                    oi=op-0xE0;ch.orn_ptr=self.orn_ptrs[oi]
                    ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                    ch.orn_ptr+=2;ch.pos_orn=0
            elif op==0xD0:quit=True
            elif 0xC1<=op<=0xCF:ch.volume=op-0xC0
            elif op==0xC0:
                ch.pos_smp=0;ch.cur_amp_slide=0;ch.cur_noise_slide=0;ch.cur_env_slide=0
                ch.pos_orn=0;ch.ton_slide_count=0;ch.cur_ton_sliding=0;ch.ton_accum=0
                ch.cur_onoff=0;ch.enabled=False;ch.just_disabled=True;quit=True
            elif 0xB2<=op<=0xBF:
                ch.env_enabled=True;self.ay[13]=op-0xB1
                ch.address+=1;hi=self._b(ch.address);ch.address+=1;lo=self._b(ch.address)
                st.env_base=(hi<<8)|lo;ch.pos_orn=0;st.cur_env_slide=0;st.cur_env_delay=0
            elif op==0xB1:ch.address+=1;ch.notes_to_skip=self._b(ch.address)
            elif op==0xB0:ch.env_enabled=False;ch.pos_orn=0
            elif 0x50<=op<=0xAF:
                ch.note=op-0x50;ch.pos_smp=0;ch.cur_amp_slide=0;ch.cur_noise_slide=0
                ch.cur_env_slide=0;ch.pos_orn=0;ch.ton_slide_count=0;ch.cur_ton_sliding=0
                ch.ton_accum=0;ch.cur_onoff=0;ch.enabled=True;ch.note_changed=True;quit=True
            elif 0x40<=op<=0x4F:
                oi=op-0x40;ch.orn_ptr=self.orn_ptrs[oi]
                ch.loop_orn=self._b(ch.orn_ptr);ch.orn_len=self._b(ch.orn_ptr+1)
                ch.orn_ptr+=2;ch.pos_orn=0
            elif 0x20<=op<=0x3F:st.noise_base=op-0x20
            elif 0x11<=op<=0x1F:
                self.ay[13]=op-0x10;ch.address+=1;hi=self._b(ch.address)
                ch.address+=1;lo=self._b(ch.address);st.env_base=(hi<<8)|lo
                st.cur_env_slide=0;st.cur_env_delay=0;ch.env_enabled=True
                ch.address+=1;si=self._b(ch.address)//2;ch.smp_ptr=self.smp_ptrs[si]
                ch.loop_smp=self._b(ch.smp_ptr);ch.smp_len=self._b(ch.smp_ptr+1)
                ch.smp_ptr+=2;ch.pos_orn=0
            elif op==0x10:
                ch.env_enabled=False;ch.address+=1;si=self._b(ch.address)//2
                ch.smp_ptr=self.smp_ptrs[si];ch.loop_smp=self._b(ch.smp_ptr)
                ch.smp_len=self._b(ch.smp_ptr+1);ch.smp_ptr+=2;ch.pos_orn=0
            elif 1<=op<=9:ef.append(op)
            ch.address+=1
        for e in reversed(ef):
            if e==1:
                ch.ton_slide_delay=self._b(ch.address);ch.address+=1
                ch.ton_slide_count=ch.ton_slide_delay
                ch.ton_slide_step=self._sw(ch.address);ch.address+=2
                ch.simple_gliss=True;ch.cur_onoff=0
            elif e==2:
                ch.simple_gliss=False;ch.cur_onoff=0
                ch.ton_slide_delay=self._b(ch.address);ch.address+=1
                ch.ton_slide_count=ch.ton_slide_delay
                ch.ton_slide_step=abs(self._sw(ch.address+2))
                ch.ton_delta=self.get_note_freq(ch.note)-self.get_note_freq(pr_note)
                ch.slide_to_note=ch.note;ch.note=pr_note
                if self.version>=6:ch.cur_ton_sliding=pr_sliding
                if ch.ton_delta-ch.cur_ton_sliding<0:ch.ton_slide_step=-ch.ton_slide_step
                ch.address+=4
            elif e==3:ch.pos_smp=self._b(ch.address);ch.address+=1
            elif e==4:ch.pos_orn=self._b(ch.address);ch.address+=1
            elif e==5:
                ch.onoff_delay=self._b(ch.address);ch.address+=1
                ch.offon_delay=self._b(ch.address);ch.address+=1
                ch.cur_onoff=ch.onoff_delay;ch.ton_slide_count=0;ch.cur_ton_sliding=0
            elif e==8:
                st.env_delay=self._b(ch.address);ch.address+=1;st.cur_env_delay=st.env_delay
                st.env_slide_add=self._sw(ch.address);ch.address+=2
            elif e==9:self.state.delay=self._b(ch.address);ch.address+=1
        ch.skip_counter=ch.notes_to_skip
    def change_registers(self,abc):
        ch=self.channels[abc];st=self.state
        if ch.enabled:
            sb=ch.smp_ptr+ch.pos_smp*4;b0=self._b(sb);b1=self._b(sb+1)
            ch.tone=self._w(sb+2)+ch.ton_accum
            if b1&0x40:ch.ton_accum=ch.tone
            ov=self._b(ch.orn_ptr+ch.pos_orn)
            if ov>=128:ov-=256
            j=ch.note+ov
            if j<0:j=0
            elif j>95:j=95
            w=self.get_note_freq(j);ch.tone=(ch.tone+ch.cur_ton_sliding+w)&0xFFF
            if ch.ton_slide_count>0:
                ch.ton_slide_count-=1
                if ch.ton_slide_count==0:
                    ch.cur_ton_sliding+=ch.ton_slide_step;ch.ton_slide_count=ch.ton_slide_delay
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
            ch.amplitude=((ch.amplitude*VT34_STEP[ch.volume])>>8) if self.version<=4 else (ch.volume*ch.amplitude+8)//15
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
        for abc in range(3):self.channels[abc].note_changed=False;self.channels[abc].just_disabled=False
        st.delay_counter-=1
        if st.delay_counter==0:
            self.channels[0].skip_counter-=1
            if self.channels[0].skip_counter==0:
                if self._b(self.channels[0].address)==0:
                    st.current_position+=1
                    if st.current_position>=self.num_positions:st.current_position=self.loop_position
                    i=self.positions[st.current_position]
                    for abc in range(3):self.channels[abc].address=self._w(self.pat_ptr+(i+abc)*2)
                    st.noise_base=0
                self.pattern_interpreter(0)
            for abc in range(1,3):
                self.channels[abc].skip_counter-=1
                if self.channels[abc].skip_counter==0:self.pattern_interpreter(abc)
            st.delay_counter=st.delay
        self.add_to_env=0;self.temp_mixer=0
        self.change_registers(0);self.change_registers(1);self.change_registers(2)
        self.ay[0]=self.channels[0].tone&0xFF;self.ay[1]=self.channels[0].tone>>8
        self.ay[2]=self.channels[1].tone&0xFF;self.ay[3]=self.channels[1].tone>>8
        self.ay[4]=self.channels[2].tone&0xFF;self.ay[5]=self.channels[2].tone>>8
        self.ay[6]=(st.noise_base+st.add_to_noise)&0x1F;self.ay[7]=self.temp_mixer
        self.ay[8]=self.channels[0].amplitude;self.ay[9]=self.channels[1].amplitude
        self.ay[10]=self.channels[2].amplitude
        env=(st.env_base+self.add_to_env+st.cur_env_slide)&0xFFFF
        self.ay[11]=env&0xFF;self.ay[12]=env>>8
        if st.cur_env_delay>0:
            st.cur_env_delay-=1
            if st.cur_env_delay==0:st.cur_env_delay=st.env_delay;st.cur_env_slide+=st.env_slide_add
        return list(self.ay)

# ═══════════════════════════════════════════════════════════════
# WAV Renderer
# ═══════════════════════════════════════════════════════════════

def pt3_frames(player, max_frames=50000):
    """Run the PT3 player: yields (registers, note_started[3]) once per 50 Hz frame."""
    for frame in range(max_frames):
        regs = player.play_tick()
        yield regs, [player.channels[i].note_changed for i in range(3)]
        if player_finished(player, frame): break

def render_wav(regs_frames, filename, sample_rate=44100, clock_rate=1773400,
               is_ym=False, stereo=True, volume=0.8, fps=50.0, layout='abc'):
    """Render register frames (iterable of 14 register values per frame) to WAV."""
    ay = AYEmulator(clock_rate, sample_rate, is_ym, fps, layout)
    chunks = []
    print(f"  Rendering WAV: {sample_rate}Hz, {'stereo' if stereo else 'mono'}, "
          f"{'YM' if is_ym else 'AY'} chip, {clock_rate} Hz, {fps:g} fps...")
    for regs in regs_frames:
        ay.set_registers(regs)
        chunks.append(ay.render_frame())
    pcm = np.concatenate(chunks)
    # the DAC is unipolar: volume changes leave slow DC steps. Real players remove
    # them with a high-pass; a zero-phase 1st order 20 Hz filter (in the spectrum) does the same
    spec = np.fft.rfft(pcm, axis=0)
    fr = np.fft.rfftfreq(len(pcm), 1.0 / sample_rate)
    spec *= (fr / np.sqrt(fr * fr + 20.0 ** 2))[:, None]
    pcm = np.fft.irfft(spec, n=len(pcm), axis=0)
    peak = float(np.abs(pcm).max()) or 1.0
    pcm = pcm * (volume / peak)
    if not stereo:
        pcm = pcm.mean(axis=1, keepdims=True)
    data = np.clip(pcm * 32767, -32768, 32767).astype('<i2')
    nch = data.shape[1]
    with open(filename, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + data.nbytes) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 1, nch, sample_rate,
                                       sample_rate * nch * 2, nch * 2, 16))
        f.write(b'data' + struct.pack('<I', data.nbytes))
        f.write(data.tobytes())
    print(f"    Rendered {len(data)} samples ({len(data)/sample_rate:.1f}s)")
    return len(data) / sample_rate

# ═══════════════════════════════════════════════════════════════
# Instruments (waveforms of the AY itself)
# ═══════════════════════════════════════════════════════════════

P_SQ = 0.62          # peak of the square sample (headroom for louder timbres)
FPS = 50             # PT3 player rate (ZX Spectrum interrupt)

def _pcm(vals, peak):
    """float list -> int16 list: DC removed, scaled so max |x| = peak."""
    a = np.array(vals, dtype=float)
    a = a - a.mean()
    m = float(np.abs(a).max()) or 1.0
    return [int(round(x)) for x in a * (peak * 32767.0 / m)]

class AYGen:
    """Samples for XM instruments: one period of what the chip really outputs."""
    @staticmethod
    def build(is_ym=False):
        dac = YM_DAC if is_ym else AY_DAC
        lf = _LFSR if _LFSR is not None else np.frombuffer(_lfsr_sequence(), dtype=np.uint8)
        square = _pcm([1] * 16 + [-1] * 16, P_SQ)
        saw = _pcm([dac[31 - i] for i in range(32)], 0.95)
        tri = _pcm([dac[31 - i] for i in range(32)] + [dac[i] for i in range(32)], 0.95)
        noise = _pcm([int(b) for b in lf[:32768]], P_SQ)
        mixed = _pcm([float(lf[i * 7 % 4000]) for i in range(32)] + [0.0] * 32, 0.95)
        def inst(name, data, rel=0, loop=True):
            rms = float(np.sqrt(np.mean(np.square(data, dtype=float)))) / 32767.0
            return {'name': name, 'data': data, 'rel': rel, 'loop': loop, 'rms': rms}
        return [inst('AY Tone', square), inst('AY Env+Tone', square),
                inst('AY Buzzer saw', saw), inst('AY Tone+Noise', mixed, 12),
                inst('AY Noise', noise), inst('AY Buzzer tri', tri, 12)]

INST_TONE, INST_ENVTONE, INST_BUZZ, INST_MIXED, INST_NOISE, INST_BUZZ_TRI = 1, 2, 3, 4, 5, 6
_INST_CACHE = {}
def instruments(is_ym=False):
    if is_ym not in _INST_CACHE:
        _INST_CACHE[is_ym] = AYGen.build(is_ym)
    return _INST_CACHE[is_ym]

def xm_volume(r, inst, is_ym, scale=1.0):
    """AC rms of a channel (1.0 = full-level square has 0.5) -> XM volume 1..64."""
    rms = instruments(is_ym)[inst - 1]['rms']
    v = 64.0 * r * (2.0 * P_SQ) / rms * scale
    return max(1, min(64, int(round(v))))

# ═══════════════════════════════════════════════════════════════
# Note extraction (raw & AY modes)
# ═══════════════════════════════════════════════════════════════

CLOCK = 1773400
REPEATING_ENV = {0x08, 0x0A, 0x0C, 0x0E}

def clamp_xm(n):
    return n if 1 <= n <= 96 else None

def freq_to_xm(freq):
    if freq < 15 or freq > 20000: return None
    return clamp_xm(int(round(69 + 12 * math.log2(freq / 440.0))) - 11)

def rate_to_xm(rate):
    """Playback rate (samples/s) of a looped 1-sample-per-step wave -> XM note."""
    return max(1, min(96, int(round(49 + 12 * math.log2(max(rate, 1.0) / 8363.0)))))

@dataclass
class NoteEvent:
    frame: int = 0; channel: int = 0; ay_channel: int = 0; mode: str = ""
    event_type: str = ""; note: int = 0; instrument: int = 1; volume: int = 64

def extract_notes_raw(player, max_frames=50000):
    events = []; prev_note = [-1, -1, -1]
    for frame in range(max_frames):
        player.play_tick()
        for ch in range(3):
            c = player.channels[ch]
            if c.just_disabled:
                if prev_note[ch] >= 0:
                    events.append(NoteEvent(frame, ch, ch, '', 'note_off', 97)); prev_note[ch] = -1
            elif c.note_changed and c.enabled:
                xv = max(1, int(64 * c.volume / 15))
                events.append(NoteEvent(frame, ch, ch, MODE_TONE, 'note_on', max(1, min(96, c.note + 1)), 1, xv))
                prev_note[ch] = c.note
        if player_finished(player, frame): break
    for ch in range(3):
        if prev_note[ch] >= 0: events.append(NoteEvent(frame + 1, ch, ch, '', 'note_off', 97))
    return events

def player_finished(player, frame):
    return (player.state.current_position >= player.num_positions - 1 and
            player.state.delay_counter == 1 and player.channels[0].skip_counter == 1 and
            frame > 100 and player._b(player.channels[0].address) == 0)

def classify(regs, ac, clock):
    """What does AY channel `ac` play this frame?  -> (mode, xm_note, instrument) or None."""
    mixer = regs[7]; vr = regs[8 + ac]
    tone_on = not (mixer >> ac) & 1
    noise_on = not (mixer >> (ac + 3)) & 1
    env_on = bool(vr & 0x10)
    period = ((regs[ac * 2 + 1] & 0x0F) << 8) | regs[ac * 2]
    if tone_on:
        note = freq_to_xm(clock / (16.0 * max(1, period)))
        if note is None: return None
        if env_on: return MODE_ENV_TONE, note, INST_ENVTONE
        if noise_on: return MODE_MIXED, note, INST_MIXED
        return MODE_TONE, note, INST_TONE
    if noise_on:
        np_ = max(1, regs[6] & 0x1F)
        return MODE_NOISE, rate_to_xm(clock / (16.0 * np_)), INST_NOISE
    return None   # buzzer (envelope only) is recognised by the caller, it needs the last shape

def extract_notes_ay(frames, channel_map, clock=CLOCK, is_ym=False, scale=1.0):
    """frames: iterable of (registers, note_started[3]) per 50 Hz frame."""
    emu = AYEmulator(clock, 44100, is_ym)
    nx = max(channel_map.values()) + 1
    cur = [None] * nx                 # per XM channel: dict(note, inst, vol)
    last_xc = [-1] * 3
    events = []; shape = 0
    def gx(a, m):
        if (a, m) in channel_map: return channel_map[(a, m)]
        return a
    frame = -1
    for frame, (regs, started) in enumerate(frames):
        emu.set_registers(regs); emu.render_frame()
        if regs[13] != 0xFF: shape = regs[13] & 0x0F
        ep = max(1, (regs[12] << 8) | regs[11])
        for ac in range(3):
            info = classify(regs, ac, clock)
            vr = regs[8 + ac]
            mixer = regs[7]
            if info is None and (vr & 0x10) and (mixer >> ac) & 1 and (mixer >> (ac + 3)) & 1 \
                    and shape in REPEATING_ENV:
                tri = shape in (0x0A, 0x0E)
                f = clock / ((512.0 if tri else 256.0) * ep)
                n = freq_to_xm(f)
                if n is not None: info = (MODE_BUZZER, n, INST_BUZZ_TRI if tri else INST_BUZZ)
            r = emu.chan_rms[ac]
            xc_old = last_xc[ac]
            if info is None or r < 1e-4:
                if xc_old >= 0 and cur[xc_old] is not None:
                    events.append(NoteEvent(frame, xc_old, ac, '', 'note_off', 97)); cur[xc_old] = None
                last_xc[ac] = -1
                continue
            mode, note, inst = info
            xc = gx(ac, mode)
            if xc_old >= 0 and xc_old != xc and cur[xc_old] is not None:
                events.append(NoteEvent(frame, xc_old, ac, '', 'note_off', 97)); cur[xc_old] = None
            last_xc[ac] = xc
            vol = xm_volume(r, inst, is_ym, scale)
            c = cur[xc]
            trig = (c is None or c['inst'] != inst or started[ac] or
                    (mode in (MODE_BUZZER, MODE_ENV_TONE) and regs[13] != 0xFF))
            if trig:
                events.append(NoteEvent(frame, xc, ac, mode, 'note_on', note, inst, vol))
                cur[xc] = {'note': note, 'inst': inst, 'vol': vol}
            else:
                if note != c['note']:
                    events.append(NoteEvent(frame, xc, ac, mode, 'pitch', note, inst, vol)); c['note'] = note
                if vol != c['vol']:
                    events.append(NoteEvent(frame, xc, ac, mode, 'vol', note, inst, vol)); c['vol'] = vol
    for xc in range(nx):
        if cur[xc] is not None: events.append(NoteEvent(frame + 1, xc, 0, '', 'note_off', 97))
    return events


# ═══════════════════════════════════════════════════════════════
# XM & MIDI writers
# ═══════════════════════════════════════════════════════════════

@dataclass
class TN:
    note: int = 0; instrument: int = 0; volume: int = 0; effect: int = 0; effect_param: int = 0

class XMWriter:
    ROWS_PER_PATTERN = 128

    def build_and_write(self, fn, events, title="", nc=6, is_ym=False, fps=FPS):
        if nc % 2: nc += 1
        nc = max(4, min(32, nc))
        # one row per player frame: 2 ticks of 1/(2*fps) s (3xx needs >= 2 ticks per row)
        speed, bpm = 2, int(round(5 * fps))
        slide_ok = bpm <= 255
        if not slide_ok:
            speed, bpm = 1, max(32, min(255, int(round(2.5 * fps))))
        insts = instruments(is_ym)
        cells = {}
        for ev in events:
            row, ch = ev.frame, ev.channel
            if ch >= nc: continue
            c = cells.setdefault((row, ch), TN())
            if ev.event_type == 'note_on':
                c.note, c.instrument, c.volume = max(1, min(96, ev.note)), ev.instrument, 0x10 + ev.volume
                c.effect = c.effect_param = 0
            elif ev.event_type == 'pitch':
                if c.note != 97 and not (c.note and c.instrument):   # not over a fresh note
                    c.note = max(1, min(96, ev.note))
                    if slide_ok: c.effect, c.effect_param = 3, 0xFF
                    else: c.instrument = ev.instrument      # no ticks for 3xx: re-trigger
                    c.volume = c.volume or 0x10 + ev.volume
            elif ev.event_type == 'vol':
                if c.note != 97 and not (c.note and c.instrument):
                    c.volume = 0x10 + ev.volume
            elif ev.event_type == 'note_off':
                if not c.note: c.note = 97
        rpp = self.ROWS_PER_PATTERN
        max_row = max((k[0] for k in cells), default=0) + 1
        np_ = max(1, min(256, (max_row + rpp - 1) // rpp))
        if (max_row + rpp - 1) // rpp > 256:
            print("  Warning: song is longer than 256 patterns, truncated")
        pats = []
        for pi in range(np_):
            pat = []
            for ri in range(rpp):
                pat.append([cells.get((pi * rpp + ri, ch), TN()) for ch in range(nc)])
            pats.append(pat)
        self._wx(fn, title, nc, bpm, speed, pats, insts, list(range(np_)))

    def _wx(self, fn, title, nc, bpm, speed, pats, insts, order):
        with open(fn, 'wb') as f:
            f.write(b'Extended Module: ')
            f.write(title.encode('ascii', 'replace')[:20].ljust(20, b'\x00'))
            f.write(b'\x1a')
            f.write(b'PT32XM Converter    ')
            f.write(struct.pack('<HIHHHHHHH', 0x0104, 276, len(order), 0, nc, len(pats),
                                min(len(insts), 128), 1, speed) + struct.pack('<H', bpm))
            ot = bytearray(256)
            for i, o in enumerate(order[:256]): ot[i] = o
            f.write(ot)
            for pat in pats:
                pk = bytearray()
                for row in pat:
                    for ch in range(nc):
                        nd = row[ch] if ch < len(row) else TN()
                        hn, hi, hv = nd.note > 0, nd.instrument > 0, nd.volume > 0
                        hf, hp = nd.effect > 0, nd.effect_param > 0
                        if not (hn or hi or hv or hf or hp):
                            pk.append(0x80)
                        else:
                            pk.append(0x80 | (1 if hn else 0) | (2 if hi else 0) | (4 if hv else 0) |
                                      (8 if hf else 0) | (16 if hp else 0))
                            if hn: pk.append(nd.note & 0xFF)
                            if hi: pk.append(nd.instrument & 0xFF)
                            if hv: pk.append(nd.volume & 0xFF)
                            if hf: pk.append(nd.effect & 0xFF)
                            if hp: pk.append(nd.effect_param & 0xFF)
                f.write(struct.pack('<IBH', 9, 0, len(pat)) + struct.pack('<H', len(pk)) + pk)
            for inst in insts[:128]:
                data = inst['data']
                ihs = 263
                f.write(struct.pack('<I', ihs) + inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00'))
                f.write(struct.pack('<BHI', 0, 1, 40) + bytearray(96))
                ve = bytearray(48); struct.pack_into('<HHHH', ve, 0, 0, 64, 100, 64); f.write(ve)
                pe = bytearray(48); struct.pack_into('<HHHH', pe, 0, 0, 32, 100, 32); f.write(pe)
                # envelopes off, fadeout 0: note-off (97) cuts the sound at once
                f.write(bytes([2, 2, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0]))
                f.write(struct.pack('<H', 0))
                f.write(b'\x00' * (ihs - (4 + 22 + 1 + 2 + 4 + 96 + 48 + 48 + 14 + 2)))
                n = len(data)
                f.write(struct.pack('<III', n * 2, 0, n * 2 if inst['loop'] else 0))
                f.write(struct.pack('<BbBBb', 64, 0, (1 if inst['loop'] else 0) | 0x10, 128, inst['rel']))
                f.write(b'\x00')
                f.write(inst['name'].encode('ascii', 'replace')[:22].ljust(22, b'\x00'))
                prev = 0
                for v in data:
                    f.write(struct.pack('<h', ((v - prev + 32768) % 65536) - 32768)); prev = v


class MIDIWriter:
    TPQN = 480
    PROGRAM = {MODE_TONE: 80, MODE_ENV_TONE: 81, MODE_BUZZER: 38, MODE_MIXED: 87, MODE_NOISE: 0}

    @staticmethod
    def _vlq(v):
        v = max(0, int(v)); r = [v & 0x7F]; v >>= 7
        while v: r.append((v & 0x7F) | 0x80); v >>= 7
        r.reverse(); return bytes(r)

    @staticmethod
    def _drum(xm_note):
        return 42 if xm_note >= 75 else 38 if xm_note >= 58 else 36

    def write(self, fn, events, bpm=125, fps=FPS, title=""):
        tpqn = self.TPQN
        tpf = tpqn * bpm / (60.0 * fps)
        usec = int(60_000_000 / bpm)
        by_ch = {}
        for ev in events: by_ch.setdefault(ev.channel, []).append(ev)
        t0 = [(0, bytes([0xFF, 0x51, 0x03, (usec >> 16) & 0xFF, (usec >> 8) & 0xFF, usec & 0xFF]))]
        if title:
            td = title.encode('ascii', errors='replace')[:127]; t0.append((0, bytes([0xFF, 0x01, len(td)]) + td))
        tracks = [t0]
        free = [c for c in range(16) if c != 9]
        for xc in sorted(by_ch):
            evs = by_ch[xc]
            modes = {e.mode for e in evs if e.event_type == 'note_on'}
            drum = modes == {MODE_NOISE}
            if drum: mc = 9
            elif free: mc = free.pop(0)
            else: break
            trk = []
            note = None; prog = None; vel = 0; expr = -99
            def off(t):
                nonlocal note
                if note is not None: trk.append((t, bytes([0x80 | mc, note, 0]))); note = None
            def on(t, n, v):
                nonlocal note
                trk.append((t, bytes([0x90 | mc, n, v]))); note = n
            last_t = 0
            for ev in evs:
                t = int(ev.frame * tpf); last_t = t
                if ev.event_type == 'note_on':
                    off(t)
                    vel = max(1, min(127, ev.volume * 2 - 1))
                    if drum:
                        on(t, self._drum(ev.note), vel)
                    else:
                        p = self.PROGRAM.get(ev.mode, 80)
                        if p != prog: trk.append((t, bytes([0xC0 | mc, p]))); prog = p
                        e = max(1, min(127, ev.volume * 2 - 1))
                        if abs(e - expr) >= 1: trk.append((t, bytes([0xB0 | mc, 11, e]))); expr = e
                        on(t, max(0, min(127, ev.note + 11)), 100)
                elif ev.event_type == 'pitch' and not drum and note is not None:
                    n = max(0, min(127, ev.note + 11))
                    if n != note: off(t); on(t, n, 100)
                elif ev.event_type == 'vol' and not drum:
                    e = max(1, min(127, ev.volume * 2 - 1))
                    if abs(e - expr) >= 4: trk.append((t, bytes([0xB0 | mc, 11, e]))); expr = e
                elif ev.event_type == 'note_off':
                    off(t)
            off(last_t + 1)
            tracks.append(trk)
        with open(fn, 'wb') as f:
            f.write(b'MThd' + struct.pack('>IHHH', 6, 1, len(tracks), tpqn))
            for t in tracks:
                t.sort(key=lambda x: x[0]); d = bytearray(); prev = 0
                for at, ed in t:
                    d.extend(self._vlq(max(0, at - prev))); d.extend(ed); prev = at
                d.extend(self._vlq(0)); d.extend(b'\xFF\x2F\x00')
                f.write(b'MTrk' + struct.pack('>I', len(d)) + d)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def shift_events(events, semitones):
    """--transpose/--octave: move every melodic note (noise stays on its own scale)."""
    if not semitones: return
    for e in events:
        if e.event_type in ('note_on', 'pitch') and e.mode != MODE_NOISE:
            e.note = max(1, min(96, e.note + semitones))

def convert_pt3(input_file, output_file=None, transpose=0, octave=0,
                fmt='xm', midi_file=None, wav_file=None,
                channel_map_spec='default', raw_notes=False,
                sample_rate=44100, is_ym=False, wav_volume=0.8,
                clock=CLOCK, norm_scale=1.0, layout='abc'):
    base = os.path.splitext(input_file)[0]
    if not output_file: output_file = base + '.xm'
    with open(input_file, 'rb') as f: data = f.read()
    channel_map = parse_channel_map(channel_map_spec)
    num_xm_ay = max(channel_map.values()) + 1

    print(f"Конвертация: {input_file}")
    player = PT3Player(data)
    print(f"  Title: {player.title}")
    print(f"  Author: {player.author}")
    print(f"  Version: {player.version}, ToneTable: {player.tone_table_id}")
    print(f"  Speed: {player.initial_delay}, Positions: {player.num_positions}, Loop: {player.loop_position}")

    total_rows = player.num_positions * 64
    max_frames = total_rows * player.initial_delay + 1000

    if fmt in ('wav', 'all') or wav_file:
        wf = wav_file or base + '.wav'
        dur = render_wav((r for r, _ in pt3_frames(PT3Player(data), max_frames)), wf, sample_rate,
                         clock, is_ym, stereo=True, volume=wav_volume, layout=layout)
        print(f"  WAV: {wf} ({os.path.getsize(wf)/1024:.1f} KB, {dur:.1f}s)")
        if fmt == 'wav' and not midi_file:
            print("\nГотово!")
            return

    if raw_notes:
        num_xm = 4; mode_str = "RAW"
        events = extract_notes_raw(PT3Player(data), max_frames)
    else:
        num_xm = num_xm_ay; mode_str = f"AY ({channel_map_spec})"
        events = extract_notes_ay(pt3_frames(PT3Player(data), max_frames), channel_map, clock, is_ym, norm_scale)
    shift_events(events, transpose + 12 * octave)

    note_ons = [e for e in events if e.event_type == 'note_on']
    print(f"  Mode: {mode_str}, Notes: {len(note_ons)}")
    if not note_ons and fmt != 'wav':
        print("  No notes!"); return

    if fmt in ('xm', 'both', 'all'):
        xf = output_file if output_file.endswith('.xm') else base + '.xm'
        XMWriter().build_and_write(xf, events, player.title, num_xm, is_ym)
        print(f"  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {num_xm} ch)")

    if fmt in ('midi', 'both', 'all') or midi_file:
        mf = midi_file or base + '.mid'
        MIDIWriter().write(mf, events, 125, FPS, player.title)
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")

    dur = max(e.frame for e in events) / float(FPS)
    print(f"\n  Total: {len(note_ons)} notes, {dur:.1f}s")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='PT3→XM/MIDI/WAV Converter v8 (AY emulator, checked against zxtune123)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Output formats:
  (default)     XM only
  --midi        XM + MIDI
  --midi-only   MIDI only
  --wav         WAV only (AY emulation)
  --all         XM + MIDI + WAV

WAV options:
  --sample-rate   Sample rate (default 44100)
  --ym            Use YM chip DAC table (default: AY)
  --clock         Chip clock in Hz (default 1773400 = ZX Spectrum 128; Pentagon: 1750000)
  --wav-volume    WAV peak level 0.0-1.0 (default 0.8)

XM/MIDI:
  XM rows are player frames (50 Hz), volume follows the chip level every frame,
  pitch changes inside a note use portamento 3FF (no re-trigger).
  --norm-scale    XM volume multiplier (default 1.0)

Note modes:
  --raw-notes   Original PT3 notes (no ornaments/arpeggio)
  (default)     AY register analysis with --channel-map

Examples:
  python pt32xm-midi-wav.py music.pt3 --wav
  python pt32xm-midi-wav.py music.pt3 --all
  python pt32xm-midi-wav.py music.pt3 --wav --ym --sample-rate 48000
  python pt32xm-midi-wav.py music.pt3 --raw-notes --midi
  python pt32xm-midi-wav.py music.pt3 --channel-map compact --all
""")
    ap.add_argument('input', nargs='*')
    ap.add_argument('-o', '--output')
    ap.add_argument('--octave', type=int, default=0)
    ap.add_argument('--transpose', type=int, default=0)
    ap.add_argument('--midi', action='store_true')
    ap.add_argument('--midi-only', action='store_true')
    ap.add_argument('--midi-file', type=str, default=None)
    ap.add_argument('--wav', action='store_true', help='Render WAV')
    ap.add_argument('--wav-file', type=str, default=None)
    ap.add_argument('--all', action='store_true', help='Output XM+MIDI+WAV')
    ap.add_argument('--sample-rate', type=int, default=44100)
    ap.add_argument('--ym', action='store_true', help='Use YM DAC table')
    ap.add_argument('--clock', type=int, default=CLOCK)
    ap.add_argument('--layout', choices=['abc', 'mono'], default='abc', help='WAV stereo layout')
    ap.add_argument('--wav-volume', type=float, default=0.8)
    ap.add_argument('--norm-scale', type=float, default=1.0)
    ap.add_argument('--channel-map', type=str, default='default')
    ap.add_argument('--raw-notes', action='store_true')
    ap.add_argument('--list-presets', action='store_true')
    args = ap.parse_args()

    if args.list_presets:
        for name, m in CHANNEL_PRESETS.items():
            nc = max(m.values()) + 1; print(f"  {name} ({nc} ch):")
            for (ay, mode), xm in sorted(m.items()): print(f"    {get_channel_name(ay, mode):>10s} → XM {xm}")
            print()
        sys.exit(0)

    if not args.input: ap.print_help(); sys.exit(1)
    if args.output and len(args.input) > 1: print("--output: single file only"); sys.exit(1)

    if args.all: fmt = 'all'
    elif args.wav and not args.midi and not args.midi_only: fmt = 'wav'
    elif args.midi_only: fmt = 'midi'
    elif args.midi: fmt = 'both'
    else: fmt = 'xm'

    try: parse_channel_map(args.channel_map)
    except ValueError as e: print(f"Error: {e}"); sys.exit(1)

    for inp in args.input:
        if not os.path.exists(inp): print(f"Not found: {inp}"); continue
        try:
            convert_pt3(inp, args.output, args.transpose, args.octave, fmt, args.midi_file,
                        args.wav_file, args.channel_map, args.raw_notes, args.sample_rate,
                        args.ym, args.wav_volume, args.clock, args.norm_scale, args.layout)
        except Exception as e:
            import traceback; print(f"Error: {e}"); traceback.print_exc()
    print("\nГотово!")


if __name__ == '__main__':
    main()
