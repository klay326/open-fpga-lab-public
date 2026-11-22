#!/bin/bash
# Build openFPGALoader (upstream master 2aafd90) plus the XPCU fix from
# https://github.com/trabucayre/openFPGALoader/pull/772 and install it as
# ~/opt/openxc7/bin/openFPGALoader (ahead of Homebrew's on PATH after
# sourcing ~/opt/openxc7/export.sh).
#
# Why: the Waveshare Platform Cable USB clone enumerates as 03fd:0013. It
# runs the uploaded xusb_xp2.hex but never re-enumerates as 03fd:0008, so
# stock openFPGALoader waits 100 s for 0008 and fails. The fix opens 0013
# once GET_VERSION answers after the upload (3 s fallback), and skips the
# upload entirely while the firmware is still in RAM (until a power loss).
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
