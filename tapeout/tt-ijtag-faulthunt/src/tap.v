// IEEE 1149.1 TAP controller + 4-bit IR
// - No IDCODE: BYPASS loaded on Test-Logic-Reset (allowed when IDCODE absent)
// - IR capture = 4'b0001 (two LSBs = 01 per 1149.1)
// - TDO driven on falling TCK, only in Shift-IR / Shift-DR
`default_nettype none

module tap (
    input  wire       tck,
    input  wire       trst_n,     // async reset (TT rst_n)
    input  wire       tms,
    input  wire       tdi,
    input  wire       dr_tdo,     // serial out of the selected DR
    output reg        tdo,
    output reg  [3:0] ir,
    output wire       tlr,        // in Test-Logic-Reset
    output wire       rti,        // in Run-Test/Idle
    output wire       capture_dr,
    output wire       shift_dr,
    output wire       update_dr
);
    localparam [3:0]
        TLR  = 4'h0, RTI  = 4'h1, SDRS = 4'h2, CDR  = 4'h3,
        SHDR = 4'h4, E1DR = 4'h5, PDR  = 4'h6, E2DR = 4'h7,
        UDR  = 4'h8, SIRS = 4'h9, CIR  = 4'hA, SHIR = 4'hB,
        E1IR = 4'hC, PIR  = 4'hD, E2IR = 4'hE, UIR  = 4'hF;

    localparam [3:0] IR_BYPASS = 4'hF;

    reg [3:0] state, next;

    always @(*) begin
        case (state)
            TLR:  next = tms ? TLR  : RTI;
            RTI:  next = tms ? SDRS : RTI;
            SDRS: next = tms ? SIRS : CDR;
            CDR:  next = tms ? E1DR : SHDR;
            SHDR: next = tms ? E1DR : SHDR;
            E1DR: next = tms ? UDR  : PDR;
            PDR:  next = tms ? E2DR : PDR;
            E2DR: next = tms ? UDR  : SHDR;
            UDR:  next = tms ? SDRS : RTI;
            SIRS: next = tms ? TLR  : CIR;
            CIR:  next = tms ? E1IR : SHIR;
            SHIR: next = tms ? E1IR : SHIR;
            E1IR: next = tms ? UIR  : PIR;
            PIR:  next = tms ? E2IR : PIR;
            E2IR: next = tms ? UIR  : SHIR;
            UIR:  next = tms ? SDRS : RTI;
            default: next = TLR;
        endcase
    end

    always @(posedge tck or negedge trst_n)
        if (!trst_n) state <= TLR;
        else         state <= next;

    assign tlr        = (state == TLR);
    assign rti        = (state == RTI);
    assign capture_dr = (state == CDR);
    assign shift_dr   = (state == SHDR);
    assign update_dr  = (state == UDR);

    // Instruction register
    reg [3:0] ir_sr;
    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            ir_sr <= 4'b0001;
            ir    <= IR_BYPASS;
        end else begin
            if (state == TLR)       ir    <= IR_BYPASS;
            else if (state == UIR)  ir    <= ir_sr;
            if (state == CIR)       ir_sr <= 4'b0001;
            else if (state == SHIR) ir_sr <= {tdi, ir_sr[3:1]};
        end
    end

    // TDO on falling edge
    always @(negedge tck or negedge trst_n)
        if (!trst_n)             tdo <= 1'b0;
        else if (state == SHIR)  tdo <= ir_sr[0];
        else if (state == SHDR)  tdo <= dr_tdo;
endmodule
