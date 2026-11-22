#!/usr/bin/env python3
"""Poll the DQS/DQ IDELAY taps on the board (qmtech_ddr3.v debug commands):
'?' state, '*' last commanded DQS tap, '%'/'&' actual DQS tap lane 0/1
(CNTVALUEOUT), '+' actual DQ0 tap. If commanded moves and actual doesn't,
the VAR_LOAD tap loads aren't taking effect."""
import collections, sys, time
import serial
port = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbserial-B002EN6R"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 20
s = serial.Serial(port, 9600, timeout=0.3); time.sleep(0.2); s.reset_input_buffer()
def q(c):
    s.write(c); b = s.read(1); return b[0] if b else None
pairs = collections.Counter(); rows = []
t0 = time.time()
while time.time() - t0 < secs:
    st, cmd, a0, a1, dq = q(b'?'), q(b'*'), q(b'%'), q(b'&'), q(b'+')
    st = st - 0x40 if st is not None else None
    rows.append((round(time.time() - t0, 1), st, cmd, a0, a1, dq)); pairs[(cmd, a0, a1, dq)] += 1
print("t(s) state cmd dqs0 dqs1 dq0  (first 12 samples)")
for r in rows[:12]: print(*r)
print(f"\n{len(rows)} samples; distinct commanded={sorted({r[2] for r in rows})}")
print(f"distinct actual dqs0={sorted({r[3] for r in rows})} dqs1={sorted({r[4] for r in rows})} dq0={sorted({r[5] for r in rows})}")
print("most common (cmd, dqs0, dqs1, dq0):", pairs.most_common(6))
