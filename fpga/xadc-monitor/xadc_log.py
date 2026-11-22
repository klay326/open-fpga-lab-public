#!/usr/bin/env python3
"""Logger / plotter for fpga/xadc-monitor.

Reads the ring-oscillator lines the FPGA streams on the JP5 UART and, every
--xadc-every seconds, the on-die XADC (temperature, VCCINT, VCCAUX, VCCBRAM)
over JTAG with `openFPGALoader --read-xadc`. Writes one CSV row per UART line
(XADC columns carry the most recent JTAG reading) and prints a live summary.

    ~/opt/litex/venv/bin/python -I xadc_log.py                  # log to xadc_log.csv
    ~/opt/litex/venv/bin/python -I xadc_log.py --xadc-every 0   # ROs only, no JTAG
    ~/opt/litex/venv/bin/python -I xadc_log.py --plot xadc_log.csv   # -> xadc_log.svg

UART line formats (hex):
    R r0 r1 r2 r3 r4                       default build
    X t i a b c41 c42 R r0 r1 r2 r3 r4     -DXADC_FABRIC build (XADC over DRP)
rN = RO/2 edges in a 2^20-cycle gate of the 50 MHz clock.

Conversions (UG480, 12-bit codes):  T = code*503.975/4096 - 273.15 degC,
V = code/4096*3 V.  The plotter needs no third-party packages (writes SVG).
"""
import argparse, csv, json, os, re, subprocess, sys, time

REF_HZ = 50e6
GATE = 1 << 20
RO_NAMES = ["ro0_X0Y0_BL", "ro1_X1Y0_BR", "ro2_X1Y3_mid", "ro3_X0Y6_TL", "ro4_X1Y6_TR"]
XADC_COLS = ["temp_c", "vccint_v", "vccaux_v", "vccbram_v"]

HERE = os.path.dirname(os.path.abspath(__file__))
# The patched openFPGALoader (XPCU-clone fix) lives here; fall back to PATH.
OFL = os.path.expanduser("~/opt/openxc7/bin/openFPGALoader")
if not os.path.exists(OFL):
    OFL = "openFPGALoader"
FIRMWARE = os.path.normpath(os.path.join(HERE, "../../handoff/cable/xusb_hex/xusb_xp2.hex"))


def temp_c(code12):
    return code12 * 503.975 / 4096 - 273.15


def volts(code12):
    return code12 / 4096 * 3.0


def ro_mhz(count):
    return 2 * count * REF_HZ / GATE / 1e6


def parse_line(line):
    """Return (xadc dict or None, [RO MHz]) or None for a garbled line."""
    tok = line.split()
    try:
        r = tok.index("R")
        ros = [ro_mhz(int(t, 16)) for t in tok[r + 1:]]
        xadc = None
        if tok and tok[0] == "X":
            t, i, a, b = (int(v, 16) >> 4 for v in tok[1:5])
            xadc = dict(temp_c=temp_c(t), vccint_v=volts(i), vccaux_v=volts(a), vccbram_v=volts(b))
    except (ValueError, IndexError):
        return None
    if not ros:
        return None
    return xadc, ros


def read_xadc_jtag(freq):
    """One XADC snapshot over JTAG; None on failure. Takes ~1-2 s."""
    cmd = [OFL, "-c", "xilinxPlatformCableUsb", "--freq", str(freq)]
    if os.path.exists(FIRMWARE):
        cmd += ["--probe-firmware", FIRMWARE]
    cmd += ["--read-xadc"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    start = out.find("{")
    if start < 0:
        return None
    try:
        # openFPGALoader emits a trailing comma before the closing brace
        d = json.loads(re.sub(r",\s*}", "}", out[start:out.rfind("}") + 1]))
    except ValueError:
        return None
    raw = d.get("raw", {})
    vbram = raw.get("6")
    return dict(temp_c=d.get("temp"), vccint_v=d.get("vccint"), vccaux_v=d.get("vccaux"),
                vccbram_v=volts((int(vbram) & 0xFFFF) >> 4) if vbram is not None else None)


def log(args):
    import serial  # pyserial, present in the litex venv
    port = serial.Serial(args.port, args.baud, timeout=2)
    xadc, last_jtag = {}, 0.0
    new = not os.path.exists(args.csv)
    with open(args.csv, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_s"] + XADC_COLS + [n + "_mhz" for n in RO_NAMES])
        t0 = time.time()
        while args.duration <= 0 or time.time() - t0 < args.duration:
            if args.xadc_every > 0 and time.time() - last_jtag >= args.xadc_every:
                port.close()          # JTAG read takes a while; don't let the UART FIFO overflow
                snap = read_xadc_jtag(args.jtag_freq)
                last_jtag = time.time()
                port.open()
                port.reset_input_buffer()
                if snap:
                    xadc = snap
                else:
                    print("  (JTAG XADC read failed)", file=sys.stderr)
            line = port.readline().decode("ascii", "replace").strip()
            p = parse_line(line)
            if not p:
                continue
            fx, ros = p
            if fx:
                xadc = fx         # fabric XADC wins when present
            row = [round(time.time() - t0, 2)] + [xadc.get(c) for c in XADC_COLS] + ros
            w.writerow(row)
            f.flush()
            xs = ("T=%5.1fC VCCINT=%.3f VCCAUX=%.3f VCCBRAM=%.3f" % tuple(xadc[c] for c in XADC_COLS)
                  if xadc and None not in xadc.values() else "XADC n/a")
            print("%7.1fs  %s  RO MHz: %s" % (row[0], xs, " ".join("%7.2f" % r for r in ros)))


def plot(args):
    rows = list(csv.DictReader(open(args.plot)))
    if not rows:
        sys.exit("no data in " + args.plot)
    t = [float(r["time_s"]) for r in rows]
    series = [(c, [float(r[c]) if r[c] not in ("", "None") else None for r in rows])
              for c in rows[0] if c != "time_s"]
    W, H, L, R, PH, GAP = 900, 0, 70, 20, 140, 30
    colors = ["#2a6fdb", "#d9480f", "#2b8a3e", "#862e9c", "#e67700"]
    panels = [("Temperature (C)", ["temp_c"]), ("Supplies (V)", ["vccint_v", "vccaux_v", "vccbram_v"]),
              ("Ring-oscillator frequency (MHz)", [c for c, _ in series if c.endswith("_mhz")])]
    out = []
    y0 = 20
    t_lo, t_hi = min(t), max(t) or 1
    for title, cols in panels:
        data = [(c, v) for c, v in series if c in cols]
        vals = [x for _, v in data for x in v if x is not None]
        if not vals:
            continue
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            lo, hi = lo - 0.5, hi + 0.5
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        sx = lambda x: L + (x - t_lo) / ((t_hi - t_lo) or 1) * (W - L - R)
        sy = lambda y: y0 + PH - (y - lo) / (hi - lo) * PH
        out.append('<text x="%d" y="%d" font-size="13" font-weight="600">%s</text>' % (L, y0 - 6, title))
        out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#bbb"/>' % (L, y0, W - L - R, PH))
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            out.append('<text x="%d" y="%.1f" font-size="10" text-anchor="end">%.3g</text>' % (L - 4, sy(v) + 3, v))
        for i, (c, v) in enumerate(data):
            pts = " ".join("%.1f,%.1f" % (sx(x), sy(y)) for x, y in zip(t, v) if y is not None)
            col = colors[i % len(colors)]
            out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.5"/>' % (pts, col))
            out.append('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>' % (L + 8 + 150 * i, y0 + 12, col, c))
        y0 += PH + GAP
    out.append('<text x="%d" y="%d" font-size="11">time (s): %.0f .. %.0f</text>' % (L, y0 - 8, t_lo, t_hi))
    H = y0
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="sans-serif">' \
          '<rect width="100%%" height="100%%" fill="white"/>%s</svg>' % (W, H, "".join(out))
    dst = os.path.splitext(args.plot)[0] + ".svg"
    open(dst, "w").write(svg)
    print("wrote", dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/cu.usbserial-B002EN6R")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--csv", default="xadc_log.csv")
    ap.add_argument("--duration", type=float, default=0, help="seconds, 0 = until Ctrl-C")
    ap.add_argument("--xadc-every", type=float, default=10, help="JTAG XADC poll period in s, 0 = off")
    ap.add_argument("--jtag-freq", type=int, default=1500000)
    ap.add_argument("--plot", metavar="CSV", help="render CSV to SVG and exit")
    args = ap.parse_args()
    try:
        plot(args) if args.plot else log(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
