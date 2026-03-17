# LLM-Guided Predicate Generation for IC3 Model Checking

This project integrates **LLM-generated predicates** into the [IC3IA](https://link.springer.com/chapter/10.1007/978-3-030-81688-9_22) (IC3 via Implicit Predicate Abstraction) engine to accelerate formal hardware verification. By seeding IC3IA with predicates inferred by a large language model, the costly CEGAR (Counter-Example Guided Abstraction Refinement) loop is reduced or eliminated entirely, enabling **width-independent, scalable model checking**.

Built on top of [Pono](https://github.com/stanford-centaur/pono), a flexible SMT-based model checker from Stanford.

## Key Idea

IC3IA is a powerful model checking algorithm that operates at the **predicate abstraction** level using SMT solvers. Instead of reasoning over individual bits (as in bit-level IC3), it reasons over high-level predicates like `register_a == register_b` or `counter < threshold`. However, IC3IA traditionally discovers these predicates through an expensive iterative CEGAR loop using Craig interpolation.

**Our insight:** An LLM can analyze the design's RTL/structure and generate semantically meaningful predicates *before* verification begins. When these predicates are injected into IC3IA:

- **CEGAR refinement iterations drop to 0** — the LLM-provided predicates are sufficient for proof
- **Per-query cost becomes O(#predicates)** instead of O(datapath width)
- **Verification scales independently of bit-width** (e.g., 8-bit and 64-bit designs verify in similar time)

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  RTL Design  │────▶│  LLM Predicate   │────▶│  Pono IC3IA     │
│  (Verilog/   │     │  Generator       │     │  with seeded    │
│   BTOR2)     │     │                  │     │  predicates     │
└─────────────┘     └──────────────────┘     └────────┬────────┘
                      Outputs JSON:                    │
                      - eq, ult, ule                   ▼
                      - state variable               SAFE / UNSAFE
                        relationships                (0 refinements)
```

## What Was Modified

This fork extends Pono with the following changes:

### Core Engine Changes
- **`options/options.h`** — Added `--ic3ia-predicates <path>` CLI option to provide a JSON file of initial predicates
- **`options/options.cpp`** — Option parsing for the predicate file path and related flags
- **`engines/ic3ia.cpp`** — Modified `IC3IA::initialize()` to load and inject external predicates via `add_predicate()`
- **`engines/ic3base.h/cpp`** — Added incremental frame dumping for analysis (blocking clause extraction)
- **`pono.cpp`** — Wired up the new options in the main entry point

### Analysis Infrastructure
- **`scripts/`** — Python scripts for batch analysis, predicate pattern mining, waveform analysis, and temporal pattern extraction
- **`predicates/`** — Example LLM-generated predicate files in JSON format for various benchmarks
- **`results/`** — Aggregate analysis results from running IC3IA with and without LLM predicates

## Predicate JSON Format

Predicates are specified as a JSON array. Each predicate defines a relation between state variables (or a state variable and a constant):

```json
[
  {
    "predicate_type": "eq",
    "operands": { "op1": "copy1.product", "op2": "copy2.product" }
  },
  {
    "predicate_type": "eq",
    "operands": { "op1": "copy1.product", "op2": "0" }
  },
  {
    "predicate_type": "ult",
    "operands": { "op1": "counter", "op2": "threshold" }
  }
]
```

Supported predicate types: `eq` (equality), `ult` (unsigned less-than), `ule` (unsigned less-or-equal).

## Usage

```bash
# Standard IC3IA (no LLM predicates — relies on CEGAR refinement)
pono -e ic3ia model.btor2

# IC3IA seeded with LLM-generated predicates
pono -e ic3ia --ic3ia-predicates predicates.json model.btor2

# Skip init-derived predicates, use only LLM predicates
pono -e ic3ia --ic3ia-predicates predicates.json --ic3ia-skip-init-predicates model.btor2

# With blocking clause dump for analysis
pono -e ic3ia --ic3ia-predicates predicates.json --dump-blocking-clauses output.json model.btor2
```

## Example: Shift-Add Multiplier Miter

A representative benchmark is a miter circuit comparing two multiplier implementations. The LLM generates 7 predicates capturing the key register equivalences:

| # | Predicate |
|---|-----------|
| 1 | `copy1.state == copy2.state` |
| 2 | `copy1.count == copy2.count` |
| 3 | `copy1.a_reg == copy2.a_reg` |
| 4 | `copy1.b_reg == copy2.b_reg` |
| 5 | `copy1.product == copy2.product` |
| 6 | `copy1.product == 0` |
| 7 | `copy2.product == 0` |

### Results

| Configuration | Refinement Iterations | Scales with Width? |
|---|---|---|
| IC3IA (no predicates) | Many; may timeout at W≥32 | Yes (expensive) |
| IC3IA + 7 LLM predicates | **0** | **No** (width-independent) |
| rIC3 bit-level + predicates (prior work) | N/A (bit-level) | Still O(width) per query |

## Predicate Pattern Analysis

Analysis of blocking clauses across benchmarks reveals that IC3IA lemmas are dominated by **equality predicates** (96.5%), confirming that LLM-generated equality relationships are well-aligned with what the proof engine needs:

- **141 predicate atoms** across 9 analyzed benchmarks
- **96.5% equality**, 3.5% comparison predicates
- Average clause size: **3.43 atoms**
- Predicates classified by simulation behavior: 18.4% always-true (invariants), 58.9% phased (state-dependent), 16.3% periodic

## Comparison with Bit-Level Approach

| Aspect | rIC3 (bit-level) | Pono IC3IA + LLM (this work) |
|--------|-------------------|-------------------------------|
| Abstraction level | SAT (bitblasted) | **SMT (word-level)** |
| BTOR2 modification needed | Yes (embed as latches) | **No** |
| Custom predicate lifting | Yes (Rust code) | **No** (native to IC3IA) |
| Per-query cost | O(datapath width) | **O(#predicates)** |
| Integration complexity | High | **Low** (just `add_predicate()`) |

## Building

This project is built on top of [Pono](https://github.com/stanford-centaur/pono). Follow the standard Pono build instructions:

```bash
./contrib/setup-smt-switch.sh
./contrib/setup-btor2tools.sh
./configure.sh
cd build && make
```

See the [Pono README](https://github.com/stanford-centaur/pono#setup) for detailed setup instructions, dependencies, and optional configurations.

## Project Structure

```
engines/
  ic3ia.cpp/h        # IC3IA engine — modified to accept external predicates
  ic3base.cpp/h      # Base IC3 engine — added incremental dump support
options/
  options.cpp/h      # CLI option parsing — added --ic3ia-predicates
predicates/           # Example LLM-generated predicate JSON files
scripts/              # Analysis and batch-run scripts
results/              # Aggregate analysis results
pono.cpp              # Main entry point
```

## Credits

- **Pono Model Checker**: [stanford-centaur/pono](https://github.com/stanford-centaur/pono) — Makai Mann, Ahmed Irfan, Florian Lonsing, et al. (CAV 2021)
- **Smt-Switch**: [stanford-centaur/smt-switch](https://github.com/stanford-centaur/smt-switch) — Solver-agnostic C++ SMT API (SAT 2021)

## License

BSD 3-Clause License (same as Pono). See [LICENSE](LICENSE).