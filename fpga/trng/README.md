# trng: ring-oscillator TRNG with on-chip health tests (XC7K325T, openXC7)

A raw true-random-number noise source built only from fabric LUTs, with
NIST SP 800-90B continuous health tests running on the FPGA.

* **Noise source**: 16 free-running LUT rings of 3, 5 and 7 stages. Their
  outputs are XORed and sampled by the 50 MHz clock through a 2-FF
  synchroniser. Phase jitter accumulated between samples is the entropy.
* **Output**: raw bits with *no* conditioning, sent as binary at **1 Mbaud**
  8N1 on the JP5 UART (AE22). That's about 800 kbit/s.
* **Health tests** (SP 800-90B §4.4), sized for an assumed min-entropy
  H = 0.5 bit/sample and a false-alarm rate of α = 2⁻²⁰:
  * Repetition Count Test: alarm on 41 identical bits in a row.
  * Adaptive Proportion Test: alarm if a 1024-bit window's first value
    recurs ≥ 793 times.

  An alarm latches, stops the stream and lights N26. Send `C` to clear it.
* **RX commands** (single bytes): `0`–`9` sample every 2^n clocks (default 4),
  `C` clears an alarm, `S` stops and `G` starts the stream.
* LEDs (active low): R26 heartbeat, P26 streaming, N26 alarm.

## Build / flash / test

```bash
source ~/opt/openxc7/export.sh
cd fpga/trng
make && make program JTAG_FREQ=1500000
~/opt/litex/venv/bin/python -I trng_test.py                 # 1 MB capture + report
~/opt/litex/venv/bin/python -I trng_test.py --file trng_raw.bin
```

`trng_raw.bin` is ready for the full NIST estimator, e.g. `ea_non_iid -v
trng_raw.bin 1` from usnistgov/SP800-90B_EntropyAssessment, which isn't
installed here.

## Measured (2026-11-22, 8 Mbit, room temperature)

| Metric                         | Result                                   |
|--------------------------------|------------------------------------------|
| Throughput                     | 798 kbit/s (UART-limited)                |
| P(1)                           | 0.4743 (static bias −2.6 %)              |
| Shannon entropy                | 0.998 bit/bit                            |
| 90B MCV min-entropy            | **0.926 bit/bit**                        |
| Autocorrelation, lags 1–8      | all within ±0.0011 (3σ bound ±0.0015)    |
| Runs test                      | z = +0.76                                |
| Longest run / max APT count    | 23 (cutoff 41) / 605 (cutoff 793)        |

The bias is the same at 3.1 Mbit/s, 391 kbit/s and 98 kbit/s sampling, so it
is a static duty-cycle bias of the XORed ring signal, not a shortage of
jitter. Raw sources normally look like this, and it is why the byte
chi-square fails (z ≈ 940). Before using the output as random numbers, run it
through a vetted conditioner, for example SHA-256 over at least 2× as many
raw bits as output bits. The min-entropy of 0.93 is well above the H = 0.5
the health tests are sized for.

Ideas for next steps: on-chip conditioning (SHA-256 or a CBC-MAC), an entropy
estimate against temperature using the `pvt-lab` heater, and a check that
the 16 rings stay independent (they could lock through supply coupling).
