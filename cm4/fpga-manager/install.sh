#!/bin/bash
# Install / update fpga-manager on the CM4. Run from a staged copy (publish.sh
# does this for you):  sudo ./install.sh
#   MIGRATE_QUEUE=<dir>  first install only: copy that queue state (holder,
#                        queue, log) into /var/lib/fpga-queue
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }

getent group fpgaq >/dev/null || groupadd --system fpgaq
id fpgamgr >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin fpgamgr
usermod -aG dialout,plugdev,fpgaq fpgamgr
[ -n "${SUDO_USER:-}" ] && usermod -aG fpgaq "$SUDO_USER" 2>/dev/null || true

# the shared board queue
install -m 755 "$HERE/fpga-queue" /usr/local/bin/fpga-queue
install -d -o root -g fpgaq -m 2775 /var/lib/fpga-queue
if [ -n "${MIGRATE_QUEUE:-}" ]; then
    for f in holder queue log; do
        if [ -f "$MIGRATE_QUEUE/$f" ]; then install -o root -g fpgaq -m 664 "$MIGRATE_QUEUE/$f" /var/lib/fpga-queue/$f; else rm -f /var/lib/fpga-queue/$f; fi
    done
    echo "queue state migrated from $MIGRATE_QUEUE"
fi
touch /var/lib/fpga-queue/{queue,log,.flock}; chgrp fpgaq /var/lib/fpga-queue/{queue,log,.flock}; chmod 664 /var/lib/fpga-queue/{queue,log,.flock}

# catalog + bitstreams (hash-checked again here)
install -d -m 755 /etc/fpga-manager /var/lib/fpga-manager /var/lib/fpga-manager/bitstreams
install -m 644 "$HERE/catalog.json" /etc/fpga-manager/catalog.json
python3 -I - "$HERE/bitstreams" /var/lib/fpga-manager/bitstreams /etc/fpga-manager/catalog.json <<'PY'
import hashlib, json, os, shutil, sys
src, dst, cat = sys.argv[1:4]
for p in json.load(open(cat))["projects"]:
    if p.get("file"):
        print("ok      %-16s uses %s (%s)" % (p["id"], p["file"], "present" if os.path.exists(p["file"]) else "MISSING")); continue
    s, d = os.path.join(src, p["id"] + ".bit"), os.path.join(dst, p["id"] + ".bit")
    for f in (s, d):
        if os.path.exists(f) and hashlib.sha256(open(f, "rb").read()).hexdigest() == p["sha256"]:
            if f == s:
                shutil.copyfile(s, d + ".tmp"); os.chmod(d + ".tmp", 0o644); os.replace(d + ".tmp", d)
            print("ok      %-16s %s" % (p["id"], p["sha256"][:12])); break
    else:
        print("MISSING %-16s (no bitstream with sha %s)" % (p["id"], p["sha256"][:12]))
PY
[ -f /var/lib/fpga-manager/state.json ] || echo '{"project": null, "history": []}' > /var/lib/fpga-manager/state.json
chmod 644 /var/lib/fpga-manager/state.json
touch /etc/fpga-manager/users.json; chown root:fpgamgr /etc/fpga-manager/users.json; chmod 640 /etc/fpga-manager/users.json
[ -s /etc/fpga-manager/users.json ] || echo '{}' > /etc/fpga-manager/users.json

# code
install -m 755 "$HERE/fpga-flash" /usr/local/sbin/fpga-flash
install -d /opt/fpga-manager/web
install -m 644 "$HERE/fpga_manager.py" /opt/fpga-manager/fpga_manager.py
install -m 644 "$HERE/jtaginfo.py" /opt/fpga-manager/jtaginfo.py
install -m 644 "$HERE/web/index.html" /opt/fpga-manager/web/index.html
install -m 440 "$HERE/systemd/sudoers" /etc/sudoers.d/fpga-manager
visudo -cf /etc/sudoers.d/fpga-manager >/dev/null
install -m 644 "$HERE/systemd/fpga-manager.tmpfiles" /etc/tmpfiles.d/fpga-manager.conf
systemd-tmpfiles --create /etc/tmpfiles.d/fpga-manager.conf
install -d /etc/systemd/system/pvtd.service.d
install -m 644 "$HERE/systemd/pvtd-dropin.conf" /etc/systemd/system/pvtd.service.d/fpga-manager.conf
install -m 644 "$HERE/systemd/fpga-manager.service" /etc/systemd/system/fpga-manager.service
systemctl daemon-reload
systemctl enable fpga-manager
systemctl restart fpga-manager
sleep 1
systemctl --no-pager --lines=3 status fpga-manager
[ "$(cat /etc/fpga-manager/users.json)" != "{}" ] || echo "no users yet: sudo python3 /opt/fpga-manager/fpga_manager.py passwd <name>  (as root: FPGA_MANAGER_USERS defaults to /etc/fpga-manager/users.json)"
