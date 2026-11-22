#!/bin/bash
# Build openFPGALoader v1.2.0 with the XPCU skip-firmware patch and install it
# as ~/opt/openxc7/bin/openFPGALoader (ahead of Homebrew's on PATH after
# sourcing ~/opt/openxc7/export.sh).
#
# Why: the Waveshare Platform Cable USB clone enumerates as 03fd:0013 but
# already runs XPCU firmware. Stock openFPGALoader treats 0013 as "needs
# firmware", uploads xusb_xp2.hex (ACKed, ignored), then waits forever for
# 03fd:0008. With this patch, `--probe-firmware none` opens 0013 directly.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/opt/openxc7}"
SRC="$PREFIX/src/openFPGALoader"
brew install cmake pkgconf libftdi libusb hidapi zlib
[ -d "$SRC" ] || git clone https://github.com/trabucayre/openFPGALoader.git "$SRC"
cd "$SRC"
git fetch --tags -q
git checkout -q v1.2.0
git checkout -q -- src/xilinxPlatformCableUSB.cpp
git apply "$HERE/xpcu-skip-firmware.patch"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_UDEV=OFF
cmake --build build -j"$(sysctl -n hw.ncpu)"
mkdir -p "$PREFIX/bin"
cp build/openFPGALoader "$PREFIX/bin/openFPGALoader"
"$PREFIX/bin/openFPGALoader" --Version
