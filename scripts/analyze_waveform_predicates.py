#!/usr/bin/env python3
"""Analyze IC3 blocking clause predicates on simulation waveforms.

Given:
  1. Blocking clauses JSON (from --dump-blocking-clauses)
  2. Simulation trace JSON (from --simulate / --simulate-output)

This script evaluates each predicate atom on the waveform and reports patterns:
  - Always true / always false (invariant-like)
  - Monotonic (once flips, stays flipped)
  - Periodic / oscillating
  - Phase transitions (stable regions separated by changes)
  - Correlation between predicates
"""

import json
import re
import sys
import os
from collections import defaultdict


def parse_bv_value(val_str):
    """Parse a bitvector string to integer."""
    val_str = val_str.strip()
    if val_str.startswith("#b"):
        return int(val_str[2:], 2)
    if val_str.startswith("#x"):
        return int(val_str[2:], 16)
    # raw binary string
    if all(c in '01' for c in val_str) and len(val_str) > 0:
        return int(val_str, 2)
    try:
        return int(val_str)
    except ValueError:
        return None


def extract_atoms(clauses_data):
    """Extract unique predicate atoms from blocking clauses."""
    atoms = {}
    for frame in clauses_data.get("frames", []):
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                atom_str = lit.get("atom", "")
                if atom_str and atom_str not in atoms:
                    atoms[atom_str] = parse_predicate(atom_str)
    return atoms


def parse_predicate(atom_str):
    """Parse a predicate atom string into an evaluable structure.
    
    Handles patterns like:
      (= stateN #bVALUE)
      (bvult stateN #bVALUE)
      (bvslt stateN #bVALUE)
      (bvule stateN stateM)
    """
    s = atom_str.strip()
    
    # Match (op arg1 arg2)
    m = re.match(r'\((\S+)\s+(\S+)\s+(\S+)\)', s)
    if not m:
        return {"op": "unknown", "raw": s}
    
    op, arg1, arg2 = m.group(1), m.group(2), m.group(3)
    
    result = {"op": op, "raw": s}
    
    # Determine which args are state variables vs constants
    for i, arg in enumerate([arg1, arg2]):
        key = f"arg{i}"
        if arg.startswith("#b") or arg.startswith("#x"):
            result[key] = {"type": "const", "value": parse_bv_value(arg)}
        elif arg.startswith("state") or arg.startswith("input"):
            result[key] = {"type": "var", "name": arg}
        else:
            result[key] = {"type": "unknown", "raw": arg}
    
    return result


def find_var_in_trace(var_name, step_data):
    """Find a variable value in a simulation step by matching name."""
    # Direct match
    if var_name in step_data:
        return parse_bv_value(step_data[var_name])
    # Try matching stateN pattern to named variables
    return None


def build_name_map(trace_data, clauses_data):
    """Build mapping from stateN (SMT internal) names to signal names in trace.
    
    The simulation trace includes a 'name_map' field: {smt_name -> human_name}.
    We build a reverse map so predicates using stateN can find values in the trace.
    """
    name_map = {}
    
    # Use the name_map from simulation trace (smt_name -> human_name)
    if "name_map" in trace_data:
        name_map = dict(trace_data["name_map"])
    
    return name_map


def evaluate_predicate(pred, step_data, name_map=None):
    """Evaluate a parsed predicate on one simulation step.
    Returns True/False/None (None if can't evaluate).
    """
    op = pred.get("op")
    if op == "unknown":
        return None
    
    def get_value(arg_info):
        if arg_info["type"] == "const":
            return arg_info["value"]
        if arg_info["type"] == "var":
            vname = arg_info["name"]
            # Try direct
            val = find_var_in_trace(vname, step_data)
            if val is not None:
                return val
            # Try name_map
            if name_map and vname in name_map:
                val = find_var_in_trace(name_map[vname], step_data)
                if val is not None:
                    return val
        return None
    
    v0 = get_value(pred.get("arg0", {}))
    v1 = get_value(pred.get("arg1", {}))
    
    if v0 is None or v1 is None:
        return None
    
    if op == "=":
        return v0 == v1
    elif op == "bvult":
        return v0 < v1
    elif op == "bvule":
        return v0 <= v1
    elif op == "bvslt":
        # signed comparison — need bitwidth info, approximate
        return v0 < v1
    elif op == "bvsle":
        return v0 <= v1
    elif op == "bvugt":
        return v0 > v1
    elif op == "bvuge":
        return v0 >= v1
    elif op == "bvsgt":
        return v0 > v1
    elif op == "bvsge":
        return v0 >= v1
    elif op == "distinct":
        return v0 != v1
    
    return None


def classify_waveform(values):
    """Classify a boolean waveform pattern.
    Returns (pattern_label, metrics_dict).
    """
    # Filter out None values
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return "unresolvable", {}
    
    true_count = sum(1 for _, v in valid if v)
    false_count = sum(1 for _, v in valid if not v)
    total = len(valid)
    duty_ratio = true_count / total
    
    base_metrics = {
        "duty_ratio": round(duty_ratio, 3),
        "true_steps": true_count,
        "false_steps": false_count,
        "total_steps": total,
    }
    
    if true_count == total:
        return "always_true", base_metrics
    if false_count == total:
        return "always_false", base_metrics
    
    bool_seq = [v for _, v in valid]
    
    # Count transitions
    transitions = []
    for i in range(1, len(bool_seq)):
        if bool_seq[i] != bool_seq[i-1]:
            transitions.append(i)
    
    num_transitions = len(transitions)
    transition_density = num_transitions / (total - 1) if total > 1 else 0
    base_metrics["num_transitions"] = num_transitions
    base_metrics["transition_density"] = round(transition_density, 4)
    
    if num_transitions == 0:
        return "constant", base_metrics
    
    # Check monotonic (single flip)
    if num_transitions == 1:
        settling_time = transitions[0]
        base_metrics["settling_time"] = settling_time
        if bool_seq[0] and not bool_seq[-1]:
            return "monotonic_falling", base_metrics
        elif not bool_seq[0] and bool_seq[-1]:
            return "monotonic_rising", base_metrics
    
    # Check exact periodic
    detected_period = None
    for period in range(1, min(len(bool_seq) // 2 + 1, 200)):
        is_periodic = True
        for i in range(period, len(bool_seq)):
            if bool_seq[i] != bool_seq[i % period]:
                is_periodic = False
                break
        if is_periodic:
            detected_period = period
            break
    
    if detected_period:
        # Compute duty ratio within one period
        one_cycle = bool_seq[:detected_period]
        cycle_duty = sum(one_cycle) / detected_period
        base_metrics["period"] = detected_period
        base_metrics["cycle_duty_ratio"] = round(cycle_duty, 3)
        return f"periodic_p{detected_period}", base_metrics
    
    # Compute phases (stable regions)
    phases = []
    current_val = bool_seq[0]
    current_len = 1
    for i in range(1, len(bool_seq)):
        if bool_seq[i] == current_val:
            current_len += 1
        else:
            phases.append((current_val, current_len))
            current_val = bool_seq[i]
            current_len = 1
    phases.append((current_val, current_len))
    
    base_metrics["num_phases"] = len(phases)
    phase_lengths = [n for _, n in phases]
    base_metrics["avg_phase_length"] = round(sum(phase_lengths) / len(phase_lengths), 1)
    base_metrics["max_phase_length"] = max(phase_lengths)
    base_metrics["min_phase_length"] = min(phase_lengths)
    
    # Settling: find last transition to see if it converges
    last_phase_len = phases[-1][1]
    if last_phase_len > total * 0.5:
        base_metrics["settling_time"] = total - last_phase_len
        base_metrics["converged_to"] = "T" if phases[-1][0] else "F"
    
    # Detect quasi-periodic via autocorrelation on transition gaps
    if num_transitions >= 4:
        gaps = [transitions[i+1] - transitions[i] for i in range(len(transitions)-1)]
        if len(set(gaps)) <= 2:
            approx_period = round(sum(gaps) / len(gaps), 1)
            base_metrics["approx_period"] = approx_period
            base_metrics["cycle_duty_ratio"] = round(duty_ratio, 3)
            return f"quasi_periodic_p{approx_period}", base_metrics
    
    if len(phases) <= 4:
        phase_str = " -> ".join(f"{'T' if v else 'F'}x{n}" for v, n in phases)
        return f"phased", base_metrics
    
    return f"mixed", base_metrics


def compute_correlation(wf1, wf2):
    """Compute correlation between two boolean waveforms."""
    pairs = [(a, b) for a, b in zip(wf1, wf2) if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    
    agree = sum(1 for a, b in pairs if a == b)
    total = len(pairs)
    
    return agree / total


def analyze(clauses_path, sim_path, output_path=None):
    with open(clauses_path) as f:
        clauses_data = json.load(f)
    with open(sim_path) as f:
        sim_data = json.load(f)
    
    steps = sim_data.get("steps", [])
    if not steps:
        print("ERROR: No simulation steps found")
        return
    
    atoms = extract_atoms(clauses_data)
    if not atoms:
        print("No predicate atoms found in blocking clauses")
        return
    
    name_map = build_name_map(sim_data, clauses_data)
    
    print(f"=== Predicate Waveform Analysis ===")
    print(f"Simulation steps: {len(steps)}")
    print(f"Predicate atoms: {len(atoms)}")
    print()
    
    # Evaluate each predicate on the waveform
    waveforms = {}
    for atom_str, pred in atoms.items():
        wf = []
        for step in steps:
            val = evaluate_predicate(pred, step, name_map)
            wf.append(val)
        waveforms[atom_str] = wf
    
    # Classify patterns
    results = []
    print("--- Predicate Patterns on Waveform ---")
    for atom_str, wf in waveforms.items():
        pattern, metrics = classify_waveform(wf)
        resolved = sum(1 for v in wf if v is not None)
        
        # Find which frames use this atom
        frames_used = []
        for frame in clauses_data.get("frames", []):
            for clause in frame.get("clauses", []):
                for lit in clause.get("literals", []):
                    if lit.get("atom") == atom_str:
                        frames_used.append(frame["level"])
        
        result = {
            "predicate": atom_str,
            "pattern": pattern,
            "metrics": metrics,
            "resolved_steps": resolved,
            "total_steps": len(wf),
            "frames_used": sorted(set(frames_used)),
            "waveform": ["T" if v else "F" if v is not None else "?" for v in wf]
        }
        results.append(result)
        
        wf_str = "".join(result["waveform"][:60])
        if len(wf) > 60:
            wf_str += "..."
        metrics_str = ", ".join(f"{k}={v}" for k, v in metrics.items())
        print(f"  {atom_str}")
        print(f"    Pattern: {pattern}  [{metrics_str}]")
        print(f"    In frames: {result['frames_used']}")
        print(f"    Waveform: {wf_str}")
        print()
    
    # Correlation analysis between predicates
    atom_list = list(waveforms.keys())
    if len(atom_list) > 1:
        print("--- Predicate Correlations ---")
        for i in range(len(atom_list)):
            for j in range(i+1, len(atom_list)):
                corr = compute_correlation(waveforms[atom_list[i]], 
                                          waveforms[atom_list[j]])
                if corr is not None:
                    label = "correlated" if corr > 0.8 else \
                            "anti-correlated" if corr < 0.2 else \
                            "independent"
                    print(f"  {atom_list[i]} vs {atom_list[j]}")
                    print(f"    Agreement: {corr:.1%} ({label})")
        print()
    
    # Summary statistics
    print("--- Pattern Summary ---")
    pattern_counts = defaultdict(int)
    for r in results:
        # Simplify pattern name for counting
        base_pattern = r["pattern"].split("(")[0]
        pattern_counts[base_pattern] += 1
    
    for pat, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"  {pat}: {count}")
    
    # Key insights
    print()
    print("--- Key Insights ---")
    always_true = [r for r in results if r["pattern"] == "always_true"]
    always_false = [r for r in results if r["pattern"] == "always_false"]
    monotonic = [r for r in results if "monotonic" in r["pattern"]]
    
    if always_true:
        print(f"  {len(always_true)} predicates are ALWAYS TRUE on simulation:")
        print(f"    -> These may be true invariants or too weak to be useful")
        for r in always_true:
            print(f"       {r['predicate']}")
    
    if always_false:
        print(f"  {len(always_false)} predicates are ALWAYS FALSE on simulation:")
        print(f"    -> These capture unreachable states (blocking unsafe regions)")
        for r in always_false:
            print(f"       {r['predicate']}")
    
    if monotonic:
        print(f"  {len(monotonic)} predicates show MONOTONIC behavior:")
        print(f"    -> These capture initialization or convergence patterns")
        for r in monotonic:
            print(f"       {r['predicate']} ({r['pattern']})")
    
    mixed = [r for r in results if "mixed" in r["pattern"] or "phased" in r["pattern"]]
    if mixed:
        print(f"  {len(mixed)} predicates show DYNAMIC behavior:")
        print(f"    -> These capture state-dependent conditions")
        for r in mixed:
            print(f"       {r['predicate']} ({r['pattern']})")
    
    # Output JSON
    if output_path:
        out = {
            "clauses_file": clauses_path,
            "simulation_file": sim_path,
            "num_steps": len(steps),
            "num_predicates": len(atoms),
            "predicates": results,
            "pattern_summary": dict(pattern_counts)
        }
        with open(output_path, 'w') as f:
            json.dump(out, f, indent=2)
        print(f"\nDetailed results written to {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <blocking_clauses.json> <simulation.json> [output.json]")
        sys.exit(1)
    
    output = sys.argv[3] if len(sys.argv) > 3 else None
    analyze(sys.argv[1], sys.argv[2], output)
