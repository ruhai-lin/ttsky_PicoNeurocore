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
 *   ui_in[5]    event_in      -- trigger full neurocore processing cycle
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
 *
 * Top-level FSM flow per event:
 *   IDLE -> AXON_WAIT -> CLEAR
 *     -> [SYN_READ -> SYN_WAIT -> SYN_NEXT] x N       (synapse scan)
 *     -> STEP
 *     -> [LEARN_READ -> LEARN_WAIT -> LEARN_WRITE? -> LEARN_NEXT] x N
 *     -> DONE -> IDLE
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
    wire       mode    = ui_in[7];
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

    // Learning write-back interface
    reg         learn_write_start;
    reg  [3:0]  learn_write_addr;
    reg  [31:0] learn_write_data;

    synapse u_synapse (
        .clk          (clk),
        .rst_n        (rst_n),
        .read_start   (syn_read_start),
        .read_addr    (syn_read_addr),
        .write_start  (learn_write_start),
        .write_addr   (learn_write_addr),
        .write_data   (learn_write_data),
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
    //  Learning module (combinational)
    // ==========================================================
    wire        learn_post_spike = spike_out[syn_entry_dendrite];
    wire [7:0]  learn_new_weight;
    wire        learn_weight_changed;

    wire [31:0] learn_updated_word = {syn_entry_raw[31:24],
                                      learn_new_weight,
                                      syn_entry_raw[15:0]};

    learning u_learning (
        .learn_en       (syn_entry_learn_en),
        .syn_active     (syn_entry_valid),
        .post_spike     (learn_post_spike),
        .current_weight (syn_entry_weight),
        .new_weight     (learn_new_weight),
        .weight_changed (learn_weight_changed)
    );

    // ==========================================================
    //  Top-level FSM
    // ==========================================================
    localparam [3:0]
        TOP_IDLE        = 4'd0,
        TOP_AXON_WAIT   = 4'd1,
        TOP_CLEAR       = 4'd2,
        TOP_SYN_READ    = 4'd3,
        TOP_SYN_WAIT    = 4'd4,
        TOP_SYN_NEXT    = 4'd5,
        TOP_STEP        = 4'd6,
        TOP_LEARN_READ  = 4'd7,
        TOP_LEARN_WAIT  = 4'd8,
        TOP_LEARN_WRITE = 4'd9,
        TOP_LEARN_NEXT  = 4'd10,
        TOP_DONE        = 4'd11;

    reg [3:0]  top_state;
    wire       top_busy = (top_state != TOP_IDLE);

    reg [4:0]  syn_remaining;
    reg [3:0]  syn_cur_addr;
    reg [3:0]  range_start;
    reg [4:0]  range_count;

    // FSM-driven dendrite control signals
    reg        fsm_syn_valid;
    reg [1:0]  fsm_syn_target;
    reg [7:0]  fsm_syn_weight;
    reg        fsm_step;
    reg        fsm_clear;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            top_state         <= TOP_IDLE;
            syn_read_start    <= 1'b0;
            syn_read_addr     <= 4'd0;
            fsm_range_ack     <= 1'b0;
            syn_remaining     <= 5'd0;
            syn_cur_addr      <= 4'd0;
            range_start       <= 4'd0;
            range_count       <= 5'd0;
            fsm_syn_valid     <= 1'b0;
            fsm_syn_target    <= 2'd0;
            fsm_syn_weight    <= 8'd0;
            fsm_step          <= 1'b0;
            fsm_clear         <= 1'b0;
            learn_write_start <= 1'b0;
            learn_write_addr  <= 4'd0;
            learn_write_data  <= 32'd0;
        end else begin
            // Pulse defaults
            syn_read_start    <= 1'b0;
            fsm_range_ack     <= 1'b0;
            fsm_syn_valid     <= 1'b0;
            fsm_step          <= 1'b0;
            fsm_clear         <= 1'b0;
            learn_write_start <= 1'b0;

            case (top_state)
            TOP_IDLE: begin
                if (event_in)
                    top_state <= TOP_AXON_WAIT;
            end

            TOP_AXON_WAIT: begin
                if (axon_range_valid) begin
                    fsm_range_ack <= 1'b1;
                    syn_cur_addr  <= axon_syn_start;
                    syn_remaining <= axon_syn_count;
                    range_start   <= axon_syn_start;
                    range_count   <= axon_syn_count;
                    top_state     <= TOP_CLEAR;
                end
            end

            TOP_CLEAR: begin
                fsm_clear <= 1'b1;
                if (syn_remaining > 5'd0)
                    top_state <= TOP_SYN_READ;
                else
                    top_state <= TOP_STEP;
            end

            // ---- Synapse scan: read entries, send weights to dendrites ----
            TOP_SYN_READ: begin
                syn_read_start <= 1'b1;
                syn_read_addr  <= syn_cur_addr;
                top_state      <= TOP_SYN_WAIT;
            end

            TOP_SYN_WAIT: begin
                if (syn_read_done) begin
                    if (syn_entry_valid) begin
                        fsm_syn_valid  <= 1'b1;
                        fsm_syn_target <= syn_entry_dendrite;
                        fsm_syn_weight <= syn_entry_weight;
                    end
                    syn_remaining <= syn_remaining - 5'd1;
                    syn_cur_addr  <= syn_cur_addr + 4'd1;
                    top_state     <= TOP_SYN_NEXT;
                end
            end

            TOP_SYN_NEXT: begin
                if (syn_remaining > 5'd0)
                    top_state <= TOP_SYN_READ;
                else
                    top_state <= TOP_STEP;
            end

            // ---- Step: trigger LIF dynamics, then start learning scan ----
            TOP_STEP: begin
                fsm_step <= 1'b1;
                if (range_count > 5'd0) begin
                    syn_cur_addr  <= range_start;
                    syn_remaining <= range_count;
                    top_state     <= TOP_LEARN_READ;
                end else begin
                    top_state <= TOP_DONE;
                end
            end

            // ---- Learning scan: re-read entries, update weights ----
            TOP_LEARN_READ: begin
                syn_read_start <= 1'b1;
                syn_read_addr  <= syn_cur_addr;
                top_state      <= TOP_LEARN_WAIT;
            end

            TOP_LEARN_WAIT: begin
                if (syn_read_done) begin
                    if (learn_weight_changed) begin
                        learn_write_start <= 1'b1;
                        learn_write_addr  <= syn_cur_addr;
                        learn_write_data  <= learn_updated_word;
                        top_state         <= TOP_LEARN_WRITE;
                    end else begin
                        top_state <= TOP_LEARN_NEXT;
                    end
                    syn_remaining <= syn_remaining - 5'd1;
                    syn_cur_addr  <= syn_cur_addr + 4'd1;
                end
            end

            TOP_LEARN_WRITE: begin
                if (syn_write_done)
                    top_state <= TOP_LEARN_NEXT;
            end

            TOP_LEARN_NEXT: begin
                if (syn_remaining > 5'd0)
                    top_state <= TOP_LEARN_READ;
                else
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
    //  Dendrite array -- 4 independent LIF neurons
    //
    //  Inputs are OR'd: direct test interface | FSM-driven signals.
    //  When the FSM is idle all fsm_* signals are 0, so the direct
    //  test interface works transparently.
    // ==========================================================
    wire [3:0] spike_out;
    wire [15:0] membrane [0:3];

    wire [3:0] dend_syn_valid;
    assign dend_syn_valid[0] = (inject_valid & (dendrite_sel == 2'd0))
                             | (fsm_syn_valid & (fsm_syn_target == 2'd0));
    assign dend_syn_valid[1] = (inject_valid & (dendrite_sel == 2'd1))
                             | (fsm_syn_valid & (fsm_syn_target == 2'd1));
    assign dend_syn_valid[2] = (inject_valid & (dendrite_sel == 2'd2))
                             | (fsm_syn_valid & (fsm_syn_target == 2'd2));
    assign dend_syn_valid[3] = (inject_valid & (dendrite_sel == 2'd3))
                             | (fsm_syn_valid & (fsm_syn_target == 2'd3));

    wire [7:0] dend_weight = fsm_syn_valid ? fsm_syn_weight : data_in;
    wire       dend_step   = step_pin  | fsm_step;
    wire       dend_clear  = clear_pin | fsm_clear;

    genvar gi;
    generate
        for (gi = 0; gi < 4; gi = gi + 1) begin : dend
            dendrite u_dendrite (
                .clk        (clk),
                .rst_n      (rst_n),
                .syn_valid  (dend_syn_valid[gi]),
                .syn_weight (dend_weight),
                .step       (dend_step),
                .clear      (dend_clear),
                .spike_out  (spike_out[gi]),
                .membrane   (membrane[gi])
            );
        end
    endgenerate

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

    wire _unused = &{ena, ui_in[6], 1'b0};

endmodule
