#!/bin/bash
# Run pono IC3IA on HWMCC25 word-level BV benchmarks
# Usage: ./scripts/run_hwmcc25.sh [timeout_seconds] [num_parallel]

PONO="$(dirname "$0")/../build/pono"
BENCH_DIR="/local/home/yzarg/hwmcc25/wordlevel/bv"
TIMEOUT="${1:-300}"   # default 300s per benchmark
PARALLEL="${2:-8}"    # default 8 parallel jobs
RESULTS_DIR="/local/home/yzarg/hwmcc25/pono_results"
mkdir -p "$RESULTS_DIR"

RESULTS_FILE="$RESULTS_DIR/ic3ia_results.csv"
echo "benchmark,result,time_seconds,exit_code" > "$RESULTS_FILE"

LOCK_FILE="$RESULTS_DIR/.lock"

run_one() {
    local btor2="$1"
    local rel_path="${btor2#$BENCH_DIR/}"
    local safe_name=$(echo "$rel_path" | tr '/' '_' | sed 's/.btor2$//')
    local log_file="$RESULTS_DIR/${safe_name}.log"

    local start_time=$(date +%s.%N)
    timeout "$TIMEOUT" "$PONO" -e ic3ia --smt-solver cvc5 -k 1000 "$btor2" > "$log_file" 2>&1
    local exit_code=$?
    local end_time=$(date +%s.%N)
    local elapsed=$(echo "$end_time - $start_time" | bc)

    # Parse result from output
    local result="unknown"
    if grep -q "^unsat" "$log_file" 2>/dev/null; then
        result="unsat"
    elif grep -q "^sat" "$log_file" 2>/dev/null; then
        result="sat"
    elif [ $exit_code -eq 124 ]; then
        result="timeout"
    else
        result="error"
    fi

    # Thread-safe write
    (
        flock -x 200
        echo "$rel_path,$result,$elapsed,$exit_code" >> "$RESULTS_FILE"
    ) 200>"$LOCK_FILE"

    echo "  $result  ${elapsed}s  $rel_path"
}

export -f run_one
export PONO BENCH_DIR TIMEOUT RESULTS_DIR RESULTS_FILE LOCK_FILE

echo "=== Pono IC3IA on HWMCC25 Word-Level BV ==="
echo "Timeout: ${TIMEOUT}s, Parallel: ${PARALLEL}"
echo "Results: $RESULTS_FILE"
echo ""

BENCHMARKS=$(find "$BENCH_DIR" -name "*.btor2" | sort)
TOTAL=$(echo "$BENCHMARKS" | wc -l)
echo "Total benchmarks: $TOTAL"
echo ""

echo "$BENCHMARKS" | xargs -P "$PARALLEL" -I{} bash -c 'run_one "{}"'

echo ""
echo "=== Summary ==="
echo "Total: $(tail -n +2 "$RESULTS_FILE" | wc -l)"
echo "Unsat: $(grep -c ',unsat,' "$RESULTS_FILE")"
echo "Sat:   $(grep -c ',sat,' "$RESULTS_FILE")"
echo "Timeout: $(grep -c ',timeout,' "$RESULTS_FILE")"
echo "Error: $(grep -c ',error,' "$RESULTS_FILE")"
echo ""
echo "Results saved to: $RESULTS_FILE"