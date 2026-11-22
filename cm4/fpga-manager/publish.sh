#!/bin/bash
# From the Mac: check every catalogued bitstream's hash, stage code + bitstreams
# on the CM4 and run install.sh there.
#   cm4/fpga-manager/publish.sh            check, stage, install (changed sources are skipped)
#   cm4/fpga-manager/publish.sh --rehash   write the current sources' hashes into catalog.json first
# Extra env for install.sh (e.g. MIGRATE_QUEUE=...) is passed through.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
# bitstreams are gitignored: take them from the main checkout, not a worktree
BIT_REPO="${BIT_REPO:-$(git -C "$REPO" worktree list --porcelain | sed -n '1s/^worktree //p')}"
PI="${PI:?set PI=<user>@<cm4-host>}"; KEY="${KEY:?set KEY=<path to the ssh key for the CM4>}"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
python3 -I - "$BIT_REPO" "$HERE/catalog.json" "$STAGE" "${1:-}" <<'PY'
import hashlib, json, os, shutil, sys
repo, catp, stage, mode = sys.argv[1:5]
cat = json.load(open(catp)); bad = 0
os.makedirs(os.path.join(stage, "bitstreams"))
for p in cat["projects"]:
    if p.get("file"):
        print("skip    %-16s flashed from %s on the Pi (pvt-web installs it)" % (p["id"], p["file"])); continue
    src = os.path.join(repo, p["source"])
    h = hashlib.sha256(open(src, "rb").read()).hexdigest()
    if h != p["sha256"]:
        if mode == "--rehash":
            print("rehash  %-16s %s -> %s" % (p["id"], p["sha256"][:12], h[:12])); p["sha256"] = h
        else:
            # not staged: the Pi keeps its installed copy if that still matches the catalog
            print("CHANGED %-16s %s is %s, catalog says %s: not updated (use --rehash to take the new build)" % (p["id"], p["source"], h[:12], p["sha256"][:12])); bad += 1; continue
    shutil.copyfile(src, os.path.join(stage, "bitstreams", p["id"] + ".bit"))
if mode == "--rehash":
    json.dump(cat, open(catp, "w"), indent=2); open(catp, "a").write("\n")
if bad:
    print("warning: %d source bitstream(s) differ from the catalog; the Pi keeps the catalogued builds" % bad)
PY
cp -R "$HERE"/{catalog.json,fpga-flash,fpga_manager.py,jtaginfo.py,install.sh,web,systemd} "$STAGE/"
cp "$REPO/tools/fpga-queue" "$STAGE/"
rsync -a --delete -e "ssh -i $KEY" "$STAGE/" "$PI:fpga-manager-stage/"
ssh -i "$KEY" "$PI" "cd fpga-manager-stage && sudo ${MIGRATE_QUEUE:+MIGRATE_QUEUE=$MIGRATE_QUEUE} ./install.sh"
