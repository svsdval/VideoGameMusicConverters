"""Дифференциальный тест: порты ABI2 (nead) / ABI3 (naudio) в usf_pyemu.py против C-кода lazyusf2.

Случайные alist-программы (команды + данные DRAM/DMEM) выполняются C-реализацией (abitest.c)
и Python-реализацией (usf_pyemu.Hle), после чего побайтно сравниваются DRAM (1 МБ) и DMEM.
Запуск: usf_tracer/abitest/run.sh [число_программ_на_вариант]

Ограничения генератора (чтобы не упираться в неопределённое поведение C): адреса DMEM кратны 16 и
входные/выходные области не пересекаются, индекс книги ADPCM < 8 (чтение за таблицей в C - UB).
"""
import sys, os, random, struct, subprocess, array
sys.path.insert(0, os.environ.get('PYEMU_DIR', '.'))
import usf_pyemu as U
import numpy as np

DRAM = 1 << 20

# В C чтение книги ADPCM за концом таблицы - UB; такие программы при расхождении не считаем ошибкой.
_oob = {'hit': False}
_orig_adpcm = U.Hle.adpcm
def _adpcm_probe(self, init, loop, dmemo, dmemi, count, last, two_bit=False):
    di = dmemi & 0xFFF
    for _ in range(count // 32):
        if (self.buf[di & 0xFFF] & 0xF) >= 8:
            _oob['hit'] = True
        di += 1 + (4 if two_bit else 8)
    return _orig_adpcm(self, init, loop, dmemo, dmemi, count, last, two_bit)
U.Hle.adpcm = _adpcm_probe

class FM:
    def __init__(s, img):
        s.rd = array.array('I'); s.rd.frombytes(img); s.rd.byteswap()
        s.spmem = [0] * 0x400
        s.rdram_size = len(img)
    def dram_read_bytes(s, addr, n):
        w = array.array('I', s.rd[addr >> 2:(addr + n + 3) >> 2]); w.byteswap()
        b = w.tobytes(); o = addr & 3
        return b[o:o + n]
    def dram_write_bytes(s, addr, data):
        for i, byte in enumerate(data):
            a = addr + i
            w = s.rd[a >> 2]; sh = (3 - (a & 3)) * 8
            s.rd[a >> 2] = (w & ~(0xFF << sh) & 0xFFFFFFFF) | (byte << sh)
    def rsp_break(s, x): pass
    def image(s):
        w = array.array('I', s.rd); w.byteswap(); return w.tobytes()

N3 = {'naudio': 'naudio', 'bk': 'bk', 'dk': 'dk', 'mp3': 'mp3', 'cbfd': 'cbfd'}
N2 = ['mk', 'sf', 'sfj', 'fz', 'wrjb', 'ys', '1080', 'oot', 'mm', 'mmb', 'ac']

def gen(variant, seed, ncmd=70):
    R = random.Random(seed)
    img = bytearray(R.getrandbits(8) & 0xF7 for _ in range(DRAM))   # индекс книги < 8: чтение за таблицей в C - UB
    # маленькие кодбуки ADPCM / фильтров
    for i in range(0x2000, 0x2400, 2):
        struct.pack_into('>h', img, i, R.randint(-0x1000, 0x1000))
    for i in range(0x40000, 0x41400, 2):
        struct.pack_into('>h', img, i, R.randint(-1500, 1500))
    for i in range(0x2400, 0x2800, 2):
        struct.pack_into('>h', img, i, 0)             # нулевая таблица (polef-ветка naudio_14)
    ab = bytearray(R.getrandbits(8) for _ in range(0x1000))
    # DMEM: не слишком громкий сигнал
    for i in range(0, 0x1000, 2):
        struct.pack_into('>h', ab, i, R.randint(-3000, 3000))
    ab = bytearray(b & 0xF7 for b in ab)
    states = [0x40000 + 0x200 * k for k in range(6)]
    def st(): return R.choice(states)
    def data(): return R.randrange(0x10000, 0xE0000, 8)
    is3 = variant in N3
    fm = FM(bytes(img)); fm.spmem[0xFF0 >> 2] = 0; hle = U.Hle(fm)
    table = hle.n3_table(variant) if is3 else hle.n2_table(variant)
    names = [f.__name__ for f in table]
    cmds = []
    M = 0x4F0
    def rnd_s16(): return R.randint(-0x8000, 0x7FFF) & 0xFFFF
    IN = lambda: R.randrange(0, 0x70, 16)
    OUT = lambda: R.randrange(0x200, 0x300, 16)
    CNT = lambda: R.randrange(0x10, 0x100, 16)
    def g_n3(name):
        if name == 'n3_adpcm':
            cnt = R.randrange(0x20, 0x100, 0x20)
            fl = R.randint(0, 3)
            w2 = fl << 28 | cnt << 16 | R.randint(0, 15) << 12 | OUT()
            return st(), w2
        if name == 'n3_resample':
            fl = R.randint(0, 1)
            if R.random() < 0.5:
                return st(), fl << 30 | R.randint(0x800, 0x7000) << 14 | R.randrange(0x190, 0x200, 16) << 2
            return st(), fl << 30 | R.randint(0x800, 0x7000) << 14 | R.randrange(0, 0x20, 16) << 2 | 1
        if name == 'n3_envmixer':
            return 3 << 24 | R.randint(0, 1) << 16 | rnd_s16(), st()
        if name == 'n3_setvol':
            f = R.choice([6, 4, 0, 6])
            return 9 << 24 | f << 16 | rnd_s16(), R.getrandbits(32) & (0x7FFF7FFF if f == 6 else 0x3FFFF)
        if name == 'n3_clearbuff': return R.randrange(0, 0x300, 2), CNT()
        if name == 'n3_loadbuff' or name == 'n3_savebuff':
            return R.randrange(0, 0x300, 16) | CNT() << 12, data()
        if name == 'n3_loadadpcm': return R.choice([0x80, 0x100]), R.choice([0x2000, 0x2000, 0x2200, 0x2400])
        if name == 'n3_dmemmove': return R.randrange(0, 0x100, 2), OUT() << 16 | CNT()
        if name == 'n3_setloop': return 0, st()
        if name == 'n3_mixer': return rnd_s16(), IN() << 16 | OUT()
        if name == 'n3_02b0': return 0, R.getrandbits(32)
        if name == 'n3_overload': return R.randrange(0, 0x100, 16), R.getrandbits(16) << 16 | R.randint(0, 0x2000)
        if name == 'n3_14': return R.randint(0, 1) << 16 | R.randint(0, 0xFFFF), R.randint(0, 1) << 24 | st()
        return None
    def g_n2(name):
        A = lambda lo, hi, st_=16: R.randrange(lo, hi, st_)
        ZI = lambda: A(0x100, 0x400)
        ZO = lambda: A(0x500, 0x800)
        if name == 'n2_setbuff':
            return ZI(), ZO() << 16 | A(0x20, 0x180, 16)
        if name == 'n2_adpcm': return R.randint(0, 7) << 16, st()
        if name == 'n2_clearbuff': return A(0, 0xD00, 2), CNT()
        if name in ('n2_loadbuff', 'n2_savebuff'): return A(0, 0xC00, 16) | CNT() << 12, data()
        if name == 'n2_mixer': return ((CNT() << 12) & 0xFF0000 if False else A(0x10, 0x100) << 12) | rnd_s16(), ZI() << 16 | ZO()
        if name == 'n2_resample': return R.randint(0, 1) << 16 | R.randint(0x800, 0x7000), st()
        if name == 'n2_resample_zoh': return R.randint(0x800, 0x7000), R.getrandbits(16)
        if name == 'n2_dmemmove': return ZI(), ZO() << 16 | CNT()
        if name in ('n2_envsetup1_mk', 'n2_envsetup1'): return R.getrandbits(32) & 0xFFFFFF, R.getrandbits(32) & 0x00FF00FF
        if name == 'n2_envsetup2': return 0, R.getrandbits(32)
        if name in ('n2_envmixer_mk', 'n2_envmixer'):
            return ZI() << 12 | R.randrange(8, 64, 8) << 8 | R.getrandbits(5), ZO() << 20 | (ZO() + 0x100) << 12 | (ZO() + 0x200) << 4 | (ZO() + 0x300) >> 4
        if name == 'n2_duplicate': return R.randint(1, 3) << 16 | ZI(), ZO() << 16
        if name == 'n2_interl': return R.randrange(2, 0x60, 2), ZI() << 16 | ZO()
        if name == 'n2_interleave_mk': return 0, A(0x100, 0x180) << 16 | A(0x300, 0x340)
        if name == 'n2_interleave': return ZO() | (A(0x10, 0x100) << 12), A(0x100, 0x180) << 16 | A(0x300, 0x340)
        if name == 'n2_addmixer': return A(0x10, 0x100) << 12, ZI() << 16 | ZO()
        if name == 'n2_hilogain': return R.randint(0, 255) << 16 | CNT(), ZO() << 16
        if name == 'n2_filter': return None
        if name == 'n2_nead16': return R.randint(1, 3) << 16 | ZI(), ZO() << 16 | R.randrange(0x20, 0x80, 32)
        if name == 'n2_polef': return R.randint(0, 1) << 16 | R.randint(0, 0x7000), st()
        if name == 'n3_loadadpcm_nead': return R.choice([0x80, 0x100]), R.choice([0x2000, 0x2000, 0x2200])
        if name == 'n2_setloop': return 0, st()
        return None
    gens = g_n3 if is3 else g_n2
    usable = [(i, n) for i, n in enumerate(names) if gens(n) is not None or n == 'n2_filter']
    # всегда начинаем с таблицы и буфера
    for k in range(ncmd):
        i, n = R.choice(usable)
        if n == 'n2_filter':
            cmds.append((i, 2 << 16 | R.randrange(0x20, 0x100, 16), 0x50000))
            cmds.append((i, R.randrange(0x100, 0x400, 16), 0x50100))
            continue
        w1, w2 = gens(n)
        cmds.append((i, w1, w2))
    # первой командой - таблица (чтобы ADPCM не работал на нулях)
    ld = [j for j, n in enumerate(names) if n in ('n3_loadadpcm', 'n3_loadadpcm_nead')]
    cmds.insert(0, (ld[0], 0x100, 0x2000))
    if not is3:
        sb = [j for j, n in enumerate(names) if n == 'n2_setbuff'][0]
        cmds.insert(1, (sb, 0x200, 0x600 << 16 | 0x100))
    return bytes(img), bytes(ab), cmds, names

def encode(cmds):
    al = bytearray()
    for i, w1, w2 in cmds:
        # w1 уже может содержать код команды в старших битах (n3_envmixer/n3_setvol) - иначе добавляем
        if w1 >> 24 == 0:
            w1 |= i << 24
        al += struct.pack('>II', w1 & 0xFFFFFFFF, w2 & 0xFFFFFFFF)
    return bytes(al)

def run_one(variant, seed, work, pyemu_only=False):
    _oob['hit'] = False
    img, ab, cmds, names = gen(variant, seed)
    al = encode(cmds)
    img = bytearray(img); img[0x1000:0x1000 + len(al)] = al; img = bytes(img)
    with open(work + '/in.bin', 'wb') as f:
        f.write(struct.pack('>II', 0x1000, len(al))); f.write(img); f.write(ab)
    subprocess.run([os.environ['ABITEST'], variant, work + '/in.bin', work + '/out.bin'], check=True)
    out = open(work + '/out.bin', 'rb').read()
    cdram, cbuf = out[:DRAM], out[DRAM:DRAM + 0x1000]
    fm = FM(img); fm.spmem[0xFF0 >> 2] = 0x1000; fm.spmem[0xFF4 >> 2] = len(al)
    hle = U.Hle(fm); hle.buf[:] = ab
    table = hle.n3_table(variant) if variant in N3 else hle.n2_table(variant)
    hle.run_alist(table)
    pdram, pbuf = fm.image(), bytes(hle.buf)
    ok = pdram == cdram and pbuf == cbuf
    if not ok and _oob['hit']:
        return True, None
    if not ok:
        bd = [i for i in range(0x1000) if pbuf[i] != cbuf[i]]
        dd = [i for i in range(DRAM) if pdram[i] != cdram[i]]
        return False, (len(bd), bd[:6], len(dd), dd[:6], [names[c[0]] for c in cmds])
    return True, None

if __name__ == '__main__':
    work = sys.argv[1]; variants = sys.argv[2].split(','); n = int(sys.argv[3])
    for v in variants:
        bad = 0
        for seed in range(n):
            ok, info = run_one(v, seed, work)
            if not ok:
                bad += 1
                if bad <= 2: print(v, 'seed', seed, 'MISMATCH', info[:4])
        print(v, 'fail %d/%d' % (bad, n))

def check_prefix(variant, img, ab, cmds, names, k, work):
    al = encode(cmds[:k])
    im = bytearray(img); im[0x1000:0x1000 + len(al)] = al; im = bytes(im)
    with open(work + '/in.bin', 'wb') as f:
        f.write(struct.pack('>II', 0x1000, len(al))); f.write(im); f.write(ab)
    subprocess.run([os.environ['ABITEST'], variant, work + '/in.bin', work + '/out.bin'], check=True)
    out = open(work + '/out.bin', 'rb').read()
    fm = FM(im); fm.spmem[0xFF0 >> 2] = 0x1000; fm.spmem[0xFF4 >> 2] = len(al)
    hle = U.Hle(fm); hle.buf[:] = ab
    table = hle.n3_table(variant) if variant in N3 else hle.n2_table(variant)
    hle.run_alist(table)
    pi = fm.image(); pb = bytes(hle.buf)
    ok = pi == out[:DRAM] and pb == out[DRAM:DRAM + 0x1000]
    if ok: return None
    cb = out[DRAM:DRAM + 0x1000]
    bd = [j for j in range(0x1000) if pb[j] != cb[j]]
    dd = [j for j in range(DRAM) if pi[j] != out[j]][:6]
    return len(bd), bd[:6], dd

def first_bad(variant, seed, work):
    img, ab, cmds, names = gen(variant, seed)
    lo, hi = 0, len(cmds)       # lo: ok, hi: bad
    if check_prefix(variant, img, ab, cmds, names, hi, work) is None: return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if check_prefix(variant, img, ab, cmds, names, mid, work) is None: lo = mid
        else: hi = mid
    i, w1, w2 = cmds[hi - 1]
    return hi, names[i], hex(w1), hex(w2), check_prefix(variant, img, ab, cmds, names, hi, work)
