# Replicating this on a MacBook Pro

Everything below is distilled from the actual bring-up session on Linux
(CachyOS/Arch) -- every command, gotcha, and dead end that was real.
Start with the status section; the native path is verified end to end.

## Status: everything runs natively on Apple Silicon (verified 2026-11-22)

Build **and** flash both work on an M-series Mac with no VM, no Vivado, and
no new cable:

- **Toolchain**: yosys 0.68, nextpnr-xilinx `bd9c74c5` and prjxray built
  from the pinned commits in `handoff/toolchain/VERSIONS.md` into
  `~/opt/openxc7`. The Mac build of `fpga/blinky` is **bit-identical** to
  the Linux build (`handoff/bitstreams/fpga-blinky.bit`); only the 9-byte
  date/time in the `.bit` header differs.
- **JTAG**: the Waveshare Platform Cable USB clone works with
  openFPGALoader plus the fix in upstream PR
  [trabucayre/openFPGALoader#772](https://github.com/trabucayre/openFPGALoader/pull/772) (`tools/openfpgaloader/`).
  `make program` flashes a full K325T bitstream in 20 s at 12 MHz TCK.

The old "blocker" (Vivado Lab has no macOS build, cable incompatible with
open tools) was a misdiagnosis -- see "JTAG cable: what was actually wrong"
below.

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
- The JTAG cable (Waveshare Xilinx Platform Cable USB clone, `03fd:0013`)
- A separate USB-to-TTL adapter for the debug console UART -- wired to the
  QMTech board's JP5 expansion header: TX/RX on ball `AE22`/`AF22` (JP5
  pins 7/8), ground shared with the JTAG header (J1 pin 6)
- The QMTech board's own dedicated barrel-jack power supply

## Open-source FPGA toolchain (yosys + nextpnr-xilinx + prjxray)

Verified recipe (M-series, macOS, Homebrew). Installs to `~/opt/openxc7`,
so no sudo; sources live in `~/opt/openxc7/src`. Don't use
`openXC7/toolchain-installer` on macOS.

```bash
brew install cmake ninja boost eigen bison flex gawk libffi tcl-tk readline \
    python@3.12 pypy3 pkgconf libftdi libusb hidapi zlib
mkdir -p ~/opt/openxc7/src && cd ~/opt/openxc7/src

# yosys 0.68 (Homebrew ships 0.71; build the pinned one). Needs Homebrew
# bison/flex ahead of Apple's ancient ones.
git clone https://github.com/YosysHQ/yosys.git && cd yosys
git checkout 38e001a6ff74ca434bf4cc02c053f53619160ab0
git submodule update --init --recursive
PATH="/opt/homebrew/opt/bison/bin:/opt/homebrew/opt/flex/bin:$PATH" \
  cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=$HOME/opt/openxc7 \
    -DPython3_EXECUTABLE=/opt/homebrew/bin/python3.12 .
cmake --build build && cmake --install build
cd ..

# nextpnr-xilinx bd9c74c5. Apple clang rejects bare -fopenmp, which the
# CMakeLists hardcodes. Either use USE_OPENMP=OFF, or (installed build) brew
# install libomp and point CMAKE_CXX_COMPILER at the clang++-omp wrapper,
# which rewrites -fopenmp to "-Xpreprocessor -fopenmp" and links -lomp. Measured on
# litex-minimal: 9.33 s vs 9.11 s, FASM byte-identical. OpenMP only covers
# the HeAP placer (~1.2 s), so it barely matters.
# Boost 1.92 prints FindBoost warnings; they're harmless.
git clone https://github.com/openXC7/nextpnr-xilinx.git && cd nextpnr-xilinx
git checkout bd9c74c5b90b65e33a5bcd1de124982441375768
cmake -B build -G Ninja -DARCH=xilinx -DBUILD_GUI=OFF -DBUILD_PYTHON=OFF \
  -DUSE_OPENMP=OFF -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE=/opt/homebrew/bin/python3.12 .
cmake --build build
mkdir -p ~/opt/openxc7/bin && cp build/nextpnr-xilinx build/bbasm ~/opt/openxc7/bin/
cd ..

# prjxray-db at the pinned commit (NOT nextpnr-xilinx's submodule, which
# points at 77e52f10). ~900 MB.
git clone https://github.com/openXC7/prjxray-db.git
(cd prjxray-db && git checkout 7a36171801cb7f4cae920aa0eda675a986649f6e)

# prjxray: xc7frames2bit (C++) + fasm2frames (Python venv + wrapper)
git clone https://github.com/openXC7/prjxray.git && cd prjxray
git checkout e1302fc6f8662a237e42790073fa641f2d9f1371
git submodule update --init --recursive
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 .
cmake --build build --target xc7frames2bit
cp build/tools/xc7frames2bit ~/opt/openxc7/bin/
/opt/homebrew/bin/python3.12 -m venv ~/opt/openxc7/venv
~/opt/openxc7/venv/bin/pip install -r requirements.txt
cat > ~/opt/openxc7/bin/fasm2frames <<EOF
#!/bin/bash
exec $HOME/opt/openxc7/venv/bin/python3 $HOME/opt/openxc7/src/prjxray/utils/fasm2frames.py "\$@"
EOF
chmod +x ~/opt/openxc7/bin/fasm2frames
cd ..

# patched openFPGALoader (see Flashing below)
~/projects/qmtech-k7-riscv-computer/tools/openfpgaloader/build.sh
```

`~/opt/openxc7/export.sh`:
```bash
export OPENXC7=$HOME/opt/openxc7
export PATH=$OPENXC7/bin:$PATH
export PRJXRAY_DB_DIR=$OPENXC7/src/prjxray-db
export NEXTPNR_XILINX_PYTHON_DIR=$OPENXC7/src/nextpnr-xilinx/xilinx/python
export KINTEX7_CHIPDB=$HOME/projects/qmtech-k7-riscv-computer/handoff/chipdb
export CHIPDB=$KINTEX7_CHIPDB
```

**Chipdb:** reuse the committed `handoff/chipdb/xc7k325tffg676.bin` (it was
generated at the same nextpnr-xilinx commit). It is a Git LFS object:
`brew install git-lfs && git lfs install --local && git lfs pull` after
cloning, or nextpnr gets a 134-byte pointer file. Expected SHA-256
`2dd3854c29b0ed627f4167973f9a44406955b73f75ec1686498446f00b1733af`.

Known noise: fasm prints "Unable to import fast Antlr4 parser
implementation" and falls back to the pure-Python textX parser. Harmless,
just slower on big (LiteX) designs.

Build and flash a demo:
```bash
source ~/opt/openxc7/export.sh
cd fpga/blinky && make && make program
```

## Flashing (openFPGALoader, patched)

```bash
openFPGALoader -c xilinxPlatformCableUsb --freq 12000000 --bitstream <file>.bit
```
(`make program` in any `fpga/*` project runs exactly this.) Success ends
with `ir: 1 isc_done 1 isc_ena 0 init 1 done 1`. `done 0` means the
bitstream didn't load -- check JTAG/power before retrying. 12 MHz takes
20 s for a full K325T bitstream; `JTAG_FREQ=6000000` (34 s) is the fallback.

### JTAG cable: what was actually wrong

- The cable enumerates on macOS as **`03fd:0013`** (stock FX2 descriptors,
  no strings). Linux recorded `03fd:0008` only *after* Vivado had loaded
  firmware into it.
- openFPGALoader uploads `xusb_xp2.hex` over the FX2 `0xA0` load request,
  and the clone **does run it**: afterwards it answers vendor request
  `0xB0`/GET_VERSION (FX2 `0x6109`, CPLD `0xfeff`, status `0x43` = target
  connected). But it **never re-enumerates** as `03fd:0008`, so stock
  openFPGALoader 1.2.0 waits 100 s for 0008 and fails with
  `FX2: fail to open device`.
- The firmware lives in the FX2's RAM. It survives until the cable loses USB
  power, then `GET_VERSION` times out until the next upload. (An earlier
  version of these notes said the cable boots its own firmware. That was
  wrong: it was an earlier upload still sitting in RAM.)
- `tools/openfpgaloader/xpcu-detect-preloaded-firmware.patch` (upstream PR
  [trabucayre/openFPGALoader#772](https://github.com/trabucayre/openFPGALoader/pull/772),
  same issue as #730) does two things:
  - It skips the upload when GET_VERSION already answers.
  - After an upload, if the cable hasn't re-enumerated within 3 s, it opens
    the cable at 0013 and checks GET_VERSION.

  Cold upload plus `--detect` takes 3.2 s. A full bitstream at 12 MHz from
  cold takes 23 s.
- `fpga/openXC7.mk` passes `--probe-firmware handoff/cable/xusb_hex/xusb_xp2.hex`,
  so `make program` works right after a replug as well. By hand:
  `openFPGALoader -c xilinxPlatformCableUsb --probe-firmware handoff/cable/xusb_hex/xusb_xp2.hex ...`.
- The original Linux failure (openFPGALoader 1.1.1) used `--board
  qmtechKintex7`, which selects **no** cable driver (it falls back to a
  direct FT2232 interface). It was never a JTAG shift-protocol problem.

Vivado Lab (`vivado_lab -mode batch -source flash.tcl` with
`connect_hw_server -allow_non_jtag`) still works on Linux and is the
fallback there; the `flash.tcl` files are kept for that.

## LiteX environment

Verified 2026-11-22. A venv with only the repos this target imports, each at
its pinned commit from `handoff/toolchain/VERSIONS.md`. Skip `litex_setup.py`.

```bash
brew install riscv64-elf-gcc riscv64-elf-binutils   # GCC 16.2.0; LiteX detects the riscv64-elf triple
mkdir -p ~/opt/litex && cd ~/opt/litex
while read url sha; do d=$(basename $url .git)
  git clone -q $url $d && (cd $d && git checkout -q $sha && git submodule update --init --recursive -q)
done <<'EOF'
https://github.com/m-labs/migen.git 4c2ae8dfeea37f235b52acb8166f12acaaae4f7c
https://github.com/enjoy-digital/litex.git cfb382f54f81a1b3e6d7fcaa9db1a011007dd764
https://github.com/litex-hub/litex-boards.git b8424ee7487281c29750c75ffee4532d56db5015
https://github.com/enjoy-digital/litedram.git 3cf585a60a37113f18a9f6c6f3ee774be521623e
https://github.com/enjoy-digital/liteeth.git 8c9150ff121cb3148d8ea26ce3b1c5200479848d
https://github.com/litex-hub/pythondata-cpu-vexriscv.git 642ecfed1c84460555d6d803d660cc60cfc1ecb6
https://github.com/litex-hub/pythondata-software-picolibc.git 6a13ccce7c575b32c102dd9dc52178505b81fe39
https://github.com/litex-hub/pythondata-software-compiler_rt.git 6eb76609c9627bf26635e57c63fb22cda7115887
EOF
/opt/homebrew/bin/python3.12 -m venv ~/opt/litex/venv
~/opt/litex/venv/bin/pip install meson==1.12.0 ninja==1.13.2 pyserial==3.5 pyyaml requests packaging
for d in migen litex litex-boards litedram liteeth pythondata-cpu-vexriscv \
         pythondata-software-picolibc pythondata-software-compiler_rt; do
  ~/opt/litex/venv/bin/pip install --no-deps -e $d
done
```

`CHIPDB` and `PRJXRAY_DB_DIR` come from `~/opt/openxc7/export.sh`.

## Building the SoC bitstream

```bash
source ~/opt/openxc7/export.sh && source ~/opt/litex/venv/bin/activate
mkdir -p build-qmtech-k7-minimal && cd build-qmtech-k7-minimal   # gitignored; firmware's default BUILD_DIR
python3 ../litex-soc/qmtech_kintex7_devboard.py \
    --toolchain=openxc7 --integrated-main-ram-size=0x8000 --build
make -C ../firmware/led-chase
litex_term /dev/cu.usbserial-B002EN6R --kernel ../firmware/led-chase/main.bin --serial-boot
```

Verified on the Mac (2026-11-22):
- The whole SoC build takes 28 s: yosys about 6 s, nextpnr about 9 s,
  fasm2frames about 8 s.
- Generated headers match the Linux build apart from timestamps, so the CSR
  map is the same.
- BIOS: `CRC passed`, `VexRiscv @ 100MHz`, `Memtest OK`, 205.7 MiB/s write,
  107.0 MiB/s read.
- Serial-boot runs at 10.8 KB/s, and `x86` runs.
- nextpnr reports sys clk at about 84 MHz ("FAIL at 100 MHz"), yet the
  hardware runs clean at 100 MHz. LiteX passes `--timing-allow-fail`.

The FT232R shows up as `/dev/cu.usbserial-B002EN6R` with Apple's built-in
driver.

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

7. **~~The JTAG cable doesn't work with open-source tools~~ -- wrong.**
   It works with patched openFPGALoader 1.2.0; see "JTAG cable: what was
   actually wrong" above.

8. **The CM4 probably *can* reflash the FPGA** now that openFPGALoader
   drives this cable: build `tools/openfpgaloader/build.sh`'s patch on the
   CM4 (arm64 Linux, swap `brew` for `apt`) and plug the cable into the CM4.
   Untested.