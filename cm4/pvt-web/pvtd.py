#!/usr/bin/env python3
"""pvtd -- FPGA die-temperature sampler and read-only web page, run on the CM4.

Two sources, each optional:
  * UART  /dev/serial0 (CM4 GPIO14/15 <-> FPGA C17/A15), the ring-oscillator
    line any of these bitstreams send at 115200 every ~0.34 s:
        R r0 r1 r2 r3 r4                         cm4/pvt-web/fpga, xadc-monitor
        R r0 r1 r2 r3 r4 H dd M m D .. .. P .. .. .. A f   fpga/pvt-lab
    f_RO = 2 * rN * 50e6 / 2^20.
  * XADC  over JTAG: `openFPGALoader --read-xadc` with the XPCU cable on the
    CM4's USB. Die temperature, VCCINT, VCCAUX. Does not reset the FPGA.

If no XADC reading is fresh and a pvt-lab calib.json is installed, the die
temperature is estimated from the ring oscillators instead (marked as such).

Samples go to SQLite every --period seconds (default 10 s, kept --keep-days).
HTTP (GET only, no shell, no JTAG, no writes):
    /              the page (web/index.html)
    /api/now       latest values
    /api/history?h=24    downsampled history for the last h hours (max 720)

Python 3 stdlib only.
"""
import argparse, json, os, re, select, sqlite3, subprocess, termios, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REF_HZ, GATE, NRO = 50e6, 1 << 20, 5
HERE = os.path.dirname(os.path.abspath(__file__))


def ro_mhz(count):
    return 2 * count * REF_HZ / GATE / 1e6


# ------------------------------------------------------------------ UART
class Uart(threading.Thread):
    daemon = True

    def __init__(self, dev):
        super().__init__()
        self.dev, self.lock = dev, threading.Lock()
        self.ros = self.extra = None
        self.t = 0.0
        self.err = "not started"

    def _open(self):
        fd = os.open(self.dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(fd)
        a[0] = 0                                      # iflag
        a[1] = 0                                      # oflag
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0                                      # lflag: raw
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)
        return fd

    def parse(self, line):
        tok = line.split()
        if len(tok) < 6 or tok[0] != "R":
            return
        try:
            ros = [ro_mhz(int(x, 16)) for x in tok[1:6]]
        except ValueError:
            return
        if not all(50 < f < 2000 for f in ros):       # a corrupted line
            return
        # pvt-lab adds single-letter fields after the ROs: H dd, M m, D ..., P ..., A f
        extra = None
        if len(tok) >= 10 and tok[6] == "H" and tok[8] == "M":
            try:
                extra = dict(heater_duty=int(tok[7], 16), heater_mode=int(tok[9], 16))
                if "A" in tok[10:] and tok.index("A", 10) + 1 < len(tok):
                    a = int(tok[tok.index("A", 10) + 1], 16)
                    extra["alarms"] = [n for b, n in ((1, "droop"), (2, "glitch"), (4, "over-temp")) if a & b]
            except ValueError:
                extra = None
        with self.lock:
            self.ros, self.extra, self.t, self.err = ros, extra, time.time(), None

    def run(self):
        while True:
            try:
                fd = self._open()
                buf = b""
                while True:
                    r, _, _ = select.select([fd], [], [], 5)
                    if not r:
                        continue
                    try:
                        chunk = os.read(fd, 512)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        time.sleep(0.2)
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.parse(line.decode("ascii", "replace"))
                    buf = buf[-512:]
            except OSError as e:
                with self.lock:
                    self.err = "uart: %s" % e.strerror
                time.sleep(5)

    def latest(self):
        with self.lock:
            return self.ros, self.extra, self.t


# ------------------------------------------------------------------ XADC
class Xadc(threading.Thread):
    daemon = True

    def __init__(self, ofl, firmware, freq, period):
        super().__init__()
        self.cmd = [ofl, "-c", "xilinxPlatformCableUsb", "--freq", str(freq)]
        if firmware and os.path.exists(firmware):
            self.cmd += ["--probe-firmware", firmware]
        self.cmd += ["--read-xadc"]
        self.period, self.lock = period, threading.Lock()
        self.val, self.t, self.err = None, 0.0, "not started"

    def read(self):
        out = subprocess.run(self.cmd, capture_output=True, text=True, timeout=30).stdout
        d = json.loads(re.sub(r",\s*}", "}", out[out.find("{"):out.rfind("}") + 1]))
        return dict(temp_c=float(d["temp"]), vccint_v=float(d["vccint"]), vccaux_v=float(d["vccaux"]))

    def run(self):
        while True:
            t0 = time.time()
            try:
                v = self.read()
                with self.lock:
                    self.val, self.t, self.err = v, time.time(), None
            except FileNotFoundError:
                with self.lock:
                    self.err = "openFPGALoader not installed"
            except Exception as e:  # no cable, cable busy, bad JSON
                with self.lock:
                    self.err = "xadc: %s" % type(e).__name__
            time.sleep(max(1.0, self.period - (time.time() - t0)))

    def latest(self):
        with self.lock:
            return self.val, self.t


# ------------------------------------------------------------------ state
class Store:
    COLS = ["temp_c", "vccint_v", "vccaux_v"] + ["ro%d_mhz" % k for k in range(NRO)] + ["ro_temp_c"]

    def __init__(self, path, keep_days):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.keep = keep_days * 86400
        cols = ", ".join("%s REAL" % c for c in self.COLS)
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS s (t INTEGER PRIMARY KEY, %s)" % cols)
            self.db.commit()

    def add(self, t, row):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO s VALUES (?%s)" % (",?" * len(self.COLS)),
                            [int(t)] + [row.get(c) for c in self.COLS])
            self.db.execute("DELETE FROM s WHERE t < ?", (int(t - self.keep),))
            self.db.commit()

    def history(self, hours, points=600):
        t1 = time.time()
        t0 = t1 - hours * 3600
        step = max(10, int(hours * 3600 / points))
        sel = ", ".join("AVG(%s)" % c for c in self.COLS)
        with self.lock:
            rows = self.db.execute("SELECT (t/?)*? AS b, %s FROM s WHERE t >= ? GROUP BY b ORDER BY b" % sel,
                                   (step, step, int(t0))).fetchall()
        return dict(step_s=step, cols=["t"] + self.COLS,
                    rows=[[r[0]] + [None if v is None else round(v, 4) for v in r[1:]] for r in rows])


class Pvtd:
    def __init__(self, args):
        self.args = args
        self.uart = Uart(args.serial)
        self.xadc = Xadc(args.ofl, args.firmware, args.jtag_freq, args.xadc_period) if args.xadc else None
        self.store = Store(args.db, args.keep_days)
        self.cal = None
        if args.calib and os.path.exists(args.calib):
            self.cal = json.load(open(args.calib))
        self.start = time.time()

    def ro_temp(self, ros, vint):
        if not self.cal or not ros:
            return None
        k = self.args.calib_ro
        c = self.cal["ro"][k]
        return (ros[k] - c["a"] - c["c"] * ((vint or 1.0) - 1.0)) / c["b"]

    def now(self):
        ros, extra, tu = self.uart.latest()
        xv, tx = self.xadc.latest() if self.xadc else (None, 0.0)
        t = time.time()
        if ros and t - tu > 10:
            ros = extra = None
        if xv and t - tx > 3 * self.args.xadc_period + 30:
            xv = None
        # calib.json is only valid for the pvt-lab bitstream (routing moves the
        # RO frequencies), and only pvt-lab sends the H/M fields.
        rt = self.ro_temp(ros, xv and xv["vccint_v"]) if extra else None
        temp, src = (xv["temp_c"], "xadc") if xv else ((rt, "ro") if rt is not None else (None, None))
        return dict(time=int(t), temp_c=temp, temp_source=src, ro_temp_c=rt,
                    vccint_v=xv and xv["vccint_v"], vccaux_v=xv and xv["vccaux_v"],
                    ro_mhz=ros, heater=extra, uptime_s=int(t - self.start),
                    sources=dict(uart=ros is not None, xadc=xv is not None,
                                 calib=self.cal is not None))

    def sampler(self):
        while True:
            time.sleep(self.args.period - time.time() % self.args.period)
            n = self.now()
            if n["temp_c"] is None and not n["ro_mhz"]:
                continue
            row = dict(temp_c=n["temp_c"], vccint_v=n["vccint_v"], vccaux_v=n["vccaux_v"],
                       ro_temp_c=n["ro_temp_c"])
            for k, f in enumerate(n["ro_mhz"] or []):
                row["ro%d_mhz" % k] = f
            self.store.add(n["time"], row)

    def run(self):
        self.uart.start()
        if self.xadc:
            self.xadc.start()
        threading.Thread(target=self.sampler, daemon=True).start()
        srv = ThreadingHTTPServer((self.args.bind, self.args.http), make_handler(self))
        srv.daemon_threads = True
        print("pvtd on %s:%d" % (self.args.bind, self.args.http), flush=True)
        srv.serve_forever()


# ------------------------------------------------------------------ http
def make_handler(p):
    page_path = os.path.join(p.args.web, "index.html")

    class H(BaseHTTPRequestHandler):
        server_version = "pvtd"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, cache="no-store"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", "no-cache")
            if u.path == "/api/now":
                return self._send(200, json.dumps(p.now()), "application/json")
            if u.path == "/api/history":
                try:
                    h = float(parse_qs(u.query).get("h", ["24"])[0])
                except ValueError:
                    h = 24
                h = min(max(h, 0.1), 720)
                return self._send(200, json.dumps(p.store.history(h)), "application/json")
            if u.path == "/healthz":
                return self._send(200, "ok\n", "text/plain")
            self._send(404, "not found\n", "text/plain")

        do_HEAD = do_GET

        def _deny(self):
            self._send(405, "read-only\n", "text/plain")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="/dev/serial0")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--http", type=int, default=8090)
    ap.add_argument("--db", default="/var/lib/pvtd/pvt.db")
    ap.add_argument("--web", default=os.path.join(HERE, "web"))
    ap.add_argument("--period", type=float, default=10, help="s between stored samples")
    ap.add_argument("--keep-days", type=float, default=30)
    ap.add_argument("--no-xadc", dest="xadc", action="store_false")
    ap.add_argument("--xadc-period", type=float, default=10, help="s between JTAG XADC reads")
    ap.add_argument("--ofl", default="/usr/local/bin/openFPGALoader")
    ap.add_argument("--firmware", default="/usr/local/share/pvtd/xusb_xp2.hex")
    ap.add_argument("--jtag-freq", type=int, default=1500000)
    ap.add_argument("--calib", default="/etc/pvtd/calib.json", help="fpga/pvt-lab calib.json")
    ap.add_argument("--calib-ro", type=int, default=2, help="RO used as thermometer")
    Pvtd(ap.parse_args()).run()


if __name__ == "__main__":
    main()
