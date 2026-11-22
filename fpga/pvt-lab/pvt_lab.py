#!/usr/bin/env python3
"""Host side of fpga/pvt-lab: heater control, RO thermometer calibration and
closed-loop die-temperature hold. Run with ~/opt/litex/venv/bin/python -I.

    pvt_lab.py monitor                   live XADC (JTAG) + RO + RO-estimated temp
    pvt_lab.py heat --duty C0 --time 120 open-loop heater, logged
    pvt_lab.py sweep                     heat in steps, fit each RO -> calib.json
    pvt_lab.py hold --temp 50 [--ro 2]   fabric loop holds the die at --temp degC
    pvt_lab.py droop                     centre the droop monitor, heater load-step test
    pvt_lab.py scope [--dec 0] [--duty FF]  on-die supply-noise capture + FFT -> scope.svg
    pvt_lab.py map [--live | --ir | --thermal 60]  8x8 ring-grid die map -> map_*.svg

Safety: the FPGA turns the heater off if the host goes quiet for 2.7 s, and the
host drops the heater and exits if XADC reads >= --tmax (default 75 degC; the
commercial XC7K325T is rated to Tj 85 degC). Ctrl-C turns the heater off.
"""
import argparse, csv, json, math, os, re, subprocess, sys, threading, time

PORT = "/dev/cu.usbserial-B002EN6R"
REF_HZ, GATE = 50e6, 1 << 20
NRO = 5
HERE = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(HERE, "calib.json")
OFL = os.path.expanduser("~/opt/openxc7/bin/openFPGALoader")
FIRMWARE = os.path.normpath(os.path.join(HERE, "../../handoff/cable/xusb_hex/xusb_xp2.hex"))


def ro_mhz(count):
    return 2 * count * REF_HZ / GATE / 1e6


def mhz_to_count(mhz):
    return int(round(mhz * 1e6 * GATE / (2 * REF_HZ)))


# ------------------------------------------------------------------ XADC (JTAG)
def read_xadc(freq=1500000):
    cmd = [OFL if os.path.exists(OFL) else "openFPGALoader", "-c", "xilinxPlatformCableUsb", "--freq", str(freq)]
    if os.path.exists(FIRMWARE):      # not shipped in the public copy of the repo
        cmd += ["--probe-firmware", FIRMWARE]
    cmd += ["--read-xadc"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        d = json.loads(re.sub(r",\s*}", "}", out[out.find("{"):out.rfind("}") + 1]))
        return dict(t=float(d["temp"]), vint=float(d["vccint"]), vaux=float(d["vccaux"]))
    except Exception:
        return None


def read_xadc_ssh(host, key):
    """XADC over the JTAG cable on the CM4 (pvtd must be stopped)."""
    cmd = ["ssh", "-i", os.path.expanduser(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
           "/usr/local/bin/openFPGALoader -c xilinxPlatformCableUsb --probe-firmware "
           "/usr/local/share/pvtd/xusb_xp2.hex --read-xadc"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        d = json.loads(re.sub(r",\s*}", "}", out[out.find("{"):out.rfind("}") + 1]))
        return dict(t=float(d["temp"]), vint=float(d["vccint"]), vaux=float(d["vccaux"]))
    except Exception:
        return None


# ------------------------------------------------------------------ board link
class Board:
    """UART reader thread + keepalive thread."""

    def __init__(self, port=PORT):
        import serial
        self.s = serial.Serial(port, 115200, timeout=1)
        self.s.reset_input_buffer()
        self.lock = threading.Lock()
        self.ros, self.duty, self.mode, self.seq = None, 0, 0, 0
        self.tdc = (None, None)   # (taps per RO period, ps per tap)
        self.dm = None             # droop monitor (min, max, mean tap position)
        self.flags = 0             # 1 droop, 2 glitch, 4 over-temperature
        self.grid = None           # (frame, [64 counts]) latest die-map frame
        self.grid_seq = 0
        self.qbuf = {}             # scope dump: line -> 16 samples
        self.alive = True
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _rx(self):
        while self.alive:
            tok = self.s.readline().decode("ascii", "replace").split()
            try:
                if tok and tok[0] == "G" and len(tok) == 66:
                    g = (int(tok[1], 16), [int(x, 16) for x in tok[2:]])
                    with self.lock:
                        self.grid = g; self.grid_seq += 1
                    continue
                if tok and tok[0] == "Q" and len(tok) == 18:
                    with self.lock:
                        self.qbuf[int(tok[1], 16)] = [int(x, 16) for x in tok[2:]]
                    continue
                if tok[0] != "R" or tok[6] != "H" or tok[8] != "M":
                    continue
                ros = [ro_mhz(int(x, 16)) for x in tok[1:6]]
                tdc = (None, None)
                if len(tok) >= 13 and tok[10] == "D" and int(tok[11], 16) and int(tok[12], 16):
                    taps = int(tok[11], 16) / 4096.0
                    tdc = (taps, 1e12 / (ro_mhz(int(tok[12], 16)) * 1e6 * taps))
                dm, flags = None, 0
                if len(tok) >= 19 and tok[13] == "P" and tok[17] == "A":
                    dm = (int(tok[14], 16), int(tok[15], 16), int(tok[16], 16) / 32768.0)
                    flags = int(tok[18], 16)
                with self.lock:
                    self.tdc = tdc
                    self.dm, self.flags = dm, flags
                    self.ros, self.duty, self.mode = ros, int(tok[7], 16), int(tok[9], 16)
                    self.seq += 1
            except (IndexError, ValueError):
                continue

    def _keepalive(self):
        while self.alive:
            self.send("K")
            time.sleep(0.5)

    def send(self, cmd):
        with self.lock:
            self.s.write((cmd + "\n").encode())

    def state(self):
        with self.lock:
            return self.ros, self.duty, self.mode, self.seq

    def wait_new(self, timeout=3):
        s0 = self.state()[3]
        t0 = time.time()
        while self.state()[3] == s0 and time.time() - t0 < timeout:
            time.sleep(0.02)
        return self.state()

    def close(self):
        self.send("H00")
        time.sleep(0.2)
        self.alive = False


# ------------------------------------------------------------------ calibration
def solve3(A, b):
    """Gaussian elimination for a 3x3 system."""
    M = [row[:] + [bb] for row, bb in zip(A, b)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit(rows, k):
    """f_k = a + b*T + c*(VCCINT-1.0), least squares. Returns (a,b,c,rms)."""
    X = [(1.0, r["t"], r["vint"] - 1.0) for r in rows]
    y = [r["ro"][k] for r in rows]
    A = [[sum(x[i] * x[j] for x in X) for j in range(3)] for i in range(3)]
    bb = [sum(x[i] * yy for x, yy in zip(X, y)) for i in range(3)]
    a, b, c = solve3(A, bb)
    res = [yy - (a + b * x[1] + c * x[2]) for x, yy in zip(X, y)]
    return a, b, c, (sum(e * e for e in res) / len(res)) ** 0.5


def load_calib():
    if os.path.exists(CALIB):
        return json.load(open(CALIB))
    return None


def ro_temp(cal, k, mhz, vint=1.0):
    a, b, c = cal["ro"][k]["a"], cal["ro"][k]["b"], cal["ro"][k]["c"]
    return (mhz - a - c * (vint - 1.0)) / b


# ------------------------------------------------------------------ commands
class Session:
    def __init__(self, args):
        self.args = args
        self.board = Board(args.port)
        self.cal = load_calib()
        self.x = None
        self.t0 = time.time()
        self.csv = None
        if args.csv:
            new = not os.path.exists(args.csv)
            self.csvf = open(args.csv, "a", newline="")
            self.csv = csv.writer(self.csvf)
            if new:
                self.csv.writerow(["time_s", "duty", "mode", "temp_c", "vccint_v", "vccaux_v"]
                                  + ["ro%d_mhz" % k for k in range(NRO)]
                                  + ["ro%d_temp_c" % k for k in range(NRO)] + ["tdc_taps", "tdc_ps"]
                                  + ["dm_min", "dm_max", "dm_mean", "flags"])
        self.board.wait_new(5)

    def poll(self):
        """One XADC read + latest RO line. Enforces the temperature limit."""
        x = read_xadc(self.args.jtag_freq)
        if x:
            self.x = x
            if x["t"] >= self.args.tmax:
                self.board.send("H00")
                sys.exit("ABORT: die at %.1f C >= tmax %.1f C, heater off" % (x["t"], self.args.tmax))
        ros, duty, mode, _ = self.board.wait_new()
        if ros is None:
            return None
        taps, ps = self.board.tdc
        rt = [ro_temp(self.cal, k, ros[k], self.x["vint"] if self.x else 1.0) if self.cal else None
              for k in range(NRO)]
        row = dict(time=time.time() - self.t0, duty=duty, mode=mode, ro=ros, rt=rt, taps=taps, ps=ps,
                   dm=self.board.dm, flags=self.board.flags,
                   **(self.x or dict(t=None, vint=None, vaux=None)))
        if self.csv:
            self.csv.writerow([round(row["time"], 2), duty, mode, row["t"], row["vint"], row["vaux"]]
                              + ["%.3f" % r for r in ros] + ["" if v is None else "%.2f" % v for v in rt]
                             + ["" if taps is None else "%.3f" % taps, "" if ps is None else "%.3f" % ps]
                             + (list(row["dm"][:2]) + ["%.2f" % row["dm"][2]] if row["dm"] else ["", "", ""])
                             + [row["flags"]])
            self.csvf.flush()
        return row

    def show(self, row, extra=""):
        xs = ("T=%5.1fC VCCINT=%.3f" % (row["t"], row["vint"])) if row["t"] is not None else "XADC n/a"
        rts = ""
        if row["rt"][0] is not None:
            rts = "  RO-T: " + " ".join("%5.1f" % v for v in row["rt"])
        tdc = "  TDC %.2f taps %.2f ps" % (row["taps"], row["ps"]) if row["ps"] else ""
        if row.get("dm"):
            tdc += "  droop %d/%.1f/%d" % (row["dm"][0], row["dm"][2], row["dm"][1])
        if row.get("flags"):
            tdc += "  ALARM:" + "".join(n for b, n in ((1, " droop"), (2, " glitch"), (4, " over-temp")) if row["flags"] & b)
        print("%6.0fs duty=%02X m%d  %s  RO: %s%s%s%s" % (
            row["time"], row["duty"], row["mode"], xs, " ".join("%6.2f" % r for r in row["ro"]), rts, tdc, extra),
            flush=True)


def cmd_monitor(s):
    while True:
        r = s.poll()
        if r:
            s.show(r)


def cmd_heat(s):
    s.board.send("H%02X" % s.args.duty)
    t_end = time.time() + s.args.time
    while time.time() < t_end:
        r = s.poll()
        if r:
            s.show(r)
    s.board.send("H00")


def cmd_sweep(s):
    """Heat at full duty up to --t-hi, then switch the heater OFF and fit on the
    cool-down. Heater current drops the local VCCINT (IR drop) by more than the
    whole thermal effect on the ROs, so only heater-off samples are usable."""
    s.board.send("HFF")
    print("--- heating to %.0f C" % s.args.t_hi, flush=True)
    t_start = time.time()
    while True:
        r = s.poll()
        if r:
            s.show(r)
            if r["t"] is not None and r["t"] >= s.args.t_hi:
                break
        if time.time() - t_start > 600:
            print("did not reach t_hi in 10 min; fitting what we have", flush=True)
            break
    s.board.send("H00")
    print("--- heater off, logging cool-down", flush=True)
    rows, hist, t_off = [], [], time.time()
    while time.time() - t_off < s.args.cool_max:
        r = s.poll()
        if not r or r["t"] is None:
            continue
        s.show(r)
        if r["duty"] == 0 and time.time() - t_off > 1.0:
            rows.append(r)
        hist.append((time.time(), r["t"]))
        hist = [h for h in hist if h[0] > time.time() - 30]
        if time.time() - t_off > 60 and len(hist) > 5 and hist[0][1] - hist[-1][1] < 0.3:
            break
    cal = {"date": time.strftime("%Y-%m-%d %H:%M"), "n": len(rows), "method": "heater-off cool-down",
           "t_range": [min(r["t"] for r in rows), max(r["t"] for r in rows)], "ro": []}
    print("\nfit  f = a + b*T + c*(VCCINT-1)   (MHz, degC, V), %d samples, %.1f..%.1f C"
          % (len(rows), cal["t_range"][0], cal["t_range"][1]))
    for k in range(NRO):
        a, b, c, rms = fit(rows, k)
        cal["ro"].append(dict(a=a, b=b, c=c, rms_mhz=rms, rms_c=abs(rms / b)))
        print("ro%d: a=%8.3f  b=%+.4f MHz/C (%+.0f ppm/C)  c=%+7.2f MHz/V  rms=%.3f MHz = %.2f C"
              % (k, a, b, b / a * 1e6, c, rms, abs(rms / b)))
    tdc = [(r["t"], r["ps"]) for r in rows if r["ps"]]
    if len(tdc) > 10:
        n = len(tdc); mt = sum(t for t, _ in tdc) / n; mp = sum(p for _, p in tdc) / n
        slope = sum((t - mt) * (p - mp) for t, p in tdc) / sum((t - mt) ** 2 for t, _ in tdc)
        cal["tdc"] = dict(ps_at_mean=mp, t_mean=mt, ps_per_c=slope)
        print("tdc: %.2f ps/tap at %.1f C, %+.4f ps/C (%+.0f ppm/C)" % (mp, mt, slope, slope / mp * 1e6))
    json.dump(cal, open(CALIB, "w"), indent=1)
    print("wrote", CALIB)


def cmd_hold(s):
    if not s.cal:
        sys.exit("no calib.json: run `sweep` first")
    if s.args.temp > s.args.tmax - 5:
        sys.exit("setpoint must be at least 5 C below tmax")
    k = s.args.ro
    c = s.cal["ro"][k]
    vint = s.x["vint"] if s.x else 1.0
    mhz = c["a"] + c["b"] * s.args.temp + c["c"] * (vint - 1.0)
    cnt = mhz_to_count(mhz)
    # temperature inversion: if this RO speeds up when hot, flip the loop
    s.board.send("P%d" % (1 if c["b"] > 0 else 0))
    s.board.send("G%X" % s.args.gain)
    s.board.send("M%02X" % s.args.dmax)
    if s.args.alarm:
        amhz = c["a"] + c["b"] * s.args.alarm + c["c"] * (vint - 1.0)
        s.board.send("A%d%06X" % (k, mhz_to_count(amhz)))
        print("on-chip over-temperature alarm armed at %.1f C (ro%d %.3f MHz)" % (s.args.alarm, k, amhz))
    s.board.send("S%d%06X" % (k, cnt))
    print("polarity %s (b=%+.4f MHz/C)" % ("inverted" if c["b"] > 0 else "normal", c["b"]))
    print("hold %.1f C on ro%d: setpoint %.3f MHz (count %06X), gain shift %d, max duty %02X"
          % (s.args.temp, k, mhz, cnt, s.args.gain, s.args.dmax), flush=True)
    t_end = time.time() + s.args.time if s.args.time > 0 else None
    while t_end is None or time.time() < t_end:
        r = s.poll()
        if r:
            s.show(r, "  err=%+.2fC" % (r["t"] - s.args.temp) if r["t"] is not None else "")
    s.board.send("H00")


def centre_droop(b):
    """The mid-chain LUT tap depends on routing, so find it on every build."""
    best = None
    for sel in range(0, 64, 2):
        b.send("T%02X" % sel)
        b.wait_new(); b.wait_new()
        if b.dm is None:
            continue
        mean = b.dm[2]
        if 20 < mean < 236 and (best is None or abs(mean - 128) < abs(best[1] - 128)):
            best = (sel, mean)
        if mean <= 5:
            break
    if not best:
        sys.exit("droop-monitor edge never landed inside the carry chain")
    print("droop monitor centred: tap 0x%02X (mean %.1f taps)" % best, flush=True)
    return best[0]


def cmd_droop(s):
    """Centre the delay-line edge in the carry chain, then step the heater
    (a load step on VCCINT) and watch the per-cycle position min/max."""
    b = s.board
    best = None
    for sel in range(0, 64, 2):
        b.send("T%02X" % sel)
        b.wait_new(); r = b.wait_new()
        if b.dm is None:
            continue
        mn, mx, mean = b.dm
        print("tap %2d: pos min %3d mean %6.2f max %3d" % (sel, mn, mean, mx), flush=True)
        if 20 < mean < 236 and (best is None or abs(mean - 128) < abs(best[1] - 128)):
            best = (sel, mean)
        if mean <= 5:      # longer delay: the edge no longer enters the chain
            break
    if not best:
        sys.exit("edge never landed inside the chain")
    b.send("T%02X" % best[0])
    print("using tap %d (mean %.1f)" % best, flush=True)
    for d in ["00", "FF", "00", "80", "00"]:
        b.send("H" + d)
        t_end = time.time() + 8
        while time.time() < t_end:
            r = s.poll()
            if r:
                s.show(r)
    b.send("H00")
    print("set thresholds with e.g. --dlo/--dhi on hold, or send L/U commands")


# ------------------------------------------------------------------ scope
def fft(x):
    """In-place iterative radix-2 FFT of a list of complex numbers."""
    n = len(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit; bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    m = 2
    while m <= n:
        w = complex(math.cos(-2 * math.pi / m), math.sin(-2 * math.pi / m))
        for k in range(0, n, m):
            wk = 1 + 0j
            for t in range(m // 2):
                a, b = x[k + t], x[k + t + m // 2] * wk
                x[k + t], x[k + t + m // 2] = a + b, a - b
                wk *= w
        m <<= 1
    return x


def svg_lines(path, panels, W=900, PH=200):
    """panels: [(title, xlabel, [(name, xs, ys)], logy)] -> simple SVG."""
    cols = ["#2a6fdb", "#d9480f", "#2b8a3e", "#862e9c"]
    out, y0, L, Rm = [], 24, 70, 20
    for title, xl, series, logy in panels:
        xs_all = [x for _, xs, _ in series for x in xs]
        ys_all = [(math.log10(y) if logy else y) for _, _, ys in series for y in ys if (y > 0 or not logy)]
        x0, x1 = min(xs_all), max(xs_all)
        lo, hi = min(ys_all), max(ys_all)
        if hi - lo < 1e-12: lo, hi = lo - 1, hi + 1
        sx = lambda v: L + (v - x0) / ((x1 - x0) or 1) * (W - L - Rm)
        sy = lambda v: y0 + PH - ((math.log10(v) if logy else v) - lo) / (hi - lo) * PH
        out.append('<text x="%d" y="%d" font-size="13" font-weight="600">%s</text>' % (L, y0 - 7, title))
        out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#bbb"/>' % (L, y0, W - L - Rm, PH))
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            out.append('<text x="%d" y="%.0f" font-size="10" text-anchor="end">%s</text>'
                       % (L - 4, y0 + PH - k / 4 * PH + 3, ("%.2g" % 10 ** v) if logy else ("%.4g" % v)))
        out.append('<text x="%d" y="%d" font-size="10">%s: %.4g .. %.4g</text>' % (L, y0 + PH + 14, xl, x0, x1))
        for i, (name, xs, ys) in enumerate(series):
            pts = " ".join("%.1f,%.1f" % (sx(a), sy(b)) for a, b in zip(xs, ys) if (b > 0 or not logy))
            out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1"/>' % (pts, cols[i % 4]))
            out.append('<text x="%d" y="%d" font-size="10" fill="%s">%s</text>' % (L + 8 + 220 * i, y0 + 12, cols[i % 4], name))
        y0 += PH + 44
    open(path, "w").write('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="sans-serif">'
                          '<rect width="100%%" height="100%%" fill="white"/>%s</svg>' % (W, y0, "".join(out)))


def capture_scope(b, dec, timeout=60):
    with b.lock:
        b.qbuf = {}
    b.send("O%X" % dec)
    t0 = time.time()
    while time.time() - t0 < timeout:
        with b.lock:
            n = len(b.qbuf)
        if n >= 1024:
            break
        time.sleep(0.2)
    with b.lock:
        q = dict(b.qbuf)
    if len(q) < 1024:
        sys.exit("scope dump incomplete: %d/1024 lines" % len(q))
    return [v / 16.0 for k in range(1024) for v in q[k]]


def cmd_scope(s):
    """Capture 16384 droop-monitor samples (tap position, 25 MS/s / 2^dec), with
    the heater at --duty, and show the time trace plus the spectrum."""
    b = s.board
    b.send("W0")                       # no quiet-window gating during the capture
    tap = s.args.tap if s.args.tap is not None else centre_droop(b)
    b.send("T%02X" % tap)
    b.send("H%02X" % s.args.duty)
    time.sleep(1.0)
    fs = 25e6 / (1 << s.args.dec)
    x = capture_scope(b, s.args.dec)
    b.send("H00"); b.send("W1")
    n = len(x); mean = sum(x) / n
    rms = (sum((v - mean) ** 2 for v in x) / n) ** 0.5
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    X = fft([complex((v - mean) * w, 0) for v, w in zip(x, win)])
    amp = [2 * abs(X[k]) / (n * 0.5) for k in range(n // 2)]      # Hann-corrected amplitude, taps
    f = [k * fs / n for k in range(n // 2)]
    peaks = sorted(((amp[k], f[k]) for k in range(2, n // 2 - 1)
                    if amp[k] > amp[k - 1] and amp[k] >= amp[k + 1]), reverse=True)[:8]
    print("scope: %d samples at %.4g S/s (record %.3g s), heater duty %02X" % (n, fs, n / fs, s.args.duty))
    print("mean position %.2f taps, rms noise %.3f taps, p-p %.2f taps" % (mean, rms, max(x) - min(x)))
    print("strongest tones:")
    for a, fr in peaks:
        print("  %10.1f Hz  %.3f taps" % (fr, a))
    base = "scope_d%d_h%02X" % (s.args.dec, s.args.duty)
    with open(base + ".csv", "w") as fcsv:
        fcsv.write("t_s,pos_taps\n")
        for i, v in enumerate(x):
            fcsv.write("%.9f,%.4f\n" % (i / fs, v))
    show = min(n, 4096)
    svg_lines(base + ".svg", [
        ("Droop-monitor position vs time (first %d samples)" % show, "time (us)",
         [("pos (taps)", [i / fs * 1e6 for i in range(show)], x[:show])], False),
        ("Spectrum (Hann), heater duty %02X" % s.args.duty, "frequency (Hz)",
         [("amplitude (taps)", f[1:], amp[1:])], True)])
    print("wrote %s.svg and %s.csv" % (base, base))


# ------------------------------------------------------------------ die map
GRID_WIN = 1 << 18


GRID_DIV = 8          # v6: local /4 in each ring + the counter's /2 (v5 was 2)


def grid_mhz(c):
    return GRID_DIV * c * REF_HZ / GRID_WIN / 1e6


def next_frame(b, timeout=5):
    s0 = b.grid_seq
    t0 = time.time()
    while b.grid_seq == s0 and time.time() - t0 < timeout:
        time.sleep(0.02)
    if b.grid_seq == s0:
        sys.exit("no die-map frames (G lines): is this the v5 bitstream?")
    return [grid_mhz(c) for c in b.grid[1]]


def svg_map(path, title, vals, unit, fmt="%.1f"):
    """8x8 heatmap; vals[row*8+col], row 0 at the bottom of the die."""
    good = [v for v in vals if v is not None]
    lo, hi = min(good), max(good)
    span = (hi - lo) or 1
    cell, L, T = 70, 30, 40
    out = ['<text x="%d" y="24" font-size="14" font-weight="600">%s</text>' % (L, title)]
    for r in range(8):
        for c in range(8):
            v = vals[r * 8 + c]
            x, y = L + c * cell, T + (7 - r) * cell
            if v is None:            # dead ring (does not oscillate in this build)
                out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#ccc" stroke="white"/>' % (x, y, cell, cell))
                out.append('<text x="%d" y="%d" font-size="11" text-anchor="middle" fill="#666">dead</text>'
                           % (x + cell // 2, y + cell // 2 + 4))
                continue
            u = (v - lo) / span
            col = "rgb(%d,%d,%d)" % (int(40 + 215 * u), int(60 + 120 * (1 - abs(u - 0.5) * 2)), int(255 - 215 * u))
            out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="white"/>' % (x, y, cell, cell, col))
            out.append('<text x="%d" y="%d" font-size="12" text-anchor="middle" fill="white">%s</text>'
                       % (x + cell // 2, y + cell // 2 + 4, fmt % v))
    out.append('<text x="%d" y="%d" font-size="11">%s; range %s .. %s; die left = X0, bottom = Y0</text>'
               % (L, T + 8 * cell + 18, unit, fmt % lo, fmt % hi))
    W, H = L + 8 * cell + 20, T + 8 * cell + 30
    open(path, "w").write('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" font-family="sans-serif">'
                          '<rect width="100%%" height="100%%" fill="white"/>%s</svg>' % (W, H, "".join(out)))


def ascii_map(vals, ref, scale):
    shades = " .:-=+*#%@"
    rows = []
    for r in range(7, -1, -1):
        rows.append(" ".join("%5.0f%s" % ((vals[r * 8 + c] - ref[r * 8 + c]) / ref[r * 8 + c] * 1e6,
                    shades[max(0, min(9, int(((vals[r * 8 + c] - ref[r * 8 + c]) / ref[r * 8 + c] * 1e6) / scale)))])
                    for c in range(8)))
    return "\n".join(rows)


def cmd_map(s):
    b = s.board
    b.send("Y1"); b.send("W0")
    tc = s.args.tempco      # ppm/degC used to turn ring shifts into degC
    if s.args.ir:
        # IR-drop map: heater off / full / off, frames back to back; the
        # off frames bracket the on frame so slow thermal drift cancels.
        b.send("H00"); time.sleep(1); next_frame(b)
        off1 = next_frame(b)
        b.send("HFF"); next_frame(b)                 # discard the switching frame
        on = next_frame(b)
        b.send("H00"); next_frame(b)
        off2 = next_frame(b)
        ref = [(a + c) / 2 for a, c in zip(off1, off2)]
        ppm = [(o - r) / r * 1e6 if r > 1 and o > 1 else None for o, r in zip(on, ref)]
        print("IR-drop map (ring shift with heater FF vs off, ppm; negative = slower = lower local VCCINT):")
        for r in range(7, -1, -1):
            print(" ".join("   dead" if ppm[r * 8 + c] is None else "%7.0f" % ppm[r * 8 + c] for c in range(8)))
        svg_map("map_ir.svg", "IR drop: ring frequency shift with full heater load (ppm)", ppm, "ppm vs heater off", "%.0f")
        print("wrote map_ir.svg")
    elif s.args.thermal:
        # Heating map. Both frames are taken with the heater ON, so the IR drop
        # (~1.5 %, far bigger than the thermal shift) cancels:
        #   on0 = first full frame after switch-on (die still ~cold)
        #   onN = frame after --thermal s of heating
        # Each ring's own tempco is then fitted on the heater-off cool-down
        # against the XADC (local JTAG, or --xadc-ssh when the cable is on the CM4).
        xadc = (lambda: read_xadc_ssh(s.args.xadc_ssh, s.args.ssh_key)) if s.args.xadc_ssh else read_xadc
        b.send("H00"); time.sleep(2); next_frame(b)
        b.send("HFF"); next_frame(b)                 # discard the switching frame
        on0 = next_frame(b)
        print("heating at FF for %.0f s..." % s.args.thermal, flush=True)
        t_end = time.time() + s.args.thermal
        while time.time() < t_end:
            x = xadc()
            if x:
                print("  T(XADC) %.1f C" % x["t"], flush=True)
                if x["t"] >= s.args.tmax:
                    b.send("H00"); sys.exit("ABORT: %.1f C >= tmax" % x["t"])
        onN = next_frame(b)
        b.send("H00"); next_frame(b)
        print("cool-down: fitting each ring's tempco against the XADC...", flush=True)
        pts, t_cd = [], time.time()
        while time.time() - t_cd < s.args.cal_time:
            x1 = xadc(); f = next_frame(b); x2 = xadc()
            if x1 and x2:
                pts.append(((x1["t"] + x2["t"]) / 2, f))
                print("  %.1f C" % pts[-1][0], flush=True)
        ts = [t for t, _ in pts]
        if len(pts) < 5 or max(ts) - min(ts) < 3:
            sys.exit("not enough cool-down span for a per-ring fit (XADC reachable?)")
        mt = sum(ts) / len(ts)
        slope = []
        for i in range(64):
            ys = [f[i] for _, f in pts]
            my = sum(ys) / len(ys)
            slope.append(sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / sum((t - mt) ** 2 for t in ts))
        dT = [(n - o) / k if o > 1 and n > 1 and abs(k) > 1e-4 else None for n, o, k in zip(onN, on0, slope)]
        print("per-ring tempco (ppm/C): min %.0f  max %.0f" % (
            min(k / on0[i] * 1e6 for i, k in enumerate(slope) if on0[i] > 1),
            max(k / on0[i] * 1e6 for i, k in enumerate(slope) if on0[i] > 1)))
        print("heating map: temperature rise during %.0f s of full heater, degC (top row = top of die):" % s.args.thermal)
        for r in range(7, -1, -1):
            print(" ".join("  dead" if dT[r * 8 + c] is None else "%6.1f" % dT[r * 8 + c] for c in range(8)))
        svg_map("map_thermal.svg", "Die heating: temperature rise after %.0f s of full heater (deg C)" % s.args.thermal,
                dT, "deg C (heater-on frames, per-ring tempco)")
        json.dump(dict(on0=on0, onN=onN, slope=slope, cool=pts), open("map_thermal.json", "w"))
        print("wrote map_thermal.svg / map_thermal.json")
    else:
        ref = next_frame(b)
        print("live map: ppm vs the first frame, top row = top of die (Ctrl-C to stop)")
        while True:
            v = next_frame(b)
            print("\x1b[2J\x1b[H" + ascii_map(v, ref, s.args.scale), flush=True)
    b.send("H00"); b.send("W1")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["monitor", "heat", "sweep", "hold", "droop", "scope", "map"])
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--csv", default="pvt_lab.csv")
    ap.add_argument("--tmax", type=float, default=75.0)
    ap.add_argument("--jtag-freq", type=int, default=1500000)
    ap.add_argument("--duty", type=lambda v: int(v, 16), default=0xC0, help="hex, heat")
    ap.add_argument("--time", type=float, default=120, help="s, heat/hold (0 = forever for hold)")
    ap.add_argument("--t-hi", type=float, default=65, help="degC, sweep: heat up to this")
    ap.add_argument("--cool-max", type=float, default=420, help="s, sweep: max cool-down log time")
    ap.add_argument("--temp", type=float, default=50, help="degC, hold")
    ap.add_argument("--ro", type=int, default=2, help="RO used as thermometer, hold")
    ap.add_argument("--gain", type=int, default=6, help="integral gain shift, hold")
    ap.add_argument("--dmax", type=lambda v: int(v, 16), default=0xFF, help="hex max duty, hold")
    ap.add_argument("--alarm", type=float, default=0, help="degC on-chip over-temp alarm, hold (0 = off)")
    ap.add_argument("--dec", type=int, default=0, help="scope decimation 2^dec (0..8)")
    ap.add_argument("--tap", type=lambda v: int(v, 16), default=None, help="hex droop delay tap, scope (default: auto-centre)")
    ap.add_argument("--ir", action="store_true", help="map: IR-drop map")
    ap.add_argument("--thermal", type=float, default=0, help="map: thermal map after N s of heating")
    ap.add_argument("--tempco", type=float, default=190, help="map: ppm/C to convert ring shift")
    ap.add_argument("--scale", type=float, default=200, help="map --live: ppm per shade step")
    ap.add_argument("--cal-time", type=float, default=150, help="map --thermal: s of cool-down for the per-ring fit")
    ap.add_argument("--xadc-ssh", default="", help="read XADC over ssh on this host (cable on the CM4)")
    ap.add_argument("--ssh-key", default="~/.ssh/qmtech_cm4")
    args = ap.parse_args()
    s = Session(args)
    try:
        dict(monitor=cmd_monitor, heat=cmd_heat, sweep=cmd_sweep, hold=cmd_hold, droop=cmd_droop,
             scope=cmd_scope, map=cmd_map)[args.cmd](s)
    except KeyboardInterrupt:
        pass
    finally:
        s.board.close()


if __name__ == "__main__":
    main()
