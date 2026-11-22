FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = pvt_lab
CHIPDB  = ${KINTEX7_CHIPDB}
ADDITIONAL_SOURCES = ro_monitor.v tdc.v dmon.v

# Ring oscillators are intentional combinational loops on fabric clocks.
PNR_ARGS = --timing-allow-fail

include ../openXC7.mk
