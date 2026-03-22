"""
PT3 to XM/MIDI/WAV Converter v7
Based on pt3player.c by Bulba/Volutar
With channel mapping, --raw-notes mode, and WAV rendering
"""

import struct, sys, os, math, array
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

# Envelope state machines
ENV_SHAPES = {
    # shape: [(segment0_dir, segment0_hold), (segment1_dir, segment1_hold)]
    # dir: 1=up, -1=down, hold: None=continue, 0=hold_bottom, 31=hold_top
}

class AYEmulator:
    """Simple AY-3-8910 emulator for WAV rendering."""
    def __init__(self, clock_rate=1773400, sample_rate=44100, is_ym=False):
        self.clock = clock_rate
        self.sr = sample_rate
        self.dac = YM_DAC if is_ym else AY_DAC
        self.step = clock_rate / (sample_rate * 8 * 8)
        # Tone
        self.tone_period = [1,1,1]
        self.tone_counter = [0,0,0]
        self.tone_out = [0,0,0]
        # Noise
        self.noise_period = 1
        self.noise_counter = 0
        self.noise = 1
        self.noise_out = 0
        # Envelope
        self.env_period = 1
        self.env_counter = 0
        self.env_shape = 0
        self.env_segment = 0
        self.env_value = 0
        # Mixer
        self.tone_off = [1,1,1]   # 1=disabled
        self.noise_off = [1,1,1]
        self.env_on = [0,0,0]
        self.volume = [0,0,0]
        # Pan
        self.pan_l = [0.9,0.5,0.1]
        self.pan_r = [0.1,0.5,0.9]
        # Interpolation
        self.x = 0.0

    def set_registers(self, regs):
        """Set all 14 AY registers from array."""
        self.tone_period[0] = max(1, (regs[1] & 0x0F) << 8 | regs[0])
        self.tone_period[1] = max(1, (regs[3] & 0x0F) << 8 | regs[2])
        self.tone_period[2] = max(1, (regs[5] & 0x0F) << 8 | regs[4])
        self.noise_period = max(1, regs[6] & 0x1F)
        mixer = regs[7]
        for i in range(3):
            self.tone_off[i] = (mixer >> i) & 1
            self.noise_off[i] = (mixer >> (i+3)) & 1
            self.env_on[i] = 1 if regs[8+i] & 0x10 else 0
            self.volume[i] = regs[8+i] & 0x0F
        self.env_period = max(1, regs[12] << 8 | regs[11])
        if regs[13] != 0xFF and regs[13] != 255:
            self.env_shape = regs[13] & 0x0F
            self.env_counter = 0
            self.env_segment = 0
            self._reset_env_segment()

    def _reset_env_segment(self):
        shape = self.env_shape
        seg = self.env_segment
        # Determine initial value based on shape and segment
        if shape < 4:  # \___
            self.env_value = 31 if seg == 0 else 0
        elif shape < 8:  # /___
            self.env_value = 0 if seg == 0 else 0
        elif shape == 8:  # \\\\
            self.env_value = 31
        elif shape == 9:  # \___
            self.env_value = 31 if seg == 0 else 0
        elif shape == 10:  # \/\/
            self.env_value = 31 if seg % 2 == 0 else 0
        elif shape == 11:  # \---
            self.env_value = 31 if seg == 0 else 31
        elif shape == 12:  # ////
            self.env_value = 0
        elif shape == 13:  # /---
            self.env_value = 0 if seg == 0 else 31
        elif shape == 14:  # /\/\  
            self.env_value = 0 if seg % 2 == 0 else 31
        elif shape == 15:  # /___
            self.env_value = 0 if seg == 0 else 0

    def _update_tone(self, ch):
        self.tone_counter[ch] += 1
        if self.tone_counter[ch] >= self.tone_period[ch]:
            self.tone_counter[ch] = 0
            self.tone_out[ch] ^= 1
        return self.tone_out[ch]

    def _update_noise(self):
        self.noise_counter += 1
        if self.noise_counter >= self.noise_period * 2:
            self.noise_counter = 0
            bit = ((self.noise ^ (self.noise >> 3)) & 1)
            self.noise = (self.noise >> 1) | (bit << 16)
        self.noise_out = self.noise & 1
        return self.noise_out

    def _update_envelope(self):
        self.env_counter += 1
        if self.env_counter >= self.env_period:
            self.env_counter = 0
            shape = self.env_shape
            seg = self.env_segment
            if shape < 4:  # \___
                if seg == 0:
                    self.env_value -= 1
                    if self.env_value < 0:
                        self.env_segment = 1
                        self.env_value = 0
            elif shape < 8:  # /___
                if seg == 0:
                    self.env_value += 1
                    if self.env_value > 31:
                        self.env_segment = 1
                        self.env_value = 0
            elif shape == 8:  # \\\\
                self.env_value -= 1
                if self.env_value < 0:
                    self.env_value = 31
            elif shape == 9:  # \___
                if seg == 0:
                    self.env_value -= 1
                    if self.env_value < 0:
                        self.env_segment = 1
                        self.env_value = 0
            elif shape == 10:  # \/\/
                if seg % 2 == 0:
                    self.env_value -= 1
                    if self.env_value < 0:
                        self.env_segment += 1
                        self.env_value = 0
                else:
                    self.env_value += 1
                    if self.env_value > 31:
                        self.env_segment += 1
                        self.env_value = 31
            elif shape == 11:  # \---
                if seg == 0:
                    self.env_value -= 1
                    if self.env_value < 0:
                        self.env_segment = 1
                        self.env_value = 31
            elif shape == 12:  # ////
                self.env_value += 1
                if self.env_value > 31:
                    self.env_value = 0
            elif shape == 13:  # /---
                if seg == 0:
                    self.env_value += 1
                    if self.env_value > 31:
                        self.env_segment = 1
                        self.env_value = 31
            elif shape == 14:  # /\/\ 
                if seg % 2 == 0:
                    self.env_value += 1
                    if self.env_value > 31:
                        self.env_segment += 1
                        self.env_value = 31
                else:
                    self.env_value -= 1
                    if self.env_value < 0:
                        self.env_segment += 1
                        self.env_value = 0
            elif shape == 15:  # /___
                if seg == 0:
                    self.env_value += 1
                    if self.env_value > 31:
                        self.env_segment = 1
                        self.env_value = 0
        return self.env_value

    def _mix_sample(self):
        noise = self._update_noise()
        envelope = self._update_envelope()
        left = 0.0; right = 0.0
        for i in range(3):
            tone = self._update_tone(i)
            out = (tone | self.tone_off[i]) & (noise | self.noise_off[i])
            if self.env_on[i]:
                amp = envelope
            else:
                amp = self.volume[i] * 2 + 1
            val = self.dac[out * amp] if out * amp < len(self.dac) else 0
            left += val * self.pan_l[i]
            right += val * self.pan_r[i]
        return left, right

    def render_sample(self):
        """Render one output sample (at sample_rate)."""
        left = 0.0; right = 0.0
        for _ in range(8):  # decimation factor
            self.x += self.step
            if self.x >= 1.0:
                self.x -= 1.0
                self._last_l, self._last_r = self._mix_sample()
            left += getattr(self, '_last_l', 0)
            right += getattr(self, '_last_r', 0)
        return left / 8, right / 8


# ═══════════════════════════════════════════════════════════════
# PT3 Player (faithful port of pt3player.c)
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

def render_wav(player, filename, sample_rate=44100, clock_rate=1773400,
               is_ym=False, stereo=True, volume=0.8, max_frames=50000):
    """Render PT3 to WAV using AY emulator."""
    frame_rate = 50
    samples_per_frame = sample_rate // frame_rate

    ay = AYEmulator(clock_rate, sample_rate, is_ym)

    # Re-init player
    player._init_playback()

    pcm_l = []
    pcm_r = []
    total_samples = 0

    print(f"  Rendering WAV: {sample_rate}Hz, {'stereo' if stereo else 'mono'}, "
          f"{'YM' if is_ym else 'AY'} chip...")

    for frame in range(max_frames):
        regs = player.play_tick()
        ay.set_registers(regs)

        for _ in range(samples_per_frame):
            l, r = ay.render_sample()
            pcm_l.append(l)
            pcm_r.append(r)
            total_samples += 1

        # Stop check
        if (player.state.current_position >= player.num_positions - 1 and
            player.state.delay_counter == 1 and
            player.channels[0].skip_counter == 1 and frame > 100):
            if player._b(player.channels[0].address) == 0:
                break

        if frame % 500 == 0 and frame > 0:
            print(f"    frame {frame}/{max_frames} "
                  f"({total_samples/sample_rate:.1f}s)...", end='\r')

    print(f"    Rendered {total_samples} samples "
          f"({total_samples/sample_rate:.1f}s)        ")

    # Normalize & write WAV
    max_val = 0
    for i in range(len(pcm_l)):
        max_val = max(max_val, abs(pcm_l[i]), abs(pcm_r[i]))
    if max_val == 0:
        max_val = 1

    scale = volume * 32767 / max_val
    nch = 2 if stereo else 1

    with open(filename, 'wb') as f:
        data_size = total_samples * nch * 2
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))           # chunk size
        f.write(struct.pack('<H', 1))            # PCM
        f.write(struct.pack('<H', nch))          # channels
        f.write(struct.pack('<I', sample_rate))  # sample rate
        f.write(struct.pack('<I', sample_rate * nch * 2))  # byte rate
        f.write(struct.pack('<H', nch * 2))      # block align
        f.write(struct.pack('<H', 16))           # bits per sample
        f.write(b'data')
        f.write(struct.pack('<I', data_size))

        for i in range(total_samples):
            sl = max(-32768, min(32767, int(pcm_l[i] * scale)))
            if stereo:
                sr = max(-32768, min(32767, int(pcm_r[i] * scale)))
                f.write(struct.pack('<hh', sl, sr))
            else:
                f.write(struct.pack('<h', sl))

    return total_samples / sample_rate

# ═══════════════════════════════════════════════════════════════
# Note extraction (raw & AY modes) — same as v6
# ═══════════════════════════════════════════════════════════════

CLOCK=1773400;REPEATING_ENV={0x08,0x0A,0x0C,0x0E}
def ay_freq(p):return CLOCK/(16.0*p)if p>0 else 0
def env_freq(p):return CLOCK/(256.0*p)if p>0 else 0
def freq_to_xm(freq):
    if freq<15 or freq>20000:return None
    midi=69+12*math.log2(freq/440.0);xm=int(round(midi))-11
    return xm if 1<=xm<=96 else None

@dataclass
class NoteEvent:
    frame:int=0;channel:int=0;ay_channel:int=0;mode:str=""
    event_type:str="";note:int=0;instrument:int=1;volume:int=64

def extract_notes_raw(player,max_frames=50000):
    events=[];prev_note=[-1,-1,-1];prev_vol=[0,0,0]
    for frame in range(max_frames):
        player.play_tick()
        for ch in range(3):
            c=player.channels[ch]
            if c.just_disabled:
                if prev_note[ch]>=0:events.append(NoteEvent(frame,ch,ch,'','note_off',97));prev_note[ch]=-1
            elif c.note_changed and c.enabled:
                if prev_note[ch]>=0:events.append(NoteEvent(frame,ch,ch,'','note_off',97))
                xn=c.note+1;xv=max(1,int(64*c.volume/15))
                events.append(NoteEvent(frame,ch,ch,MODE_TONE,'note_on',max(1,min(96,xn)),1,xv))
                prev_note[ch]=c.note;prev_vol[ch]=xv
        if(player.state.current_position>=player.num_positions-1 and
           player.state.delay_counter==1 and player.channels[0].skip_counter==1 and frame>100):
            if player._b(player.channels[0].address)==0:break
    for ch in range(3):
        if prev_note[ch]>=0:events.append(NoteEvent(frame,ch,ch,'','note_off',97))
    return events

def extract_notes_ay(player,channel_map,max_frames=50000):
    events=[];mx=max(channel_map.values())+1
    pn=[-1]*mx;pv=[0]*mx;pi=[0]*mx;ps=[True]*mx;pam=[None]*3;pax=[-1]*3
    def gx(a,m):
        k=(a,m)
        if k in channel_map:return channel_map[k]
        for f in[MODE_TONE,MODE_MIXED,MODE_BUZZER]:
            if(a,f)in channel_map:return channel_map[(a,f)]
        return a
    for frame in range(max_frames):
        regs=player.play_tick();mixer=regs[7]
        for ac in range(3):
            period=regs[ac*2]|((regs[ac*2+1]&0x0F)<<8);vr=regs[8+ac]
            ue=bool(vr&0x10);vol=vr&0x0F;to=bool(mixer&(1<<ac));no=bool(mixer&(1<<(ac+3)))
            ep=regs[11]|(regs[12]<<8);es=regs[13]if regs[13]!=0xFF else-1
            note=None;ev=vol;mode=None;inst=1
            if ue and to and no:
                if es in REPEATING_ENV and ep>0:note=freq_to_xm(env_freq(ep));ev=15;mode=MODE_BUZZER;inst=3
            elif ue and not to:
                if period>0:note=freq_to_xm(ay_freq(period));ev=15;mode=MODE_ENV_TONE;inst=2
            elif vol>0:
                if not to and period>0:
                    note=freq_to_xm(ay_freq(period));mode=MODE_MIXED if not no else MODE_TONE
                    inst=MODE_INSTRUMENTS[mode]
                elif not no:np=regs[6]&0x1F;note=max(1,min(96,72-np));mode=MODE_NOISE;inst=5
            if note is None or ev==0 or mode is None:
                ox=pax[ac]
                if ox>=0 and pn[ox]>=0:
                    events.append(NoteEvent(frame,ox,ac,pam[ac]or'','note_off',97))
                    pn[ox]=-1;pv[ox]=0;pi[ox]=0;ps[ox]=True
                pam[ac]=None;pax[ac]=-1;continue
            xc=gx(ac,mode);xv=max(1,int(64*ev/15));ox=pax[ac]
            if ox>=0 and ox!=xc:
                if pn[ox]>=0:
                    events.append(NoteEvent(frame,ox,ac,pam[ac]or'','note_off',97))
                    pn[ox]=-1;pv[ox]=0;pi[ox]=0;ps[ox]=True
            pam[ac]=mode;pax[ac]=xc;nn=False
            if pn[xc]<0:nn=True
            elif note!=pn[xc]:nn=True
            elif inst!=pi[xc]:nn=True
            elif ps[xc]:nn=True
            elif xv>pv[xc]and(xv-pv[xc])>=16:nn=True
            if nn:
                if pn[xc]>=0:events.append(NoteEvent(frame,xc,ac,mode,'note_off',97))
                events.append(NoteEvent(frame,xc,ac,mode,'note_on',note,inst,xv))
                pn[xc]=note;pv[xc]=xv;pi[xc]=inst
            ps[xc]=False
        if(player.state.current_position>=player.num_positions-1 and
           player.state.delay_counter==1 and player.channels[0].skip_counter==1 and frame>100):
            if player._b(player.channels[0].address)==0:break
    for xc in range(mx):
        if pn[xc]>=0:events.append(NoteEvent(frame,xc,event_type='note_off',note=97))
    return events

# ═══════════════════════════════════════════════════════════════
# XM & MIDI Writers (compact)
# ═══════════════════════════════════════════════════════════════

@dataclass
class TN:
    note:int=0;instrument:int=0;volume:int=0;effect:int=0;effect_param:int=0

class AYGen:
    P=32
    @classmethod
    def sq(c):return[int(max(-1,min(1,sum(math.sin(2*math.pi*h*i/c.P)/h for h in range(1,20,2))*1.2))*24000)for i in range(c.P)]
    @classmethod
    def buzz(c):p=c.P*2;return[int(max(-1,min(1,math.sin(2*math.pi*i/p)*.5+math.sin(6*math.pi*i/p)*.3+math.sin(10*math.pi*i/p)*.2))*24000)for i in range(p)]
    @classmethod
    def lead(c):return[int(max(-1,min(1,sum(math.sin(2*math.pi*i/c.P*h)*(1./h**.8)for h in range(1,12))*.5))*24000)for i in range(c.P)]
    @classmethod
    def nt(c):
        import random;random.seed(42);return[int((math.sin(2*math.pi*i/c.P)*.5+random.gauss(0,.3))*24000)for i in range(c.P)]
    @staticmethod
    def noise(n=4096):
        s,l=[],1
        for _ in range(n):b=((l>>0)^(l>>1))&1;l=(l>>1)|(b<<14);s.append(24000 if l&1 else-24000)
        return s

class XMWriter:
    def build_and_write(self,fn,events,title="",bpm=125,speed=3,fr=50,nc=6):
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
        g=AYGen
        def mk(n,p,l=True,pr=1):
            o=int(round(12*math.log2(pr)))if pr>1 else 0
            return{'name':n[:22],'samples':[{'name':n[:22],'data':p,'length':len(p),'loop_start':0,'loop_length':len(p)if l else 0,'loop_type':1 if l else 0,'volume':64,'finetune':0,'panning':128,'relative_note':o,'bits':16}]}
        return[mk("AY Tone",g.sq()),mk("AY Env+Tone",g.lead()),mk("AY Buzzer",g.buzz(),pr=2),mk("AY Tone+Noise",g.nt()),mk("AY Noise",g.noise(),l=False)]
    def _wx(self,fn,title,nc,bpm,speed,pats,insts,order):
        with open(fn,'wb') as f:
            f.write(b'Extended Module: ');f.write(title.encode('ascii','replace')[:20].ljust(20,b'\x00'))
            f.write(b'\x1a');f.write(b'PT32XM Converter    ')
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
                    tb=s.get('loop_type',0)&3;
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
    def write(self,fn,events,bpm=125,speed=3,fr=50,title=""):
        tpqn=self.tpqn;tpf=tpqn*bpm/(60.0*fr);usec=int(60_000_000/bpm)
        chs={};
        for ev in events:chs.setdefault(ev.channel,[]).append(ev)
        tracks=[];t0=[(0,bytes([0xFF,0x51,0x03,(usec>>16)&0xFF,(usec>>8)&0xFF,usec&0xFF]))]
        if title:td=title.encode('ascii',errors='replace')[:127];t0.append((0,bytes([0xFF,0x01,len(td)])+td))
        tracks.append(t0);gm=[80,81,74]
        for ci,ch in enumerate(sorted(chs.keys())):
            if ci>=15:break
            mc=ci if ci<9 else ci+1;trk=[(0,bytes([0xC0|mc,gm[ci%3]]))];act={}
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

def convert_pt3(input_file, output_file=None, transpose=0, octave=0,
                finetune=0, fmt='xm', midi_file=None, wav_file=None,
                channel_map_spec='default', raw_notes=False,
                sample_rate=44100, is_ym=False, wav_volume=0.8):
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
    print(f"  Speed: {player.initial_delay}, Positions: {player.num_positions}, Loop: {player.loop_position}")

    total_rows=player.num_positions*64
    max_frames=total_rows*player.initial_delay+1000

    # WAV rendering (uses its own player instance)
    if fmt in ('wav','all') or wav_file:
        wf = wav_file or base+'.wav'
        wav_player = PT3Player(data)  # fresh instance
        dur = render_wav(wav_player, wf, sample_rate, CLOCK, is_ym,
                         stereo=True, volume=wav_volume, max_frames=max_frames)
        print(f"  WAV: {wf} ({os.path.getsize(wf)/1024:.1f} KB, {dur:.1f}s)")
        if fmt == 'wav' and not midi_file:
            print(f"\nГотово!")
            return

    # Note extraction
    if raw_notes:
        num_xm=4;mode_str="RAW"
        player2=PT3Player(data)
        events=extract_notes_raw(player2,max_frames)
    else:
        num_xm=num_xm_ay;mode_str=f"AY ({channel_map_spec})"
        player2=PT3Player(data)
        events=extract_notes_ay(player2,channel_map,max_frames)

    note_ons=[e for e in events if e.event_type=='note_on']
    print(f"  Mode: {mode_str}, Notes: {len(note_ons)}")

    if not note_ons and fmt not in ('wav',):
        print("  No notes!");return

    bpm=125;speed=player.initial_delay

    if fmt in('xm','both','all'):
        xf=output_file if output_file.endswith('.xm')else base+'.xm'
        XMWriter().build_and_write(xf,events,player.title,bpm,speed,50,num_xm)
        print(f"  XM: {xf} ({os.path.getsize(xf)/1024:.1f} KB, {num_xm} ch)")

    if fmt in('midi','both','all') or midi_file:
        mf=midi_file or base+'.mid'
        MIDIWriter().write(mf,events,bpm,speed,50,player.title)
        print(f"  MIDI: {mf} ({os.path.getsize(mf)/1024:.1f} KB)")

    if note_ons:
        dur=max(e.frame for e in events)/50.0
        print(f"\n  Total: {len(note_ons)} notes, {dur:.1f}s")


def main():
    import argparse
    ap=argparse.ArgumentParser(
        description='PT3→XM/MIDI/WAV Converter v7',
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
  --wav-volume    WAV volume 0.0-1.0 (default 0.8)

Note modes:
  --raw-notes   Original PT3 notes (no ornaments/arpeggio)
  (default)     AY register analysis with --channel-map

Examples:
  python pt32xm.py music.pt3 --wav
  python pt32xm.py music.pt3 --all
  python pt32xm.py music.pt3 --wav --ym --sample-rate 48000
  python pt32xm.py music.pt3 --raw-notes --midi
  python pt32xm.py music.pt3 --channel-map compact --all
""")
    ap.add_argument('input',nargs='*')
    ap.add_argument('-o','--output')
    ap.add_argument('--octave',type=int,default=0)
    ap.add_argument('--transpose',type=int,default=0)
    ap.add_argument('--finetune',type=int,default=0)
    ap.add_argument('--midi',action='store_true')
    ap.add_argument('--midi-only',action='store_true')
    ap.add_argument('--midi-file',type=str,default=None)
    ap.add_argument('--wav',action='store_true',help='Render WAV')
    ap.add_argument('--wav-file',type=str,default=None)
    ap.add_argument('--all',action='store_true',help='Output XM+MIDI+WAV')
    ap.add_argument('--sample-rate',type=int,default=44100)
    ap.add_argument('--ym',action='store_true',help='Use YM DAC table')
    ap.add_argument('--wav-volume',type=float,default=0.8)
    ap.add_argument('--channel-map',type=str,default='default')
    ap.add_argument('--raw-notes',action='store_true')
    ap.add_argument('--list-presets',action='store_true')
    args=ap.parse_args()

    if args.list_presets:
        for name,m in CHANNEL_PRESETS.items():
            nc=max(m.values())+1;print(f"  {name} ({nc} ch):")
            for(ay,mode),xm in sorted(m.items()):print(f"    {get_channel_name(ay,mode):>10s} → XM {xm}")
            print()
        sys.exit(0)

    if not args.input:ap.print_help();sys.exit(1)
    if args.output and len(args.input)>1:print("--output: single file only");sys.exit(1)

    if args.all:fmt='all'
    elif args.wav and not args.midi and not args.midi_only:fmt='wav'
    elif args.midi_only:fmt='midi'
    elif args.midi:fmt='both'
    else:fmt='xm'

    # If --wav-file specified, always render wav
    if args.wav_file:
        if fmt=='xm':fmt='xm'  # keep xm but also wav via wav_file

    try:channel_map=parse_channel_map(args.channel_map)
    except ValueError as e:print(f"Error: {e}");sys.exit(1)

    for inp in args.input:
        if not os.path.exists(inp):print(f"Not found: {inp}");continue
        try:
            convert_pt3(inp,args.output,args.transpose,args.octave,args.finetune,
                        fmt,args.midi_file,args.wav_file,args.channel_map,
                        args.raw_notes,args.sample_rate,args.ym,args.wav_volume)
        except Exception as e:
            import traceback;print(f"Error: {e}");traceback.print_exc()
    print("\nГотово!")

if __name__=='__main__':main()