// IEEE 1687-2014 ICL for the IJTAG Fault Hunt chip (first pass, hand-written).
// Mirrors src/project.v. Bit 0 of every register is nearest TDO.
// Note: the Fault Hunt level-4 "broken SIB" fault is intentionally NOT modeled here --
// diagnosing the mismatch between this ICL and the silicon is the puzzle.

Module SIB_mux_pre {
    ScanInPort    si;
    ScanOutPort   so      { Source SR; }
    ScanInterface client  { Port si; Port so; Port sel; }
    ScanInPort    fromSO;
    ScanOutPort   toSI    { Source si; }
    ScanInterface host    { Port fromSO; Port toSI; Port toSel; }
    SelectPort    sel;
    ToSelectPort  toSel   { Source SR; }
    ScanRegister  SR      { ScanInSource SIBmux; CaptureSource SR; ResetValue 1'b0; }
    ScanMux SIBmux SelectedBy SR { 1'b0 : si; 1'b1 : fromSO; }
}

Module TDR4  { ScanInPort si; ScanOutPort so { Source R[0]; } SelectPort sel;
               ScanRegister R[3:0]  { ScanInSource si; ResetValue 4'h0; } }
Module TDR8  { ScanInPort si; ScanOutPort so { Source R[0]; } SelectPort sel;
               ScanRegister R[7:0]  { ScanInSource si; ResetValue 8'h00; } }
Module TDR12 { ScanInPort si; ScanOutPort so { Source R[0]; } SelectPort sel;
               ScanRegister R[11:0] { ScanInSource si; ResetValue 12'h000; } }
Module TDR16 { ScanInPort si; ScanOutPort so { Source R[0]; } SelectPort sel;
               ScanRegister R[15:0] { ScanInSource si; } }
Module TDR19 { ScanInPort si; ScanOutPort so { Source R[0]; } SelectPort sel;
               ScanRegister R[18:0] { ScanInSource si; } }

Module FaultHuntSeg {
    ScanInPort si; ScanOutPort so { Source FH_ANS.so; } SelectPort sel;
    Instance FH_CFG  Of TDR12 { InputPort si = si;            InputPort sel = sel; }
    Instance SIB_A1  Of SIB_mux_pre { InputPort si = FH_CFG.so; InputPort fromSO = FH_SCAN.so; InputPort sel = sel; }
    Instance FH_SCAN Of TDR19 { InputPort si = SIB_A1.toSI;   InputPort sel = SIB_A1.toSel; }
    Instance FH_ANS  Of TDR8  { InputPort si = SIB_A1.so;     InputPort sel = sel; }
    Alias key[7:0]   = FH_CFG.R[7:0];
    Alias fh_en      = FH_CFG.R[8];
    Alias A[3:0]     = FH_SCAN.R[18:15];
    Alias B[3:0]     = FH_SCAN.R[14:11];
    Alias OP[2:0]    = FH_SCAN.R[10:8];
    Alias RESP[7:0]  = FH_SCAN.R[7:0];   // {Y[3:0], C, Z, N, V}
    Alias ans_site[3:0] = FH_ANS.R[3:0];
    Alias ans_type[2:0] = FH_ANS.R[6:4];
    Alias submit        = FH_ANS.R[7];   // reads back FH_PASS
}

Module BistSeg {
    ScanInPort si; ScanOutPort so { Source BIST_SIG.so; } SelectPort sel;
    Instance BIST_CTRL Of TDR4  { InputPort si = si;           InputPort sel = sel; }
    Instance BIST_SIG  Of TDR16 { InputPort si = BIST_CTRL.so; InputPort sel = sel; }
    Alias start    = BIST_CTRL.R[0];     // reads back BIST_DONE
    Alias seed_sel = BIST_CTRL.R[1];
    Alias len[1:0] = BIST_CTRL.R[3:2];
}

Module RoSeg {
    ScanInPort si; ScanOutPort so { Source RO_CNT.so; } SelectPort sel;
    Instance RO_CTRL Of TDR8  { InputPort si = si;         InputPort sel = sel; }
    Instance RO_CNT  Of TDR16 { InputPort si = RO_CTRL.so; InputPort sel = sel; }
    Alias en        = RO_CTRL.R[0];
    Alias ring[1:0] = RO_CTRL.R[2:1];
    Alias gate[3:0] = RO_CTRL.R[6:3];
    Alias start     = RO_CTRL.R[7];      // reads back RO_DONE
}

Module tt_um_klay_ijtag_faulthunt {
    ScanInPort tdi; ScanOutPort tdo { Source SIB_C.so; }
    SelectPort ijtag_access;             // IR = 0x8
    Instance SIB_A Of SIB_mux_pre { InputPort si = tdi;      InputPort fromSO = FH.so;   InputPort sel = ijtag_access; }
    Instance FH    Of FaultHuntSeg { InputPort si = SIB_A.toSI; InputPort sel = SIB_A.toSel; }
    Instance SIB_B Of SIB_mux_pre { InputPort si = SIB_A.so; InputPort fromSO = BIST.so; InputPort sel = ijtag_access; }
    Instance BIST  Of BistSeg      { InputPort si = SIB_B.toSI; InputPort sel = SIB_B.toSel; }
    Instance SIB_C Of SIB_mux_pre { InputPort si = SIB_B.so; InputPort fromSO = RO.so;   InputPort sel = ijtag_access; }
    Instance RO    Of RoSeg        { InputPort si = SIB_C.toSI; InputPort sel = SIB_C.toSel; }
}
