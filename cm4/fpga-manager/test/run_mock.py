#!/usr/bin/env python3
"""Off-board test of fpga-manager + fpga-flash + fpga-queue with a fake
openFPGALoader, a fake systemctl, a fake pvtd and a pty as the CM4 UART.

  python3 cm4/fpga-manager/test/run_mock.py        (from the repo root, Mac or Pi)

Nothing here touches JTAG, a real UART or pvtd.
"""
import hashlib, http.client, http.server, json, os, pty, secrets, shutil, subprocess, sys
import tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
FM = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(FM))
PORT, PVTD_PORT = 18091, 18090
T = tempfile.mkdtemp(prefix="fm-test-")
fails = []


def ok(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        fails.append(what)


# ---------------------------------------------------------------- fixtures
for d in ("store", "run", "queue", "etc"):
    os.makedirs(os.path.join(T, d))
cat = json.load(open(os.path.join(FM, "catalog.json")))
os.makedirs(os.path.join(T, "pvtd-share"))
for p in cat["projects"]:                      # fake bitstreams, real hashes of them
    b = secrets.token_bytes(4096)
    if p.get("file"):                          # the playground: pvtd's own file, trusted by owner
        p["file"] = os.path.join(T, "pvtd-share", "playground.bit")
        open(p["file"], "wb").write(b)
        continue
    open(os.path.join(T, "store", p["id"] + ".bit"), "wb").write(b)
    p["sha256"] = hashlib.sha256(b).hexdigest()
    if p.get("health", {}).get("type") == "uart_line":
        p["health"]["within_s"] = 2
cat["max_temp_c"] = 80
json.dump(cat, open(os.path.join(T, "etc", "catalog.json"), "w"))

master, slave = pty.openpty()
slave_name = os.ttyname(slave)
conf = dict(catalog=os.path.join(T, "etc", "catalog.json"), store=os.path.join(T, "store"),
            state=os.path.join(T, "state.json"), run_dir=os.path.join(T, "run"),
            ofl=os.path.join(HERE, "fake-ofl"), firmware="/nonexistent", freqs=[6000000, 1500000],
            queue=os.path.join(REPO, "tools", "fpga-queue"), systemctl=os.path.join(HERE, "fake-systemctl"),
            serial_map={"/dev/serial0": slave_name}, trusted_uid=os.getuid(), trust_root=T, lib=FM)
json.dump(conf, open(os.path.join(T, "conf.json"), "w"))
pw = secrets.token_urlsafe(12)
sys.path.insert(0, FM)
import fpga_manager                           # noqa: E402
json.dump({"tester": {"password": fpga_manager.hash_pw(pw)}}, open(os.path.join(T, "etc", "users.json"), "w"))
open(os.path.join(T, "pvtd.active"), "w").close()           # pvtd "running", playground "loaded"
json.dump({"project": "playground", "since": time.time() - 3600, "by": "install", "history": []},
          open(os.path.join(T, "state.json"), "w"))

env = dict(os.environ, MOCK_DIR=T, FPGA_MANAGER_CONF=os.path.join(T, "conf.json"),
           FPGA_QUEUE_DIR=os.path.join(T, "queue"), MOCK_FLASH_S="0.5")


def feeder():                                  # the "FPGA": pvt-lab prints a status line
    while True:
        time.sleep(0.3)
        try:
            cur = open(os.path.join(T, "loaded")).read().strip()
        except OSError:
            cur = ""
        if cur == "pvt-lab" and not os.path.exists(os.path.join(T, "mute")):
            os.write(master, b"R 0BB8 0BB9 0BBA 0BBB 0BBC H 00 M 0\r\n")
        elif cur == "litex-cm4-uart":
            os.write(master, b"litex> \r\n")


threading.Thread(target=feeder, daemon=True).start()


class FakePvtd(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps(dict(path=self.path, ip=self.headers.get("Cf-Connecting-Ip"))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", PVTD_PORT), FakePvtd).serve_forever,
                 daemon=True).start()

daemon = subprocess.Popen(
    [sys.executable, os.path.join(FM, "fpga_manager.py"), "--bind", "127.0.0.1", "--http", str(PORT),
     "--catalog", conf["catalog"], "--users", os.path.join(T, "etc", "users.json"), "--state", conf["state"],
     "--queue-dir", os.path.join(T, "queue"), "--flash-cmd", "%s %s" % (sys.executable, os.path.join(FM, "fpga-flash")),
     "--systemctl", conf["systemctl"], "--ofl", conf["ofl"], "--firmware", "/nonexistent",
     "--pvtd-port", str(PVTD_PORT), "--min-gap", "1", "--per-hour", "100", "--fallback-gap", "1",
     "--serial-map", json.dumps(conf["serial_map"]), "--flag", os.path.join(T, "run", "foreign-bitstream")],
    env=env, stdout=open(os.path.join(T, "daemon.log"), "w"), stderr=subprocess.STDOUT)


def req(method, path, body=None, cookie=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = dict(headers or {})
    if body is not None:
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        data = json.loads(data)
    except ValueError:
        pass
    return r.status, data, r.getheader("Set-Cookie")


def status():
    return req("GET", "/manager/api/status")[1]


def wait_idle(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status()
        if not s["job"]:
            return s
        time.sleep(0.3)
    return status()


def queue(*a):
    return subprocess.run([conf["queue"]] + list(a), env=env, capture_output=True, text=True).stdout


if "--serve" in sys.argv:                     # leave it running for a look at the page
    for _ in range(50):
        try:
            req("GET", "/manager/healthz"); break
        except OSError:
            time.sleep(0.1)
    ck = req("POST", "/manager/api/login", {"user": "tester", "password": pw})[2].split(";")[0]
    req("POST", "/manager/api/switch", {"project": "pvt-lab"}, cookie=ck)
    print("http://127.0.0.1:%d/manager/" % PORT, flush=True)
    daemon.wait()
    sys.exit()
try:
    for _ in range(50):
        try:
            req("GET", "/manager/healthz")
            break
        except OSError:
            time.sleep(0.1)
    s = status()
    ok(s["loaded"] == "playground" and len(s["projects"]) == 5 and s["user"] is None, "public status lists 5 projects")
    ok(req("GET", "/manager/")[0] == 200, "page served")
    code, d, _ = req("GET", "/api/now")
    ok(code == 200 and d.get("path") == "/api/now" and d.get("ip") == "127.0.0.1", "proxied to pvtd while it runs")
    ok(req("POST", "/manager/api/switch", {"project": "trng"})[0] == 401, "switch needs login")
    ok(req("POST", "/manager/api/login", {"user": "tester", "password": "nope"})[0] == 403, "wrong password refused")
    ok(req("POST", "/manager/api/login", {"user": "tester", "password": pw},
           headers={"Origin": "https://evil.example"})[0] == 400, "cross-origin login refused")
    code, d, setc = req("POST", "/manager/api/login", {"user": "tester", "password": pw})
    ok(code == 200 and setc and "HttpOnly" in setc, "login sets an HttpOnly cookie")
    ck = setc.split(";")[0]
    ok(status()["user"] is None and req("GET", "/manager/api/status", cookie=ck)[1]["user"] == "tester", "session cookie works")

    # 1. playground -> trng
    code, d, _ = req("POST", "/manager/api/switch", {"project": "trng"}, cookie=ck)
    ok(code == 202, "switch to trng accepted")
    ok(req("POST", "/manager/api/switch", {"project": "ddr3-test"}, cookie=ck)[0] in (409, 429), "second switch while one runs refused")
    s = wait_idle()
    ok(s["loaded"] == "trng" and not s["pvtd_active"], "trng loaded, pvtd stopped")
    ok(os.path.exists(os.path.join(T, "run", "foreign-bitstream")), "pvtd kept-down flag set")
    log = open(os.path.join(T, "queue", "log")).read()
    ok("TAKE\tweb-flasher" in log and "loaded trng" in log, "queue taken and released with a note")
    ok(not open(os.path.join(T, "queue", "holder")).read() if os.path.exists(os.path.join(T, "queue", "holder")) else True, "queue free after")
    time.sleep(3)
    ok(status()["xadc"] and abs(status()["xadc"]["temp_c"] - 40.5) < 0.01, "XADC watched while trng is loaded")
    j = status()["jtag"]
    ok(j and j["idcode"] == "0x3651093" and j["model"] == "xc7k325t"
       and j["stat"]["done"] == 1 and j["stat"]["eos"] == 1 and j["stat"]["crc_error"] == 0, "JTAG status read after flash and decoded")
    time.sleep(10.5)
    ok(any(h[4] == "manager" and abs(h[1] - 40.5) < 0.01 for h in status()["hist"]), "temperature history from the watchdog")
    code, d, _ = req("GET", "/")
    ok(code == 302, "/ redirects to the manager while pvtd is down")

    # 2. hash tamper
    time.sleep(1.1)
    open(os.path.join(T, "store", "ddr3-test.bit"), "ab").write(b"x")
    req("POST", "/manager/api/switch", {"project": "ddr3-test"}, cookie=ck)
    s = wait_idle()
    ok(s["loaded"] == "trng" and s["last_result"]["rc"] == 2, "tampered bitstream refused, board untouched")

    # 3. board held by someone else
    time.sleep(1.1)
    queue("join", "someone", "test"); queue("take", "someone")
    code, d, _ = req("POST", "/manager/api/switch", {"project": "pvt-lab"}, cookie=ck)
    ok(code == 409 and "someone" in d.get("message", ""), "busy board refused (409)")
    time.sleep(2.5)
    ok(not status()["watch"]["active"], "watchdog idle while someone holds the board")
    queue("release", "someone", "test done, trng still loaded")
    ok("someone" in (status().get("stale") or {}).get("by", ""), "other holder after our switch shown as stale")

    # 4. trng -> pvt-lab, then pvt-lab goes quiet -> watchdog fallback
    time.sleep(1.1)
    code, d, _ = req("POST", "/manager/api/switch", {"project": "pvt-lab"}, cookie=ck)
    s = wait_idle()
    ok(s["loaded"] == "pvt-lab" and s["result"] == "ok", "pvt-lab loaded and its status line seen")
    time.sleep(4)
    s = status()
    ok(s["panel"] and any("0BB8" in l[1] for l in s["panel"]["lines"]), "pvt-lab UART tail on the page")
    open(os.path.join(T, "mute"), "w").close()
    t0 = time.time()
    while time.time() - t0 < 25 and status()["loaded"] != "playground":
        time.sleep(0.5)
    s = wait_idle()
    ok(s["loaded"] == "playground" and s["by"] == "watchdog" and s["pvtd_active"], "silent pvt-lab -> watchdog fell back to playground")
    ok(not os.path.exists(os.path.join(T, "run", "foreign-bitstream")), "flag cleared, pvtd started")
    os.unlink(os.path.join(T, "mute"))

    # 5. failed flash -> fallback
    time.sleep(1.1)
    open(os.path.join(T, "fail-litex-cm4-uart"), "w").close()
    req("POST", "/manager/api/switch", {"project": "litex-cm4-uart"}, cookie=ck)
    s = wait_idle()
    ok(s["loaded"] == "playground" and s["result"] == "fallback" and s["pvtd_active"], "failed flash fell back to playground")
    os.unlink(os.path.join(T, "fail-litex-cm4-uart"))

    # 6. litex, then overheat -> fallback
    time.sleep(1.1)
    req("POST", "/manager/api/switch", {"project": "litex-cm4-uart"}, cookie=ck)
    s = wait_idle()
    ok(s["loaded"] == "litex-cm4-uart", "litex loaded")
    time.sleep(3)
    ok(any("litex>" in l[1] for l in (status()["panel"] or {}).get("lines", [])), "litex console tail on the page")
    open(os.path.join(T, "temp"), "w").write("85.0")
    t0 = time.time()
    while time.time() - t0 < 25 and status()["loaded"] != "playground":
        time.sleep(0.5)
    s = wait_idle()
    ok(s["loaded"] == "playground" and s["by"] == "watchdog", "85 C -> fell back to playground")
    os.unlink(os.path.join(T, "temp"))

    # 6b. a group-writable playground file is refused
    os.chmod(os.path.join(T, "pvtd-share", "playground.bit"), 0o664)
    time.sleep(1.1)
    req("POST", "/manager/api/switch", {"project": "playground"}, cookie=ck)
    s = wait_idle()
    ok(s["last_result"]["rc"] == 2, "writable playground.bit refused")
    os.chmod(os.path.join(T, "pvtd-share", "playground.bit"), 0o644)

    # 6c. a slot reserved for web-flasher in the queue is usable; someone else ahead is not
    time.sleep(1.1)
    queue("join", "someone", "x"); queue("join", "web-flasher", "reserved")
    ok(req("POST", "/manager/api/switch", {"project": "trng"}, cookie=ck)[0] == 409, "refused while someone waits ahead of web-flasher")
    queue("leave", "someone")
    code, d, _ = req("POST", "/manager/api/switch", {"project": "trng"}, cookie=ck)
    s = wait_idle()
    ok(code == 202 and s["loaded"] == "trng" and not s["queue"], "reserved web-flasher slot used")

    # 7. rate limit
    time.sleep(1.1)
    req("POST", "/manager/api/switch", {"project": "trng"}, cookie=ck)
    code, d, _ = req("POST", "/manager/api/switch", {"project": "playground"}, cookie=ck)
    ok(code in (409, 429), "back-to-back switch rate-limited")
    wait_idle()
    time.sleep(1.1)
    req("POST", "/manager/api/switch", {"project": "playground"}, cookie=ck)
    s = wait_idle()
    ok(s["loaded"] == "playground" and s["pvtd_active"], "back to playground by hand")

    # 8. login brute force
    for _ in range(5):
        req("POST", "/manager/api/login", {"user": "tester", "password": "bad"})
    code, d, _ = req("POST", "/manager/api/login", {"user": "tester", "password": pw})
    ok(code == 403 and "too many" in d.get("error", ""), "login locked after 5 failures")
    ok(req("POST", "/manager/api/logout", {}, cookie=ck)[0] == 200 and
       req("POST", "/manager/api/switch", {"project": "trng"}, cookie=ck)[0] == 401, "logout ends the session")
    ok(len(status()["history"]) >= 5, "history recorded")
finally:
    daemon.terminate()
    print("\n%d failure(s). Logs in %s" % (len(fails), T))
    if not fails:
        shutil.rmtree(T)
sys.exit(1 if fails else 0)
