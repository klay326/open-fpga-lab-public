# xadc-monitor: on-die PVT monitor (XC7K325T, openXC7)

A small process/voltage/temperature monitor built only from what the
XC7K325T has on the die: the 7-series XADC system monitor and ring
oscillators made from fabric LUTs. It uses no third-party or vendor IP.

| Signal          | Source                                         | Path to host                |
|-----------------|------------------------------------------------|-----------------------------|
| Die temperature, VCCINT, VCCAUX, VCCBRAM | XADC (default sequencer) | **JTAG** (`openFPGALoader --read-xadc`) |
| 5 × ring-oscillator frequency | LUT rings in 5 clock regions, counted against 50 MHz F22 | JP5 UART, 115200 8N1 |

The design was meant to read the XADC from fabric over the DRP and send
everything on the UART. The openXC7 flow can't do that today (see the next
section), so the XADC reaches the host over JTAG instead. The fabric DRP code
is still in `xadc_monitor.v`, behind `` `ifdef XADC_FABRIC ``.

## XADC support gap in openXC7 (checked 2026-11)

* **yosys**: fine. `XADC` is in `cells_xtra.v`, and the design synthesises
  with one `XADC` cell.
* **nextpnr-xilinx** (bd9c74c5 + local patches): **no XADC bel.** The
  `XADC_X0Y0` site exists in the chipdb, but bels come from
  `nextpnr-xilinx-meta/<family>/site_type_*.json`, and that repo has no
  `site_type_XADC.json` for any family. The result is
  `ERROR: Unable to place cell 'xadc_i', no Bels remaining of type 'XADC'`
  (with a `BEL` attribute: `No Bel named 'XADC_X0Y0/XADC'`). `fasm.cc` has no
  XADC writer either.
* **prjxray-db**: `site_type_XADC.json` and the `MONITOR_*_FUJI2` tile types
  exist, with the XADC site in `MONITOR_BOT_FUJI2_X61Y183` (clock region X0Y3),
  but there are **no `segbits_monitor_*` files**. The `INIT_40..INIT_5F`
  configuration can't be put in the bitstream. That part could be worked
  around, because the DRP-write FSM here configures the XADC at run time.

To close the gap you would add a `site_type_XADC.json` (bel + pin map) to the
meta repo, regenerate the 462 MB chipdb, and check that the MONITOR tile needs
no configuration bits for fabric DRP access. After that, synthesising with
`XADC_FABRIC` defined should be close to working. It isn't tested on
hardware, because it can't be placed. The shared `openXC7.mk` has no hook for
defines, so you would need a `read_verilog -DXADC_FABRIC` in the yosys
command.

The XADC doesn't need to be instantiated to work. It runs in its default mode
from power-up and JTAG can always read it (UG480, "XADC JTAG DRP"), so
temperature and supplies are still available.

## Ring-oscillator monitors

Each `ro_monitor` is a 4-LUT ring: a LUT2 enable/invert stage and three LUT1
buffers, pinned with `BEL` attributes to the A–D 6-LUTs of one SLICEL so the
loop stays inside one site. Its net carries `clkbuf_inhibit`, because yosys
otherwise puts a BUFG *inside* the ring, and the RO would then measure the
global clock tree. A toggle FF divides by 2 onto a BUFG. A 24-bit counter in
that domain is passed to the 50 MHz side in Gray code and differenced every
2^20 cycles (20.97 ms).

| RO  | Slice          | Clock region | Where                       |
|-----|----------------|--------------|-----------------------------|
| ro0 | SLICE_X0Y20    | X0Y0         | bottom-left                 |
| ro1 | SLICE_X151Y20  | X1Y0         | bottom-right                |
| ro2 | SLICE_X80Y170  | X1Y3         | centre, next to config/XADC |
| ro3 | SLICE_X0Y320   | X0Y6         | top-left                    |
| ro4 | SLICE_X145Y320 | X1Y6         | top-right                   |

`f_RO = 2 · count · 50 MHz / 2^20`. RO frequency goes **down** as the die gets
hotter and **up** with VCCINT. The spread between sites shows on-die process
and IR-drop variation. That is the same information a PVT macro's
process/voltage monitors give, measured here as a delay rather than as a
calibrated code. Absolute frequency also depends on the routing nextpnr chose
inside the slice, so compare each RO with itself over time, or calibrate it
against the XADC temperature.

UART line (hex, every ~0.34 s): `R r0 r1 r2 r3 r4`.

Measured on the board (2026-11-22, idle, room ambient, XADC via JTAG):

```
T= 35.2C VCCINT=1.002 VCCAUX=1.824 VCCBRAM=1.002  RO MHz: 499.5 494.4 393.0 383.2 492.9
```

RO jitter between gates is about ±0.1 MHz (about 200 ppm). ro2 and ro3 run
about 20% slower than the other three. That is almost certainly the intra-slice
routing nextpnr picked for those rings, not process variation. Check the
`.fasm` PIPs before reading anything into it.

LEDs (active low): R26 is a heartbeat. P26 and N26 are only used in the
`XADC_FABRIC` build (DRP alive, OT alarm). U26 is reset.

## Build / flash / log

```bash
source ~/opt/openxc7/export.sh
cd fpga/xadc-monitor
make                              # xadc_monitor.bit
make program JTAG_FREQ=1500000    # ~2 min while the JTAG link is marginal
~/opt/litex/venv/bin/python -I xadc_log.py                 # live + xadc_log.csv
~/opt/litex/venv/bin/python -I xadc_log.py --plot xadc_log.csv   # -> xadc_log.svg
```

`xadc_log.py` reads the UART (`/dev/cu.usbserial-B002EN6R`). Every
`--xadc-every` seconds (default 10, `0` = off) it runs
`openFPGALoader --read-xadc` over the same JTAG cable. Each run takes a few
seconds and leaves the XADC in single-channel mode, which is harmless here. The
conversions follow UG480: `T = code·503.975/4096 − 273.15 °C` and
`V = code/4096·3 V` (12-bit codes). The plotter writes a dependency-free SVG.
