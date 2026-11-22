# cocotb + Icarus. Run: make   (gate-level: make GATES=yes, after hardening)
SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v tap.v ijtag_cells.v fault_hunt.v bist.v ro_bank.v

# Fault-free MISR for seed0 / 256 patterns (python3 -c "import model; print(hex(model.bist(model.FaultHunt())))")
BIST_GOLDEN ?= 16\'h03B5

ifneq ($(GATES),yes)
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
COMPILE_ARGS += -DSIM
else
SIM_BUILD = sim_build/gl
COMPILE_ARGS += -DGL_TEST -DFUNCTIONAL -DUSE_POWER_PINS -DSIM -DUNIT_DELAY=\#1
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v
endif

COMPILE_ARGS += -g2012 -I$(SRC_DIR) -DBIST_GOLDEN=$(BIST_GOLDEN)
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb
COCOTB_TEST_MODULES = test_chip

include $(shell cocotb-config --makefiles)/Makefile.sim
