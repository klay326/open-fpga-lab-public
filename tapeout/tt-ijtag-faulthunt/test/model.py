"""Golden models: Fault Hunt ALU (with fault injection), key decode, BIST LFSR/MISR.
Must mirror src/fault_hunt.v and src/bist.v bit-for-bit.
"""
SA0, SA1, STR, STF, BRIDGE, SIB_CLOSED, SIB_OPEN = range(7)
TYPE_NAMES = ["SA0", "SA1", "slow-to-rise", "slow-to-fall", "bridge", "SIB stuck-closed", "SIB stuck-open"]
SITE_NAMES = ["s0", "s1", "s2", "s3", "c1", "c2", "c3", "c4",
              "and[1]", "or[2]", "xor[0]", "SUB decode", "shl[1]", "Z", "V", "y[3]"]


def decode_key(key):
    """key -> (level, f1=(site,type), f2=(site,type) or None)."""
    h = key ^ 0xA7
    h2 = (((h & 0x7) << 5) | (h >> 3)) ^ 0x3C
    lvl = key >> 6
    f1s, f1t = h & 0xF, (h >> 4) & 1
    f2s = h2 & 0xF
    if f2s == f1s:
        f2s ^= 1
    f2t = (h2 >> 4) & 1
    if lvl == 2:
        f1t = 2 + ((h >> 4) & 1)
    elif lvl == 3:
        if not (h >> 5) & 1:
            f1t = BRIDGE
            f1s = {1: 2, 2: 8}.get(h & 3, 0)
        else:
            f1t = SIB_CLOSED + ((h >> 1) & 1)
            f1s = 1 + (h & 1)
    return lvl, (f1s, f1t), ((f2s, f2t) if lvl == 1 else None)


def _fv(t, x, partner, prev):
    return {SA0: 0, SA1: 1, STR: x & prev, STF: x | prev, BRIDGE: x & partner}.get(t, x)


class FaultHunt:
    def __init__(self, key=0, en=False):
        self.prev = 0
        self.set_key(key, en)

    def set_key(self, key, en):
        self.en = en
        self.level, self.f1, self.f2 = decode_key(key)

    def _site(self, sid, v, partner=1):
        if not self.en:
            return v
        s1, t1 = self.f1
        hit1 = t1 <= BRIDGE and ((s1 >> 1 == sid >> 1) if t1 == BRIDGE else s1 == sid)
        o = _fv(t1, v, partner, self.prev) if hit1 else v
        if self.f2 and self.f2[1] <= SA1 and self.f2[0] == sid:
            o = _fv(self.f2[1], o, 1, 0)
        return o

    def eval(self, a, b, op):
        """Returns (y, c, z, n, v, raw[16]) with faults applied. Does not update prev."""
        A = [(a >> i) & 1 for i in range(4)]
        B = [(b >> i) & 1 for i in range(4)]
        S = self._site
        sub_raw = int(op == 1)
        sub = S(11, sub_raw)
        BX = [x ^ sub for x in B]
        cin = sub
        s0_raw = A[0] ^ BX[0] ^ cin
        c1_raw = (A[0] & BX[0]) | (cin & (A[0] ^ BX[0]))
        c1 = S(4, c1_raw)
        p1 = A[1] ^ BX[1]
        s1_raw = p1 ^ c1
        s0 = S(0, s0_raw, s1_raw)
        s1 = S(1, s1_raw, s0_raw)
        c2_raw = (A[1] & BX[1]) | (c1 & p1)
        c2 = S(5, c2_raw)
        p2 = A[2] ^ BX[2]
        s2_raw = p2 ^ c2
        c3_raw = (A[2] & BX[2]) | (c2 & p2)
        c3 = S(6, c3_raw)
        p3 = A[3] ^ BX[3]
        s3_raw = p3 ^ c3
        c4_raw = (A[3] & BX[3]) | (c3 & p3)
        s2 = S(2, s2_raw, s3_raw)
        s3 = S(3, s3_raw, s2_raw)
        c4 = S(7, c4_raw)
        summ = s0 | s1 << 1 | s2 << 2 | s3 << 3

        and1_raw, or2_raw, xor0_raw = A[1] & B[1], A[2] | B[2], A[0] ^ B[0]
        and1 = S(8, and1_raw, or2_raw)
        or2 = S(9, or2_raw, and1_raw)
        xor0 = S(10, xor0_raw)
        and_v = (A[0] & B[0]) | and1 << 1 | (A[2] & B[2]) << 2 | (A[3] & B[3]) << 3
        or_v = (A[0] | B[0]) | (A[1] | B[1]) << 1 | or2 << 2 | (A[3] | B[3]) << 3
        xor_v = xor0 | (A[1] ^ B[1]) << 1 | (A[2] ^ B[2]) << 2 | (A[3] ^ B[3]) << 3
        shl1 = S(12, A[0])
        shl_v = shl1 << 1 | A[1] << 2 | A[2] << 3

        y_raw = {0: summ, 1: summ, 2: and_v, 3: or_v, 4: xor_v, 5: (~a) & 0xF, 6: shl_v, 7: b}[op]
        y3 = S(15, (y_raw >> 3) & 1)
        y = (y_raw & 0x7) | y3 << 3
        is_arith = int(op in (0, 1))
        c = is_arith & c4
        z_raw = int(y == 0)
        z = S(13, z_raw)
        n = (y >> 3) & 1
        v_raw = is_arith & int(A[3] == BX[3]) & int(s3 != A[3])
        v = S(14, v_raw)
        raw = [s0_raw, s1_raw, s2_raw, s3_raw, c1_raw, c2_raw, c3_raw, c4_raw,
               and1_raw, or2_raw, xor0_raw, sub_raw, A[0], z_raw, v_raw, (y_raw >> 3) & 1]
        return y, c, z, n, v, raw

    def capture(self, a, b, op):
        """Like a scan capture: returns resp byte {y,c,z,n,v}, then latches prev."""
        y, c, z, n, v, raw = self.eval(a, b, op)
        self.prev = raw[self.f1[0]]
        return (y << 4) | (c << 3) | (z << 2) | (n << 1) | v


POLY = 0x002D


def galois(x):
    return ((x << 1) & 0xFFFF) ^ (POLY if x & 0x8000 else 0)


def bist(fh, seed_sel=0, length=256):
    lfsr = 0x1D0F if seed_sel else 0xACE1
    misr = 0
    for _ in range(length):
        resp = fh.capture(lfsr & 0xF, (lfsr >> 4) & 0xF, (lfsr >> 8) & 0x7)
        misr = galois(misr) ^ resp
        lfsr = galois(lfsr)
    return misr


def scan_word(a, b, op, resp=0):
    """FH_SCAN register value: {A[3:0], B[3:0], OP[2:0], resp[7:0]} (bit 0 = V)."""
    return (a << 15) | (b << 11) | (op << 8) | resp
