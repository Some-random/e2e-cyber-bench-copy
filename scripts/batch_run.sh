#!/bin/bash

# Batch run agent on multiple tasks
# Usage: ./scripts/batch_run.sh [--mode patch-only|e2e] [--max-attempts N]

TASKS=(
    "capstone/arvo_13466"
    "capstone/arvo_13467"
    "capstone/arvo_14912"
    "capstone/arvo_58666"
    "curl/arvo_66012"
    "faad2/arvo_58287"
    "flatbuffers/arvo_46883"
    "fluent-bit/arvo_26325"
    "fluent-bit/arvo_26327"
    "fluent-bit/arvo_26345"
    "fluent-bit/arvo_26593"
    "fluent-bit/arvo_27025"
    "fluent-bit/arvo_27241"
    "fluent-bit/arvo_27279"
    "fluent-bit/arvo_27710"
    "fluent-bit/arvo_28265"
    "fluent-bit/arvo_30090"
    "fluent-bit/arvo_33750"
    "fluent-bit/arvo_34116"
    "fluent-bit/arvo_45879"
    "fluent-bit/arvo_46082"
    "fluent-bit/arvo_51132"
    "hiredis/arvo_28777"
    "libidn2/arvo_12420"
    "libpcap/arvo_48863"
    "libssh2/arvo_29769"
    "libssh2/arvo_65212"
    "md4c/arvo_31332"
    "skcms/arvo_6521"
    "wasm3/arvo_33318"
)

# Defaults
MODE=${MODE:-"e2e"}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
MAX_PARALLEL=${MAX_PARALLEL:-2}
MAX_RETRIES=${MAX_RETRIES:-2}
RETRY_DELAY=${RETRY_DELAY:-60}
STAGGER_DELAY=${STAGGER_DELAY:-10}
MODEL_ID=${MODEL_ID:-"us.anthropic.claude-sonnet-4-5-20250929-v1:0"}

while [[ $# -gt 0 ]]; do
    case $1 in
        --mode) MODE="$2"; shift 2 ;;
        --max-attempts) MAX_ATTEMPTS="$2"; shift 2 ;;
        --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
        --model) MODEL_ID="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "========================================"
echo "Mode: $MODE | Attempts: $MAX_ATTEMPTS | Parallel: $MAX_PARALLEL"
echo "Model: $MODEL_ID"
echo "Tasks: ${#TASKS[@]}"
echo "========================================"

START_TIME=$(date +%s)
BATCH_ID="$$_$(date +%s)"

run_task() {
    local task=$1
    local task_name=$(echo "$task" | tr '/' '_')
    local tmp_log="/tmp/batch_${BATCH_ID}_${task_name}.log"
    local run_dir_file="/tmp/batch_${BATCH_ID}_${task_name}.rundir"
    echo "[$task] Starting..."

    # Run and capture the run directory from output
    python3 scripts/run_agent.py \
        --mode "$MODE" \
        --max-attempts "$MAX_ATTEMPTS" \
        --bedrock-model-id "$MODEL_ID" \
        "$task" > "$tmp_log" 2>&1

    # Extract run directory from the output (look for "Output:" line)
    local run_dir=$(grep "^Output:" "$tmp_log" | head -1 | sed 's/Output: //' | xargs dirname)
    if [ -z "$run_dir" ] || [ ! -d "$run_dir" ]; then
        # Fallback to latest directory
        run_dir=$(ls -td agent_output/${task_name}/*/ 2>/dev/null | head -1)
    fi

    if [ -n "$run_dir" ] && [ -d "$run_dir" ]; then
        mv "$tmp_log" "${run_dir}/run.log"
        # Save run_dir for final summary
        echo "$run_dir" > "$run_dir_file"
    fi

    local summary_file="${run_dir}/summary.json"
    if [ -f "$summary_file" ]; then
        python3 -c "
import json
with open('$summary_file') as f:
    d = json.load(f)
status = d.get('status', 'error')
duration = d.get('duration_minutes', 0)
attempts = d.get('attempts', [])
parts = []
for a in attempts:
    s1 = a.get('stage1', '-')
    s2 = a.get('stage2', '-')
    s3 = a.get('stage3', '-')
    parts.append(f'A{a[\"attempt\"]}[{s1}/{s2}/{s3}]')
print(f'[$task] {status.upper()} ({duration:.1f}m) {\" \".join(parts)}')
print(f'  Logs: ${run_dir}/run.log')
"
    else
        echo "[$task] NO RESULT (log: $tmp_log)"
    fi
}

# Run tasks in parallel
echo ""
PIDS=()
for i in "${!TASKS[@]}"; do
    while [ ${#PIDS[@]} -ge $MAX_PARALLEL ]; do
        NEW_PIDS=()
        for pid in "${PIDS[@]}"; do
            kill -0 "$pid" 2>/dev/null && NEW_PIDS+=("$pid")
        done
        PIDS=("${NEW_PIDS[@]}")
        [ ${#PIDS[@]} -ge $MAX_PARALLEL ] && sleep 5
    done

    run_task "${TASKS[$i]}" &
    PIDS+=($!)
    sleep $STAGGER_DELAY
done

for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null; done

# Display results by reading from saved run directories
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo "=== RESULTS ==="
success=0; failed=0; errors=0

for task in "${TASKS[@]}"; do
    task_name=$(echo "$task" | tr '/' '_')
    run_dir_file="/tmp/batch_${BATCH_ID}_${task_name}.rundir"

    # Use saved run directory, fallback to latest
    if [ -f "$run_dir_file" ]; then
        run_dir=$(cat "$run_dir_file")
        rm -f "$run_dir_file"
    else
        run_dir=$(ls -td agent_output/${task_name}/*/ 2>/dev/null | head -1)
    fi

    summary_file="${run_dir}/summary.json"

    if [ -f "$summary_file" ]; then
        python3 << EOF
import json
with open('$summary_file') as f:
    d = json.load(f)
status = d.get('status', 'error')
duration = d.get('duration_minutes', 0)
attempts = d.get('attempts', [])

print(f"$task: {status.upper()} ({duration:.1f}m)")
for a in attempts:
    parts = []
    if a.get('stage1') is not None:
        parts.append(f"S1={a['stage1']}")
    if a.get('stage2') is not None:
        parts.append(f"S2={a['stage2']}")
    parts.append(f"S3={a.get('stage3', 'N/A')}")
    result = "SUCCESS" if a.get('success') else "FAILED"
    print(f"  Attempt {a['attempt']}: {' '.join(parts)} -> {result}")
EOF
        status=$(python3 -c "import json; print(json.load(open('$summary_file'))['status'])")
        case "$status" in
            success) success=$((success + 1)) ;;
            failed) failed=$((failed + 1)) ;;
            *) errors=$((errors + 1)) ;;
        esac
    else
        echo "$task: NO RESULT"
        errors=$((errors + 1))
    fi
done

echo ""
echo "=== SUMMARY ==="
echo "Total: ${#TASKS[@]} | Success: $success | Failed: $failed | Error: $errors"
echo "Duration: $((DURATION / 60))m $((DURATION % 60))s"
