"""Chip-level cocotb tests: TAP, IJTAG network, Fault Hunt, BIST, RO bank."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from jtag import Jtag, IR_BYPASS, IR_IJTAG, IR_FH_DIRECT
from network import Network
import model as M

TCK_NS = 100  # 10 MHz


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, TCK_NS, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    j = Jtag(dut)
    await j.reset()
    return j


def pin(dut, bit):
    return (int(dut.uo_out.value) >> bit) & 1


def cfg_word(key, en=1):
    return (en << 8) | key


def ans_word(site, typ, submit=1):
    return (submit << 7) | (typ << 4) | site


async def apply_pattern(net, a, b, op):
    """Load operands, capture, unload. Returns the captured response byte."""
    await net.scan({"FH_SCAN": M.scan_word(a, b, op)})       # shift operands in
    res = await net.scan({"FH_SCAN": M.scan_word(a, b, op)}) # capture + shift out
    return res["FH_SCAN"] & 0xFF


# ---------------------------------------------------------------- TAP
@cocotb.test()
async def test_tap_basics(dut):
    j = await setup(dut)
    assert await j.shift_ir(IR_BYPASS) == 0b0001, "IR capture must be 0001"
    assert await j.dr_length() == 1, "BYPASS after reset must be 1 bit"
    await j.shift_ir(0x3)  # unused opcode -> bypass
    assert await j.dr_length() == 1


# ---------------------------------------------------------------- network
@cocotb.test()
async def test_ijtag_lengths(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    assert await j.dr_length() == 3, "all SIBs closed = 3 bits"
    net.reset_state()

    combos = [
        ({"SIB_A": 1}, 3 + 12 + 1 + 8),
        ({"SIB_B": 1}, 3 + 20),
        ({"SIB_C": 1}, 3 + 24),
        ({"SIB_A": 1, "SIB_B": 1, "SIB_C": 1}, 3 + 21 + 20 + 24),
    ]
    for opens, expect in combos:
        net.reset_state()
        await net.scan(opens)
        if opens.get("SIB_A"):
            await net.scan({"SIB_A1": 1})
            expect += 19
        assert net.length() == expect
        got = await j.dr_length()
        assert got == expect, f"{opens}: expected {expect}, got {got}"
    dut._log.info("fully open path = 87 bits: OK")


@cocotb.test()
async def test_fh_direct(dut):
    j = await setup(dut)
    await j.shift_ir(IR_FH_DIRECT)
    assert await j.dr_length() == 19


# ---------------------------------------------------------------- Fault Hunt
@cocotb.test()
async def test_alu_fault_free(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    net.iwrite("FH_CFG", cfg_word(0, en=0))
    net.iread("FH_SCAN")
    await net.iapply()
    fh = M.FaultHunt()
    rng = random.Random(1)
    for _ in range(40):
        a, b, op = rng.randrange(16), rng.randrange(16), rng.randrange(8)
        got = await apply_pattern(net, a, b, op)
        exp = fh.capture(a, b, op)
        assert got == exp, f"a={a} b={b} op={op}: got {got:08b} exp {exp:08b}"


@cocotb.test()
async def test_faults_match_model(dut):
    """For keys across all ALU levels, silicon response == golden faulty model."""
    j = await setup(dut)
    net = Network(j)
    await net.select()
    rng = random.Random(7)
    keys = [0x05, 0x2A, 0x41, 0x7F, 0x83, 0xB0, 0xC2, 0xC9]
    last = (0, 0, 0)   # operands left in FH_SCAN (prev for transition faults)
    for key in keys:
        lvl, f1, f2 = M.decode_key(key)
        if f1[1] >= M.SIB_CLOSED:
            continue
        net.iwrite("FH_CFG", cfg_word(key))
        net.iread("FH_SCAN")
        await net.iapply()
        fh = M.FaultHunt(key, True)
        fh.capture(*last)
        for _ in range(12):
            a, b, op = rng.randrange(16), rng.randrange(16), rng.randrange(8)
            last = (a, b, op)
            got = await apply_pattern(net, a, b, op)
            exp = fh.capture(a, b, op)
            assert got == exp, f"key {key:#x} {f1}: a={a} b={b} op={op} got {got:08b} exp {exp:08b}"
        dut._log.info(f"key {key:#04x} L{lvl+1} f1={M.SITE_NAMES[f1[0]]}/{M.TYPE_NAMES[f1[1]]}: OK")


@cocotb.test()
async def test_answer_pass(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    key = 0x05
    _, (site, typ), _ = M.decode_key(key)
    net.iwrite("FH_CFG", cfg_word(key))
    await net.iapply()
    assert pin(dut, 1) == 0
    net.iwrite("FH_ANS", ans_word((site + 1) & 0xF, typ))     # wrong answer
    await net.iapply()
    await j.idle(2)
    assert pin(dut, 1) == 0, "wrong answer must not pass"
    net.iwrite("FH_ANS", ans_word(site, typ))
    await net.iapply()
    await j.idle(2)
    assert pin(dut, 1) == 1, "correct answer must set FH_PASS"
    assert (int(dut.uo_out.value) >> 2) & 3 == 0, "level pins = 0 for level 1"


@cocotb.test()
async def test_network_fault(dut):
    """Level 4 broken-SIB fault changes the scan path length."""
    j = await setup(dut)
    net = Network(j)
    await net.select()
    key = next(k for k in range(0xC0, 0x100)
               if M.decode_key(k)[1] == (1, M.SIB_CLOSED))     # SIB_B stuck closed
    net.iwrite("FH_CFG", cfg_word(key))
    await net.iapply()
    await net.scan({"SIB_A": 0, "SIB_B": 1, "SIB_C": 0})       # try to open B
    assert await j.dr_length() == 3, "SIB_B stuck closed: B segment must stay out"
    # TMS reset clears the fault (can't brick the chip)
    await j.reset()
    net.reset_state()
    await net.select()
    await net.scan({"SIB_B": 1})
    assert await j.dr_length() == 23


# ---------------------------------------------------------------- BIST
@cocotb.test()
async def test_bist(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    net.iwrite("BIST_CTRL", 0b0001)          # start, seed0, len0 (256)
    await net.iapply()
    await j.idle(300)
    assert pin(dut, 4) == 1, "BIST_DONE"
    net.iread("BIST_SIG")
    res = await net.iapply()
    exp = M.bist(M.FaultHunt(), 0, 256)
    assert res["BIST_SIG"] == exp, f"MISR {res['BIST_SIG']:#06x} != model {exp:#06x}"
    assert pin(dut, 5) == 1, "BIST_PASS"
    dut._log.info(f"golden signature {exp:#06x}")


@cocotb.test()
async def test_bist_catches_fault(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    net.iwrite("FH_CFG", cfg_word(0x2A))
    net.iwrite("BIST_CTRL", 0b0000)
    await net.iapply()
    net.iwrite("BIST_CTRL", 0b0001)
    await net.iapply()
    await j.idle(300)
    net.iread("BIST_SIG")
    res = await net.iapply()
    fh = M.FaultHunt(0x2A, True)
    assert res["BIST_SIG"] == M.bist(fh, 0, 256)
    assert pin(dut, 5) == 0, "faulty ALU must fail BIST"


# ---------------------------------------------------------------- RO bank
@cocotb.test()
async def test_ro_bank(dut):
    j = await setup(dut)
    net = Network(j)
    await net.select()
    counts = []
    for sel in range(4):
        ctrl = (0 << 3) | (sel << 1) | 1          # gate=256 TCK, sel, en
        net.iwrite("RO_CTRL", ctrl)
        await net.iapply()
        net.iwrite("RO_CTRL", ctrl | 0x80)        # start
        await net.iapply()
        await j.idle(270)
        net.iread("RO_CNT")
        net.iread("RO_CTRL")
        res = await net.iapply()
        assert res["RO_CTRL"] >> 7 == 1, "done flag"
        counts.append(res["RO_CNT"])
    # sim half-periods 3/4/5/2 ns, gate 25.6 us, /16 prescale
    dut._log.info(f"RO counts (INV, NAND, NOR, INVx4) = {counts}")
    for c, half in zip(counts, (3, 4, 5, 2)):
        exp = 25600 / (2 * half) / 16
        assert abs(c - exp) / exp < 0.05, f"count {c} vs ~{exp:.0f}"
