#!/bin/bash
# Install pvtd on the CM4. Run from a checkout of this folder on the CM4:
#   sudo ./install.sh            # everything, incl. openFPGALoader build
#   sudo SKIP_OFL=1 ./install.sh # just update pvtd / the page
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

if [ -z "${SKIP_OFL:-}" ]; then
    # openFPGALoader with the XPCU-clone fix (upstream PR #772), see
    # tools/openfpgaloader/build.sh for why.
    apt-get install -y --no-install-recommends git cmake g++ pkg-config \
        libftdi1-dev libusb-1.0-0-dev libhidapi-dev zlib1g-dev libudev-dev
    SRC=/usr/local/src/openFPGALoader
    [ -d "$SRC" ] || git clone https://github.com/trabucayre/openFPGALoader.git "$SRC"
    git -C "$SRC" checkout -q -- .
    git -C "$SRC" fetch -q origin
    git -C "$SRC" checkout -q 2aafd90
    git -C "$SRC" apply "$REPO/tools/openfpgaloader/xpcu-detect-preloaded-firmware.patch"
    cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DENABLE_UDEV=OFF
    cmake --build "$SRC/build" -j"$(nproc)"
    install -m 755 "$SRC/build/openFPGALoader" /usr/local/bin/openFPGALoader
fi
install -d /usr/local/share/pvtd
# Platform Cable USB firmware. Xilinx's license doesn't allow redistributing it,
# so the public copy of this repo leaves it out: keep an installed one, or
# take it from an ISE/Vivado install (<Vivado>/data/xicom/xusb_xp2.hex).
FW_SRC="$REPO/handoff/cable/xusb_hex/xusb_xp2.hex"; FW=/usr/local/share/pvtd/xusb_xp2.hex
if [ -f "$FW_SRC" ]; then
    install -m 644 "$FW_SRC" "$FW"
elif [ -f "$FW" ]; then
    echo "note: $FW_SRC not in this checkout, keeping the installed $FW"
else
    echo "WARNING: no xusb_xp2.hex. Copy it from an ISE/Vivado install (<Vivado>/data/xicom/xusb_xp2.hex)"
    echo "         to $FW; until then the XADC reads and bitstream reloads need the cable's firmware already loaded."
fi
install -m 644 "$HERE/60-xpcu.rules" /etc/udev/rules.d/60-xpcu.rules
grep -q "^dtoverlay=uart3" /boot/firmware/config.txt || { echo "dtoverlay=uart3" >> /boot/firmware/config.txt; echo "added dtoverlay=uart3: reboot needed for the TRNG UART"; }
udevadm control --reload-rules && udevadm trigger --subsystem-match=usb

id pvtd >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin pvtd
install -d /opt/pvtd/web /etc/pvtd
install -m 644 "$HERE/pvtd.py" /opt/pvtd/pvtd.py
install -m 644 "$HERE/web/index.html" /opt/pvtd/web/index.html
# The playground bitstream: pvtd reloads it over JTAG if the FPGA loses its
# configuration (power cycle). Build it first (playground/, make).
NEWBIT="$HERE/playground/playground.bit"; OLDBIT=/usr/local/share/pvtd/playground.bit
if [ -f "$NEWBIT" ] && ! cmp -s "$NEWBIT" "$OLDBIT"; then
    install -m 644 "$NEWBIT" "$OLDBIT"
    # new placement = new RO frequencies: restart the self-calibration from now
    install -d -o pvtd -g pvtd /var/lib/pvtd 2>/dev/null || install -d /var/lib/pvtd
    date +%s > /var/lib/pvtd/calib_epoch
    rm -f /var/lib/pvtd/calib_auto.json /var/lib/pvtd/calib_grid.json
    echo "new playground.bit: RO calibration restarts (flash it, e.g. pvtd reloads it if the FPGA is silent)"
fi
# Ring-oscillator calibration: pvtd fits its own against the XADC
# (/var/lib/pvtd/calib_auto.json). A pvt-lab calib.json only matches the
# pvt-lab bitstream it was swept on, so it is NOT installed for the playground;
# a stale one is removed. Copy one to /etc/pvtd/calib.json by hand if needed.
rm -f /etc/pvtd/calib.json
install -m 644 "$HERE/pvtd.service" /etc/systemd/system/pvtd.service
systemctl daemon-reload
systemctl enable pvtd
systemctl restart pvtd
sleep 2
systemctl --no-pager --lines=5 status pvtd
