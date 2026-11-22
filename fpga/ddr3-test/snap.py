#!/usr/bin/env python3
"""Dump the data write calibration read back (state 14 entry), several times.
Expected (write_pattern[63:0]) = 9177298cd0ad51c1."""
import serial, sys, time
s = serial.Serial(sys.argv[1] if len(sys.argv) > 1 else '/dev/cu.usbserial-B002EN6R', 9600, timeout=0.5)
time.sleep(0.2); s.reset_input_buffer()
def q(c):
    s.write(c); b = s.read(1); return b[0] if b else None
for i in range(int(sys.argv[2]) if len(sys.argv) > 2 else 6):
    s.write(b'#'); time.sleep(0.4)
    ok = q(b']')
    v = sum(q(b'[') << (8 * k) for k in range(8))
    print(f"valid={ok} read_lane_data={v:016x}  max_state={q(b'!') - 0x40}")
print("expected        9177298cd0ad51c1")
