# xadc-monitor: on-die PVT monitor (XC7K325T, openXC7)

A small process/voltage/temperature monitor built only from what the
XC7K325T has on the die: the 7-series XADC system monitor and ring
oscillators made from fabric LUTs. It uses no third-party or vendor IP.

| Signal          | Source                                         | Path to host                |
|-----------------|------------------------------------------------|-----------------------------|
| Die temperature, VCCINT, VCCAUX, VCCBRAM | XADC (default sequencer) | **JTAG** (`openFPGALoader --read-xadc`) |
| 5 × ring-oscillator frequency | LUT rings in 5 clock regions, counted against 50 MHz F22 | JP5 UART, 115200 8N1 |

There are two builds. The default one sends the ring oscillators on the UART
and reads the XADC over JTAG, and it works with stock openXC7.
`make XADC_FABRIC=1` reads the XADC from fabric over its DRP. With the
nextpnr-xilinx and chipdb additions in the next section, it now places,
routes and loads. On hardware, though, the DRP never answers, because of
MONITOR-tile configuration bits that prjxray lacks. So the JTAG path is still
the working one.

## XADC from fabric in openXC7

**Status (2026-11-22): the XADC places, routes and loads, but the fabric DRP
does not respond on hardware.** Stock openXC7 can't place an `XADC` at all
(`ERROR: Unable to place cell 'xadc_i', no Bels remaining of type 'XADC'`).
This repo closes the nextpnr side of that. The remaining blocker is
configuration bits that prjxray never mapped.

What this repo adds:

1. **The bel.** nextpnr-xilinx takes its bels from
   `nextpnr-xilinx-meta/<family>/site_type_*.json`, and that repo has no
   `site_type_XADC.json`. `tools/nextpnr-xilinx/gen_site_type_xadc.py`
   generates one from prjxray-db's XADC site for all five families. It is one
   bel `XADC` of type `XADC_XADC`, with the 105 non-TEST pins mapped 1:1 onto
   site pins. The Kintex-7 file is in `tools/nextpnr-xilinx/meta/kintex7/`.
2. **The packer rule.** `tools/nextpnr-xilinx/xadc-bel.patch` renames `XADC`
   to `XADC_XADC` in `pack_cfg()` and pre-places it on its single site. It
   also drops constant ties on `VP`/`VN`/`VAUXP*`/`VAUXN*`. Those are analog
   inputs from dedicated pads, and the fabric has no route to them.
3. **FASM for the MONITOR tile.** `fasm2frames` rejects
   `MONITOR_BOT_FUJI2_X61Y183.MONITOR_DCLK.MONITOR_CLK1_0` because prjxray-db
   has no segbits or ppips for `MONITOR_*_FUJI2`. Every pip in those tiles is a
   1:1 hardwire (each wire has one source and one sink), so the same patch
   leaves them out of the FASM. That is how prjxray treats the `always` ppips
   in `CFG_CENTER_*`.

With these, the design packs, places (`XADC: 1/1`), routes and builds a
bitstream. The routed netlist is correct end to end:

| XADC pin | Route |
|----------|-------|
| DCLK | BUFG → `HCLK_L_X64Y182` leaf 10 → `INT_L_X24Y175.CLK_L1` → `MONITOR_DCLK` |
| DEN / DWE | `INT_L_X24Y176` `IMUX_L41` / `IMUX_L42` → `MONITOR_DEN` / `MONITOR_DWE` |
| DADDR, DI | IMUXes of `INT_L_X24Y175/176` → `MONITOR_DADDR*` / `MONITOR_DI*` |
| DRDY, DO, OT, ALM | `MONITOR_*` → `MONITOR_LOGIC_OUTS_B*` → `INT_L` `LOGIC_OUTS_L*` |

**Hardware result:** the bitstream loads (DONE=1) and the ring oscillators
run, but **every DRP access times out**. The UART shows `X DEAD DEAD DEAD DEAD
DEAD DEAD` on every line, and LED P26 (DRP alive) stays off. Over JTAG, at the
same moment, `openFPGALoader --read-xadc` reads 34.4 °C, VCCINT 1.001 V and
VCCAUX 1.824 V. The XADC itself is fine; it just doesn't answer on its fabric
port.

**Why (best explanation):** the MONITOR tile owns 30 frames × 101 words
(`CLB_IO_CLK`, base 0x00000C00) of configuration memory, and prjxray has mapped
none of it. Its `033-mon-xadc` fuzzer is a Vivado-only stub that covers
`INIT_43`, and no prjxray-db (f4pga or openXC7, any family) has
`segbits_monitor_*`. fasm2frames leaves those frames all zero, which is the
state of a bitstream that doesn't instantiate the XADC. JTAG DRP access works
without instantiation (UG480) because it is clocked by TCK. So a
missing "fabric port in use" bit, or a `DCLKINV` select left at a value that
passes no clock, explains this result. The `INIT_40..INIT_5F` values can't go
in the bitstream either, but they don't matter here: the FSM writes them over
the DRP.

**To close it:** you need a few Vivado bitstreams (any 7-series part, with and
without an `XADC`, and with `IS_DCLK_INVERTED` = 0 and 1). Diffing the MONITOR
frames gives the enable/inverter bits, which go in a `segbits_monitor_bot*.db`
plus a small `XADC` writer in `fasm.cc`. Bit-searching the 97 kbit region on
the board without a reference isn't practical, and it risks analog/test-mode
bits. Until then, use the default build, which reads the XADC over JTAG.

### Building the fabric variant (builds and loads; DRP dead, see above)

```bash
source ~/opt/openxc7/export.sh
# one-off: meta checkout with the XADC site type, patched nextpnr, new chipdb
W=~/opt/openxc7/src/xadc-work
git clone https://github.com/openxc7/nextpnr-xilinx-meta $W/meta
git -C $W/meta checkout a4af910
python3 -I tools/nextpnr-xilinx/gen_site_type_xadc.py ~/opt/openxc7/src/prjxray-db $W/meta
git -C ~/opt/openxc7/src/nextpnr-xilinx apply $PWD/tools/nextpnr-xilinx/xadc-bel.patch   # then rebuild nextpnr-xilinx
N=~/opt/openxc7/src/nextpnr-xilinx/xilinx
pypy3 $N/python/bbaexport.py --device xc7k325tffg676-1 --xray ~/opt/openxc7/src/prjxray-db/kintex7 \
    --metadata $W/meta/kintex7 --constids $N/constids.inc --bba $W/chipdb/xc7k325tffg676.bba
bbasm -l $W/chipdb/xc7k325tffg676.bba $W/chipdb/xc7k325tffg676.bin     # ~40 s total, 462 MB

cd fpga/xadc-monitor
PATH=$W/bin:$PATH make XADC_FABRIC=1 CHIPDB=$W/chipdb            # -> xadc_monitor_fabric.bit
PATH=$W/bin:$PATH make XADC_FABRIC=1 CHIPDB=$W/chipdb program JTAG_FREQ=1500000
~/opt/litex/venv/bin/python -I xadc_log.py --xadc-every 0          # X fields: DEAD until the bits are known
```

(Run the clone and generator lines from the repo root.) `XADC_FABRIC=1`
passes `-DXADC_FABRIC` to yosys through the new `YOSYS_DEFINES` hook in
`fpga/openXC7.mk`. Its outputs are `xadc_monitor_fabric.*`. The new chipdb is
a superset of the stock one: the only change is the XADC bel. Pass it with
`CHIPDB=` rather than overwriting `handoff/chipdb/`.

The XADC doesn't need to be instantiated to work. It runs in its default mode
from power-up and JTAG can always read it (UG480, "XADC JTAG DRP"). That is
how the default build gets temperature and supplies.

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
