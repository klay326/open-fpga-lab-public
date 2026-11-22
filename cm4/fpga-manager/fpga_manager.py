#!/usr/bin/env python3
"""fpga-manager: web project manager / flasher for the QMTech K7 board (CM4 side).

Unprivileged daemon (user fpgamgr). Flashing goes through
`sudo -n /usr/local/sbin/fpga-flash switch <id> --by <user>`, which only takes
catalogued, hash-checked bitstreams and holds the shared fpga-queue while it
works.

HTTP (default :8091):
  GET  /manager/                  the page
  GET  /manager/api/status        catalog, what's loaded, queue, temperature, panel
  POST /manager/api/login         {"user","password"} -> session cookie
  POST /manager/api/logout
  POST /manager/api/switch        {"project": id}   (logged in; rate-limited)
  everything else                 proxied to pvtd (127.0.0.1:8090) while the
                                  playground is loaded; "/" goes to /manager/
                                  otherwise

While a project other than the playground is loaded (pvtd is stopped then), the
daemon watches it, but only while nobody holds the board queue:
  * XADC over JTAG every 10 s; above the catalog's max_temp_c it falls back
    to the playground
  * the CM4 UART the project's panel/health names (tail shown on the page);
    health "uart_line": no matching line for 3x within_s -> fall back
Fallbacks happen at most once per 15 min.

  fpga_manager.py passwd <user>   set a password (run on the Pi with sudo)
"""
import argparse, base64, collections, getpass, hashlib, hmac, http.client, json, os, re
import secrets, select, subprocess, sys, termios, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_S = 12 * 3600
SCRYPT = dict(n=1 << 15, r=8, p=1, maxmem=64 << 20)


# ------------------------------------------------------------------ passwords
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, dklen=32, **SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT["n"], SCRYPT["r"], SCRYPT["p"],
                                       base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def check_pw(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=32, maxmem=64 << 20)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


DUMMY_HASH = hash_pw(secrets.token_hex(8))      # equal-time answer for unknown users


# ------------------------------------------------------------------ helpers
def open_serial(dev, baud):
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    if os.isatty(fd):
        a = termios.tcgetattr(fd)
        sp = getattr(termios, "B%d" % baud)
        a[0] = 0; a[1] = 0; a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL; a[3] = 0
        a[4] = a[5] = sp
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def read_lines(path, n=None):
    try:
        with open(path) as f:
            lines = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    except OSError:
        return []
    return lines[-n:] if n else lines


class SerialTail(threading.Thread):
    """Read-only tail of one CM4 UART."""
    daemon = True

    def __init__(self, dev, baud, match=None):
        super().__init__()
        self.dev, self.baud = dev, baud
        self.rx = re.compile(match) if match else None
        self.lines = collections.deque(maxlen=200)
        self.last_line = self.last_match = 0.0
        self.started = time.time()
        self.stop = threading.Event()
        self.err = None

    def run(self):
        while not self.stop.is_set():
            try:
                fd = open_serial(self.dev, self.baud)
            except OSError as e:
                self.err = str(e)
                self.stop.wait(5)
                continue
            self.err, buf = None, b""
            try:
                while not self.stop.is_set():
                    r, _, _ = select.select([fd], [], [], 1)
                    if not r:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        time.sleep(0.2)
                        continue
                    buf += chunk
                    parts = re.split(rb"\r?\n|\r", buf)
                    buf = parts.pop()[-2048:]
                    for raw in parts:
                        s = raw.decode("ascii", "replace")
                        s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)          # strip ANSI
                        s = "".join(c if c.isprintable() else "." for c in s)[:240]
                        if not s:
                            continue
                        now = time.time()
                        self.lines.append((now, s))
                        self.last_line = now
                        if self.rx and self.rx.search(s):
                            self.last_match = now
            except OSError as e:
                self.err = str(e)
            finally:
                os.close(fd)


# ------------------------------------------------------------------ manager
class Manager:
    def __init__(self, a):
        self.a = a
        self.lock = threading.Lock()
        self.sessions = {}                       # token -> (user, expiry)
        self.login_fail = collections.defaultdict(collections.deque)   # ip -> times
        self.login_fail_all = collections.deque()
        self.switch_times = collections.deque()
        self.job = None                          # running switch: dict
        self.tail = None                         # SerialTail for the loaded project
        self.tail_for = None
        self.xadc, self.xadc_t, self.xadc_err = None, 0.0, None
        self.last_fallback = 0.0
        self.watch = dict(active=False, why="starting")
        self.events = collections.deque(maxlen=30)

    # ---------------------------------------------------------- data
    def catalog(self):
        return json.load(open(self.a.catalog))

    def state(self):
        try:
            return json.load(open(self.a.state))
        except (OSError, ValueError):
            return {"project": None, "history": []}

    def queue(self):
        h = read_lines(os.path.join(self.a.queue_dir, "holder"))
        q = read_lines(os.path.join(self.a.queue_dir, "queue"))
        log = read_lines(os.path.join(self.a.queue_dir, "log"), 200)
        return (dict(name=h[0][0], since=h[0][1] if len(h[0]) > 1 else "", what=h[0][2] if len(h[0]) > 2 else "")
                if h else None,
                [dict(name=x[0], since=x[1] if len(x) > 1 else "", what=x[2] if len(x) > 2 else "") for x in q],
                log)

    def pvtd_active(self):
        try:
            return subprocess.run([self.a.systemctl, "is-active", "--quiet", "pvtd"], timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def note(self, msg):
        self.events.appendleft(dict(t=time.time(), msg=msg))
        print(time.strftime("%F %T ") + msg, flush=True)

    # ---------------------------------------------------------- auth
    def users(self):
        try:
            return json.load(open(self.a.users))
        except (OSError, ValueError):
            return {}

    def login(self, user, pw, ip):
        now = time.time()
        with self.lock:
            for dq in (self.login_fail[ip], self.login_fail_all):
                while dq and now - dq[0] > 600:
                    dq.popleft()
            if len(self.login_fail[ip]) >= 5 or len(self.login_fail_all) >= 20:
                return None, "too many failed logins, try again in 10 minutes"
        u = self.users().get(user) if isinstance(user, str) else None
        ok = check_pw(pw, u["password"] if u else DUMMY_HASH) and u is not None
        with self.lock:
            if not ok:
                self.login_fail[ip].append(now)
                self.login_fail_all.append(now)
                return None, "wrong user or password"
            tok = secrets.token_urlsafe(32)
            self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
            self.sessions[tok] = (user, now + SESSION_S)
        self.note("login: %s from %s" % (user, ip))
        return tok, "ok"

    def session_user(self, tok):
        with self.lock:
            s = self.sessions.get(tok or "")
            if s and s[1] > time.time():
                return s[0]
        return None

    def logout(self, tok):
        with self.lock:
            self.sessions.pop(tok or "", None)

    # ---------------------------------------------------------- switching
    def switch_allowed(self):
        now = time.time()
        with self.lock:
            while self.switch_times and now - self.switch_times[0] > 3600:
                self.switch_times.popleft()
            if self.job:
                return False, "a switch is already running"
            if self.switch_times and now - self.switch_times[-1] < self.a.min_gap:
                return False, "wait %d s between switches" % (self.a.min_gap - (now - self.switch_times[-1]))
            if len(self.switch_times) >= self.a.per_hour:
                return False, "at most %d switches per hour" % self.a.per_hour
        return True, ""

    def start_switch(self, pid, who, automatic=False):
        cat = self.catalog()
        if pid not in {p["id"] for p in cat["projects"]}:
            return 400, "unknown project"
        if not automatic:
            ok, why = self.switch_allowed()
            if not ok:
                return 429, why
        holder, q, _ = self.queue()
        if holder or q:
            return 409, "board busy: %s" % ("held by " + holder["name"] if holder else q[0]["name"] + " is waiting")
        with self.lock:
            if self.job:
                return 409, "a switch is already running"
            self.job = dict(target=pid, by=who, started=time.time(), output="")
            if not automatic:
                self.switch_times.append(time.time())
        threading.Thread(target=self._run_switch, args=(pid, who), daemon=True).start()
        return 202, "switching to " + pid

    def _run_switch(self, pid, who):
        self.note("switch to %s by %s" % (pid, who))
        self.stop_tail()                      # free the CM4 UART for fpga-flash / pvtd
        try:
            r = subprocess.run(self.a.flash_cmd + ["switch", pid, "--by", who],
                               capture_output=True, text=True, timeout=900)
            out, rc = (r.stdout + r.stderr)[-4000:], r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            out, rc = str(e), 4
        res = {0: "loaded", 3: "failed, fell back to the playground", 4: "FAILED, board state unknown",
               10: "board busy", 2: "refused"}.get(rc, "exit %d" % rc)
        self.note("switch to %s: %s" % (pid, res))
        with self.lock:
            self.job = None
            self.last_result = dict(target=pid, rc=rc, result=res, output=out, t=time.time())

    # ---------------------------------------------------------- watching
    def stop_tail(self):
        if self.tail:
            self.tail.stop.set()
            self.tail.join(timeout=3)
        self.tail, self.tail_for = None, None

    def read_xadc(self):
        base = [self.a.ofl, "-c", "xilinxPlatformCableUsb"]
        if os.path.exists(self.a.firmware):
            base += ["--probe-firmware", self.a.firmware]
        out = subprocess.run(base + ["--freq", "1500000", "--read-xadc"],
                             capture_output=True, text=True, timeout=30).stdout
        d = json.loads(re.sub(r",\s*}", "}", out[out.find("{"):out.rfind("}") + 1]))
        return dict(temp_c=float(d["temp"]), vccint_v=float(d["vccint"]), vccaux_v=float(d["vccaux"]))

    def stale(self, st, log):
        """Did anyone other than web-flasher take the board since our last switch?"""
        # the log is in order: look after web-flasher's last release (minute timestamps aren't enough)
        mine = [i for i, ln in enumerate(log) if len(ln) >= 3 and ln[1] == "RELEASE" and ln[2] == "web-flasher"]
        if not mine:
            return None
        after = log[mine[-1] + 1:]
        for i, ln in enumerate(after):
            if len(ln) >= 3 and ln[1] == "TAKE" and ln[2] != "web-flasher":
                rel = [x for x in after[i:] if len(x) >= 3 and x[1] == "RELEASE" and x[2] == ln[2]]
                return dict(by=ln[2], at=ln[0], note=rel[0][3] if rel and len(rel[0]) > 3 else "")
        return None

    def watcher(self):
        last_x = 0.0
        while True:
            time.sleep(2)
            try:
                self._watch_once(last_x)
                if self.watch.get("xadc_due"):
                    last_x = time.time()
            except Exception as e:                # keep watching whatever happens
                self.watch = dict(active=False, why="watcher error: %s" % type(e).__name__)

    def _watch_once(self, last_x):
        holder, q, log = self.queue()
        st = self.state()
        cat = self.catalog()
        byid = {p["id"]: p for p in cat["projects"]}
        p = byid.get(st.get("project"))
        why = None
        if self.job:
            why = "switch running"
        elif holder:
            why = "board held by %s" % holder["name"]
        elif p is None:
            why = "no catalogued project loaded"
        elif p.get("owner") == "pvtd" or self.pvtd_active():
            why = "pvtd owns the board"
        elif not os.path.exists(self.a.flag):
            why = "pvtd isn't held down (CM4 restarted?): not watching"
        elif self.stale(st, log):
            why = "board was used by %s after the last web switch" % self.stale(st, log)["by"]
        if why:
            self.stop_tail()
            self.watch = dict(active=False, why=why)
            return
        # we watch: the loaded project is ours and the board is idle
        want = (p.get("panel") or {}).get("port") or (p.get("health") or {}).get("port")
        if want and self.tail_for != p["id"]:
            self.stop_tail()
            h = p.get("health") or {}
            self.tail = SerialTail(self.a.serial_map.get(want, want),
                                   int((p.get("panel") or {}).get("baud") or h.get("baud") or 115200),
                                   h.get("match") if h.get("type") == "uart_line" else None)
            self.tail.start()
            self.tail_for = p["id"]
        self.watch = dict(active=True, why="watching %s" % p["id"], xadc_due=time.time() - last_x > 10)
        fault = None
        if self.watch["xadc_due"] and self.a.xadc:
            try:
                self.xadc, self.xadc_t, self.xadc_err = self.read_xadc(), time.time(), None
                if self.xadc["temp_c"] > float(cat.get("max_temp_c", 80)):
                    fault = "die at %.1f C (limit %s C)" % (self.xadc["temp_c"], cat.get("max_temp_c", 80))
            except Exception as e:
                self.xadc_err = type(e).__name__
        h = p.get("health") or {}
        if h.get("type") == "uart_line" and self.tail:
            lim = 3 * float(h.get("within_s", 30))
            ref = max(self.tail.last_match, self.tail.started)
            if time.time() - ref > lim:
                fault = "no status line from %s for %.0f s" % (p["id"], time.time() - ref)
        if fault and p["id"] != cat["fallback"]:
            if time.time() - self.last_fallback > self.a.fallback_gap:
                self.last_fallback = time.time()
                self.note("watchdog: %s -> falling back to %s" % (fault, cat["fallback"]))
                self.start_switch(cat["fallback"], "watchdog", automatic=True)
            else:
                self.watch["fault"] = fault + " (fallback already tried recently)"

    # ---------------------------------------------------------- status
    def status(self, user):
        cat = self.catalog()
        st = self.state()
        holder, q, log = self.queue()
        p = st.get("project")
        pub = []
        for x in cat["projects"]:
            pub.append({k: x.get(k) for k in ("id", "name", "description", "uarts", "owner")})
            pub[-1]["panel"] = (x.get("panel") or {}).get("type")
            pub[-1]["sha256"] = x["sha256"][:12]
        panel = None
        if self.tail and self.tail_for == p:
            panel = dict(lines=[[t, s] for t, s in list(self.tail.lines)[-60:]], err=self.tail.err,
                         last_line=self.tail.last_line or None)
        with self.lock:
            job = dict(self.job) if self.job else None
            last = getattr(self, "last_result", None)
        return dict(
            time=time.time(), user=user, projects=pub, fallback=cat["fallback"],
            loaded=p, since=st.get("since"), by=st.get("by"), result=st.get("result"),
            message=st.get("message"), job=dict(st.get("job") or {}, **job) if job else None,
            last_result={k: v for k, v in last.items() if k != "output"} if last else None,
            stale=self.stale(st, log) if p else None,
            flag=os.path.exists(self.a.flag),
            pvtd_active=self.pvtd_active(),
            holder=holder, queue=q,
            log=[" ".join(x) for x in log[-12:]][::-1],
            history=(st.get("history") or [])[:10],
            watch={k: v for k, v in self.watch.items() if k != "xadc_due"},
            xadc=self.xadc if self.xadc and time.time() - self.xadc_t < 60 else None,
            xadc_t=self.xadc_t or None,
            panel=panel, events=list(self.events)[:10],
            limits=dict(min_gap_s=self.a.min_gap, per_hour=self.a.per_hour),
        )

    def run(self):
        threading.Thread(target=self.watcher, daemon=True).start()
        srv = ThreadingHTTPServer((self.a.bind, self.a.http), make_handler(self))
        srv.daemon_threads = True
        print("fpga-manager on %s:%d" % (self.a.bind, self.a.http), flush=True)
        srv.serve_forever()


# ------------------------------------------------------------------ http
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host"}


def make_handler(m):
    page = os.path.join(m.a.web, "index.html")

    class H(BaseHTTPRequestHandler):
        server_version = "fpga-manager"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, cache="no-store", extra=()):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b)

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj), "application/json", extra=extra)

        def ip(self):
            return self.headers.get("Cf-Connecting-Ip") or self.client_address[0]

        def https(self):
            return "https" in (self.headers.get("Cf-Visitor") or "") or self.headers.get("X-Forwarded-Proto") == "https"

        def token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "fm_session":
                    return v
            return None

        def body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2048 or "application/json" not in (self.headers.get("Content-Type") or ""):
                return None
            # CSRF: JSON content type can't be sent cross-site without CORS; also check Origin
            o = self.headers.get("Origin")
            if o and urlparse(o).netloc != self.headers.get("Host"):
                return None
            try:
                d = json.loads(self.rfile.read(n))
                return d if isinstance(d, dict) else None
            except ValueError:
                return None

        # ------------------------------------------------ routes
        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/manager", "/manager/"):
                if u.path == "/manager":
                    return self._send(301, "", "text/plain", extra=[("Location", "/manager/")])
                with open(page, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8", "no-cache")
            if u.path == "/manager/api/status":
                return self._json(200, m.status(m.session_user(self.token())))
            if u.path == "/manager/healthz":
                return self._send(200, "ok\n", "text/plain")
            if u.path.startswith("/manager/"):
                return self._send(404, "not found\n", "text/plain")
            return self.proxy()

        do_HEAD = do_GET

        def do_POST(self):
            u = urlparse(self.path)
            if not u.path.startswith("/manager/"):
                return self.proxy()
            if u.path == "/manager/api/logout":
                m.logout(self.token())
                return self._json(200, dict(ok=True), extra=[("Set-Cookie", "fm_session=; Path=/manager; Max-Age=0; HttpOnly; SameSite=Strict")])
            req = self.body_json()
            if req is None:
                return self._json(400, dict(error="bad request"))
            if u.path == "/manager/api/login":
                tok, msg = m.login(req.get("user"), str(req.get("password") or ""), self.ip())
                if not tok:
                    return self._json(403, dict(error=msg))
                c = "fm_session=%s; Path=/manager; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (
                    tok, SESSION_S, "; Secure" if self.https() else "")
                return self._json(200, dict(ok=True, user=req.get("user")), extra=[("Set-Cookie", c)])
            if u.path == "/manager/api/switch":
                user = m.session_user(self.token())
                if not user:
                    return self._json(401, dict(error="log in to switch projects"))
                pid = req.get("project")
                if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", pid):
                    return self._json(400, dict(error="bad project"))
                code, msg = m.start_switch(pid, user)
                return self._json(code, dict(ok=code == 202, message=msg))
            return self._send(404, "not found\n", "text/plain")

        def _deny(self):
            self._send(405, "no\n", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

        # ------------------------------------------------ pvtd proxy
        def proxy(self):
            if not m.pvtd_active():
                if urlparse(self.path).path in ("/", "/index.html"):
                    return self._send(302, "", "text/plain", extra=[("Location", "/manager/")])
                return self._json(503, dict(error="the playground isn't loaded right now; see /manager/"))
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                return self._json(413, dict(error="too big"))
            body = self.rfile.read(n) if n else None
            hdr = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            hdr["Cf-Connecting-Ip"] = self.ip()
            try:
                c = http.client.HTTPConnection(m.a.pvtd_host, m.a.pvtd_port, timeout=15)
                c.request(self.command, self.path, body=body, headers=hdr)
                r = c.getresponse()
                data = r.read()
            except OSError:
                return self._json(502, dict(error="pvtd not answering"))
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

    return H


# ------------------------------------------------------------------ main
def passwd(path, user):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", user):
        sys.exit("bad user name")
    pw = getpass.getpass("new password for %s: " % user)
    if len(pw) < 12:
        sys.exit("use at least 12 characters")
    if getpass.getpass("again: ") != pw:
        sys.exit("didn't match")
    try:
        users = json.load(open(path))
    except (OSError, ValueError):
        users = {}
    users[user] = dict(password=hash_pw(pw), set=time.strftime("%F"))
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640), "w") as f:
        json.dump(users, f, indent=1)
    if os.path.exists(path):                   # keep root:fpgamgr 0640 so the daemon can read it
        st = os.stat(path)
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    print("saved %s (restart fpga-manager not needed)" % user)


def main():
    if sys.argv[1:2] == ["passwd"] and len(sys.argv) == 3:
        return passwd(os.environ.get("FPGA_MANAGER_USERS", "/etc/fpga-manager/users.json"), sys.argv[2])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--http", type=int, default=8091)
    ap.add_argument("--web", default=os.path.join(HERE, "web"))
    ap.add_argument("--catalog", default="/etc/fpga-manager/catalog.json")
    ap.add_argument("--users", default="/etc/fpga-manager/users.json")
    ap.add_argument("--state", default="/var/lib/fpga-manager/state.json")
    ap.add_argument("--queue-dir", default="/var/lib/fpga-queue")
    ap.add_argument("--flash-cmd", default="sudo -n /usr/local/sbin/fpga-flash",
                    type=lambda s: s.split())
    ap.add_argument("--flag", default="/run/fpga-manager/foreign-bitstream")
    ap.add_argument("--systemctl", default="/usr/bin/systemctl")
    ap.add_argument("--ofl", default="/usr/local/bin/openFPGALoader")
    ap.add_argument("--firmware", default="/usr/local/share/pvtd/xusb_xp2.hex")
    ap.add_argument("--no-xadc", dest="xadc", action="store_false")
    ap.add_argument("--pvtd-host", default="127.0.0.1")
    ap.add_argument("--pvtd-port", type=int, default=8090)
    ap.add_argument("--min-gap", type=float, default=60, help="s between switches")
    ap.add_argument("--per-hour", type=int, default=12, help="switches per hour")
    ap.add_argument("--fallback-gap", type=float, default=900, help="s between watchdog fallbacks")
    ap.add_argument("--serial-map", type=json.loads, default={}, help="tests: JSON {device: replacement}")
    Manager(ap.parse_args()).run()


if __name__ == "__main__":
    main()
