################################################################################
# IO constraints
################################################################################
# JP5_serial:0.tx
set_property LOC AE22 [get_ports {JP5_serial_tx}]
set_property IOSTANDARD LVCMOS33 [get_ports {JP5_serial_tx}]

# JP5_serial:0.rx
set_property LOC AF22 [get_ports {JP5_serial_rx}]
set_property IOSTANDARD LVCMOS33 [get_ports {JP5_serial_rx}]

# cpu_reset:0
set_property LOC U26 [get_ports {cpu_reset}]
set_property IOSTANDARD LVCMOS33 [get_ports {cpu_reset}]

# clk50:0
set_property LOC F22 [get_ports {clk50}]
set_property IOSTANDARD LVCMOS33 [get_ports {clk50}]

# user_led:0
set_property LOC R26 [get_ports {user_led0}]
set_property IOSTANDARD LVCMOS33 [get_ports {user_led0}]

# user_led:1
set_property LOC P26 [get_ports {user_led1}]
set_property IOSTANDARD LVCMOS33 [get_ports {user_led1}]

# user_led:2
set_property LOC N26 [get_ports {user_led2}]
set_property IOSTANDARD LVCMOS33 [get_ports {user_led2}]

################################################################################
# Clock constraints
################################################################################

create_clock -name clk50 -period 20.0 [get_ports clk50]