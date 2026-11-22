"""IJTAG network model + mini PDL-style retargeter (iWrite / iRead / iApply).

Tracks SIB state, computes the active scan path, and turns register-level
reads/writes into DR scans. This is the seed of the standalone ICL/PDL
retargeter planned for after tapeout.
"""
from jtag import IR_IJTAG

WIDTH = {
    "FH_CFG": 12, "FH_SCAN": 19, "SIB_A1": 1, "FH_ANS": 8, "SIB_A": 1,
    "BIST_CTRL": 4, "BIST_SIG": 16, "SIB_B": 1,
    "RO_CTRL": 8, "RO_CNT": 16, "SIB_C": 1,
}
# Which SIBs must be open (outermost first) for a register to be on the path
NEEDS = {
    "FH_CFG": ["SIB_A"], "FH_SCAN": ["SIB_A", "SIB_A1"], "SIB_A1": ["SIB_A"], "FH_ANS": ["SIB_A"],
    "BIST_CTRL": ["SIB_B"], "BIST_SIG": ["SIB_B"],
    "RO_CTRL": ["SIB_C"], "RO_CNT": ["SIB_C"],
    "SIB_A": [], "SIB_B": [], "SIB_C": [],
}
SIBS = ("SIB_A", "SIB_A1", "SIB_B", "SIB_C")


def path(u):
    """Active scan path, TDI -> TDO, for SIB states u (dict name -> 0/1)."""
    p = []
    if u["SIB_A"]:
        p.append("FH_CFG")
        if u["SIB_A1"]:
            p.append("FH_SCAN")
        p += ["SIB_A1", "FH_ANS"]
    p.append("SIB_A")
    if u["SIB_B"]:
        p += ["BIST_CTRL", "BIST_SIG"]
    p.append("SIB_B")
    if u["SIB_C"]:
        p += ["RO_CTRL", "RO_CNT"]
    p.append("SIB_C")
    return p


class Network:
    def __init__(self, jtag):
        self.j = jtag
        self.reset_state()

    def reset_state(self):
        self.u = {s: 0 for s in SIBS}
        self.held = {r: 0 for r in WIDTH}   # last value written to each register
        self.pending = {}
        self.reads = set()

    async def select(self):
        await self.j.shift_ir(IR_IJTAG)

    def length(self):
        return sum(WIDTH[r] for r in path(self.u))

    async def scan(self, writes=None):
        """One DR scan on the current path. Returns {reg: captured value}."""
        writes = writes or {}
        p = path(self.u)
        bits = []
        for r in reversed(p):                      # TDO end first
            v = writes.get(r, self.held[r])
            bits += [(v >> i) & 1 for i in range(WIDTH[r])]
        out = await self.j.shift_dr(bits)
        res, k = {}, 0
        for r in reversed(p):
            w = WIDTH[r]
            res[r] = sum(out[k + i] << i for i in range(w))
            k += w
        for r in p:
            if r in writes:
                self.held[r] = writes[r]
            if r in SIBS:
                self.u[r] = self.held[r]
        return res

    # ---- PDL-style API ----
    def iwrite(self, reg, val):
        self.pending[reg] = val

    def iread(self, reg):
        self.reads.add(reg)

    async def iapply(self):
        targets = set(self.pending) | self.reads
        need = {s for r in targets for s in NEEDS[r]}
        for _ in range(4):
            p = path(self.u)
            if all(r in p for r in targets):
                res = await self.scan(dict(self.pending))
                self.pending.clear()
                self.reads.clear()
                return res
            # open the next layer of SIBs on the way to the targets
            await self.scan({s: 1 for s in need if s in p})
        raise RuntimeError(f"could not reach {targets}")
