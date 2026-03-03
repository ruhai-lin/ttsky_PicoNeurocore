# SPDX-FileCopyrightText: (c) 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

"""
PicoNeurocore -- cocotb test suite

Phase 1: Verify dendrite (LIF) behaviour via the direct test interface.
Phase 2: Verify event -> axon -> top-level FSM flow.
Phase 3: Verify SRAM programming and synapse entry read/parse.

Run-mode pin map (ui_in[7]=0):
  ui_in[0]    inject_valid  -- pulse to send weight to selected dendrite
  ui_in[1]    step          -- pulse to run LIF dynamics on all dendrites
  ui_in[2]    clear         -- pulse to reset accumulators for next round
  ui_in[4:3]  dendrite_sel  -- target dendrite (0-3)
  ui_in[5]    event_in      -- pulse to trigger axon -> synapse FSM flow
  uio_in[7:0] syn_weight   -- signed 8-bit weight
  uo_out[3:0] spike_out    -- fire status of dendrites 0-3
  uo_out[4]   top_busy     -- 1 while FSM is processing
  uo_out[6]   syn_entry_valid -- last-read synapse valid bit

Program-mode pin map (ui_in[7]=1):
  ui_in[3:0]  addr         -- SRAM word address (0-15)
  ui_in[4]    byte_load    -- load uio_in byte into 32-bit accumulator
  ui_in[5]    word_write   -- write accumulated word to SRAM[addr]
  uio_in[7:0] data_byte   -- byte to accumulate
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# -- ui_in bit-field constants ------------------------------------------------
INJECT   = 1 << 0
STEP     = 1 << 1
CLEAR    = 1 << 2
EVENT_IN = 1 << 5
MODE_BIT = 1 << 7

def dend_sel(d):
    return (d & 0x3) << 3

def weight_u8(val):
    """Signed int -> unsigned 8-bit (two's complement)."""
    return val & 0xFF

# -- Output pin helpers (X-safe: treat unknown bits as 0) ---------------------

def _uo_bit(dut, bit_pos):
    """Read a single bit from uo_out, treating X/Z as 0."""
    binstr = dut.uo_out.value.binstr
    idx = len(binstr) - 1 - bit_pos
    return 1 if binstr[idx] == '1' else 0

def get_spikes(dut):
    return (_uo_bit(dut, 3) << 3 | _uo_bit(dut, 2) << 2
            | _uo_bit(dut, 1) << 1 | _uo_bit(dut, 0))

def get_top_busy(dut):
    return _uo_bit(dut, 4)

def get_syn_entry_valid(dut):
    return _uo_bit(dut, 6)

# -- Reusable primitives ------------------------------------------------------

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

async def trigger_event(dut):
    """Pulse event_in to start the top-level FSM."""
    dut.ui_in.value = EVENT_IN
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = 0

async def wait_fsm_idle(dut, max_cycles=30):
    """Wait for the top FSM to return to IDLE (top_busy=0)."""
    for _ in range(max_cycles):
        await ClockCycles(dut.clk, 1)
        if get_top_busy(dut) == 0:
            return
    assert False, "FSM did not return to IDLE within max cycles"

async def sram_program_word(dut, addr, word):
    """Program a 32-bit word into SRAM at the given address (0-15).

    Uses program mode: load 4 bytes (LSB first) then trigger word_write.
    """
    for i in range(4):
        byte_val = (word >> (i * 8)) & 0xFF
        dut.uio_in.value = byte_val
        dut.ui_in.value = MODE_BIT | (1 << 4)       # mode=1, byte_load=1
        await ClockCycles(dut.clk, 1)
        dut.ui_in.value = MODE_BIT                   # deassert byte_load
        await ClockCycles(dut.clk, 1)

    # Write accumulated word to SRAM
    dut.ui_in.value = MODE_BIT | (1 << 5) | (addr & 0xF)  # word_write + addr
    await ClockCycles(dut.clk, 1)
    dut.ui_in.value = MODE_BIT                       # deassert word_write
    await ClockCycles(dut.clk, 2)                    # let SRAM latch

    # Return to run mode
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 1)

def build_synapse_word(valid, learn_en, dendrite_id, weight):
    """Build a 32-bit synapse entry word.

    Format: [31] valid | [30] learn_en | [29:28] dendrite_id
            | [27:24] reserved | [23:16] weight | [15:0] reserved
    """
    w = 0
    if valid:
        w |= (1 << 31)
    if learn_en:
        w |= (1 << 30)
    w |= (dendrite_id & 0x3) << 28
    w |= (weight & 0xFF) << 16
    return w

# ==============================================================================
#  Phase 1: Dendrite tests
# ==============================================================================

@cocotb.test()
async def test_dendrite_fire(dut):
    """Inject enough weight to exceed threshold, verify fire and reset."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=120 -> potential = 0 - 1(leak) + 120 = 119 >= 100(thresh) -> fire
    await inject_weight(dut, 0, 120)
    await pulse_step(dut)

    spikes = get_spikes(dut)
    assert spikes & 1, f"Dendrite 0 should fire, got spikes={spikes:#06b}"
    assert not (spikes & 0xE), f"Only dendrite 0 should fire, got spikes={spikes:#06b}"

    await pulse_clear(dut)
    assert get_spikes(dut) == 0, "Spikes should be cleared after clear pulse"


@cocotb.test()
async def test_dendrite_no_fire(dut):
    """Inject weight below threshold, verify no fire."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=30 -> potential = 0 - 1 + 30 = 29 < 100 -> no fire
    await inject_weight(dut, 1, 30)
    await pulse_step(dut)

    assert get_spikes(dut) == 0, "No dendrite should fire with weight=30"


@cocotb.test()
async def test_dendrite_accumulation(dut):
    """Membrane potential persists across rounds (leaky integration)."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Round 1: inject 60 -> potential = 0 - 1 + 60 = 59, no fire
    await inject_weight(dut, 0, 60)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: should not fire (potential~59)"
    await pulse_clear(dut)

    # Round 2: inject 50 -> potential = 59 - 1 + 50 = 108 >= 100 -> fire
    await inject_weight(dut, 0, 50)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 2: dendrite 0 should fire (potential~108)"


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

    # Round 1: inject 99 -> potential = 0 - 1 + 99 = 98
    await inject_weight(dut, 0, 99)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: no fire (potential=98)"
    await pulse_clear(dut)

    # Round 2: no inject -> potential = 98 - 1 = 97
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 2: no fire (potential=97)"
    await pulse_clear(dut)

    # Round 3: inject 2 -> potential = 97 - 1 + 2 = 98, no fire
    await inject_weight(dut, 0, 2)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 3: no fire (potential=98, leak confirmed)"
    await pulse_clear(dut)

    # Round 4: inject 3 -> potential = 98 - 1 + 3 = 100 >= 100 -> fire
    await inject_weight(dut, 0, 3)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 4: dendrite 0 should fire (potential=100)"


@cocotb.test()
async def test_dendrite_inhibition(dut):
    """Negative (inhibitory) weight reduces potential."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Round 1: inject +80 -> potential = 79
    await inject_weight(dut, 0, 80)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

    # Round 2: inject -30 -> potential = 79 - 1 + (-30) = 48
    await inject_weight(dut, 0, -30)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

    # Round 3: inject +60 -> potential = 48 - 1 + 60 = 107 >= 100 -> fire
    await inject_weight(dut, 0, 60)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Should fire: excitation overcomes prior inhibition"


# ==============================================================================
#  Phase 2: Event -> Axon -> FSM flow tests
# ==============================================================================

@cocotb.test()
async def test_event_fsm_basic(dut):
    """Event triggers FSM: top_busy goes high then returns to idle."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    assert get_top_busy(dut) == 0, "FSM should start idle"

    await trigger_event(dut)
    await ClockCycles(dut.clk, 1)
    assert get_top_busy(dut) == 1, "FSM should be busy after event"

    await wait_fsm_idle(dut)
    assert get_top_busy(dut) == 0, "FSM should return to idle"


@cocotb.test()
async def test_no_event_stays_idle(dut):
    """Without event_in, FSM stays idle."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await ClockCycles(dut.clk, 10)
    assert get_top_busy(dut) == 0, "FSM should stay idle without event"


# ==============================================================================
#  Phase 3: SRAM programming + synapse read tests
# ==============================================================================

@cocotb.test()
async def test_sram_program_and_synapse_read(dut):
    """Program a valid synapse entry at addr 0, trigger event, verify parsed fields."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # valid=1, learn=1, dendrite=2, weight=42
    entry = build_synapse_word(valid=1, learn_en=1, dendrite_id=2, weight=42)
    await sram_program_word(dut, addr=0, word=entry)

    # Trigger event -> axon produces range -> FSM reads synapse at addr 0
    await trigger_event(dut)
    await wait_fsm_idle(dut)

    # External pin: syn_entry_valid should be 1
    assert get_syn_entry_valid(dut) == 1, \
        "syn_entry_valid (uo_out[6]) should be 1 for valid entry"

    # Hierarchical access to check parsed fields
    syn = dut.user_project.u_synapse
    assert syn.entry_valid.value == 1
    assert syn.entry_learn_en.value == 1
    assert syn.entry_dendrite.value == 2
    assert syn.entry_weight.value == 42


@cocotb.test()
async def test_synapse_read_invalid_entry(dut):
    """Program an invalid synapse entry (valid=0), verify syn_entry_valid=0."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    entry = build_synapse_word(valid=0, learn_en=0, dendrite_id=1, weight=10)
    await sram_program_word(dut, addr=0, word=entry)

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_syn_entry_valid(dut) == 0, \
        "syn_entry_valid should be 0 for invalid entry"


@cocotb.test()
async def test_synapse_read_negative_weight(dut):
    """Verify correct parsing of negative (inhibitory) weight."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight = -10 -> 0xF6 unsigned
    entry = build_synapse_word(valid=1, learn_en=0, dendrite_id=0, weight=-10)
    await sram_program_word(dut, addr=0, word=entry)

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_syn_entry_valid(dut) == 1
    raw_w = dut.user_project.u_synapse.entry_weight.value.integer
    assert raw_w == 0xF6, \
        f"entry_weight should be 0xF6 (-10 signed), got {raw_w:#04x}"
