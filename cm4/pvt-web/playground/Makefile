FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = playground
CHIPDB  = ${KINTEX7_CHIPDB}
# pvt_lab.v, ro_monitor.v, tdc.v, dmon.v, scope.v, ro_grid.v(+_inst.vh): copies of fpga/pvt-lab (v6) so its
# ongoing work can't move this design's RO calibration; trng.v used in place.
ADDITIONAL_SOURCES = pvt_lab.v ro_monitor.v tdc.v dmon.v scope.v ro_grid.v ../../../fpga/trng/trng.v

# Ring oscillators are intentional combinational loops.
PNR_ARGS = --timing-allow-fail

include ../../../fpga/openXC7.mk
