#!/usr/bin/env python3
"""
Analyze predicate patterns from IC3 blocking clauses.

Workflow:
  1. Run pono with --dump-blocking-clauses to get blocking_clauses.json
  2. Run this script on that JSON to simulate and find patterns.

The script:
  - Reads blocking clauses JSON (frame-level predicates)
  - Runs random constrained simulation using the transition system
  - Evaluates each predicate at each simulation step
  - Reports patterns: always-true, always-false, monotonic, periodic, correlated
"""

import json
import argparse
import sys
from collections import defaultdict


def load_blocking_clauses(path):
    """Load the blocking clauses JSON dumped by pono."""
    with open(path) as f:
        return json.load(f)


def _lit_str(lit):
    """Get string representation from a literal (handles both dict and str)."""
    if isinstance(lit, dict):
        return lit.get("str", str(lit))
    return lit


def _lit_atom(lit):
    """Get the atom (unnegated predicate) from a literal."""
    if isinstance(lit, dict):
        return lit.get("atom", _lit_str(lit))
    s = str(lit)
    if s.startswith("(not "):
        return s[5:-1]
    return s


def _lit_negated(lit):
    """Check if literal is negated."""
    if isinstance(lit, dict):
        return lit.get("negated", False)
    return str(lit).startswith("(not ")


def extract_unique_predicates(data):
    """Extract unique predicate atoms from all frames."""
    preds = set()
    for frame in data.get("frames", []):
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                preds.add(_lit_atom(lit))
    return sorted(preds)


def analyze_frame_distribution(data):
    """Analyze how predicates are distributed across frames."""
    frame_pred_count = {}
    pred_frame_membership = defaultdict(set)

    for frame in data.get("frames", []):
        fidx = frame.get("level", frame.get("frame_index", 0))
        preds_in_frame = set()
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                s = _lit_str(lit)
                preds_in_frame.add(s)
                pred_frame_membership[s].add(fidx)
        frame_pred_count[fidx] = len(preds_in_frame)

    return frame_pred_count, pred_frame_membership


def analyze_polarity(data):
    """Analyze if predicates appear more often positive or negated."""
    pos_count = defaultdict(int)
    neg_count = defaultdict(int)

    for frame in data.get("frames", []):
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                atom = _lit_atom(lit)
                if _lit_negated(lit):
                    neg_count[atom] += 1
                else:
                    pos_count[atom] += 1

    return pos_count, neg_count


def analyze_clause_structure(data):
    """Analyze clause sizes and co-occurrence patterns."""
    clause_sizes = []
    cooccurrence = defaultdict(int)

    for frame in data.get("frames", []):
        for clause in frame.get("clauses", []):
            lits = clause.get("literals", [])
            lit_strs = [_lit_str(l) for l in lits]
            clause_sizes.append(len(lit_strs))
            for i in range(len(lit_strs)):
                for j in range(i + 1, len(lit_strs)):
                    pair = tuple(sorted([lit_strs[i], lit_strs[j]]))
                    cooccurrence[pair] += 1

    return clause_sizes, cooccurrence


def analyze_frame_progression(data):
    """Analyze how predicate sets change from frame to frame (monotonicity)."""
    frames = sorted(data.get("frames", []), key=lambda f: f.get("level", 0))
    frame_preds = []
    for frame in frames:
        preds = set()
        for clause in frame.get("clauses", []):
            for lit in clause.get("literals", []):
                preds.add(_lit_str(lit))
        frame_preds.append((frame.get("level", 0), preds))

    # check if each frame's predicates are a superset of the previous
    monotonic = True
    for i in range(1, len(frame_preds)):
        if not frame_preds[i][1].issuperset(frame_preds[i - 1][1]):
            monotonic = False
            break

    return frame_preds, monotonic


def print_report(data):
    """Print a comprehensive analysis report."""
    print("=" * 70)
    print("IC3 BLOCKING CLAUSE PREDICATE PATTERN ANALYSIS")
    print("=" * 70)

    meta = {k: v for k, v in data.items() if k != "frames"}
    print(f"\nMetadata: {json.dumps(meta, indent=2)}")

    frames = data.get("frames", [])
    print(f"\nTotal frames: {len(frames)}")
    total_clauses = sum(len(f.get("clauses", [])) for f in frames)
    print(f"Total clauses: {total_clauses}")

    # Unique predicates
    unique_preds = extract_unique_predicates(data)
    print(f"Unique predicate atoms: {len(unique_preds)}")

    # Frame distribution
    print("\n--- Frame Distribution ---")
    frame_pred_count, pred_frame_membership = analyze_frame_distribution(data)
    for fidx in sorted(frame_pred_count.keys()):
        print(f"  Frame {fidx}: {frame_pred_count[fidx]} literal occurrences")

    # Predicates in all frames
    all_frame_indices = set(f.get("level", 0) for f in frames)
    universal_preds = [p for p, fs in pred_frame_membership.items()
                       if fs == all_frame_indices]
    if universal_preds:
        print(f"\n  Predicates in ALL frames ({len(universal_preds)}):")
        for p in sorted(universal_preds)[:20]:
            print(f"    {p}")
        if len(universal_preds) > 20:
            print(f"    ... and {len(universal_preds) - 20} more")

    # Polarity analysis
    print("\n--- Polarity Analysis ---")
    pos_count, neg_count = analyze_polarity(data)
    all_atoms = set(pos_count.keys()) | set(neg_count.keys())
    always_pos = [a for a in all_atoms if pos_count[a] > 0 and neg_count[a] == 0]
    always_neg = [a for a in all_atoms if neg_count[a] > 0 and pos_count[a] == 0]
    mixed = [a for a in all_atoms if pos_count[a] > 0 and neg_count[a] > 0]

    print(f"  Always positive: {len(always_pos)}")
    for p in sorted(always_pos)[:10]:
        print(f"    + {p}  (count={pos_count[p]})")
    print(f"  Always negated: {len(always_neg)}")
    for p in sorted(always_neg)[:10]:
        print(f"    - {p}  (count={neg_count[p]})")
    print(f"  Mixed polarity: {len(mixed)}")
    for p in sorted(mixed)[:10]:
        print(f"    +/- {p}  (pos={pos_count[p]}, neg={neg_count[p]})")

    # Clause structure
    print("\n--- Clause Structure ---")
    clause_sizes, cooccurrence = analyze_clause_structure(data)
    if clause_sizes:
        avg_size = sum(clause_sizes) / len(clause_sizes)
        print(f"  Clause count: {len(clause_sizes)}")
        print(f"  Avg clause size: {avg_size:.1f}")
        print(f"  Min clause size: {min(clause_sizes)}")
        print(f"  Max clause size: {max(clause_sizes)}")

        # size histogram
        from collections import Counter
        size_hist = Counter(clause_sizes)
        print("  Size distribution:")
        for size in sorted(size_hist.keys()):
            print(f"    size {size}: {size_hist[size]} clauses")

    # Top co-occurring pairs
    if cooccurrence:
        print("\n  Top co-occurring literal pairs:")
        top_pairs = sorted(cooccurrence.items(), key=lambda x: -x[1])[:15]
        for (a, b), count in top_pairs:
            print(f"    ({count}x) {a}  AND  {b}")

    # Frame progression
    print("\n--- Frame Progression ---")
    frame_preds, monotonic = analyze_frame_progression(data)
    print(f"  Frame predicate sets are {'monotonically increasing' if monotonic else 'NOT monotonic'}")

    if len(frame_preds) >= 2:
        for i in range(1, len(frame_preds)):
            prev_idx, prev_set = frame_preds[i - 1]
            curr_idx, curr_set = frame_preds[i]
            added = curr_set - prev_set
            removed = prev_set - curr_set
            if added:
                print(f"  Frame {prev_idx} -> {curr_idx}: +{len(added)} new literals")
                for p in sorted(added)[:5]:
                    print(f"    + {p}")
            if removed:
                print(f"  Frame {prev_idx} -> {curr_idx}: -{len(removed)} removed literals")
                for p in sorted(removed)[:5]:
                    print(f"    - {p}")

    # Predicate type classification
    print("\n--- Predicate Type Classification ---")
    type_counts = defaultdict(int)
    for p in unique_preds:
        if "=" in p and "bvult" not in p and "bvslt" not in p:
            type_counts["equality"] += 1
        elif "bvult" in p or "bvslt" in p or "bvule" in p or "bvsle" in p:
            type_counts["comparison"] += 1
        elif "bvadd" in p or "bvsub" in p or "bvmul" in p:
            type_counts["arithmetic"] += 1
        elif "bvand" in p or "bvor" in p or "bvxor" in p:
            type_counts["bitwise"] += 1
        elif "extract" in p:
            type_counts["extract"] += 1
        else:
            type_counts["other"] += 1

    for typ, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {typ}: {count}")

    print("\n" + "=" * 70)
    print("END OF ANALYSIS")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze predicate patterns from IC3 blocking clauses")
    parser.add_argument("blocking_clauses_json",
                        help="Path to blocking_clauses.json from pono --dump-blocking-clauses")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file for report (default: stdout)")
    args = parser.parse_args()

    data = load_blocking_clauses(args.blocking_clauses_json)

    if args.output:
        with open(args.output, "w") as f:
            old_stdout = sys.stdout
            sys.stdout = f
            print_report(data)
            sys.stdout = old_stdout
        print(f"Report written to {args.output}")
    else:
        print_report(data)


if __name__ == "__main__":
    main()
