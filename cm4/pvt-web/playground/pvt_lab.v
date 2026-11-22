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
//      K          keepalive (any valid command also feeds the watchdog)
//
// LEDs (active low): led[0] heartbeat, led[1] heater on, led[2] watchdog tripped.

`default_nettype none

module pvt_lab #(
    parameter HBANKS = 50,   // playground: 5 groups x 10 banks
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
    // playground: ro1 moved in from SLICE_X151Y20 (right-most column), where it
    // read 300 MHz, then 0, depending on routing (see cm4/pvt-web/README.md).
    ro_monitor #(.LOC("SLICE_X145Y20"))  ro1 (.clk(clk), .en(!rst), .count(ro_cnt[24+:24]));
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
    reg [26:0] wdog   = 27'd0;
    reg        cmd_ok = 1'b0;

    always @(posedge clk) begin
        cmd_ok <= 1'b0;
        aclr   <= 1'b0;
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
        .wmin(dm_min), .wmax(dm_max), .wsum(dm_sum), .alarm_lo(dm_lo), .alarm_hi(dm_hi));

    // ---------------------------------------------------------------- heater
    // The heater banks run on their own gated clocks: HGROUPS BUFGCEs, each
    // feeding HBANKS/HGROUPS banks. A group's clock runs only during its PWM
    // on-phase, so an idle heater draws no clock-tree power (configuration
    // start-up no longer sees the step of ~200k clocked FFs), and the groups'
    // PWM phases are staggered by 256/HGROUPS to smooth the supply current.
    //
    // playground: the heater runs at HMHZ (100 MHz, from a PLL) instead of the
    // 50 MHz system clock, for about twice the power. The PWM counter and the
    // BUFGCE enables run in that clock domain; duty (quasi-static, changes at
    // most every few ms) is brought across with a 2-FF synchroniser. Until the
    // PLL locks the heater stays off.
    // 5 groups (not 6): with the extra free-running 100 MHz BUFG, 6 groups
    // ran a clock region out of global clock lines (nextpnr failed to route).
    localparam HGROUPS = 5;
    wire hfb, hpll, hpll_locked, hclk_free;
    PLLE2_ADV #(
        .BANDWIDTH("OPTIMIZED"), .COMPENSATION("ZHOLD"), .DIVCLK_DIVIDE(1),
        .CLKFBOUT_MULT(20), .CLKFBOUT_PHASE(0.0), .CLKIN1_PERIOD(20.0),   // 1000 MHz VCO
        .CLKOUT0_DIVIDE(10), .CLKOUT0_PHASE(0.0), .CLKOUT0_DUTY_CYCLE(0.5) // 100 MHz heater
    ) hpll_i (
        .CLKIN1(clk), .CLKIN2(1'b0), .CLKINSEL(1'b1),
        .CLKFBIN(hfb), .CLKFBOUT(hfb),
        .CLKOUT0(hpll), .CLKOUT1(), .CLKOUT2(), .CLKOUT3(), .CLKOUT4(), .CLKOUT5(),
        .RST(1'b0), .PWRDWN(1'b0), .LOCKED(hpll_locked),
        .DADDR(7'd0), .DCLK(1'b0), .DEN(1'b0), .DI(16'd0), .DWE(1'b0), .DO(), .DRDY()
    );
    BUFG hbufg (.I(hpll), .O(hclk_free));
    reg [7:0] duty_h1 = 8'd0, duty_h = 8'd0;
    reg [1:0] lock_h = 2'b00;
    always @(posedge hclk_free) begin
        duty_h1 <= duty; duty_h <= duty_h1;
        lock_h  <= {lock_h[0], hpll_locked};
    end
    reg [7:0] pwm = 8'd0;
    always @(posedge hclk_free) pwm <= pwm + 1'b1;
    genvar b, gg;
    generate for (gg = 0; gg < HGROUPS; gg = gg + 1) begin : hgrp
        wire [7:0] ph = pwm + (gg * 256 / HGROUPS);
        reg ce = 1'b0;
        always @(posedge hclk_free) ce <= lock_h[1] && (ph < duty_h);
        wire hclk;
        BUFGCE hbuf (.I(hpll), .CE(ce), .O(hclk));
        for (b = 0; b < HBANKS / HGROUPS; b = b + 1) begin : bank
            heater_bank #(.N(HBITS)) hb (.clk(hclk));
        end
    end endgenerate

    // ---------------------------------------------------------------- UART TX
    localparam MSG_LEN = 84;
    reg [23:0] rate = 24'd0;
    reg [23:0] sr [0:NRO-1];
    reg [7:0]  sduty = 8'd0;
    reg [1:0]  smode = 2'd0;
    reg [23:0] stdc = 24'd0, stro = 24'd0, sdsum = 24'd0;
    reg [8:0]  sdmin = 9'd0, sdmax = 9'd0;
    reg [2:0]  sflag = 3'd0;
    reg [6:0]  idx = 7'd0;
    reg        busy = 1'b0;
    reg [9:0]  bdiv = 10'd0;
    reg [3:0]  bitn = 4'd0;
    reg [9:0]  shreg = 10'h3FF;
    reg        txr = 1'b1;
    assign tx = txr;
    assign cm4_tx = txr;

    function [7:0] hexc;
        input [3:0] n;
        hexc = (n < 4'd10) ? (8'h30 + n) : (8'h41 + n - 4'd10);
    endfunction

    reg [7:0] ch;
    integer   kk, pp;
    always @* begin
        ch = 8'h20; kk = 0; pp = 0;
        if (idx == 0) ch = "R";
        else if (idx >= 2 && idx < 2 + NRO*7) begin
            kk = (idx - 2) / 7; pp = (idx - 2) % 7;
            if (pp != 6) ch = hexc(sr[kk] >> (4 * (5 - pp)));
        end
        else if (idx == 37) ch = "H";
        else if (idx == 39) ch = hexc(sduty[7:4]);
        else if (idx == 40) ch = hexc(sduty[3:0]);
        else if (idx == 42) ch = "M";
        else if (idx == 44) ch = hexc({2'b00, smode});
        else if (idx == 46) ch = "D";
        else if (idx >= 48 && idx < 54) ch = hexc(stdc >> (4 * (53 - idx)));
        else if (idx >= 55 && idx < 61) ch = hexc(stro >> (4 * (60 - idx)));
        else if (idx == 62) ch = "P";
        else if (idx >= 64 && idx < 67) ch = hexc({3'b000, sdmin} >> (4 * (66 - idx)));
        else if (idx >= 68 && idx < 71) ch = hexc({3'b000, sdmax} >> (4 * (70 - idx)));
        else if (idx >= 72 && idx < 78) ch = hexc(sdsum >> (4 * (77 - idx)));
        else if (idx == 79) ch = "A";
        else if (idx == 81) ch = hexc({1'b0, sflag});
        else if (idx == 82) ch = 8'h0D;
        else if (idx == 83) ch = 8'h0A;
    end

    integer j;
    always @(posedge clk) begin
        rate <= rate + 1'b1;
        if (rst) begin
            busy <= 1'b0; txr <= 1'b1; bitn <= 4'd0;
        end else if (!busy) begin
            if (&rate) begin
                for (j = 0; j < NRO; j = j + 1) sr[j] <= qcnt[24*j +: 24];
                sduty <= duty_req; smode <= mode; stdc <= tdc_sum; stro <= tdc_ro_cnt;
                sdmin <= dm_min; sdmax <= dm_max; sdsum <= dm_sum; sflag <= {talarm, dm_hi, dm_lo};
                idx <= 7'd0; busy <= 1'b1; bitn <= 4'd0;
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
                if (idx == MSG_LEN - 1) busy <= 1'b0;
                else idx <= idx + 1'b1;
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
