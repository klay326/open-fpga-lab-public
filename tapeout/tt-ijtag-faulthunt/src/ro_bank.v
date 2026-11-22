// Ring-oscillator bank (process / temperature monitor).
// 4 rings, one enabled at a time, counted over a TCK-timed gate.
//
//   TCK domain : gate timer, start/clear, done flag
//   RO domain  : 4-bit ripple prescaler (/16) -> 16-bit counter
// The ring only runs during the gate, so the count is static when it is
// captured into RO_CNT (done=1) -- no live clock-domain crossing.
//
// SYNTHESIS NOTE: rings are combinational loops. Before hardening, replace
// ro_stage's generic gates with hand-instantiated GF180 standard cells,
// keep (* keep *) on every stage net, and break the loops for STA
// (set_disable_timing). The `SIM branch is a behavioral delay model.
`default_nettype none

module ro_ring #(
    parameter STAGES = 31,       // odd
    parameter KIND   = 0,        // 0 INV x1, 1 NAND2, 2 NOR2, 3 INV x4 (cell choice at synthesis)
    parameter HALF_NS = 3        // sim-only half period
) (
    input  wire en,
    output wire out
);
`ifdef SIM
    reg r = 1'b0;
    always begin
        wait (en);
        #(HALF_NS) r = ~r;
        if (!en) r = 1'b0;
    end
    assign out = r;
`else
    // Stage 0 is a NAND enable so the ring is fully stopped when idle.
    (* keep *) wire [STAGES-1:0] n;
    assign n[0] = ~(en & n[STAGES-1]);
    genvar i;
    generate
        for (i = 1; i < STAGES; i = i + 1) begin : g_stage
            // TODO: instantiate PDK cell per KIND (inv_1 / nand2_1 with tied input /
            //       nor2_1 with tied input / inv_4).
            if (KIND == 1)      assign n[i] = ~(n[i-1] & 1'b1);
            else if (KIND == 2) assign n[i] = ~(n[i-1] | 1'b0);
            else                assign n[i] = ~n[i-1];
        end
    endgenerate
    assign out = n[STAGES-1];
`endif
endmodule

module ro_bank #(
    parameter ST_INV   = 31,
    parameter ST_NAND  = 31,
    parameter ST_NOR   = 31,
    parameter ST_INV4  = 31
) (
    input  wire        tck,
    input  wire        rst_n,
    input  wire        tlr,
    input  wire        en,
    input  wire [1:0]  sel,
    input  wire [3:0]  gate,       // gate = 2^(gate[2:0] + 8) TCK cycles
    input  wire        start,      // rising edge starts a measurement
    input  wire        free_run,   // pin: force selected ring on (scope debug)
    output wire [15:0] count,
    output reg         done,
    output wire        ro_div      // ring / 256 for a scope or counter
);
    // ---------------- TCK domain: gate timer ----------------
    reg        start_d, gating, clr;
    reg [15:0] gcnt;
    reg [2:0]  settle;
    wire [15:0] glen = 16'd1 << (gate[2:0] + 3'd0 + 4'd8);

    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
            start_d <= 1'b0; gating <= 1'b0; clr <= 1'b1;
            gcnt <= 16'd0; settle <= 3'd0; done <= 1'b0;
        end else if (tlr) begin
            start_d <= 1'b0; gating <= 1'b0; clr <= 1'b1; done <= 1'b0; settle <= 3'd0;
        end else begin
            start_d <= start;
            clr     <= 1'b0;
            if (start & ~start_d) begin
                clr <= 1'b1; gating <= 1'b1; gcnt <= 16'd0; done <= 1'b0; settle <= 3'd0;
            end else if (gating) begin
                gcnt <= gcnt + 16'd1;
                if (gcnt == glen - 16'd1) begin
                    gating <= 1'b0;
                    settle <= 3'd4;
                end
            end else if (settle != 3'd0) begin
                settle <= settle - 3'd1;
                if (settle == 3'd1) done <= 1'b1;
            end
        end
    end

    // ---------------- rings ----------------
    wire run = (gating & en) | free_run;
    wire [3:0] ro;
    ro_ring #(.STAGES(ST_INV),  .KIND(0), .HALF_NS(3)) u_r0 (.en(run & (sel == 2'd0)), .out(ro[0]));
    ro_ring #(.STAGES(ST_NAND), .KIND(1), .HALF_NS(4)) u_r1 (.en(run & (sel == 2'd1)), .out(ro[1]));
    ro_ring #(.STAGES(ST_NOR),  .KIND(2), .HALF_NS(5)) u_r2 (.en(run & (sel == 2'd2)), .out(ro[2]));
    ro_ring #(.STAGES(ST_INV4), .KIND(3), .HALF_NS(2)) u_r3 (.en(run & (sel == 2'd3)), .out(ro[3]));
    wire ro_clk = ro[sel];

    // ---------------- RO domain: prescaler + counter ----------------
    reg [3:0] pre;     // ripple /16
    always @(posedge ro_clk or posedge clr) if (clr) pre[0] <= 1'b0; else pre[0] <= ~pre[0];
    always @(negedge pre[0] or posedge clr) if (clr) pre[1] <= 1'b0; else pre[1] <= ~pre[1];
    always @(negedge pre[1] or posedge clr) if (clr) pre[2] <= 1'b0; else pre[2] <= ~pre[2];
    always @(negedge pre[2] or posedge clr) if (clr) pre[3] <= 1'b0; else pre[3] <= ~pre[3];

    reg [15:0] cnt;
    always @(negedge pre[3] or posedge clr)
        if (clr) cnt <= 16'd0;
        else     cnt <= cnt + 16'd1;

    assign count  = cnt;
    assign ro_div = cnt[3];   // ring / 256
endmodule
