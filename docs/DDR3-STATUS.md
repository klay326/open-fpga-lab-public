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
