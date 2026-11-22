// ro_grid -- 8x8 grid of ring oscillators across the die, for a live
// thermal / IR-drop map.
//
// Only ONE ring runs at a time (one-hot enable), so rings can't
// injection-lock to each other and the map adds no switching noise of its
// own. Each ring is divided by 4 next to itself (two toggle FFs on local
// routing) before it goes through the 64:1 mux into a single shared counter
// (one more toggle FF + 24-bit Gray counter, like ro_monitor.v). Without the
// local divider, 500-600 MHz rings drove the mux and a BUFG-clocked counter
// rated at ~400 MHz, and edges got swallowed (counts jumped between three
// levels). Per ring:
//   switch + settle 2^12 clk  ->  Gray snapshot  ->  2^WIN clk window  ->
//   Gray snapshot  ->  count = RO/2 edges in the window
// A full frame of 64 rings takes 64 * (2^12 + 2^WIN + 512) clocks
// (0.34 s at WIN = 18). f_ring = 8 * count * 50e6 / 2^WIN.
// Sites come from tools/gen_ro_grid.py (ro_grid_inst.vh): index = row*8+col,
// row 0 at the bottom of the die, col 0 at the left.

`default_nettype none

module ro_grid #(
    parameter WIN = 18
) (
    input  wire        clk,
    input  wire        run,
    input  wire [5:0]  rd_idx,       // read port for the UART line generator
    output wire [23:0] rd_cnt,
    output reg  [7:0]  frame = 8'd0, // increments when a full frame is ready
    output reg         frame_stb = 1'b0
);
    reg  [63:0] en = 64'd0;
    wire [63:0] ring;
`include "ro_grid_inst.vh"

    reg  [5:0] sel = 6'd0;
    wire       rsel = ring[sel];

    // RO domain: toggle + Gray counter (clocked from the mux output)
    reg        half = 1'b0;
    always @(posedge rsel) half <= ~half;
    reg [23:0] bin = 24'd0, gray = 24'd0;
    always @(posedge half) begin
        bin  <= bin + 1'b1;
        gray <= bin ^ (bin >> 1);
    end

    // clk domain
    (* ASYNC_REG = "TRUE" *) reg [23:0] g1 = 24'd0, g2 = 24'd0;
    always @(posedge clk) begin g1 <= gray; g2 <= g1; end
    reg [23:0] gsnap = 24'd0, b;
    integer i;
    always @* begin
        b[23] = gsnap[23];
        for (i = 22; i >= 0; i = i - 1) b[i] = b[i+1] ^ gsnap[i];
    end

    // cnt is filled ring by ring; at frame end it is copied to cnt_out, so the
    // read port always shows one complete frame.
    reg [23:0] cnt [0:63];
    reg [23:0] cnt_out [0:63];
    initial for (i = 0; i < 64; i = i + 1) begin cnt[i] = 24'd0; cnt_out[i] = 24'd0; end
    assign rd_cnt = cnt_out[rd_idx];
    reg copy = 1'b0;
    always @(posedge clk) begin
        if (copy) for (i = 0; i < 64; i = i + 1) cnt_out[i] <= cnt[i];
    end

    localparam S_SW = 2'd0, S_START = 2'd1, S_WIN = 2'd2, S_END = 2'd3;
    reg [1:0]  st = S_SW;
    reg [WIN:0] t = 0;
    reg [23:0] bstart = 24'd0;
    always @(posedge clk) begin
        frame_stb <= copy;      // one cycle after the copy
        copy <= 1'b0;
        if (!run) begin
            en <= 64'd0; st <= S_SW; t <= 0; sel <= 6'd0;
        end else begin
            t <= t + 1'b1;
            case (st)
                S_SW: begin                      // ring `sel` on, the rest off; settle
                    en <= 64'd1 << sel;
                    if (t == (1 << 12)) begin gsnap <= g2; t <= 0; st <= S_START; end
                end
                S_START: if (t == 9'd256) begin bstart <= b; t <= 0; st <= S_WIN; end
                S_WIN: if (t == (1 << WIN)) begin gsnap <= g2; t <= 0; st <= S_END; end
                default: if (t == 9'd256) begin
                    cnt[sel] <= b - bstart;
                    t <= 0; st <= S_SW;
                    if (sel == 6'd63) begin
                        sel <= 6'd0; frame <= frame + 1'b1; copy <= 1'b1;
                    end else
                        sel <= sel + 1'b1;
                end
            endcase
        end
    end
endmodule

// One grid ring: LUT2 (en & ~fb) + 3 LUT1 buffers in the A..D 6-LUTs of one
// slice, plus a local /4 (clkbuf_inhibit keeps yosys from putting BUFGs on it).
module grid_ring #(parameter LOC = "SLICE_X0Y0") (input wire en, output wire out);
    (* keep *) wire n0, n1, n2;
    (* keep, clkbuf_inhibit *) wire n3;
    (* keep, BEL = {LOC, "/A6LUT"} *) LUT2 #(.INIT(4'b0100)) s0 (.O(n0), .I0(n3), .I1(en));
    (* keep, BEL = {LOC, "/B6LUT"} *) LUT1 #(.INIT(2'b10))   s1 (.O(n1), .I0(n0));
    (* keep, BEL = {LOC, "/C6LUT"} *) LUT1 #(.INIT(2'b10))   s2 (.O(n2), .I0(n1));
    (* keep, BEL = {LOC, "/D6LUT"} *) LUT1 #(.INIT(2'b10))   s3 (.O(n3), .I0(n2));
    (* clkbuf_inhibit *) reg d1 = 1'b0;
    (* clkbuf_inhibit *) reg d2 = 1'b0;
    always @(posedge n3) d1 <= ~d1;
    always @(posedge d1) d2 <= ~d2;
    assign out = d2;
endmodule

`default_nettype wire
