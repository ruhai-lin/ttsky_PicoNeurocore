/*
 * Synapse — SRAM interface and synapse-table entry parser
 *
 * Role in the PicoNeurocore data flow:
 *   axon --> [range] --> synapse <--> SRAM <--> learning
 *                              |
 *                              +--> [weight, dendrite_id] --> dendrite
 *
 * Synapse entry format (one 32-bit SRAM word per synapse):
 *   [31]    valid       -- entry is active
 *   [30]    learn_en    -- learning is allowed
 *   [29:28] dendrite_id -- target dendrite (0-3)
 *   [27:24] reserved
 *   [23:16] weight      -- signed 8-bit synaptic weight
 *   [15:0]  reserved    -- for future expansion (tag, delay, etc.)
 *
 * The module executes one SRAM read or write at a time, driven by
 * the top-level FSM via read_start / write_start pulses.
 *
 * SRAM timing (single-port OpenRAM, negedge read/write):
 *   Read:  issue cycle N -> SRAM latches N+1 posedge -> data at N+1 negedge -> capture N+2
 *   Write: issue cycle N -> SRAM latches N+1 posedge -> write at N+1 negedge -> done N+2
 */

`default_nettype none

module synapse (
    input  wire        clk,
    input  wire        rst_n,

    // Read command (from top-level FSM)
    input  wire        read_start,
    input  wire [3:0]  read_addr,

    // Write command (from learning path in phase 5)
    input  wire        write_start,
    input  wire [3:0]  write_addr,
    input  wire [31:0] write_data,

    // Status
    output reg         read_done,
    output reg         write_done,

    // Parsed synapse fields (stable from read_done until next read_start)
    output wire        entry_valid,
    output wire        entry_learn_en,
    output wire [1:0]  entry_dendrite,
    output wire [7:0]  entry_weight,
    output wire [31:0] entry_raw,

    // SRAM interface -- directly wired to OpenRAM macro via top-level mux
    output reg  [3:0]  sram_addr,
    output reg  [31:0] sram_din,
    output reg  [3:0]  sram_wmask,
    output reg         sram_csb,
    output reg         sram_web,
    input  wire [31:0] sram_dout
);

    // ---- Internal FSM ----
    localparam [2:0]
        S_IDLE      = 3'd0,
        S_READ_CMD  = 3'd1,
        S_READ_CAP  = 3'd2,
        S_WRITE_CMD = 3'd3,
        S_WRITE_FIN = 3'd4;

    reg [2:0]  state;
    reg [31:0] captured;

    // ---- Field extraction from the captured word ----
    assign entry_raw      = captured;
    assign entry_valid    = captured[31];
    assign entry_learn_en = captured[30];
    assign entry_dendrite = captured[29:28];
    assign entry_weight   = captured[23:16];

    // ---- FSM ----
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= S_IDLE;
            captured   <= 32'd0;
            read_done  <= 1'b0;
            write_done <= 1'b0;
            sram_csb   <= 1'b1;
            sram_web   <= 1'b1;
            sram_addr  <= 4'd0;
            sram_din   <= 32'd0;
            sram_wmask <= 4'b0000;
        end else begin
            read_done  <= 1'b0;
            write_done <= 1'b0;
            sram_csb   <= 1'b1;
            sram_web   <= 1'b1;

            case (state)
            S_IDLE: begin
                if (read_start) begin
                    sram_addr <= read_addr;
                    sram_csb  <= 1'b0;
                    sram_web  <= 1'b1;
                    state     <= S_READ_CMD;
                end else if (write_start) begin
                    sram_addr  <= write_addr;
                    sram_din   <= write_data;
                    sram_wmask <= 4'b1111;
                    sram_csb   <= 1'b0;
                    sram_web   <= 1'b0;
                    state      <= S_WRITE_CMD;
                end
            end

            S_READ_CMD: begin
                state <= S_READ_CAP;
            end

            S_READ_CAP: begin
                captured  <= sram_dout;
                read_done <= 1'b1;
                state     <= S_IDLE;
            end

            S_WRITE_CMD: begin
                state <= S_WRITE_FIN;
            end

            S_WRITE_FIN: begin
                write_done <= 1'b1;
                state      <= S_IDLE;
            end

            default: state <= S_IDLE;
            endcase
        end
    end

endmodule
