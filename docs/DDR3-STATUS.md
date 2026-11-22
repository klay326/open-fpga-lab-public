# DDR3 on the open flow: status (2026-11-22)

**Short version:** DDR3 works on this board with Vivado's MIG (QMTech's test
bitstream, flashed from the Mac, calibrates and passes). The open flow now
*builds* a LiteX DDR3 SoC (with a newer prjxray-db), but it does **not run**. This isn't a Mac vs Omarchy difference. Both hosts produce identical
results for the same tool versions. What changed is the prjxray-db
version, not the OS.

## Why it failed on Omarchy

The Omarchy toolchain pins prjxray-db at `7a36171`. A DDR3 build
(`qmtech_kintex7_devboard.py --toolchain=openxc7`, no
`--integrated-main-ram-size`) gets through yosys and nextpnr and then dies in
`fasm2frames`, because the db has no bits for 5 features nextpnr emits for
the HP-bank DDR3 IO (bank column `INT_R_X95Y101..Y123`):

| feature | sites |
|---|---|
| `INT_R.IOB_COL_BANK_ACTIVE` | 21 |
| `INT_R.IOB_COL_OBUF_CASCADE_Y1` | 21 |
| `RIOB18.IOB_Y0.OBUF_HP_BANK_GLUE` | 24 |
| `RIOB18.IOB_Y1.OBUF_HP_BANK_GLUE` | 23 |
| `RIOB18.IOB_Y0.SSTL12_SSTL135_SSTL15.IN_DIFF` | 2 (DQS pair) |

The Mac hits exactly the same errors at the same pin.

## What the newer db changes

Upstream `openXC7/prjxray-db` HEAD (`0c3504f`, 99 commits past the pin)
added these in `3bd17eb` ("kintex7: HP-bank IOB18/INT IOB_COL segbits").
With it, `fasm2frames` succeeds and a full bitstream comes out. **But every
one of those bits is *mirrored*, not fuzzed on Kintex-7** (`*.origin_info.db`):

- `IOB_COL_*`, `SSTL..IN_DIFF`: `origin:virtex7-arch-mirror`
- `OBUF_HP_BANK_GLUE`: `origin:liob18-arch-mirror` (copied from the left-side IOB18 tiles)

The db update is otherwise harmless. Re-assembling the known-good BRAM-only
`litex-minimal` `.fasm` with HEAD gives **bit-identical frames** to the pinned db.

## Hardware results (Mac build, HEAD db, same chipdb)

| build | nextpnr sys clk | result |
|---|---|---|
| BRAM-only, 100 MHz (reference) | 83.7 MHz (FAIL at 100) | boots, memtest OK, firmware runs |
| DDR3, 100 MHz | 87.1 MHz (FAIL at 100) | BIOS banner, then endless `*** disabled spurious irq 3 ***` before SDRAM init |
| DDR3, 75 MHz | 85.0 MHz (PASS) | prints the first logo characters, then dead (no console) |

IRQ 3 isn't assigned (UART=0, timer0=1), so an unconnected interrupt input
is reading as asserted. That's the same symptom as the reverted
`fpga/overclock-experiment/`. Both designs change the clock tree. But the
75 MHz build fixes timing and gets *worse*, so plain sys-clock setup
violations aren't the explanation.

## Leading hypothesis (unproven)

The mirrored bits are wrong for Kintex-7. `IOB_COL_*` live in `INT_R`, which
are general routing tiles. If the Virtex-7 bit positions don't hold on
Kintex-7, setting them turns on unknown routing in the DDR3 bank column and
corrupts whatever fabric logic passes through. That would explain failures
that differ from build to build in logic (CPU, IRQ) that has nothing to do
with DDR3. None of the 6 `IOB_COL_*` bits collide with a *known* `INT_R`
feature, but the db only knows the bits someone has identified.

## Ground truth from Vivado (done 2026-11-22)

QMTech's Vivado 2018.3 MIG example (`Test04_DDR3_mig_7series_0_3_ex_.zip`,
`impl_2/example_top.bit`: DDR3-800, x16, 4:1 PHY, LED0/R26 =
`init_calib_complete`, LED1/P26 = `tg_compare_error`) flashes fine with
openFPGALoader. I decoded it with prjxray `bit2fasm` (HEAD db) and
compared it with nextpnr's DDR3 `.fasm`. The DDR3 IO tiles match
(`RIOB18_X95Y101..Y147`):

| feature | Vivado, DDR3 column | nextpnr |
|---|---|---|
| `IOB_COL_BANK_ACTIVE` | 23 tiles | 21 tiles (20 match) |
| `IOB_COL_OBUF_CASCADE_Y1` | **1 tile** (`INT_R_X95Y137`) | **21 tiles** |
| `OBUF_HP_BANK_GLUE` | **1 site** (`RIOB18_X95Y137.IOB_Y1`) | **47 sites** |

The `OBUF_CASCADE_Y1` bit pattern (`30_041 32_016 33_061`) also decodes in
71 ordinary fabric INT tiles of the Vivado design, nowhere near IO. So those
bits overlap real routing PIPs, and nextpnr setting them in 20 extra
`INT_R_X95` tiles turns on 20 stray routing connections. That's the best
explanation for the CPU corruption so far.

**Hardware results (2026-11-22):**

- **The Vivado MIG example passes on this board.** The user LEDs are active-low
  (3V3 → 1K → LED → FPGA pin). D2 (R26, `init_calib_complete`) is off, so
  calibration is done. D3 (P26, `tg_compare_error`) is lit, so there are no
  errors. D7 (N26, unused) glows faintly from Vivado's default pull-down. So
  the DDR3 chip, the board wiring and the JTAG flow are all fine. The problem
  is in the open toolchain.
- **Removing the over-applied bits doesn't help.** `ddr3-fixed.bit` is the
  nextpnr DDR3 `.fasm` (100 MHz) with the 66 extra `OBUF_CASCADE_Y1` /
  `OBUF_HP_BANK_GLUE` lines dropped. It fails exactly like the original: banner,
  then a `spurious irq 3` flood (2,098 lines in 45 s). The over-application is
  real, but it isn't what breaks the CPU, so the hypothesis below is
  **refuted** for those two features.

## Next experiments, cheapest first

1. ~~Strip the over-applied mirrored bits~~: done, no change (see above).
   Next is a **full Vivado-vs-nextpnr diff of the whole DDR3 PHY region**
   (`RIOI`/`HCLK_IOI` tiles: OSERDES/ISERDES/IDELAY/ODELAY/IDELAYCTRL/DCI
   features), not just the five mirrored features. The CPU fails at
   interrupt enable, before any DDR access, which points at something
   global, e.g. clocking (`HCLK_IOI`, BUFR/BUFIO) or IDELAYCTRL/DCI. Something
   that disturbs the sys clock domain fits that better than local IO bits do.
2. A LiteX DDR3 build with the SDRAM controller kept but the PHY outputs
   tied off, to bisect whether the PHY's IO, clocking or the CRG is what
   corrupts the CPU.
3. Same build with the CPU and BIOS forced far away from the X95 column
   (pblock-style placement constraints), to see whether the corruption is local.
4. ~~Get ground truth~~ (done, see above): a Vivado-generated DDR3 bitstream for this
   board (QMTech ships one: `handoff/board-docs/.../Test04_DDR3_mig_7series_*.zip`)
   → `bit2fasm` → compare the `INT_R_X95*` / `RIOB18` features against the
   mirrored ones. That gives the real Kintex-7 answer and is worth upstreaming.

## Reproduce

```bash
source ~/opt/openxc7/export.sh
export PRJXRAY_DB_DIR=$HOME/opt/openxc7/src/prjxray-db-head   # git worktree at openXC7/prjxray-db 0c3504f
source ~/opt/litex/venv/bin/activate
mkdir -p build-qmtech-k7-ddr3 && cd build-qmtech-k7-ddr3
python3 ../litex-soc/qmtech_kintex7_devboard.py --toolchain=openxc7 --build   # add --sys-clk-freq 75e6 for the 75 MHz variant
```

## Standalone (non-LiteX) DDR3 test: `fpga/ddr3-test/` (WIP, 2026-11-22)

UberDDR3 controller/PHY (from openXC7 demo-projects `ddr3-test-arty-s7`)
ported to this board: PLL from 50 MHz, the same DDR3 LOCs as LiteX, UART on
JP5 at 9600. `?` returns `'@'` + the calibration state, `0`-`5` return DQS
ISERDES samples, and `test.py` runs the full write/read round-trip.

Findings so far:
- Builds clean with the HEAD db after replacing the PHY's fabric-inverted
  `!i_ddr3_clk` with a PLL 180-degree clock (`i_ddr3_clk_180`). Before that,
  fasm2frames failed on `RIOI_TBYTESRC.IOI_OCLKM_0.IOI_IMUX31_1`.
- The controller loops MPR_READ -> COLLECT_DQS -> ANALYZE_DQS (states 2-4)
  and never calibrates. DQS ISERDES samples read as all 0.
- **The capture path works.** Opposite pulls on the DQS legs (patched in the
  FASM) make lane 0 read `0xff` or `0x00`. So the DRAM isn't answering, which
  puts the problem on the command/CK side.
- Making the IOB glue bits match Vivado's (IBUF/OBUF_HP_BANK_GLUE, IOB_COL_*)
  changes nothing.
- `ODELAY_SUPPORTED=1` (HP bank): prjxray-db has **no feature to route an
  ODELAYE2 output back to the pad** in HP IOI tiles. Only
  `RIOI_O0.RIOI_OLOGIC0_OQ always` and `OMUX.D1` exist. So ODELAYs are
  effectively dropped and the controller's command/CK delay assumptions break.
  Still stuck in states 2-4.
- `ODELAY_SUPPORTED=0` with CK forwarded through an ODDR in the IO tile
  (D1=0, D2=1, C=i_ddr3_clk): CK's IOLOGIC now matches Vivado's exactly.
  Still stuck in states 2-4.
- **nextpnr-xilinx VREF bug:** it ignores `INTERNAL_VREF` and always emits
  `HCLK_IOI.VREF.V_675_MV` (right for SSTL135/DDR3L, wrong for this SSTL15
  board). Vivado sets `HCLK_IOI_X235Y130.VREF.V_750_MV`. `fasm_fixup.py`
  rewrites it. No change on its own (DQS doesn't need VREF; DQ reads will).
- **nextpnr-xilinx SING-tile bug (fixed locally, upstreamable):** the
  UberDDR3 bitslip-training OSERDES->ISERDES pairs connect only through OFB.
  nextpnr places such pairs on the first free OSERDES bels anywhere, which
  includes single-site `LIOI3_SING` tiles. There it emits
  `ILOGIC_Y1.ISERDES.OFB_USED` alongside the `ILOGIC0_OFB` pip, so the loopback
  is dead and calibration hangs in state 1 (BITSLIP_DQS_TRAIN_1). Whether you
  hit it depends on placement luck: the ODELAY=1 build happened to dodge it.
  The fix is to skip `*_SING` tile types in `pack_io_xc7.cc`
  (`tools/nextpnr-xilinx/skip-sing-tiles-for-ofb-serdes.patch`).
- **IOB config now matches Vivado on every DDR3 pin.** `fasm_fixup.py`
  replaces nextpnr's HP-bank glue bits with Vivado's per-pin set
  (`vivado-bank34-glue.txt`): nextpnr put `OBUF_HP_BANK_GLUE` on every output,
  but Vivado puts it only on the LVCMOS15 RESET#. It also replaces nextpnr's
  partial `LVCMOS15_LVCMOS18.DRIVE.I12_I16...` on RESET# with the full
  `LVCMOS15.DRIVE.I12`. The partial feature misses 7 drive bits and forces
  39_23 low where I12 needs it high, a real nextpnr/db bug for any LVCMOS15
  output in an HP bank. The only remaining IOB difference is on Vivado's input
  side for DQ/DQS: IOBUF_DCIEN control (`DCITERMDISABLE`, `IBUFDISABLE`) and
  `IBUF_LOW_PWR=FALSE`. Those don't explain a DRAM that never drives DQS.
  **Still stuck in states 2-4, DQS all 0.**
- **Timing is now the prime suspect.** The controller clock domain makes
  only 47-64 MHz across 6 placer seeds, against 83.33 MHz. The worst path is
  the calibration FSM (`state_calibrate` -> a CE), 15.7 ns, 86% of it routing.
  The DDR:controller ratio is fixed at 4:1, and DDR3 (DLL on) can't run below
  ~303 MHz, so the controller can't simply be slowed down. nextpnr's xc7
  timing model is pessimistic (LiteX ran fine at 100 MHz with an "84 MHz"
  report), but probably not by 30%.

### Root causes found and fixed (2026-11-22, later)

1. **CS# is tied low on this board, and the controller relies on CS#.**
   UberDDR3 fills each 4:1 command slot with that slot's opcode (ACT, RD,
   WR) and deselects the unused ones with CS#=1. With CS# grounded, the
   DRAM executed every dummy ACT/RD/WR, including during init, so it was
   never initialized. Found in simulation (`sim/`, Micron model): "ACT during
   init" right after CKE. Fix (`ddr3_phy.v`, `cmd_gated`): a deselected
   slot becomes a NOP. After that, the board's DRAM drives DQS on MPR reads
   for the first time.
2. **The vendored controller was old.** openXC7's demo copy failed write
   calibration in simulation too, on every attempt. Current upstream
   (79d8fd3) passes upstream's own Icarus test at exactly this config (12/3
   ns, no ODELAY, x16). `fpga/ddr3-test` now uses upstream RTL plus our
   patches, and `sim/run.sh` reaches **DONE_CALIBRATE** with CS# tied low as
   on the board. It also passes nextpnr timing (101 MHz vs 83.33).
   (Testbench gotcha: clocks must start so that ddr_clk rises with ctrl_clk.
   My first testbench had CLKDIV 180 degrees off CLK, which faked a
   one-cycle write offset.)
3. **JTAG got marginal after the USB replug.** At 12 MHz, and even at 3 MHz
   for this design, configuration ends `done 0` with no CRC error, because
   prjxray bitstreams carry no CRC. At 1.5 MHz it's `done 1`
   (`make program JTAG_FREQ=1500000`, ~2 min). Re-seat the cable and ribbon
   before trusting 12 MHz again.

Hardware status with upstream RTL: the first attempt reaches state 14
(write-calibration check), restarts once, then loops in read DQS
calibration (states 2-5). DQS toggles on reads (sticky OR 0xff, AND 0x00).
Next: find out why read calibration doesn't lock on the board when it does
in simulation (board flight time vs IDELAY sweep range; try SPEED_BIN 3
and a different read-delay start; log `dqs_start_index` / idelay taps over
the UART).

### Offline round: why the board fails where the sim passes (2026-11-22)

- **Trace delay isn't it.** `sim/run.sh` has `BUS=<ps>` (Micron model delay
  on every DRAM pin, each way). Calibration reaches DONE at 0, 250, 500, 750,
  1000 and 1500 ps; longer delays only take longer to lock.
- **FPGA-side read termination is missing.** Vivado's MIG on this board uses
  DCI on DQ/DQS (`dci_inouts_inputs=1`, IOBUF_DCIEN), so the read lines are
  terminated at the FPGA. prjxray-db has no DCI termination features (only
  the per-pin `DCITERMDISABLE` mux), and HP banks have no `IN_TERM`
  fallback, so the open flow leaves the read lines unterminated at the FPGA.
  This is the leading suspect for read calibration failing on the board.
- **DRAM-side drive and ODT now match MIG.** `DIC=2'b01` (RZQ/7, 34 ohm) and
  `RTT_NOM=3'b001` (RZQ/4, 60 ohm), instead of UberDDR3's RZQ/6 defaults.
  Sim still reaches DONE. Not yet tried on the board.
- **Board-side visibility:** `make CAL_DEBUG=1` builds with UberDDR3's
  `UART_DEBUG_READ_LEVEL`. JP5 TX then prints every read-calibration step
  (dqs_start_index, repeat counts, "Glitch: Reached End") at 9600 8N1,
  instead of serving the UART bridge. Its debug UART is renamed
  `uber_uart_tx` to avoid clashing with the bridge's `uart_tx`.

Next on the board: flash the normal build (MIG drive/ODT) and see whether
read calibration locks. If not, flash `CAL_DEBUG=1` and read the log. If
termination is the cause, options are a lower DDR clock (DLL-off mode) or
adding DCI support to prjxray (fuzz the HCLK_IOI/IOB DCI bits).

### Pad read-back probes (2026-11-22)

With no scope available, the control, address and bank outputs (and CK)
are now IOBUFs with T=0. They drive exactly as an OBUF would, and their
receivers read the pad back. A 200 MHz sampler, not locked to the 333 MHz
CK, keeps sticky OR/AND registers, readable over the UART (`pads.py`,
`abpads.py`; `6`/`7`/`8` and `9`..`>`).

- **CK was a flat line in every build with the ODDR.** In this flow, an ODDR
  in an HP IO tile only ever drives D1: D1=0/D2=1 reads 0, D1=1/D2=0 reads 1.
  A slow square wave on D1=D2 does reach the pad, so the clock, output path
  and probe all work, and only the falling-edge (D2) half is dead. The same
  holds with D1/D2 from flip-flops instead of constants. CK now comes from an
  OSERDESE2 (DDR 8:1, pattern 0101_0101, the same setup the DQS OSERDES
  uses), and it toggles on the pad. This is an open-flow bug worth reporting
  (nextpnr-xilinx or prjxray-db: ODDR in `RIOI_TBYTESRC`).
- With CK running: RESET# and CKE are high, and RAS#/CAS#/WE# toggle. Since
  configuration, A0-A10, A12, A13 and BA0-2 have all toggled; A11 stayed 0,
  which is plausible for these MR values. CS# is tied low on the board (MIG
  has no CS# pin either).
- DQS with opposite pulls reads `0xff` on both lanes, so the FPGA releases
  DQS and the capture path works. Without pulls it reads `0x00`: **the DRAM
  still never drives DQS** during MPR reads. Inverting the CK pattern (moving
  the command sampling point half a period) changes nothing.

So every DRAM input pin now toggles, and the DRAM still doesn't answer. What
the probes can't show is timing at the DRAM: command/address setup and hold
around CK, the CK frequency itself, and whether the init sequence and its
timing are right.

Next, cheapest first:
1. Simulate the PHY plus controller init with a DDR3 model (Micron's
   `ddr3.v`; UberDDR3 has `MICRON_SIM`) at the exact parameters used here,
   to rule out a port/config error in init.
2. Capture the pad-level command stream in fabric: ISERDES on the RAS#,
   CAS#, WE# and A10 pads, clocked like the DRAM, into a small trace buffer
   dumped over the UART. That shows the actual MRS/ZQCL/MPR sequence and its
   alignment to CK.
3. The controller-domain timing failure (47-64 MHz vs 83 MHz, below)
   remains: pipeline the calibration FSM.

Older notes:
1. Find out *which* endpoints fail (all of them, not just the worst). If any
   feed the command OSERDES D inputs, commands are being corrupted. A
   pipeline register stage there is cheap.
2. Pipeline or floorplan the calibration FSM so the controller domain meets
   83 MHz in nextpnr. Then retest.
3. Run the same RTL through Vivado (synthesis + P&R only, same XDC). If
   Vivado's build calibrates, the RTL is good and the bug is open-flow
   timing/routing. If it doesn't, the port (board params, CK polarity, init)
   is wrong.
