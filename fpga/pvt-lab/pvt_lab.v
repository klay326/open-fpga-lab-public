// pvt_lab -- on-die thermal lab for the QMTech XC7K325T board.
//
//  * The 5 LUT ring-oscillator monitors from fpga/xadc-monitor (ro_monitor.v).
//  * An on-chip heater: HBANKS banks of rotating shift registers plus an XOR
//    stage, every FF and LUT toggling each cycle while clocked. The banks sit
//    on 6 BUFGCE-gated clocks; the duty cycle (0..255/256, PWM at 50 MHz/256,
//    phase-staggered per group) sets the dissipated power.
//  * A fabric temperature controller: an integral loop that drives the heater
//    duty so one chosen RO holds a target count. The RO is the thermometer;
//    the host turns a degC setpoint into a count using its calibration.
//  * A safety watchdog: the heater forces itself off if no valid UART command
//    arrives for 2^27 cycles (2.7 s). A crashed or unplugged host cannot leave
//    the heater running.
//
// UART (JP5, 115200 8N1). TX, every 2^24 cycles (0.34 s), hex:
//      R r0 r1 r2 r3 r4 H dd M m D ssssss ffffff P nnn xxx wwwwww A f\r\n
//   rN = RO/2 edges per 2^20-cycle gate (f = 2*rN*50e6/2^20), dd = heater duty,
//   m = mode (0 off/open loop, 1 closed loop, 2 watchdog tripped),
//   ssssss = TDC sum of taps-per-RO-period over 4096 snapshots (tdc.v),
//   ffffff = count of the TDC's own source RO (same units as rN),
//   nnn/xxx/wwwwww = droop monitor min/max/sum of tap position over 2^15
//   rising launches (dmon.v), f = alarm flags (1 droop, 2 glitch, 4 over-temp).
//   With the quiet window on, rN are the counts from the last heater-off gate.
// Other TX lines (sent between status lines):
//      G ff c0 c1 ... c63\r\n     die map frame ff: 64 x 6 hex counts, RO/2 edges
//                                in 2^18 clk (f = 2*c*50e6/2^18); index row*8+col
//      Q lll s0 ... s15\r\n       scope dump line lll (0..3FF): 16 x 4 hex samples,
//                                16 * mean droop position
// RX commands, one per line ('\n' ends it), hex arguments:
//      Hdd        open loop, heater duty = dd (00 = off)
//      Sirrrrrr   closed loop: hold RO i (0..4) at count rrrrrr
//      Gg         integral gain shift g (0..F, default 6; bigger = slower)
//      Mdd        maximum duty the controller may use (default C0)
//      Pp         loop polarity: 0 = RO slows when hot, 1 = RO speeds up when hot
//                 (temperature inversion, what this die does at 1.0 V)
//      Wq         quiet window on (1, default) / off (0): heater forced off for
//                 every 4th 21 ms gate; ROs, loop and alarm use only those gates,
//                 because heater current drops the local VCCINT (IR drop)
//      Aicccccc   on-chip over-temperature alarm: trip when RO i's quiet count
//                 crosses cccccc on the hot side (00 = off). Latches heater off.
//      Tss        droop monitor delay-line tap (0..3F)
//      Lxxx/Uxxx  droop (pos < L) / glitch (pos > U) alarm thresholds
//      C          clear droop, glitch and temperature alarms
//      Od         scope: capture 16384 droop-monitor samples, each the mean of
//                 2^d raw ones (25 MS/s / 2^d), then dump them as Q lines
//      Yr         die map (8x8 ring grid) on (1, default) / off (0)
//      K          keepalive (any valid command also feeds the watchdog)
//
// LEDs (active low): led[0] heartbeat, led[1] heater on, led[2] watchdog tripped.

`default_nettype none

module pvt_lab #(
    parameter HBANKS = 48,
    parameter HBITS  = 2048
) (
    input  wire       clk,      // 50 MHz, F22
    input  wire       rst_n,    // U26, active low
    output wire       tx,       // AE22
    output wire       cm4_tx,   // A15 = CM4 GPIO15 (RXD0): same stream as tx, read-only
    input  wire       rx,       // AF22
    output wire [2:0] led
);
    // ---------------------------------------------------------------- reset
    reg [2:0]  rst_sync = 3'b000;
    reg [19:0] por      = 20'd0;
    always @(posedge clk) begin
        rst_sync <= {rst_sync[1:0], rst_n};
        if (!rst_sync[2]) por <= 20'd0;
        else if (!por[19]) por <= por + 1'b1;
    end
    wire rst = !por[19];

    // ---------------------------------------------------------------- ROs
    localparam NRO = 5;
    wire [24*NRO-1:0] ro_cnt;
    ro_monitor #(.LOC("SLICE_X0Y20"))    ro0 (.clk(clk), .en(!rst), .count(ro_cnt[ 0+:24]));
    ro_monitor #(.LOC("SLICE_X151Y20"))  ro1 (.clk(clk), .en(!rst), .count(ro_cnt[24+:24]));
    ro_monitor #(.LOC("SLICE_X80Y170"))  ro2 (.clk(clk), .en(!rst), .count(ro_cnt[48+:24]));
    ro_monitor #(.LOC("SLICE_X0Y320"))   ro3 (.clk(clk), .en(!rst), .count(ro_cnt[72+:24]));
    ro_monitor #(.LOC("SLICE_X145Y320")) ro4 (.clk(clk), .en(!rst), .count(ro_cnt[96+:24]));

    // TDC: its own source RO just below the carry chain
    wire [23:0] tdc_ro_cnt, tdc_sum;
    wire        tdc_sig;
    ro_monitor #(.LOC("SLICE_X82Y98")) ro_tdc (.clk(clk), .en(!rst), .count(tdc_ro_cnt), .ring(tdc_sig));
    tdc #(.LOC("SLICE_X82Y100"), .NC(64), .AVG(12)) tdc_i (.clk(clk), .sig(tdc_sig), .sum(tdc_sum), .upd());

    // All ro_monitor gates run off the same free-running 20-bit counter phase
    // (each has its own copy, all reset at configuration), so this one marks
    // "new counts available" for the controller.
    reg [19:0] gate = 20'd0;
    always @(posedge clk) gate <= gate + 1'b1;
    reg gate_tick = 1'b0;
    always @(posedge clk) gate_tick <= (gate == 20'd260); // just after ro_monitor updates (gate 255)

    // Quiet window: gate period gq==3 has the heater forced off. Its RO counts
    // appear at gate 255 of the next period (gq==0) and are latched into qcnt.
    reg [1:0] gq = 2'd0;
    always @(posedge clk) if (&gate) gq <= gq + 1'b1;
    reg qen = 1'b1;
    wire quiet = qen && (gq == 2'd3);
    reg [24*NRO-1:0] qcnt = {24*NRO{1'b0}};
    reg qtick = 1'b0;
    always @(posedge clk) begin
        qtick <= 1'b0;
        if (gate_tick && (gq == 2'd0 || !qen)) begin qcnt <= ro_cnt; qtick <= 1'b1; end
    end

    // ---------------------------------------------------------------- UART RX
    localparam BAUD_DIV = 434;
    reg [1:0]  rx_s = 2'b11;
    always @(posedge clk) rx_s <= {rx_s[0], rx};
    reg [9:0]  rx_div = 10'd0;
    reg [3:0]  rx_bit = 4'd0;
    reg [7:0]  rx_sh  = 8'd0;
    reg        rx_busy = 1'b0;
    reg        rx_stb  = 1'b0;
    reg [7:0]  rx_byte = 8'd0;
    always @(posedge clk) begin
        rx_stb <= 1'b0;
        if (!rx_busy) begin
            if (!rx_s[1]) begin rx_busy <= 1'b1; rx_div <= BAUD_DIV/2; rx_bit <= 4'd0; end
        end else if (rx_div == BAUD_DIV - 1) begin
            rx_div <= 10'd0;
            rx_bit <= rx_bit + 1'b1;
            if (rx_bit == 4'd0) begin
                if (rx_s[1]) rx_busy <= 1'b0;             // false start
            end else if (rx_bit <= 4'd8)
                rx_sh <= {rx_s[1], rx_sh[7:1]};
            else begin
                rx_busy <= 1'b0;
                if (rx_s[1]) begin rx_stb <= 1'b1; rx_byte <= rx_sh; end
            end
        end else
            rx_div <= rx_div + 1'b1;
    end

    // command parser
    reg [7:0]  cmd = 8'd0;
    reg [31:0] arg = 32'd0;
    reg [4:0]  nd  = 5'd0;
    wire       is_hex = (rx_byte >= "0" && rx_byte <= "9") || (rx_byte >= "A" && rx_byte <= "F")
                     || (rx_byte >= "a" && rx_byte <= "f");
    wire [3:0] hexv = (rx_byte <= "9") ? rx_byte[3:0] : (rx_byte[3:0] + 4'd9);

    reg [1:0]  mode   = 2'd0;        // 0 open, 1 closed, 2 watchdog tripped
    reg [7:0]  duty_o = 8'd0;        // open-loop duty
    reg [2:0]  ro_sel = 3'd0;
    reg [23:0] setpt  = 24'd0;
    reg [3:0]  gshift = 4'd6;
    reg [7:0]  dmax   = 8'hC0;
    reg        pol    = 1'b0;
    reg [2:0]  asel   = 3'd0;
    reg [23:0] athr   = 24'd0;
    reg [5:0]  dsel   = 6'd24;
    reg [8:0]  dlo    = 9'd0;
    reg [8:0]  dhi    = 9'h1FF;
    reg        aclr   = 1'b0;
    reg        sc_arm = 1'b0;
    reg [3:0]  sc_dec = 4'd0;
    reg        grid_run = 1'b1;
    reg [26:0] wdog   = 27'd0;
    reg        cmd_ok = 1'b0;

    always @(posedge clk) begin
        cmd_ok <= 1'b0;
        aclr   <= 1'b0;
        sc_arm <= 1'b0;
        if (rst) begin
            mode <= 2'd0; duty_o <= 8'd0; nd <= 5'd0; cmd <= 8'd0;
            gshift <= 4'd6; dmax <= 8'hC0; wdog <= 27'd0;
        end else begin
            wdog <= (&wdog) ? wdog : wdog + 1'b1;
            if (&wdog && mode != 2'd2) begin
                mode <= 2'd2; duty_o <= 8'd0;
            end
            if (rx_stb) begin
                if (rx_byte == 8'h0A || rx_byte == 8'h0D) begin
                    cmd_ok <= 1'b1;
                    case (cmd)
                        "H": begin mode <= 2'd0; duty_o <= arg[7:0]; wdog <= 0; end
                        "S": begin mode <= 2'd1; ro_sel <= arg[26:24]; setpt <= arg[23:0]; wdog <= 0; end
                        "G": begin gshift <= arg[3:0]; wdog <= 0; end
                        "M": begin dmax <= arg[7:0]; wdog <= 0; end
                        "P": begin pol <= arg[0]; wdog <= 0; end
                        "W": begin qen <= arg[0]; wdog <= 0; end
                        "A": begin asel <= arg[26:24]; athr <= arg[23:0]; wdog <= 0; end
                        "T": begin dsel <= arg[5:0]; wdog <= 0; end
                        "L": begin dlo <= arg[8:0]; wdog <= 0; end
                        "U": begin dhi <= arg[8:0]; wdog <= 0; end
                        "C": begin aclr <= 1'b1; wdog <= 0; end
                        "O": begin sc_arm <= 1'b1; sc_dec <= arg[3:0]; wdog <= 0; end
                        "Y": begin grid_run <= arg[0]; wdog <= 0; end
                        "K": wdog <= 0;
                        default: ;
                    endcase
                    cmd <= 8'd0; arg <= 32'd0;
                end else if (cmd == 8'd0)
                    begin cmd <= rx_byte & 8'hDF; arg <= 32'd0; end  // upper-case
                else if (is_hex)
                    arg <= {arg[27:0], hexv};
            end
        end
    end

    // ---------------------------------------------------------------- controller
    // Integral loop: RO count above setpoint = die too cold = more heat.
    reg [23:0] cur;
    always @* begin
        case (ro_sel)
            3'd0: cur = qcnt[ 0+:24];
            3'd1: cur = qcnt[24+:24];
            3'd2: cur = qcnt[48+:24];
            3'd3: cur = qcnt[72+:24];
            default: cur = qcnt[96+:24];
        endcase
    end
    reg signed [25:0] err = 26'sd0;
    reg signed [25:0] step = 26'sd0;
    reg [15:0] integ = 16'd0;                // duty = integ[15:8]
    reg [1:0]  cph = 2'd0;
    always @(posedge clk) begin
        if (mode != 2'd1) begin
            integ <= 16'd0; cph <= 2'd0;
        end else begin
            case (cph)
                2'd0: if (qtick && cur != 24'd0) begin
                          err <= pol ? $signed({2'b00, setpt}) - $signed({2'b00, cur})
                                     : $signed({2'b00, cur}) - $signed({2'b00, setpt});
                          cph <= 2'd1;
                      end
                2'd1: begin step <= err >>> gshift; cph <= 2'd2; end
                default: begin
                    // saturate integ + step into [0, dmax<<8]
                    if ($signed({10'd0, integ}) + step < 0)
                        integ <= 16'd0;
                    else if ($signed({10'd0, integ}) + step > $signed({10'd0, dmax, 8'hFF}))
                        integ <= {dmax, 8'hFF};
                    else
                        integ <= integ + step[15:0];
                    cph <= 2'd0;
                end
            endcase
        end
    end

    wire [7:0] duty_req = (mode == 2'd1) ? integ[15:8] : (mode == 2'd0) ? duty_o : 8'd0;

    // on-chip over-temperature alarm (quiet counts, polarity-aware)
    reg [23:0] acur;
    always @* begin
        case (asel)
            3'd0: acur = qcnt[ 0+:24];
            3'd1: acur = qcnt[24+:24];
            3'd2: acur = qcnt[48+:24];
            3'd3: acur = qcnt[72+:24];
            default: acur = qcnt[96+:24];
        endcase
    end
    reg talarm = 1'b0;
    always @(posedge clk) begin
        if (aclr || rst) talarm <= 1'b0;
        else if (qtick && athr != 24'd0 && acur != 24'd0 && (pol ? (acur > athr) : (acur < athr)))
            talarm <= 1'b1;
    end
    wire [7:0] duty = (quiet || talarm) ? 8'd0 : duty_req;

    // ---------------------------------------------------------------- droop monitor
    wire [8:0]  dm_min, dm_max;
    wire [23:0] dm_sum;
    wire        dm_lo, dm_hi;
    dmon #(.LOC("SLICE_X86Y200"), .NL(64), .NC(64), .WIN(15)) dmon_i (
        .clk(clk), .sel(dsel), .thr_lo(dlo), .thr_hi(dhi), .clr(aclr),
        .wmin(dm_min), .wmax(dm_max), .wsum(dm_sum), .alarm_lo(dm_lo), .alarm_hi(dm_hi),
        .pos_o(dm_pos), .vld_o(dm_vld));

    // ---------------------------------------------------------------- scope + die map
    wire [8:0]  dm_pos;
    wire        dm_vld;
    wire        sc_done, sc_busy;
    wire [15:0] sc_rdata;
    reg  [13:0] sc_raddr = 14'd0;
    scope #(.AW(14)) scope_i (.clk(clk), .pos(dm_pos), .vld(dm_vld), .arm(sc_arm), .dec(sc_dec),
        .done(sc_done), .busy(sc_busy), .raddr(sc_raddr), .rdata(sc_rdata));

    wire [23:0] gr_cnt;
    wire [7:0]  gr_frame;
    wire        gr_stb;
    wire [5:0]  gr_idx;
    ro_grid #(.WIN(18)) grid_i (.clk(clk), .run(grid_run && !rst), .rd_idx(gr_idx), .rd_cnt(gr_cnt),
        .frame(gr_frame), .frame_stb(gr_stb));

    // ---------------------------------------------------------------- heater
    // The heater banks run on their own gated clocks: HGROUPS BUFGCEs, each
    // feeding HBANKS/HGROUPS banks. A group's clock runs only during its PWM
    // on-phase, so an idle heater draws no clock-tree power (configuration
    // start-up no longer sees the step of ~200k clocked FFs), and the groups'
    // PWM phases are staggered by 256/HGROUPS to smooth the supply current.
    localparam HGROUPS = 6;
    reg [7:0] pwm = 8'd0;
    always @(posedge clk) pwm <= pwm + 1'b1;
    genvar b, gg;
    generate for (gg = 0; gg < HGROUPS; gg = gg + 1) begin : hgrp
        wire [7:0] ph = pwm + (gg * 256 / HGROUPS);
        reg ce = 1'b0;
        always @(posedge clk) ce <= (ph < duty);
        wire hclk;
        BUFGCE hbuf (.I(clk), .CE(ce), .O(hclk));
        for (b = 0; b < HBANKS / HGROUPS; b = b + 1) begin : bank
            heater_bank #(.N(HBITS)) hb (.clk(hclk));
        end
    end endgenerate

    // ---------------------------------------------------------------- UART TX
    // Three line types share the UART. Status lines go out every 2^24 clk; a
    // die-map line after each grid frame; scope lines (one per 16 samples)
    // after a capture, until all 1024 have gone. Status always wins.
    localparam LEN_STAT = 84, LEN_GRID = 5 + 64*7 + 2, LEN_SCOPE = 6 + 16*5 + 2;
    reg [23:0] rate = 24'd0;
    reg [23:0] sr [0:NRO-1];
    reg [7:0]  sduty = 8'd0;
    reg [1:0]  smode = 2'd0;
    reg [23:0] stdc = 24'd0, stro = 24'd0, sdsum = 24'd0;
    reg [8:0]  sdmin = 9'd0, sdmax = 9'd0;
    reg [2:0]  sflag = 3'd0;
    reg [8:0]  idx = 9'd0;
    reg [8:0]  len = 9'd0;
    reg [1:0]  src = 2'd0;               // 0 status, 1 grid, 2 scope
    reg        busy = 1'b0;
    reg [9:0]  bdiv = 10'd0;
    reg [3:0]  bitn = 4'd0;
    reg [9:0]  shreg = 10'h3FF;
    reg        txr = 1'b1;
    assign tx = txr;
    assign cm4_tx = txr;

    reg        stat_pend = 1'b0, grid_pend = 1'b0, dumping = 1'b0;
    reg [7:0]  gframe = 8'd0;
    reg [9:0]  ql = 10'd0;               // scope line number
    reg [15:0] qb [0:15];                // scope line buffer
    reg [4:0]  qf = 5'd0;                // fill counter
    reg        qfill = 1'b0, qready = 1'b0;
    integer j;

    function [7:0] hexc;
        input [3:0] n;
        hexc = (n < 4'd10) ? (8'h30 + n) : (8'h41 + n - 4'd10);
    endfunction

    reg [7:0] ch_s, ch_g, ch_q, ch;
    integer   kk, pp, gk, gp, qk, qp;
    always @* begin
        ch_s = 8'h20; kk = 0; pp = 0;
        if (idx == 0) ch_s = "R";
        else if (idx >= 2 && idx < 2 + NRO*7) begin
            kk = (idx - 2) / 7; pp = (idx - 2) % 7;
            if (pp != 6) ch_s = hexc(sr[kk] >> (4 * (5 - pp)));
        end
        else if (idx == 37) ch_s = "H";
        else if (idx == 39) ch_s = hexc(sduty[7:4]);
        else if (idx == 40) ch_s = hexc(sduty[3:0]);
        else if (idx == 42) ch_s = "M";
        else if (idx == 44) ch_s = hexc({2'b00, smode});
        else if (idx == 46) ch_s = "D";
        else if (idx >= 48 && idx < 54) ch_s = hexc(stdc >> (4 * (53 - idx)));
        else if (idx >= 55 && idx < 61) ch_s = hexc(stro >> (4 * (60 - idx)));
        else if (idx == 62) ch_s = "P";
        else if (idx >= 64 && idx < 67) ch_s = hexc({3'b000, sdmin} >> (4 * (66 - idx)));
        else if (idx >= 68 && idx < 71) ch_s = hexc({3'b000, sdmax} >> (4 * (70 - idx)));
        else if (idx >= 72 && idx < 78) ch_s = hexc(sdsum >> (4 * (77 - idx)));
        else if (idx == 79) ch_s = "A";
        else if (idx == 81) ch_s = hexc({1'b0, sflag});
        else if (idx == 82) ch_s = 8'h0D;
        else if (idx == 83) ch_s = 8'h0A;

        ch_g = 8'h20; gk = 0; gp = 0;
        if (idx == 0) ch_g = "G";
        else if (idx == 2) ch_g = hexc(gframe[7:4]);
        else if (idx == 3) ch_g = hexc(gframe[3:0]);
        else if (idx >= 5 && idx < 5 + 64*7) begin
            gk = (idx - 5) / 7; gp = (idx - 5) % 7;
            if (gp != 6) ch_g = hexc(gr_cnt >> (4 * (5 - gp)));
        end
        else if (idx == LEN_GRID - 2) ch_g = 8'h0D;
        else if (idx == LEN_GRID - 1) ch_g = 8'h0A;

        ch_q = 8'h20; qk = 0; qp = 0;
        if (idx == 0) ch_q = "Q";
        else if (idx >= 2 && idx < 5) ch_q = hexc({2'b00, ql} >> (4 * (4 - idx)));
        else if (idx >= 6 && idx < 6 + 16*5) begin
            qk = (idx - 6) / 5; qp = (idx - 6) % 5;
            if (qp != 4) ch_q = hexc(qb[qk] >> (4 * (3 - qp)));
        end
        else if (idx == LEN_SCOPE - 2) ch_q = 8'h0D;
        else if (idx == LEN_SCOPE - 1) ch_q = 8'h0A;

        ch = (src == 2'd0) ? ch_s : (src == 2'd1) ? ch_g : ch_q;
    end
    assign gr_idx = gk[5:0];

    always @(posedge clk) begin
        rate <= rate + 1'b1;
        if (&rate) stat_pend <= 1'b1;
        if (gr_stb) begin gframe <= gr_frame; grid_pend <= 1'b1; end
        if (sc_arm) begin dumping <= 1'b1; ql <= 10'd0; qready <= 1'b0; qfill <= 1'b0; end

        // scope line buffer fill: 16 reads, 1-cycle BRAM latency
        if (dumping && sc_done && !qready && !qfill && !(busy && src == 2'd2)) begin
            qfill <= 1'b1; qf <= 5'd0; sc_raddr <= {ql, 4'd0};
        end else if (qfill) begin
            sc_raddr <= sc_raddr + 1'b1;
            if (qf != 5'd0) qb[qf - 1] <= sc_rdata;
            if (qf == 5'd16) begin qfill <= 1'b0; qready <= 1'b1; end
            qf <= qf + 1'b1;
        end

        if (rst) begin
            busy <= 1'b0; txr <= 1'b1; bitn <= 4'd0; stat_pend <= 1'b0; grid_pend <= 1'b0; dumping <= 1'b0;
        end else if (!busy) begin
            if (stat_pend) begin
                for (j = 0; j < NRO; j = j + 1) sr[j] <= qcnt[24*j +: 24];
                sduty <= duty_req; smode <= mode; stdc <= tdc_sum; stro <= tdc_ro_cnt;
                sdmin <= dm_min; sdmax <= dm_max; sdsum <= dm_sum; sflag <= {talarm, dm_hi, dm_lo};
                stat_pend <= 1'b0; src <= 2'd0; len <= LEN_STAT;
                idx <= 9'd0; busy <= 1'b1; bitn <= 4'd0;
            end else if (grid_pend) begin
                grid_pend <= 1'b0; src <= 2'd1; len <= LEN_GRID;
                idx <= 9'd0; busy <= 1'b1; bitn <= 4'd0;
            end else if (dumping && qready) begin
                src <= 2'd2; len <= LEN_SCOPE;
                idx <= 9'd0; busy <= 1'b1; bitn <= 4'd0;
            end
        end else if (bitn == 4'd0) begin
            shreg <= {1'b1, ch, 1'b0};
            bitn  <= 4'd10; bdiv <= 10'd0;
        end else if (bdiv == BAUD_DIV - 1) begin
            bdiv  <= 10'd0;
            txr   <= shreg[0];
            shreg <= {1'b1, shreg[9:1]};
            bitn  <= bitn - 1'b1;
            if (bitn == 4'd1) begin
                if (idx == len - 1) begin
                    busy <= 1'b0;
                    if (src == 2'd2) begin
                        qready <= 1'b0;
                        if (&ql) dumping <= 1'b0; else ql <= ql + 1'b1;
                    end
                end else idx <= idx + 1'b1;
            end
        end else
            bdiv <= bdiv + 1'b1;
    end

    assign led[0] = ~rate[23];
    assign led[1] = ~(duty != 8'd0);
    assign led[2] = ~(mode == 2'd2 || talarm || dm_lo || dm_hi);
endmodule

// One heater bank: an N-bit register loaded with 0101... that rotates on
// every (gated) clock, so every FF toggles every cycle, plus an XOR stage (also toggling
// every cycle) whose register is kept so synthesis cannot drop it.
module heater_bank #(parameter N = 2048) (
    input wire clk
);
    (* keep *) reg [N-1:0] q = {(N/2){2'b01}};
    (* keep *) reg [N-1:0] h = {N{1'b0}};
    integer i;
    always @(posedge clk) begin
        q <= {q[N-2:0], q[N-1]};
        for (i = 0; i < N; i = i + 1)
            h[i] <= q[i] ^ q[(i+3)%N] ^ q[(i+5)%N];
    end
endmodule

`default_nettype wire
