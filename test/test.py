# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

"""
PicoNeurocore — cocotb test suite

Phase 1: Verify dendrite (LIF) behaviour via the direct test interface.

Pin mapping (phase 1):
  ui_in[0]    inject_valid   — pulse to send weight to selected dendrite
  ui_in[1]    step           — pulse to run LIF dynamics on all dendrites
  ui_in[2]    clear          — pulse to reset accumulators for next round
  ui_in[4:3]  dendrite_sel   — target dendrite (0–3)
  uio_in[7:0] syn_weight     — signed 8-bit weight
  uo_out[3:0] spike_out      — fire status of dendrites 0–3
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# ── ui_in bit-field helpers ──────────────────────────────────────────
INJECT = 1 << 0
STEP   = 1 << 1
CLEAR  = 1 << 2

def dend_sel(d):
    return (d & 0x3) << 3

def weight_u8(val):
    """Signed int → unsigned 8-bit (two's complement)."""
    return val & 0xFF

# ── Reusable primitives ─────────────────────────────────────────────

async def reset(dut):
    dut.rst_n.value = 0
    dut.ena.value   = 1
    dut.ui_in.value  = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 4)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

async def inject_weight(dut, dendrite_id, weight):
    """Send one syn_valid pulse with the given weight to a specific dendrite."""
    dut.uio_in.value = weight_u8(weight)
    dut.ui_in.value  = INJECT | dend_sel(dendrite_id)
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)

async def pulse_step(dut):
    dut.ui_in.value = STEP
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)

async def pulse_clear(dut):
    dut.ui_in.value = CLEAR
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)

def get_spikes(dut):
    return dut.uo_out.value.integer & 0x0F

# ── Tests ────────────────────────────────────────────────────────────

@cocotb.test()
async def test_dendrite_fire(dut):
    """Inject enough weight to exceed threshold, verify fire and reset."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=120 → potential = 0 − 1(leak) + 120 = 119 ≥ 100(thresh) → fire
    await inject_weight(dut, 0, 120)
    await pulse_step(dut)

    spikes = get_spikes(dut)
    assert spikes & 1, f"Dendrite 0 should fire, got spikes={spikes:#06b}"
    assert not (spikes & 0xE), f"Only dendrite 0 should fire, got spikes={spikes:#06b}"

    # After clear, spike flag must drop
    await pulse_clear(dut)
    assert get_spikes(dut) == 0, "Spikes should be cleared after clear pulse"


@cocotb.test()
async def test_dendrite_no_fire(dut):
    """Inject weight below threshold, verify no fire."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=30 → potential = 0 − 1 + 30 = 29 < 100 → no fire
    await inject_weight(dut, 1, 30)
    await pulse_step(dut)

    assert get_spikes(dut) == 0, "No dendrite should fire with weight=30"


@cocotb.test()
async def test_dendrite_accumulation(dut):
    """Membrane potential persists across rounds (leaky integration)."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Round 1: inject 60 → potential = 0 − 1 + 60 = 59, no fire
    await inject_weight(dut, 0, 60)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: should not fire (potential≈59)"
    await pulse_clear(dut)

    # Round 2: inject 50 → potential = 59 − 1 + 50 = 108 ≥ 100 → fire
    await inject_weight(dut, 0, 50)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 2: dendrite 0 should fire (potential≈108)"


@cocotb.test()
async def test_dendrite_independence(dut):
    """Different dendrites maintain independent state."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Dendrite 2: weight=120 (will fire), dendrite 3: weight=30 (won't fire)
    await inject_weight(dut, 2, 120)
    await inject_weight(dut, 3, 30)
    await pulse_step(dut)

    spikes = get_spikes(dut)
    assert spikes & 0x4, f"Dendrite 2 should fire, got spikes={spikes:#06b}"
    assert not (spikes & 0x8), f"Dendrite 3 should not fire, got spikes={spikes:#06b}"
    assert not (spikes & 0x3), f"Dendrites 0,1 untouched, got spikes={spikes:#06b}"


@cocotb.test()
async def test_dendrite_leak(dut):
    """Verify that leak reduces potential by 1 each step (even without input)."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Round 1: inject 99 → potential = 0 − 1 + 99 = 98
    await inject_weight(dut, 0, 99)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: no fire (potential=98)"
    await pulse_clear(dut)

    # Round 2: no inject → potential = 98 − 1 = 97
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 2: no fire (potential=97)"
    await pulse_clear(dut)

    # Round 3: inject 2 → potential = 97 − 1 + 2 = 98, no fire
    # (without leak it would be 99 + 2 = 101 → fire, so this proves leak works)
    await inject_weight(dut, 0, 2)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 3: no fire (potential=98, leak confirmed)"
    await pulse_clear(dut)

    # Round 4: inject 3 → potential = 98 − 1 + 3 = 100 ≥ 100 → fire
    await inject_weight(dut, 0, 3)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 4: dendrite 0 should fire (potential=100)"


@cocotb.test()
async def test_dendrite_inhibition(dut):
    """Negative (inhibitory) weight reduces potential."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Round 1: inject +80 → potential = 79
    await inject_weight(dut, 0, 80)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

    # Round 2: inject −30 → potential = 79 − 1 + (−30) = 48
    await inject_weight(dut, 0, -30)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

    # Round 3: inject +60 → potential = 48 − 1 + 60 = 107 ≥ 100 → fire
    await inject_weight(dut, 0, 60)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Should fire: excitation overcomes prior inhibition"


# ── Phase 2: Axon tests ─────────────────────────────────────────────

EVENT_IN  = 1 << 5   # ui_in[5]
RANGE_ACK = 1 << 6   # ui_in[6]

def get_range_valid(dut):
    return (dut.uo_out.value.integer >> 4) & 1

@cocotb.test()
async def test_axon_range(dut):
    """Axon produces a valid synapse range and holds it until acknowledged."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Pulse event_in
    dut.ui_in.value = EVENT_IN
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)

    # range_valid should be HIGH and stay HIGH (valid/ack handshake)
    assert get_range_valid(dut) == 1, "range_valid should be high after event"

    # It should persist for another cycle
    await ClockCycles(dut.clk, 1)
    assert get_range_valid(dut) == 1, "range_valid should persist until ack"

    # Acknowledge the range
    dut.ui_in.value = RANGE_ACK
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)

    assert get_range_valid(dut) == 0, "range_valid should drop after ack"


@cocotb.test()
async def test_axon_no_event(dut):
    """Without event_in, axon stays idle."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await ClockCycles(dut.clk, 5)
    assert get_range_valid(dut) == 0, "range_valid should stay low without event"


@cocotb.test()
async def test_axon_ignores_event_while_busy(dut):
    """A second event is ignored while a range is still outstanding."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # First event
    dut.ui_in.value = EVENT_IN
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)
    assert get_range_valid(dut) == 1

    # Second event while range is outstanding (should be ignored)
    dut.ui_in.value = EVENT_IN
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)
    assert get_range_valid(dut) == 1, "range_valid still high, second event ignored"

    # Ack clears the range
    dut.ui_in.value = RANGE_ACK
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, 1)
    assert get_range_valid(dut) == 0, "range_valid cleared after ack"
