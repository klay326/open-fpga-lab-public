// xadc_monitor -- on-die PVT-style monitor for the QMTech XC7K325T board.
//
// Default build (openXC7): five LUT ring oscillators placed in different clock
// regions, each counted against the 50 MHz F22 clock, as a process / voltage /
// temperature delay monitor. One ASCII line every ~0.34 s on the JP5 UART
// (AE22 TX), 115200 8N1, all hex:
//
//      R r0r0r0 r1r1r1 r2r2r2 r3r3r3 r4r4r4\r\n
//
//   rN = RO/2 edges counted in a 2^20-cycle (20.97 ms) gate of the 50 MHz clock:
//   f_RO = 2 * rN * 50e6 / 2^20.
//
// The XADC block below is compiled only with -DXADC_FABRIC. It synthesises in
// yosys, but nextpnr-xilinx has no XADC bel (nextpnr-xilinx-meta has no
// site_type_XADC.json) and prjxray-db has no MONITOR-tile segbits, so it cannot
// be placed today -- see README.md. With it, each line is prefixed by
//
//      X tttt iiii aaaa bbbb CCCC DDDD
//
//   tttt/iiii/aaaa/bbbb = XADC status regs 0x00/0x01/0x02/0x06 (12-bit code in
//   [15:4]); CCCC/DDDD = config regs 0x41/0x42 read back; DEAD = DRP timeout.
//   In the default build, xadc_log.py reads the XADC over JTAG instead.
//
// LEDs (active low): led[0] heartbeat, led[1] XADC DRP alive and led[2] XADC
// over-temperature (both XADC_FABRIC only). U26 (active low): reset.

`default_nettype none

module xadc_monitor (
    input  wire       clk,      // 50 MHz, F22
    input  wire       rst_n,    // U26, active low
    output wire       tx,       // AE22 -> FT232R RXD
    input  wire       rx,       // AF22 (unused)
    output wire [2:0] led       // active low
);

    // ---------------------------------------------------------------- reset
    reg [2:0]  rst_sync = 3'b000;
    reg [19:0] por      = 20'd0;          // ~21 ms power-on hold
    always @(posedge clk) begin
        rst_sync <= {rst_sync[1:0], rst_n};
        if (!rst_sync[2])
            por <= 20'd0;
        else if (!por[19])
            por <= por + 1'b1;
    end
    wire rst = !por[19];

`ifdef XADC_FABRIC
    localparam XW = 6;
    // ---------------------------------------------------------------- XADC
    reg  [6:0]  daddr = 7'd0;
    reg  [15:0] di    = 16'd0;
    reg         den   = 1'b0;
    reg         dwe   = 1'b0;
    wire [15:0] do_;
    wire        drdy;
    wire        ot;
    wire [7:0]  alm;

    // The INIT_4x values below are what Vivado would bake into the bitstream.
    // prjxray-db has no MONITOR-tile segbits, so openXC7 cannot emit them; the
    // FSM writes the same values over the DRP after configuration instead.
    (* BEL = "XADC_X0Y0/XADC" *) XADC #(
        .INIT_40(16'h0000),     // no averaging
        .INIT_41(16'h2EF0),     // continuous sequencer, cal on, alarms off, OT on
        .INIT_42(16'h0400),     // ADCCLK = DCLK/4 = 12.5 MHz
        .INIT_48(16'h4701),     // seq: cal, temp, VCCINT, VCCAUX, VCCBRAM
        .INIT_49(16'h0000)
    ) xadc_i (
        .DCLK(clk), .DADDR(daddr), .DEN(den), .DWE(dwe), .DI(di),
        .DO(do_), .DRDY(drdy),
        .RESET(1'b0), .CONVST(1'b0), .CONVSTCLK(1'b0),
        .VP(1'b0), .VN(1'b0), .VAUXP(16'h0), .VAUXN(16'h0),
        .OT(ot), .ALM(alm),
        .BUSY(), .CHANNEL(), .EOC(), .EOS(), .JTAGBUSY(), .JTAGLOCKED(),
        .JTAGMODIFIED(), .MUXADDR()
    );

    // DRP script: 5 writes, then reads 0x00,0x01,0x02,0x06,0x41,0x42 forever.
    function [23:0] wr_step;            // {addr, data}
        input [2:0] i;
        case (i)
            3'd0: wr_step = {8'h42, 16'h0400};
            3'd1: wr_step = {8'h48, 16'h4701};
            3'd2: wr_step = {8'h49, 16'h0000};
            3'd3: wr_step = {8'h40, 16'h0000};
            default: wr_step = {8'h41, 16'h2EF0};  // last: restarts sequencer
        endcase
    endfunction
    function [6:0] rd_addr;
        input [2:0] i;
        case (i)
            3'd0: rd_addr = 7'h00;  // temperature
            3'd1: rd_addr = 7'h01;  // VCCINT
            3'd2: rd_addr = 7'h02;  // VCCAUX
            3'd3: rd_addr = 7'h06;  // VCCBRAM
            3'd4: rd_addr = 7'h41;  // config 1 readback
            default: rd_addr = 7'h42;  // config 2 readback
        endcase
    endfunction

    wire [23:0] wr_cur = wr_step(step);

    localparam S_IDLE = 2'd0, S_WAIT = 2'd1, S_GAP = 2'd2;
    reg [1:0]  st = S_IDLE;
    reg        writing = 1'b1;
    reg [2:0]  step = 3'd0;
    reg [11:0] tmo = 12'd0;
    reg [15:0] xr [0:5];
    reg        drp_ok = 1'b0;
    integer k;
    initial for (k = 0; k < 6; k = k + 1) xr[k] = 16'h0000;

    always @(posedge clk) begin
        den <= 1'b0;
        dwe <= 1'b0;
        if (rst) begin
            st <= S_IDLE; writing <= 1'b1; step <= 3'd0; drp_ok <= 1'b0;
        end else case (st)
            S_IDLE: begin
                den <= 1'b1;
                if (writing) begin
                    {daddr, di} <= wr_cur[22:0];
                    dwe <= 1'b1;
                end else
                    daddr <= rd_addr(step);
                tmo <= 12'd0;
                st  <= S_WAIT;
            end
            S_WAIT: begin
                tmo <= tmo + 1'b1;
                if (drdy || &tmo) begin
                    if (!writing)
                        xr[step] <= drdy ? do_ : 16'hDEAD;
                    if (drdy) drp_ok <= 1'b1;
                    st <= S_GAP;
                end
            end
            S_GAP: begin
                if (writing) begin
                    if (step == 3'd4) begin writing <= 1'b0; step <= 3'd0; end
                    else step <= step + 1'b1;
                end else
                    step <= (step == 3'd5) ? 3'd0 : step + 1'b1;
                st <= S_IDLE;
            end
            default: st <= S_IDLE;
        endcase
    end

`else
    localparam XW = 0;
    wire        ot = 1'b0;
    wire        drp_ok = 1'b0;
    reg  [15:0] xr [0:5];
    integer k;
    initial for (k = 0; k < 6; k = k + 1) xr[k] = 16'h0000;
`endif

    // ---------------------------------------------------------------- ROs
    localparam NRO = 5;
    wire [24*NRO-1:0] ro_cnt;
    ro_monitor #(.LOC("SLICE_X0Y20"))    ro0 (.clk(clk), .en(!rst), .count(ro_cnt[ 0+:24])); // X0Y0 bottom-left
    ro_monitor #(.LOC("SLICE_X151Y20"))  ro1 (.clk(clk), .en(!rst), .count(ro_cnt[24+:24])); // X1Y0 bottom-right
    ro_monitor #(.LOC("SLICE_X80Y170"))  ro2 (.clk(clk), .en(!rst), .count(ro_cnt[48+:24])); // X1Y3 next to the XADC/config column
    ro_monitor #(.LOC("SLICE_X0Y320"))   ro3 (.clk(clk), .en(!rst), .count(ro_cnt[72+:24])); // X0Y6 top-left
    ro_monitor #(.LOC("SLICE_X145Y320")) ro4 (.clk(clk), .en(!rst), .count(ro_cnt[96+:24])); // X1Y6 top-right

    // ---------------------------------------------------------------- report
    // 115200 baud: 434 clocks/bit at 50 MHz.
    localparam BAUD_DIV = 434;
    localparam RB       = (XW > 0) ? 32 : 0;         // start of the R section
    localparam MSG_LEN  = RB + 2 + NRO*7 + 2;         // 71 or 39

    reg [23:0] rate = 24'd0;              // line period ~0.34 s
    reg [15:0] sx [0:5];                  // snapshot
    reg [23:0] sr [0:NRO-1];
    reg [6:0]  idx = 7'd0;
    reg        busy = 1'b0;

    reg [9:0]  bdiv = 10'd0;
    reg [3:0]  bitn = 4'd0;
    reg [9:0]  shreg = 10'h3FF;
    reg        txr = 1'b1;
    assign tx = txr;

    function [7:0] hexc;
        input [3:0] n;
        hexc = (n < 4'd10) ? (8'h30 + n) : (8'h41 + n - 4'd10);
    endfunction

    reg [7:0] ch;
    integer   kk, pp;
    always @* begin
        ch = 8'h20; kk = 0; pp = 0;
        if (XW > 0 && idx == 0) ch = "X";
        else if (XW > 0 && idx >= 2 && idx < 32) begin
            kk = (idx - 2) / 5; pp = (idx - 2) % 5;
            if (pp != 4) ch = hexc(sx[kk] >> (4 * (3 - pp)));
        end else if (idx == RB) ch = "R";
        else if (idx >= RB + 2 && idx < RB + 2 + NRO*7) begin
            kk = (idx - RB - 2) / 7; pp = (idx - RB - 2) % 7;
            if (pp != 6) ch = hexc(sr[kk] >> (4 * (5 - pp)));
        end else if (idx == MSG_LEN - 2) ch = 8'h0D;
        else if (idx == MSG_LEN - 1) ch = 8'h0A;
    end

    integer j;
    always @(posedge clk) begin
        rate <= rate + 1'b1;
        if (rst) begin
            busy <= 1'b0; txr <= 1'b1; bitn <= 4'd0;
        end else if (!busy) begin
            if (&rate) begin
                for (j = 0; j < 6; j = j + 1) sx[j] <= xr[j];
                for (j = 0; j < NRO; j = j + 1) sr[j] <= ro_cnt[24*j +: 24];
                idx <= 7'd0; busy <= 1'b1; bitn <= 4'd0;
            end
        end else if (bitn == 4'd0) begin
            shreg <= {1'b1, ch, 1'b0};    // stop, data LSB-first, start
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

    // ---------------------------------------------------------------- LEDs
    assign led[0] = ~rate[23];
    assign led[1] = ~drp_ok;
    assign led[2] = ~ot;

endmodule

// One LUT ring oscillator plus its frequency counter.
// Ring: LUT2 NAND-style enable stage (out = en & ~fb) followed by STAGES-1 LUT1
// buffers, all pinned to one slice so the ring delay is local to that site. A
// toggle FF divides by 2, a 24-bit Gray counter runs in that domain, and the
// 50 MHz side samples it through a 2-FF synchroniser and differences it every
// 2^20 clocks.
module ro_monitor #(
    parameter LOC = "SLICE_X0Y0"
) (
    input  wire        clk,
    input  wire        en,
    output reg  [23:0] count = 24'd0
);
    // clkbuf_inhibit: n3 also clocks the toggle FF, and yosys would otherwise
    // insert a BUFG on it -- putting the global clock tree inside the ring.
    (* keep *) wire n0, n1, n2;
    (* keep, clkbuf_inhibit *) wire n3;
    (* keep, BEL = {LOC, "/A6LUT"} *) LUT2 #(.INIT(4'b0100)) s0 (.O(n0), .I0(n3), .I1(en));
    (* keep, BEL = {LOC, "/B6LUT"} *) LUT1 #(.INIT(2'b10))   s1 (.O(n1), .I0(n0));
    (* keep, BEL = {LOC, "/C6LUT"} *) LUT1 #(.INIT(2'b10))   s2 (.O(n2), .I0(n1));
    (* keep, BEL = {LOC, "/D6LUT"} *) LUT1 #(.INIT(2'b10))   s3 (.O(n3), .I0(n2));

    // RO domain
    reg        half = 1'b0;
    always @(posedge n3) half <= ~half;
    reg [23:0] bin = 24'd0, gray = 24'd0;
    always @(posedge half) begin
        bin  <= bin + 1'b1;
        gray <= bin ^ (bin >> 1);
    end

    // clk domain
    (* ASYNC_REG = "TRUE" *) reg [23:0] g1 = 24'd0, g2 = 24'd0;
    reg [23:0] prev = 24'd0;
    reg [19:0] gate = 20'd0;
    reg [23:0] b;
    integer i;
    always @* begin
        b[23] = g2[23];
        for (i = 22; i >= 0; i = i - 1) b[i] = b[i+1] ^ g2[i];
    end
    always @(posedge clk) begin
        g1 <= gray;
        g2 <= g1;
        gate <= gate + 1'b1;
        if (&gate) begin
            count <= b - prev;
            prev  <= b;
        end
    end
endmodule

`default_nettype wire
