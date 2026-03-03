/*
 * Learning -- Minimal Hebbian-like weight update rule
 *
 * Role in the PicoNeurocore data flow:
 *   After the dendrite step (integrate/fire), the top-level FSM re-scans
 *   all synapse entries.  For each entry the learning module decides:
 *     - Potentiate (weight += 1) if the synapse was active AND
 *       the target dendrite fired AND learning is enabled.
 *     - Depress    (weight -= 1) if the synapse was active AND
 *       the target dendrite did NOT fire AND learning is enabled.
 *     - No change  otherwise.
 *
 *   Weight is clamped to the signed 8-bit range [-128, +127].
 *   The module is purely combinational; the top FSM latches its outputs
 *   and drives the synapse write-back path.
 */

`default_nettype none

module learning (
    input  wire        learn_en,        // per-synapse learning enable
    input  wire        syn_active,      // synapse entry was valid (participated)
    input  wire        post_spike,      // target dendrite fired this round
    input  wire [7:0]  current_weight,  // signed 8-bit weight from SRAM

    output wire [7:0]  new_weight,      // updated weight (same if no change)
    output wire        weight_changed   // 1 = weight was modified
);

    wire signed [7:0] w = current_weight;
    wire at_max = (w == 8'sd127);
    wire at_min = (w == -8'sd128);

    wire do_update  = learn_en & syn_active;
    wire potentiate = do_update & post_spike;
    wire depress    = do_update & ~post_spike;

    assign new_weight = potentiate ? (at_max ? current_weight : current_weight + 8'd1)
                      : depress    ? (at_min ? current_weight : current_weight - 8'd1)
                      :              current_weight;

    assign weight_changed = (potentiate & ~at_max) | (depress & ~at_min);

endmodule
