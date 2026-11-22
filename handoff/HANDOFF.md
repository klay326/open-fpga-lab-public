# K7 handoff bundle

Built 2026-11-21 so this project can travel on an Apple Silicon MacBook Pro
without this machine. Read `MACOS-SETUP.md` in the main repo
(`qmtech-k7-riscv-computer`) for the macOS-specific setup walkthrough and
platform blocker (no macOS build of Vivado Lab Edition). This file covers
what that one doesn't: exact working commands, wiring, and physical specs
pulled straight from this session.

## Repo

`https://github.com/klay326/qmtech-k7-riscv-computer` (now **private**).
Clone it normally; this bundle supplements it with everything too large
or too proprietary to live in git history comfortably (though per your
own call, the large/proprietary stuff is going into git too this time --
see below).

## Hardware wiring (exact pins/balls)

**Debug console UART** (JP5 expansion header, 25x2):
- JP5 pin 7 = FPGA ball `AE22` = UART **TX**
- JP5 pin 8 = FPGA ball `AF22` = UART **RX**
- Ground: shared with the JTAG header, J1 pin 6
- IOStandard: LVCMOS33

**Serial adapter type: FTDI, not CH340.** Confirmed from `dmesg`:
```
usb 1-3: Product: FT232R USB UART
usb 1-3: Manufacturer: FTDI
usb 1-3: Detected FT232R
```
It shows up as `/dev/ttyUSB0` via the kernel's built-in `ftdi_sio` driver
-- no extra driver needed on Linux. On macOS, FTDI chips are also
supported by Apple's built-in driver (unlike CH340, which needs a
separate WCH driver) -- this is good news, this specific adapter should
just work on macOS with no driver install.

**CM4 UART** (`--with-cm4-uart` build only, GPIO14/GPIO15 on the CM4
header):
- GPIO 14 = FPGA ball `C17` = TX (from the CM4's perspective; RX into the
  FPGA)
- GPIO 15 = FPGA ball `A15` = RX (from the CM4's perspective; TX from the
  FPGA)
- Same physical pins a full-size Raspberry Pi uses for its own primary
  serial console.

**JTAG**: standard Xilinx 2x7 header (J1), connected via the Platform
Cable USB clone's stock ribbon cable -- no custom pin mapping was needed
or discovered for JTAG itself, unlike the UART which required manual
wiring to specific balls.

**Power supply**: the QMTech board runs off its own dedicated barrel-jack
PSU (not powered over USB). Its exact rated voltage/current was never
read off the label and recorded during this session -- **check and write
down the printed rating on the actual power brick before traveling**,
especially since the CM4 now draws additional current through the same
rail and a marginal/underrated supply was a real suspect for some
mid-session power flakiness (see the main repo's session history).

## Exact commands that worked

**Toolchain env:**
```bash
source /opt/openxc7/export.sh
source ~/Projects/litex/litex-venv/bin/activate
export CHIPDB=~/chipdb   # wherever chipdb/xc7k325tffg676.bin ends up
```

**Flash any bitstream** (same pattern for all of them -- `fpga/*/flash.tcl`
or a LiteX build's generated `flash.tcl`):
```bash
vivado_lab -mode batch -source flash.tcl
```
Success looks like `INFO: [Labtools 27-3164] End of startup status: HIGH`.
`LOW` means the bitstream didn't load -- check JTAG/power connections
before retrying blindly; this happened repeatedly this session and was
always a real loose-connection/power issue, confirmed via `dmesg` showing
both the JTAG cable and the separate UART adapter disconnecting and
re-enumerating at the same moment (a real power event, not software).

**Build the SoC** (from the main repo's `litex-soc/`):
```bash
python3 litex-soc/qmtech_kintex7_devboard.py \
    --toolchain=openxc7 --integrated-main-ram-size=0x8000 --build
# add --with-cm4-uart for the second-UART variant
```

**Build firmware against a specific SoC build** (critical: must match
whichever bitstream is actually flashed right now):
```bash
cd firmware/led-chase
BUILD_DIR=<path-to-build>/build/qmtech_kintex7_devboard make
```

**Load firmware over serial:**
```bash
litex_term /dev/ttyUSB0 --kernel main.bin --serial-boot
# at the litex> BIOS prompt that appears: reboot
```
Must be run in a real interactive terminal -- it calls `termios.
tcgetattr` on stdin and silently misbehaves if launched from a script or
piped.

## What's in this bundle, directory by directory

- `cable/` -- the cable's own firmware blobs (`xusb_*.hex`, from inside
  the Vivado Lab install) and the udev rules that grant non-root USB
  access to it, plus its last-known `lsusb` identity.
- `bitstreams/` -- four ready-to-flash `.bit` files; see
  `bitstreams/KNOWN-GOOD.md` for which is which and the firmware-rebuild
  gotcha.
- `litex-builds/{minimal,cm4-uart}/` -- each variant's full gateware
  output, generated C headers (`software/include/generated/`), its own
  `flash.tcl`, and a matching pre-built `main.bin` for that exact
  variant.
- `chipdb/` -- the 441MB generated `xc7k325tffg676.bin` nextpnr-xilinx
  chip database (slow to regenerate from scratch -- see
  `chipdb/NEXTPNR-COMMIT.md` for the exact commit it came from if it ever
  needs rebuilding instead).
- `toolchain/` -- `VERSIONS.md` (every tool/repo commit pinned), `pip-
  freeze.txt` from the LiteX venv, the `fasm2frames` wrapper script,
  `export.sh`, and an empty `patches/` (no actual patches to the
  installer were needed -- see VERSIONS.md for the workarounds used
  instead).
- `board-docs/` -- full clones (git history stripped) of
  `ChinaQMTECH/QMTECH_Kintex-7_Development_Board` and
  `ChinaQMTECH/QMTECH_XC7K325T_CORE_BOARD`, including the PDFs and
  schematics.
- `uncommitted/` -- `project-repo-git-status.txt` (the main repo's
  working tree was clean at capture time -- nothing was actually lost)
  and `stray-files-note.md` (cruft found outside the repo, documented but
  not copied since none of it was load-bearing).

## Known issues to remember (full writeups are in the main repo)

- Stale `main.bin` built against the wrong SoC variant's headers boots,
  prints `Liftoff!`, then hangs silently -- not a hardware bug, rebuild
  against the right `BUILD_DIR`.
- `donut`/`mandel` show garbled output specifically on the `cm4-uart`
  bitstream, confirmed via controlled A/B test to not be a wiring issue --
  real, unresolved, likely a routing/timing-margin cost from the added
  peripheral.
- FPGA bitstreams are volatile -- any power blip wipes them; zero bytes
  back from the console UART means "reflash," not "debug software."
- An endless `EEEEEEEE` flood on the console means the BIOS's serial
  loader is stuck mid-frame; only a reflash or physical reset clears it.
- This specific JTAG cable clone doesn't work with `openFPGALoader`/
  `xc3sprog`/`urjtag` on any platform -- Vivado Lab Edition is the only
  reliable path, which is the whole reason the macOS story is complicated.
