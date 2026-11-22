#!/bin/bash
# Build openFPGALoader (upstream master 2aafd90) plus the XPCU fix from
# https://github.com/trabucayre/openFPGALoader/pull/772 and install it as
# ~/opt/openxc7/bin/openFPGALoader (ahead of Homebrew's on PATH after
# sourcing ~/opt/openxc7/export.sh).
#
# Why: the Waveshare Platform Cable USB clone enumerates as 03fd:0013 but
# already runs XPCU firmware. Stock openFPGALoader treats 0013 as "needs
# firmware", uploads xusb_xp2.hex (ACKed, ignored), then waits forever for
# 03fd:0008. The fix probes XPCU GET_VERSION first and skips the upload if
# the cable answers. Plain `-c xilinxPlatformCableUsb` then just works.
# Once #772 is merged and released, `brew install openfpgaloader` is enough.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/opt/openxc7}"
SRC="$PREFIX/src/openFPGALoader"
brew install cmake pkgconf libftdi libusb hidapi zlib
[ -d "$SRC" ] || git clone https://github.com/trabucayre/openFPGALoader.git "$SRC"
cd "$SRC"
git checkout -q -- .
git fetch --tags -q origin
git checkout -q 2aafd90
git apply "$HERE/xpcu-detect-preloaded-firmware.patch"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_UDEV=OFF
cmake --build build -j"$(sysctl -n hw.ncpu)"
mkdir -p "$PREFIX/bin"
cp build/openFPGALoader "$PREFIX/bin/openFPGALoader"
"$PREFIX/bin/openFPGALoader" --Version
