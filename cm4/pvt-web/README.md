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
- **Ring divider fix (ro_monitor.v).** ro1 read 492, ~300, 0, 490 and 0 MHz
  across builds, at both SLICE_X151Y20 and SLICE_X145Y20. The cause: the
  ring net (500-600 MHz) ran to an unpinned toggle FF and on as a clock, and
  long routes swallowed edges. Now two `clkbuf_inhibit` toggle stages divide
  by 4 next to the ring (as in pvt-lab's ro_grid.v), and the count is doubled
  so the line format stays the same. ro1 is back at SLICE_X151Y20 and runs at
  625 MHz.

Each new `playground.bit` restarts the RO self-calibration: `install.sh`
writes `/var/lib/pvtd/calib_epoch` and pvtd fits only samples after it.

`fpga/` holds the earlier read-only design (xadc-monitor stream mirrored to
A15). It's no longer needed.

Also from pvt-lab v6 (the XADC session's work):

- **Die map** (`ro_grid.v`): 64 rings in an 8x8 grid, read one at a time,
  `G ff c0..c63` every 0.34 s, f = 8*c*50e6/2^18, index row*8+col with row 0
  at the bottom. On this build all 64 run, at 417-611 MHz.
- **Supply-noise scope** (`scope.v`): `Od` captures 16384 droop-monitor
  samples at 25 MS/s / 2^d into block RAM, dumped as `Q lll s0..s15`
  (16 x mean tap position) in about 8 s.

Measured on this build: with the heater off, ripple at ~79 kHz and 9.5 taps
rms (the regulator). At full heater, the heater's tone at 1.953 MHz
(100 MHz / 256 x 5 groups) and 5.1 taps rms. IR drop at full heater runs
from -22.5k ppm (centre-top) to -30.6k ppm (bottom row).

Die-map temperature calibration (open): the grid reads its 64 rings one at a
time (~5 ms each), so heater switching lands on some rings and not others.
That includes the quiet window (heater off for 1 of every 4 21-ms gates),
the PWM, and the hold loop's duty changes every 3 s. Under load that is a
~23,000 ppm effect per ring, against ~200 ppm/C of temperature. Fitting
per-ring tempcos against the XADC on such frames failed (2,400 ppm of scatter
at a fixed temperature). The next try: quiet window off and steady duty, or
pvt-lab's two-heater-on-frames method, where the IR drop cancels. Until then
the page's Temperature view says it isn't reliable, and Change shows heating.

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

### Other views (pvtd, no extra hardware)

- **Console**: a ring buffer of what the FPGA sends (R lines, die-map
  summaries, scope dump markers), XADC reads, TRNG summaries, commands sent,
  and pvtd events. A small command language (`help`, `status`, `heater`,
  `hold`, `off`, `quiet`, `droop`, `clear`, `scope`, `ir`, `trng`, `roll`,
  `id`, `thermal`) maps onto the same validated, rate-limited actions.
  Nothing raw reaches the UART.
- **Chip fingerprint (RO PUF)**: bit i = die-map ring i faster than the die
  median. 64-bit ID, plus a 32-bit key from the rings furthest from the
  median in a heater-off reference frame (`/var/lib/pvtd/puf_ref.json`, one
  per bitstream). Measured: 0/64 bits flipped from 38 to 65 C. The ID changes
  with each rebuild (routing near the rings), so it is stable per bitstream
  only.
- **Thermal model**: an output-error fit of T = ambient + g1*lp(u, tau1) +
  g2*lp(u, tau2) to the XADC and heater history (3 s samples, last 2 h, a
  grid search on the two time constants). It isn't biased by XADC noise,
  unlike an ARX fit. Measured: ambient 39.7 C, tau 14 s (15 C) and 126 s
  (40 C), +55 C at full 100 MHz heater, fit rms 0.29 C.
- **Speed vs temperature**: heater-off samples binned by XADC temperature:
  ring speed shift, droop-monitor edge position, carry-chain ps per tap.
- **TRNG entropy vs temperature**: min-entropy and P(1) logged every 10 s.
  Measured on this build: H_min 0.883 / 0.887 / 0.885 and P(1) 0.463-0.465
  at 40 / 50 / 60 C, flat within the scatter. The bias depends on placement
  (P(1) 0.473 in the standalone TRNG build).
- **Scope waterfall**: one background capture a minute (6.25 MS/s, heater
  untouched), 160 log-spaced bins from 10 kHz to 3.1 MHz, the last hour.
- **History**: a 1 s in-memory buffer for chart ranges up to 1 h, plus the
  hold setpoint, drawn as a dashed line.

### HTTP API

| | |
|---|---|
| `GET /` | the page (`web/index.html`) |
| `GET /api/now` | everything live |
| `GET /api/history?h=24` | downsampled history (max 720 h) |
| `GET/POST /api/console` | log lines after `?after=n` / `{"cmd": "..."}` (80 chars max) |
| `GET /api/bytemp`, `/api/trng/vstemp`, `/api/waterfall` | binned-by-temperature data, TRNG vs temperature, background spectra |
| `GET /api/scope` | last scope capture: trace, spectrum (1024 max-hold bins), top peaks |
| `GET /api/trng/random` | 32 bytes: SHA-256 over the raw bits received since the last call |
| `POST /api/control` | `{"action": ...}`: `heater {duty 0-255}`, `hold {temp_c 30-65}`, `off`, `quiet {on}`, `clear`, `droop {tap 0-63}`, `trng {run}`, `trng_rate {div 0-9}`, `trng_clear`, `ir_map`, `scope {dec 0-8, load}` |

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
