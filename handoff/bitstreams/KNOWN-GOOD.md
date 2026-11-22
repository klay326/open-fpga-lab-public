# Which bitstream is known-good

- **`litex-minimal.bit`** -- the primary known-stable bitstream used for
  the whole session. RISC-V SoC (VexRiscv, RV32IM), BRAM-only (32KB, no
  DDR3), no CM4 UART. All five firmware demos (`chase`, `donut`, `pi`,
  `mandel`, `x86`) confirmed clean on this bitstream. **Start here.**

- **`litex-cm4-uart.bit`** -- same SoC plus a second UART peripheral on
  GPIO14/15 for the CM4. Interactive/low-throughput firmware commands
  (`chase`, `pi`, `x86`) confirmed clean. `donut`/`mandel` (continuous
  high-throughput output) show garbled characters -- a real, isolated,
  still-unresolved bug (ruled out cable/wiring via a controlled A/B test
  against `litex-minimal.bit` with identical wiring). See the main repo's
  `cm4/README.md` for the full writeup.
  **Whichever of these two you flash, you MUST rebuild firmware against
  that exact build's own headers first** -- see `litex-builds/*/software/
  include/generated/csr.h` and HANDOFF.md. Reusing a `main.bin` built for
  the other variant will boot, print "Liftoff!", and hang silently with
  no further output (a CSR-address mismatch, not a hardware fault).

- **`fpga-blinky.bit`** / **`fpga-button-chase.bit`** -- plain Verilog
  demos, no RISC-V SoC at all. Good smoke tests for "is JTAG flashing
  working at all" before touching LiteX.
