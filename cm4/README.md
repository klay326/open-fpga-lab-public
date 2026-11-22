# CM4 integration

The Raspberry Pi Compute Module 4's eMMC is flashed with Raspberry Pi OS
(64-bit, Lite) and reachable over SSH at `qmtech-cm4.local`, seated on the
QMTech carrier next to the FPGA.

## The link: a second UART on GPIO14/15

**Status: partially working.** A second, independent UART peripheral on
GPIO14/TX and GPIO15/RX (`../litex-soc/`, the `--with-cm4-uart` flag) -- the
same physical pins a full-size Raspberry Pi uses for its own primary serial
port, so the CM4's own hardware UART can talk directly to the RISC-V core's
console, no USB-to-TTL adapter needed on that side.

Two separate bugs got tangled together here across two debugging sessions;
both are now understood.

**Bug 1 (fixed): looked like corruption, was actually a stale firmware
build.** This was previously documented as: enabling the flag built and
flashed fine, but a firmware `serialboot` upload over the *existing* debug
UART (JP5, unrelated pins) failed reproducibly with framing errors, and the
BIOS got stuck echoing error bytes afterward. The leading theory at the time
was `nextpnr-xilinx` timing-margin erosion from the added peripheral -- that
theory was wrong. The actual cause: adding `cm4_uart` shifts every other
peripheral's CSR address (`UART` moved from `0x2000` to `0x2800`, `TIMER0`
from `0x1800` to `0x2000`, `CM4_UART` took `0x0`), and the firmware binary
being uploaded (`main.bin`) was still built against the *previous* SoC
variant's generated headers. The BIOS itself was always fine -- it uploaded
and jumped to the firmware correctly every time (`Liftoff!` printed right on
schedule). The firmware then silently wrote its console output to the old,
now-wrong UART address and hung with no visible output, which looked
identical to a corrupted upload from the outside. Rebuilding firmware
against `build-qmtech-k7-cm4`'s own `software/include/generated/csr.h`
(i.e. pointing `BUILD_DIR` at the build actually running on the board, not a
different one) fixed it immediately.

**Takeaway for next time:** whenever the *bitstream* changes to a different
SoC configuration, `firmware/led-chase` must be rebuilt against that
specific build's headers before reloading -- a stale `main.bin` from a
different `--with-*` combination will boot but hang silently after
`Liftoff!`, which is easy to mistake for a hardware/timing bug.

**Bug 2 (confirmed real, unresolved): high-throughput console output gets
corrupted, but only on this bitstream.** With firmware correctly rebuilt for
`--with-cm4-uart`, interactive/low-throughput commands (`chase`, `pi`,
`x86`) run clean. But `donut` and `mandel` -- both continuous, fast,
full-screen ANSI redraws -- show garbled/stray characters mixed into the
output. This was isolated with a controlled A/B test: same physical JTAG
cable, same debug-UART wiring, same firmware source, swapped only the
bitstream. `donut`/`mandel` are clean on the plain `build-qmtech-k7-minimal`
bitstream and corrupt on `build-qmtech-k7-cm4`. That rules out cable/wiring
flakiness (a real, separate issue this project hit earlier while debugging
this) and points back at the `--with-cm4-uart` design itself -- likely
exactly the original timing-margin theory, just showing up as sustained
console-output corruption rather than upload corruption. Not yet root-caused
with a scope or STA report; needs a clear-headed pass before trusting this
build with anything high-throughput.

### CM4-side setup

1. Edit `/boot/firmware/config.txt`:
   ```
   enable_uart=1
   dtoverlay=disable-bt
   ```
   (`disable-bt` matters -- without it, the CM4's primary PL011 UART is
   claimed by Bluetooth and only the much less reliable "mini UART" is
   exposed as the serial port.)
2. Stop the login shell from claiming the port: `sudo raspi-config` ->
   Interface Options -> Serial Port -> "login shell over serial" = No,
   "serial port hardware enabled" = Yes. (Equivalently: remove any
   `console=serial0,...` from `/boot/firmware/cmdline.txt` and
   `sudo systemctl disable --now serial-getty@ttyAMA0.service`.)
3. Reboot, then `/dev/serial0` should exist and be free to use.
4. Either run `litex_term /dev/serial0 --serial-boot --kernel <path>` on the
   CM4 directly (same package, works identically to running it on the dev
   machine), or use `cm4_link.py` here for a quick manual poke at the
   console.

### Verified end to end (2026-11-22)

Both directions work over GPIO14/15 at 115200 8N1, checked with no firmware
(BIOS prompt on JP5, `cm4_uart` driven by hand with `mem_write`/`mem_read`):
- FPGA -> CM4: `mem_write 0xf0000000 <byte>` (CM4_UART RXTX), received
  byte-exact on the CM4 with `stty -F /dev/serial0 115200 raw; cat /dev/serial0`.
- CM4 -> FPGA: `printf "PI!" > /dev/serial0`, read back on the FPGA with
  `mem_read 0xf0000008` (RXEMPTY), `mem_read 0xf0000000` (RXTX),
  `mem_write 0xf0000010 0x2` (EV_PENDING, pop) per byte.

Three things had to be fixed to get there:
1. **TX/RX were swapped** in `gpio_serial`: the FPGA transmitted on GPIO14,
   which is the CM4's own TXD0, so both sides drove GPIO14 and the CM4 heard
   nothing. Now FPGA tx = GPIO:15 (ball A15), rx = GPIO:14 (ball C17).
2. **The SoC script imported litex-boards' platform**, where `gpio_serial`
   is commented out, so `--with-cm4-uart` failed on a clean checkout. It now
   imports `litex-soc/qmtech_kintex7_devboard_platform.py`.
3. **Build it at `--sys-clk-freq 50e6`.** At 100 MHz nextpnr reports ~73-91
   MHz and the BIOS floods `*** disabled spurious irq 3 ***` (there is no
   IRQ 3; the CPU misbehaves). At 50 MHz it passes timing (83 MHz) and runs
   clean. This is likely Bug 2 above as well.

```bash
python3 ../litex-soc/qmtech_kintex7_devboard.py --toolchain=openxc7 \
    --integrated-main-ram-size=0x8000 --with-cm4-uart --sys-clk-freq 50e6 --build
```
`handoff/bitstreams/litex-cm4-uart.bit` is this build. Firmware must be
rebuilt against this build's headers (different clock and CSR map).

CM4 side as done: `enable_uart=1` + `dtoverlay=disable-bt` in
`/boot/firmware/config.txt`, `console=serial0,115200` removed from
`cmdline.txt` (backups `*.bak-20261122-134650`). `/dev/serial0` ->
`ttyAMA0`, PL011 on GPIO14/15. No pyserial on the Pi; `cm4_link.py` needs
`sudo apt install python3-serial`.

### First use: live die monitor

`pvt-web/` runs on the CM4 and publishes the FPGA's die temperature, supplies
and ring-oscillator data at https://fpga.klayshomelab.com (read-only). See
`pvt-web/README.md`.

### Files here

- `cm4_link.py` -- minimal pyserial passthrough to the FPGA's console over
  `/dev/serial0`. Run on the CM4.
- `flash_fpga.sh` -- **confirmed blocked** as of writing (see the comment
  in the script) -- the CM4 can't currently reflash the FPGA itself, because
  Vivado Lab Edition (needed for this project's specific JTAG cable) has no
  ARM64 build, and openFPGALoader/xc3sprog don't work with this cable
  regardless of architecture. Kept for when either a working plain-FTDI
  cable replaces the current one, or upstream JTAG tooling adds support for
  this one.

## GPIO pin reference

The QMTech carrier routes the CM4's GPIO0-27 straight to specific FPGA
balls (same numbering as a real Raspberry Pi header) -- see the `"GPIO"`
connector table in `../litex-soc/qmtech_kintex7_devboard_platform.py` for
the full mapping if you want to wire up something beyond the UART pins.

## Still to do

- **Root-cause bug 2 above** (high-throughput console corruption on the
  `cm4-uart` bitstream) -- safe to use for interactive commands, not yet
  safe for anything pushing continuous fast output
- Verify the UART link actually works end to end from the CM4 side (the
  FPGA side is confirmed working for interactive use; the CM4-side steps
  above are untested)
- Decide what the CM4 is actually *for* once connected -- network-facing
  dashboard, bitstream library/switcher, or something else. Not designed
  yet.
