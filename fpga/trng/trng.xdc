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
