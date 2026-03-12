#!/usr/bin/env python3
"""
Batch predicate pattern analysis across multiple benchmarks.

Runs pono IC3 on each benchmark, dumps blocking clauses, analyzes patterns,
and produces aggregate statistics across all benchmarks.

Usage:
  python3 scripts/run_batch_predicate_analysis.py [options]
  python3 scripts/run_batch_predicate_analysis.py --timeout 600 --engine ic3ia
  python3 scripts/run_batch_predicate_analysis.py --benchmarks samples/int_win.btor2 samples/uv_example.btor2
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from collections import defaultdict, Counter
from pathlib import Path


# ============================================================
# Helper: literal field accessors (same as in analyze_predicate_patterns.py)
# ============================================================

def _lit_str(lit):
    if isinstance(lit, dict):
        return lit.get("str", str(lit))
    return lit

def _lit_atom(lit):
    if isinstance(lit, dict):
        return lit.get("atom", _lit_str(lit))
    s = str(lit)
    if s.startswith("(not "):
        return s[5:-1]
    return s

def _lit_negated(lit):
    if isinstance(lit, dict):
        return lit.get("negated", False)
    return str(lit).startswith("(not ")


# ============================================================
# Discover benchmarks
# ============================================================

def discover_benchmarks(pono_dir, extra_dirs=None):
    """Find all .btor2 benchmark files."""
    benchmarks = []

    # pono samples
    samples = sorted(glob.glob(os.path.join(pono_dir, "samples", "*.btor2")))
    benchmarks.extend(samples)

    # hwmcc25 benchmarks
    hwmcc_dir = os.path.join(os.path.dirname(pono_dir), "hwmcc25")
    if os.path.isdir(hwmcc_dir):
        hwmcc = sorted(glob.glob(os.path.join(hwmcc_dir, "**", "*.btor2"), recursive=True))
        benchmarks.extend(hwmcc)

    # extra directories
    if extra_dirs:
        for d in extra_dirs:
            if os.path.isdir(d):
                benchmarks.extend(sorted(glob.glob(os.path.join(d, "**", "*.btor2"), recursive=True)))
            elif os.path.isfile(d):
                benchmarks.append(d)

    return benchmarks


# ============================================================
# Run pono and collect blocking clauses
# ============================================================

def run_pono_ic3(pono_bin, benchmark, output_json, engine="ic3ia", bound=500, timeout=600,
                 sim_steps=0, sim_output=None):
    """Run pono IC3 on a benchmark and dump blocking clauses + simulation trace.
    Returns (result, elapsed_seconds, error_msg).
    result: "TRUE", "FALSE", "UNKNOWN", "TIMEOUT", "ERROR"
    """
    cmd = [
        pono_bin,
        "-e", engine,
        "-k", str(bound),
        "--dump-blocking-clauses", output_json,
    ]
    if sim_steps > 0 and sim_output:
        cmd += ["--simulate", str(sim_steps), "--simulate-output", sim_output]
    cmd.append(benchmark)

    import signal

    start = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Send SIGTERM first to let pono dump partial blocking clauses
            proc.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            elapsed = time.time() - start
            return "TIMEOUT", elapsed, f"Timed out after {timeout}s"

        elapsed = time.time() - start
        stdout = stdout.strip()
        stderr = stderr.strip()

        if "unsat" in stdout:
            return "TRUE", elapsed, None
        elif "sat" in stdout and "unsat" not in stdout:
            return "FALSE", elapsed, None
        elif "unknown" in stdout:
            return "UNKNOWN", elapsed, None
        elif "error" in stdout.lower() or proc.returncode != 0:
            return "ERROR", elapsed, stderr or stdout
        else:
            return "UNKNOWN", elapsed, stdout

    except Exception as e:
        elapsed = time.time() - start
        return "ERROR", elapsed, str(e)


# ============================================================
# Analyze a single blocking clauses JSON (static patterns)
# ============================================================

def analyze_single(clause_json_path):
    """Analyze a single blocking clauses JSON and return stats dict."""
    try:
        with open(clause_json_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    frames = data.get("frames", [])
    all_atoms = data.get("all_atoms", [])
    atom_stats = data.get("atom_stats", {})

    # basic counts
    total_clauses = sum(len(f.get("clauses", [])) for f in frames)
    num_frames = len(frames)
    num_atoms = len(all_atoms)

    # clause sizes
    clause_sizes = []
    for frame in frames:
        for clause in frame.get("clauses", []):
            clause_sizes.append(len(clause.get("literals", [])))

    # polarity
    pos_only = 0
    neg_only = 0
    mixed = 0
    for atom, stats in atom_stats.items():
        pc = stats.get("pos_count", 0)
        nc = stats.get("neg_count", 0)
        if pc > 0 and nc == 0:
            pos_only += 1
        elif nc > 0 and pc == 0:
            neg_only += 1
        elif pc > 0 and nc > 0:
            mixed += 1

    # predicate type classification
    type_counts = Counter()
    for p in all_atoms:
        if "=" in p and "bvult" not in p and "bvslt" not in p:
            type_counts["equality"] += 1
        elif any(op in p for op in ["bvult", "bvslt", "bvule", "bvsle", "bvugt", "bvsgt"]):
            type_counts["comparison"] += 1
        elif any(op in p for op in ["bvadd", "bvsub", "bvmul", "bvudiv", "bvsdiv"]):
            type_counts["arithmetic"] += 1
        elif any(op in p for op in ["bvand", "bvor", "bvxor", "bvnot"]):
            type_counts["bitwise"] += 1
        elif "extract" in p:
            type_counts["extract"] += 1
        elif "concat" in p:
            type_counts["concat"] += 1
        elif "ite" in p:
            type_counts["ite"] += 1
        else:
            type_counts["other"] += 1

    # frame progression: monotonic?
    frame_preds = []
    for frame in sorted(frames, key=lambda f: f.get("level", 0)):
        preds = set()
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                preds.add(_lit_str(lit))
        frame_preds.append(preds)

    monotonic = True
    for i in range(1, len(frame_preds)):
        if not frame_preds[i].issuperset(frame_preds[i - 1]):
            monotonic = False
            break

    # atom frame span (how many frames does each atom appear in)
    atom_frame_spans = []
    for atom, stats in atom_stats.items():
        mn = stats.get("min_frame", 0)
        mx = stats.get("max_frame", 0)
        atom_frame_spans.append(mx - mn + 1 if mx >= mn else 1)

    return {
        "num_frames": num_frames,
        "num_clauses": total_clauses,
        "num_atoms": num_atoms,
        "clause_sizes": clause_sizes,
        "avg_clause_size": sum(clause_sizes) / len(clause_sizes) if clause_sizes else 0,
        "polarity": {"positive_only": pos_only, "negative_only": neg_only, "mixed": mixed},
        "pred_types": dict(type_counts),
        "frame_monotonic": monotonic,
        "atom_frame_spans": atom_frame_spans,
        "avg_atom_frame_span": sum(atom_frame_spans) / len(atom_frame_spans) if atom_frame_spans else 0,
        "all_atoms": all_atoms,
    }


# ============================================================
# Aggregate stats across benchmarks
# ============================================================

def aggregate_stats(results):
    """Aggregate per-benchmark stats into cross-benchmark summary."""
    agg = {
        "total_benchmarks": len(results),
        "results": Counter(),
        "benchmarks_with_clauses": 0,
        "total_atoms": 0,
        "total_clauses": 0,
        "total_frames": 0,
        "clause_size_distribution": Counter(),
        "pred_type_distribution": Counter(),
        "polarity_distribution": {"positive_only": 0, "negative_only": 0, "mixed": 0},
        "monotonic_count": 0,
        "non_monotonic_count": 0,
        "atoms_per_benchmark": [],
        "clauses_per_benchmark": [],
        "frames_per_benchmark": [],
        "avg_clause_sizes": [],
        "avg_atom_frame_spans": [],
        "waveform_pattern_distribution": Counter(),
        "benchmarks_with_waveform": 0,
        "per_benchmark": [],
    }

    for r in results:
        agg["results"][r["result"]] += 1

        bm_summary = {
            "benchmark": r["benchmark_name"],
            "result": r["result"],
            "elapsed": r.get("elapsed", 0),
        }

        stats = r.get("stats")
        if stats:
            agg["benchmarks_with_clauses"] += 1
            agg["total_atoms"] += stats["num_atoms"]
            agg["total_clauses"] += stats["num_clauses"]
            agg["total_frames"] += stats["num_frames"]

            for sz in stats["clause_sizes"]:
                agg["clause_size_distribution"][sz] += 1

            for typ, cnt in stats["pred_types"].items():
                agg["pred_type_distribution"][typ] += cnt

            for k in ["positive_only", "negative_only", "mixed"]:
                agg["polarity_distribution"][k] += stats["polarity"][k]

            if stats["frame_monotonic"]:
                agg["monotonic_count"] += 1
            else:
                agg["non_monotonic_count"] += 1

            agg["atoms_per_benchmark"].append(stats["num_atoms"])
            agg["clauses_per_benchmark"].append(stats["num_clauses"])
            agg["frames_per_benchmark"].append(stats["num_frames"])
            agg["avg_clause_sizes"].append(stats["avg_clause_size"])
            agg["avg_atom_frame_spans"].append(stats["avg_atom_frame_span"])

            bm_summary["num_atoms"] = stats["num_atoms"]
            bm_summary["num_clauses"] = stats["num_clauses"]
            bm_summary["num_frames"] = stats["num_frames"]
            bm_summary["pred_types"] = stats["pred_types"]
            bm_summary["polarity"] = stats["polarity"]
            bm_summary["monotonic"] = stats["frame_monotonic"]
            bm_summary["avg_clause_size"] = stats["avg_clause_size"]

        # Waveform pattern aggregation
        wf = r.get("waveform")
        if wf:
            agg["benchmarks_with_waveform"] += 1
            for pat, cnt in wf.items():
                agg["waveform_pattern_distribution"][pat] += cnt
            bm_summary["waveform_patterns"] = wf

        agg["per_benchmark"].append(bm_summary)

    return agg


def _safe_avg(lst):
    return sum(lst) / len(lst) if lst else 0


def print_aggregate_report(agg, output_file=None):
    """Print the aggregate summary report."""
    import io
    buf = io.StringIO()

    def p(s=""):
        buf.write(s + "\n")

    p("=" * 70)
    p("AGGREGATE PREDICATE PATTERN ANALYSIS ACROSS BENCHMARKS")
    p("=" * 70)

    p(f"\nTotal benchmarks: {agg['total_benchmarks']}")
    p(f"Benchmarks with blocking clauses: {agg['benchmarks_with_clauses']}")
    p()
    p("--- Prover Results ---")
    for res in ["TRUE", "FALSE", "UNKNOWN", "TIMEOUT", "ERROR"]:
        cnt = agg["results"].get(res, 0)
        if cnt > 0:
            p(f"  {res}: {cnt}")

    p()
    p("--- Overall Counts ---")
    p(f"  Total predicate atoms: {agg['total_atoms']}")
    p(f"  Total blocking clauses: {agg['total_clauses']}")
    p(f"  Total frames: {agg['total_frames']}")
    p(f"  Avg atoms per benchmark: {_safe_avg(agg['atoms_per_benchmark']):.1f}")
    p(f"  Avg clauses per benchmark: {_safe_avg(agg['clauses_per_benchmark']):.1f}")
    p(f"  Avg frames per benchmark: {_safe_avg(agg['frames_per_benchmark']):.1f}")
    p(f"  Avg clause size: {_safe_avg(agg['avg_clause_sizes']):.2f}")
    p(f"  Avg atom frame span: {_safe_avg(agg['avg_atom_frame_spans']):.2f}")

    p()
    p("--- Predicate Type Distribution ---")
    total_typed = sum(agg["pred_type_distribution"].values())
    for typ, cnt in sorted(agg["pred_type_distribution"].items(), key=lambda x: -x[1]):
        pct = cnt / total_typed * 100 if total_typed > 0 else 0
        p(f"  {typ}: {cnt} ({pct:.1f}%)")

    p()
    p("--- Polarity Distribution ---")
    total_pol = sum(agg["polarity_distribution"].values())
    for k in ["positive_only", "negative_only", "mixed"]:
        cnt = agg["polarity_distribution"][k]
        pct = cnt / total_pol * 100 if total_pol > 0 else 0
        p(f"  {k}: {cnt} ({pct:.1f}%)")

    p()
    p("--- Clause Size Distribution ---")
    for sz in sorted(agg["clause_size_distribution"].keys()):
        cnt = agg["clause_size_distribution"][sz]
        p(f"  size {sz}: {cnt} clauses")

    p()
    p("--- Frame Progression ---")
    p(f"  Monotonic: {agg['monotonic_count']}")
    p(f"  Non-monotonic: {agg['non_monotonic_count']}")

    # Waveform pattern section
    wf_dist = agg.get("waveform_pattern_distribution", {})
    if wf_dist:
        p()
        p("--- Waveform Predicate Patterns (simulation) ---")
        p(f"  Benchmarks with waveform analysis: {agg.get('benchmarks_with_waveform', 0)}")
        total_wf = sum(wf_dist.values())
        for pat, cnt in sorted(wf_dist.items(), key=lambda x: -x[1]):
            pct = cnt / total_wf * 100 if total_wf > 0 else 0
            p(f"  {pat}: {cnt} ({pct:.1f}%)")
        p()
        p("  Interpretation:")
        p("    always_true   = invariant predicate (holds on all reachable states)")
        p("    always_false  = blocks unreachable regions")
        p("    monotonic_*   = initialization/convergence predicate")
        p("    periodic_*    = cyclic state behavior")
        p("    phased/mixed  = state-dependent dynamic predicate")
        p("    unresolvable  = predicate uses variables not in simulation trace")

    p()
    p("=" * 70)
    p("PER-BENCHMARK SUMMARY")
    p("=" * 70)
    p()
    p(f"{'Benchmark':<40} {'Result':<8} {'Atoms':>6} {'Clauses':>8} {'Frames':>7} {'Mono':>5} {'Time':>7}")
    p("-" * 80)
    for bm in agg["per_benchmark"]:
        name = bm["benchmark"][:39]
        res = bm["result"]
        atoms = bm.get("num_atoms", "-")
        clauses = bm.get("num_clauses", "-")
        frames = bm.get("num_frames", "-")
        mono = "Y" if bm.get("monotonic") else ("N" if "monotonic" in bm else "-")
        elapsed = f"{bm.get('elapsed', 0):.1f}s"
        p(f"{name:<40} {res:<8} {str(atoms):>6} {str(clauses):>8} {str(frames):>7} {mono:>5} {elapsed:>7}")

    p()
    p("=" * 70)
    p("END OF AGGREGATE ANALYSIS")
    p("=" * 70)

    report = buf.getvalue()
    print(report)

    if output_file:
        with open(output_file, "w") as f:
            f.write(report)
        print(f"\nReport also written to {output_file}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Batch predicate pattern analysis across benchmarks")
    parser.add_argument("--pono-dir", default="/local/home/yzarg/pono",
                        help="Path to pono source directory")
    parser.add_argument("--pono-bin", default=None,
                        help="Path to pono binary (default: <pono-dir>/build/pono)")
    parser.add_argument("--engine", default="ic3ia",
                        help="IC3 engine variant (default: ic3ia)")
    parser.add_argument("--bound", type=int, default=500,
                        help="BMC bound / IC3 frame limit (default: 500)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Timeout per benchmark in seconds (default: 600)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory for results (default: <pono-dir>/results)")
    parser.add_argument("--benchmarks", nargs="*", default=None,
                        help="Specific benchmark files (overrides auto-discovery)")
    parser.add_argument("--extra-dirs", nargs="*", default=None,
                        help="Extra directories to scan for .btor2 files")
    parser.add_argument("--report", default=None,
                        help="Path for aggregate report text file")
    parser.add_argument("--max-benchmarks", type=int, default=None,
                        help="Limit number of benchmarks to run")
    parser.add_argument("--sim-steps", type=int, default=500,
                        help="Number of simulation cycles per benchmark (default: 500)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip benchmarks whose output files already exist")
    args = parser.parse_args()

    pono_dir = os.path.abspath(args.pono_dir)
    pono_bin = args.pono_bin or os.path.join(pono_dir, "build", "pono")
    output_dir = args.output_dir or os.path.join(pono_dir, "results")

    if not os.path.isfile(pono_bin):
        print(f"Error: pono binary not found at {pono_bin}")
        sys.exit(1)

    # Create output directories
    clauses_dir = os.path.join(output_dir, "blocking_clauses")
    sim_dir = os.path.join(output_dir, "simulation")
    waveform_dir = os.path.join(output_dir, "waveform_analysis")
    analysis_dir = os.path.join(output_dir, "analysis")
    os.makedirs(clauses_dir, exist_ok=True)
    os.makedirs(sim_dir, exist_ok=True)
    os.makedirs(waveform_dir, exist_ok=True)
    os.makedirs(analysis_dir, exist_ok=True)

    # Import waveform analysis module
    waveform_script = os.path.join(pono_dir, "scripts", "analyze_waveform_predicates.py")

    # Discover benchmarks
    if args.benchmarks:
        benchmarks = args.benchmarks
    else:
        benchmarks = discover_benchmarks(pono_dir, args.extra_dirs)

    if args.max_benchmarks:
        benchmarks = benchmarks[:args.max_benchmarks]

    sim_steps = args.sim_steps

    print(f"Found {len(benchmarks)} benchmarks")
    print(f"Engine: {args.engine}, Bound: {args.bound}, Timeout: {args.timeout}s")
    print(f"Simulation: {sim_steps} cycles per benchmark")
    print(f"Output directory: {output_dir}")
    print()

    # Process each benchmark
    all_results = []
    for i, bm in enumerate(benchmarks):
        bm_name = os.path.splitext(os.path.basename(bm))[0]
        clause_json = os.path.join(clauses_dir, f"{bm_name}_clauses.json")
        sim_json = os.path.join(sim_dir, f"{bm_name}_sim.json")
        wf_json = os.path.join(waveform_dir, f"{bm_name}_waveform.json")

        # Resume: skip if all output files already exist
        if args.resume and os.path.isfile(clause_json) and os.path.isfile(wf_json):
            print(f"[{i+1}/{len(benchmarks)}] {bm_name} ... SKIPPED (resume)", flush=True)
            # Re-load cached results
            entry = {
                "benchmark_path": bm,
                "benchmark_name": bm_name,
                "result": "CACHED",
                "elapsed": 0,
                "error": None,
            }
            stats = analyze_single(clause_json)
            if stats:
                entry["stats"] = stats
                entry["clause_json"] = clause_json
            if os.path.isfile(wf_json):
                try:
                    with open(wf_json) as f:
                        wf_data = json.load(f)
                    entry["waveform"] = wf_data.get("pattern_summary", {})
                    entry["waveform_json"] = wf_json
                except Exception:
                    pass
            all_results.append(entry)
            continue

        print(f"[{i+1}/{len(benchmarks)}] {bm_name} ... ", end="", flush=True)

        result, elapsed, err = run_pono_ic3(
            pono_bin, bm, clause_json,
            engine=args.engine, bound=args.bound, timeout=args.timeout,
            sim_steps=sim_steps, sim_output=sim_json
        )

        entry = {
            "benchmark_path": bm,
            "benchmark_name": bm_name,
            "result": result,
            "elapsed": elapsed,
            "error": err,
        }

        # Analyze if we got blocking clauses
        if os.path.isfile(clause_json):
            stats = analyze_single(clause_json)
            if stats:
                entry["stats"] = stats
                entry["clause_json"] = clause_json
                print(f"{result} ({elapsed:.1f}s) atoms={stats['num_atoms']} clauses={stats['num_clauses']}", end="")
            else:
                print(f"{result} ({elapsed:.1f}s) [no valid clauses]", end="")
        else:
            print(f"{result} ({elapsed:.1f}s) [no dump]", end="")

        # Run waveform analysis if both clauses and simulation exist
        if os.path.isfile(clause_json) and os.path.isfile(sim_json):
            try:
                proc = subprocess.run(
                    [sys.executable, waveform_script, clause_json, sim_json, wf_json],
                    capture_output=True, text=True, timeout=60
                )
                if os.path.isfile(wf_json):
                    with open(wf_json) as f:
                        wf_data = json.load(f)
                    entry["waveform"] = wf_data.get("pattern_summary", {})
                    entry["waveform_json"] = wf_json
                    patterns = wf_data.get("pattern_summary", {})
                    pat_str = ", ".join(f"{k}={v}" for k, v in patterns.items())
                    print(f" | waveform: {pat_str}", end="")
            except Exception as e:
                print(f" | waveform error: {e}", end="")

        print()  # newline
        all_results.append(entry)

    # Aggregate
    print("\n" + "=" * 70)
    print("AGGREGATING RESULTS...")
    print("=" * 70 + "\n")

    agg = aggregate_stats(all_results)

    # Save aggregate JSON
    agg_json_path = os.path.join(output_dir, "aggregate_stats.json")
    # Convert Counters to dicts for JSON serialization
    agg_json = dict(agg)
    agg_json["results"] = dict(agg_json["results"])
    agg_json["clause_size_distribution"] = {str(k): v for k, v in agg_json["clause_size_distribution"].items()}
    agg_json["pred_type_distribution"] = dict(agg_json["pred_type_distribution"])
    agg_json["waveform_pattern_distribution"] = dict(agg_json["waveform_pattern_distribution"])
    # Remove non-serializable per-benchmark atoms
    for bm in agg_json.get("per_benchmark", []):
        bm.pop("all_atoms", None)

    with open(agg_json_path, "w") as f:
        json.dump(agg_json, f, indent=2, default=str)
    print(f"Aggregate JSON saved to {agg_json_path}")

    # Print and save report
    report_path = args.report or os.path.join(output_dir, "aggregate_summary.txt")
    print_aggregate_report(agg, report_path)


if __name__ == "__main__":
    main()
