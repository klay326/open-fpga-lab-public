`timescale 1ps / 1ps
// DCIRESET has no effect in simulation (DCI isn't modelled): ready immediately.
module DCIRESET (input wire RST, output wire LOCKED);
    assign LOCKED = 1'b1;
endmodule
