![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# PicoNeurocore

A minimal Loihi-style neurocore prototype that implements event-driven spiking neural network processing. This project builds a complete neurocore on a single 64-byte OpenRAM SRAM macro (16 × 32-bit words = 16 synapses), featuring 4 Leaky Integrate-and-Fire (LIF) dendrites, synapse SRAM storage, and Hebbian learning mechanisms.

## 🧠 Core Features

- **Event-Driven Processing**: Input spike events trigger complete neurocore processing cycles
- **4 Independent LIF Neurons**: Each dendrite implements full leaky integrate-and-fire dynamics
- **16 Programmable Synapses**: Stored in SRAM with configurable weight, target dendrite, and learning enable
- **Online Hebbian Learning**: Automatic weight updates based on synaptic activity and postsynaptic firing
- **Dual-Mode Operation**: Run mode (event processing) and program mode (SRAM configuration)

## 📊 System Architecture Visualization

<p align="center">
  <img src="docs/neurocore.gif" alt="PicoNeurocore Data Flow" width="600">
</p>

<p align="center">
  <em>The animation above conceptually demonstrates how input spikes flow through the neurocore and produce outputs</em>
</p>

## 🔧 Module Architecture

PicoNeurocore consists of four core modules coordinated by a top-level finite state machine:

### 1. **Axon Module** (`axon.v`)

**Function**: Event-to-synapse-range mapper

The Axon module receives input spike events and determines which segment of the synapse table should be scanned. In this minimal version, it produces a fixed range covering the entire SRAM (start=0, count=16).

**Key Features**:
- Receives input event pulses via `event_valid`
- Outputs synapse range (`syn_start`, `syn_count`) using valid/ack handshake protocol
- Ensures sequential event processing by ignoring new events while a previous range is outstanding
- `event_id` input reserved for future multi-axon expansion

**Interface**:
- Inputs: `event_valid`, `event_id[7:0]` (reserved)
- Outputs: `range_valid`, `syn_start[3:0]`, `syn_count[4:0]`
- Handshake: `range_ack` (provided by top-level FSM)

### 2. **Synapse Module** (`synapse.v`)

**Function**: SRAM interface and synapse-table entry parser

The Synapse module manages SRAM read/write operations and parses 32-bit synapse entries. It handles the timing requirements of the single-port OpenRAM macro (3-cycle read/write latency).

**Synapse Entry Format** (32-bit SRAM word):
- `[31]`: `valid` - entry is active
- `[30]`: `learn_en` - learning is enabled for this synapse
- `[29:28]`: `dendrite_id` - target dendrite (0-3)
- `[27:24]`: reserved
- `[23:16]`: `weight` - signed 8-bit synaptic weight
- `[15:0]`: reserved (for future expansion: tag, delay, etc.)

**Interface**:
- Read operation: `read_start`, `read_addr[3:0]` → `read_done`, `entry_*` fields
- Write operation: `write_start`, `write_addr[3:0]`, `write_data[31:0]` → `write_done`
- SRAM interface: `sram_*` signals directly connected to OpenRAM macro via top-level mux

### 3. **Dendrite Module** (`dendrite.v`)

**Function**: Leaky Integrate-and-Fire (LIF) neuron compartment

Each dendrite (four in total) implements a complete LIF neuron with the following dynamics:

1. **Integration Phase**: Accumulates synaptic weights into `input_acc` when `syn_valid` is asserted
2. **Leak Phase**: Membrane potential decays by `V_LEAK` each time step
3. **Fire Phase**: When membrane potential exceeds threshold (`V_THRESH = 100`), neuron fires and resets to `V_RESET = 0`

**Fixed Parameters**:
- Threshold: `V_THRESH = 100`
- Reset value: `V_RESET = 0`
- Leak value: `V_LEAK = 1`

**Interface**:
- Inputs: `syn_valid`, `syn_weight[7:0]` (signed)
- Control: `step` (triggers LIF dynamics), `clear` (resets accumulator)
- Outputs: `spike_out` (fire flag), `membrane[15:0]` (current membrane potential)

### 4. **Learning Module** (`learning.v`)

**Function**: Minimal Hebbian-like weight update rule

The Learning module implements a simple Hebbian plasticity rule:

- **Potentiation**: If synapse was active AND target dendrite fired AND learning enabled → `weight += 1`
- **Depression**: If synapse was active AND target dendrite did NOT fire AND learning enabled → `weight -= 1`
- **No Change**: Otherwise

Weights are clamped to the signed 8-bit range `[-128, +127]`. The module is purely combinational; the top-level FSM latches its outputs and drives the synapse write-back path.

**Interface**:
- Inputs: `learn_en`, `syn_active`, `post_spike`, `current_weight[7:0]`
- Outputs: `new_weight[7:0]`, `weight_changed`

## 🔄 Top-Level Module Interconnection

The top-level module `tt_um_piconeurocore_top` (`project.v`) coordinates all four modules through a finite state machine (FSM).

### Data Flow Path

```
Input Event → Axon (range lookup) → Synapse (SRAM read/parse)
  → Dendrite (LIF integrate/fire) → Learning (weight update)
  → Synapse (SRAM write-back) → Output Spikes
```

### FSM State Flow

Each event triggers the following processing cycle:

1. **IDLE** → **AXON_WAIT**: Wait for Axon module to determine synapse range
2. **CLEAR**: Reset all dendrite accumulators
3. **SYN_READ → SYN_WAIT → SYN_NEXT** (loop N times):
   - Scan synapse range
   - Read each synapse entry
   - Send valid synapse weights to corresponding dendrites
4. **STEP**: Trigger LIF dynamics for all dendrites (integrate + leak + threshold check)
5. **LEARN_READ → LEARN_WAIT → LEARN_WRITE? → LEARN_NEXT** (loop N times):
   - Re-scan synapse range
   - Apply learning rule to each active synapse
   - Write back to SRAM if weight changed
6. **DONE** → **IDLE**: Complete processing cycle

### Module Interconnection Details

- **Axon → Synapse**: Provides `syn_start` and `syn_count` to specify scan range
- **Synapse → Dendrite**: Routes `entry_dendrite` to one of 4 dendrites, `entry_weight` as input
- **Dendrite → Learning**: `spike_out` signals indicate whether target dendrite fired
- **Learning → Synapse**: Updated weights written back to SRAM via `learn_updated_word`
- **SRAM Multiplexing**: SRAM access multiplexed between run mode and program mode


## 📝 Usage

### Run Mode (mode = 0)

- `ui_in[5]`: `event_in` - Trigger full neurocore processing cycle
- `ui_in[4:3]`: `dendrite_sel` - Direct dendrite test: select target (0-3)
- `ui_in[2]`: `clear` - Direct dendrite test: reset accumulators
- `ui_in[1]`: `step` - Direct dendrite test: LIF dynamics
- `ui_in[0]`: `inject_valid` - Direct dendrite test: send weight
- `uio_in[7:0]`: `syn_weight` - 8-bit weight / data byte
- `uo_out[3:0]`: `spike_out` - Dendrite fire flags
- `uo_out[4]`: `top_busy` - FSM processing flag
- `uo_out[6]`: `syn_entry_valid` - Last-read synapse valid bit

### Program Mode (mode = 1)

- `ui_in[3:0]`: `addr` - SRAM word address (0-15)
- `ui_in[4]`: `byte_load` - Load `uio_in` byte into 32-bit accumulator
- `ui_in[5]`: `word_write` - Write accumulated word to SRAM[addr]
- `uio_in[7:0]`: `data_byte` - Byte to accumulate

## 🧪 Testing

See [test/README.md](test/README.md) for detailed testing instructions.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital and analog designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Reference

- Loihi: [Loihi: A Neuromorphic Manycore Processor with On-Chip Learning](https://ieeexplore.ieee.org/document/8259423)

- OpenRAM: https://github.com/VLSIDA/OpenRAM

- OpenRAM ttsky-testchip: https://github.com/VLSIDA/tt25a_openram_testchip