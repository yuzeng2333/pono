#!/usr/bin/env python3
"""
BTOR2 substitution-based decomposer.

Instead of adding equality constraints (which bloat the formula),
this replaces copy2 state IDs with copy1 state IDs for assumed-equal modules.
This physically merges the two copies, letting COI eliminate the dead logic.
"""

import sys
import os
from collections import defaultdict

# BTOR2 ops and which argument positions (0-indexed from after sort) are node IDs
# Format: op -> list of arg positions that are node ID references
# Positions not listed are literal values (bit indices, widths, constants, etc.)
BTOR2_ID_ARGS = {
    # Unary ops: 1 arg (position 0)
    'not': [0], 'inc': [0], 'dec': [0], 'neg': [0], 'redand': [0], 'redor': [0],
    'redxor': [0],
    # Binary ops: 2 args (positions 0, 1)
    'and': [0,1], 'nand': [0,1], 'nor': [0,1], 'or': [0,1], 'xnor': [0,1], 'xor': [0,1],
    'add': [0,1], 'sub': [0,1], 'mul': [0,1],
    'sdiv': [0,1], 'udiv': [0,1], 'smod': [0,1], 'srem': [0,1], 'urem': [0,1],
    'sll': [0,1], 'sra': [0,1], 'srl': [0,1], 'rol': [0,1], 'ror': [0,1],
    'eq': [0,1], 'neq': [0,1],
    'sgt': [0,1], 'sgte': [0,1], 'slt': [0,1], 'slte': [0,1],
    'ugt': [0,1], 'ugte': [0,1], 'ult': [0,1], 'ulte': [0,1],
    'concat': [0,1], 'implies': [0,1],
    'read': [0,1], 'saddo': [0,1], 'uaddo': [0,1], 'sdivo': [0,1],
    'smulo': [0,1], 'umulo': [0,1], 'ssubo': [0,1], 'usubo': [0,1],
    # Ternary ops
    'ite': [0,1,2], 'write': [0,1,2],
    # Special: literal args
    'slice': [0],       # slice sort arg upper lower (upper/lower are literals)
    'sext': [0],        # sext sort arg width (width is literal)
    'uext': [0],        # uext sort arg width (width is literal)
    # Init/next: state_id and value_id
    'init': [1,2],      # init sort state value (after sort: state=pos1, value=pos2)
    'next': [1,2],      # next sort state value
    # Constraint/bad/output: single arg
    'constraint': [0], 'bad': [0], 'output': [0], 'fair': [0], 'justice': [0],
    # No args
    'sort': [], 'state': [], 'input': [],
    'const': [], 'constd': [], 'consth': [], 'one': [], 'ones': [], 'zero': [],
}


def parse_btor2(btor2_path, depth=1):
    """Parse BTOR2 and identify copy1/copy2 state pairs by module."""
    with open(btor2_path) as f:
        lines = f.readlines()
    
    state_names = {}
    pairs_by_module = defaultdict(list)
    copy1_by_base = {}
    copy2_by_base = {}
    
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            continue
        parts = stripped.split()
        if len(parts) >= 4 and parts[1] == 'state':
            btor_id = int(parts[0])
            sort_id = int(parts[2])
            name = parts[3]
            state_names[btor_id] = name
            
            if name.startswith('copy1.') or name.startswith('copy2.'):
                prefix = name[:5]
                rest = name[6:]
                parts_hier = rest.split('.')
                if len(parts_hier) > depth:
                    module = '.'.join(parts_hier[:depth])
                elif len(parts_hier) > 1:
                    module = '.'.join(parts_hier[:-1])
                else:
                    module = '_top'
                
                if prefix == 'copy1':
                    copy1_by_base[rest] = (btor_id, sort_id, module)
                else:
                    copy2_by_base[rest] = (btor_id, sort_id, module)
    
    for base, (id1, sort1, mod) in copy1_by_base.items():
        if base in copy2_by_base:
            id2, sort2, _ = copy2_by_base[base]
            assert sort1 == sort2
            pairs_by_module[mod].append((id1, id2, sort1, base))
    
    return lines, pairs_by_module


def substitute_line(parts, subst, op):
    """Substitute node ID references in a BTOR2 line, respecting op semantics."""
    new_parts = list(parts)
    
    id_positions = BTOR2_ID_ARGS.get(op)
    if id_positions is None:
        # Unknown op — be conservative, don't substitute
        return new_parts
    
    # Arguments start at index 3 (after: id op sort)
    # But for init/next, the format is: id op sort state_id value
    # In BTOR2_ID_ARGS, positions are relative to after the sort field
    base_idx = 3  # id=0, op=1, sort=2, args start at 3
    
    for pos in id_positions:
        idx = base_idx + pos
        if idx >= len(new_parts):
            break
        if new_parts[idx].startswith(';'):
            break
        try:
            val = int(new_parts[idx])
            if val in subst:
                new_parts[idx] = str(subst[val])
        except ValueError:
            pass
    
    return new_parts


def generate_substituted_subproblem(lines, target_module, pairs_by_module, output_path):
    """Generate sub-problem using ID substitution instead of constraints."""
    # Build substitution map: copy2_id -> copy1_id (for NON-target modules)
    subst = {}
    for module, pairs in pairs_by_module.items():
        if module == target_module:
            continue
        for id1, id2, sort_id, base in pairs:
            subst[id2] = id1
    
    skip_state_ids = set(subst.keys())
    
    # Find lines to skip (state/init/next for substituted copy2 states)
    skip_lines = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        tag = parts[1]
        if tag == 'state':
            if int(parts[0]) in skip_state_ids:
                skip_lines.add(i)
        elif tag in ('init', 'next'):
            if int(parts[3]) in skip_state_ids:
                skip_lines.add(i)
    
    # Find bitvec 1 sort
    sort1 = None
    for line in lines:
        parts = line.strip().split()
        if len(parts) >= 4 and parts[1] == 'sort' and parts[2] == 'bitvec' and parts[3] == '1':
            sort1 = int(parts[0])
            break
    
    new_lines = []
    max_id = 0
    
    for i, line in enumerate(lines):
        if i in skip_lines:
            new_lines.append(f'; MERGED: {line.rstrip()}\n')
            continue
        
        stripped = line.strip()
        if not stripped or stripped.startswith(';'):
            new_lines.append(line)
            continue
        
        parts = stripped.split()
        op = parts[1] if len(parts) > 1 else ''
        
        if op == 'bad':
            new_lines.append(f'; REMOVED: {stripped}\n')
            continue
        
        new_parts = substitute_line(parts, subst, op)
        new_lines.append(' '.join(new_parts) + '\n')
        
        try:
            max_id = max(max_id, int(parts[0]))
        except ValueError:
            pass
    
    # Add property
    next_id = max_id + 1
    target_pairs = pairs_by_module[target_module]
    new_lines.append(f'\n; === Property: module {target_module} states equal ===\n')
    
    eq_ids = []
    for id1, id2, sort_id, base in target_pairs:
        eq_id = next_id; next_id += 1
        new_lines.append(f'{eq_id} eq {sort1} {id1} {id2} ; check {base}\n')
        eq_ids.append(eq_id)
    
    all_eq = eq_ids[0]
    for eid in eq_ids[1:]:
        and_id = next_id; next_id += 1
        new_lines.append(f'{and_id} and {sort1} {all_eq} {eid}\n')
        all_eq = and_id
    
    not_id = next_id; next_id += 1
    new_lines.append(f'{not_id} not {sort1} {all_eq}\n')
    bad_id = next_id; next_id += 1
    new_lines.append(f'{bad_id} bad {not_id} ; module {target_module}\n')
    
    with open(output_path, 'w') as f:
        f.writelines(new_lines)
    
    return len(subst)


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <input.btor2> <output_dir> <target_module> [depth]")
        sys.exit(1)
    
    btor2_path = sys.argv[1]
    output_dir = sys.argv[2]
    target_module = sys.argv[3]
    depth = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    os.makedirs(output_dir, exist_ok=True)
    
    lines, pairs_by_module = parse_btor2(btor2_path, depth)
    
    if target_module not in pairs_by_module:
        print(f"Module '{target_module}' not found. Available: {sorted(pairs_by_module.keys())}")
        sys.exit(1)
    
    output_path = os.path.join(output_dir, f'subst_{target_module}.btor2')
    n_subst = generate_substituted_subproblem(lines, target_module, pairs_by_module, output_path)
    
    target_pairs = len(pairs_by_module[target_module])
    print(f"Target: {target_module} ({target_pairs} pairs)")
    print(f"Substituted: {n_subst} copy2 state IDs replaced with copy1")
    print(f"Output: {output_path}")


if __name__ == '__main__':
    main()
