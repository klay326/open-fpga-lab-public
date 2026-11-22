#!/usr/bin/env python3
"""DDR3 round-trip test for qmtech_ddr3.bit over the JP5 UART (9600 8N1).

Protocol (qmtech_ddr3.v): 'a'..'z' writes that byte to DDR3 at address ~byte,
'A'..'Z' reads the matching lowercase address back. Every completed request
(write or read) returns one byte. A read that returns the right lowercase
letter proves the controller calibrated and the data survived DDR3.

Usage: python3 test.py [/dev/cu.usbserial-XXXX] [rounds]
"""
import glob
import random
import sys
import time

import serial


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else (glob.glob("/dev/cu.usbserial-*") + ["/dev/ttyUSB0"])[0]
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    s = serial.Serial(port, 9600, timeout=1.0)
    time.sleep(0.2)
    s.reset_input_buffer()

    letters = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    ok = bad = timeouts = 0
    for r in range(rounds):
        order = letters[:]
        random.shuffle(order)
        for ch in order:  # writes
            s.write(ch.encode())
            if len(s.read(1)) != 1:
                timeouts += 1
        random.shuffle(order)
        for ch in order:  # reads
            s.write(ch.upper().encode())
            got = s.read(1)
            if len(got) != 1:
                timeouts += 1
                print(f"  read {ch.upper()}: TIMEOUT")
            elif got.decode(errors="replace") == ch:
                ok += 1
            else:
                bad += 1
                print(f"  read {ch.upper()}: expected {ch!r}, got {got!r}")
        print(f"round {r + 1}: ok={ok} bad={bad} timeouts={timeouts}")

    total = rounds * len(letters)
    print(f"\n{'PASS' if ok == total else 'FAIL'}: {ok}/{total} reads correct, "
          f"{bad} wrong, {timeouts} timeouts")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
