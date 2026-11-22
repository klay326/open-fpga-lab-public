// pvt_web -- fpga/xadc-monitor's ring-oscillator stream, sent to both the JP5
// header UART (FT232R on the Mac) and the CM4's own UART (GPIO15 = RXD0), so
// cm4/pvt-web/pvtd.py on the CM4 can read it without any cable to the Mac.
// Line format and conversions: fpga/xadc-monitor/xadc_monitor.v.

`default_nettype none

module pvt_web (
    input  wire       clk,      // 50 MHz, F22
    input  wire       rst_n,    // U26, active low
    output wire       tx,       // AE22, JP5 -> FT232R RXD
    input  wire       rx,       // AF22 (unused)
    output wire       cm4_tx,   // A15 = CM4 GPIO15 (RXD0)
    input  wire       cm4_rx,   // C17 = CM4 GPIO14 (TXD0), unused
    output wire [2:0] led
);
    wire t;
    xadc_monitor mon (.clk(clk), .rst_n(rst_n), .tx(t), .rx(rx), .led(led));
    assign tx     = t;
    assign cm4_tx = t;
endmodule

`default_nettype wire
