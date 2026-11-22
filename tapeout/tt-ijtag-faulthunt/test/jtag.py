"""Pin-level IEEE 1149.1 driver for cocotb (TT pinout: clk=TCK, ui_in[0]=TMS, ui_in[1]=TDI, uo_out[0]=TDO).

The same API is meant to be ported to MicroPython on the TT demoboard's RP2040.
Bit lists are LSB-first (first element = first bit shifted = ends nearest TDO).
"""
from cocotb.triggers import FallingEdge, RisingEdge

IR_IJTAG = 0x8
IR_FH_DIRECT = 0xA
IR_BYPASS = 0xF


class Jtag:
    def __init__(self, dut):
        self.dut = dut
        self._ui = 0

    def _set(self, tms, tdi):
        self._ui = (self._ui & ~0b11) | (tms & 1) | ((tdi & 1) << 1)
        self.dut.ui_in.value = self._ui

    def set_free_run(self, on):
        self._ui = (self._ui & ~0b100) | ((1 if on else 0) << 2)
        self.dut.ui_in.value = self._ui

    async def step(self, tms, tdi=0):
        """One TCK cycle. Returns TDO as seen at the rising edge."""
        await FallingEdge(self.dut.clk)
        self._set(tms, tdi)
        await RisingEdge(self.dut.clk)
        return int(self.dut.uo_out.value) & 1

    async def reset(self):
        for _ in range(5):
            await self.step(1)
        await self.step(0)  # -> Run-Test/Idle

    async def idle(self, n):
        for _ in range(n):
            await self.step(0)

    async def _shift(self, bits):
        out = []
        for i, b in enumerate(bits):
            out.append(await self.step(1 if i == len(bits) - 1 else 0, b))
        return out

    async def shift_ir(self, val, n=4):
        """RTI -> Shift-IR -> RTI. Returns captured IR bits as int."""
        for t in (1, 1, 0, 0):
            await self.step(t)
        out = await self._shift([(val >> i) & 1 for i in range(n)])
        await self.step(1)  # Update-IR
        await self.step(0)  # RTI
        return sum(b << i for i, b in enumerate(out))

    async def shift_dr(self, bits):
        """RTI -> Shift-DR -> Update-DR -> RTI. Returns shifted-out bits (LSB-first)."""
        for t in (1, 0, 0):
            await self.step(t)
        out = await self._shift(bits)
        await self.step(1)  # Update-DR
        await self.step(0)  # RTI
        return out

    async def dr_length(self, max_len=200):
        """Measure current DR length with a marker bit. Note: the final update writes zeros."""
        pad = max_len
        out = await self.shift_dr([0] * pad + [1] + [0] * max_len)
        for i in range(pad, len(out)):
            if out[i] == 1:
                return i - pad
        return None
