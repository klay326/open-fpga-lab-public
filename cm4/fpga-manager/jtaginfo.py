"""Read-only JTAG facts about the FPGA, shared by fpga-flash and fpga_manager.py.

Everything here only READS: --detect (IDCODE), --read-dna, and
--read-register STAT, which sends a sync word plus one type-1 read packet over
CFG_IN and shifts the answer out of CFG_OUT. Nothing is written to the
configuration logic, so a running design is not disturbed. The caller must hold
the board (fpga-queue) or otherwise own the cable, as for any JTAG access.
"""
import os, re, subprocess, time

# UG470 table 5-25, 7-series STAT register
STAT_BITS = [
    ("crc_error", 0, 1), ("part_secured", 1, 1), ("mmcm_lock", 2, 1), ("dci_match", 3, 1),
    ("eos", 4, 1), ("gts_cfg_b", 5, 1), ("gwe", 6, 1), ("ghigh_b", 7, 1), ("mode", 8, 3),
    ("init_complete", 11, 1), ("init_b", 12, 1), ("release_done", 13, 1), ("done", 14, 1),
    ("id_error", 15, 1), ("dec_error", 16, 1), ("xadc_over_temp", 17, 1),
    ("startup_state", 18, 3), ("bus_width", 25, 2),
]


def decode_stat(raw):
    return {name: (raw >> off) & ((1 << n) - 1) for name, off, n in STAT_BITS}


def _run(base, args, timeout=30):
    r = subprocess.run(base + args, capture_output=True, text=True, timeout=timeout)
    return r.stdout + r.stderr


def read(ofl, firmware, freq=1500000):
    """Returns a dict (keys missing when a read failed) plus "t" and "errors"."""
    base = [ofl, "-c", "xilinxPlatformCableUsb"]
    if firmware and os.path.exists(firmware):
        base += ["--probe-firmware", firmware]
    base += ["--freq", str(freq)]
    d, errs = {"t": time.time()}, []
    try:
        out = _run(base, ["--detect"])
        m = re.search(r"idcode\s+(0x[0-9a-fA-F]+)", out)
        if m:
            d["idcode"] = m.group(1).lower()
        for k in ("manufacturer", "family", "model"):
            m = re.search(r"^\s*%s\s+(\S+)" % k, out, re.M)
            if m:
                d[k] = m.group(1)
        if "idcode" not in d:
            errs.append("detect: " + (out.strip().splitlines() or ["no output"])[-1][:120])
        out = _run(base, ["--read-dna"])
        m = re.search(r"dna[^\n]*?(0x[0-9a-fA-F]{8,})", out, re.I)
        if m:
            d["dna"] = m.group(1).lower()
        out = _run(base, ["--read-register", "STAT"])
        m = re.search(r"raw value:\s*(0x[0-9a-fA-F]+)", out)
        if m:
            raw = int(m.group(1), 16)
            d["stat_raw"] = "0x%08x" % raw
            d["stat"] = decode_stat(raw)
        else:
            errs.append("STAT: " + (out.strip().splitlines() or ["no output"])[-1][:120])
    except (OSError, subprocess.TimeoutExpired) as e:
        errs.append(type(e).__name__)
    d["errors"] = errs
    return d
