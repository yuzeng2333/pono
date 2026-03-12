# Task: Integrate LLM-Generated Predicates into Pono IC3IA

## Background

We have a project ([LLM_Predicate_Embedding_IC3](file:///local/home/yzarg/LLM_Predicate_Embedding_IC3)) that uses an LLM to generate predicates for hardware model checking. These predicates express word-level relationships between design registers (e.g., `copy1.product == copy2.product`, `copy1.state == copy2.state`, `copy1.product == 0`).

Previously, we integrated these predicates into **rIC3** (a bit-level IC3 engine) by:
1. Embedding predicates as constrained latches in the BTOR2 file
2. Implementing a "predicate lift" mechanism that post-hoc replaces bit-level IC3 lemmas with predicate-level lemmas

This worked but had a fundamental limitation: rIC3 is bit-level, so every SAT query still operates on the full bitblasted CNF transition relation. Predicates reduced lemma count (803→29 at W=64) but per-query cost still scales with datapath width.

**Pono's IC3IA engine solves this problem natively.** IC3IA (IC3 via Implicit Predicate Abstraction) already operates at the predicate level using SMT, making per-query cost O(#predicates) instead of O(width). We want to seed IC3IA with LLM-generated predicates to reduce/eliminate CEGAR refinement iterations.

## Goal

Add the ability to provide **initial predicates** to Pono's IC3IA engine from an external file (JSON), so that LLM-generated predicates can be injected before the CEGAR loop begins.

## Existing IC3IA Architecture (Key Files)

- **`engines/ic3ia.h`** / **`engines/ic3ia.cpp`**: IC3IA engine implementation
  - `IC3IA::initialize()` — extracts initial predicates from init/property, calls `add_predicate()`
  - `IC3IA::add_predicate(const Term & pred)` — adds a predicate to `predset_` and the implicit abstractor
  - `IC3IA::refine()` — CEGAR refinement: uses interpolation to discover new predicates
  - `predset_` — `UnorderedTermSet` of current predicates
  
- **`modifiers/implicit_predicate_abstractor.h`** / `.cpp`: Manages the abstract transition relation
  - `ia_.do_abstraction()` — computes abstract model from current predicates
  - `ia_.predicates()` — returns current predicate vector

- **`options/options.h`**: Pono command-line options
- **`frontends/`**: BTOR2 frontend that parses the input model
- **`pono.cpp`**: Main entry point

## What to Implement

### Step 1: Add a command-line option for predicate file

In `options/options.h` and the option parsing code, add:
```
--ic3ia-predicates <path-to-json>    Initial predicates for IC3IA from external file
```

### Step 2: Define the JSON predicate format

The predicate JSON file contains an array of predicate objects. Each predicate has:
```json
[
  {
    "predicate_type": "eq",
    "operands": {
      "op1": "copy1.product",
      "op2": "copy2.product"
    }
  },
  {
    "predicate_type": "eq",
    "operands": {
      "op1": "copy1.product",
      "op2": "0"
    }
  }
]
```

Where:
- `predicate_type`: `"eq"` (equality), potentially `"neq"`, `"ult"`, `"uge"` in the future
- `operands.op1`: Name of a state variable in the BTOR2/SMT model (must match the symbol name in the transition system)
- `operands.op2`: Either another state variable name OR a constant string like `"0"`

### Step 3: Parse predicates and construct SMT terms

Create a utility function (e.g., in a new file `utils/predicate_reader.h/.cpp`) that:

1. Reads the JSON file
2. For each predicate, looks up the operand names in the transition system's state variables
3. Constructs the SMT term:
   - `"eq"` → `solver->make_term(Equal, op1_term, op2_term)`
   - For constant `"0"` → `solver->make_term(0, sort)` where sort matches op1's sort
4. Returns a `TermVec` of predicate terms

Pseudocode:
```cpp
TermVec read_predicates(const std::string & json_path,
                        const TransitionSystem & ts,
                        const SmtSolver & solver) {
    TermVec predicates;
    // Parse JSON
    // For each predicate:
    //   Look up op1 in ts.named_terms() or ts.state_vars()
    //   Construct op2 (variable lookup or constant creation)
    //   Create SMT term based on predicate_type
    //   predicates.push_back(term)
    return predicates;
}
```

### Step 4: Inject predicates into IC3IA initialization

In `IC3IA::initialize()`, after the existing predicate extraction from init/property, add:

```cpp
// After existing predicate extraction...

// Add LLM-generated predicates from external file
if (!options_.ic3ia_predicate_file_.empty()) {
    TermVec llm_preds = read_predicates(
        options_.ic3ia_predicate_file_, conc_ts_, solver_);
    for (const auto & p : llm_preds) {
        add_predicate(p);
    }
    logger.log(1, "Added {} LLM predicates from {}", 
               llm_preds.size(), options_.ic3ia_predicate_file_);
}
```

## Test Case: Shift-Add Multiplier Miter

The benchmark is at: `/local/home/yzarg/LLM_Predicate_Embedding_IC3/benchmark/verilog/`

To generate a BTOR2 file from Verilog (without predicate latches — those are NOT needed for Pono):
```bash
yosys -p "read_verilog shift_add_multiplier_miter.sv; prep -top top; write_btor2 mult_miter.btor2"
```

The predicates JSON is at: `/local/home/yzarg/LLM_Predicate_Embedding_IC3/output/shift_add_multiplier_miter/predicates_singlecopy.json`

**Note**: For Pono, you do NOT need the `btor2_info` or `predicate_id` fields in the JSON. Only `predicate_type` and `operands` are needed. Pono will look up variables by name from the transition system.

Expected usage:
```bash
pono -e ic3ia --ic3ia-predicates predicates.json mult_miter.btor2
```

## Expected Results

### Without LLM predicates
IC3IA starts with predicates from init/property only. It must discover additional predicates through expensive CEGAR refinement iterations (interpolation). At larger widths (W=32, W=64), this may timeout.

### With LLM predicates
IC3IA starts with near-complete set of predicates. Expected:
- **0 or minimal refinement iterations** (the LLM predicates should be sufficient for proof)
- **Width-independent performance** — the abstract model has ~7 predicate variables regardless of W
- The 7 predicates that worked well:
  1. `copy1.state == copy2.state`
  2. `copy1.count == copy2.count`
  3. `copy1.a_reg == copy2.a_reg`
  4. `copy1.b_reg == copy2.b_reg`
  5. `copy1.product == copy2.product`
  6. `copy1.product == 0`
  7. `copy2.product == 0`

## Key Differences from rIC3 Integration

| Aspect | rIC3 (what we did) | Pono IC3IA (what to do) |
|--------|-------------------|------------------------|
| BTOR2 modification | Required (add latch nodes) | **Not needed** |
| Predicate matching/lift | Required (custom Rust code) | **Not needed** (IC3IA does this natively) |
| Abstract transition | Not computed (bit-level SAT) | **Computed automatically** by `ImplicitPredicateAbstractor` |
| Integration point | Modified IC3 generalization | **Just call `add_predicate()`** |
| SMT vs SAT | SAT (bitblasted) | **SMT (word-level)** |

## Dependencies

- JSON parsing: Pono already uses nlohmann/json or you can add it. Alternatively, use a simple custom parser for this small format.
- smt-switch: Pono's SMT abstraction layer (already integrated)

## Validation

1. Run IC3IA on `mult_miter.btor2` without predicates, note number of refinement iterations and time
2. Run IC3IA on `mult_miter.btor2` with predicates, verify fewer/zero refinements
3. Test at W=8, W=32, W=64 to verify width-independent behavior
4. Compare with rIC3 results (rIC3 at W=64 with 7 predicates: 29 lemmas, 11.3s)