// scope -- on-die supply-noise oscilloscope built on the droop monitor.
//
// dmon.v produces a path-delay position every rising launch (25 MS/s at
// 50 MHz). After `arm`, this block stores DEPTH consecutive values in block
// RAM, each the mean of 2^dec raw samples (dec = 0..8), in units of
// 1/16 tap (value = 16 * mean position). Sample rate = 25 MS/s / 2^dec.
// At dec = 0 the 16k-sample record covers 655 us (FFT bin 1.5 kHz, Nyquist
// 12.5 MHz); at dec = 8 it covers 168 ms (bin 6 Hz, Nyquist 49 kHz).
// `done` stays high from the end of the capture until the next `arm`; the
// read port is a plain synchronous BRAM read (1-cycle latency).

`default_nettype none

module scope #(
    parameter AW = 14            // 2^14 = 16384 samples
) (
    input  wire          clk,
    input  wire [8:0]    pos,
    input  wire          vld,
    input  wire          arm,
    input  wire [3:0]    dec,
    output reg           done = 1'b0,
    output reg           busy = 1'b0,
    input  wire [AW-1:0] raddr,
    output reg  [15:0]   rdata = 16'd0
);
    reg [15:0] mem [0:(1<<AW)-1];

    reg [3:0]    d = 4'd0;
    reg [16:0]   acc = 17'd0;
    reg [8:0]    n = 9'd0;
    reg [AW-1:0] waddr = 0;
    reg          we = 1'b0;
    reg [15:0]   wdata = 16'd0;

    always @(posedge clk) begin
        we <= 1'b0;
        if (arm) begin
            busy <= 1'b1; done <= 1'b0; waddr <= 0; acc <= 17'd0; n <= 9'd0;
            d <= (dec > 4'd8) ? 4'd8 : dec;
        end else if (busy && vld) begin
            if (n == (9'd1 << d) - 1'b1) begin
                // mean * 16 = (acc + pos) * 16 / 2^d
                wdata <= ({acc + pos, 4'b0000} >> d);
                we <= 1'b1;
                acc <= 17'd0; n <= 9'd0;
            end else begin
                acc <= acc + pos; n <= n + 1'b1;
            end
        end
        if (we) begin
            waddr <= waddr + 1'b1;
            if (&waddr) begin busy <= 1'b0; done <= 1'b1; end
        end
    end

    always @(posedge clk) begin
        if (we) mem[waddr] <= wdata;
        rdata <= mem[raddr];
    end
endmodule

`default_nettype wire
