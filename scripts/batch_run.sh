#!/bin/bash

# Batch run agent and test patches in parallel
# Usage: ./scripts/batch_run.sh

# 10 tasks with smallest patches that have run_poc.sh (26-33 lines)
TASKS=(
    "fluent-bit/arvo_27279"    # 26 lines
    "fluent-bit/arvo_27025"    # 27 lines
    "fluent-bit/arvo_26593"    # 28 lines
    "fluent-bit/arvo_28265"    # 29 lines
    "fluent-bit/arvo_51132"    # 29 lines
    "fluent-bit/arvo_27710"    # 30 lines
    "fluent-bit/arvo_30090"    # 30 lines
    "fluent-bit/arvo_33750"    # 33 lines
    "fluent-bit/arvo_34116"    # 33 lines
    "fluent-bit/arvo_46082"    # 33 lines
)

TOTAL=${#TASKS[@]}
RESULTS_DIR="batch_results_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

START_TIME=$(date +%s)
echo "Starting batch run at $(date)"
echo "Results directory: $RESULTS_DIR"
echo "Processing $TOTAL tasks in parallel..."
echo ""

# Function to run a single task
run_task() {
    local task="$1"
    local task_id="$2"
    local task_name=$(echo "$task" | tr '/' '_')
    local log_file="$RESULTS_DIR/${task_name}.log"
    local result_file="$RESULTS_DIR/${task_name}.result"

    local task_start=$(date +%s)
    echo "[$task_id/$TOTAL] Starting: $task" | tee -a "$log_file"

    # Run agent
    local agent_start=$(date +%s)
    python3 scripts/run_agent.py --run-prepare --run-cleanup "$task" >> "$log_file" 2>&1
    local agent_status=$?
    local agent_end=$(date +%s)
    local agent_duration=$((agent_end - agent_start))

    if [ $agent_status -ne 0 ]; then
        echo "AGENT_FAILED|$task|$agent_duration|0|0" > "$result_file"
        echo "[$task_id/$TOTAL] Agent FAILED: $task (${agent_duration}s)" | tee -a "$log_file"
        return
    fi

    # Find patch file
    local latest_run=$(ls -t "agent_output/$task_name" 2>/dev/null | head -1)
    local patch_file="agent_output/$task_name/$latest_run/output/fix.patch"

    if [ ! -f "$patch_file" ]; then
        echo "AGENT_NO_PATCH|$task|$agent_duration|0|0" > "$result_file"
        echo "[$task_id/$TOTAL] No patch: $task (${agent_duration}s)" | tee -a "$log_file"
        return
    fi

    # Test patch
    local test_start=$(date +%s)
    python3 scripts/validate.py --run-prepare --run-cleanup --patch-file "$patch_file" "$task" >> "$log_file" 2>&1
    local test_status=$?
    local test_end=$(date +%s)
    local test_duration=$((test_end - test_start))

    local total_duration=$((test_end - task_start))

    if [ $test_status -eq 0 ]; then
        echo "SUCCESS|$task|$agent_duration|$test_duration|$total_duration" > "$result_file"
        echo "[$task_id/$TOTAL] SUCCESS: $task (agent: ${agent_duration}s, test: ${test_duration}s)" | tee -a "$log_file"
    else
        echo "TEST_FAILED|$task|$agent_duration|$test_duration|$total_duration" > "$result_file"
        echo "[$task_id/$TOTAL] Test FAILED: $task (agent: ${agent_duration}s, test: ${test_duration}s)" | tee -a "$log_file"
    fi
}

# Limit concurrent tasks to avoid Bedrock rate limiting
# Claude Opus 4.5 has strict token quotas
MAX_CONCURRENT=${MAX_CONCURRENT:-2}
STAGGER_DELAY=${STAGGER_DELAY:-10}  # seconds between launches

echo "Running with max $MAX_CONCURRENT concurrent tasks..."
echo ""

PIDS=()
for i in "${!TASKS[@]}"; do
    # Wait if we've reached max concurrent
    while [ ${#PIDS[@]} -ge $MAX_CONCURRENT ]; do
        # Remove finished PIDs
        NEW_PIDS=()
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                NEW_PIDS+=("$pid")
            fi
        done
        PIDS=("${NEW_PIDS[@]}")

        if [ ${#PIDS[@]} -ge $MAX_CONCURRENT ]; then
            sleep 10
        fi
    done

    run_task "${TASKS[$i]}" "$((i+1))" &
    PIDS+=($!)
    echo "  Started task $((i+1))/${#TASKS[@]}, ${#PIDS[@]} running..."

    # Small delay between launches
    sleep $STAGGER_DELAY
done

echo "All tasks launched. Waiting for remaining to complete..."
echo ""

# Wait for all remaining tasks
for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null
done

END_TIME=$(date +%s)
TOTAL_DURATION=$((END_TIME - START_TIME))

echo ""
echo "========================================"
echo "BATCH COMPLETE"
echo "========================================"
echo "Total time: $((TOTAL_DURATION / 60)) min $((TOTAL_DURATION % 60)) sec"
echo ""

# Collect and display results
AGENT_SUCCESS=0
TEST_SUCCESS=0
AGENT_FAILED=0

echo "Results:"
echo "--------"
for task in "${TASKS[@]}"; do
    task_name=$(echo "$task" | tr '/' '_')
    result_file="$RESULTS_DIR/${task_name}.result"

    if [ -f "$result_file" ]; then
        result=$(cat "$result_file")
        status=$(echo "$result" | cut -d'|' -f1)
        agent_time=$(echo "$result" | cut -d'|' -f3)
        test_time=$(echo "$result" | cut -d'|' -f4)

        case "$status" in
            SUCCESS)
                echo "✓ $task - FIXED (agent: ${agent_time}s, test: ${test_time}s)"
                AGENT_SUCCESS=$((AGENT_SUCCESS + 1))
                TEST_SUCCESS=$((TEST_SUCCESS + 1))
                ;;
            TEST_FAILED)
                echo "✗ $task - patch didn't fix (agent: ${agent_time}s, test: ${test_time}s)"
                AGENT_SUCCESS=$((AGENT_SUCCESS + 1))
                ;;
            AGENT_FAILED)
                echo "✗ $task - agent failed (${agent_time}s)"
                AGENT_FAILED=$((AGENT_FAILED + 1))
                ;;
            AGENT_NO_PATCH)
                echo "✗ $task - no patch generated (${agent_time}s)"
                AGENT_FAILED=$((AGENT_FAILED + 1))
                ;;
        esac
    else
        echo "? $task - no result file"
    fi
done

echo ""
echo "========================================"
echo "SUMMARY"
echo "========================================"
echo "Total tasks:    $TOTAL"
echo "Agent success:  $AGENT_SUCCESS/$TOTAL"
echo "Tests passed:   $TEST_SUCCESS/$TOTAL"
echo "Total time:     $((TOTAL_DURATION / 60)) min $((TOTAL_DURATION % 60)) sec"
echo "Results dir:    $RESULTS_DIR"
echo "========================================"
