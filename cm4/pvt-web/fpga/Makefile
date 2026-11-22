FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = pvt_web
CHIPDB  = ${KINTEX7_CHIPDB}
ADDITIONAL_SOURCES = ../../../fpga/xadc-monitor/xadc_monitor.v

# Ring oscillators are intentional combinational loops (see xadc-monitor).
PNR_ARGS = --timing-allow-fail

include ../../../fpga/openXC7.mk
