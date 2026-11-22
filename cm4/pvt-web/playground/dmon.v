// dmon -- per-cycle supply droop / glitch detector (delay-line monitor).
//
// Every clock a launch FF toggles. Its edge runs through a LUT delay line
// (NL buffers; `sel` picks the tap, so the host can centre the edge) and then
// up a CARRY4 chain of 4*NC taps. The next clock edge captures the chain, so
// the number of taps the new value has reached, `pos`, measures the path delay
// in this cycle:
//     VCCINT droop  -> gates slower -> pos drops
//     overshoot / clock or voltage glitch -> pos rises
// That is the structure of the "critical path monitor" / droop detector used
// on processors. Only rising launches are counted (rise and fall delays differ).
//
// Per window of 2^WIN rising samples: min, max and sum of pos are published.
// Alarms latch whenever pos < thr_lo (droop) or pos > thr_hi (glitch); `clr`
// clears them. Thresholds of 0 / 0x1FF disable them.

`default_nettype none

module dmon #(
    parameter LOC = "SLICE_X86Y200",
    parameter NL  = 64,
    parameter NC  = 64,
    parameter WIN = 15
) (
    input  wire        clk,
    input  wire [5:0]  sel,
    input  wire [8:0]  thr_lo,
    input  wire [8:0]  thr_hi,
    input  wire        clr,
    output reg  [8:0]  wmin = 9'h1FF,
    output reg  [8:0]  wmax = 9'h000,
    output reg  [23:0] wsum = 24'd0,
    output reg         alarm_lo = 1'b0,
    output reg         alarm_hi = 1'b0,
    output wire [8:0]  pos_o,       // per-cycle position (for scope.v)
    output wire        vld_o
);
    localparam NT = 4 * NC;

    // launch
    reg l = 1'b0;
    always @(posedge clk) l <= ~l;

    // LUT delay line
    (* keep *) wire [NL-1:0] d;
    (* keep *) LUT1 #(.INIT(2'b10)) dl0 (.O(d[0]), .I0(l));
    genvar i;
    generate for (i = 1; i < NL; i = i + 1) begin : dl
        (* keep *) LUT1 #(.INIT(2'b10)) b (.O(d[i]), .I0(d[i-1]));
    end endgenerate
    wire tap = d[sel];

    // carry chain
    wire [NT-1:0] co;
    genvar c;
    generate for (c = 0; c < NC; c = c + 1) begin : ch
        if (c == 0) begin : root
            (* keep, BEL = {LOC, "/CARRY4"} *)
            CARRY4 cy (.CO(co[3:0]), .O(), .CI(1'b0), .CYINIT(tap), .DI(4'b0000), .S(4'b1111));
        end else begin : link
            (* keep *)
            CARRY4 cy (.CO(co[4*c+3:4*c]), .O(), .CI(co[4*c-1]), .CYINIT(1'b0), .DI(4'b0000), .S(4'b1111));
        end
    end endgenerate

    // capture: at edge k+1 the chain holds the value launched at edge k (= l now)
    (* keep *) reg [NT-1:0] cap = {NT{1'b0}};
    reg lk = 1'b0;
    always @(posedge clk) begin cap <= co; lk <= l; end

    // normalise (ones = taps reached by the new value) + pipelined popcount
    reg [NT-1:0] t = {NT{1'b0}};
    reg          rise1 = 1'b0;
    always @(posedge clk) begin t <= lk ? cap : ~cap; rise1 <= lk; end

    function [4:0] pc16;
        input [15:0] v;
        integer k;
        begin pc16 = 0; for (k = 0; k < 16; k = k + 1) pc16 = pc16 + v[k]; end
    endfunction
    reg [4:0] p16 [0:NC/4-1];
    reg       rise2 = 1'b0;
    integer   m;
    always @(posedge clk) begin
        for (m = 0; m < NC/4; m = m + 1) p16[m] <= pc16(t[16*m +: 16]);
        rise2 <= rise1;
    end
    reg [8:0] pos = 9'd0;
    reg       rise3 = 1'b0;
    reg [8:0] acc;
    always @* begin
        acc = 9'd0;
        for (m = 0; m < NC/4; m = m + 1) acc = acc + p16[m];
    end
    always @(posedge clk) begin pos <= acc; rise3 <= rise2; end
    assign pos_o = pos;
    assign vld_o = rise3;

    // window statistics + alarms
    reg [8:0]  cmin = 9'h1FF, cmax = 9'h000;
    reg [23:0] csum = 24'd0;
    reg [WIN-1:0] n = 0;
    always @(posedge clk) begin
        if (clr) begin alarm_lo <= 1'b0; alarm_hi <= 1'b0; end
        if (rise3) begin
            if (pos < thr_lo) alarm_lo <= 1'b1;
            if (pos > thr_hi) alarm_hi <= 1'b1;
            n <= n + 1'b1;
            if (&n) begin
                wmin <= (pos < cmin) ? pos : cmin;
                wmax <= (pos > cmax) ? pos : cmax;
                wsum <= csum + pos;
                cmin <= 9'h1FF; cmax <= 9'h000; csum <= 24'd0;
            end else begin
                if (pos < cmin) cmin <= pos;
                if (pos > cmax) cmax <= pos;
                csum <= csum + pos;
            end
        end
    end
endmodule

`default_nettype wire
