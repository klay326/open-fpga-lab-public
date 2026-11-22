`timescale 1ps / 1ps
// Init + calibration of the board's exact ddr3_top/controller/phy (sim copies
// with Xilinx primitives mapped to UberDDR3's *_model) against Micron's DDR3
// model. Clocks as on the board: 83.33 / 333.33 MHz edge-aligned, 200 MHz ref,
// 333 MHz @90 and @180. CS# tied low as on the board.
module tb;
    // ddr_clk starts high so its rising edges land on ctrl_clk rising edges
    // (6 ns, 18 ns, ...) as from the PLL; clk_90 then rises 750 ps after ddr_clk.
    reg ctrl_clk = 0, ddr_clk = 1, ref_clk = 0, ddr_clk_90 = 1;
    wire ddr_clk_180 = ~ddr_clk;
    always #6000 ctrl_clk = ~ctrl_clk;
    always #1500 ddr_clk  = ~ddr_clk;
    always #2500 ref_clk  = ~ref_clk;
    initial begin #750; forever #1500 ddr_clk_90 = ~ddr_clk_90; end

    reg rst_n = 0;
    initial begin #200_000; rst_n = 1; end

    wire ck_p, ck_n, reset_n, cke, ras_n, cas_n, we_n, odt, cs_n_dut;
    wire [13:0] addr; wire [2:0] ba;
    wire [15:0] dq; wire [1:0] dqs, dqs_n, dm;
    wire [31:0] dbg1;

    ddr3_top #(
        .CONTROLLER_CLK_PERIOD(12_000), .DDR3_CLK_PERIOD(3_000),
        .ROW_BITS(14), .COL_BITS(10), .BA_BITS(3), .BYTE_LANES(2),
        .AUX_WIDTH(16), .WB2_ADDR_BITS(32), .WB2_DATA_BITS(32),
        .SPEED_BIN(`SIM_SPEED_BIN), .SDRAM_CAPACITY(`SIM_CAPACITY), .MICRON_SIM(1),
        .ODELAY_SUPPORTED(0), .SECOND_WISHBONE(0), .BIST_MODE(`SIM_BIST)
    ) dut (
        .i_controller_clk(ctrl_clk), .i_ddr3_clk(ddr_clk), .i_ref_clk(ref_clk),
        .i_ddr3_clk_90(ddr_clk_90), .i_ddr3_clk_180(ddr_clk_180), .i_rst_n(rst_n),
        .i_wb_cyc(1'b1), .i_wb_stb(1'b0), .i_wb_we(1'b0), .i_wb_addr('0), .i_wb_data('0),
        .i_wb_sel('1), .i_aux('0), .o_wb_stall(), .o_wb_ack(), .o_wb_err(), .o_wb_data(), .o_aux(),
        .i_wb2_cyc(1'b0), .i_wb2_stb(1'b0), .i_wb2_we(1'b0), .i_wb2_addr(32'd0),
        .i_wb2_data(32'd0), .i_wb2_sel(4'd0), .o_wb2_stall(), .o_wb2_ack(), .o_wb2_data(),
        .o_ddr3_clk_p(ck_p), .o_ddr3_clk_n(ck_n), .o_ck_probe(),
        .o_ddr3_reset_n(reset_n), .o_ddr3_cke(cke), .o_ddr3_cs_n(cs_n_dut),
        .o_ddr3_ras_n(ras_n), .o_ddr3_cas_n(cas_n), .o_ddr3_we_n(we_n),
        .o_ddr3_addr(addr), .o_ddr3_ba_addr(ba),
        .io_ddr3_dq(dq), .io_ddr3_dqs(dqs), .io_ddr3_dqs_n(dqs_n),
        .o_ddr3_dm(dm), .o_ddr3_odt(odt),
        .o_calib_complete(), .o_debug1(dbg1), .i_user_self_refresh(1'b0), .uart_tx()
    );

    ddr3 mem (
        .rst_n(reset_n), .ck(ck_p), .ck_n(ck_n), .cke(cke),
`ifdef CS_CONNECTED
        .cs_n(cs_n_dut),
`else
        .cs_n(1'b0),   // as on the QMTech board
`endif
        .ras_n(ras_n), .cas_n(cas_n), .we_n(we_n), .dm_tdqs(dm), .ba(ba),
        .addr({2'b00, addr}), .dq(dq), .dqs(dqs), .dqs_n(dqs_n), .tdqs_n(), .odt(odt)
    );


    // command trace as the DRAM samples it (CK rising edge), first N non-NOPs
    integer ncmd = 0;
    always @(posedge ck_p) if (cke && ncmd < `TRACE_N) begin
        if ({ras_n, cas_n, we_n} != 3'b111) begin
            ncmd = ncmd + 1;
            $display("%t ps  DRAM sees %s  ba=%0d addr=%h  odt=%b", $time,
                {ras_n,cas_n,we_n} == 3'b000 ? "MRS " : {ras_n,cas_n,we_n} == 3'b001 ? "REF " :
                {ras_n,cas_n,we_n} == 3'b010 ? "PRE " : {ras_n,cas_n,we_n} == 3'b011 ? "ACT " :
                {ras_n,cas_n,we_n} == 3'b100 ? "WR  " : {ras_n,cas_n,we_n} == 3'b101 ? "RD  " :
                "ZQC ", ba, addr, odt);
        end
    end
`ifdef WAVE_FROM
    initial begin
        #(`WAVE_FROM);
`ifdef WAVE_DQ
        $monitor("%t ps  ck=%b dqs=%b dqs_n=%b dq=%h dm=%b odt=%b", $time, ck_p, dqs, dqs_n, dq, dm, odt);
`else
        $monitor("%t ps  ck=%b ddr_clk=%b  ras=%b cas=%b we=%b cke=%b ba=%0d a=%h", $time, ck_p, ddr_clk, ras_n, cas_n, we_n, cke, ba, addr);
`endif
        #(`WAVE_LEN) $monitoroff;
    end
`endif
    // what the controller sends: phy cmd slots
    reg [4:0] last = 5'h1f;
    always @(posedge ctrl_clk) if (dbg1[4:0] != last) begin
        last <= dbg1[4:0];
        if (!`QUIET_STATES) $display("%t ps  state_calibrate = %0d", $time, dbg1[4:0]);
        if (dbg1[4:0] == 5'd14) $display("%t ps  state 14", $time);
        if (dbg1[4:0] == 5'd23) begin $display("PASS: DONE_CALIBRATE"); $finish; end
    end
    initial begin
        #(`SIM_TIME_US * 1_000_000);
        $display("TIMEOUT at %0d us: state_calibrate = %0d", `SIM_TIME_US, dbg1[4:0]);
        $finish;
    end
endmodule
