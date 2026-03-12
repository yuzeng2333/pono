#!/usr/bin/env python3
"""
Temporal pattern analysis of IC3 blocking clause predicates on simulation waveforms.

Usage:
  # Step 1: Run IC3 and dump blocking clauses
  ./build/pono -e ic3ia -k 500 --dump-blocking-clauses clauses.json input.btor2

  # Step 2: Get a simulation trace (BMC witness as VCD, then convert, or use JSON)
  ./build/pono -e bmc -k 50 --vcd trace.vcd --witness input.btor2

  # Step 3: Analyze
  python3 scripts/analyze_temporal_patterns.py clauses.json --vcd trace.vcd
  python3 scripts/analyze_temporal_patterns.py clauses.json --trace trace.json
"""

import json
import argparse
import sys
import re
from collections import defaultdict
from itertools import combinations


# ============================================================
# VCD Parser (minimal, for pono output)
# ============================================================

def parse_vcd(vcd_path):
    """Parse a VCD file and return {signal_name: [(time, value), ...]}."""
    signals = {}      # id -> name
    waveforms = {}    # name -> [(time, value)]
    current_time = 0

    with open(vcd_path) as f:
        in_defs = False
        scope_stack = []
        for line in f:
            line = line.strip()
            if line.startswith("$scope"):
                parts = line.split()
                if len(parts) >= 3:
                    scope_stack.append(parts[2])
            elif line.startswith("$upscope"):
                if scope_stack:
                    scope_stack.pop()
            elif line.startswith("$var"):
                parts = line.split()
                # $var wire 16 ! counter $end
                if len(parts) >= 5:
                    var_id = parts[3]
                    var_name = parts[4]
                    signals[var_id] = var_name
                    waveforms[var_name] = []
            elif line.startswith("#"):
                current_time = int(line[1:])
            elif line and not line.startswith("$"):
                # value change: either "bVALUE ID" or "0ID" / "1ID"
                if line.startswith("b"):
                    parts = line.split()
                    if len(parts) == 2:
                        val = parts[0][1:]  # strip 'b'
                        vid = parts[1]
                        if vid in signals:
                            waveforms[signals[vid]].append((current_time, val))
                elif len(line) >= 2 and line[0] in "01xzXZ":
                    val = line[0]
                    vid = line[1:]
                    if vid in signals:
                        waveforms[signals[vid]].append((current_time, val))

    return waveforms


def vcd_to_trace(waveforms, num_steps=None):
    """Convert VCD waveforms to step-indexed trace.
    Returns list of dicts: [{signal: value, ...}, ...]
    """
    # find all unique times
    all_times = sorted(set(t for w in waveforms.values() for t, _ in w))
    if num_steps and len(all_times) > num_steps:
        all_times = all_times[:num_steps]

    trace = []
    current_vals = {}
    time_idx = 0
    for step, t in enumerate(all_times):
        # update values at this time
        for name, changes in waveforms.items():
            for ct, cv in changes:
                if ct == t:
                    current_vals[name] = cv
        trace.append(dict(current_vals))

    return trace


def load_trace_json(path):
    """Load a simulation trace from JSON: {"steps": [{signal: value}, ...]}"""
    with open(path) as f:
        data = json.load(f)
    if "steps" in data:
        return data["steps"]
    if isinstance(data, list):
        return data
    return []


# ============================================================
# Predicate Evaluator (SMT-LIB style on concrete values)
# ============================================================

def evaluate_predicate_on_trace(atom_str, trace):
    """Evaluate a predicate atom string at each step of the trace.
    Returns list of booleans (one per step), or None if can't evaluate.
    """
    results = []

    # Parse common patterns:
    # (= stateN #bVALUE)  -- equality with constant
    # (= stateN stateM)   -- equality between states
    # (bvult stateN stateM) -- unsigned less than
    eq_const = re.match(r'\(=\s+(\w+)\s+#b([01]+)\)', atom_str)
    eq_var = re.match(r'\(=\s+(\w+)\s+(\w+)\)', atom_str)
    bvult = re.match(r'\(bvult\s+(\w+)\s+(\w+)\)', atom_str)
    bvule = re.match(r'\(bvule\s+(\w+)\s+(\w+)\)', atom_str)

    for step_vals in trace:
        if eq_const:
            var_name = eq_const.group(1)
            const_val = eq_const.group(2)
            if var_name in step_vals:
                val = step_vals[var_name]
                # pad or trim to match
                val_padded = val.zfill(len(const_val))
                results.append(val_padded == const_val)
            else:
                results.append(None)
        elif eq_var:
            v1 = eq_var.group(1)
            v2 = eq_var.group(2)
            if v1 in step_vals and v2 in step_vals:
                results.append(step_vals[v1] == step_vals[v2])
            else:
                results.append(None)
        elif bvult:
            v1 = bvult.group(1)
            v2 = bvult.group(2)
            if v1 in step_vals and v2 in step_vals:
                try:
                    results.append(int(step_vals[v1], 2) < int(step_vals[v2], 2))
                except ValueError:
                    results.append(None)
            else:
                results.append(None)
        elif bvule:
            v1 = bvule.group(1)
            v2 = bvule.group(2)
            if v1 in step_vals and v2 in step_vals:
                try:
                    results.append(int(step_vals[v1], 2) <= int(step_vals[v2], 2))
                except ValueError:
                    results.append(None)
            else:
                results.append(None)
        else:
            return None  # can't parse this predicate

    return results


# ============================================================
# Temporal Pattern Detectors
# ============================================================

def compute_run_lengths(waveform):
    """Compute runs of consecutive same-value.
    Returns list of (value, length) tuples.
    """
    if not waveform:
        return []
    runs = []
    current = waveform[0]
    length = 1
    for v in waveform[1:]:
        if v == current:
            length += 1
        else:
            runs.append((current, length))
            current = v
            length = 1
    runs.append((current, length))
    return runs


def detect_monotonic(waveform):
    """Detect if predicate transitions at most once (monotonic rise or fall).
    Returns (is_monotonic, direction, transition_step) or (False, None, None).
    """
    transitions = []
    for i in range(1, len(waveform)):
        if waveform[i] != waveform[i - 1] and waveform[i] is not None and waveform[i - 1] is not None:
            transitions.append(i)
    if len(transitions) == 0:
        return True, "constant", None
    if len(transitions) == 1:
        direction = "rise" if waveform[transitions[0]] else "fall"
        return True, direction, transitions[0]
    return False, None, None


def detect_phase_based(waveform, min_phase_len=2):
    """Detect if predicate is true only during a specific cycle range.
    Returns (is_phase, true_start, true_end) or (False, None, None).
    """
    runs = compute_run_lengths(waveform)
    true_runs = [(i, r) for i, r in enumerate(runs) if r[0] is True]
    if len(true_runs) == 1:
        idx, (_, length) = true_runs[0]
        if length >= min_phase_len:
            start = sum(r[1] for r in runs[:idx])
            end = start + length - 1
            return True, start, end
    false_runs = [(i, r) for i, r in enumerate(runs) if r[0] is False]
    if len(false_runs) == 1:
        idx, (_, length) = false_runs[0]
        if length >= min_phase_len:
            start = sum(r[1] for r in runs[:idx])
            end = start + length - 1
            return True, start, end  # phase of "false"
    return False, None, None


def detect_periodic(waveform, min_periods=3):
    """Detect if predicate oscillates with a period.
    Returns (is_periodic, period) or (False, None).
    """
    if len(waveform) < 4:
        return False, None

    # try periods from 1 to len/min_periods
    for period in range(1, len(waveform) // min_periods + 1):
        matches = True
        for i in range(period, len(waveform)):
            if waveform[i] != waveform[i % period]:
                matches = False
                break
        if matches:
            return True, period
    return False, None


def detect_eventually_stable(waveform, stable_threshold=0.5):
    """Detect if predicate settles to a constant after initial transient.
    Returns (is_stable, stable_value, settle_step).
    """
    if not waveform:
        return False, None, None
    n = len(waveform)
    # scan from the end backwards to find where it last changed
    last_val = waveform[-1]
    settle_step = n - 1
    for i in range(n - 2, -1, -1):
        if waveform[i] != last_val:
            settle_step = i + 1
            break
    else:
        settle_step = 0

    stable_len = n - settle_step
    if stable_len >= n * stable_threshold:
        return True, last_val, settle_step
    return False, None, None


def compute_duty_cycle(waveform):
    """Fraction of steps where predicate is True."""
    valid = [v for v in waveform if v is not None]
    if not valid:
        return None
    return sum(1 for v in valid if v) / len(valid)


def compute_toggle_rate(waveform):
    """Number of transitions / (total steps - 1)."""
    if len(waveform) < 2:
        return 0.0
    transitions = sum(1 for i in range(1, len(waveform))
                      if waveform[i] != waveform[i - 1]
                      and waveform[i] is not None
                      and waveform[i - 1] is not None)
    return transitions / (len(waveform) - 1)


def detect_sustained_runs(waveform, threshold=5):
    """Find runs of same value >= threshold cycles.
    Returns list of (value, start_step, length).
    """
    runs = compute_run_lengths(waveform)
    sustained = []
    step = 0
    for val, length in runs:
        if length >= threshold and val is not None:
            sustained.append((val, step, length))
        step += length
    return sustained


def detect_correlated_pairs(pred_waveforms, threshold=0.9):
    """Find pairs of predicates that are highly correlated (agree or disagree).
    Returns list of (pred_a, pred_b, correlation, type).
    """
    names = list(pred_waveforms.keys())
    results = []
    for a, b in combinations(names, 2):
        wa = pred_waveforms[a]
        wb = pred_waveforms[b]
        n = min(len(wa), len(wb))
        if n == 0:
            continue
        agree = sum(1 for i in range(n) if wa[i] == wb[i] and wa[i] is not None)
        disagree = sum(1 for i in range(n) if wa[i] is not None and wb[i] is not None and wa[i] != wb[i])
        valid = agree + disagree
        if valid == 0:
            continue
        if agree / valid >= threshold:
            results.append((a, b, agree / valid, "agree"))
        elif disagree / valid >= threshold:
            results.append((a, b, disagree / valid, "disagree"))
    return results


def detect_triggered(pred_waveforms):
    """Find predicates where B changes only after A changes.
    Returns list of (trigger_pred, dependent_pred).
    """
    names = list(pred_waveforms.keys())
    results = []
    for a, b in combinations(names, 2):
        wa = pred_waveforms[a]
        wb = pred_waveforms[b]
        n = min(len(wa), len(wb))
        if n < 3:
            continue

        # check if B's first transition happens after A's first transition
        a_first = None
        b_first = None
        for i in range(1, n):
            if a_first is None and wa[i] != wa[i - 1] and wa[i] is not None:
                a_first = i
            if b_first is None and wb[i] != wb[i - 1] and wb[i] is not None:
                b_first = i

        if a_first is not None and b_first is not None:
            if a_first < b_first:
                results.append((a, b, a_first, b_first))
            elif b_first < a_first:
                results.append((b, a, b_first, a_first))

    return results


# ============================================================
# Main Report
# ============================================================

def print_temporal_report(clause_data, pred_waveforms, num_steps):
    """Print comprehensive temporal pattern analysis."""
    print("=" * 70)
    print("TEMPORAL PREDICATE PATTERN ANALYSIS")
    print("=" * 70)
    print(f"\nSimulation length: {num_steps} steps")
    print(f"Predicates evaluated: {len(pred_waveforms)}")

    if not pred_waveforms:
        print("\nNo predicates could be evaluated on the trace.")
        print("=" * 70)
        return

    # Per-predicate analysis
    print("\n" + "=" * 70)
    print("PER-PREDICATE TEMPORAL PATTERNS")
    print("=" * 70)

    sustained_threshold = max(5, num_steps // 10)  # at least 10% of trace

    for name in sorted(pred_waveforms.keys()):
        wf = pred_waveforms[name]
        print(f"\n--- {name} ---")

        # Basic stats
        duty = compute_duty_cycle(wf)
        toggle = compute_toggle_rate(wf)
        runs = compute_run_lengths(wf)
        max_true_run = max((l for v, l in runs if v is True), default=0)
        max_false_run = max((l for v, l in runs if v is False), default=0)

        print(f"  Duty cycle:   {duty:.1%}" if duty is not None else "  Duty cycle:   N/A")
        print(f"  Toggle rate:  {toggle:.3f} (transitions per step)")
        print(f"  Max true run: {max_true_run} steps")
        print(f"  Max false run: {max_false_run} steps")

        # Waveform preview (first 60 steps)
        preview = "".join("1" if v else "0" if v is not None else "?" for v in wf[:60])
        print(f"  Waveform:     {preview}{'...' if len(wf) > 60 else ''}")

        # Monotonic
        is_mono, direction, step = detect_monotonic(wf)
        if is_mono:
            if direction == "constant":
                val = wf[0] if wf else None
                print(f"  >> MONOTONIC: always {'TRUE' if val else 'FALSE'}")
            else:
                print(f"  >> MONOTONIC {direction} at step {step}")

        # Phase-based
        is_phase, start, end = detect_phase_based(wf)
        if is_phase and not is_mono:
            print(f"  >> PHASE: active during steps {start}-{end}")

        # Periodic
        is_periodic, period = detect_periodic(wf)
        if is_periodic:
            print(f"  >> PERIODIC with period {period}")

        # Eventually stable
        is_stable, stable_val, settle = detect_eventually_stable(wf)
        if is_stable and not is_mono:
            print(f"  >> EVENTUALLY STABLE: settles to {'TRUE' if stable_val else 'FALSE'} at step {settle}")

        # Sustained runs
        sustained = detect_sustained_runs(wf, threshold=sustained_threshold)
        if sustained:
            print(f"  >> SUSTAINED RUNS (>={sustained_threshold} steps):")
            for val, start, length in sustained:
                print(f"     {'TRUE' if val else 'FALSE'} for {length} steps starting at step {start}")

    # Cross-predicate analysis
    print("\n" + "=" * 70)
    print("CROSS-PREDICATE PATTERNS")
    print("=" * 70)

    # Correlated pairs
    correlated = detect_correlated_pairs(pred_waveforms)
    if correlated:
        print("\n--- Correlated Pairs ---")
        for a, b, corr, typ in sorted(correlated, key=lambda x: -x[2]):
            print(f"  {a}")
            print(f"    {'AGREES' if typ == 'agree' else 'DISAGREES'} with")
            print(f"  {b}")
            print(f"    correlation: {corr:.1%}")
    else:
        print("\n--- Correlated Pairs: none found ---")

    # Triggered pairs
    triggered = detect_triggered(pred_waveforms)
    if triggered:
        print("\n--- Triggered Pairs ---")
        for trigger, dependent, t_step, d_step in triggered:
            print(f"  {trigger} (changes at step {t_step})")
            print(f"    TRIGGERS")
            print(f"  {dependent} (changes at step {d_step})")
    else:
        print("\n--- Triggered Pairs: none found ---")

    # Summary classification
    print("\n" + "=" * 70)
    print("PATTERN SUMMARY")
    print("=" * 70)

    categories = defaultdict(list)
    for name, wf in sorted(pred_waveforms.items()):
        is_mono, direction, _ = detect_monotonic(wf)
        toggle = compute_toggle_rate(wf)
        duty = compute_duty_cycle(wf)
        sustained = detect_sustained_runs(wf, threshold=sustained_threshold)

        if is_mono and direction == "constant":
            val = wf[0] if wf else None
            categories[f"always_{'true' if val else 'false'}"].append(name)
        elif is_mono:
            categories[f"monotonic_{direction}"].append(name)
        elif detect_periodic(wf)[0]:
            categories["periodic"].append(name)
        elif detect_eventually_stable(wf)[0]:
            categories["eventually_stable"].append(name)
        elif toggle < 0.05:
            categories["state_like_low_toggle"].append(name)
        elif sustained:
            categories["has_sustained_runs"].append(name)
        else:
            categories["dynamic"].append(name)

    for cat in ["always_true", "always_false", "monotonic_rise", "monotonic_fall",
                "eventually_stable", "periodic", "state_like_low_toggle",
                "has_sustained_runs", "dynamic"]:
        preds = categories.get(cat, [])
        if preds:
            print(f"\n  {cat} ({len(preds)}):")
            for p in preds:
                duty = compute_duty_cycle(pred_waveforms[p])
                print(f"    {p}  (duty={duty:.1%})" if duty is not None else f"    {p}")

    print("\n" + "=" * 70)
    print("END OF TEMPORAL ANALYSIS")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Temporal pattern analysis of IC3 predicates on simulation waveforms")
    parser.add_argument("blocking_clauses_json",
                        help="Path to blocking_clauses.json from pono --dump-blocking-clauses")
    parser.add_argument("--vcd", default=None,
                        help="VCD file from pono --vcd")
    parser.add_argument("--trace", default=None,
                        help="JSON trace file: [{signal: value}, ...]")
    parser.add_argument("--steps", type=int, default=None,
                        help="Limit analysis to first N steps")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file (default: stdout)")
    args = parser.parse_args()

    # Load blocking clauses
    with open(args.blocking_clauses_json) as f:
        clause_data = json.load(f)

    atoms = clause_data.get("all_atoms", [])
    if not atoms:
        print("No predicate atoms found in blocking clauses.")
        return

    # Load trace
    trace = None
    if args.vcd:
        waveforms = parse_vcd(args.vcd)
        trace = vcd_to_trace(waveforms, args.steps)
    elif args.trace:
        trace = load_trace_json(args.trace)
        if args.steps:
            trace = trace[:args.steps]

    if not trace:
        print("No simulation trace provided. Use --vcd or --trace.")
        print(f"Found {len(atoms)} predicate atoms to evaluate:")
        for a in atoms:
            print(f"  {a}")
        print("\nTo generate a trace, run:")
        print("  ./build/pono -e bmc -k 50 --vcd trace.vcd --witness input.btor2")
        return

    num_steps = len(trace)
    print(f"Loaded trace with {num_steps} steps")

    # Evaluate each predicate atom on the trace
    pred_waveforms = {}
    for atom in atoms:
        wf = evaluate_predicate_on_trace(atom, trace)
        if wf is not None:
            pred_waveforms[atom] = wf
        else:
            print(f"  Warning: Could not evaluate predicate: {atom}")

    # Output
    if args.output:
        with open(args.output, "w") as f:
            old_stdout = sys.stdout
            sys.stdout = f
            print_temporal_report(clause_data, pred_waveforms, num_steps)
            sys.stdout = old_stdout
        print(f"Report written to {args.output}")
    else:
        print_temporal_report(clause_data, pred_waveforms, num_steps)


if __name__ == "__main__":
    main()
