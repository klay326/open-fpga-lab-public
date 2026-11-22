# JTAG cable identity

Not plugged in at the moment this bundle was built, so this is the last
confirmed ID from this session's logs:

```
Bus 001 Device NNN: ID 03fd:0008 Xilinx, Inc. Platform Cable USB II
```

- VID `03fd` / PID `0008` -- "Xilinx, Inc. Platform Cable USB II"
- This is a **clone**, not a genuine Xilinx cable. It has a confirmed,
  reproducible protocol incompatibility with `openFPGALoader`, `xc3sprog`,
  and `urjtag` -- all three fail identically at the same low-level JTAG
  shift operation. Only Vivado Lab Edition's own Tcl interface
  (`vivado_lab -mode batch -source flash.tcl` with
  `connect_hw_server -allow_non_jtag`) reliably works with it.
- The PID can also show up as `0007`, `0009`, `000d`, `000f`, `0013`, or
  `0015` depending on which Xilinx cable variant's firmware is active --
  see `udev-rules/52-xilinx-pcusb.rules`, which grants `MODE="666"` to all
  of them.
