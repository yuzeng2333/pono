#!/usr/bin/env python3
"""
BTOR2 Compositional Decomposer for Miter Circuits

Given a miter BTOR2 with copy1.* and copy2.* state variables,
generates per-module sub-problems where:
  - Property: copy1.module.states == copy2.module.states
  - All other module pairs are assumed equal (constraints)
  - Pono's --static-coi will shrink each sub-problem automatically

Usage:
  python3 btor2_decompose.py <input.btor2> <output_dir>
"""

import sys
import os
import re
from collections import defaultdict


def parse_btor2_states(btor2_path, depth=1):
    """Parse BTOR2 file and return state variable info grouped by module.
    
    depth: how many hierarchy levels to use for grouping.
           1 = top-level modules (e.g., rob, csr_exe_unit)
           2 = two levels (e.g., csr_exe_unit.div, fp_pipeline.fpiu_unit)
    """
    states = {}       # btor_id -> (sort_id, name)
    state_names = {}  # btor_id -> name
    modules = defaultdict(list)  # module_name -> [btor_id, ...]

    with open(btor2_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(';'):
                continue
            parts = line.split()
            if len(parts) >= 3 and parts[1] == 'state':
                btor_id = int(parts[0])
                sort_id = int(parts[2])
                name = parts[3] if len(parts) > 3 else f"state{btor_id}"
                states[btor_id] = (sort_id, name)
                state_names[btor_id] = name

                # Extract module from hierarchical name
                # e.g., copy1.rob.rob_val_0 -> copy1.rob
                if name.startswith('copy1.') or name.startswith('copy2.'):
                    prefix = name[:5]  # copy1 or copy2
                    rest = name[6:]    # everything after "copy1."
                    # Get module at specified depth
                    parts_hier = rest.split('.')
                    if len(parts_hier) > depth:
                        module = '.'.join(parts_hier[:depth])
                    elif len(parts_hier) > 1:
                        module = '.'.join(parts_hier[:-1])
                    else:
                        module = '_top'
                    modules[module].append((btor_id, sort_id, name))

    return states, state_names, modules


def find_copy_pairs(modules):
    """Find corresponding state variable pairs between copy1 and copy2."""
    pairs_by_module = {}  # module -> [(copy1_id, copy2_id, sort_id, base_name), ...]

    for module, state_list in modules.items():
        copy1_states = {}
        copy2_states = {}
        for btor_id, sort_id, name in state_list:
            if name.startswith('copy1.'):
                base = name[6:]  # strip copy1.
                copy1_states[base] = (btor_id, sort_id)
            elif name.startswith('copy2.'):
                base = name[6:]  # strip copy2.
                copy2_states[base] = (btor_id, sort_id)

        pairs = []
        for base, (id1, sort1) in copy1_states.items():
            if base in copy2_states:
                id2, sort2 = copy2_states[base]
                assert sort1 == sort2, f"Sort mismatch for {base}: {sort1} vs {sort2}"
                pairs.append((id1, id2, sort1, base))

        if pairs:
            pairs_by_module[module] = pairs

    return pairs_by_module


def read_btor2_lines(btor2_path):
    """Read all lines from BTOR2 file."""
    with open(btor2_path) as f:
        return f.readlines()


def find_existing_ids(lines):
    """Find the maximum BTOR ID used in the file."""
    max_id = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith(';'):
            continue
        parts = line.split()
        if parts:
            try:
                max_id = max(max_id, int(parts[0]))
            except ValueError:
                pass
    return max_id


def find_sort_for_bitvec(lines, width):
    """Find or note sort ID for bitvec of given width."""
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 3 and parts[1] == 'sort' and parts[2] == 'bitvec' and int(parts[3]) == width:
            return int(parts[0])
    return None


def generate_module_subproblem(btor2_path, lines, module, pairs, all_pairs_by_module, output_dir):
    """
    Generate a sub-BTOR2 for verifying one module pair.

    Strategy:
    - Remove the original 'bad' line
    - Add constraints: for all OTHER modules, copy1.state == copy2.state
    - Add new property: all states in THIS module are equal
    """
    max_id = find_existing_ids(lines)
    next_id = max_id + 1

    # Find sort for bitvec 1 (for boolean operations)
    sort1 = find_sort_for_bitvec(lines, 1)

    new_lines = []

    # Copy all lines except 'bad' and existing 'constraint' lines
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            new_lines.append(line)
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1] == 'bad':
            # Skip original bad property
            new_lines.append(f'; REMOVED original bad: {stripped}\n')
            continue
        new_lines.append(line)

    new_lines.append(f'\n; === Compositional sub-problem for module: {module} ===\n')

    # Add ASSUMPTION constraints: for every OTHER module, states are equal
    new_lines.append(f'; --- Assumptions: other modules have equal states ---\n')
    for other_module, other_pairs in all_pairs_by_module.items():
        if other_module == module:
            continue
        for id1, id2, sort_id, base_name in other_pairs:
            # eq_id = (copy1.state == copy2.state)
            eq_id = next_id; next_id += 1
            new_lines.append(f'{eq_id} eq {sort1} {id1} {id2} ; assume {base_name}\n')
            # constraint: this equality must hold
            constraint_id = next_id; next_id += 1
            new_lines.append(f'{constraint_id} constraint {eq_id}\n')

    # Add PROPERTY: all states in THIS module must be equal
    new_lines.append(f'; --- Property: module {module} states are equal ---\n')

    if not pairs:
        print(f"  WARNING: module {module} has no pairs, skipping")
        return

    # Build conjunction of equalities
    eq_ids = []
    for id1, id2, sort_id, base_name in pairs:
        eq_id = next_id; next_id += 1
        new_lines.append(f'{eq_id} eq {sort1} {id1} {id2} ; check {base_name}\n')
        eq_ids.append(eq_id)

    # AND them together
    if len(eq_ids) == 1:
        all_eq_id = eq_ids[0]
    else:
        all_eq_id = eq_ids[0]
        for i in range(1, len(eq_ids)):
            and_id = next_id; next_id += 1
            new_lines.append(f'{and_id} and {sort1} {all_eq_id} {eq_ids[i]}\n')
            all_eq_id = and_id

    # NOT (all equal) = bad state
    not_id = next_id; next_id += 1
    new_lines.append(f'{not_id} not {sort1} {all_eq_id}\n')

    # bad: not all equal
    bad_id = next_id; next_id += 1
    new_lines.append(f'{bad_id} bad {not_id} ; module {module} equivalence\n')

    # Write output
    output_path = os.path.join(output_dir, f'module_{module}.btor2')
    with open(output_path, 'w') as f:
        f.writelines(new_lines)

    return output_path


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input.btor2> <output_dir> [depth]")
        sys.exit(1)

    btor2_path = sys.argv[1]
    output_dir = sys.argv[2]
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    os.makedirs(output_dir, exist_ok=True)

    print(f"Parsing {btor2_path} (depth={depth})...")
    states, state_names, modules = parse_btor2_states(btor2_path, depth)
    print(f"  Found {len(states)} state variables in {len(modules)} modules")

    pairs_by_module = find_copy_pairs(modules)
    print(f"\nModule pair counts:")
    total_pairs = 0
    for module in sorted(pairs_by_module.keys(), key=lambda m: -len(pairs_by_module[m])):
        count = len(pairs_by_module[module])
        total_pairs += count
        print(f"  {module}: {count} state pairs")
    print(f"  TOTAL: {total_pairs} pairs")

    print(f"\nGenerating sub-problems in {output_dir}/...")
    for module in sorted(pairs_by_module.keys()):
        pairs = pairs_by_module[module]
        out_path = generate_module_subproblem(
            btor2_path, read_btor2_lines(btor2_path),
            module, pairs, pairs_by_module, output_dir)
        if out_path:
            print(f"  {module}: {len(pairs)} pairs -> {out_path}")

    # Also generate a script to run all sub-problems
    script_path = os.path.join(output_dir, 'run_all.sh')
    with open(script_path, 'w') as f:
        f.write('#!/bin/bash\n')
        f.write('# Run all compositional sub-problems\n')
        f.write(f'PONO=${{PONO:-pono}}\n')
        f.write(f'TIMEOUT=${{TIMEOUT:-120}}\n\n')
        for module in sorted(pairs_by_module.keys()):
            f.write(f'echo "=== Module: {module} ({len(pairs_by_module[module])} pairs) ==="\n')
            f.write(f'timeout $TIMEOUT $PONO -e ic3ia --static-coi '
                    f'--ic3ia-skip-init-predicates --no-ic3-unsatcore-gen '
                    f'-v 1 -k 50 '
                    f'{output_dir}/module_{module}.btor2\n')
            f.write(f'echo "EXIT: $?"\n')
            f.write(f'echo ""\n\n')
    os.chmod(script_path, 0o755)
    print(f"\nGenerated runner script: {script_path}")


if __name__ == '__main__':
    main()