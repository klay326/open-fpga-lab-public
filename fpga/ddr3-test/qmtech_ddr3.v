`timescale 1ns / 1ps
`default_nettype none
// DDR3 round-trip test for the QMTech XC7K325T dev board -- no LiteX, no CPU.
//
// Controller/PHY: ddr3_controller.v / ddr3_phy.v / ddr3_top.v (UberDDR3, as
// vendored in openXC7/demo-projects ddr3-test-arty-s7; see UPSTREAM-COMMIT).
// Top level adapted from that demo's arty_ddr3.v for this board:
//   - 50 MHz F22 in -> PLLE2 VCO 1 GHz -> 83.33 / 333.33 / 200 / 333.33@90 MHz
//     (same rates the Arty demo is verified at: DDR3-667, 4:1 serdes)
//   - 16-bit DDR3 on HP bank 34, pins identical to LiteX's qmtech_kintex7_devboard
//   - UART on JP5 (AE22 = TX, AF22 = RX), 9600 8N1
//   - user LEDs are active-low (3V3 -> 1K -> LED -> pin)
//
// UART protocol (same as the Arty demo, plus '?' -> '@'+calibration state): send 'a'..'z' to write that byte to
// DDR3 at address ~byte; send 'A'..'Z' to read back the matching lowercase
// letter's address -- the controller returns the stored byte.
//
// LEDs (lit = true):  D2 (R26) = PLL locked
//                     D3 (P26) = controller reached DONE_CALIBRATE (state 23)
//                     D7 (N26) = heartbeat (~1.2 Hz from the controller clock)
module qmtech_ddr3 (
    input  wire        clk50,
    input  wire        sw2_n,          // U26, active-low push button: reset
    // DDR3
    output wire        ddr3_clk_p, ddr3_clk_n,
    output wire        ddr3_reset_n,
    output wire        ddr3_cke,
    output wire        ddr3_ras_n,
    output wire        ddr3_cas_n,
    output wire        ddr3_we_n,
    output wire [13:0] ddr3_addr,
    output wire [2:0]  ddr3_ba,
    inout  wire [15:0] ddr3_dq,
    inout  wire [1:0]  ddr3_dqs_p, ddr3_dqs_n,
    output wire [1:0]  ddr3_dm,
    output wire        ddr3_odt,
    // UART
    input  wire        rx,
    output wire        tx,
    // LEDs, active-low
    output wire [2:0]  led_n
);
    wire i_rst = ~sw2_n;

    // ---------------- clocks ----------------
    wire clk_fb, pll_locked;
    wire c0, c1, c2, c3, c4;
    wire i_controller_clk, i_ddr3_clk, i_ref_clk, i_ddr3_clk_90, i_ddr3_clk_180;
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"),
        .COMPENSATION("ZHOLD"),
        .DIVCLK_DIVIDE(1),
        .CLKFBOUT_MULT(20),          // 50 MHz * 20 = 1000 MHz VCO
        .CLKFBOUT_PHASE(0.0),
        .CLKIN1_PERIOD(20.0),
        .CLKOUT0_DIVIDE(12), .CLKOUT0_PHASE(0.0),  .CLKOUT0_DUTY_CYCLE(0.5), // 83.333 MHz controller
        .CLKOUT1_DIVIDE(3),  .CLKOUT1_PHASE(0.0),  .CLKOUT1_DUTY_CYCLE(0.5), // 333.333 MHz DDR3
        .CLKOUT2_DIVIDE(5),  .CLKOUT2_PHASE(0.0),  .CLKOUT2_DUTY_CYCLE(0.5), // 200 MHz IDELAYCTRL ref
        .CLKOUT3_DIVIDE(3),  .CLKOUT3_PHASE(90.0), .CLKOUT3_DUTY_CYCLE(0.5), // 333.333 MHz @ 90 deg
        .CLKOUT4_DIVIDE(3),  .CLKOUT4_PHASE(180.0), .CLKOUT4_DUTY_CYCLE(0.5) // 333.333 MHz @ 180 deg (= ~i_ddr3_clk)
    ) pll (
        .CLKIN1(clk50), .CLKIN2(1'b0), .CLKINSEL(1'b1),
        .CLKFBIN(clk_fb), .CLKFBOUT(clk_fb),
        .CLKOUT0(c0), .CLKOUT1(c1), .CLKOUT2(c2), .CLKOUT3(c3),
        .CLKOUT4(c4), .CLKOUT5(),
        .RST(i_rst), .PWRDWN(1'b0), .LOCKED(pll_locked),
        .DADDR(7'd0), .DCLK(1'b0), .DEN(1'b0), .DI(16'd0), .DWE(1'b0), .DO(), .DRDY()
    );
    BUFG b0 (.I(c0), .O(i_controller_clk));
    BUFG b1 (.I(c1), .O(i_ddr3_clk));
    BUFG b2 (.I(c2), .O(i_ref_clk));
    BUFG b3 (.I(c3), .O(i_ddr3_clk_90));
    BUFG b4 (.I(c4), .O(i_ddr3_clk_180));

    // ---------------- UART <-> wishbone bridge (from the Arty demo) ----------------
    wire       m_axis_tvalid;
    wire [7:0] rd_data;
    wire       o_wb_ack, o_wb_stall, o_aux;
    wire [7:0] o_wb_data;
    wire [63:0] o_debug1;
    reg        i_wb_stb = 0, i_wb_we = 0;
    reg  [7:0] i_wb_data = 0, i_wb_addr = 0;
    // '?' -> reply '@' + calibration state (0..23; 'W' = DONE_CALIBRATE)
    // '0'/'1' -> sticky OR of DQS ISERDES samples, lane 0 / lane 1
    // '2'/'3' -> sticky AND of DQS ISERDES samples, lane 0 / lane 1
    // '4'..'5'-> live DQS ISERDES sample, lane 0 / lane 1
    // '#'      -> clear the sticky registers
    reg        dbg_valid = 0;
    reg  [7:0] dbg_data = 0;
    reg [15:0] dqs_or = 0, dqs_and = 16'hffff;
    wire [15:0] dqs_now = o_debug1[31:16];

    always @(posedge i_controller_clk) begin
        i_wb_stb  <= 0;
        i_wb_we   <= 0;
        i_wb_addr <= 0;
        i_wb_data <= 0;
        dbg_valid <= 0;
        dqs_or  <= dqs_or  | dqs_now;
        dqs_and <= dqs_and & dqs_now;
        if (m_axis_tvalid && rd_data == 8'h3F) begin                // '?': status
            dbg_valid <= 1;
            dbg_data  <= 8'h40 + {3'b000, o_debug1[4:0]};
        end else if (m_axis_tvalid && rd_data >= 8'h30 && rd_data <= 8'h35) begin
            dbg_valid <= 1;
            case (rd_data[2:0])
                3'd0: dbg_data <= dqs_or[7:0];
                3'd1: dbg_data <= dqs_or[15:8];
                3'd2: dbg_data <= dqs_and[7:0];
                3'd3: dbg_data <= dqs_and[15:8];
                3'd4: dbg_data <= dqs_now[7:0];
                default: dbg_data <= dqs_now[15:8];
            endcase
        end else if (m_axis_tvalid && rd_data == 8'h23) begin       // '#': clear
            dqs_or  <= 0;
            dqs_and <= 16'hffff;
        end else if (!o_wb_stall && m_axis_tvalid) begin
            if (rd_data >= 8'd97 && rd_data <= 8'd122) begin        // 'a'..'z': write
                i_wb_stb  <= 1;
                i_wb_we   <= 1;
                i_wb_addr <= ~rd_data;
                i_wb_data <= rd_data;
            end else if (rd_data >= 8'd65 && rd_data <= 8'd90) begin // 'A'..'Z': read
                i_wb_stb  <= 1;
                i_wb_we   <= 0;
                i_wb_addr <= ~(rd_data + 8'd32);
            end
        end
    end

    uart #(.DATA_WIDTH(8)) uart_m (
        .clk(i_controller_clk), .rst(i_rst),
        .s_axis_tdata(dbg_valid ? dbg_data : o_wb_data),
        .s_axis_tvalid(o_wb_ack | dbg_valid), .s_axis_tready(),
        .m_axis_tdata(rd_data), .m_axis_tvalid(m_axis_tvalid), .m_axis_tready(1'b1),
        .rxd(rx), .txd(tx),
        .prescale(16'd1085)   // 83.333 MHz / (9600 * 8)
    );

    // ---------------- DDR3 controller ----------------
    ddr3_top #(
        .ROW_BITS(14), .COL_BITS(10), .BA_BITS(3),
        .DQ_BITS(8), .LANES(2),
        .CONTROLLER_CLK_PERIOD(12), .DDR3_CLK_PERIOD(3),
        .ODELAY_SUPPORTED(0),     // HP bank has ODELAYE2, but prjxray-db has no feature routing its output back to the pad, so use the 90-deg-clock scheme
        .AUX_WIDTH(16), .WB2_ADDR_BITS(32), .WB2_DATA_BITS(32),
        .OPT_LOWPOWER(1), .OPT_BUS_ABORT(1),
        .MICRON_SIM(0), .TEST_DATAMASK(1)
    ) ddr3_top (
        .i_controller_clk(i_controller_clk), .i_ddr3_clk(i_ddr3_clk),
        .i_ref_clk(i_ref_clk), .i_ddr3_clk_90(i_ddr3_clk_90), .i_ddr3_clk_180(i_ddr3_clk_180),
        .i_rst_n(!i_rst && pll_locked),
        .i_wb_cyc(1'b1), .i_wb_stb(i_wb_stb), .i_wb_we(i_wb_we),
        .i_wb_addr(i_wb_addr), .i_wb_data(i_wb_data), .i_wb_sel(16'hffff),
        .i_aux(i_wb_we),
        .o_wb_stall(o_wb_stall), .o_wb_ack(o_wb_ack), .o_wb_data(o_wb_data), .o_aux(o_aux),
        .i_wb2_cyc(1'b0), .i_wb2_stb(1'b0), .i_wb2_we(1'b0), .i_wb2_addr(32'd0),
        .i_wb2_data(32'd0), .i_wb2_sel(4'd0),
        .o_wb2_stall(), .o_wb2_ack(), .o_wb2_data(),
        .o_ddr3_clk_p(ddr3_clk_p), .o_ddr3_clk_n(ddr3_clk_n),
        .o_ddr3_reset_n(ddr3_reset_n), .o_ddr3_cke(ddr3_cke),
        .o_ddr3_cs_n(),            // CS# is tied low on this board
        .o_ddr3_ras_n(ddr3_ras_n), .o_ddr3_cas_n(ddr3_cas_n), .o_ddr3_we_n(ddr3_we_n),
        .o_ddr3_addr(ddr3_addr), .o_ddr3_ba_addr(ddr3_ba),
        .io_ddr3_dq(ddr3_dq), .io_ddr3_dqs(ddr3_dqs_p), .io_ddr3_dqs_n(ddr3_dqs_n),
        .o_ddr3_dm(ddr3_dm), .o_ddr3_odt(ddr3_odt),
        .o_debug1(o_debug1)
    );

    // ---------------- status LEDs (active-low) ----------------
    reg [25:0] hb = 0;
    always @(posedge i_controller_clk) hb <= hb + 1'b1;
    wire calibrated = (o_debug1[4:0] == 5'd23);
    assign led_n[0] = ~pll_locked;     // D2 / R26
    assign led_n[1] = ~calibrated;     // D3 / P26
    assign led_n[2] = ~hb[25];         // D7 / N26
endmodule
`default_nettype wire
