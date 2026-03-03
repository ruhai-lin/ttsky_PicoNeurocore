/*
 * Axon — Event-to-synapse-range mapper
 *
 * Role in the PicoNeurocore data flow:
 *   input event --> axon --> [syn_start, syn_count] --> synapse module
 *
 * When an input event arrives (event_valid pulse), the axon determines which
 * segment of the synapse table should be scanned.  In this minimal version,
 * a fixed range is produced: start=0, count=16 (the entire 64-byte SRAM).
 *
 * Interface uses a valid/ack handshake:
 *   - range_valid goes HIGH when a range is ready
 *   - range_valid stays HIGH until range_ack is pulsed (by the top-level FSM)
 *   - syn_start and syn_count are stable while range_valid is HIGH
 *   - new events are ignored while a previous range is outstanding
 *
 * The event_id input is reserved for future multi-axon expansion.
 */

`default_nettype none

module axon (
    input  wire        clk,
    input  wire        rst_n,

    // Event input
    input  wire        event_valid,    // 1-cycle pulse: an event has arrived
    input  wire [7:0]  event_id,       // reserved for future multi-axon routing

    // Synapse range output (valid/ack handshake)
    input  wire        range_ack,      // 1-cycle pulse: FSM consumed the range
    output reg         range_valid,    // HIGH while a range is pending
    output reg  [3:0]  syn_start,      // SRAM start address
    output reg  [4:0]  syn_count       // number of entries to read (0-16)
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            range_valid <= 1'b0;
            syn_start   <= 4'd0;
            syn_count   <= 5'd0;
        end else if (range_ack) begin
            range_valid <= 1'b0;
        end else if (event_valid && !range_valid) begin
            range_valid <= 1'b1;
            syn_start   <= 4'd0;
            syn_count   <= 5'd16;
        end
    end

endmodule
