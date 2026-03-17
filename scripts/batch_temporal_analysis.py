#!/usr/bin/env python3
"""
Batch temporal pattern analysis across all benchmarks with useful predicates.

Runs analyze_temporal_patterns.py on every benchmark that has both:
  - blocking clauses with atoms > 0
  - a simulation trace

Aggregates results across all benchmarks.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Import temporal analysis functions
sys.path.insert(0, os.path.dirname(__file__))
from analyze_temporal_patterns import (
    evaluate_predicate_on_trace,
    load_trace_json,
    compute_duty_cycle,
    compute_toggle_rate,
    compute_run_lengths,
    detect_monotonic,
    detect_phase_based,
    detect_periodic,
    detect_eventually_stable,
    detect_sustained_runs,
    detect_correlated_pairs,
    detect_triggered,
)


def analyze_one_benchmark(args_tuple):
    """Analyze temporal patterns for one benchmark.
    Returns dict with per-benchmark temporal analysis results.
    """
    (clause_json, sim_json, bm_name) = args_tuple

    result = {
        "benchmark": bm_name,
        "num_atoms": 0,
        "num_evaluated": 0,
        "num_unevaluable": 0,
        "num_steps": 0,
        "patterns": Counter(),
        "per_predicate": [],
        "correlated_pairs": [],
        "triggered_pairs": [],
        "error": None,
    }

    try:
        with open(clause_json) as f:
            clause_data = json.load(f)
        atoms = clause_data.get("all_atoms", [])
        result["num_atoms"] = len(atoms)

        if not atoms:
            result["error"] = "no atoms"
            return result

        # Load simulation trace
        trace = load_trace_json(sim_json)
        if not trace:
            result["error"] = "empty trace"
            return result

        result["num_steps"] = len(trace)
        sustained_threshold = max(5, len(trace) // 10)

        # Evaluate each predicate
        pred_waveforms = {}
        for atom in atoms:
            wf = evaluate_predicate_on_trace(atom, trace)
            if wf is not None:
                pred_waveforms[atom] = wf
            else:
                result["num_unevaluable"] += 1

        result["num_evaluated"] = len(pred_waveforms)

        # Classify each predicate
        for name, wf in pred_waveforms.items():
            is_mono, direction, mono_step = detect_monotonic(wf)
            is_periodic, period = detect_periodic(wf)
            is_stable, stable_val, settle_step = detect_eventually_stable(wf)
            is_phase, phase_start, phase_end = detect_phase_based(wf)
            duty = compute_duty_cycle(wf)
            toggle = compute_toggle_rate(wf)
            sustained = detect_sustained_runs(wf, threshold=sustained_threshold)

            # Classify
            if is_mono and direction == "constant":
                val = wf[0] if wf else None
                category = f"always_{'true' if val else 'false'}"
            elif is_mono:
                category = f"monotonic_{direction}"
            elif is_periodic:
                category = f"periodic_p{period}"
            elif is_stable:
                category = "eventually_stable"
            elif is_phase:
                category = "phase_based"
            elif toggle < 0.05:
                category = "state_like_low_toggle"
            elif sustained:
                category = "has_sustained_runs"
            else:
                category = "dynamic"

            result["patterns"][category] += 1

            pred_info = {
                "atom": name,
                "category": category,
                "duty_cycle": duty,
                "toggle_rate": toggle,
            }
            if is_mono:
                pred_info["monotonic_direction"] = direction
                if mono_step is not None:
                    pred_info["transition_step"] = mono_step
            if is_periodic:
                pred_info["period"] = period
            if is_stable:
                pred_info["settle_step"] = settle_step
                pred_info["stable_value"] = stable_val
            if is_phase:
                pred_info["phase_range"] = [phase_start, phase_end]

            result["per_predicate"].append(pred_info)

        # Cross-predicate analysis
        if len(pred_waveforms) >= 2:
            corr = detect_correlated_pairs(pred_waveforms, threshold=0.9)
            result["correlated_pairs"] = [(a, b, c, t) for a, b, c, t in corr]

            trig = detect_triggered(pred_waveforms)
            result["triggered_pairs"] = [(a, b, s1, s2) for a, b, s1, s2 in trig]

    except Exception as e:
        result["error"] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Batch temporal pattern analysis across all benchmarks")
    parser.add_argument("--results-dir", default=None,
                        help="Results directory (default: <pono-dir>/results)")
    parser.add_argument("--pono-dir", default="/home/yzarg/pono",
                        help="Pono source directory")
    parser.add_argument("-j", "--jobs", type=int, default=8,
                        help="Number of parallel workers (default: 8)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output JSON file for aggregate results")
    parser.add_argument("--report", default=None,
                        help="Output text report file")
    args = parser.parse_args()

    results_dir = args.results_dir or os.path.join(args.pono_dir, "results")
    clauses_dir = os.path.join(results_dir, "blocking_clauses")
    sim_dir = os.path.join(results_dir, "simulation")

    # Find benchmarks with both useful clauses and simulation traces
    tasks = []
    for f in sorted(os.listdir(clauses_dir)):
        if not f.endswith("_clauses.json"):
            continue
        clause_path = os.path.join(clauses_dir, f)
        bm_name = f.replace("_clauses.json", "")
        sim_path = os.path.join(sim_dir, f"{bm_name}_sim.json")

        if not os.path.isfile(sim_path):
            continue

        # Quick check: has atoms?
        try:
            with open(clause_path) as fh:
                data = json.load(fh)
            if len(data.get("all_atoms", [])) == 0:
                continue
        except Exception:
            continue

        tasks.append((clause_path, sim_path, bm_name))

    print(f"Found {len(tasks)} benchmarks with useful predicates and simulation traces")
    print(f"Running temporal analysis with {args.jobs} workers...\n")

    # Run in parallel
    all_results = []
    completed = 0

    with ProcessPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(analyze_one_benchmark, t): t[2] for t in tasks}

        for future in as_completed(futures):
            completed += 1
            bm_name = futures[future]
            try:
                result = future.result()
                all_results.append(result)
                n_eval = result["num_evaluated"]
                n_atoms = result["num_atoms"]
                pats = dict(result["patterns"])
                pat_str = ", ".join(f"{k}={v}" for k, v in sorted(pats.items(), key=lambda x: -x[1]))
                err = result.get("error", "")
                if err:
                    print(f"  ({completed}/{len(tasks)}) {bm_name}: ERROR - {err}")
                else:
                    print(f"  ({completed}/{len(tasks)}) {bm_name}: {n_eval}/{n_atoms} evaluated | {pat_str}")
            except Exception as e:
                print(f"  ({completed}/{len(tasks)}) {bm_name}: EXCEPTION - {e}")

    # Sort by benchmark name
    all_results.sort(key=lambda r: r["benchmark"])

    # Aggregate
    print("\n" + "=" * 70)
    print("AGGREGATE TEMPORAL PATTERN ANALYSIS")
    print("=" * 70)

    total_atoms = sum(r["num_atoms"] for r in all_results)
    total_evaluated = sum(r["num_evaluated"] for r in all_results)
    total_unevaluable = sum(r["num_unevaluable"] for r in all_results)
    total_correlated = sum(len(r["correlated_pairs"]) for r in all_results)
    total_triggered = sum(len(r["triggered_pairs"]) for r in all_results)

    # Aggregate pattern counts
    agg_patterns = Counter()
    for r in all_results:
        for pat, cnt in r["patterns"].items():
            agg_patterns[pat] += cnt

    # Aggregate duty cycle and toggle rate stats per category
    category_duty_cycles = defaultdict(list)
    category_toggle_rates = defaultdict(list)
    all_predicates = []
    for r in all_results:
        for p in r["per_predicate"]:
            cat = p["category"]
            if p["duty_cycle"] is not None:
                category_duty_cycles[cat].append(p["duty_cycle"])
            category_toggle_rates[cat].append(p["toggle_rate"])
            all_predicates.append({
                "benchmark": r["benchmark"],
                **p
            })

    print(f"\nBenchmarks analyzed: {len(all_results)}")
    print(f"Total predicate atoms: {total_atoms}")
    print(f"Successfully evaluated: {total_evaluated} ({total_evaluated/total_atoms*100:.1f}%)" if total_atoms > 0 else "")
    print(f"Could not evaluate: {total_unevaluable}")
    print(f"Correlated pairs found: {total_correlated}")
    print(f"Triggered pairs found: {total_triggered}")

    print(f"\n--- Temporal Pattern Distribution ---")
    total_pats = sum(agg_patterns.values())
    for pat, cnt in sorted(agg_patterns.items(), key=lambda x: -x[1]):
        pct = cnt / total_pats * 100 if total_pats > 0 else 0
        avg_duty = sum(category_duty_cycles.get(pat, [])) / len(category_duty_cycles.get(pat, [1])) if category_duty_cycles.get(pat) else 0
        avg_toggle = sum(category_toggle_rates.get(pat, [])) / len(category_toggle_rates.get(pat, [1])) if category_toggle_rates.get(pat) else 0
        print(f"  {pat:<30} {cnt:>5} ({pct:>5.1f}%)  avg_duty={avg_duty:.2f}  avg_toggle={avg_toggle:.3f}")

    # Useful predicates summary
    print(f"\n--- Predicate Usefulness for IC3 Guidance ---")
    invariant_preds = agg_patterns.get("always_true", 0) + agg_patterns.get("always_false", 0)
    convergent_preds = agg_patterns.get("monotonic_rise", 0) + agg_patterns.get("monotonic_fall", 0)
    stable_preds = agg_patterns.get("eventually_stable", 0)
    periodic_preds = sum(v for k, v in agg_patterns.items() if k.startswith("periodic"))
    dynamic_preds = agg_patterns.get("dynamic", 0) + agg_patterns.get("has_sustained_runs", 0)
    state_like = agg_patterns.get("state_like_low_toggle", 0)
    phase_preds = agg_patterns.get("phase_based", 0)

    print(f"  Invariant (always true/false):     {invariant_preds:>5}  -- strong candidates for inductive strengthening")
    print(f"  Convergent (monotonic rise/fall):   {convergent_preds:>5}  -- initialization/convergence predicates")
    print(f"  Eventually stable:                  {stable_preds:>5}  -- transient then invariant")
    print(f"  Phase-based:                        {phase_preds:>5}  -- active in specific cycle ranges")
    print(f"  Periodic:                           {periodic_preds:>5}  -- cyclic behavior, potential clock/counter")
    print(f"  State-like (low toggle):            {state_like:>5}  -- rarely changing, state predicates")
    print(f"  Dynamic:                            {dynamic_preds:>5}  -- complex temporal behavior")

    # Per-benchmark summary table
    print(f"\n--- Per-Benchmark Summary ---")
    print(f"{'Benchmark':<45} {'Atoms':>6} {'Eval':>5} {'Inv':>4} {'Conv':>5} {'Stbl':>5} {'Dyn':>4} {'Corr':>5}")
    print("-" * 85)
    for r in all_results:
        if r.get("error"):
            continue
        pats = r["patterns"]
        inv = pats.get("always_true", 0) + pats.get("always_false", 0)
        conv = pats.get("monotonic_rise", 0) + pats.get("monotonic_fall", 0)
        stbl = pats.get("eventually_stable", 0)
        dyn = pats.get("dynamic", 0) + pats.get("has_sustained_runs", 0)
        corr = len(r["correlated_pairs"])
        name = r["benchmark"][:44]
        print(f"{name:<45} {r['num_atoms']:>6} {r['num_evaluated']:>5} {inv:>4} {conv:>5} {stbl:>5} {dyn:>4} {corr:>5}")

    print(f"\n{'='*70}")
    print("END OF AGGREGATE TEMPORAL ANALYSIS")
    print(f"{'='*70}")

    # Save JSON output
    output_path = args.output or os.path.join(results_dir, "temporal_analysis_aggregate.json")
    output_data = {
        "summary": {
            "benchmarks_analyzed": len(all_results),
            "total_atoms": total_atoms,
            "total_evaluated": total_evaluated,
            "total_unevaluable": total_unevaluable,
            "total_correlated_pairs": total_correlated,
            "total_triggered_pairs": total_triggered,
            "pattern_distribution": dict(agg_patterns),
        },
        "per_benchmark": [{
            "benchmark": r["benchmark"],
            "num_atoms": r["num_atoms"],
            "num_evaluated": r["num_evaluated"],
            "num_unevaluable": r["num_unevaluable"],
            "num_steps": r["num_steps"],
            "patterns": dict(r["patterns"]),
            "num_correlated_pairs": len(r["correlated_pairs"]),
            "num_triggered_pairs": len(r["triggered_pairs"]),
            "error": r.get("error"),
        } for r in all_results],
        "all_predicates": all_predicates,
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\nJSON results saved to {output_path}")

    # Save text report
    report_path = args.report or os.path.join(results_dir, "temporal_analysis_report.txt")
    # Re-capture output to file
    import io
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf

    print("=" * 70)
    print("AGGREGATE TEMPORAL PATTERN ANALYSIS")
    print("=" * 70)
    print(f"\nBenchmarks analyzed: {len(all_results)}")
    print(f"Total predicate atoms: {total_atoms}")
    print(f"Successfully evaluated: {total_evaluated}")
    print(f"Could not evaluate: {total_unevaluable}")
    print(f"Correlated pairs: {total_correlated}")
    print(f"Triggered pairs: {total_triggered}")
    print(f"\n--- Pattern Distribution ---")
    for pat, cnt in sorted(agg_patterns.items(), key=lambda x: -x[1]):
        pct = cnt / total_pats * 100 if total_pats > 0 else 0
        print(f"  {pat:<30} {cnt:>5} ({pct:>5.1f}%)")
    print(f"\n--- IC3 Guidance Usefulness ---")
    print(f"  Invariant:     {invariant_preds}")
    print(f"  Convergent:    {convergent_preds}")
    print(f"  Stable:        {stable_preds}")
    print(f"  Phase-based:   {phase_preds}")
    print(f"  Periodic:      {periodic_preds}")
    print(f"  State-like:    {state_like}")
    print(f"  Dynamic:       {dynamic_preds}")

    sys.stdout = old_stdout
    with open(report_path, "w") as f:
        f.write(buf.getvalue())
    print(f"Text report saved to {report_path}")


if __name__ == "__main__":
    main()