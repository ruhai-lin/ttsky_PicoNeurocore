/*
 * Dendrite — Leaky Integrate-and-Fire (LIF) neuron compartment
 *
 * Role in the PicoNeurocore data flow:
 *   synapse module → [syn_valid + syn_weight] → dendrite → [spike_out] → learning
 *
 * Each processing round follows this sequence (enforced by the top-level FSM):
 *   1. 'clear' resets the input accumulator and spike flag
 *   2. One or more 'syn_valid' pulses accumulate weights into input_acc
 *   3. 'step' triggers:  potential += input_acc − leak;  threshold check;  fire/reset
 *   4. 'spike_out' stays valid until the next 'clear'
 *
 * The three control signals (clear, syn_valid, step) are mutually exclusive in time.
 */

`default_nettype none

module dendrite (
    input  wire        clk,
    input  wire        rst_n,

    // Synaptic input — driven by synapse module (or test harness in phase 1)
    input  wire        syn_valid,
    input  wire [7:0]  syn_weight,      // signed 8-bit weight

    // Control — driven by the top-level state machine
    input  wire        step,            // integrate + leak + threshold check
    input  wire        clear,           // prepare for the next processing round

    // Output
    output reg         spike_out,       // 1 = neuron fired (valid from step until clear)
    output wire [15:0] membrane         // current membrane potential (debug / readback)
);

    // ---- LIF parameters (fixed for this prototype) ----
    localparam signed [15:0] V_THRESH = 16'sd100;
    localparam signed [15:0] V_RESET  = 16'sd0;
    localparam signed [15:0] V_LEAK   = 16'sd1;

    // ---- Internal state ----
    reg signed [15:0] potential;
    reg signed [15:0] input_acc;

    assign membrane = potential;

    // Sign-extend the 8-bit weight to 16 bits
    wire signed [15:0] weight_ext = {{8{syn_weight[7]}}, syn_weight};

    // Combinational next-potential (used by the step logic)
    wire signed [15:0] next_potential = potential - V_LEAK + input_acc;

    // ---- Accumulate synaptic contributions ----
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            input_acc <= 16'sd0;
        else if (clear)
            input_acc <= 16'sd0;
        else if (syn_valid)
            input_acc <= input_acc + weight_ext;
    end

    // ---- LIF dynamics: leak, integrate, fire ----
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            potential <= V_RESET;
            spike_out <= 1'b0;
        end else if (clear) begin
            spike_out <= 1'b0;
        end else if (step) begin
            if (next_potential >= V_THRESH) begin
                potential <= V_RESET;
                spike_out <= 1'b1;
            end else begin
                potential <= (next_potential > V_RESET) ? next_potential : V_RESET;
                spike_out <= 1'b0;
            end
        end
    end

endmodule
