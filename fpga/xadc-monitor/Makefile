FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = xadc_monitor
CHIPDB  = ${KINTEX7_CHIPDB}

# The ring-oscillator loops are intentional combinational loops and their
# toggle/counter FFs are clocked from fabric nets; neither has a meaningful
# timing constraint.
PNR_ARGS = --timing-allow-fail

include ../openXC7.mk
