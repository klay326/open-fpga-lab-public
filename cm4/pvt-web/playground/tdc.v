// Carry-chain time-to-digital converter used as an on-chip delay monitor.
//
// `sig` (a free-running ring oscillator, asynchronous to clk) runs up a chain
// of NC CARRY4s in propagate mode (S=1111, DI=0), so tap k is `sig` delayed
// by k carry stages. clk snapshots all 4*NC taps. In one snapshot, consecutive
// 0->1 transitions along the chain are one RO period apart, so
//
//     taps_per_period = T_RO / tau_tap   ->   tau_tap = 1 / (f_RO * taps)
//
// Each snapshot is scanned bit-serially (one tap per clock, with a 3-tap
// majority filter against the usual FPGA TDC "bubbles"). The distance between
// the first two rising transitions is summed over 2^AVG valid snapshots and
// published in `sum`. Mean taps per period = sum / 2^AVG.
// Only the root CARRY4 is pinned; nextpnr places the rest of the chain above it.

`default_nettype none

module tdc #(
    parameter LOC = "SLICE_X82Y100",
    parameter NC  = 64,
    parameter AVG = 12
) (
    input  wire        clk,
    input  wire        sig,
    output reg  [23:0] sum = 24'd0,
    output reg         upd = 1'b0
);
    localparam NT = 4 * NC;

    wire [NT-1:0] co;
    genvar c;
    generate for (c = 0; c < NC; c = c + 1) begin : ch
        if (c == 0) begin : root
            (* keep, BEL = {LOC, "/CARRY4"} *)
            CARRY4 cy (.CO(co[3:0]), .O(), .CI(1'b0), .CYINIT(sig), .DI(4'b0000), .S(4'b1111));
        end else begin : link
            (* keep *)
            CARRY4 cy (.CO(co[4*c+3:4*c]), .O(), .CI(co[4*c-1]), .CYINIT(1'b0), .DI(4'b0000), .S(4'b1111));
        end
    end endgenerate

    (* keep *) reg [NT-1:0] cap = {NT{1'b0}};
    (* keep *) reg [NT-1:0] cap2 = {NT{1'b0}};
    always @(posedge clk) begin
        cap  <= co;
        cap2 <= cap;        // second stage: metastability settling
    end

    // bit-serial scanner
    reg [NT-1:0] sh = {NT{1'b0}};
    reg [8:0]    pos = 9'd0;
    reg          scanning = 1'b0;
    reg          prev = 1'b1;
    reg [8:0]    p1 = 9'd0;
    reg [1:0]    nfound = 2'd0;
    reg [23:0]   acc = 24'd0;
    reg [AVG:0]  n = 0;
    wire         maj = (sh[0] & sh[1]) | (sh[0] & sh[2]) | (sh[1] & sh[2]);

    always @(posedge clk) begin
        upd <= 1'b0;
        if (!scanning) begin
            sh <= cap2; pos <= 9'd0; prev <= 1'b1; nfound <= 2'd0; scanning <= 1'b1;
        end else begin
            sh   <= {1'b0, sh[NT-1:1]};
            pos  <= pos + 1'b1;
            prev <= maj;
            if (!prev && maj && pos != 0) begin
                if (nfound == 2'd0) begin p1 <= pos; nfound <= 2'd1; end
                else if (nfound == 2'd1) begin
                    acc <= acc + (pos - p1);
                    nfound <= 2'd2;
                end
            end
            if (pos == NT - 3 || nfound == 2'd2) begin
                scanning <= 1'b0;
                if (nfound == 2'd2) begin
                    if (n == (1 << AVG) - 1) begin
                        sum <= acc; acc <= 24'd0; n <= 0; upd <= 1'b1;
                    end else
                        n <= n + 1'b1;
                end
            end
        end
    end
endmodule

`default_nettype wire
