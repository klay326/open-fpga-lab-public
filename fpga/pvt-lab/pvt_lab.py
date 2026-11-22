#!/usr/bin/env python3
"""Host side of fpga/pvt-lab: heater control, RO thermometer calibration and
closed-loop die-temperature hold. Run with ~/opt/litex/venv/bin/python -I.

    pvt_lab.py monitor                   live XADC (JTAG) + RO + RO-estimated temp
    pvt_lab.py heat --duty C0 --time 120 open-loop heater, logged
    pvt_lab.py sweep                     heat in steps, fit each RO -> calib.json
    pvt_lab.py hold --temp 50 [--ro 2]   fabric loop holds the die at --temp degC
    pvt_lab.py droop                     centre the droop monitor, heater load-step test

Safety: the FPGA turns the heater off if the host goes quiet for 2.7 s, and the
host drops the heater and exits if XADC reads >= --tmax (default 75 degC; the
commercial XC7K325T is rated to Tj 85 degC). Ctrl-C turns the heater off.
"""
import argparse, csv, json, os, re, subprocess, sys, threading, time

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
    cmd = [OFL if os.path.exists(OFL) else "openFPGALoader", "-c", "xilinxPlatformCableUsb",
           "--freq", str(freq), "--probe-firmware", FIRMWARE, "--read-xadc"]
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
        self.alive = True
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _rx(self):
        while self.alive:
            tok = self.s.readline().decode("ascii", "replace").split()
            try:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["monitor", "heat", "sweep", "hold", "droop"])
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
    args = ap.parse_args()
    s = Session(args)
    try:
        dict(monitor=cmd_monitor, heat=cmd_heat, sweep=cmd_sweep, hold=cmd_hold, droop=cmd_droop)[args.cmd](s)
    except KeyboardInterrupt:
        pass
    finally:
        s.board.close()


if __name__ == "__main__":
    main()
