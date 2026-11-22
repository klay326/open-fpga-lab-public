# fpga-manager: pick a project in a web page, the CM4 flashes it

A project catalog plus a web flasher for the QMTech XC7K325T board. The CM4
next to the FPGA holds the JTAG cable (XPCU clone), so it does the flashing.
Anyone can see what's loaded. Switching needs a login.

```
 browser ──> :8091 fpga_manager.py (user fpgamgr)
               │  /manager/            page + status API (public)
               │  /manager/api/switch  login required, rate-limited
               │  everything else ───> pvtd :8090 while the playground is loaded
               │
               └─ sudo -n /usr/local/sbin/fpga-flash switch <id> --by <user>   (root)
                    1. catalog lookup + SHA-256 check of the stored bitstream
                    2. fpga-queue join/take web-flasher (refuses if anyone holds or waits)
                    3. stop pvtd, set /run/fpga-manager/foreign-bitstream
                    4. openFPGALoader ... --freq 6000000 (then 1.5 MHz) --bitstream, needs "done 1"
                    5. project check (e.g. pvt-lab's status line on /dev/serial0)
                    6. any failure -> flash the playground the same way
                    7. playground: clear the flag, start pvtd
                    8. state.json, fpga-queue release with "web-flasher: loaded <id> (<sha8>)"
```

## Files

| | |
|---|---|
| `catalog.json` | the projects: id, name, description, repo `source`, `sha256`, the UARTs each one speaks, `panel`, `health`, and the fallback |
| `fpga-flash` | the privileged flasher (root, via sudo). The only thing that runs openFPGALoader with `--bitstream` |
| `fpga_manager.py` | the web daemon: page, API, login, rate limits, watchdog, pvtd proxy |
| `web/index.html` | the page |
| `systemd/` | unit, sudoers rule, `pvtd.service` drop-in, tmpfiles |
| `install.sh` | installs everything on the CM4 (run by `publish.sh`) |
| `publish.sh` | from the Mac: checks hashes, stages, installs |
| `test/run_mock.py` | off-board end-to-end test with a fake openFPGALoader / systemctl / pvtd and a pty as the UART |
| `../../tools/fpga-queue` | the shared board queue (same script on the Mac and the Pi) |

## Catalog

Only catalogued bitstreams whose SHA-256 matches can be flashed. There's no
upload from the web, because a bad bitstream can cause IO contention or cook
the board with the heater. To add or update a project:

1. build it, add or edit its entry in `catalog.json` (`source` is a path in the repo)
2. `cm4/fpga-manager/publish.sh --rehash` writes the new hashes into
   `catalog.json`, copies the bitstreams to `/var/lib/fpga-manager/bitstreams/<id>.bit`
   and reinstalls. Commit `catalog.json`, which pins exactly which build is allowed.

**One exception: the playground.** It's flashed from pvtd's own
`/usr/local/share/pvtd/playground.bit`, not from a pinned copy. pvt-web's
`install.sh` replaces that file on purpose (and restarts the RO calibration),
and pvtd already reloads it by itself, so a second copy would drift. Instead of
a hash, `fpga-flash` requires the file and every directory above it to be
root-owned and not group- or world-writable (`"trust": "root-owned"`), and it
records the hash it flashed in `state.json` and the queue note.

Without `--rehash`, `publish.sh` stops if any source no longer matches its
catalogued hash, so a rebuild can't slip in by accident. `install.sh` checks
the hashes again on the Pi, and `fpga-flash` checks them before every flash.

| id | what it speaks | where the CM4 can see it | panel / health |
|---|---|---|---|
| `playground` (pvtd's file) | pvt_lab lines + K keepalive on serial0 115200; TRNG on ttyAMA3 1 Mbaud | both | pvtd's own page; pvtd does keepalives, XADC, reloads |
| `pvt-lab` | pvt_lab v4 on JP5 115200; status mirrored A15 -> serial0 | serial0 (RX only) | UART tail; no `R ...` line for 3x30 s -> fallback |
| `trng` | raw bits on JP5 1 Mbaud | no | info |
| `litex-cm4-uart` | LiteX BIOS console on serial0 115200 (+ JP5) | serial0 | UART tail |
| `ddr3-test` | report on JP5 9600 | no | info |

`pvt-lab` is the v4 snapshot (`pvt_lab_v4.bit`); v5 is still in progress.

## Who owns the board when

- **Playground loaded:** pvtd owns it: XADC polling, keepalives on C17 and
  UART3, and the 90 s reflash watchdog. fpga-manager only proxies to it.
- **Another project loaded:** pvtd is stopped, and the systemd drop-in
  (`ConditionPathExists=!/run/fpga-manager/foreign-bitstream`) keeps it from
  being started, including by pvt-web's `install.sh`. fpga-manager watches the
  project, but only while nobody holds the queue:
  - XADC over JTAG every 10 s. Above `max_temp_c` (80 °C) it falls back to the playground.
  - the CM4 UART in `panel`/`health`, read-only (tail shown on the page)
  - `health: uart_line`: no matching line for 3 x `within_s` -> fallback
  - fallbacks happen at most once per 15 min
- **Someone else holds the queue:** fpga-manager doesn't touch JTAG or
  UARTs, and switches are refused. If someone other than web-flasher held
  the board after the last web switch, the page says the board may hold
  something else and shows their release note. Automatic fallbacks stop
  until the next web switch.
- **CM4 reboot:** `/run` is cleared, so pvtd starts and its watchdog reloads
  the playground if the FPGA is silent. The page warns that the recorded
  project may be stale.

## The board queue now lives on the Pi

`/var/lib/fpga-queue/` (group `fpgaq`, flock-locked) is the one shared truth.
`tools/fpga-queue` is the same script on both sides. On the Mac it forwards
over ssh when `~/.fpga-board/remote` exists:

```bash
echo "-i ~/.ssh/qmtech_cm4 kladams@qmtech-cm4.local" > ~/.fpga-board/remote
tools/fpga-queue status          # now runs /usr/local/bin/fpga-queue on the Pi
```

Cut-over (once): stage the Mac's `~/.fpga-board/{holder,queue,log}` and install
with `MIGRATE_QUEUE=<dir>`, then write the `remote` file in the same minute.
Remove the file to go back to a Mac-local queue.

## Auth and limits

- One or more accounts in `/etc/fpga-manager/users.json` (root:fpgamgr 0640),
  stored as scrypt hashes (N=2^15). Set or change a password on the Pi:
  ```bash
  sudo python3 /opt/fpga-manager/fpga_manager.py passwd <name>
  ```
- The session cookie is `HttpOnly; SameSite=Strict; Path=/manager`, plus `Secure`
  behind Cloudflare, and lasts 12 h. Sessions are in memory, so a restart logs
  everyone out. POSTs need a JSON body, and a cross-site `Origin` is refused.
- Logins: 5 failures per IP or 20 overall in 10 min locks login for 10 min.
- Switches: one at a time, at least 60 s apart, at most 12 per hour
  (`--min-gap`, `--per-hour`).
- The sudoers rule allows `fpga-flash switch *` only. fpga-flash checks the id
  against the root-owned catalog and the hash, so a compromised daemon can at
  worst load another catalogued project.

## Install / update

```bash
cm4/fpga-manager/publish.sh        # from the Mac
```

Logs: `journalctl -u fpga-manager -f`. LAN: `http://10.0.0.144:8091/manager/`.
CLI on the Pi: `sudo fpga-flash switch trng --by kladams`, `fpga-flash state`.

Public: `fpga.klayshomelab.com` currently routes to pvtd `:8090`. Pointing
the tunnel route at `:8091` puts the manager at `/manager/` and keeps the
playground at `/`, proxied. That's a public change, so it hasn't been made.

## Tests

```bash
~/opt/litex/venv/bin/python -I cm4/fpga-manager/test/run_mock.py   # Mac (needs hashlib.scrypt)
python3 -I cm4/fpga-manager/test/run_mock.py                       # Pi
```

34 checks: auth, CSRF, rate limits, hash tamper, busy queue, the pvtd flag,
watchdog fallbacks (silent UART, 85 °C, failed flash), proxying. No hardware is touched.
