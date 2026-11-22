# pvt-web: live FPGA die monitor on fpga.klayshomelab.com

The CM4 next to the Kintex-7 samples the FPGA's die temperature, supply
voltages and ring-oscillator speeds, stores them, and serves a read-only page
that the homelab's Cloudflare tunnel publishes at
**https://fpga.klayshomelab.com**.

```
FPGA ring oscillators --UART A15->GPIO15--> /dev/serial0 --.
FPGA XADC ------------JTAG (XPCU cable on CM4 USB)---------+--> pvtd (CM4 :8090)
                                                                 |  SQLite, 30 days
                                    homelab-host cloudflared tunnel <--'
                                                 |
                                    https://fpga.klayshomelab.com
```

## Data sources

| Source | What | Needs |
|---|---|---|
| UART `/dev/serial0` (115200) | 5 ring-oscillator frequencies; with pvt-lab also heater duty, loop mode, alarm flags | A bitstream that drives FPGA ball A15 (CM4 GPIO15): `fpga/pvt_web.bit` here, or `fpga/pvt-lab` (it has a `cm4_tx` port) |
| XADC over JTAG | Die temperature, VCCINT, VCCAUX (authoritative) | The XPCU cable plugged into the CM4's USB. `openFPGALoader --read-xadc` every 10 s; it does not reset the FPGA |
| RO thermometer | Temperature estimate from ro2 when no XADC reading is fresh | `/etc/pvtd/calib.json` from `fpga/pvt-lab/pvt_lab.py sweep`; only applied to pvt-lab-format lines, because the calibration holds only for that exact bitstream |

The XADC is only reachable over JTAG in openXC7 (fabric DRP access does not
work, see `fpga/xadc-monitor/README.md`). While the cable is on the CM4 the Mac
cannot flash the board; move it back (or flash from the CM4 with
`/usr/local/bin/openFPGALoader`) when needed. pvtd just shows "no XADC" while
the cable is elsewhere.

## Files

- `pvtd.py` -- the daemon. Python 3 stdlib only (termios serial, sqlite3,
  http.server). GET-only: `/`, `/api/now`, `/api/history?h=24` (max 720 h),
  `/healthz`; everything else is 404/405. No JTAG, shell or write path is
  reachable over HTTP.
- `web/index.html` -- the page: live temperature, supplies, 1 h/24 h/7 d/30 d
  chart, ring oscillators by die location.
- `pvtd.service` -- systemd unit, runs as the unprivileged `pvtd` user
  (dialout + plugdev), `ProtectSystem=strict`, only `/var/lib/pvtd` writable.
- `60-xpcu.rules` -- udev rule giving plugdev the cable (03fd:0013/0008).
- `install.sh` -- builds openFPGALoader 2aafd90 + the XPCU fix
  (`tools/openfpgaloader/xpcu-detect-preloaded-firmware.patch`, upstream PR
  #772) into `/usr/local/bin`, installs the firmware hex, udev rule, user,
  pvtd and the unit. The openFPGALoader build takes ~25 min on the CM4.
- `fpga/` -- `pvt_web.v`: `fpga/xadc-monitor` with its UART TX mirrored onto
  A15 for the CM4. `make program JTAG_FREQ=1500000` after
  `source ~/opt/openxc7/export.sh`.

## Install / update on the CM4

From the Mac (the CM4 has no repo checkout):

```bash
cd ~/projects/qmtech-k7-riscv-computer
rsync -aR -e "ssh -i ~/.ssh/qmtech_cm4" cm4/pvt-web tools/openfpgaloader handoff/cable/xusb_hex/xusb_xp2.hex kladams@qmtech-cm4.local:qmtech-repo/
ssh -i ~/.ssh/qmtech_cm4 kladams@qmtech-cm4.local 'cd qmtech-repo/cm4/pvt-web && sudo ./install.sh'
```

`sudo SKIP_OFL=1 ./install.sh` skips the openFPGALoader build (page/daemon
updates only). To use a pvt-lab calibration:
`scp fpga/pvt-lab/calib.json` to the CM4 and `sudo install -m644 calib.json /etc/pvtd/calib.json && sudo systemctl restart pvtd`.

Logs: `journalctl -u pvtd -f`. LAN check: `curl http://10.0.0.144:8090/api/now`.

## CM4 prerequisites (done 2026-11-22)

- `/dev/serial0` = PL011 on GPIO14/15: `enable_uart=1`, `dtoverlay=disable-bt`,
  no serial console (see `../README.md`).
- DHCP reservation qmtech-cm4 `2c:cf:67:8a:e1:16` -> 10.0.0.144 (UniFi).

## Publishing

Same pattern as the homelab's other public read-only pages:

- Route in `/etc/cloudflared/config.yml` on homelab-host (tunnel
  `94765154-...`): `fpga.klayshomelab.com -> http://10.0.0.144:8090`
  (backup `config.yml.bak-fpga`), then `systemctl restart cloudflared`.
- Proxied CNAME `fpga.klayshomelab.com -> <tunnel>.cfargotunnel.com`.
- No Cloudflare Access app on purpose: the page is public and read-only.
- Registered in Command Center (SERVICES row + `cc-versions.py` spec) and as a
  Homarr tile.

To take it down: remove the route + restart the tunnel, delete the CNAME, the
Command Center row/spec and the Homarr tile, then `sudo systemctl disable --now pvtd`.
