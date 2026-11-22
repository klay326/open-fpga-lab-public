set_property LOC F22 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
create_clock -period 20.000 -name clk [get_ports clk]

set_property LOC U26 [get_ports rst_n]
set_property IOSTANDARD LVCMOS18 [get_ports rst_n]

# JP5 header UART (pin 7 = TX, pin 8 = RX)
set_property LOC AE22 [get_ports tx]
set_property IOSTANDARD LVCMOS33 [get_ports tx]
set_property LOC AF22 [get_ports rx]
set_property IOSTANDARD LVCMOS33 [get_ports rx]

set_property LOC R26 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[0]}]
set_property LOC P26 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[1]}]
set_property LOC N26 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[2]}]

# Mirror of the UART stream to the Raspberry Pi CM4 (cm4/pvt-web)

# CM4 UART0 (pvt_lab): FPGA TX -> GPIO15 (RXD0), FPGA RX <- GPIO14 (TXD0)
set_property LOC A15 [get_ports cm4_tx]
set_property IOSTANDARD LVCMOS33 [get_ports cm4_tx]
set_property LOC C17 [get_ports cm4_rx]
set_property IOSTANDARD LVCMOS33 [get_ports cm4_rx]

# CM4 UART3 (trng, 1 Mbaud): FPGA TX -> GPIO5 (RXD3), FPGA RX <- GPIO4 (TXD3)
set_property LOC C11 [get_ports trng_tx]
set_property IOSTANDARD LVCMOS33 [get_ports trng_tx]
set_property LOC E18 [get_ports trng_rx]
set_property IOSTANDARD LVCMOS33 [get_ports trng_rx]
