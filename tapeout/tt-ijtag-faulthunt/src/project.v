// IJTAG Fault Hunt -- Tiny Tapeout top level
//
// Pins
//   clk = TCK, rst_n = TRST_N
//   ui_in[0] TMS, ui_in[1] TDI, ui_in[2] RO_FREE_RUN
//   uo_out[0] TDO, [1] FH_PASS, [3:2] FH_LEVEL, [4] BIST_DONE,
//   uo_out[5] BIST_PASS, [6] RO_DIV (ring/256), [7] TAP_RTI
//
// Instructions (4-bit IR): 0x8 IJTAG_ACCESS, 0xA FH_DIRECT, 0xF BYPASS (others -> BYPASS)
//
// IJTAG network, TDI -> TDO (a SIB's segment sits before the SIB bit):
//   [FH_CFG 12][FH_SCAN 19][SIB_A1][FH_ANS 8][SIB_A]
//   [BIST_CTRL 4][BIST_SIG 16][SIB_B]
//   [RO_CTRL 8][RO_CNT 16][SIB_C]                      all closed = 3b, all open = 87b
`default_nettype none

module tt_um_klay_ijtag_faulthunt #(
    parameter [15:0] BIST_GOLDEN = 16'h03B5   // fault-free MISR, seed0/len0 (from test/model.py)
) (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
    wire tck = clk;
    wire tms = ui_in[0];
    wire tdi = ui_in[1];
    wire ro_free_run = ui_in[2];

    // ---------------- TAP ----------------
    wire [3:0] ir;
    wire tlr, rti, cap, sh, upd, tdo, dr_tdo;
    tap u_tap (
        .tck(tck), .trst_n(rst_n), .tms(tms), .tdi(tdi), .dr_tdo(dr_tdo), .tdo(tdo),
        .ir(ir), .tlr(tlr), .rti(rti), .capture_dr(cap), .shift_dr(sh), .update_dr(upd)
    );

    wire sel_ijtag  = (ir == 4'h8);
    wire sel_direct = (ir == 4'hA);

    reg bypass_ff;
    always @(posedge tck or negedge rst_n)
        if (!rst_n)                 bypass_ff <= 1'b0;
        else if (cap)               bypass_ff <= 1'b0;
        else if (sh)                bypass_ff <= tdi;

    // ---------------- instrument wires ----------------
    wire [3:0] y; wire fc, fz, fn, fv;
    wire [7:0] fh_resp = {y, fc, fz, fn, fv};
    wire fh_pass; wire [1:0] fh_level;
    wire sib_b_closed, sib_b_open, sib_c_closed, sib_c_open;

    wire bist_running, bist_done; wire [3:0] bist_a, bist_b; wire [2:0] bist_op;
    wire [15:0] misr;
    wire [15:0] ro_count; wire ro_done, ro_div;

    // ---------------- SIB_A segment ----------------
    wire selA, selA1;
    wire cfg_so, scan_so, a1_so, ans_so, sibA_so;
    wire [11:0] cfg_q, cfg_sr;
    wire [18:0] scan_q, scan_sr;
    wire [7:0]  ans_q, ans_sr;

    // FH_CFG: {rsvd[2:0], fh_en, key[7:0]}
    tdr #(.N(12)) u_fh_cfg (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(selA), .capture(cap), .shift(sh), .update(upd),
        .si(tdi), .so(cfg_so), .cap_val(cfg_q), .q(cfg_q), .sr(cfg_sr));

    // FH_SCAN: the ALU scan chain. {A[3:0], B[3:0], OP[2:0], Y[3:0], C, Z, N, V}
    // Operand bits are scan flops with no functional input (they hold in Capture);
    // result bits capture the ALU output.
    wire sel_scan = selA1 | (sel_direct);
    wire scan_si  = sel_direct ? tdi : cfg_so;
    tdr #(.N(19)) u_fh_scan (
        .tck(tck), .rst_n(rst_n), .tlr(1'b0), .sel(sel_scan), .capture(cap), .shift(sh), .update(1'b0),
        .si(scan_si), .so(scan_so), .cap_val({scan_sr[18:8], fh_resp}), .q(scan_q), .sr(scan_sr));

    sib u_sib_a1 (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(selA), .capture(cap), .shift(sh), .update(upd),
        .si(cfg_so), .seg_so(scan_so), .force_closed(1'b0), .force_open(1'b0),
        .so(a1_so), .seg_sel(selA1));

    // FH_ANS: {submit, type[2:0], site[3:0]}
    tdr #(.N(8)) u_fh_ans (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(selA), .capture(cap), .shift(sh), .update(upd),
        .si(a1_so), .so(ans_so), .cap_val({fh_pass, ans_q[6:0]}), .q(ans_q), .sr(ans_sr));

    sib u_sib_a (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(sel_ijtag), .capture(cap), .shift(sh), .update(upd),
        .si(tdi), .seg_so(ans_so), .force_closed(1'b0), .force_open(1'b0),
        .so(sibA_so), .seg_sel(selA));

    // ---------------- SIB_B segment ----------------
    wire selB, bctl_so, bsig_so, sibB_so;
    wire [3:0] bctl_q, bctl_sr;
    wire [15:0] bsig_q, bsig_sr;

    // BIST_CTRL: {len[1:0], seed_sel, start}; capture returns done in bit 0
    tdr #(.N(4)) u_bist_ctrl (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(selB), .capture(cap), .shift(sh), .update(upd),
        .si(sibA_so), .so(bctl_so), .cap_val({bctl_q[3:1], bist_done}), .q(bctl_q), .sr(bctl_sr));

    tdr #(.N(16)) u_bist_sig (
        .tck(tck), .rst_n(rst_n), .tlr(1'b0), .sel(selB), .capture(cap), .shift(sh), .update(1'b0),
        .si(bctl_so), .so(bsig_so), .cap_val(misr), .q(bsig_q), .sr(bsig_sr));

    sib u_sib_b (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(sel_ijtag), .capture(cap), .shift(sh), .update(upd),
        .si(sibA_so), .seg_so(bsig_so), .force_closed(sib_b_closed), .force_open(sib_b_open),
        .so(sibB_so), .seg_sel(selB));

    // ---------------- SIB_C segment ----------------
    wire selC, rctl_so, rcnt_so, sibC_so;
    wire [7:0] rctl_q, rctl_sr;
    wire [15:0] rcnt_q, rcnt_sr;

    // RO_CTRL: {start, gate[3:0], sel[1:0], en}; capture returns done in bit 7
    tdr #(.N(8)) u_ro_ctrl (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(selC), .capture(cap), .shift(sh), .update(upd),
        .si(sibB_so), .so(rctl_so), .cap_val({ro_done, rctl_q[6:0]}), .q(rctl_q), .sr(rctl_sr));

    tdr #(.N(16)) u_ro_cnt (
        .tck(tck), .rst_n(rst_n), .tlr(1'b0), .sel(selC), .capture(cap), .shift(sh), .update(1'b0),
        .si(rctl_so), .so(rcnt_so), .cap_val(ro_count), .q(rcnt_q), .sr(rcnt_sr));

    sib u_sib_c (
        .tck(tck), .rst_n(rst_n), .tlr(tlr), .sel(sel_ijtag), .capture(cap), .shift(sh), .update(upd),
        .si(sibB_so), .seg_so(rcnt_so), .force_closed(sib_c_closed), .force_open(sib_c_open),
        .so(sibC_so), .seg_sel(selC));

    // ---------------- DR output mux ----------------
    assign dr_tdo = sel_ijtag  ? sibC_so :
                    sel_direct ? scan_so : bypass_ff;

    // ---------------- instruments ----------------
    wire [3:0] alu_a  = bist_running ? bist_a  : scan_sr[18:15];
    wire [3:0] alu_b  = bist_running ? bist_b  : scan_sr[14:11];
    wire [2:0] alu_op = bist_running ? bist_op : scan_sr[10:8];
    wire fh_step = (sel_scan & cap) | bist_running;

    fault_hunt u_fh (
        .tck(tck), .rst_n(rst_n), .tlr(tlr),
        .key(cfg_q[7:0]), .fh_en(cfg_q[8]), .cfg_upd(selA & upd),
        .a(alu_a), .b(alu_b), .op(alu_op), .step(fh_step),
        .y(y), .c(fc), .z(fz), .n(fn), .v(fv),
        .ans_site(ans_q[3:0]), .ans_type(ans_q[6:4]), .ans_submit(ans_q[7]), .ans_upd(selA & upd),
        .pass(fh_pass), .level(fh_level),
        .sib_b_closed(sib_b_closed), .sib_b_open(sib_b_open),
        .sib_c_closed(sib_c_closed), .sib_c_open(sib_c_open));

    bist u_bist (
        .tck(tck), .rst_n(rst_n), .tlr(tlr),
        .start(bctl_q[0]), .seed_sel(bctl_q[1]), .len(bctl_q[3:2]), .resp(fh_resp),
        .running(bist_running), .done(bist_done),
        .a(bist_a), .b(bist_b), .op(bist_op), .misr(misr));

    wire bist_pass = bist_done & (misr == BIST_GOLDEN) & ~bctl_q[1] & (bctl_q[3:2] == 2'd0);

    ro_bank u_ro (
        .tck(tck), .rst_n(rst_n), .tlr(tlr),
        .en(rctl_q[0]), .sel(rctl_q[2:1]), .gate(rctl_q[6:3]), .start(rctl_q[7]),
        .free_run(ro_free_run), .count(ro_count), .done(ro_done), .ro_div(ro_div));

    // ---------------- pins ----------------
    assign uo_out  = {rti, ro_div, bist_pass, bist_done, fh_level, fh_pass, tdo};
    assign uio_out = 8'h00;
    assign uio_oe  = 8'h00;

    wire _unused = &{ena, uio_in, ui_in[7:3], scan_q, bsig_q, rcnt_q, cfg_sr[11:9],
                     cfg_q[11:9], ans_sr, bctl_sr, rctl_sr, bsig_sr, rcnt_sr, 1'b0};
endmodule
