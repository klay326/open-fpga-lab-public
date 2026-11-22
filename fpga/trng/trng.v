// trng -- ring-oscillator-jitter true random number generator, XC7K325T.
//
// Entropy source: NRO free-running LUT rings of different lengths (3, 5 and
// 7 LUTs, so they don't lock to each other). Their outputs are XORed and the
// XOR is sampled by the 50 MHz clock through a 2-FF synchroniser. One raw bit
// is taken every 2^DIV clocks (default DIV=4, 3.1 Mbit/s offered); the UART
// takes bytes as fast as it can, so the effective rate is about 800 kbit/s.
// There is NO post-processing: the stream is the raw noise source, which is
// what you evaluate with SP 800-90B tools.
//
// On-chip continuous health tests (NIST SP 800-90B section 4.4), sized for an
// assumed min-entropy H = 0.5 bit/sample and false-alarm rate 2^-20:
//   Repetition Count Test:   alarm if 41 identical bits in a row
//   Adaptive Proportion Test: alarm if the first bit of a 1024-bit window
//                             recurs >= 793 times in that window
// On an alarm, output stops (latched) and led[2] lights. Send 'C' to clear.
//
// UART: JP5 AE22 TX / AF22 RX, 1,000,000 baud 8N1 (50 MHz / 50 exactly), binary.
// RX commands (single bytes): '0'..'9' set DIV, 'C' clears an alarm,
// 'S' stops / 'G' starts the stream (default: running).
//
// LEDs (active low): led[0] heartbeat, led[1] streaming, led[2] health alarm.

`default_nettype none

module trng #(
    parameter NRO = 16
) (
    input  wire       clk,
    input  wire       rst_n,
    output wire       tx,
    input  wire       rx,
    output wire [2:0] led
);
    // ---------------------------------------------------------------- reset
    reg [2:0]  rst_sync = 3'b000;
    reg [19:0] por = 20'd0;
    always @(posedge clk) begin
        rst_sync <= {rst_sync[1:0], rst_n};
        if (!rst_sync[2]) por <= 20'd0;
        else if (!por[19]) por <= por + 1'b1;
    end
    wire rst = !por[19];

    // ---------------------------------------------------------------- noise source
    wire [NRO-1:0] ro;
    genvar g;
    generate for (g = 0; g < NRO; g = g + 1) begin : rings
        free_ro #(.STAGES(3 + 2 * (g % 3))) r (.en(!rst), .out(ro[g]));
    end endgenerate

    wire raw_x = ^ro;
    (* ASYNC_REG = "TRUE" *) reg s1 = 1'b0, s2 = 1'b0;
    always @(posedge clk) begin s1 <= raw_x; s2 <= s1; end

    // ---------------------------------------------------------------- UART RX
    localparam BAUD_DIV = 50;
    reg [1:0] rx_s = 2'b11;
    always @(posedge clk) rx_s <= {rx_s[0], rx};
    reg [5:0] rx_div = 6'd0;
    reg [3:0] rx_bit = 4'd0;
    reg [7:0] rx_sh = 8'd0;
    reg       rx_busy = 1'b0, rx_stb = 1'b0;
    always @(posedge clk) begin
        rx_stb <= 1'b0;
        if (!rx_busy) begin
            if (!rx_s[1]) begin rx_busy <= 1'b1; rx_div <= BAUD_DIV/2; rx_bit <= 4'd0; end
        end else if (rx_div == BAUD_DIV - 1) begin
            rx_div <= 6'd0; rx_bit <= rx_bit + 1'b1;
            if (rx_bit == 4'd0) begin if (rx_s[1]) rx_busy <= 1'b0; end
            else if (rx_bit <= 4'd8) rx_sh <= {rx_s[1], rx_sh[7:1]};
            else begin rx_busy <= 1'b0; rx_stb <= rx_s[1]; end
        end else rx_div <= rx_div + 1'b1;
    end

    reg [3:0] div = 4'd4;
    reg       run = 1'b1;
    reg       clr = 1'b0;
    always @(posedge clk) begin
        clr <= 1'b0;
        if (rx_stb) begin
            if (rx_sh >= "0" && rx_sh <= "9") div <= rx_sh[3:0];
            else if (rx_sh == "C") clr <= 1'b1;
            else if (rx_sh == "S") run <= 1'b0;
            else if (rx_sh == "G") run <= 1'b1;
        end
    end

    // ---------------------------------------------------------------- sampling
    reg [15:0] sdiv = 16'd0;
    reg        bit_stb = 1'b0;
    reg        bitv = 1'b0;
    always @(posedge clk) begin
        bit_stb <= 1'b0;
        sdiv <= sdiv + 1'b1;
        if ((sdiv & ((16'd1 << div) - 1'b1)) == 16'd0) begin
            bit_stb <= 1'b1; bitv <= s2;
        end
    end

    // ---------------------------------------------------------------- health tests
    localparam RCT_C = 41, APT_W = 1024, APT_C = 793;
    reg        alarm = 1'b0;
    reg        rct_last = 1'b0;
    reg [5:0]  rct_n = 6'd0;
    reg [9:0]  apt_i = 10'd0;
    reg        apt_ref = 1'b0;
    reg [10:0] apt_n = 11'd0;
    always @(posedge clk) begin
        if (rst || clr) begin
            alarm <= 1'b0; rct_n <= 6'd0; apt_i <= 10'd0; apt_n <= 11'd0;
        end else if (bit_stb) begin
            // RCT
            if (bitv == rct_last) begin
                if (rct_n == RCT_C - 1) alarm <= 1'b1; else rct_n <= rct_n + 1'b1;
            end else begin
                rct_last <= bitv; rct_n <= 6'd1;
            end
            // APT
            if (apt_i == 10'd0) begin
                apt_ref <= bitv; apt_n <= 11'd1;
            end else if (bitv == apt_ref) begin
                if (apt_n == APT_C - 1) alarm <= 1'b1;
                apt_n <= apt_n + 1'b1;
            end
            apt_i <= apt_i + 1'b1;     // wraps at 1024
        end
    end

    // ---------------------------------------------------------------- byte packer + UART TX
    reg [7:0] acc = 8'd0;
    reg [2:0] nb = 3'd0;
    reg [7:0] fifo = 8'd0;
    reg       full = 1'b0;
    reg       txgo;
    always @(posedge clk) begin
        if (bit_stb && !full) begin
            acc <= {acc[6:0], bitv};
            nb  <= nb + 1'b1;
            if (nb == 3'd7) begin fifo <= {acc[6:0], bitv}; full <= run && !alarm; end
        end
        if (txgo) full <= 1'b0;
    end

    reg [5:0] bdiv = 6'd0;
    reg [3:0] bitn = 4'd0;
    reg [9:0] shreg = 10'h3FF;
    reg       txr = 1'b1;
    assign tx = txr;
    always @(*) txgo = (bitn == 4'd0) && full;
    always @(posedge clk) begin
        if (txgo) begin
            shreg <= {1'b1, fifo, 1'b0}; bitn <= 4'd10; bdiv <= 6'd0;
        end else if (bitn != 4'd0) begin
            if (bdiv == BAUD_DIV - 1) begin
                bdiv <= 6'd0; txr <= shreg[0]; shreg <= {1'b1, shreg[9:1]}; bitn <= bitn - 1'b1;
            end else bdiv <= bdiv + 1'b1;
        end
    end

    reg [24:0] hb = 25'd0;
    always @(posedge clk) hb <= hb + 1'b1;
    assign led[0] = ~hb[24];
    assign led[1] = ~(run && !alarm);
    assign led[2] = ~alarm;
endmodule

// Free-running ring: LUT2 (en & ~fb) + STAGES-1 LUT1 buffers. Not pinned.
module free_ro #(parameter STAGES = 3) (input wire en, output wire out);
    (* keep, clkbuf_inhibit *) wire [STAGES-1:0] n;
    (* keep *) LUT2 #(.INIT(4'b0100)) s0 (.O(n[0]), .I0(n[STAGES-1]), .I1(en));
    genvar i;
    generate for (i = 1; i < STAGES; i = i + 1) begin : buf_
        (* keep *) LUT1 #(.INIT(2'b10)) s (.O(n[i]), .I0(n[i-1]));
    end endgenerate
    assign out = n[STAGES-1];
endmodule

`default_nettype wire
