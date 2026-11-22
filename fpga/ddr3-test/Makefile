FAMILY  = kintex7
PART    = xc7k325tffg676-1
BOARD   = qmtechKintex7
PROJECT = qmtech_ddr3
CHIPDB  = ${KINTEX7_CHIPDB}
ADDITIONAL_SOURCES = ddr3_top.v ddr3_controller.v ddr3_phy.v uart.v uart_rx.v uart_tx.v

# DDR3 IO (HP bank 34) needs bits that only exist in prjxray-db after 3bd17eb
# (pinned 7a36171 lacks them). A git worktree of openXC7/prjxray-db at 0c3504f:
#   git -C ~/opt/openxc7/src/prjxray-db worktree add ../prjxray-db-head 0c3504f
PRJXRAY_DB_DIR := $(HOME)/opt/openxc7/src/prjxray-db-head

# The demo this comes from has never met its derived controller-clock
# constraint either; hardware round-trip (test.py) is the real check.
PNR_ARGS = --timing-allow-fail

include ../openXC7.mk

# nextpnr-xilinx (bd9c74c5) ignores `set_property INTERNAL_VREF` and always
# emits HCLK_IOI.VREF.V_675_MV for any SSTL bank (correct for SSTL135/DDR3L,
# wrong for SSTL15). This board is SSTL15 DDR3 on internal VREF; Vivado's MIG
# sets 0.75 V (HCLK_IOI_X235Y130.VREF.V_750_MV). Fix it up in the FASM.
${PROJECT}.frames: ${PROJECT}.fasm fasm_fixup.py vivado-bank34-glue.txt
	python3 fasm_fixup.py $< vivado-bank34-glue.txt > ${PROJECT}.fixed.fasm
	fasm2frames --part ${PART} --db-root ${PRJXRAY_DB_DIR}/${FAMILY} ${PROJECT}.fixed.fasm > $@

.PHONY: test
test:
	python3 test.py
