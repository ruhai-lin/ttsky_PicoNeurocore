# SPDX-FileCopyrightText: (c) 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

"""
PicoNeurocore -- cocotb test suite

Phase 1: Verify dendrite (LIF) behaviour via the direct test interface.
Phase 2: Verify event -> axon -> top-level FSM flow.
Phase 3: Verify SRAM programming and synapse entry read/parse.
Phase 4: Verify full event processing: synapse -> dendrite integration.

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

async def wait_fsm_idle(dut, max_cycles=500):
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

async def program_synapse_table(dut, entries):
    """Program a list of synapse entries into SRAM starting at address 0.

    entries: list of dicts with keys valid, learn_en, dendrite_id, weight.
    Remaining SRAM words (up to 16) are filled with zeros (invalid).
    """
    for i in range(16):
        if i < len(entries):
            e = entries[i]
            word = build_synapse_word(**e)
        else:
            word = 0x00000000
        await sram_program_word(dut, addr=i, word=word)

# ==============================================================================
#  Phase 1: Dendrite tests
# ==============================================================================

@cocotb.test()
async def test_dendrite_fire(dut):
    """Inject enough weight to exceed threshold, verify fire and reset."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

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

    await inject_weight(dut, 1, 30)
    await pulse_step(dut)

    assert get_spikes(dut) == 0, "No dendrite should fire with weight=30"


@cocotb.test()
async def test_dendrite_accumulation(dut):
    """Membrane potential persists across rounds (leaky integration)."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await inject_weight(dut, 0, 60)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: should not fire (potential~59)"
    await pulse_clear(dut)

    await inject_weight(dut, 0, 50)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 2: dendrite 0 should fire (potential~108)"


@cocotb.test()
async def test_dendrite_independence(dut):
    """Different dendrites maintain independent state."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

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

    await inject_weight(dut, 0, 99)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 1: no fire (potential=98)"
    await pulse_clear(dut)

    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 2: no fire (potential=97)"
    await pulse_clear(dut)

    await inject_weight(dut, 0, 2)
    await pulse_step(dut)
    assert get_spikes(dut) == 0, "Round 3: no fire (potential=98, leak confirmed)"
    await pulse_clear(dut)

    await inject_weight(dut, 0, 3)
    await pulse_step(dut)
    assert get_spikes(dut) & 1, "Round 4: dendrite 0 should fire (potential=100)"


@cocotb.test()
async def test_dendrite_inhibition(dut):
    """Negative (inhibitory) weight reduces potential."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await inject_weight(dut, 0, 80)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

    await inject_weight(dut, 0, -30)
    await pulse_step(dut)
    assert get_spikes(dut) == 0
    await pulse_clear(dut)

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
    """Program a valid synapse entry, trigger event, verify parsed fields.

    The entry is placed at addr 15 (last read by the FSM) so it remains
    in the synapse captured register after the full scan completes.
    """
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    entries = [dict(valid=0, learn_en=0, dendrite_id=0, weight=0)] * 15
    entries.append(dict(valid=1, learn_en=1, dendrite_id=2, weight=42))
    await program_synapse_table(dut, entries)

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_syn_entry_valid(dut) == 1, \
        "syn_entry_valid (uo_out[6]) should be 1 for valid entry"

    try:
        syn = dut.user_project.u_synapse
        assert syn.entry_valid.value == 1
        assert syn.entry_learn_en.value == 1
        assert syn.entry_dendrite.value == 2
        assert syn.entry_weight.value == 42
    except AttributeError:
        # In GL test, internal signals may not be accessible
        # Verify through behavior: weight=42 to dendrite 2 should not fire (threshold=100)
        spikes = get_spikes(dut)
        assert not (spikes & 0x4), "Dendrite 2 should not fire with weight=42"
        # Also verify no other dendrites fired
        assert spikes == 0, f"Only valid entry was to dendrite 2 with weight=42, no spikes expected, got {spikes:#06b}"



@cocotb.test()
async def test_synapse_read_invalid_entry(dut):
    """Program an invalid synapse entry (valid=0), verify syn_entry_valid=0."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    entries = [dict(valid=0, learn_en=0, dendrite_id=0, weight=0)] * 15
    entries.append(dict(valid=0, learn_en=0, dendrite_id=1, weight=10))
    await program_synapse_table(dut, entries)

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

    entries = [dict(valid=0, learn_en=0, dendrite_id=0, weight=0)] * 15
    entries.append(dict(valid=1, learn_en=0, dendrite_id=0, weight=-10))
    await program_synapse_table(dut, entries)

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_syn_entry_valid(dut) == 1
    try:
        raw_w = dut.user_project.u_synapse.entry_weight.value.integer
        assert raw_w == 0xF6, \
            f"entry_weight should be 0xF6 (-10 signed), got {raw_w:#04x}"
    except AttributeError:
        # In GL test, try SRAM access as fallback
        try:
            w = sram_weight_at(dut, 15)
            assert w == 0xF6, \
                f"SRAM weight should be 0xF6 (-10 signed), got {w:#04x}"
        except AttributeError:
            # If SRAM also not accessible, verify through behavior:
            # Weight=-10 is inhibitory, should not cause firing
            spikes = get_spikes(dut)
            assert not (spikes & 0x1), \
                "Dendrite 0 should not fire with inhibitory weight=-10"


# ==============================================================================
#  Phase 4: Full event processing -- synapse -> dendrite integration
# ==============================================================================

@cocotb.test()
async def test_full_event_single_synapse_fires(dut):
    """One valid synapse with large weight causes its target dendrite to fire."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Synapse 0 -> dendrite 0, weight=120 (above threshold of 100)
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=120),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    spikes = get_spikes(dut)
    assert spikes & 0x1, f"Dendrite 0 should fire (weight=120), got spikes={spikes:#06b}"
    assert not (spikes & 0xE), f"Only dendrite 0 should fire, got spikes={spikes:#06b}"


@cocotb.test()
async def test_full_event_below_threshold(dut):
    """One valid synapse with small weight: dendrite should not fire."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Synapse 0 -> dendrite 1, weight=30 (below threshold)
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=1, weight=30),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) == 0, "No dendrite should fire with weight=30"


@cocotb.test()
async def test_full_event_multiple_dendrites(dut):
    """Synapses targeting different dendrites: some fire, some don't."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=120),  # fires
        dict(valid=1, learn_en=0, dendrite_id=1, weight=30),   # no fire
        dict(valid=1, learn_en=1, dendrite_id=2, weight=110),  # fires
        dict(valid=1, learn_en=0, dendrite_id=3, weight=50),   # no fire
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    spikes = get_spikes(dut)
    assert spikes & 0x1, "Dendrite 0 should fire (w=120)"
    assert not (spikes & 0x2), "Dendrite 1 should NOT fire (w=30)"
    assert spikes & 0x4, "Dendrite 2 should fire (w=110)"
    assert not (spikes & 0x8), "Dendrite 3 should NOT fire (w=50)"


@cocotb.test()
async def test_full_event_accumulation(dut):
    """Multiple synapses to the same dendrite: weights accumulate."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Two synapses both targeting dendrite 0:
    #   weight 60 + weight 50 = 110 -> potential = 0 - 1(leak) + 110 = 109 >= 100 -> fire
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=0, weight=60),
        dict(valid=1, learn_en=0, dendrite_id=0, weight=50),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    spikes = get_spikes(dut)
    assert spikes & 0x1, \
        f"Dendrite 0 should fire (accumulated weight=110), got spikes={spikes:#06b}"


@cocotb.test()
async def test_full_event_invalid_skipped(dut):
    """Invalid synapse entries are skipped and don't affect dendrites."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Entry 0: valid, dendrite 0, weight=120 (fires)
    # Entry 1: INVALID, dendrite 1, weight=120 (should be skipped)
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=0, weight=120),
        dict(valid=0, learn_en=0, dendrite_id=1, weight=120),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    spikes = get_spikes(dut)
    assert spikes & 0x1, "Dendrite 0 should fire (valid entry)"
    assert not (spikes & 0x2), "Dendrite 1 should NOT fire (invalid entry skipped)"


@cocotb.test()
async def test_full_event_inhibitory_prevents_fire(dut):
    """Excitatory + inhibitory synapses: net effect below threshold."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Dendrite 0: +80 + (-30) = 50 -> potential = 0 - 1 + 50 = 49 < 100 -> no fire
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=0, weight=80),
        dict(valid=1, learn_en=0, dendrite_id=0, weight=-30),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) == 0, \
        "Dendrite 0 should NOT fire (net weight=50, potential=49)"


@cocotb.test()
async def test_full_event_potential_persists(dut):
    """Membrane potential persists across events (leaky integration)."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # Dendrite 0: weight=60 -> potential = 0 - 1 + 60 = 59 (no fire)
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=0, weight=60),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)
    assert not (get_spikes(dut) & 0x1), "Event 1: no fire (potential~59)"

    # Event 2: same synapse table, potential = 59 - 1 + 60 = 118 >= 100 -> fire
    await trigger_event(dut)
    await wait_fsm_idle(dut)
    assert get_spikes(dut) & 0x1, "Event 2: dendrite 0 should fire (potential~118)"


# ==============================================================================
#  Phase 5: Learning -- weight update and SRAM write-back
# ==============================================================================

def sram_weight_at(dut, addr):
    """Read the weight field [23:16] from SRAM internal memory at addr."""
    word = dut.user_project.SRAM.mem[addr].value.integer
    return (word >> 16) & 0xFF


@cocotb.test()
async def test_learning_potentiation(dut):
    """Active synapse + dendrite fires + learn_en -> weight increases by 1."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=120 -> potential = 0 - 1 + 120 = 119 >= 100 -> fires
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=120),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) & 0x1, "Dendrite 0 should fire"
    w = sram_weight_at(dut, 0)
    assert w == 121, f"Weight should be 121 (120+1), got {w}"


@cocotb.test()
async def test_learning_depression(dut):
    """Active synapse + dendrite does NOT fire + learn_en -> weight decreases by 1."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=30 -> potential = 0 - 1 + 30 = 29 < 100 -> no fire
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=30),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) == 0, "Dendrite 0 should NOT fire"
    w = sram_weight_at(dut, 0)
    assert w == 29, f"Weight should be 29 (30-1), got {w}"


@cocotb.test()
async def test_learning_disabled(dut):
    """learn_en=0 -> weight unchanged even if dendrite fires."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    await program_synapse_table(dut, [
        dict(valid=1, learn_en=0, dendrite_id=0, weight=120),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) & 0x1, "Dendrite 0 should fire"
    w = sram_weight_at(dut, 0)
    assert w == 120, f"Weight should remain 120 (learn disabled), got {w}"


@cocotb.test()
async def test_learning_clamp_max(dut):
    """Weight at +127, dendrite fires, potentiation -> stays at 127."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=127 -> potential = 0 - 1 + 127 = 126 >= 100 -> fires
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=127),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) & 0x1, "Dendrite 0 should fire"
    w = sram_weight_at(dut, 0)
    assert w == 127, f"Weight should stay at 127 (clamped), got {w}"


@cocotb.test()
async def test_learning_clamp_min(dut):
    """Weight at -128 (0x80), dendrite no fire, depression -> stays at -128."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=-128 -> potential = max(0, 0-1+(-128)) = 0 -> no fire
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=-128),
    ])

    await trigger_event(dut)
    await wait_fsm_idle(dut)

    assert get_spikes(dut) == 0, "Dendrite 0 should NOT fire"
    w = sram_weight_at(dut, 0)
    assert w == 0x80, f"Weight should stay at 0x80 (-128 clamped), got {w:#04x}"


@cocotb.test()
async def test_learning_multi_event_drift(dut):
    """Repeated events gradually increase weight via potentiation."""
    clock = Clock(dut.clk, 20, units="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)

    # weight=110 -> fires each event -> potentiation each time
    await program_synapse_table(dut, [
        dict(valid=1, learn_en=1, dendrite_id=0, weight=110),
    ])

    for i in range(3):
        await trigger_event(dut)
        await wait_fsm_idle(dut)

    w = sram_weight_at(dut, 0)
    assert w == 113, f"After 3 events weight should be 113 (110+3), got {w}"
