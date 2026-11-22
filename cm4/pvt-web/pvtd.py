#!/usr/bin/env python3
"""pvtd -- the FPGA playground daemon behind fpga.klayshomelab.com (runs on the CM4).

Sources (each optional, the page shows what is there):
  * pvt_lab on CM4 UART0 /dev/serial0, 115200 (FPGA A15 -> GPIO15, GPIO14 -> C17):
        R r0..r4 [H dd M m D ssssss ffffff P nnn xxx wwwwww A f]
    ring oscillators (f = 2*rN*50e6/2^20; with pvt-lab these are heater-off
    "quiet" counts), heater duty/mode, TDC, droop monitor, alarm flags.
    Commands go back on the same UART (see fpga/pvt-lab/pvt_lab.v).
  * trng on CM4 UART3 /dev/ttyAMA3, 1 Mbaud binary (FPGA C11 -> GPIO5, GPIO4 -> E18):
    raw ring-oscillator bits; single-byte commands 0-9 rate, S stop, G go, C clear.
  * XADC over JTAG: `openFPGALoader --read-xadc` with the XPCU cable on the CM4.

Ring-oscillator thermometers are fitted on the CM4 against the XADC
(f_k = a + b*T + c*(VCCINT-1) over the stored history), so a new bitstream
calibrates itself once the heater has swept the die over >= 8 C. Until then
/etc/pvtd/calib.json (from fpga/pvt-lab sweep) is used if present.

Public control API (anyone may use it; every input is validated, rate-limited
and bounded, there is no path to JTAG, files or a shell):
    POST /api/control  {"action": ..., ...}
        heater {duty 0-255} | hold {temp_c} | off | quiet {on} | clear
        | droop {tap 0-63} | trng {run} | trng_rate {div 0-9} | trng_clear
Safety: the heater is refused without a trusted temperature, turns off at
--tmax (and stays off until the die is 10 C cooler), every heater/hold
session has a --lease after which it turns off, the pvt_lab watchdog drops
the heater 2.7 s after pvtd stops sending keepalives, and the on-chip
over-temperature alarm is armed at --tmax + 3 C as a hardware backstop.

GET /, /api/now, /api/history?h=24, /api/trng/random, /healthz.
Python 3 stdlib only.
"""
import argparse, base64, collections, hashlib, json, math, os, re, select, sqlite3, subprocess
import termios, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REF_HZ, GATE, NRO = 50e6, 1 << 20, 5
HERE = os.path.dirname(os.path.abspath(__file__))


def ro_mhz(count):
    return 2 * count * REF_HZ / GATE / 1e6


GRID_N = 64            # 8x8 die-map rings, index row*8+col, row 0 at the bottom


def grid_mhz(count):
    """Die-map ring: local /4 + the counter's /2, 2^18-cycle window (pvt-lab v6)."""
    return 8 * count * REF_HZ / (1 << 18) / 1e6


def fft(x):
    """In-place iterative radix-2 FFT of a list of complex numbers."""
    n = len(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
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


def mhz_to_count(mhz):
    return int(round(mhz * 1e6 * GATE / (2 * REF_HZ)))


def open_serial(dev, baud, write=True):
    fd = os.open(dev, (os.O_RDWR if write else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    speed = getattr(termios, "B%d" % baud, None)
    if speed is not None:
        a[4] = a[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, a)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


# ------------------------------------------------------------------ pvt_lab link
class PvtLink(threading.Thread):
    daemon = True

    def __init__(self, dev):
        super().__init__()
        self.dev, self.lock, self.wlock = dev, threading.Lock(), threading.Lock()
        self.fd = None
        self.d, self.t = None, 0.0
        self.grid, self.grid_t, self.grid_seq = None, 0.0, 0    # latest die-map frame (MHz)
        self.qbuf = {}                                          # scope dump: line -> 16 samples

    def parse(self, line):
        tok = line.split()
        if tok and tok[0] == "G" and len(tok) == 2 + GRID_N:
            try:
                g = [grid_mhz(int(x, 16)) for x in tok[2:]]
            except ValueError:
                return
            g = [f if 100 < f < 1500 else None for f in g]      # dead / garbled ring -> None
            with self.lock:
                self.grid, self.grid_t = g, time.time()
                self.grid_seq += 1
            return
        if tok and tok[0] == "Q" and len(tok) == 18:
            try:
                k, v = int(tok[1], 16), [int(x, 16) for x in tok[2:]]
            except ValueError:
                return
            with self.lock:
                self.qbuf[k] = v
            return
        if len(tok) < 6 or tok[0] != "R":
            return
        try:
            ros = [ro_mhz(int(x, 16)) for x in tok[1:6]]
        except ValueError:
            return
        # a ring that doesn't oscillate (count 0) or a garbled field reads None
        ros = [f if 50 < f < 2000 else None for f in ros]
        if not any(ros):
            return
        d = dict(ro=ros, lab=False)
        kv = {}
        i = 6
        while i < len(tok):                      # single-letter keys, hex values until the next key
            k, j = tok[i], i + 1
            while j < len(tok) and not (len(tok[j]) == 1 and tok[j] in "HMDPA"):
                j += 1
            kv[k] = tok[i + 1:j]
            i = j
        try:
            if "H" in kv and "M" in kv:
                d.update(lab=True, duty=int(kv["H"][0], 16), mode=int(kv["M"][0], 16))
            if "D" in kv and len(kv["D"]) >= 2 and int(kv["D"][0], 16) and int(kv["D"][1], 16):
                taps = int(kv["D"][0], 16) / 4096.0
                d.update(tdc_taps=taps, tdc_ps=1e12 / (ro_mhz(int(kv["D"][1], 16)) * 1e6 * taps))
            if "P" in kv and len(kv["P"]) >= 3:
                d.update(droop_min=int(kv["P"][0], 16), droop_max=int(kv["P"][1], 16),
                         droop_avg=int(kv["P"][2], 16) / 32768.0)
            if "A" in kv and kv["A"]:
                a = int(kv["A"][0], 16)
                d["alarms"] = [n for b, n in ((1, "droop"), (2, "glitch"), (4, "over-temp")) if a & b]
        except (ValueError, IndexError):
            return
        with self.lock:
            self.d, self.t = d, time.time()

    def send(self, cmd):
        with self.wlock:
            if self.fd is None:
                return False
            try:
                os.write(self.fd, (cmd + "\n").encode())
                return True
            except OSError:
                return False

    def run(self):
        while True:
            try:
                self.fd = open_serial(self.dev, 115200)
                buf = b""
                while True:
                    r, _, _ = select.select([self.fd], [], [], 5)
                    if not r:
                        continue
                    try:
                        chunk = os.read(self.fd, 512)
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
            except OSError:
                self.fd = None
                time.sleep(5)

    def latest(self, max_age=10):
        with self.lock:
            return self.d if self.d and time.time() - self.t < max_age else None


# ------------------------------------------------------------------ TRNG link
class TrngLink(threading.Thread):
    """Raw bits at 1 Mbaud. Keeps a rolling window for statistics."""
    daemon = True
    WIN = 1 << 16            # bytes kept for statistics

    def __init__(self, dev):
        super().__init__()
        self.dev, self.lock = dev, threading.Lock()
        self.fd = None
        self.buf = bytearray()
        self.rate = collections.deque()   # (time, bytes), last 3 s
        self.last_rx, self.total = 0.0, 0
        self.want_run, self.div = True, 4
        self.pool = hashlib.sha256()                # conditioner input

    def send(self, b):
        if self.fd is not None:
            try:
                os.write(self.fd, b)
                return True
            except OSError:
                pass
        return False

    def run(self):
        while True:
            try:
                self.fd = open_serial(self.dev, 1000000)
                self.send(b"%dG" % self.div if self.want_run else b"S")
                while True:
                    r, _, _ = select.select([self.fd], [], [], 1)
                    if not r:
                        continue
                    try:
                        chunk = os.read(self.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    now = time.time()
                    with self.lock:
                        self.buf += chunk
                        if len(self.buf) > self.WIN:
                            del self.buf[:len(self.buf) - self.WIN]
                        self.rate.append((now, len(chunk)))
                        while self.rate and now - self.rate[0][0] > 3:
                            self.rate.popleft()
                        self.last_rx = now
                        self.total += len(chunk)
                        self.pool.update(chunk)
            except OSError:
                self.fd = None
                time.sleep(5)

    def random(self, n=32):
        """Conditioned output: SHA-256 over everything received since the last call
        (>= 2x as many raw bits as output bits, else None)."""
        with self.lock:
            if len(self.buf) < 2 * n or time.time() - self.last_rx > 5:
                return None
            out = self.pool.digest()[:n]
            self.pool = hashlib.sha256(out)
            return out

    def stats(self):
        now = time.time()
        with self.lock:
            live = now - self.last_rx < 2
            data = bytes(self.buf[-8192:])
            pts = [r for r in self.rate if now - r[0] < 3]
            bps = sum(r[1] for r in pts) * 8 / 3.0 if pts else 0.0
            total = self.total
        st = dict(live=live, running=self.want_run, div=self.div, bitrate=bps if live else 0.0,
                  total_bytes=total)
        if live and len(data) >= 1024:
            n = len(data) * 8
            ones = sum(bin(b).count("1") for b in data)
            p1 = ones / n
            pmax = max(p1, 1 - p1)
            # 90B most-common-value estimate with its 99% upper bound
            pu = min(1.0, pmax + 2.576 * math.sqrt(pmax * (1 - pmax) / (n - 1)))
            bits = int.from_bytes(data, "big")
            x = bits ^ (bits >> 1)
            trans = bin(x & ((1 << (n - 1)) - 1)).count("1")
            # longest run
            s = bin(bits)[2:].zfill(n)
            longest = max(len(m.group(0)) for m in re.finditer(r"0+|1+", s))
            st.update(p1=p1, min_entropy=-math.log2(pu), shannon=-(p1 * math.log2(p1) + (1 - p1) * math.log2(1 - p1)),
                      autocorr1=1 - 2 * trans / (n - 1) - (2 * p1 - 1) ** 2, longest_run=longest,
                      bitmap=base64.b64encode(data[-2048:]).decode())
        elif not live and self.want_run and total:
            st["note"] = "stream stopped (health-test alarm latched?)"
        return st


# ------------------------------------------------------------------ XADC over JTAG
class Jtag(threading.Thread):
    daemon = True

    def __init__(self, args):
        super().__init__()
        self.args, self.lock = args, threading.Lock()   # lock = the cable
        self.base = [args.ofl, "-c", "xilinxPlatformCableUsb"]
        if os.path.exists(args.firmware):
            self.base += ["--probe-firmware", args.firmware]
        self.val, self.t, self.err = None, 0.0, "not started"
        self.last_load, self.load_msg = 0.0, None

    def read(self):
        out = subprocess.run(self.base + ["--freq", str(self.args.jtag_freq), "--read-xadc"],
                             capture_output=True, text=True, timeout=30).stdout
        d = json.loads(re.sub(r",\s*}", "}", out[out.find("{"):out.rfind("}") + 1]))
        return dict(temp_c=float(d["temp"]), vccint_v=float(d["vccint"]), vccaux_v=float(d["vccaux"]))

    def load(self, bit):
        """Reconfigure the FPGA (only used when it has lost its design)."""
        with self.lock:
            self.last_load = time.time()
            for freq in (6000000, 1500000):
                r = subprocess.run(self.base + ["--freq", str(freq), "--bitstream", bit],
                                   capture_output=True, text=True, timeout=300)
                if "done 1" in r.stdout + r.stderr:
                    self.load_msg = "reloaded %s at %s" % (os.path.basename(bit), time.strftime("%H:%M"))
                    return True
            self.load_msg = "reload failed at %s" % time.strftime("%H:%M")
            return False

    def run(self):
        while True:
            t0 = time.time()
            with self.lock:
                try:
                    v = self.read()
                    self.val, self.t, self.err = v, time.time(), None
                except FileNotFoundError:
                    self.err = "openFPGALoader not installed"
                except Exception as e:
                    self.err = type(e).__name__
            time.sleep(max(1.0, self.args.xadc_period - (time.time() - t0)))

    def latest(self):
        return self.val if self.val and time.time() - self.t < 3 * self.args.xadc_period + 30 else None


# ------------------------------------------------------------------ storage
class Store:
    COLS = (["temp_c", "vccint_v", "vccaux_v"] + ["ro%d_mhz" % k for k in range(NRO)] +
            ["ro_temp_c", "duty", "tdc_ps", "droop_avg", "trng_p1", "quiet"])

    def __init__(self, path, keep_days):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.keep = keep_days * 86400
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS s (t INTEGER PRIMARY KEY)")
            have = {r[1] for r in self.db.execute("PRAGMA table_info(s)")}
            for c in self.COLS:
                if c not in have:
                    self.db.execute("ALTER TABLE s ADD COLUMN %s REAL" % c)
            self.db.commit()

    def add(self, t, row):
        cols = ["t"] + self.COLS
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO s (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                            [int(t)] + [row.get(c) for c in self.COLS])
            self.db.execute("DELETE FROM s WHERE t < ?", (int(t - self.keep),))
            self.db.commit()

    def add_grid(self, t, duty, temp, vint, cells):
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS g (t INTEGER PRIMARY KEY, duty REAL, temp REAL, vint REAL, cells TEXT)")
            self.db.execute("INSERT OR REPLACE INTO g VALUES (?,?,?,?,?)",
                            (int(t), duty, temp, vint, json.dumps([None if c is None else round(c, 4) for c in cells])))
            self.db.execute("DELETE FROM g WHERE t < ?", (int(t - self.keep),))
            self.db.commit()

    def grid_rows(self, since):
        """Heater-off die-map frames with an XADC reading: (T, vint, cells)."""
        with self.lock:
            try:
                rows = self.db.execute("SELECT temp, vint, cells FROM g WHERE t >= ? AND duty = 0 AND temp IS NOT NULL "
                                       "AND vint IS NOT NULL", (int(since),)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def history(self, hours, points=600):
        t0 = time.time() - hours * 3600
        step = max(10, int(hours * 3600 / points))
        sel = ", ".join("AVG(%s)" % c for c in self.COLS)
        with self.lock:
            rows = self.db.execute("SELECT (t/?)*? AS b, %s FROM s WHERE t >= ? GROUP BY b ORDER BY b" % sel,
                                   (step, step, int(t0))).fetchall()
        return dict(step_s=step, cols=["t"] + self.COLS,
                    rows=[[r[0]] + [None if v is None else round(v, 4) for v in r[1:]] for r in rows])

    def calib_rows(self, since):
        with self.lock:
            return self.db.execute("SELECT temp_c, vccint_v, %s FROM s WHERE t >= ? AND temp_c IS NOT NULL "
                                   "AND vccint_v IS NOT NULL AND (quiet = 1 OR duty = 0)" %
                                   ",".join("ro%d_mhz" % k for k in range(NRO)), (int(since),)).fetchall()


def solve3(A, b):
    M = [row[:] + [bb] for row, bb in zip(A, b)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            raise ValueError("singular")
        for r in range(3):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][3] / M[i][i] for i in range(3)]


def fit_calib(rows):
    """rows: (T, vint, f0..f4). Returns calib dict like pvt_lab.py sweep."""
    cal = {"ro": [], "n": len(rows), "t_range": [min(r[0] for r in rows), max(r[0] for r in rows)],
           "date": time.strftime("%Y-%m-%d %H:%M"), "source": "auto (pvtd vs XADC)"}
    for k in range(NRO):
        rk = [r for r in rows if r[2 + k] is not None]   # each ring on its own samples
        if len(rk) < 60 or max(r[0] for r in rk) - min(r[0] for r in rk) < 8:
            cal["ro"].append(dict(a=0.0, b=0.0, c=0.0, rms_mhz=None, rms_c=99.0))
            continue
        X = [(1.0, r[0], r[1] - 1.0) for r in rk]
        y = [r[2 + k] for r in rk]
        A = [[sum(x[i] * x[j] for x in X) for j in range(3)] for i in range(3)]
        bb = [sum(x[i] * yy for x, yy in zip(X, y)) for i in range(3)]
        a, b, c = solve3(A, bb)
        res = [yy - (a + b * x[1] + c * x[2]) for x, yy in zip(X, y)]
        rms = (sum(e * e for e in res) / len(res)) ** 0.5
        cal["ro"].append(dict(a=a, b=b, c=c, rms_mhz=rms, rms_c=abs(rms / b) if b else 99.0))
    return cal


# ------------------------------------------------------------------ the daemon
class Pvtd:
    def __init__(self, args):
        self.args = args
        self.lab = PvtLink(args.serial)
        self.trng = TrngLink(args.trng) if args.trng else None
        self.jtag = Jtag(args) if args.xadc else None
        self.store = Store(args.db, args.keep_days)
        self.lock = threading.Lock()
        self.cal, self.cal_src = None, None
        self.auto_cal_path = os.path.join(os.path.dirname(args.db), "calib_auto.json")
        for path, src in ((self.auto_cal_path, "auto"), (args.calib, "file")):
            if path and os.path.exists(path):
                try:
                    self.cal, self.cal_src = json.load(open(path)), src
                    break
                except ValueError:
                    pass
        # control state
        self.ctl = dict(mode="off", duty=0, hold_c=None, lease_until=0.0, quiet=True, droop_tap=args.droop_tap)
        self.cooldown = False
        self.pi = None
        self.grid_ref, self.grid_ref_t, self.grid_off_since = None, 0.0, None
        self.grid_cal = None
        self.grid_cal_path = os.path.join(os.path.dirname(args.db), "calib_grid.json")
        if os.path.exists(self.grid_cal_path):
            try:
                self.grid_cal = json.load(open(self.grid_cal_path))
            except ValueError:
                pass
        self.ir = None                     # last IR-drop snapshot
        self.scope = None                  # last scope capture
        self.busy = None                   # "scope" / "ir map" while a sequence owns the heater
        self.seq_last = 0.0
        self.log = collections.deque(maxlen=40)
        self.hits = collections.defaultdict(collections.deque)
        self.glob = collections.deque()
        self.start = time.time()
        self.last_lab = time.time()

    # -------------------------------------------- thermometry
    def best_ro(self):
        if not self.cal:
            return None
        return min(range(NRO), key=lambda k: self.cal["ro"][k].get("rms_c", 99.0))

    def ro_temps(self, d, vint):
        if not self.cal or not d or not d["lab"]:
            return None
        out = []
        for k in range(NRO):
            c = self.cal["ro"][k]
            f = d["ro"][k]
            out.append((f - c["a"] - c["c"] * ((vint or 1.0) - 1.0)) / c["b"] if c["b"] and f else None)
        return out

    def now(self):
        d = self.lab.latest()
        xv = self.jtag.latest() if self.jtag else None
        vint = xv and xv["vccint_v"]
        rts = self.ro_temps(d, vint)
        k = self.best_ro()
        rt = rts[k] if rts and k is not None else None
        temp, src = (xv["temp_c"], "xadc") if xv else ((rt, "ro%d" % k) if rt is not None else (None, None))
        with self.lock:
            ctl = dict(self.ctl)
            ctl["lease_s"] = max(0, int(ctl.pop("lease_until") - time.time())) if ctl["mode"] != "off" else 0
            log = list(self.log)
            cool = self.cooldown
        out = dict(time=int(time.time()), temp_c=temp, temp_source=src,
                   vccint_v=vint, vccaux_v=xv and xv["vccaux_v"],
                   ro_mhz=d and d["ro"], ro_temp_c=rts, thermometer=k,
                   calib=self.cal and dict(source=self.cal_src, n=self.cal.get("n"), t_range=self.cal.get("t_range"),
                                           rms_c=[round(r.get("rms_c", 99), 2) for r in self.cal["ro"]]),
                   lab=d and {kk: d.get(kk) for kk in ("duty", "mode", "tdc_taps", "tdc_ps", "droop_min",
                                                       "droop_max", "droop_avg", "alarms") if kk in d},
                   control=ctl, cooldown=cool, tmax=self.args.tmax, max_hold=self.args.max_hold,
                   log=log, uptime_s=int(time.time() - self.start),
                   sources=dict(lab=d is not None and d["lab"], ro=d is not None, xadc=xv is not None,
                                trng=bool(self.trng and self.trng.stats()["live"])),
                   jtag_note=self.jtag.load_msg if self.jtag else None)
        if self.trng:
            out["trng"] = self.trng.stats()
        g = self.lab.grid if time.time() - self.lab.grid_t < 3 else None
        if g:
            out["grid"] = dict(mhz=[None if f is None else round(f, 4) for f in g],
                               ref=self.grid_ref and [None if f is None else round(f, 4) for f in self.grid_ref],
                               ref_age_s=int(time.time() - self.grid_ref_t) if self.grid_ref else None,
                               temp=[None if v is None else round(v, 2) for v in (self.grid_temps(g, vint) or [])] or None,
                               cal=self.grid_cal and dict(n=self.grid_cal["n"], t_range=self.grid_cal["t_range"]),
                               ir=self.ir)
        out["busy"] = self.busy
        out["scope_t"] = self.scope and self.scope["t"]
        return out

    # -------------------------------------------- control
    def note(self, who, what):
        with self.lock:
            self.log.appendleft(dict(t=int(time.time()), who=who, what=what))

    def apply(self, mode, duty=0, hold_c=None):
        """Push a heater state to the FPGA. Caller validated it."""
        if mode == "off":
            self.lab.send("H00")
        elif mode == "heater":
            self.lab.send("H%02X" % duty)
        elif mode == "hold" and self.jtag and self.jtag.latest():
            self.pi = dict(i=None, t=0.0)          # host loop on the XADC, see hold_step()
            self.lab.send("H00")
        elif mode == "hold":
            self.pi = None
            k = self.best_ro()
            c = self.cal["ro"][k]
            xv = self.jtag.latest() if self.jtag else None
            vint = xv["vccint_v"] if xv else 1.0
            cnt = mhz_to_count(c["a"] + c["b"] * hold_c + c["c"] * (vint - 1.0))
            self.lab.send("P%d" % (1 if c["b"] > 0 else 0))
            self.lab.send("G6")
            self.lab.send("MFF")
            self.lab.send("S%d%06X" % (k, cnt))

    def hold_step(self):
        """PI loop on the XADC (one step per new reading). duty in 0..255.
        Plant: about +25 C at full duty, a fast (~15 s) and a slow (~2 min) pole."""
        xv, t = (self.jtag.val, self.jtag.t) if self.jtag else (None, 0)
        if not xv or not self.pi or t == self.pi["t"]:
            return
        dt = min(t - self.pi["t"], 10.0) if self.pi["t"] else 3.0
        self.pi["t"] = t
        err = self.ctl["hold_c"] - xv["temp_c"]
        kp, ki = 30.0, 0.35
        if self.pi["i"] is None:                    # bumpless start: feed-forward from the plant gain
            self.pi["i"] = max(0.0, min(255.0, (self.ctl["hold_c"] - 38.0) * 10.2))
        p = kp * err
        new_i = self.pi["i"] + ki * err * dt
        u = p + new_i
        if 0.0 < u < 255.0 or (u >= 255.0 and err < 0) or (u <= 0.0 and err > 0):
            self.pi["i"] = max(0.0, min(255.0, new_i))   # anti-windup: integrate only when not pushing a limit
        duty = int(max(0, min(255, round(p + self.pi["i"]))))
        with self.lock:
            self.ctl["duty"] = duty
        self.lab.send("H%02X" % duty)

    def arm_hw_alarm(self):
        """On-chip over-temperature alarm at tmax+3 C on the best RO (hardware backstop)."""
        if self.ctl["droop_tap"] is not None:
            self.lab.send("T%02X" % self.ctl["droop_tap"])
        k = self.best_ro()
        if k is None or not self.cal["ro"][k]["b"]:
            return
        c = self.cal["ro"][k]
        self.lab.send("P%d" % (1 if c["b"] > 0 else 0))
        self.lab.send("A%d%06X" % (k, mhz_to_count(c["a"] + c["b"] * (self.args.tmax + 3))))

    def trusted_temp(self):
        n = self.now()
        if n["temp_source"] == "xadc":
            return n["temp_c"]
        if n["temp_source"] and self.cal_src == "auto" and n["calib"] and n["calib"]["rms_c"][n["thermometer"]] < 2.5:
            return n["temp_c"]
        if n["temp_source"] and self.cal_src == "file":
            return n["temp_c"]
        return None

    def rate_ok(self, ip):
        now = time.time()
        q = self.hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        while self.glob and now - self.glob[0] > 60:
            self.glob.popleft()
        if len(q) >= self.args.rate or len(self.glob) >= 4 * self.args.rate:
            return False
        q.append(now)
        self.glob.append(now)
        if len(self.hits) > 5000:
            self.hits.clear()
        return True

    def control(self, req, who):
        a = req.get("action")
        if a in ("heater", "hold") and self.busy:
            return 409, "busy: %s in progress" % self.busy
        if a in ("heater", "hold"):
            if self.lab.latest() is None or not self.lab.latest()["lab"]:
                return 409, "the FPGA isn't running the playground design right now"
            t = self.trusted_temp()
            if t is None:
                return 409, "no trusted temperature reading, so the heater stays off"
            if self.cooldown or t >= self.args.tmax - 2:
                return 409, "cooling down: the heater unlocks below %.0f C" % (self.args.tmax - 10)
        if a == "heater":
            duty = req.get("duty")
            if not isinstance(duty, int) or not 0 <= duty <= 255:
                return 400, "duty must be 0-255"
            mode = "heater" if duty else "off"
            with self.lock:
                self.ctl.update(mode=mode, duty=duty, hold_c=None, lease_until=time.time() + self.args.lease)
            self.apply(mode, duty)
            self.note(who, "heater %d%%" % round(duty / 2.55) if duty else "heater off")
        elif a == "hold":
            tc = req.get("temp_c")
            if not isinstance(tc, (int, float)) or not 30 <= tc <= self.args.max_hold:
                return 400, "hold temperature must be 30-%d C" % self.args.max_hold
            if not self.cal and not (self.jtag and self.jtag.latest()):
                return 409, "no XADC and no ring-oscillator calibration yet"
            with self.lock:
                self.ctl.update(mode="hold", duty=0, hold_c=float(tc), lease_until=time.time() + self.args.lease)
            self.apply("hold", hold_c=float(tc))
            self.note(who, "hold %.1f C" % tc)
        elif a == "off":
            with self.lock:
                self.ctl.update(mode="off", duty=0, hold_c=None)
            self.apply("off")
            self.note(who, "heater off")
        elif a == "quiet":
            on = req.get("on")
            if not isinstance(on, bool):
                return 400, "on must be true/false"
            with self.lock:
                self.ctl["quiet"] = on
            self.lab.send("W%d" % on)
            self.note(who, "quiet window %s" % ("on" if on else "off"))
        elif a == "clear":
            self.lab.send("C")
            self.arm_hw_alarm()
            self.note(who, "cleared alarms")
        elif a == "droop":
            tap = req.get("tap")
            if not isinstance(tap, int) or not 0 <= tap <= 63:
                return 400, "tap must be 0-63"
            with self.lock:
                self.ctl["droop_tap"] = tap
            self.lab.send("T%02X" % tap)
            self.note(who, "droop tap %d" % tap)
        elif a == "ir_map":
            why = self.seq_check(heats=True)
            if why:
                return 409, why
            self.busy = "IR-drop map"
            threading.Thread(target=self.run_ir_map, args=(who,), daemon=True).start()
            return 202, "measuring the IR-drop map (about 3 s)"
        elif a == "scope":
            dec, load = req.get("dec", 0), req.get("load", False)
            if not isinstance(dec, int) or not 0 <= dec <= 8 or not isinstance(load, bool):
                return 400, "dec must be 0-8 and load true/false"
            why = self.seq_check(heats=load)
            if why:
                return 409, why
            self.busy = "scope capture"
            threading.Thread(target=self.run_scope, args=(who, dec, load), daemon=True).start()
            return 202, "capturing; the dump takes about 9 s"
        elif a in ("trng", "trng_rate", "trng_clear"):
            if not self.trng:
                return 409, "no TRNG link"
            if a == "trng":
                run = req.get("run")
                if not isinstance(run, bool):
                    return 400, "run must be true/false"
                self.trng.want_run = run
                self.trng.send(b"G" if run else b"S")
                self.note(who, "TRNG %s" % ("started" if run else "stopped"))
            elif a == "trng_rate":
                div = req.get("div")
                if not isinstance(div, int) or not 0 <= div <= 9:
                    return 400, "div must be 0-9"
                self.trng.div = div
                self.trng.send(b"%d" % div)
                self.note(who, "TRNG sample every 2^%d clocks" % div)
            else:
                self.trng.send(b"C")
                if self.trng.want_run:
                    self.trng.send(b"G")
                self.note(who, "TRNG alarm cleared")
        else:
            return 400, "unknown action"
        return 200, "ok"

    # -------------------------------------------- loops
    # -------------------------------------------- die map (8x8 grid of rings)
    def grid_tick(self, duty):
        """1 Hz: keep a heater-off reference frame (EMA) for 'change since the heater went on'."""
        g = self.lab.grid if time.time() - self.lab.grid_t < 3 else None
        if not g:
            return
        if duty or self.busy:
            self.grid_off_since = None
            return
        if self.grid_off_since is None:
            self.grid_off_since = time.time()
        if time.time() - self.grid_off_since < 5:      # let the IR step and the first frames settle
            return
        if self.grid_ref is None:
            self.grid_ref = list(g)
        else:
            self.grid_ref = [None if (a is None or b is None) else 0.8 * a + 0.2 * b
                             for a, b in zip(self.grid_ref, g)]
        self.grid_ref_t = time.time()

    def grid_temps(self, g, vint):
        if not self.grid_cal or not g:
            return None
        out = []
        for f, c in zip(g, self.grid_cal["ring"]):
            out.append((f - c["a"] - c["c"] * ((vint or 1.0) - 1.0)) / c["b"] if f and c and c["b"] else None)
        return out

    def refit_grid(self, since):
        """Per-ring tempco from heater-off frames vs the XADC (no IR drop in them)."""
        rows = self.store.grid_rows(since)
        if len(rows) < 60 or max(r[0] for r in rows) - min(r[0] for r in rows) < 8:
            return
        cal = {"ring": [], "n": len(rows), "t_range": [min(r[0] for r in rows), max(r[0] for r in rows)],
               "date": time.strftime("%Y-%m-%d %H:%M")}
        for i in range(GRID_N):
            rk = [(T, v, cells[i]) for T, v, cells in rows if cells[i] is not None]
            if len(rk) < 60:
                cal["ring"].append(None)
                continue
            X = [(1.0, T, v - 1.0) for T, v, _ in rk]
            y = [f for _, _, f in rk]
            A = [[sum(x[a] * x[b] for x in X) for b in range(3)] for a in range(3)]
            bb = [sum(x[a] * yy for x, yy in zip(X, y)) for a in range(3)]
            try:
                a, b, c = solve3(A, bb)
            except ValueError:
                cal["ring"].append(None)
                continue
            res = [yy - (a + b * x[1] + c * x[2]) for x, yy in zip(X, y)]
            rms = (sum(e * e for e in res) / len(res)) ** 0.5
            cal["ring"].append(dict(a=a, b=b, c=c, rms_c=abs(rms / b) if b else 99.0) if b else None)
        tmp = self.grid_cal_path + ".tmp"
        json.dump(cal, open(tmp, "w"))
        os.replace(tmp, self.grid_cal_path)
        self.grid_cal = cal

    def next_frame(self, timeout=3):
        s0, t0 = self.lab.grid_seq, time.time()
        while self.lab.grid_seq == s0:
            if time.time() - t0 > timeout:
                raise RuntimeError("no die-map frames")
            time.sleep(0.02)
        return list(self.lab.grid)

    def seq_check(self, heats):
        """Gate for the heater-driving sequences (IR map, loaded scope)."""
        if self.busy:
            return "busy: %s in progress" % self.busy
        if time.time() - self.seq_last < self.args.seq_gap:
            return "one capture every %d s, try again shortly" % self.args.seq_gap
        d = self.lab.latest()
        if not d or not d["lab"]:
            return "the FPGA isn't running the playground design right now"
        if heats:
            if self.ctl["mode"] != "off":
                return "turn the heater off first (someone is using it)"
            t = self.trusted_temp()
            if t is None or self.cooldown or t >= self.args.tmax - 5:
                return "the die is too warm or has no temperature reading"
        return None

    def run_ir_map(self, who):
        """Heater off / full / off frames back to back: ring shift under load = local IR drop."""
        try:
            self.lab.send("W0")
            self.lab.send("H00")
            self.next_frame(); off1 = self.next_frame()
            self.lab.send("HFF")
            self.next_frame(); on = self.next_frame()
            self.lab.send("H00")
            self.next_frame(); off2 = self.next_frame()
            ppm = [None if (a is None or b is None or c is None) else (b - (a + c) / 2) / ((a + c) / 2) * 1e6
                   for a, b, c in zip(off1, on, off2)]
            self.ir = dict(t=int(time.time()), ppm=[None if v is None else round(v, 1) for v in ppm])
            good = [v for v in ppm if v is not None]
            self.note(who, "IR-drop map: %.0f to %.0f ppm" % (min(good), max(good)) if good else "IR-drop map")
        except RuntimeError as e:
            self.lab.send("H00")
            self.note("pvtd", "IR-drop map failed: %s" % e)
        finally:
            self.lab.send("W%d" % self.ctl["quiet"])
            self.busy, self.seq_last = None, time.time()

    def run_scope(self, who, dec, load):
        """16384 droop-monitor samples at 25 MS/s / 2^dec, dumped as Q lines (~8 s), then FFT."""
        try:
            self.lab.send("W0")
            if load:
                self.lab.send("HFF")
                time.sleep(0.6)
            with self.lab.lock:
                self.lab.qbuf = {}
            self.lab.send("O%X" % dec)
            t0 = time.time()
            while not self.lab.qbuf and time.time() - t0 < 5:   # first Q line = capture finished
                time.sleep(0.01)
            if load:
                self.lab.send("H00")
            while len(self.lab.qbuf) < 1024 and time.time() - t0 < 30:
                time.sleep(0.2)
            with self.lab.lock:
                q = dict(self.lab.qbuf)
            if len(q) < 1024:
                raise RuntimeError("dump incomplete (%d/1024 lines)" % len(q))
            x = [v / 16.0 for k in range(1024) for v in q[k]]
            n, fs = len(x), 25e6 / (1 << dec)
            mean = sum(x) / n
            rms = (sum((v - mean) ** 2 for v in x) / n) ** 0.5
            win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
            X = fft([complex((v - mean) * w, 0) for v, w in zip(x, win)])
            amp = [4 * abs(X[k]) / n for k in range(n // 2)]       # Hann-corrected amplitude, taps
            peaks = [k for k in range(3, n // 2 - 1) if amp[k] > amp[k - 1] and amp[k] >= amp[k + 1]]
            peaks = sorted(peaks, key=lambda k: -amp[k])[:5]
            G = 8                                                    # 8192 bins -> 1024 (max-hold)
            self.scope = dict(t=int(time.time()), dec=dec, fs=fs, load=load, n=n, mean=round(mean, 3),
                              rms=round(rms, 3), trace=[round(v, 2) for v in x[:2048]],
                              spec=[round(max(amp[i:i + G]), 5) for i in range(0, n // 2, G)], bin_hz=fs / n * G,
                              peaks=[dict(f=k * fs / n, a=round(amp[k], 4)) for k in peaks])
            self.note(who, "scope capture (%s, %.3g MS/s): %.2f taps rms" % ("full heater" if load else "heater off",
                                                                            fs / 1e6, rms))
        except RuntimeError as e:
            self.lab.send("H00")
            self.note("pvtd", "scope capture failed: %s" % e)
        finally:
            self.lab.send("W%d" % self.ctl["quiet"])
            self.busy, self.seq_last = None, time.time()

    def tune_droop(self):
        """Binary-search the droop-monitor tap that puts the edge nearest mid-range
        (avg position falls as the tap rises; the right tap moves with routing)."""
        def avg_at(tap):
            self.lab.send("T%02X" % tap)
            seen, t0 = None, time.time()
            n = 0
            while time.time() - t0 < 3:                 # skip the line in flight, use the next
                time.sleep(0.05)
                d = self.lab.latest(2)
                if d and id(d) != seen:
                    seen, n = id(d), n + 1
                    if n >= 3:
                        return d.get("droop_avg")
            return None
        lo, hi, best = 0, 63, None
        while lo <= hi:
            mid = (lo + hi) // 2
            a = avg_at(mid)
            if a is None:
                return
            if best is None or abs(a - 128) < abs(best[1] - 128):
                best = (mid, a)
            if a > 128:
                lo = mid + 1
            else:
                hi = mid - 1
        if best:
            with self.lock:
                self.ctl["droop_tap"] = best[0]
            self.lab.send("T%02X" % best[0])
            self.note("pvtd", "droop monitor tuned: tap %d (edge at %.0f/256)" % best)

    def supervisor(self):
        """1 Hz: keepalive, lease, temperature guard, self-calibration, FPGA reload."""
        armed_for = None
        tuned = self.args.droop_tap is not None
        last_fit = 0.0
        while True:
            time.sleep(1)
            self.lab.send("K")
            d = self.lab.latest()
            if d and d["lab"]:
                self.last_lab = time.time()
                if d.get("mode") == 2 and self.ctl["mode"] == "off":
                    self.lab.send("H00")                       # clear a stale watchdog trip
                if not tuned:
                    tuned = True
                    self.tune_droop()
                if armed_for != id(self.cal):
                    self.lab.send("W%d" % self.ctl["quiet"])
                    self.arm_hw_alarm()
                    armed_for = id(self.cal)
            with self.lock:
                mode, lease = self.ctl["mode"], self.ctl["lease_until"]
            t = self.trusted_temp()
            if mode != "off":
                why = None
                if time.time() > lease:
                    why = "lease ended"
                elif t is None:
                    why = "lost the temperature reading"
                elif t >= self.args.tmax:
                    why = "reached %.0f C" % self.args.tmax
                    self.cooldown = True
                elif d and d.get("mode") == 2:
                    why = "FPGA watchdog tripped"
                if why:
                    with self.lock:
                        self.ctl.update(mode="off", duty=0, hold_c=None)
                    self.apply("off")
                    self.note("pvtd", "heater off: " + why)
                elif mode == "heater":
                    self.apply("heater", self.ctl["duty"])       # re-assert after a watchdog/reset
                elif mode == "hold" and self.pi is not None:
                    if self.jtag and self.jtag.latest():
                        self.hold_step()
                    else:                                         # XADC lost: hand over to the FPGA loop
                        if self.cal:
                            self.apply("hold", hold_c=self.ctl["hold_c"])
            if self.cooldown and t is not None and t < self.args.tmax - 10:
                self.cooldown = False
            # self-calibration against the XADC, every 10 min
            self.grid_tick(d.get("duty") if d else 0)
            if self.jtag and time.time() - last_fit > 600:
                last_fit = time.time()
                self.refit()
            # FPGA lost its design (power cycle): reload the playground bitstream
            if (self.jtag and self.args.bitstream and os.path.exists(self.args.bitstream)
                    and time.time() - self.last_lab > 90 and time.time() - self.jtag.last_load > 900):
                self.note("pvtd", "no data from the FPGA for 90 s, reloading the playground design")
                self.jtag.load(self.args.bitstream)
                self.last_lab = time.time()
                tuned = self.args.droop_tap is not None

    def refit(self):
        # only samples from the current bitstream (install.sh writes calib_epoch
        # when playground.bit changes, because routing moves the RO frequencies)
        since = time.time() - 7 * 86400
        try:
            since = max(since, float(open(os.path.join(os.path.dirname(self.args.db), "calib_epoch")).read()))
        except (OSError, ValueError):
            pass
        self.refit_grid(since)
        rows = self.store.calib_rows(since)
        if len(rows) < 60 or max(r[0] for r in rows) - min(r[0] for r in rows) < 8:
            return
        try:
            cal = fit_calib(rows)
        except ValueError:
            return
        k = min(range(NRO), key=lambda i: cal["ro"][i]["rms_c"])
        if cal["ro"][k]["rms_c"] > 2.5:
            return
        tmp = self.auto_cal_path + ".tmp"
        json.dump(cal, open(tmp, "w"), indent=1)
        os.replace(tmp, self.auto_cal_path)
        self.cal, self.cal_src = cal, "auto"

    def sampler(self):
        while True:
            time.sleep(self.args.period - time.time() % self.args.period)
            n = self.now()
            if n["temp_c"] is None and not n["ro_mhz"]:
                continue
            # temp_c is the XADC only: the self-calibration fits the ROs against it
            row = dict(temp_c=n["temp_c"] if n["temp_source"] == "xadc" else None,
                       vccint_v=n["vccint_v"], vccaux_v=n["vccaux_v"],
                       ro_temp_c=(n["ro_temp_c"][n["thermometer"]] if n["ro_temp_c"] else None))
            for k, f in enumerate(n["ro_mhz"] or []):
                row["ro%d_mhz" % k] = f
            lab = n["lab"] or {}
            # quiet: the RO counts are heater-off samples (clean for the RO calibration)
            row.update(duty=lab.get("duty"), tdc_ps=lab.get("tdc_ps"), droop_avg=lab.get("droop_avg"),
                       trng_p1=(n.get("trng") or {}).get("p1"), quiet=1 if n["control"]["quiet"] else 0)
            self.store.add(n["time"], row)
            g = self.lab.grid if time.time() - self.lab.grid_t < 3 else None
            if g and not self.busy:
                self.store.add_grid(n["time"], lab.get("duty"), row["temp_c"], n["vccint_v"], g)

    def run(self):
        self.lab.start()
        if self.trng:
            self.trng.start()
        if self.jtag:
            self.jtag.start()
        threading.Thread(target=self.sampler, daemon=True).start()
        threading.Thread(target=self.supervisor, daemon=True).start()
        srv = ThreadingHTTPServer((self.args.bind, self.args.http), make_handler(self))
        srv.daemon_threads = True
        print("pvtd on %s:%d" % (self.args.bind, self.args.http), flush=True)
        try:
            srv.serve_forever()
        finally:
            self.lab.send("H00")


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

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def who(self):
            c = self.headers.get("Cf-Ipcountry")
            return "visitor (%s)" % c if c and re.fullmatch(r"[A-Z]{2}", c) else "visitor"

        def ip(self):
            return self.headers.get("Cf-Connecting-Ip") or self.client_address[0]

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                with open(page_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", "no-cache")
            if u.path == "/api/now":
                return self._json(200, p.now())
            if u.path == "/api/history":
                try:
                    h = float(parse_qs(u.query).get("h", ["24"])[0])
                except ValueError:
                    h = 24
                return self._json(200, p.store.history(min(max(h, 0.1), 720)))
            if u.path == "/api/trng/random":
                if not p.trng or not p.rate_ok(self.ip()):
                    return self._json(429 if p.trng else 409, dict(error="unavailable"))
                r = p.trng.random(32)
                return self._json(200 if r else 409, dict(hex=r.hex()) if r else dict(error="TRNG not streaming"))
            if u.path == "/api/scope":
                return self._json(200 if p.scope else 404, p.scope or dict(error="no capture yet"))
            if u.path == "/healthz":
                return self._send(200, "ok\n", "text/plain")
            self._send(404, "not found\n", "text/plain")

        do_HEAD = do_GET

        def do_POST(self):
            if urlparse(self.path).path != "/api/control":
                return self._send(405, "no\n", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1024 or "json" not in (self.headers.get("Content-Type") or ""):
                return self._json(400, dict(error="bad request"))
            if not p.rate_ok(self.ip()):
                return self._json(429, dict(error="slow down: too many changes, try again in a minute"))
            try:
                req = json.loads(self.rfile.read(n))
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, dict(error="bad json"))
            code, msg = p.control(req, self.who())
            self._json(code, dict(ok=code in (200, 202), message=msg))

        def _deny(self):
            self._send(405, "no\n", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="/dev/serial0")
    ap.add_argument("--trng", default="/dev/ttyAMA3", help="'' to disable")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--http", type=int, default=8090)
    ap.add_argument("--db", default="/var/lib/pvtd/pvt.db")
    ap.add_argument("--web", default=os.path.join(HERE, "web"))
    ap.add_argument("--period", type=float, default=10, help="s between stored samples")
    ap.add_argument("--keep-days", type=float, default=30)
    ap.add_argument("--no-xadc", dest="xadc", action="store_false")
    ap.add_argument("--xadc-period", type=float, default=3)
    ap.add_argument("--ofl", default="/usr/local/bin/openFPGALoader")
    ap.add_argument("--firmware", default="/usr/local/share/pvtd/xusb_xp2.hex")
    ap.add_argument("--jtag-freq", type=int, default=1500000)
    ap.add_argument("--bitstream", default="/usr/local/share/pvtd/playground.bit",
                    help="reloaded over JTAG if the FPGA goes quiet ('' = never)")
    ap.add_argument("--calib", default="/etc/pvtd/calib.json")
    ap.add_argument("--droop-tap", type=lambda v: int(v, 0), default=None,
                    help="fixed droop-monitor delay tap; default: found automatically (edge mid-range)")
    ap.add_argument("--seq-gap", type=int, default=15, help="s between scope / IR-map captures (all visitors)")
    ap.add_argument("--tmax", type=float, default=72.0, help="heater cut-off, C")
    ap.add_argument("--max-hold", type=int, default=65, help="highest hold setpoint, C")
    ap.add_argument("--lease", type=float, default=600, help="s a heater/hold session lasts")
    ap.add_argument("--rate", type=int, default=20, help="control requests per minute per visitor")
    Pvtd(ap.parse_args()).run()


if __name__ == "__main__":
    main()
