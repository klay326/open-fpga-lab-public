// IJTAG building blocks: generic TDR and SIB
// Bit order: sr[0] is nearest TDO. Shift = {si, sr[N-1:1]}.
`default_nettype none

// Test Data Register with optional update (shadow) stage.
//   sel     : this register is on the active scan path
//   cap_val : loaded in Capture-DR (ctrl regs feed back q; status regs feed instrument data)
//   q       : update-stage outputs (instrument controls); glitch-free during shift
//   sr      : raw shift stage (used directly by the scan-chain TDR)
module tdr #(
    parameter N = 8,
    parameter [N-1:0] RST = {N{1'b0}}
) (
    input  wire         tck,
    input  wire         rst_n,
    input  wire         tlr,
    input  wire         sel,
    input  wire         capture,
    input  wire         shift,
    input  wire         update,
    input  wire         si,
    output wire         so,
    input  wire [N-1:0] cap_val,
    output reg  [N-1:0] q,
    output reg  [N-1:0] sr
);
    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
            sr <= {N{1'b0}};
            q  <= RST;
        end else begin
            if (sel & capture)    sr <= cap_val;
            else if (sel & shift) sr <= {si, sr[N-1:1]};
            if (tlr)                 q <= RST;
            else if (sel & update)   q <= sr;
        end
    end
    assign so = sr[0];
endmodule

// Segment Insertion Bit (IEEE 1687).
//   Closed (u=0): si -> [S] -> so          (segment frozen)
//   Open   (u=1): si -> segment -> [S] -> so
// force_closed / force_open model a broken SIB (Fault Hunt level 4 "network" fault).
module sib (
    input  wire tck,
    input  wire rst_n,
    input  wire tlr,
    input  wire sel,          // parent path selected
    input  wire capture,
    input  wire shift,
    input  wire update,
    input  wire si,
    input  wire seg_so,       // last bit of the hosted segment
    input  wire force_closed,
    input  wire force_open,
    output wire so,
    output wire seg_sel       // hosted segment is on the active path
);
    reg s, u;
    wire u_eff = force_open | (u & ~force_closed);

    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
            s <= 1'b0;
            u <= 1'b0;
        end else begin
            if (sel & capture)    s <= u_eff;
            else if (sel & shift) s <= u_eff ? seg_so : si;
            if (tlr)                u <= 1'b0;
            else if (sel & update)  u <= s;
        end
    end
    assign so      = s;
    assign seg_sel = sel & u_eff;
endmodule
