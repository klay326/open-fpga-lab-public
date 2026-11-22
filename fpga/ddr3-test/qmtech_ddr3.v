`timescale 1ns / 1ps
`include "build_config.vh"   // `make CAL_DEBUG=1`: JP5 TX carries the controller's calibration log
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
    inout  wire        ddr3_clk_p, ddr3_clk_n,
    inout  wire        ddr3_reset_n,   // inout only for pad read-back (debug probes)
    inout  wire        ddr3_cke,
    inout  wire        ddr3_ras_n,
    inout  wire        ddr3_cas_n,
    inout  wire        ddr3_we_n,
    inout  wire [13:0] ddr3_addr,
    inout  wire [2:0]  ddr3_ba,
    inout  wire [15:0] ddr3_dq,
    inout  wire [1:0]  ddr3_dqs_p, ddr3_dqs_n,
    output wire [1:0]  ddr3_dm,
    inout  wire        ddr3_odt,
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
    wire [31:0] o_debug2, o_debug3;
    wire        calib_complete;
    wire        bridge_tx, cal_dbg_tx;
    // IDELAY tap read-back: '%' DQS lane0 actual, '&' DQS lane1 actual,
    // '*' last commanded DQS tap, '+' DQ0 actual (all 0..31)
    wire [9:0]  dbg_dqs_cnt; wire [4:0] dbg_dq0_cnt, dbg_dqs_cmd;
    reg  [63:0] rd_snap = 0;      // read_lane_data at first entry to state 14 since '#'
    reg         rd_snap_valid = 0;
    reg  [2:0]  rd_ptr = 0;       // '[' -> next snapshot byte (LSB first), ']' -> rewind
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
    reg  [3:0] pad_clr_cnt = 0;
    reg  [4:0] max_state = 0;     // '!' -> highest calibration state reached since '#'
    reg [15:0] n_restart = 0;     // '$' -> number of 0-after-nonzero transitions (low byte)
    reg  [4:0] prev_state = 0;   // stretches '#' so the 200 MHz domain sees it
    wire       pad_clr = |pad_clr_cnt;
    wire [15:0] dqs_now = o_debug1[31:16];

    always @(posedge i_controller_clk) begin
        i_wb_stb  <= 0;
        i_wb_we   <= 0;
        i_wb_addr <= 0;
        i_wb_data <= 0;
        dbg_valid <= 0;
        if (pad_clr_cnt != 0) pad_clr_cnt <= pad_clr_cnt - 1'b1;
        prev_state <= o_debug1[4:0];
        if (o_debug1[4:0] > max_state) max_state <= o_debug1[4:0];
        if (o_debug1[4:0] == 5'd14 && prev_state != 5'd14 && !rd_snap_valid) begin
            rd_snap <= {o_debug2, o_debug3}; rd_snap_valid <= 1;
        end
        if (o_debug1[4:0] == 0 && prev_state != 0) n_restart <= n_restart + 1'b1;
        dqs_or  <= dqs_or  | dqs_now;
        dqs_and <= dqs_and & dqs_now;
        if (m_axis_tvalid && rd_data == 8'h3F) begin                // '?': status
            dbg_valid <= 1;
            dbg_data  <= 8'h40 + {3'b000, o_debug1[4:0]};
        end else if (m_axis_tvalid && rd_data == 8'h5B) begin       // '['
            dbg_valid <= 1; dbg_data <= rd_snap[8*rd_ptr +: 8]; rd_ptr <= rd_ptr + 1'b1;
        end else if (m_axis_tvalid && rd_data == 8'h5D) begin       // ']'
            dbg_valid <= 1; dbg_data <= {7'd0, rd_snap_valid}; rd_ptr <= 0;
        end else if (m_axis_tvalid && (rd_data == 8'h25 || rd_data == 8'h26 || rd_data == 8'h2A || rd_data == 8'h2B)) begin
            dbg_valid <= 1;
            dbg_data  <= rd_data == 8'h25 ? {3'd0, dbg_dqs_cnt[4:0]} :
                         rd_data == 8'h26 ? {3'd0, dbg_dqs_cnt[9:5]} :
                         rd_data == 8'h2A ? {3'd0, dbg_dqs_cmd}      : {3'd0, dbg_dq0_cnt};
        end else if (m_axis_tvalid && rd_data == 8'h21) begin       // '!'
            dbg_valid <= 1; dbg_data <= 8'h40 + {3'd0, max_state};
        end else if (m_axis_tvalid && rd_data == 8'h24) begin       // '$'
            dbg_valid <= 1; dbg_data <= n_restart[7:0];
        end else if (m_axis_tvalid && rd_data >= 8'h39 && rd_data <= 8'h3E) begin
            dbg_valid <= 1;
            case (rd_data)
                8'h39: dbg_data <= ab_or[7:0];
                8'h3A: dbg_data <= ab_or[15:8];
                8'h3B: dbg_data <= {7'd0, ab_or[16]};
                8'h3C: dbg_data <= ab_and[7:0];
                8'h3D: dbg_data <= ab_and[15:8];
                default: dbg_data <= {7'd0, ab_and[16]};
            endcase
        end else if (m_axis_tvalid && rd_data >= 8'h36 && rd_data <= 8'h38) begin
            dbg_valid <= 1;
            dbg_data  <= rd_data == 8'h36 ? {1'b0, pad_or} :
                         rd_data == 8'h37 ? {1'b0, pad_and} : {1'b0, pad_s2};
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
            pad_clr_cnt <= 4'hf;
            max_state <= 0; n_restart <= 0; rd_snap_valid <= 0;
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
        .rxd(rx), .txd(bridge_tx),
        .prescale(16'd1085)   // 83.333 MHz / (9600 * 8)
    );

    // ---------------- pad read-back probes (debug) ----------------
    // Control outputs go through IOBUFs with T=0: they drive exactly as an
    // OBUF would, and the receiver reads back what is really on the pad.
    // Sampled with the 200 MHz ref clock (not locked to 333 MHz CK), so a
    // toggling pad shows up as both 0 and 1 in the sticky OR/AND.
    //   bit 6 CK, 5 RESET#, 4 CKE, 3 RAS#, 2 CAS#, 1 WE#, 0 ODT
    wire o_reset_n, o_cke, o_ras_n, o_cas_n, o_we_n, o_odt, ck_probe;
    wire [5:0] pad_in;
    IOBUF #(.IOSTANDARD("LVCMOS15")) iob_reset (.IO(ddr3_reset_n), .I(o_reset_n), .T(1'b0), .O(pad_in[5]));
    IOBUF #(.IOSTANDARD("SSTL15")) iob_cke   (.IO(ddr3_cke),   .I(o_cke),   .T(1'b0), .O(pad_in[4]));
    IOBUF #(.IOSTANDARD("SSTL15")) iob_ras   (.IO(ddr3_ras_n), .I(o_ras_n), .T(1'b0), .O(pad_in[3]));
    IOBUF #(.IOSTANDARD("SSTL15")) iob_cas   (.IO(ddr3_cas_n), .I(o_cas_n), .T(1'b0), .O(pad_in[2]));
    IOBUF #(.IOSTANDARD("SSTL15")) iob_we    (.IO(ddr3_we_n),  .I(o_we_n),  .T(1'b0), .O(pad_in[1]));
    IOBUF #(.IOSTANDARD("SSTL15")) iob_odt   (.IO(ddr3_odt),   .I(o_odt),   .T(1'b0), .O(pad_in[0]));
    reg [2:0] pclr_s = 0;
    // address/bank read-back: same scheme, 17 bits {ba, addr}
    wire [13:0] o_addr; wire [2:0] o_ba; wire [16:0] ab_in;
    genvar gi;
    generate for (gi = 0; gi < 14; gi = gi + 1) begin : g_addr
        IOBUF #(.IOSTANDARD("SSTL15")) iob (.IO(ddr3_addr[gi]), .I(o_addr[gi]), .T(1'b0), .O(ab_in[gi]));
    end
    for (gi = 0; gi < 3; gi = gi + 1) begin : g_ba
        IOBUF #(.IOSTANDARD("SSTL15")) iob (.IO(ddr3_ba[gi]), .I(o_ba[gi]), .T(1'b0), .O(ab_in[14 + gi]));
    end endgenerate
    reg [16:0] ab_s1 = 0, ab_s2 = 0, ab_or = 0, ab_and = 17'h1ffff;
    always @(posedge i_ref_clk) begin
        ab_s1 <= ab_in; ab_s2 <= ab_s1;
        if (pclr_s[2]) begin ab_or <= 0; ab_and <= 17'h1ffff; end
        else begin ab_or <= ab_or | ab_s2; ab_and <= ab_and & ab_s2; end
    end
    reg [6:0] pad_s1 = 0, pad_s2 = 0, pad_or = 0, pad_and = 7'h7f;
    always @(posedge i_ref_clk) begin
        pad_s1 <= {ck_probe, pad_in};
        pad_s2 <= pad_s1;
        pclr_s <= {pclr_s[1:0], pad_clr};
        if (pclr_s[2]) begin
            pad_or <= 0; pad_and <= 7'h7f;
        end else begin
            pad_or <= pad_or | pad_s2; pad_and <= pad_and & pad_s2;
        end
    end

    // ---------------- DDR3 controller ----------------
    ddr3_top #(
        .CONTROLLER_CLK_PERIOD(12_000), .DDR3_CLK_PERIOD(3_000),   // ps: 83.33 / 333.33 MHz (DDR3-667)
        .ROW_BITS(14), .COL_BITS(10), .BA_BITS(3), .BYTE_LANES(2),
        .AUX_WIDTH(16), .WB2_ADDR_BITS(32), .WB2_DATA_BITS(32),
        .SPEED_BIN(2),            // MT41K128M16JT-15E: DDR3-1333 (9-9-9) timings
        .SDRAM_CAPACITY(3),       // 2 Gb
        .MICRON_SIM(0),
        .ODELAY_SUPPORTED(0),     // HP bank has ODELAYE2, but prjxray-db has no feature routing its output back to the pad, so use the 90-deg-clock scheme
        .SECOND_WISHBONE(0),
        .BIST_MODE(1),            // calibration includes one full-memory write/read BIST
        .DIC(2'b01),              // DRAM drive RZQ/7 (34 ohm) and ...
        .RTT_NOM(3'b001)          // ... ODT RZQ/4 (60 ohm): what Vivado's MIG uses on this board
    ) ddr3_top (
        .i_controller_clk(i_controller_clk), .i_ddr3_clk(i_ddr3_clk),
        .i_ref_clk(i_ref_clk), .i_ddr3_clk_90(i_ddr3_clk_90), .i_ddr3_clk_180(i_ddr3_clk_180),
        .i_rst_n(!i_rst && pll_locked),
        .i_wb_cyc(1'b1), .i_wb_stb(i_wb_stb), .i_wb_we(i_wb_we),
        .i_wb_addr(i_wb_addr), .i_wb_data(i_wb_data), .i_wb_sel(16'hffff),
        .i_aux(i_wb_we),
        .o_wb_stall(o_wb_stall), .o_wb_ack(o_wb_ack), .o_wb_err(), .o_wb_data(o_wb_data), .o_aux(o_aux),
        .i_wb2_cyc(1'b0), .i_wb2_stb(1'b0), .i_wb2_we(1'b0), .i_wb2_addr(32'd0),
        .i_wb2_data(32'd0), .i_wb2_sel(4'd0),
        .o_wb2_stall(), .o_wb2_ack(), .o_wb2_data(),
        .o_ddr3_clk_p(ddr3_clk_p), .o_ddr3_clk_n(ddr3_clk_n), .o_ck_probe(ck_probe),
        .o_ddr3_reset_n(o_reset_n), .o_ddr3_cke(o_cke),
        .o_ddr3_cs_n(),            // CS# is tied low on this board
        .o_ddr3_ras_n(o_ras_n), .o_ddr3_cas_n(o_cas_n), .o_ddr3_we_n(o_we_n),
        .o_ddr3_addr(o_addr), .o_ddr3_ba_addr(o_ba),
        .io_ddr3_dq(ddr3_dq), .io_ddr3_dqs(ddr3_dqs_p), .io_ddr3_dqs_n(ddr3_dqs_n),
        .o_ddr3_dm(ddr3_dm), .o_ddr3_odt(o_odt),
        .o_calib_complete(calib_complete),
        .o_dbg_idelay_dqs_cnt(dbg_dqs_cnt), .o_dbg_idelay_dq0_cnt(dbg_dq0_cnt), .o_dbg_idelay_dqs_cmd(dbg_dqs_cmd),
        .o_debug1(o_debug1),
        .i_user_self_refresh(1'b0),
        .uart_tx(cal_dbg_tx)
    );
    // JP5 TX: the UART bridge normally; the controller's read-calibration log
    // (9600 8N1 text, UberDDR3 UART_DEBUG_READ_LEVEL) in CAL_DEBUG builds.
`ifdef UART_DEBUG_READ_LEVEL
    assign tx = cal_dbg_tx;
`else
    assign tx = bridge_tx;
`endif
    assign o_debug2 = 32'd0, o_debug3 = 32'd0;   // upstream controller has no debug2/3

    // ---------------- status LEDs (active-low) ----------------
    reg [25:0] hb = 0;
    always @(posedge i_controller_clk) hb <= hb + 1'b1;
    wire calibrated = calib_complete;
    assign led_n[0] = ~pll_locked;     // D2 / R26
    assign led_n[1] = ~calibrated;     // D3 / P26
    assign led_n[2] = ~hb[25];         // D7 / N26
endmodule
`default_nettype wire
