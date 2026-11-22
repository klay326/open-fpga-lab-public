// Fault Hunt: 4-bit ALU with 16 injectable fault sites, key-driven fault
// generator, and answer checker.
//
// Fault types (3b): 0 SA0, 1 SA1, 2 slow-to-rise, 3 slow-to-fall,
//                   4 bridge (wired-AND of a site pair), 5 SIB stuck-closed,
//                   6 SIB stuck-open (types 5/6 act on the IJTAG network, not the ALU)
//
// Fault sites:  0-3 s0..s3 (adder sums)   4-7 c1..c4 (carries)
//               8 and[1]  9 or[2]  10 xor[0]  11 SUB decode
//               12 shl[1] 13 Z flag 14 V flag 15 y[3]
// Bridge pairs: (0,1) (2,3) (8,9)
`default_nettype none

// One fault site. All decoding (site match, type) is done once in fault_hunt;
// each site only gets its one-hot hit bits and shared type lines.
module fsite (
    input  wire v,        // fault-free value
    input  wire partner,  // fault-free value of bridge partner (ID^1)
    input  wire prev,     // value of this net at previous capture (transition faults)
    input  wire hit1,     // fault 1 targets this site
    input  wire hit2,     // fault 2 (stuck-at) targets this site
    input  wire t1_sa1, t1_str, t1_stf, t1_br,   // fault-1 type (SA0 = none of these)
    input  wire t2_sa1,
    output wire o
);
    wire f1v = t1_sa1 | (t1_str & v & prev) | (t1_stf & (v | prev)) | (t1_br & v & partner);
    wire o1  = hit1 ? f1v : v;
    assign o = hit2 ? t2_sa1 : o1;
endmodule

module fault_hunt (
    input  wire       tck,
    input  wire       rst_n,
    input  wire       tlr,
    // config (FH_CFG update stage)
    input  wire [7:0] key,
    input  wire       fh_en,
    input  wire       cfg_upd,      // FH_CFG Update-DR pulse (clears progress)
    // ALU operands (from scan chain or BIST)
    input  wire [3:0] a,
    input  wire [3:0] b,
    input  wire [2:0] op,
    input  wire       step,         // capture / BIST step: latch prev for transition fault
    output wire [3:0] y,
    output wire       c, z, n, v,
    // answer (FH_ANS update stage)
    input  wire [3:0] ans_site,
    input  wire [2:0] ans_type,
    input  wire       ans_submit,
    input  wire       ans_upd,      // FH_ANS Update-DR pulse
    output wire       pass,
    output wire [1:0] level,
    // network faults
    output wire       sib_b_closed, sib_b_open, sib_c_closed, sib_c_open
);
    // ---------------- key -> fault generator ----------------
    wire [7:0] h  = key ^ 8'hA7;
    wire [7:0] h2 = {h[2:0], h[7:3]} ^ 8'h3C;
    wire [1:0] lvl = key[7:6];

    reg [3:0] f1_site, f2_site;
    reg [2:0] f1_type, f2_type;
    always @(*) begin
        f1_site = h[3:0];
        f1_type = {2'b00, h[4]};
        f2_site = (h2[3:0] == h[3:0]) ? (h2[3:0] ^ 4'd1) : h2[3:0];
        f2_type = {2'b00, h2[4]};
        case (lvl)
            2'd2: f1_type = {2'b01, h[4]};                 // transition
            2'd3: if (!h[5]) begin                          // bridge
                      f1_type = 3'd4;
                      case (h[1:0])
                          2'd1: f1_site = 4'd2;
                          2'd2: f1_site = 4'd8;
                          default: f1_site = 4'd0;
                      endcase
                  end else begin                            // broken SIB
                      f1_type = 3'd5 + {2'b00, h[1]};
                      f1_site = 4'd1 + {3'b000, h[0]};      // 1 = SIB_B, 2 = SIB_C
                  end
            default: ;
        endcase
    end
    wire f1_en  = fh_en;
    wire f2_en  = fh_en & (lvl == 2'd1);
    wire net_en = f1_en & (f1_type >= 3'd5);

    assign sib_b_closed = net_en & (f1_site == 4'd1) & (f1_type == 3'd5);
    assign sib_b_open   = net_en & (f1_site == 4'd1) & (f1_type == 3'd6);
    assign sib_c_closed = net_en & (f1_site == 4'd2) & (f1_type == 3'd5);
    assign sib_c_open   = net_en & (f1_site == 4'd2) & (f1_type == 3'd6);
    assign level        = fh_en ? lvl : 2'd0;

    // shared fault decode (one-hot site hits + type lines)
    wire        f1_alu = f1_en & (f1_type <= 3'd4);
    wire [15:0] dec1   = 16'd1 << f1_site;
    wire [15:0] pair1  = {dec1[14], dec1[15], dec1[12], dec1[13], dec1[10], dec1[11], dec1[8], dec1[9],
                          dec1[6],  dec1[7],  dec1[4],  dec1[5],  dec1[2],  dec1[3],  dec1[0], dec1[1]};
    wire        t1_br  = (f1_type == 3'd4);
    wire [15:0] hit1   = f1_alu ? (t1_br ? (dec1 | pair1) : dec1) : 16'd0;
    wire [15:0] hit2   = f2_en ? (16'd1 << f2_site) : 16'd0;
    wire        t1_sa1 = (f1_type == 3'd1);
    wire        t1_str = (f1_type == 3'd2);
    wire        t1_stf = (f1_type == 3'd3);
    wire        t2_sa1 = f2_type[0];

    // ---------------- ALU with fault sites ----------------
    wire [15:0] raw;     // fault-free value of every site
    reg  prev;
    always @(posedge tck or negedge rst_n)
        if (!rst_n)     prev <= 1'b0;
        else if (step)  prev <= raw[f1_site];

    `define FS(FID, VAL, PART, OUT) \
        fsite fs_``OUT (.v(VAL), .partner(PART), .prev(prev), \
            .hit1(hit1[FID]), .hit2(hit2[FID]), .t1_sa1(t1_sa1), .t1_str(t1_str), \
            .t1_stf(t1_stf), .t1_br(t1_br), .t2_sa1(t2_sa1), .o(OUT));

    // SUB decode
    wire sub_raw = (op == 3'b001);
    wire sub;
    `FS(4'd11, sub_raw, 1'b1, sub)
    wire [3:0] bx  = b ^ {4{sub}};
    wire       cin = sub;

    // ripple adder
    wire s0_raw = a[0] ^ bx[0] ^ cin;
    wire c1_raw = (a[0] & bx[0]) | (cin & (a[0] ^ bx[0]));
    wire s0, c1;
    wire s1_raw_pre = a[1] ^ bx[1];
    wire s1_raw = s1_raw_pre ^ c1;
    `FS(4'd0, s0_raw, s1_raw, s0)
    `FS(4'd4, c1_raw, 1'b1, c1)
    wire c2_raw = (a[1] & bx[1]) | (c1 & s1_raw_pre);
    wire s1, c2;
    `FS(4'd1, s1_raw, s0_raw, s1)
    `FS(4'd5, c2_raw, 1'b1, c2)
    wire s2_pre = a[2] ^ bx[2];
    wire s2_raw = s2_pre ^ c2;
    wire c3_raw = (a[2] & bx[2]) | (c2 & s2_pre);
    wire s3_pre = a[3] ^ bx[3];
    wire c3;
    `FS(4'd6, c3_raw, 1'b1, c3)
    wire s3_raw = s3_pre ^ c3;
    wire c4_raw = (a[3] & bx[3]) | (c3 & s3_pre);
    wire s2, s3, c4;
    `FS(4'd2, s2_raw, s3_raw, s2)
    `FS(4'd3, s3_raw, s2_raw, s3)
    `FS(4'd7, c4_raw, 1'b1, c4)
    wire [3:0] sum = {s3, s2, s1, s0};

    // logic ops
    wire and1_raw = a[1] & b[1];
    wire or2_raw  = a[2] | b[2];
    wire xor0_raw = a[0] ^ b[0];
    wire and1, or2, xor0;
    `FS(4'd8,  and1_raw, or2_raw,  and1)
    `FS(4'd9,  or2_raw,  and1_raw, or2)
    `FS(4'd10, xor0_raw, 1'b1,     xor0)
    wire [3:0] and_v = {a[3] & b[3], a[2] & b[2], and1, a[0] & b[0]};
    wire [3:0] or_v  = {a[3] | b[3], or2, a[1] | b[1], a[0] | b[0]};
    wire [3:0] xor_v = {a[3] ^ b[3], a[2] ^ b[2], a[1] ^ b[1], xor0};
    wire shl1;
    `FS(4'd12, a[0], 1'b1, shl1)
    wire [3:0] shl_v = {a[2], a[1], shl1, 1'b0};

    reg [3:0] y_raw;
    always @(*) begin
        case (op)
            3'd0, 3'd1: y_raw = sum;
            3'd2:       y_raw = and_v;
            3'd3:       y_raw = or_v;
            3'd4:       y_raw = xor_v;
            3'd5:       y_raw = ~a;
            3'd6:       y_raw = shl_v;
            default:    y_raw = b;
        endcase
    end
    wire y3;
    `FS(4'd15, y_raw[3], 1'b1, y3)
    assign y = {y3, y_raw[2:0]};

    wire is_arith = (op[2:1] == 2'b00);
    assign c = is_arith & c4;
    wire z_raw = (y == 4'd0);
    `FS(4'd13, z_raw, 1'b1, z)
    assign n = y[3];
    wire v_raw = is_arith & (a[3] == bx[3]) & (s3 != a[3]);
    `FS(4'd14, v_raw, 1'b1, v)
    `undef FS

    assign raw = {y_raw[3], v_raw, z_raw, a[0], sub_raw, xor0_raw, or2_raw, and1_raw,
                  c4_raw, c3_raw, c2_raw, c1_raw, s3_raw, s2_raw, s1_raw, s0_raw};

    // ---------------- answer checker ----------------
    function automatic match(input [3:0] s, input [2:0] t, input [3:0] fs, input [2:0] ft);
        match = (t == ft) & ((ft == 3'd4) ? (s[3:1] == fs[3:1]) : (s == fs));
    endfunction

    reg ans_upd_d, found1, found2;
    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
            ans_upd_d <= 1'b0; found1 <= 1'b0; found2 <= 1'b0;
        end else begin
            ans_upd_d <= ans_upd;
            if (tlr | cfg_upd) begin
                found1 <= 1'b0; found2 <= 1'b0;
            end else if (ans_upd_d & ans_submit) begin
                if (match(ans_site, ans_type, f1_site, f1_type))          found1 <= 1'b1;
                if (f2_en & match(ans_site, ans_type, f2_site, f2_type))  found2 <= 1'b1;
            end
        end
    end
    assign pass = fh_en & found1 & (found2 | ~f2_en);
endmodule
