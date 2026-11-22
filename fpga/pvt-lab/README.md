# pvt-lab: on-die thermal lab (XC7K325T, openXC7)

One bitstream that turns the K325T into its own PVT test setup. It uses only
fabric and the XADC, with no vendor or third-party IP (see `PARITY.md` for the
function-level comparison with commercial on-die PVT macros).

| Block | File | What it does |
|---|---|---|
| 5 ring-oscillator monitors | `ro_monitor.v` | 4-LUT rings pinned to slices in clock regions X0Y0, X1Y0, X1Y3, X0Y6, X1Y6, counted against 50 MHz |
| Heater | `pvt_lab.v` | 48 banks × 2048 rotating FFs + XOR stage (≈200k FFs, 100k LUTs), on 6 `BUFGCE`-gated clocks, PWM duty 0..255, phase-staggered |
| Temperature controller | `pvt_lab.v` | Fabric integral loop that drives the heater duty so a chosen RO holds a target count (= temperature, via calibration) |
| Quiet window | `pvt_lab.v` | Heater forced off every 4th 21 ms gate. ROs, loop and alarm use only those gates |
| Over-temperature alarm | `pvt_lab.v` | On-chip, from a calibrated RO; latches the heater off and needs no host (verified) |
| Carry-chain TDC | `tdc.v` | 64×CARRY4 delay line sampling a dedicated RO; reports ps per carry tap |
| Droop / glitch detector | `dmon.v` | Per-cycle delay-line monitor: edge through a tunable LUT delay into 256 carry taps, popcount each cycle, min/max/mean, latched droop/glitch alarms |
| Safety watchdog | `pvt_lab.v` | Heater off if no UART command for 2.7 s |

The UART protocol and line format are documented at the top of `pvt_lab.v`.
The same stream is mirrored on A15 for the CM4 (`cm4/pvt-web`).

## Host tool

```bash
source ~/opt/openxc7/export.sh
cd fpga/pvt-lab && make && make program JTAG_FREQ=6000000
P="$HOME/opt/litex/venv/bin/python -I pvt_lab.py"
$P monitor                      # XADC (JTAG) + ROs + RO temperature + TDC + droop
$P sweep                        # heat to 65 C, fit on the heater-off cool-down -> calib.json
$P hold --temp 50 --alarm 70    # fabric loop holds 50 C; on-chip trip at 70 C
$P droop                        # centre the delay line, heater load-step test
```

Safety: the host drops the heater and exits at `--tmax` (default 75 °C). The
commercial XC7K325T is rated to Tj = 85 °C. The FPGA watchdog also covers a
crashed or unplugged host, and the on-chip alarm covers a broken loop.

## Results (2026-11-22, board in open air, ~22 °C room)

**Heater**: full duty heats the die **+22 °C in 75 s** (36 → 58 °C, still
rising) with about 5 mV of VCCINT sag at the XADC.

**IR drop beats temperature.** With the heater on, ro0 reads about 7 MHz
lower than with it off, while the whole 22 °C rise moves it only about
+2 MHz. So RO-as-thermometer only works on heater-off samples. That's why the
calibration fits on the cool-down and the controller uses the quiet window.
ASIC PVT macros hit the same problem: local supply noise shows up as an
apparent temperature change.

**Temperature inversion.** Every RO gets *faster* when hot (+100…+250 ppm/°C),
and the carry chain does too (−1220 ppm/°C in delay). At VCCINT = 1.0 V this
28 nm die is below its temperature-inversion voltage, so loss of threshold
voltage beats loss of mobility. The controller has a polarity bit for this.

**RO thermometer calibration** (`f = a + b·T + c·(VCCINT−1)`, fitted against
XADC over 811 heater-off samples, 39–64 °C; `calib_v3_bitstream.json`):

| RO | site / region | b (MHz/°C) | ppm/°C | rms error vs XADC |
|----|---------------|-----------:|-------:|------------------:|
| ro0 | X0Y20 / X0Y0  | +0.099 | +208 | 1.4 °C |
| ro1 | X151Y20 / X1Y0 | +0.092 | +189 | 5.3 °C |
| ro2 | X80Y170 / X1Y3 (centre) | +0.115 | +250 | **1.1 °C** |
| ro3 | X0Y320 / X0Y6 | (bimodal, 472 ↔ 477 MHz) | – | unusable |
| ro4 | X145Y320 / X1Y6 | +0.042 | +103 | 2.5 °C |

ro2 sits next to the XADC (in the config column) and tracks it best. The
corner ROs see a different local temperature, a lateral gradient while the
die cools. Part of every rms figure is the XADC's own ±4 °C / 0.12 °C-per-LSB
behaviour. RO frequencies depend on the routing inside each slice, so
**every new bitstream needs its own `sweep`**.

**TDC**: 52 ps per carry tap at 44 °C (taps per RO period ≈ 34), drifting
−0.064 ps/°C.

**Droop / glitch detector** (`dmon.v`, v4 bitstream). The delay-line edge
enters the 256-tap carry chain at LUT taps 40–48 and centres at tap 44
(about 18 ns of LUT delay against the 20 ns clock). The heater is used as a
load step on VCCINT (quiet window off, `W0`):

| Heater duty | Mean tap position | Window min / max | ro2 (MHz) |
|---|---|---|---|
| 00 | 131 | 96–107 / 150–167 | 392.2 |
| 80 | 119.4 | 104 / 131 | 387.2 |
| FF | **99 (−32 taps)** | 86 / 127 | 382.4 |

The per-cycle position falls with the load and comes back when it is
removed. With the droop threshold at `L05A` (90), the alarm stayed clear for
8 s idle, tripped within the full-load step, stayed latched after the load
was removed, and `C` cleared it.

Caveat: the UART snapshot happens every 2²⁴ clocks, a multiple of the 4-gate
quiet-window cycle, so with the quiet window on (the default) the reported
droop window is always a heater-off one. Send `W0` to watch droop under load.
Decoupling the two counters is a to-do.

**v4 recalibration** (this HDL, `calib_v4_bitstream.json`, 809 heater-off
samples, 43–64 °C). With different routing, all five ROs are usable:
ro0 ±1.22 °C, ro1 ±1.18 °C, ro2 ±1.21 °C, ro3 ±1.86 °C, ro4 ±1.61 °C rms vs
the XADC, at +137…+212 ppm/°C. The TDC tempco came out with the opposite sign
from v3 (+965 vs −1221 ppm/°C). The TDC's ±1-tap scatter is too large to
trust its sign over a 20 °C span, so treat the TDC as a process monitor, not
a thermometer.

**Closed-loop hold** (`hold --temp 55 --ro 0 --gain 4 --alarm 70`). This is
the fabric integral loop on ro0's quiet counts with polarity inverted. The
heater runs at full duty until ro0 reaches 55 °C, overshoots to 59.3 °C
(integrator windup; anti-windup is a to-do), then settles. Over the last
100 s: **ro0 55.07 ± 0.53 °C**, heater duty 165–206. The XADC at the die
centre read 57.3 ± 0.7 °C at the same time. That's a real **lateral thermal
gradient**: the heater sits mid-die, so the corner RO is about 2 °C cooler
than the centre while heating. The heater-off calibration is unaffected,
because the die is nearly uniform when it isn't being heated.

**On-chip over-temperature alarm.** With `A0<count>` armed at 61 °C on ro0
and the heater requested at full duty (`HFF`, open loop), the FPGA tripped
when ro0's quiet reading crossed 61 °C (XADC 63.4 °C, again the gradient).
It latched flag 4 and forced the heater off by itself, and the XADC fell
2 °C within 2 s while `HFF` was still being requested. No host is needed for
the trip.

Full heater with the quiet window on plateaus at about 63 °C (XADC) in open
air.

## On-die supply-noise scope (`scope.v`, v5)

`pvt_lab.py scope` auto-centres the droop monitor, sets the heater, captures
16 384 consecutive per-cycle delay positions into block RAM (25 MS/s, or the
mean of 2^d samples with `--dec d`), dumps them over UART (`Q` lines,
about 8 s) and plots the time trace and a Hann-windowed spectrum
(`scope_d<dec>_h<duty>.svg`). It works like an oscilloscope probe on
VCCINT from inside the die, in units of delay-line taps.

Measured 2026-11-22, v5 build (mid-chain tap 0x20):

| Capture | Mean pos | rms | Strongest lines |
|---|---|---|---|
| 25 MS/s, heater off | 116.9 taps | **10.5 taps** | **74.8 kHz** (12 taps), 36.6 kHz, 145 kHz, 484 kHz |
| 25 MS/s, heater FF | 88.7 taps | 4.8 taps | 574 kHz, **1.171875 MHz**, 146 kHz, 2.34 MHz, 5.86 MHz |
| 391 kS/s (dec 6), heater FF | 86.7 taps | 2.2 taps | 153 kHz, 21.7 kHz cluster |

* **1.171875 MHz = 6 × 195.3125 kHz.** That is exactly the heater PWM
  (50 MHz / 256) times its 6 phase-staggered BUFGCE groups. The heater's own
  load signature shows up in the supply, from inside the die, with its
  harmonics (2.34, 5.86 MHz).
* **Heater off is noisier (10.5 taps rms) than full load (4.8).** The
  dominant 75 kHz tone at light load, gone under heavy load, looks like the
  board's VCCINT buck regulator in light-load burst (PFM) mode switching to
  fixed-frequency PWM under load (574 kHz is a candidate switching frequency).
  The regulator part isn't checked on the schematic; this is a hypothesis.
* The mean position shift (117 → 89 taps) is the DC IR drop under load,
  the same effect the droop alarm uses.

## Die map (`ro_grid.v`)

An 8×8 grid of 4-LUT rings spread over the die (`tools/gen_ro_grid.py` →
`ro_grid_inst.vh`; index = row·8 + col, row 0 at the bottom). Only one ring
runs at a time, through a shared counter; one frame takes 0.34 s.
`pvt_lab.py map` gives a live terminal heatmap (ppm vs the first frame);
`map --ir` an IR-drop map (heater FF vs off, bracketed by off frames);
`map --thermal 60` a temperature-rise map after 60 s of heating (heater off
for the hot frame, 190 ppm/°C nominal). Both write SVGs.

v5 first light: most cells showed **−1.3 … −1.6 % (−13 000 … −16 000 ppm)**
under full heater load, matching the IR drop seen on the status ROs. About
20 cells jumped between three frequency levels (~395 / 470 / 560 MHz). The
500–600 MHz rings drove the 64:1 mux and a BUFG-clocked counter that is
timed at only ~375–430 MHz, so edges got swallowed. v6 divides each ring by
4 in its own slice before the mux.

**v6 (local ÷4 per ring)**: all **64/64 rings work**, including the top and
bottom rows that read 0 in v5 (the "dead" rings were the same swallowed
edges). They run at 443–605 MHz, with 32–151 ppm frame-to-frame scatter
(about 0.2–0.8 °C equivalent).

**IR-drop map** (`map --ir`, `results/map_ir.svg`): every cell slows by
**−1.22 … −1.66 %** under full heater load, in a smooth top-to-bottom
gradient. Rows 7–4 (top half) sit around −13 300 ppm, rows 2–0 (bottom)
around −15 300 to −16 600 ppm, with the worst corner at bottom right. The
bottom of the die sees about 20 % more IR drop than the top. Plausibly that
is where VCCINT is fed from on the package or board; that's unchecked.

**Heating map** (`map --thermal 60 --xadc-ssh …`, `results/map_thermal.svg`):
two heater-*on* frames (just after switch-on, and after 60 s), so the IR
drop cancels. Each ring's own tempco was fitted on the heater-off
cool-down against the XADC: **66–239 ppm/°C**, so the per-ring fit is
essential. Rings show +4 … +15.7 °C (mostly 10–15 °C) against +15.2 °C at
the XADC (47.4 → 62.6 °C). The magnitude is right, but there's no clear
gradient: the placer spreads the heater banks over the whole die, so the
die heats almost uniformly, and the ±3 °C spread is mostly fit noise. A
heater confined to one region would show a hotspot.

## Known issues

- **ro1 at `SLICE_X151Y20` (right edge, X1Y0) is unreliable.** Across builds
  it read 492 MHz (xadc-monitor), ~300 MHz (pvt-lab v4, which explains its
  odd `a` in `calib_v4_bitstream.json`) and 0 (an early playground build),
  while nextpnr's timing for it stayed at ~448 MHz each time. The likely
  cause is a routing resource in that edge tile that prjxray-db models
  wrongly. In the CM4 playground's copy (`cm4/pvt-web/playground`, 59aaf00)
  it was moved to `SLICE_X145Y20` and reads ~490 MHz like the other rings.
  This folder still has X151; change `ro1`'s `LOC` and re-run `sweep` if you
  rebuild.
- **The droop-monitor tap is routing-dependent.** The mid-chain LUT tap was
  0x2C in pvt-lab v4 and 0x2C then 0x30 in playground builds. Always run
  `pvt_lab.py droop` (auto-centring) after a rebuild rather than hard-coding it.
- The droop window reported over UART always lands in a quiet (heater-off)
  gate unless `W0` is sent (see the droop results above).
- The hold loop has no anti-windup: about 4 °C of overshoot from a cold start.
- **Rings in the bottom (Y≈20) and top (Y≈328) rows of the die often don't
  oscillate.** In v5, 10 of the 16 grid cells in those rows read 0, and so
  did the status ROs ro0 (X0Y20, fine in v4) and ro1 (X151Y20). The middle
  rows all ran. It's build-dependent, so it points at routing resources that
  prjxray-db models wrongly in those tile rows, not at the silicon. Not yet
  root-caused: dumping the routed nets (`nextpnr --write`) for a dead vs a
  live ring would show which PIPs differ.
