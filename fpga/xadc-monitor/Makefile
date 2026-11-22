FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = xadc_monitor
CHIPDB  = ${KINTEX7_CHIPDB}

# `make XADC_FABRIC=1`: XADC read over its DRP from fabric and sent on the UART
# (needs the XADC-capable chipdb and nextpnr-xilinx, see README.md). Its outputs
# are xadc_monitor_fabric.*, so the default JTAG-XADC build is left alone.
ifneq ($(XADC_FABRIC),)
PROJECT       = xadc_monitor_fabric
TOP_VERILOG   = xadc_monitor.v
TOP_MODULE    = xadc_monitor
XDC           = xadc_monitor.xdc
YOSYS_DEFINES = XADC_FABRIC
endif

# The ring-oscillator loops are intentional combinational loops and their
# toggle/counter FFs are clocked from fabric nets; neither has a meaningful
# timing constraint.
PNR_ARGS = --timing-allow-fail

include ../openXC7.mk
