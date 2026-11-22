#!/usr/bin/env python3
"""FASM fix-ups for nextpnr-xilinx output on the QMTech K7 DDR3 bank (HP bank 34).

1. VREF: nextpnr ignores INTERNAL_VREF and always emits VREF.V_675_MV (SSTL135).
   This board is SSTL15 -> 0.75 V, as Vivado's MIG sets.
2. HP-bank glue bits: replace nextpnr's OBUF/IBUF_HP_BANK_GLUE on bank-34 IOBs with
   exactly what Vivado's working MIG bitstream sets on the same pins
   (vivado-bank34-glue.txt, decoded with bit2fasm). nextpnr sets OBUF glue on
   every output; Vivado sets it only on the LVCMOS15 RESET# pin.
3. LVCMOS15 drive: nextpnr emits only the shared LVCMOS15_LVCMOS18.DRIVE.I12_I16..
   feature, which leaves the drive partly configured (it even forces 39_23 low,
   which DRIVE.I12 needs high). Emit the full LVCMOS15.DRIVE.I12, as Vivado does.

usage: fasm_fixup.py in.fasm vivado-bank34-glue.txt > out.fasm
"""
import sys

src, gluef = sys.argv[1], sys.argv[2]
glue = set(l.strip() for l in open(gluef) if l.strip())
out = []
for l in open(src):
    s = l.strip()
    if s.endswith('.VREF.V_675_MV'):
        s = s[: -len('V_675_MV')] + 'V_750_MV'
    if s.startswith('RIOB18_') and s.endswith('_HP_BANK_GLUE'):
        continue  # re-added from the Vivado reference below
    if s.endswith('.LVCMOS15_LVCMOS18.DRIVE.I12_I16_I2_I4_I6_I8'):
        s = s.replace('.LVCMOS15_LVCMOS18.DRIVE.I12_I16_I2_I4_I6_I8', '.LVCMOS15.DRIVE.I12')
    out.append(s)
used = {'.'.join(s.split('.')[:2]) for s in out if s.startswith('RIOB18_')}
out += sorted(g for g in glue if '.'.join(g.split('.')[:2]) in used)
sys.stdout.write('\n'.join(out) + '\n')
