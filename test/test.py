# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

@cocotb.test()
async def test_sram_counter(dut):
    """Test 8-bit counter with SRAM: write counter values, then read back."""

    dut._log.info("Start")

    # Single clock, 50 MHz (20 ns period)
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())

    # Reset
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    # Phase 1: write mode (ui_in[0]=1). Run 16 cycles so we write 0..15 to addresses 0..15
    dut.ui_in.value = 1  # write_enable = 1
    await ClockCycles(dut.clk, 16)

    # Phase 2: read mode (ui_in[0]=0). Counter continues; addr = counter[3:0], we read back stored value
    dut.ui_in.value = 0  # write_enable = 0
    await ClockCycles(dut.clk, 1)  # one cycle for read to settle if model has latency

    for i in range(16):
        await ClockCycles(dut.clk, 1)
        # SRAM has 1-cycle read latency: uo_out is the value at addr from previous cycle.
        # After the initial wait, first uo_out is from addr 0 (value 0). Then we see 1,2,...,15,0.
        read_val = dut.uo_out.value.integer
        expected = (i + 2)
        dut._log.info("read cycle %d: uo_out=%d expected=%d", i, read_val, expected)
        assert read_val == expected, "uo_out=%d expected=%d" % (read_val, expected)

    dut._log.info("All 16 read-back values matched.")
