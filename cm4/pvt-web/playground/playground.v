// playground -- the resident design behind fpga.klayshomelab.com.
//
// Two independent blocks, unmodified, each on its own CM4 UART:
//
//  * pvt_lab (fpga/pvt-lab, copied here): 5 LUT ring oscillators, on-chip
//    heater, RO-based closed-loop temperature hold, TDC, droop/glitch monitor,
//    watchdog. ASCII lines + commands at 115200 on CM4 UART0:
//      A15 = GPIO15 (RXD0) <- FPGA TX,  C17 = GPIO14 (TXD0) -> FPGA RX.
//    Also on the JP5 header (AE22/AF22) for the Mac. RX from the two hosts is
//    ANDed (both idle high), so only one host should talk at a time.
//
//  * trng (fpga/trng): 16 free-running rings, SP 800-90B health tests, raw
//    bits as binary at 1 Mbaud on CM4 UART3 (dtoverlay=uart3):
//      C11 = GPIO5 (RXD3) <- FPGA TX,   E18 = GPIO4 (TXD3) -> FPGA RX.
//
// LEDs show pvt_lab's (heartbeat, heater, alarm/watchdog).

`default_nettype none

module playground (
    input  wire       clk,       // 50 MHz, F22
    input  wire       rst_n,     // U26
    output wire       tx,        // AE22, JP5 -> FT232R
    input  wire       rx,        // AF22, JP5
    output wire       cm4_tx,    // A15, CM4 GPIO15 RXD0
    input  wire       cm4_rx,    // C17, CM4 GPIO14 TXD0
    output wire       trng_tx,   // C11, CM4 GPIO5 RXD3
    input  wire       trng_rx,   // E18, CM4 GPIO4 TXD3
    output wire [2:0] led
);
    wire pvt_tx;
    pvt_lab lab (.clk(clk), .rst_n(rst_n), .tx(pvt_tx), .cm4_tx(cm4_tx),
                 .rx(rx & cm4_rx), .led(led));
    assign tx = pvt_tx;

    wire [2:0] trng_led;
    trng rng (.clk(clk), .rst_n(rst_n), .tx(trng_tx), .rx(trng_rx), .led(trng_led));
endmodule

`default_nettype wire
