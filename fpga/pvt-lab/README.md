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
