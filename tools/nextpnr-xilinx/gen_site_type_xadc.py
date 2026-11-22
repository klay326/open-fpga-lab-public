#!/usr/bin/env python3
"""Generate nextpnr-xilinx-meta site_type_XADC.json from prjxray-db.

The XADC site holds one bel, XADC (type XADC_XADC), whose pins map 1:1 onto
the site pins. The TEST* pins are factory-test only (not on the XADC
primitive) and are left out. The DCLKINV / CONVSTCLKINV site muxes are not
modelled: prjxray-db has no MONITOR segbits, so their state can't be set.

usage: gen_site_type_xadc.py <prjxray-db root> <meta root>
"""
import json, os, re, sys

def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]

def main(db_root, meta_root):
    for family in sorted(os.listdir(meta_root)):
        src = os.path.join(db_root, family, "site_type_XADC.json")
        dst_dir = os.path.join(meta_root, family)
        if not os.path.isdir(dst_dir) or not os.path.exists(src):
            continue
        with open(src) as f:
            st = json.load(f)
        assert st["type"] == "XADC"
        pins = {p: d["direction"] for p, d in st["site_pins"].items()
                if not p.startswith("TEST")}
        dirmap = {"IN": "INPUT", "OUT": "OUTPUT"}
        bel_pins, site_pins = {}, {}
        for p in sorted(pins, key=natkey):
            d = dirmap[pins[p]]
            bel_pins[p] = {"dir": d, "wire": p}
            site_pins[p] = {"primary": p, "wire": p, "dir": d}
        out = {"XADC": {
            "bels": {"XADC": {"type": "XADC_XADC", "class": "BEL", "pins": bel_pins}},
            "pips": [],
            "pins": site_pins}}
        with open(os.path.join(dst_dir, "site_type_XADC.json"), "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
        print(family, len(site_pins), "pins")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
