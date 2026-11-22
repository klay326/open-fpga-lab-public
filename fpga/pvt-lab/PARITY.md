# Feature parity: on-die PVT functions vs. what the fabric can do

This page compares the *categories* of on-die sensor IP that commercial PVT
vendors list publicly (e.g. the product names on analogbits.com/product-selector,
checked 2026-11-22) with what this repo builds from 7-series fabric and the
XADC. It uses only public product names. Datasheets are login-gated and were
**not** used, so no vendor specs are compared and none are copied. The numbers
below are our own measurements or Xilinx public docs (UG480).

| Function (public product category) | Here | How / status |
|---|---|---|
| **PVT sensor**: temperature + voltage + process in one block | ✅ | XADC temperature and VCCINT/VCCAUX/VCCBRAM (JTAG) + 5 ring oscillators in 5 clock regions (`pvt-lab`, `xadc-monitor`) |
| **Core-powered PVT sensor** (runs off the core supply, no analog rail) | ✅ | The LUT ring oscillators and the CARRY4 TDC run only on VCCINT fabric |
| **Temperature sensor** | ✅ | XADC on-die diode, ±4 °C max (UG480), JTAG readout. Fabric DRP readout is blocked by missing prjxray MONITOR bits (see `../xadc-monitor/README.md`) |
| **High-precision temp sensor / thermometer** | 🟡 | RO thermometer calibrated against the XADC over a heater sweep (`pvt_lab.py sweep`). Accuracy is bounded by the XADC reference. Gap: a multi-point external reference (thermocouple on the lid) |
| **Thermal diode** (external DXP/DXN access) | ⛔ | 7-series DXP/DXN pins are board-dependent; the QMTech core board doesn't break them out (not checked on the schematic) |
| **Process monitor** | ✅ | RO frequency spread across 5 regions; carry-chain delay per tap (TDC, ps) |
| **Power-supply (voltage) monitor** | ✅ | XADC VCCINT/VCCAUX/VCCBRAM at about 1 mV/LSB (JTAG). RO and TDC give a fabric-only VCCINT sensitivity through the `c` term of the fit |
| **Droop detector** (fast VCCINT dips) | ✅ | `dmon.v`: per-cycle delay-line monitor (LUT delay + 256 carry taps, popcount each cycle). Heater full load moves the mean 131 → 99 taps; latched alarm verified |
| **Glitch detector** (voltage spikes / fault injection) | 🟡 | Same monitor with a high-side threshold (`U`) and a latched alarm; built, not yet exercised with a real glitch source |
| **Alarms / thresholds** | ✅ | On-chip over-temperature alarm from the calibrated RO (latches heater off, no host; verified), droop/glitch alarms, heater watchdog, host-side cutoff |
| **Power-on reset** | ➖ | Handled by the FPGA's own configuration POR; out of scope |
| **Digital readout interface** | ✅ | UART stream (JP5) + JTAG; CM4 path in progress (separate session) |
| Automotive grade | ➖ | Silicon qualification, n/a |

Legend: ✅ done · 🟡 partial · ❌ not yet · ⛔ hardware can't · ➖ not applicable

Remaining: exercise the glitch alarm with a real fault source (e.g. clock glitching
through an MMCM), and a better temperature reference for the high-precision row.
