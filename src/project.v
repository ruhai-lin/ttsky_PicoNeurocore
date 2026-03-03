/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none
//`timescale 1ns / 1ps
`include "macros/sky130_sram_1rw_tiny.v"

module tt_um_openram_top (
    `ifdef USE_POWER_PINS
      input VPWR,
      input VGND,
    `endif
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path (unused)
    output wire [7:0] uio_out,  // IOs: Output path (unused)
    output wire [7:0] uio_oe,   // IOs: Enable path (unused, 0=input)
    input  wire       ena,      // always 1 when the design is powered
    input  wire       clk,      // main clock
    input  wire       rst_n     // async reset, active low
);

  // Simple 8-bit counter
  reg [7:0] counter;

  // Use ui_in[0] as a simple write-enable: 1=write, 0=read
  wire write_enable = ui_in[0];

  // SRAM interface signals
  localparam ADDR_WIDTH  = 4;
  localparam DATA_WIDTH  = 32;
  localparam WMASK_WIDTH = 4;

  wire [ADDR_WIDTH-1:0]  addr;
  wire [DATA_WIDTH-1:0]  din;
  wire [DATA_WIDTH-1:0]  dout;
  wire [WMASK_WIDTH-1:0] wmask;

  wire csb;
  wire web;

  // Counter: increments on each clock when enabled, resets to 0 on rst_n low
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      counter <= 8'd0;
    else if (ena)
      counter <= counter + 8'd1;
  end

  // Address: use low 4 bits of the counter (16 locations)
  assign addr  = counter[3:0];

  // Data to write: store the current counter value in the low 8 bits
  assign din   = {24'd0, counter};

  // Always write all bytes when writing
  assign wmask = {WMASK_WIDTH{1'b1}};

  // Always keep the SRAM enabled
  assign csb = 1'b0;

  // Active-low write enable: write when write_enable is 1
  assign web = ~write_enable;

  // SRAM instance
  sky130_sram_1rw_tiny SRAM (
    `ifdef USE_POWER_PINS
      .vccd1(VPWR),
      .vssd1(VGND),
    `endif
      .clk0   (clk),
      .csb0   (csb),
      .web0   (web),
      .wmask0 (wmask),
      .addr0  (addr),
      .din0   (din),
      .dout0  (dout)
  );

  // Drive outputs:
  // - uo_out shows the low 8 bits read from SRAM
  assign uo_out = dout[7:0];

  // All bidirectional IOs unused
  assign uio_out = 8'd0;
  assign uio_oe  = 8'd0;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in[7:1], uio_in, 1'b0};

endmodule
