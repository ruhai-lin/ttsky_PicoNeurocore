/*
 * PicoNeurocore -- Top-level module (Tiny Tapeout wrapper)
 *
 * A minimal Loihi 1-style neurocore prototype built on a single 64-byte
 * OpenRAM SRAM macro (16 x 32-bit words = 16 synapses).
 *
 * Main data flow:
 *   input event -> axon (range lookup) -> synapse (SRAM read/parse)
 *     -> dendrite (LIF integrate/fire) -> learning (weight update)
 *     -> synapse (SRAM write-back)
 *
 * Operating modes (selected by ui_in[7]):
 *   mode=0 (run):     Event processing + direct dendrite test interface
 *   mode=1 (program): Byte-by-byte SRAM programming via external pins
 *
 * Run-mode pin map:
 *   ui_in[0]    inject_valid  -- direct dendrite test: send weight
 *   ui_in[1]    step          -- direct dendrite test: LIF dynamics
 *   ui_in[2]    clear         -- direct dendrite test: reset accumulators
 *   ui_in[4:3]  dendrite_sel  -- direct dendrite test: target (0-3)
 *   ui_in[5]    event_in      -- trigger axon -> synapse FSM flow
 *   uio_in[7:0] syn_weight   -- 8-bit weight / data byte
 *   uo_out[3:0] spike_out    -- dendrite fire flags
 *   uo_out[4]   top_busy     -- 1 while FSM is processing
 *   uo_out[6]   syn_entry_valid -- last-read synapse valid bit
 *
 * Program-mode pin map:
 *   ui_in[3:0]  addr         -- SRAM word address (0-15)
 *   ui_in[4]    byte_load    -- load uio_in byte into 32-bit accumulator
 *   ui_in[5]    word_write   -- write accumulated word to SRAM[addr]
 *   uio_in[7:0] data_byte   -- byte to accumulate
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
    //  Pin decode
    // ==========================================================
    wire       mode   = ui_in[7];
    wire [7:0] data_in = uio_in;

    // Run-mode signals (gated by ~mode)
    wire       inject_valid = ~mode & ui_in[0];
    wire       step_pin     = ~mode & ui_in[1];
    wire       clear_pin    = ~mode & ui_in[2];
    wire [1:0] dendrite_sel = ui_in[4:3];
    wire       event_in     = ~mode & ui_in[5];

    // Program-mode signals (gated by mode)
    wire       prog_byte_load  = mode & ui_in[4];
    wire       prog_word_write = mode & ui_in[5];

    // ==========================================================
    //  SRAM programming -- byte accumulator + write driver
    // ==========================================================
    reg [31:0] prog_word;
    reg [1:0]  prog_byte_cnt;
    reg        prog_sram_csb;
    reg        prog_sram_web;
    reg [3:0]  prog_sram_addr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            prog_word      <= 32'd0;
            prog_byte_cnt  <= 2'd0;
            prog_sram_csb  <= 1'b1;
            prog_sram_web  <= 1'b1;
            prog_sram_addr <= 4'd0;
        end else begin
            prog_sram_csb <= 1'b1;
            prog_sram_web <= 1'b1;
            if (prog_byte_load) begin
                case (prog_byte_cnt)
                    2'd0: prog_word[7:0]   <= data_in;
                    2'd1: prog_word[15:8]  <= data_in;
                    2'd2: prog_word[23:16] <= data_in;
                    2'd3: prog_word[31:24] <= data_in;
                endcase
                prog_byte_cnt <= prog_byte_cnt + 2'd1;
            end else if (prog_word_write) begin
                prog_sram_csb  <= 1'b0;
                prog_sram_web  <= 1'b0;
                prog_sram_addr <= ui_in[3:0];
                prog_byte_cnt  <= 2'd0;
            end
        end
    end

    // ==========================================================
    //  Axon
    // ==========================================================
    wire        axon_range_valid;
    wire [3:0]  axon_syn_start;
    wire [4:0]  axon_syn_count;
    reg         fsm_range_ack;

    axon u_axon (
        .clk         (clk),
        .rst_n       (rst_n),
        .event_valid (event_in),
        .event_id    (8'd0),
        .range_ack   (fsm_range_ack),
        .range_valid (axon_range_valid),
        .syn_start   (axon_syn_start),
        .syn_count   (axon_syn_count)
    );

    // ==========================================================
    //  Synapse
    // ==========================================================
    reg         syn_read_start;
    reg  [3:0]  syn_read_addr;
    wire        syn_read_done;
    wire        syn_write_done;
    wire        syn_entry_valid;
    wire        syn_entry_learn_en;
    wire [1:0]  syn_entry_dendrite;
    wire [7:0]  syn_entry_weight;
    wire [31:0] syn_entry_raw;
    wire [3:0]  syn_sram_addr;
    wire [31:0] syn_sram_din;
    wire [3:0]  syn_sram_wmask;
    wire        syn_sram_csb;
    wire        syn_sram_web;

    synapse u_synapse (
        .clk          (clk),
        .rst_n        (rst_n),
        .read_start   (syn_read_start),
        .read_addr    (syn_read_addr),
        .write_start  (1'b0),
        .write_addr   (4'd0),
        .write_data   (32'd0),
        .read_done    (syn_read_done),
        .write_done   (syn_write_done),
        .entry_valid  (syn_entry_valid),
        .entry_learn_en(syn_entry_learn_en),
        .entry_dendrite(syn_entry_dendrite),
        .entry_weight (syn_entry_weight),
        .entry_raw    (syn_entry_raw),
        .sram_addr    (syn_sram_addr),
        .sram_din     (syn_sram_din),
        .sram_wmask   (syn_sram_wmask),
        .sram_csb     (syn_sram_csb),
        .sram_web     (syn_sram_web),
        .sram_dout    (sram_dout)
    );

    // ==========================================================
    //  SRAM mux + macro instance
    // ==========================================================
    wire [3:0]  sram_addr  = mode ? prog_sram_addr : syn_sram_addr;
    wire [31:0] sram_din   = mode ? prog_word      : syn_sram_din;
    wire [3:0]  sram_wmask = mode ? 4'b1111        : syn_sram_wmask;
    wire        sram_csb   = mode ? prog_sram_csb  : syn_sram_csb;
    wire        sram_web   = mode ? prog_sram_web  : syn_sram_web;
    wire [31:0] sram_dout;

    sky130_sram_1rw_tiny SRAM (
        `ifdef USE_POWER_PINS
          .vccd1 (VPWR),
          .vssd1 (VGND),
        `endif
        .clk0   (clk),
        .csb0   (sram_csb),
        .web0   (sram_web),
        .wmask0 (sram_wmask),
        .addr0  (sram_addr),
        .din0   (sram_din),
        .dout0  (sram_dout)
    );

    // ==========================================================
    //  Dendrite array -- 4 independent LIF neurons
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
                .syn_weight (data_in),
                .step       (step_pin),
                .clear      (clear_pin),
                .spike_out  (spike_out[gi]),
                .membrane   (membrane[gi])
            );
        end
    endgenerate

    // ==========================================================
    //  Top-level FSM -- mini version for phase 3
    //  Handles: event -> axon lookup -> single synapse read
    // ==========================================================
    localparam [2:0]
        TOP_IDLE      = 3'd0,
        TOP_AXON_WAIT = 3'd1,
        TOP_SYN_WAIT  = 3'd2,
        TOP_DONE      = 3'd3;

    reg [2:0] top_state;
    wire top_busy = (top_state != TOP_IDLE);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            top_state      <= TOP_IDLE;
            syn_read_start <= 1'b0;
            syn_read_addr  <= 4'd0;
            fsm_range_ack  <= 1'b0;
        end else begin
            syn_read_start <= 1'b0;
            fsm_range_ack  <= 1'b0;
            case (top_state)
            TOP_IDLE: begin
                if (event_in)
                    top_state <= TOP_AXON_WAIT;
            end
            TOP_AXON_WAIT: begin
                if (axon_range_valid) begin
                    fsm_range_ack  <= 1'b1;
                    syn_read_addr  <= axon_syn_start;
                    syn_read_start <= 1'b1;
                    top_state      <= TOP_SYN_WAIT;
                end
            end
            TOP_SYN_WAIT: begin
                if (syn_read_done)
                    top_state <= TOP_DONE;
            end
            TOP_DONE: begin
                top_state <= TOP_IDLE;
            end
            default: top_state <= TOP_IDLE;
            endcase
        end
    end

    // ==========================================================
    //  Output mapping
    // ==========================================================
    assign uo_out[3:0] = spike_out;
    assign uo_out[4]   = top_busy;
    assign uo_out[5]   = 1'b0;
    assign uo_out[6]   = syn_entry_valid;
    assign uo_out[7]   = 1'b0;

    assign uio_out = 8'd0;
    assign uio_oe  = 8'd0;

    wire _unused = &{ena, ui_in[6],
                     axon_syn_count, syn_write_done,
                     syn_entry_learn_en, syn_entry_dendrite,
                     syn_entry_weight, syn_entry_raw, 1'b0};

endmodule
