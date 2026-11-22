`default_nettype none

// One LUT ring oscillator plus its frequency counter.
// Ring: LUT2 NAND-style enable stage (out = en & ~fb) followed by STAGES-1 LUT1
// buffers, all pinned to one slice so the ring delay is local to that site. A
// toggle FF divides by 2, a 24-bit Gray counter runs in that domain, and the
// 50 MHz side samples it through a 2-FF synchroniser and differences it every
// 2^20 clocks.
module ro_monitor #(
    parameter LOC  = "SLICE_X0Y0",
    parameter DLOC = ""          // playground: slice for the first /2 stage, the
                                 // other slice of LOC's CLB tile ("" = unpinned)
) (
    input  wire        clk,
    input  wire        en,
    output reg  [23:0] count = 24'd0,
    output wire        ring        // raw ring signal (TDC source)
);
    // clkbuf_inhibit: n3 also clocks the toggle FF, and yosys would otherwise
    // insert a BUFG on it -- putting the global clock tree inside the ring.
    (* keep *) wire n0, n1, n2;
    (* keep, clkbuf_inhibit *) wire n3;
    assign ring = n3;
    (* keep, BEL = {LOC, "/A6LUT"} *) LUT2 #(.INIT(4'b0100)) s0 (.O(n0), .I0(n3), .I1(en));
    (* keep, BEL = {LOC, "/B6LUT"} *) LUT1 #(.INIT(2'b10))   s1 (.O(n1), .I0(n0));
    (* keep, BEL = {LOC, "/C6LUT"} *) LUT1 #(.INIT(2'b10))   s2 (.O(n2), .I0(n1));
    (* keep, BEL = {LOC, "/D6LUT"} *) LUT1 #(.INIT(2'b10))   s3 (.O(n3), .I0(n2));

    // RO domain. playground: divide by 4 next to the ring before the counter
    // (like fpga/pvt-lab ro_grid.v). A /2 signal at 200-300 MHz routed as a
    // clock to the counter swallowed edges in some builds (ro1 read 492, ~300,
    // 0, 490, 0 MHz across builds). count is doubled below so the line format
    // and f = 2*count*50e6/2^20 stay the same (1 LSB of ~2.5M lost).
    // The first stage (clocked by the 500-600 MHz ring itself) is pinned into
    // the paired slice of the ring's CLB tile when DLOC is set: it can't share
    // the ring's slice (one clock per slice, and the ring's LUTs are taken),
    // but the paired slice is the shortest route the fabric has.
    (* clkbuf_inhibit *) wire half;
    (* clkbuf_inhibit *) reg quarter = 1'b0;
    generate if (DLOC != "") begin : pin
        wire half_n;
        (* keep, BEL = {DLOC, "/A6LUT"} *) LUT1 #(.INIT(2'b01)) inv (.O(half_n), .I0(half));
        (* keep, BEL = {DLOC, "/AFF"} *) FDRE #(.INIT(1'b0)) ff (.C(n3), .CE(1'b1), .R(1'b0), .D(half_n), .Q(half));
    end else begin : free
        reg h = 1'b0;
        always @(posedge n3) h <= ~h;
        assign half = h;
    end endgenerate
    always @(posedge half) quarter <= ~quarter;
    reg [23:0] bin = 24'd0, gray = 24'd0;
    always @(posedge quarter) begin
        bin  <= bin + 1'b1;
        gray <= bin ^ (bin >> 1);
    end

    // clk domain
    (* ASYNC_REG = "TRUE" *) reg [23:0] g1 = 24'd0, g2 = 24'd0;
    reg [23:0] prev = 24'd0;
    reg [19:0] gate = 20'd0;
    // Snapshot the synchronised Gray code once per gate and decode it from the
    // snapshot, which then holds still for 2^20 cycles. Decoding g2 directly
    // is a 24-deep XOR chain re-evaluated every cycle, and in a congested
    // design it misses 20 ns and corrupts the count.
    reg [23:0] gsnap = 24'd0;
    reg [23:0] b;
    integer i;
    always @* begin
        b[23] = gsnap[23];
        for (i = 22; i >= 0; i = i - 1) b[i] = b[i+1] ^ gsnap[i];
    end
    always @(posedge clk) begin
        g1 <= gray;
        g2 <= g1;
        gate <= gate + 1'b1;
        if (&gate) gsnap <= g2;
        if (gate == 20'd255) begin       // decode has had 256 cycles to settle
            count <= (b - prev) << 1;
            prev  <= b;
        end
    end
endmodule

`default_nettype wire
