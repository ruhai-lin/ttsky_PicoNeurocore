/*
 * PicoNeurocore — Ultra-minimal Loihi-1 style neurocore prototype
 * Top module for Tiny Tapeout / OpenRAM
 *
 * Main data flow (fully connected across development phases):
 *   input event --> axon --> synapse (SRAM) --> dendrite --> spike --> learning --> SRAM writeback
 *
 * Module inventory:
 *   - 4 x dendrite  (LIF neurons)
 *   - 1 x axon      (event --> synapse range selector)
 *   - 1 x synapse   (SRAM interface + field parsing)   -- phase 3
 *   - 1 x learning  (weight update logic)               -- phase 5
 *   - 1 x OpenRAM SRAM macro (16 x 32-bit, 64 bytes)
 *
 * Current phase: 2 — dendrite array + axon, with direct test interface
 *
 * Pin mapping (phase 2):
 *   ui_in[0]   : inject_valid  — pulse to inject weight into selected dendrite
 *   ui_in[1]   : step          — pulse to run LIF dynamics on all dendrites
 *   ui_in[2]   : clear         — pulse to reset accumulators for next round
 *   ui_in[4:3] : dendrite_sel  — target dendrite for weight injection (0-3)
 *   ui_in[5]   : event_in      — pulse to trigger axon lookup
 *   ui_in[6]   : range_ack     — pulse to acknowledge axon range (test/debug)
 *   ui_in[7]   : reserved
 *   uio_in[7:0]: syn_weight    — signed 8-bit weight value
 *   uo_out[3:0]: spike_out     — fire status of dendrites 0-3
 *   uo_out[4]  : axon_range_valid — high for 1 cycle when axon has a range ready
 *   uo_out[7:5]: reserved (0)
 */

`default_nettype none
`include "macros/sky130_sram_1rw_tiny.v"

module tt_um_openram_top (
    `ifdef USE_POWER_PINS
      input VPWR,
      input VGND,
    `endif
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

    // ==========================================================
    //  Pin decode (phase 2)
    // ==========================================================
    wire       inject_valid = ui_in[0];
    wire       step         = ui_in[1];
    wire       clear        = ui_in[2];
    wire [1:0] dendrite_sel = ui_in[4:3];
    wire       event_in     = ui_in[5];
    wire       range_ack    = ui_in[6];
    wire [7:0] syn_weight   = uio_in;

    // ==========================================================
    //  Axon — event-to-synapse-range mapper
    // ==========================================================
    wire        axon_range_valid;
    wire [3:0]  axon_syn_start;
    wire [4:0]  axon_syn_count;

    axon u_axon (
        .clk         (clk),
        .rst_n       (rst_n),
        .event_valid (event_in),
        .event_id    (8'd0),              // single axon, fixed id
        .range_ack   (range_ack),
        .range_valid (axon_range_valid),
        .syn_start   (axon_syn_start),
        .syn_count   (axon_syn_count)
    );

    // ==========================================================
    //  Dendrite array — 4 independent LIF neurons
    // ==========================================================
    wire [3:0] spike_out;
    wire [15:0] membrane [0:3];

    wire [3:0] dend_syn_valid;
    assign dend_syn_valid[0] = inject_valid & (dendrite_sel == 2'd0);
    assign dend_syn_valid[1] = inject_valid & (dendrite_sel == 2'd1);
    assign dend_syn_valid[2] = inject_valid & (dendrite_sel == 2'd2);
    assign dend_syn_valid[3] = inject_valid & (dendrite_sel == 2'd3);

    genvar gi;
    generate
        for (gi = 0; gi < 4; gi = gi + 1) begin : dend
            dendrite u_dendrite (
                .clk        (clk),
                .rst_n      (rst_n),
                .syn_valid  (dend_syn_valid[gi]),
                .syn_weight (syn_weight),
                .step       (step),
                .clear      (clear),
                .spike_out  (spike_out[gi]),
                .membrane   (membrane[gi])
            );
        end
    endgenerate

    // ==========================================================
    //  SRAM macro — present but idle; synapse module drives it from phase 3
    // ==========================================================
    wire [31:0] sram_dout;

    sky130_sram_1rw_tiny SRAM (
        `ifdef USE_POWER_PINS
          .vccd1 (VPWR),
          .vssd1 (VGND),
        `endif
        .clk0   (clk),
        .csb0   (1'b1),
        .web0   (1'b1),
        .wmask0 (4'b0000),
        .addr0  (4'b0000),
        .din0   (32'b0),
        .dout0  (sram_dout)
    );

    // ==========================================================
    //  Output mapping
    // ==========================================================
    assign uo_out[3:0] = spike_out;
    assign uo_out[4]   = axon_range_valid;
    assign uo_out[7:5] = 3'd0;

    assign uio_out = 8'd0;
    assign uio_oe  = 8'd0;

    wire _unused = &{ena, ui_in[7], sram_dout,
                     axon_syn_start, axon_syn_count, 1'b0};

endmodule
