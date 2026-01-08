#!/bin/bash

# Batch run agent on multiple tasks in parallel
# Usage: ./scripts/batch_run.sh --tasks FILE [OPTIONS]
#
# Options:
#   --tasks FILE              Task list file (required)
#   --mode patch-only|e2e     Mode (default: e2e)
#   --max-attempts N          Retry attempts (default: 3)
#   --max-parallel N          Parallel jobs (default: 2)
#   --model MODEL_ID          Bedrock model ID
#   --aws-profile PROFILE     AWS profile for credentials (default: bedrock-profile)
#   --slice-context-dir DIR   Pre-computed code slices (default: /tmp/slice_contexts)
#   --no-slice-context        Disable slice context
#   --stop                    Kill all running batch processes and containers
#
# Examples:
#   ./scripts/batch_run.sh --tasks scripts/tasks_30.txt --max-parallel 4
#   ./scripts/batch_run.sh --tasks scripts/tasks_30.txt --aws-profile default
#   ./scripts/batch_run.sh --stop   # Kill all running batch jobs

# Handle --stop first (before other parsing)
if [[ "$1" == "--stop" ]]; then
    echo "Stopping all batch_run processes..."

    # Kill batch_run.sh processes (except this one)
    pkill -9 -f "batch_run.sh --tasks" 2>/dev/null

    # Kill run_agent.py processes
    pkill -9 -f "run_agent.py" 2>/dev/null

    # Kill docker exec processes
    pkill -9 -f "docker exec" 2>/dev/null

    # Stop all running containers
    CONTAINERS=$(docker ps -q 2>/dev/null)
    if [ -n "$CONTAINERS" ]; then
        echo "Stopping $(echo "$CONTAINERS" | wc -l) containers..."
        docker kill $CONTAINERS 2>/dev/null
    fi

    # Clean up temp files
    rm -f /tmp/batch_*.log /tmp/batch_*.rundir 2>/dev/null

    echo "Done. All batch processes stopped."
    exit 0
fi

# Defaults
MODE=${MODE:-"e2e"}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
MAX_PARALLEL=${MAX_PARALLEL:-2}
STAGGER_DELAY=${STAGGER_DELAY:-10}
MODEL_ID=${MODEL_ID:-"us.anthropic.claude-sonnet-4-5-20250929-v1:0"}
AWS_PROFILE_ARG=${AWS_PROFILE:-"bedrock-profile"}
SLICE_CONTEXT_DIR=${SLICE_CONTEXT_DIR:-"/tmp/slice_contexts"}
TASK_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --tasks) TASK_FILE="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --max-attempts) MAX_ATTEMPTS="$2"; shift 2 ;;
        --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
        --model) MODEL_ID="$2"; shift 2 ;;
        --aws-profile) AWS_PROFILE_ARG="$2"; shift 2 ;;
        --slice-context-dir) SLICE_CONTEXT_DIR="$2"; shift 2 ;;
        --no-slice-context) SLICE_CONTEXT_DIR=""; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Load tasks from file or use default
TASKS=()
if [ -n "$TASK_FILE" ]; then
    if [ ! -f "$TASK_FILE" ]; then
        echo "ERROR: Task file not found: $TASK_FILE"
        exit 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        TASKS+=("$line")
    done < "$TASK_FILE"
    echo "Loaded ${#TASKS[@]} tasks from $TASK_FILE"
else
    echo "ERROR: No task file specified. Use --tasks FILE"
    echo "Example: $0 --tasks scripts/all_tasks.txt"
    exit 1
fi

if [ ${#TASKS[@]} -eq 0 ]; then
    echo "ERROR: No tasks to run"
    exit 1
fi

echo "========================================"
echo "Mode: $MODE | Attempts: $MAX_ATTEMPTS | Parallel: $MAX_PARALLEL"
echo "Model: $MODEL_ID"
echo "AWS Profile: $AWS_PROFILE_ARG"
echo "Slice context: ${SLICE_CONTEXT_DIR:-disabled}"
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
    SLICE_ARG=""
    if [ -n "$SLICE_CONTEXT_DIR" ]; then
        SLICE_ARG="--slice-context-dir $SLICE_CONTEXT_DIR"
    fi

    python3 scripts/run_agent.py \
        --mode "$MODE" \
        --max-attempts "$MAX_ATTEMPTS" \
        --bedrock-model-id "$MODEL_ID" \
        --aws-profile "$AWS_PROFILE_ARG" \
        $SLICE_ARG \
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
