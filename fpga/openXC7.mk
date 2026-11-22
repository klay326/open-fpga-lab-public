NEXTPNR_XILINX_DIR ?= /snap/openxc7/current/opt/nextpnr-xilinx
NEXTPNR_XILINX_PYTHON_DIR ?= ${NEXTPNR_XILINX_DIR}/python
PRJXRAY_DB_DIR ?= ${NEXTPNR_XILINX_DIR}/external/prjxray-db

DBPART = $(shell echo ${PART} | sed -e 's/-[0-9]//g')
SPEEDGRADE = $(shell echo ${PART} | sed -e 's/.*\-\([0-9]\)/\1/g')

CHIPDB ?= ../chipdb/
ifeq ($(CHIPDB),)
CHIPDB = ../chipdb/
endif

PYPY3 ?= pypy3

TOP ?= ${PROJECT}
TOP_MODULE ?= ${TOP}
TOP_VERILOG ?= ${TOP}.v

PNR_DEBUG ?= # --verbose --debug

BOARD ?= UNKNOWN

# JTAG: the Waveshare Xilinx Platform Cable USB clone (03fd:0013). It boots its
# own XPCU firmware, so stock openFPGALoader's firmware upload is ACKed and
# ignored and it waits forever for 03fd:0008. `--probe-firmware none` needs the
# patched build from tools/openfpgaloader/build.sh (installed to
# ~/opt/openxc7/bin, first on PATH after sourcing ~/opt/openxc7/export.sh).
# `--board ${BOARD}` alone selects no cable driver and fails -- that was the
# original "cable doesn't work with openFPGALoader" result.
# Override, e.g.: make program JTAG_LINK="-c digilent_hs2"
JTAG_CABLE ?= xilinxPlatformCableUsb
JTAG_PROBE_FW ?= none
# 12 MHz: 20 s per full K325T bitstream (verified, DONE=1); 6000000 = 34 s
JTAG_FREQ ?= 12000000
JTAG_LINK ?= -c ${JTAG_CABLE} --probe-firmware ${JTAG_PROBE_FW} --freq ${JTAG_FREQ}

XDC ?= ${PROJECT}.xdc

# fasm2frames writes its target through a shell redirection, so a failure part-way
# leaves a truncated .frames that is NEWER than its prerequisite. The next make then
# skips regenerating it and feeds the partial file to xc7frames2bit, which happily
# produces a normal-looking ~9.7 MB bitstream. It flashes, reports done 1, and the
# board does nothing -- done 1 means configuration completed, not that the design works.
# A CI matrix that checks only "was a .bit produced" reports green on exactly this.
#
# .DELETE_ON_ERROR makes make remove a target whose recipe failed, so the next run
# rebuilds it instead of building on top of the wreckage. It covers .fasm, .json and
# .bit as well, at no cost when nothing fails.
.DELETE_ON_ERROR:

.PHONY: all
all: ${PROJECT}.bit

.PHONY: program
program: ${PROJECT}.bit
	openFPGALoader ${JTAG_LINK} --bitstream $<

${PROJECT}.json: ${TOP_VERILOG} ${ADDITIONAL_SOURCES}
	yosys -p "synth_xilinx -flatten -abc9 ${SYNTH_OPTS} -arch xc7 -top ${TOP_MODULE}; write_json ${PROJECT}.json" $< ${ADDITIONAL_SOURCES}

# The chip database only needs to be generated once
# that is why we don't clean it with make clean
${CHIPDB}/${DBPART}.bin:
	${PYPY3} ${NEXTPNR_XILINX_PYTHON_DIR}/bbaexport.py --device ${PART} --bba ${DBPART}.bba
	bbasm -l ${DBPART}.bba ${CHIPDB}/${DBPART}.bin
	rm -f ${DBPART}.bba

${PROJECT}.fasm: ${PROJECT}.json ${CHIPDB}/${DBPART}.bin ${XDC}
	nextpnr-xilinx --chipdb ${CHIPDB}/${DBPART}.bin --xdc ${XDC} --json ${PROJECT}.json --fasm $@ ${PNR_ARGS} ${PNR_DEBUG}
	
${PROJECT}.frames: ${PROJECT}.fasm
	fasm2frames --part ${PART} --db-root ${PRJXRAY_DB_DIR}/${FAMILY} $< > $@

${PROJECT}.bit: ${PROJECT}.frames
	xc7frames2bit --part_file ${PRJXRAY_DB_DIR}/${FAMILY}/${PART}/part.yaml --part_name ${PART} --frm_file $< --output_file $@

.PHONY: clean
clean:
	@rm -f *.bit
	@rm -f *.frames
	@rm -f *.fasm
	@rm -f *.json

.PHONY: pnrclean
pnrclean:
	rm *.fasm *.frames *.bit
