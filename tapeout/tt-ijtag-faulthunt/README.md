# IJTAG Fault Hunt (Tiny Tapeout, GF180)

An IEEE 1149.1 TAP plus an IEEE 1687 (IJTAG) SIB network that hosts three
instruments:

- **Fault Hunt**: a 4-bit ALU with 16 injectable fault sites. It's a DFT diagnosis puzzle.
- **BIST**: an LFSR/MISR logic BIST on the same ALU.
- **RO bank**: four ring oscillators with a gated counter, for process and temperature monitoring.

## Layout

```
src/
  project.v      top: pins, TAP, SIB network, TDRs, instruments
  tap.v          1149.1 TAP + 4-bit IR (no IDCODE)
  ijtag_cells.v  generic TDR (shift + update) and SIB (with fault overrides)
  fault_hunt.v   ALU, fault sites, key->fault generator, answer checker
  bist.v         LFSR/MISR BIST
  ro_bank.v      rings (sim model + synthesis TODO), prescaler, counter, gate timer
test/
  jtag.py        pin-level JTAG driver (port to RP2040 MicroPython later)
  network.py     IJTAG path model + iWrite/iRead/iApply mini-retargeter
  model.py       golden ALU/fault/BIST models
  test_chip.py   10 cocotb tests
ijtag/faulthunt.icl   first-pass ICL
```

## Run

```
cd test && make            # needs iverilog >= 12, cocotb >= 2.0
```

Current status: 10/10 tests pass at RTL. Generic GF180 synthesis
(gf180mcu_fd_sc_mcu7t5v0, tt corner) gives 1,255 cells and about 35,900 µm².
That's roughly 33% of a 1x2 GF tile area (2 × 350 × 160 µm), before the rings
are hand-instantiated.

## Before the first harden (TODO)

1. Copy `src/` and `test/` into the Tiny Tapeout GF180 Verilog template repo. Set
   `top_module: tt_um_klay_ijtag_faulthunt`, tiles `1x2`, clock 10 MHz in info.yaml.
2. Replace the generic gates in `ro_ring` with hand-instantiated GF180 cells, and
   add loop-break constraints (set_disable_timing) for STA.
3. `BIST_GOLDEN` in project.v is 16'h03B5. If the ALU or BIST changes, recompute it with
   `python3 -c "import model; print(hex(model.bist(model.FaultHunt())))"` (run in test/).

## Fault Hunt cheat sheet (spoilers)

| Level (key[7:6]) | Faults |
| --- | --- |
| 1 (00) | one stuck-at |
| 2 (01) | two stuck-at |
| 3 (10) | one slow-to-rise / slow-to-fall (needs pattern pairs) |
| 4 (11) | one bridge (pairs s0/s1, s2/s3, and[1]/or[2]) or a broken SIB_B / SIB_C |

Answer = FH_ANS `{submit, type[2:0], site[3:0]}`. Types: 0 SA0, 1 SA1, 2 STR,
3 STF, 4 bridge (either site of the pair), 5 SIB stuck-closed, 6 SIB stuck-open
(site 1 = SIB_B, 2 = SIB_C). A TMS reset clears any fault.
