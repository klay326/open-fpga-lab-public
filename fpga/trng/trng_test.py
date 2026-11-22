#!/usr/bin/env python3
"""Capture and evaluate the raw TRNG stream from fpga/trng.

    ~/opt/litex/venv/bin/python -I trng_test.py                  # 1 MB capture + report
    ~/opt/litex/venv/bin/python -I trng_test.py --bytes 4000000 --div 6
    ~/opt/litex/venv/bin/python -I trng_test.py --file raw.bin   # analyse a saved file

Writes the raw capture to trng_raw.bin, ready for external tools (NIST
SP 800-90B `ea_non_iid -v trng_raw.bin 1`, `ent`, dieharder -g 201).
The built-in report is a quick check, not a certification: bias, the 90B
most-common-value min-entropy estimate, lag-1..8 serial correlation, runs,
byte chi-square, and the repetition-count / adaptive-proportion tests
re-run on the host.
"""
import argparse, math, sys, time

PORT = "/dev/cu.usbserial-B002EN6R"


def capture(port, n, div):
    import serial
    s = serial.Serial(port, 1000000, timeout=2)
    s.write(b"S")
    time.sleep(0.05)
    s.reset_input_buffer()
    s.write(b"C" + str(div).encode() + b"G")
    buf = bytearray()
    t0 = time.time()
    while len(buf) < n:
        chunk = s.read(min(65536, n - len(buf)))
        if not chunk:
            s.close()
            sys.exit("stream stopped after %d bytes: health-test alarm (led N26) or no bitstream" % len(buf))
        buf += chunk
    dt = time.time() - t0
    s.write(b"S")
    s.close()
    print("captured %d bytes in %.1f s = %.0f kbit/s" % (n, dt, n * 8 / dt / 1e3))
    return bytes(buf)


def bits_of(data):
    for b in data:
        for i in range(7, -1, -1):
            yield (b >> i) & 1


def report(data):
    nbits = len(data) * 8
    ones = sum(bin(b).count("1") for b in data)
    p1 = ones / nbits
    pmax = max(p1, 1 - p1)
    # 90B 6.3.1 most common value estimate (upper 99% bound)
    pu = min(1.0, pmax + 2.576 * math.sqrt(pmax * (1 - pmax) / (nbits - 1)))
    h_mcv = -math.log2(pu)
    h_shannon = -(p1 * math.log2(p1) + (1 - p1) * math.log2(1 - p1)) if 0 < p1 < 1 else 0
    print("bits %d  ones %.5f  bias %+.5f" % (nbits, p1, p1 - 0.5))
    print("Shannon entropy %.5f bit/bit   90B MCV min-entropy %.5f bit/bit" % (h_shannon, h_mcv))

    # serial correlation at lags 1..8 (on +/-1 bits), on up to 4 Mbit
    lim = min(nbits, 4_000_000)
    b = [2 * x - 1 for x, _ in zip(bits_of(data), range(lim))]
    m = sum(b) / lim
    var0 = sum((x - m) ** 2 for x in b) / lim
    corr = []
    for lag in range(1, 9):   # mean-centred autocorrelation (bias removed)
        c = sum((b[i] - m) * (b[i + lag] - m) for i in range(lim - lag)) / ((lim - lag) * var0)
        corr.append(c)
    print("autocorr lag1..8: " + " ".join("%+.4f" % c for c in corr) +
          "   (3-sigma bound +/-%.4f)" % (3 / math.sqrt(lim)))

    # runs test (Wald-Wolfowitz on the bit sequence)
    runs = 1 + sum(1 for i in range(1, lim) if b[i] != b[i - 1])
    n1 = sum(1 for x in b if x > 0); n0 = lim - n1
    mu = 2 * n1 * n0 / lim + 1
    var = (mu - 1) * (mu - 2) / (lim - 1)
    print("runs %d  expected %.0f  z = %+.2f" % (runs, mu, (runs - mu) / math.sqrt(var)))

    # byte chi-square, 255 dof
    cnt = [0] * 256
    for x in data:
        cnt[x] += 1
    e = len(data) / 256
    chi = sum((c - e) ** 2 / e for c in cnt)
    z = (chi - 255) / math.sqrt(2 * 255)
    print("byte chi-square %.1f (255 dof, z = %+.2f)" % (chi, z))

    # host re-run of the health tests at the same cutoffs (H = 0.5)
    rct_max, run, last = 0, 0, None
    apt_max = 0
    it = bits_of(data)
    window = []
    for i, x in enumerate(it):
        run = run + 1 if x == last else 1
        last = x
        rct_max = max(rct_max, run)
        window.append(x)
        if len(window) == 1024:
            apt_max = max(apt_max, window.count(window[0]))
            window = []
    print("longest run %d (RCT cutoff 41)   max APT count %d / 1024 (cutoff 793)" % (rct_max, apt_max))

    bound = 3 / math.sqrt(lim)
    corr_ok = all(abs(c) < max(bound, 0.005) for c in corr)
    print("verdict: min-entropy %.3f bit/bit %s the H = 0.5 design assumption; %s"
          % (h_mcv, ">=" if h_mcv >= 0.5 else "BELOW", "no significant serial correlation"
             if corr_ok else "SERIAL CORRELATION present -- raise --div"))
    if abs(p1 - 0.5) > 0.005:
        print("         (static bias: raw source needs a conditioner, e.g. SHA-256 over >= 2x bits)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--bytes", type=int, default=1_000_000)
    ap.add_argument("--div", type=int, default=4, help="sample every 2^div clocks (0-9)")
    ap.add_argument("--file", help="analyse an existing capture instead")
    ap.add_argument("--out", default="trng_raw.bin")
    args = ap.parse_args()
    if args.file:
        data = open(args.file, "rb").read()
    else:
        data = capture(args.port, args.bytes, args.div)
        open(args.out, "wb").write(data)
        print("raw capture ->", args.out)
    report(data)


if __name__ == "__main__":
    main()
