# pvt-web: the FPGA playground on fpga.klayshomelab.com

A public, interactive page for the Kintex-7 on the QMTech board. Anyone can
heat the die, hold a temperature, move the droop-monitor tap, run or stop the
TRNG and roll dice from it, and everyone sees the same board live. The CM4
next to the FPGA runs the daemon (`pvtd`), the safety limits and the page.

```
                 FPGA (playground.bit)                       CM4 (pvtd :8090)
 pvt_lab: ROs, heater, hold loop, TDC, droop ---UART0 115200--- /dev/serial0
     A15 -> GPIO15 (RXD0), C17 <- GPIO14 (TXD0)
 trng: 16 rings + SP 800-90B health tests  ----UART3 1 Mbaud--- /dev/ttyAMA3
     C11 -> GPIO5 (RXD3), E18 <- GPIO4 (TXD3)
 XADC (temp, VCCINT, VCCAUX)               ----JTAG (XPCU cable on CM4 USB)
                                                                  |
              homelab-host cloudflared tunnel  <--------------'
                         https://fpga.klayshomelab.com
```

## FPGA design (`playground/`)

`playground.v` instantiates two existing blocks unmodified, each on its own
CM4 UART:

- `pvt_lab` -- frozen copies of `fpga/pvt-lab` v4 (`pvt_lab.v`, `ro_monitor.v`,
  `tdc.v`, `dmon.v`), so later pvt-lab work can't move this design's ring
  oscillators. Its RX is the AND of JP5 (Mac) and C17 (CM4); only one host
  should talk at a time.
- `trng` -- `fpga/trng/trng.v` in place.

```bash
source ~/opt/openxc7/export.sh
cd cm4/pvt-web/playground && make && make program JTAG_FREQ=1500000
```

Differences from `fpga/pvt-lab`, from testing on hardware:

- **ro1 moved to `SLICE_X145Y20`.** At `SLICE_X151Y20` (the right-most column)
  it measured 492 MHz in xadc-monitor, ~300 MHz in pvt_web and pvt-lab v4, and
  0 in the first playground build. nextpnr's timing for it was normal every
  time. The likely cause is a routing resource in that edge tile that
  prjxray-db doesn't describe correctly; this hasn't been proven.
- **Heater at 100 MHz** from a PLL (5 BUFGCE groups x 10 banks; 6 groups
  ran a clock region out of global lines). Full power from 41 C: +21.6 C in
  30 s, hitting the 72 C cut-out after about 45 s; the 50 MHz heater did
  +10.3 C in 30 s and plateaued near 61-63 C. VCCINT sags to ~0.988 V at full
  power (0.994 V at 50 MHz).
- **Droop monitor tap**: pvtd finds it at start (binary search for the edge
  nearest mid-range) because it moves with routing: 0x2C, 0x30, 0x28 in three
  builds.
- **ro1 is still fragile.** It worked at SLICE_X145Y20 in one build (490 MHz)
  and read 0 at the same LOC in the next, so it isn't the edge column. Each
  ring's divided output is routed as a global clock by nextpnr; the suspect
  is a clock-routing path that prjxray encodes wrongly. Not yet fixed.

Each new `playground.bit` restarts the RO self-calibration: `install.sh`
writes `/var/lib/pvtd/calib_epoch` and pvtd fits only samples after it.

`fpga/` holds the earlier read-only design (xadc-monitor stream mirrored to
A15). It's no longer needed.

## pvtd (`pvtd.py`, Python 3 stdlib only)

- Reads the pvt_lab line (ROs, heater duty/mode, TDC, droop min/avg/max,
  alarm flags), the TRNG byte stream (rate, P(1), 90B MCV min-entropy,
  Shannon entropy, lag-1 autocorrelation, longest run, a 16 kbit bitmap) and
  the XADC over JTAG every 5 s.
- **Self-calibrates the ring oscillators**: every 10 min it fits
  `f_k = a + b*T + c*(VCCINT-1)` for each RO against the XADC over the last
  7 days of stored samples, once those span >= 8 C. The result is
  `/var/lib/pvtd/calib_auto.json`, and the best-fitting RO becomes the hold
  loop's thermometer. So heating the die is also what calibrates it.
- **Reloads the bitstream** (`/usr/local/share/pvtd/playground.bit`) over JTAG
  if the FPGA sends nothing for 90 s (power cycle), at most every 15 min.
- History: SQLite `/var/lib/pvtd/pvt.db`, a sample every 10 s, kept 30 days.

### HTTP API

| | |
|---|---|
| `GET /` | the page (`web/index.html`) |
| `GET /api/now` | everything live |
| `GET /api/history?h=24` | downsampled history (max 720 h) |
| `GET /api/trng/random` | 32 bytes: SHA-256 over the raw bits received since the last call |
| `POST /api/control` | `{"action": ...}`: `heater {duty 0-255}`, `hold {temp_c 30-65}`, `off`, `quiet {on}`, `clear`, `droop {tap 0-63}`, `trng {run}`, `trng_rate {div 0-9}`, `trng_clear` |

The control API is public on purpose (the user's choice). What limits it:

- Strict validation; JSON bodies only, max 1 KB.
- Rate limits: 20 changes/min per visitor (by Cf-Connecting-Ip, kept in
  memory only) and 80/min overall.
- There is no path from HTTP to JTAG, files or a shell.
- The heater is refused unless there is a trusted temperature (XADC, or a
  good RO fit). It turns off at `--tmax` 72 C and stays locked until 62 C.
- Hold setpoints are capped at 65 C.
- Every heater/hold session ends after 10 min (`--lease`).
- The pvt_lab watchdog drops the heater 2.7 s after pvtd stops sending
  keepalives.
- pvtd arms the FPGA's own over-temperature alarm at tmax + 3 C on the
  thermometer RO, which latches the heater off in hardware.

The activity log shows who changed what as "visitor (CC)" (Cloudflare country
code only).

## Install / update on the CM4

From the Mac (the CM4 has no repo checkout):

```bash
cd ~/projects/qmtech-k7-riscv-computer
rsync -aR -e "ssh -i ~/.ssh/qmtech_cm4" cm4/pvt-web tools/openfpgaloader handoff/cable/xusb_hex/xusb_xp2.hex kladams@qmtech-cm4.local:qmtech-repo/
ssh -i ~/.ssh/qmtech_cm4 kladams@qmtech-cm4.local 'cd qmtech-repo/cm4/pvt-web && sudo SKIP_OFL=1 ./install.sh'
```

Without `SKIP_OFL=1` it also builds openFPGALoader 2aafd90 with the XPCU
fix (`tools/openfpgaloader/xpcu-detect-preloaded-firmware.patch`, upstream PR
#772). That takes about 25 min on the CM4. The installer also sets up the udev
rule (`60-xpcu.rules`), the firmware hex, the `pvtd` user (dialout, plugdev),
`dtoverlay=uart3` (a reboot is needed the first time) and the hardened unit
(`pvtd.service`: `ProtectSystem=strict`, only `/var/lib/pvtd` writable).

Logs: `journalctl -u pvtd -f`. LAN: `curl http://10.0.0.144:8090/api/now`.

## CM4 prerequisites (done 2026-11-22)

- `/boot/firmware/config.txt`: `enable_uart=1`, `dtoverlay=disable-bt`,
  `dtoverlay=uart3`; no serial console (see `../README.md`).
- DHCP reservation qmtech-cm4 `2c:cf:67:8a:e1:16` -> 10.0.0.144 (UniFi).
- The XPCU JTAG cable on the CM4's USB, for the XADC, self-calibration and
  reloads. While it is there the Mac can't flash the board; flash from the CM4
  with `/usr/local/bin/openFPGALoader -c xilinxPlatformCableUsb --probe-firmware /usr/local/share/pvtd/xusb_xp2.hex ...`
  (stop pvtd first so the XADC reads don't collide).

## Publishing

- Route in `/etc/cloudflared/config.yml` on homelab-host (tunnel
  `94765154-...`): `fpga.klayshomelab.com -> http://10.0.0.144:8091`, i.e.
  via fpga-manager (see `cm4/fpga-manager/README.md`), which proxies `/` and
  `/api/*` to pvtd on :8090 while the playground is loaded and serves the
  project manager at `/manager/`. (Backups: `config.yml.bak-fpga`,
  `config.yml.bak-fpga-manager`.)
- Proxied CNAME to `<tunnel>.cfargotunnel.com`.
- No Cloudflare Access, on purpose.
- Registered in Command Center (SERVICES row + `cc-versions.py` spec) and as
  a Homarr tile.

To take it down: remove the route and restart `cloudflared`, delete
the CNAME, the Command Center row/spec and the Homarr tile, then run
`sudo systemctl disable --now pvtd`.
