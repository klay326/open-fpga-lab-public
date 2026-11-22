# Replicating this on a MacBook Pro

Everything below is distilled from the actual bring-up session on Linux
(CachyOS/Arch) -- every command, gotcha, and dead end that was real.
Read the **blocker** section first; it affects how you plan the whole trip.

## Read this first: the one real blocker

**Vivado Lab Edition (used here for JTAG programming) has no macOS build.**
AMD/Xilinx only ships Vivado and Vivado Lab for Windows and Linux. That's
the tool this whole project used to flash bitstreams, because the specific
JTAG cable on hand (a Xilinx Platform Cable USB clone, USB VID `03fd`)
doesn't correctly implement the low-level JTAG shift protocol that
`openFPGALoader`, `xc3sprog`, and `urjtag` all expect -- confirmed by all
three failing identically against it.

So on a Mac you have three real options:

1. **Run a Linux VM** (UTM, Parallels, VMware) with USB passthrough for the
   JTAG cable, and do the flashing step there. Everything else (LiteX
   builds, firmware builds, CM4 flashing) can stay native on macOS.
2. **Bring a Linux machine** for flashing only.
3. **Get a different JTAG cable** that's actually compatible with
   `openFPGALoader` (which does support macOS) and skip Vivado Lab on both
   platforms. This was never tested in this project -- the cable on hand is
   known-bad with open-source tools, but a genuine Xilinx cable or a
   well-supported FTDI-based one would likely just work. This is the
   cleanest long-term fix if you're willing to buy a cable, but it's a
   research task, not a guaranteed drop-in.

Decide which of these before you leave for the holidays -- it determines
whether you need to pack a laptop that can run a VM, bring a second machine,
or order a cable in advance.

## The actual code

Everything built this session is published at:

```
https://github.com/klay326/qmtech-k7-riscv-computer
```

Clone it on the Mac:
```bash
git clone https://github.com/klay326/qmtech-k7-riscv-computer.git
```

Read its main `README.md` for the project overview; this file is only the
macOS-specific delta and the lessons learned debugging it.

## Hardware list

- QMTech XC7K325T Kintex-7 core board + QMTech Development Board carrier
- Raspberry Pi Compute Module 4 (CM4)
- A CM4-to-full-size-Pi adapter board with DIP switches for eMMC
  programming mode (a BigTreeTech CM4 adapter was used here)
- The JTAG cable (see blocker above)
- A separate USB-to-TTL adapter for the debug console UART -- wired to the
  QMTech board's JP5 expansion header: TX/RX on ball `AE22`/`AF22` (JP5
  pins 7/8), ground shared with the JTAG header (J1 pin 6)
- The QMTech board's own dedicated barrel-jack power supply

## Open-source FPGA toolchain (yosys + nextpnr-xilinx + prjxray)

On Linux this came from `openXC7/toolchain-installer`. On macOS, Homebrew
has most of the build dependencies, but **this exact path was never tested
on macOS in this project** -- treat it as the starting point, not a known-
good recipe:

```bash
brew install cmake ninja boost eigen python3 libftdi libusb pkg-config
git clone https://github.com/openXC7/toolchain-installer
cd toolchain-installer
./toolchain-sources-builder.sh
source /opt/openxc7/export.sh
```

Known gotchas hit on Linux that will likely recur on macOS:

- **yosys**: the installer script assumes yosys's old Makefile build;
  newer yosys dropped it for CMake. If the installer's yosys step fails,
  build it directly:
  ```bash
  cmake -B build -DCMAKE_INSTALL_PREFIX=/opt/openxc7 .
  cmake --build build -j$(sysctl -n hw.ncpu)
  cmake --install build
  ```
- **prjxray's Python side never gets installed** (needed for
  `fasm2frames`). Fix: a dedicated venv plus a wrapper script:
  ```bash
  python3 -m venv /opt/openxc7/venv
  /opt/openxc7/venv/bin/pip install -r prjxray/requirements.txt  # adjust path
  ```
  then create `/opt/openxc7/bin/fasm2frames`:
  ```bash
  #!/bin/bash
  exec /opt/openxc7/venv/bin/python3 /path/to/prjxray/utils/fasm2frames.py "$@"
  ```

## Flashing (Vivado Lab Edition -- Linux/VM only, see blocker)

1. Free AMD/Xilinx account, download **Vivado Lab Edition** (not full
   Vivado -- much smaller, no license needed, program/debug only).
2. On Linux: the `vivado-lab-edition` AUR package, or run the downloaded
   `xsetup` installer with `--batch Install` directly.
3. If it's missing `libtinfo.so.5`/`libncurses.so.5` on a newer distro,
   copy them from the bundled `lib/lnx64.o/SuSE/` directory inside the
   Vivado Lab install.
4. Programming uses its Tcl interface, not the GUI:
   ```
   vivado_lab -mode batch -source flash.tcl
   ```
   Every `fpga/*/flash.tcl` and the LiteX build's generated `flash.tcl`
   follow the same pattern:
   ```tcl
   open_hw_manager
   connect_hw_server -allow_non_jtag
   open_hw_target
   current_hw_device [get_hw_devices xc7k325t_0]
   refresh_hw_device -update_hw_probes false [lindex [get_hw_devices xc7k325t_0] 0]
   set_property PROGRAM.FILE {<path-to>.bit} [get_hw_devices xc7k325t_0]
   program_hw_devices [get_hw_devices xc7k325t_0]
   close_hw_manager
   ```
   The `-allow_non_jtag` flag on `connect_hw_server` is required for this
   cable and is easy to miss -- it was only found by diffing against the
   GUI's own `vivado_lab.jou` journal file after a successful GUI flash.
5. Confirm success: the Tcl output should say
   `INFO: [Labtools 27-3164] End of startup status: HIGH`. `LOW` means the
   bitstream didn't actually load -- usually a loose JTAG/power connection
   (see Known Issues below).

## LiteX environment

```bash
python3 -m venv ~/litex-venv
source ~/litex-venv/bin/activate
pip install meson ninja        # not pulled in automatically
```

RISC-V GCC toolchain: on Linux, `litex_setup.py --gcc=riscv` tried to
`pacman install` without root and failed; it had to be installed manually.
On macOS there's no equivalent LiteX-automated path either -- use the
[xPack RISC-V toolchain](https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack)
or a Homebrew RISC-V tap, and make sure it ends up on `PATH` before running
`litex_setup.py`.

Set the chipdb path (needed by the openxc7 toolchain backend):
```bash
export CHIPDB=~/chipdb
```

## Building the SoC bitstream

```bash
python3 litex-soc/qmtech_kintex7_devboard.py \
    --toolchain=openxc7 --integrated-main-ram-size=0x8000 --build
```

- `--integrated-main-ram-size=0x8000` means block-RAM-only (32KB), no DDR3
  -- the open-source Kintex-7 database (`prjxray-db`) is missing segment
  definitions for I/O bank cascade primitives the DDR3 PHY needs
  (`IOB_COL_OBUF_CASCADE_Y1`, `IOB_COL_BANK_ACTIVE`). Patching that
  database or using a paid Vivado Standard/Enterprise license for
  synthesis are the only ways around it; this was never attempted here.
- `--with-cm4-uart` adds a second, independent UART peripheral on
  GPIO14/15 for a Raspberry Pi CM4 to talk to. See Known Issues below --
  it works, with one specific unresolved caveat.

## Building and loading firmware

```bash
cd firmware/led-chase
BUILD_DIR=<path-to-the-SoC-build-dir>/build/qmtech_kintex7_devboard make
```

Load it over serial (no bitstream rebuild needed for firmware-only
changes):
```bash
litex_term <serial-device> --kernel main.bin --serial-boot
# at the BIOS prompt that appears: reboot
```

On macOS the serial device will be something like
`/dev/tty.usbserial-XXXX` or `/dev/cu.usbserial-XXXX`, not `/dev/ttyUSB0`.
If it's a CH340-based USB-to-TTL adapter (not FTDI), macOS needs the WCH
CH340 driver installed separately -- Apple's built-in driver support
covers FTDI chips but not CH340.

**The one gotcha that cost the most time this session:** `litex_term`
needs a real interactive terminal (it calls `termios.tcgetattr` on stdin
to manage raw keyboard input) -- it does not work well launched from a
non-interactive script or piped through another tool. Always run it
directly in a real terminal window.

**The other gotcha, equally costly:** whenever the *bitstream*'s SoC
configuration changes (e.g. toggling `--with-cm4-uart` on or off), every
peripheral's CSR address shifts. If you reload a `main.bin` that was built
against a *different* build's generated headers, the BIOS will boot fine,
print `Liftoff!` (its normal "handing off to firmware" message), and then
hang with zero further output -- because the firmware is writing its
console output to the now-wrong UART address. This looks exactly like a
hardware/timing bug but isn't. **Always rebuild firmware with `BUILD_DIR`
pointed at the specific build directory matching whatever bitstream is
currently flashed.**

## Demo commands

Once at the firmware's own prompt (not the BIOS's `litex>` prompt):

- `chase` -- LED chase pattern in software
- `donut` -- spinning ASCII donut, computed on the RISC-V core
- `pi` -- Rabinowitz-Wagon pi-digit spigot algorithm, stress test
- `mandel` -- fixed-point (Q16.16) Mandelbrot renderer, stress test
- `x86` -- minimal 8086 interpreter executing real machine code
  (`hello8086.asm` in `firmware/led-chase/`, assembled with `nasm`)

## Flashing the CM4's eMMC

Hardware prep: seat the CM4 on the carrier board with the eMMC-programming
DIP switches, and set them **before** applying power:

- `USBOTG` -> ON (puts the CM4's USB-C port in device mode)
- `RPIBOOT` -> ON (forces USB boot mode instead of booting from eMMC --
  this is the equivalent of the official IO board's `nRPIBOOT` jumper)
- `NEXTRST` -> leave at default (never confirmed what this does exactly;
  only worth toggling if `rpiboot` fails to detect the device)
- `EEPROMWP` -> irrelevant for this step (it protects the CM4's separate
  bootloader-config EEPROM, not the eMMC; only matters if you later run
  `rpi-eeprom-update`)

Build and run `rpiboot`:
```bash
brew install libusb pkg-config
git clone https://github.com/raspberrypi/usbboot.git
cd usbboot
make
sudo ./rpiboot
```
Connect the adapter's USB data port to the Mac, power it on, then run the
above. The eMMC should enumerate as a normal USB mass-storage device
(check with `diskutil list`).

Flash Raspberry Pi OS with the native macOS **Raspberry Pi Imager** app
(much simpler than Linux -- no root/display-server fighting). Use its
gear-icon advanced options *before* writing to pre-set:
- hostname
- SSH enabled + username/password or a public key
- WiFi SSID/password if needed

Before first boot (or after, over SSH/console), set up the UART link to
the FPGA in `/boot/firmware/config.txt`:
```
enable_uart=1
dtoverlay=disable-bt
```
(`disable-bt` matters -- without it, Bluetooth claims the CM4's good PL011
UART and only the much less reliable "mini UART" is exposed.)

Flip `USBOTG`/`RPIBOOT` back to normal before the next power-on so it
boots from eMMC instead of staying in USB-boot mode.

Find it on the network and SSH in:
```bash
ping <hostname>.local
ssh <username>@<hostname>.local
```

## Known issues and root causes (so you don't rediscover them)

1. **`rpi-imager` fails with "Authorization required, but no authorization
   protocol specified" and exits silently.** Caused by a leaked
   `SUDO_USER` env var making it think it needs to self-elevate to root,
   which then can't authenticate to the display session. Fix: launch with
   a clean environment (`env -u SUDO_USER -u SUDO_UID -u SUDO_GID
   rpi-imager`).

2. **The FPGA bitstream is volatile (SRAM-based).** Any power loss --
   a PC reboot that cuts power to a shared supply, a loose barrel jack, a
   bumped cable, a stray button press on a `PROGRAM`-type button -- wipes
   it completely. Symptom: the console UART returns zero bytes, even after
   sending input. This isn't a software bug; it just needs a fresh JTAG
   reflash every time it happens.

3. **An endless `EEEEEEEE` flood on the console** means the BIOS's serial
   boot-loader got stuck mid-frame after an aborted or corrupted upload,
   and is now echoing a frame-error reply to every byte it receives. Only
   a JTAG reflash or physical reset clears it. Don't try to type plain
   commands to "wake it up" -- they just get misinterpreted as more
   malformed frames and produce more `E`s.

4. **If two independent USB devices (the JTAG cable and a separate
   USB-serial adapter) disconnect and re-enumerate at the same moment**,
   that's a real power event upstream of both, not a software/firmware
   issue -- check the barrel jack connector and the PSU's rated current,
   especially after adding the CM4 (which draws its own load through the
   same supply).

5. **A build hanging silently right after printing `Liftoff!`** is the
   firmware/header-mismatch bug described above in "Building and loading
   firmware" -- not a hardware regression. Always check `BUILD_DIR` first.

6. **A real, confirmed, still-unresolved bug**: with `--with-cm4-uart`
   enabled and firmware correctly rebuilt against its headers,
   interactive/low-throughput commands (`chase`, `pi`, `x86`) run clean,
   but `donut` and `mandel` (continuous, fast, full-screen ANSI redraws)
   show garbled/stray characters. This was isolated with a controlled A/B
   test: identical cable and wiring, only the bitstream swapped --
   `donut`/`mandel` are clean on the plain bitstream (no `cm4-uart`) and
   corrupt on the `cm4-uart` one. That rules out cable/wiring flakiness and
   points at the added UART peripheral costing real routing/timing margin
   under `nextpnr-xilinx`. Not yet root-caused with a scope or timing
   report -- worth a clear-headed pass with real tools if you want to push
   on it over the holidays.

7. **The JTAG cable itself (VID `03fd` clone) doesn't work with
   `openFPGALoader`/`xc3sprog`/`urjtag`** regardless of host OS -- this is
   almost certainly going to be true on macOS too if you try those tools
   there instead of Vivado Lab. See the blocker section at the top.

8. **The CM4 can't reflash the FPGA itself** -- Vivado Lab Edition has no
   ARM64 build, and the open-source JTAG tools don't work with this cable
   on any architecture. This is unresolved and not expected to change
   without either a different cable or upstream JTAG tooling support.
