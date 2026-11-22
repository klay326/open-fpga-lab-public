# QMTech Kintex-7 RISC-V Computer

Bring-up and demo projects for a **QMTech XC7K325T Kintex-7 development board**,
built entirely through an open-source FPGA toolchain (yosys + nextpnr-xilinx +
prjxray), flashed with a (patched) **openFPGALoader** -- fully open source,
and it runs natively on Linux and Apple Silicon macOS.

The bigger goal this project is working towards: pairing this FPGA with a
Raspberry Pi Compute Module 4 to build a small heterogeneous computer -- an
ARM Linux side (the CM4) and a RISC-V side (synthesized into the FPGA fabric),
talking to each other.

Setting this up on a Mac? See [`MACOS-SETUP.md`](MACOS-SETUP.md) -- the
verified native recipe (toolchain built from pinned commits, bit-identical
output to the Linux build, JTAG flashing included).

## Board

- QMTech XC7K325T Kintex-7 core board on their Development Board carrier
- Xilinx Platform Cable USB (clone) for JTAG
- Board manual / schematics: see `ChinaQMTECH/QMTECH_Kintex-7_Development_Board`
  and `ChinaQMTECH/QMTECH_XC7K325T_CORE_BOARD` on GitHub

## Toolchain setup

**Synthesis (fully open source):**
```bash
git clone https://github.com/openXC7/toolchain-installer
cd toolchain-installer
./toolchain-sources-builder.sh   # builds yosys, nextpnr-xilinx, prjxray
source /opt/openxc7/export.sh
```
Note: as of writing, the installer script doesn't correctly build `yosys`
(newer yosys dropped its classic Makefile for CMake) or install prjxray's
Python side (needed for `fasm2frames`) -- see git history / commit notes in
this repo for the exact workarounds if you hit the same issues.

**Flashing:** openFPGALoader plus the fix from upstream PR
[trabucayre/openFPGALoader#772](https://github.com/trabucayre/openFPGALoader/pull/772)
(`tools/openfpgaloader/build.sh`), then `make program`:
```bash
openFPGALoader -c xilinxPlatformCableUsb --freq 12000000 --bitstream <file>.bit
```
The JTAG cable (Waveshare Xilinx Platform Cable USB clone, `03fd:0013`)
already runs its own XPCU firmware; stock openFPGALoader tries to upload
firmware it ignores and then times out. The fix detects the running firmware and
skips the upload. (Earlier notes here blamed the cable's JTAG protocol -- that was
openFPGALoader 1.1.1 run with `--board`, which selects no cable driver. See
`MACOS-SETUP.md`.) Vivado Lab (`vivado_lab -mode batch -source flash.tcl`,
with `connect_hw_server -allow_non_jtag`) still works on x86 Linux as a
fallback.

## Structure

```
fpga/
  openXC7.mk               -- shared build rules (from openXC7/demo-projects)
  blinky/                  -- first bring-up: 3 LEDs flashing in sync
  button-chase/            -- LED chase pattern, pause/reverse via onboard buttons
  overclock-experiment/    -- button-controlled runtime CPU overclock (known broken, see its README)
litex-soc/                 -- the LiteX SoC definition (CPU/memory-map/peripherals)
firmware/
  led-chase/               -- bare-metal RISC-V firmware for the LiteX SoC above
cm4/                       -- CM4 integration: scripts + notes for pairing a
                              Raspberry Pi CM4 with the FPGA (see its README)
tapeout/
  tt-ijtag-faulthunt/      -- Tiny Tapeout GF180 chip (TTGF26c, 1x2 tiles): 1149.1 TAP +
                              IEEE 1687 SIB network hosting a fault-injectable ALU
                              ("Fault Hunt"), LFSR/MISR BIST and a ring-oscillator bank
                              (see its README)
```

### FPGA-only demos (`fpga/`)

Each directory is self-contained: `make` builds the bitstream, `make
program` flashes it over JTAG (`flash.tcl` is the Vivado Lab fallback).

- **blinky** -- clock divider + 3 LEDs, first hardware bring-up
- **button-chase** -- LED chase animation, SW2 pauses/resumes, SW3 reverses
  direction (see the board manual for the actual pin mapping / active-low
  logic)

### RISC-V SoC + firmware (`litex-soc/` + `firmware/led-chase`)

This firmware runs on a [LiteX](https://github.com/enjoy-digital/litex)
SoC (VexRiscv CPU, RV32IM) defined in `litex-soc/` and built via:
```bash
python3 litex-soc/qmtech_kintex7_devboard.py \
    --toolchain=openxc7 --integrated-main-ram-size=0x8000 --build
```
(see `litex-soc/README.md` for the full setup. There's also a
`--with-cm4-uart` flag for a second UART on GPIO14/15 for a Raspberry Pi
CM4 to talk to -- works for interactive commands, but see `cm4/README.md`
for a real, unresolved bug where continuous high-throughput output
(`donut`/`mandel`) gets corrupted on this build specifically)
(`--integrated-main-ram-size` uses internal block RAM instead of DDR3 --
the DDR3 PHY needs I/O primitives that the open-source Kintex-7 database
doesn't fully cover yet, so this is a deliberate simplification: a RISC-V
computer with 32KB of RAM instead of DDR3's 256MB, until that toolchain gap
is closed or a Vivado Standard/Enterprise license is used for synthesis.)

Build the firmware against that SoC's generated headers:
```bash
cd firmware/led-chase
BUILD_DIR=<path-to-litex-build-dir>/build/qmtech_kintex7_devboard make
```

Load it onto the running SoC over serial (no bitstream rebuild needed for
firmware changes):
```bash
litex_term /dev/ttyUSB0 --kernel main.bin --serial-boot
# then at the BIOS prompt: reboot
```

UART wiring (no onboard USB-serial chip on this board): TX/RX on the JP5
expansion header, ball `AE22`/`AF22` (JP5 pins 7/8), ground shared with the
JTAG header (J1 pin 6) or any other board ground point.

**Commands once loaded:**
- `chase` -- LED chase pattern, same idea as the hardware version but now
  it's software you can edit and reload in seconds
- `donut` -- the classic a1k0n spinning ASCII donut, computed live on the
  RISC-V core
- `pi` -- verifies + continuously stress-tests via the Rabinowitz-Wagon pi
  digit spigot algorithm (integer-only, no FPU needed); memory-bound by the
  small on-chip RAM
- `mandel` -- fixed-point (Q16.16) Mandelbrot set renderer + continuous
  stress test; compute-bound rather than memory-bound, much heavier per
  second than `pi`
- `x86` -- a minimal 8086 instruction interpreter (not the full ISA, just
  the handful of opcodes needed for a small test program) executing a real
  x86 binary assembled from `hello8086.asm` with `nasm -f bin` (verified
  with a native run before porting/embedding as a byte array) -- the
  RISC-V core interpreting genuine x86 machine code, instruction by
  instruction. Requires `nasm` only if you want to modify the test program
  and regenerate its byte array; the firmware itself has no assembler
  dependency, the bytes are already baked in

## Known limitations / future work

- **DDR3 builds but doesn't run yet.** The pinned `prjxray-db` (`7a36171`)
  lacks bits for 5 HP-bank IO features. Upstream HEAD adds them, but they're
  mirrored from Virtex-7 (not fuzzed on Kintex-7), and the resulting DDR3
  bitstreams fail on hardware (spurious-IRQ flood at 100 MHz, dead console at
  75 MHz). The hardware results and next experiments are in
  [`docs/DDR3-STATUS.md`](docs/DDR3-STATUS.md).
- **Button-controlled runtime overclock** (a `BUFGMUX`-based clock switch
  selecting between two PLL outputs) was prototyped and confirmed working
  for LED speed, but caused a spurious-interrupt regression that wasn't
  fully run to ground before being reverted. See
  `fpga/overclock-experiment/` for the actual code and a full writeup of
  what broke -- worth revisiting with more time, the core idea (glitchless
  clock switching via a physical button) works.
