// LFSR/MISR logic BIST for the Fault Hunt ALU.
// Galois LFSR and MISR, poly x^16 + x^5 + x^3 + x^2 + 1 (0x002D).
// One ALU pattern per TCK while running (TCK must keep toggling, e.g. in Run-Test/Idle).
`default_nettype none

module bist (
    input  wire        tck,
    input  wire        rst_n,
    input  wire        tlr,
    input  wire        start,      // BIST_CTRL.start (level; rising edge starts a run)
    input  wire        seed_sel,
    input  wire [1:0]  len,        // run length = 2^(8 + 2*len) patterns
    input  wire [7:0]  resp,       // {y[3:0], c, z, n, v}
    output reg         running,
    output reg         done,
    output wire [3:0]  a,
    output wire [3:0]  b,
    output wire [2:0]  op,
    output reg  [15:0] misr
);
    localparam [15:0] POLY  = 16'h002D;
    localparam [15:0] SEED0 = 16'hACE1;
    localparam [15:0] SEED1 = 16'h1D0F;

    reg [15:0] lfsr;
    reg [14:0] cnt;
    reg        start_d;

    wire [14:0] run_len = (len == 2'd0) ? 15'd256  :
                          (len == 2'd1) ? 15'd1024 :
                          (len == 2'd2) ? 15'd4096 : 15'd16384;

    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
            lfsr <= SEED0; misr <= 16'd0; cnt <= 15'd0;
            running <= 1'b0; done <= 1'b0; start_d <= 1'b0;
        end else if (tlr) begin
            running <= 1'b0; done <= 1'b0; start_d <= 1'b0;
        end else begin
            start_d <= start;
            if (start & ~start_d) begin
                lfsr    <= seed_sel ? SEED1 : SEED0;
                misr    <= 16'd0;
                cnt     <= run_len;
                running <= 1'b1;
                done    <= 1'b0;
            end else if (running) begin
                lfsr <= {lfsr[14:0], 1'b0} ^ (lfsr[15] ? POLY : 16'd0);
                misr <= ({misr[14:0], 1'b0} ^ (misr[15] ? POLY : 16'd0)) ^ {8'd0, resp};
                cnt  <= cnt - 15'd1;
                if (cnt == 15'd1) begin
                    running <= 1'b0;
                    done    <= 1'b1;
                end
            end
        end
    end

    assign a  = lfsr[3:0];
    assign b  = lfsr[7:4];
    assign op = lfsr[10:8];
endmodule
