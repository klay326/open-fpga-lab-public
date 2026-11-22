#!/bin/bash
# Simulate init + calibration of fpga/ddr3-test's controller/PHY against
# Micron's DDR3 model (Icarus). Micron's model and the Xilinx primitive models
# come from UberDDR3 (not redistributed here); fetched at a pinned commit.
#   ./run.sh [sim_time_us]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
UBER=build/UberDDR3; UBER_COMMIT=79d8fd3
[ -d "$UBER" ] || git clone -q https://github.com/AngeloJacobo/UberDDR3.git "$UBER"
git -C "$UBER" checkout -q "$UBER_COMMIT"
mkdir -p build/inc
# x16 (TWO_LANES_x8), 8Gb parameters (only density shipped), DDR3-1600 bin (CL5 legal at 3 ns)
sed -e 's|^`define EIGHT_LANES_x8|//`define EIGHT_LANES_x8|' -e 's|^//`define TWO_LANES_x8|`define TWO_LANES_x8|' \
    "$UBER/testbench/sim_defines.vh" > build/inc/sim_defines.vh
cp "$UBER/testbench/8192Mb_ddr3_parameters.vh" build/inc/
# BUS=<ps>: board trace delay, applied by the model on every DRAM pin each way
perl -pi -e "s/(parameter BUS_DELAY\s*=\s*)\d+/\${1}${BUS:-0}/" build/inc/8192Mb_ddr3_parameters.vh
# Primitive models; the ISERDES model is extended to accept the OFB loopback
# (OFB_USED="TRUE", IOBDELAY="NONE") the PHY uses for bitslip training.
mkdir -p build/models; cp "$UBER"/testbench/models/*.v build/models/
perl -0pi -e 's/if\(IOBDELAY != "IFD"\) begin.*?\$stop;\s*end//s; s/if\(OFB_USED != "FALSE"\) begin.*?\$stop;\s*end//s;
  s/(parameter OFB_USED = "FALSE";)/$1\n  wire din_sel = (OFB_USED == "TRUE") ? OFB : DDLY;/; s/<= DDLY;/<= din_sel;/g' \
  build/models/ISERDESE2_model.v
# board RTL with Xilinx primitives renamed to the behavioural models
perl -pe 's/\b(OSERDESE2|ISERDESE2|IDELAYE2|ODELAYE2|IOBUFDS_DCIEN|IOBUF_DCIEN|IOBUFDS|IOBUF|OBUFDS|OBUF|IDELAYCTRL)\b(?=\s*(#|[A-Za-z_]))/$1_model/g' \
    ../ddr3_phy.v > build/ddr3_phy_sim.v
iverilog -g2012 -o build/sim -s tb -DSIM_MODEL -DNO_TEST_MODEL -DSIM_TIME_US="${1:-2000}" -DTRACE_N="${TRACE_N:-20}" -DQUIET_STATES="${QUIET:-0}" -DSIM_CAPACITY="${CAP:-5}" -DSIM_SPEED_BIN="${SPEED_BIN:-2}" -DSIM_BIST="${BIST:-0}" ${CK_DIRECT:+-DQMTECH_CK_DIRECT} ${CS:+-DCS_CONNECTED} ${WAVE_DQ:+-DWAVE_DQ} ${WAVE:+-DWAVE_FROM=$WAVE -DWAVE_LEN=${WAVE_LEN:-30000}} -I build/inc -I .. \
    tb_qmtech_ddr3.sv stubs.v "$UBER/testbench/ddr3.sv" build/models/*.v \
    ../ddr3_top.v ../ddr3_controller.v build/ddr3_phy_sim.v
vvp -n build/sim | grep -v -E "^tb\.mem\.(data_task|cmd_task)" | tail -${TAIL:-60}
