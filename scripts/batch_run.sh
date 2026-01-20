#!/bin/bash
# Unified batch runner for e2e-cyber-bench agents
#
# Usage:
#   bash scripts/batch_run.sh [tasks_file] [max_parallel] [OPTIONS]
#
# Options (via environment variables or positional args):
#   AGENT=claude-code|openhands    Agent backend (default: claude-code)
#   MODE=e2e|patch-only            Mode (default: e2e)
#   MAX_PARALLEL=N                 Parallel jobs (default: 4)
#   MAX_ATTEMPTS=N                 Retry attempts (default: 1)
#   BEDROCK_MODEL_ID=...           Model ID
#   AWS_PROFILE=...                AWS profile (default: bedrock-profile1)
#   AWS_REGION=...                 AWS region (default: us-west-2)
#   AGENT_OUTPUT_DIR=...           Output directory
#   TIMEOUT=N                      Agent timeout in seconds (default: 5400)
#
# Examples:
#   # Claude Code (default)
#   bash scripts/batch_run.sh scripts/tasks_30.txt 50
#
#   # OpenHands
#   AGENT=openhands bash scripts/batch_run.sh scripts/tasks_30.txt 4
#
#   # With custom settings
#   AWS_PROFILE=my-profile MAX_ATTEMPTS=3 bash scripts/batch_run.sh tasks.txt
#
#   # Stop all running jobs
#   bash scripts/batch_run.sh --stop

set -euo pipefail

# Handle --stop first
if [[ "${1:-}" == "--stop" ]]; then
    echo "Stopping all batch processes..."

    # Kill batch_run.sh processes
    pkill -9 -f "batch_run.sh" 2>/dev/null || true

    # Kill run_agent.py processes
    pkill -9 -f "run_agent.py" 2>/dev/null || true

    # Kill Docker containers
    docker ps -q --filter "name=claude-agent" 2>/dev/null | xargs -r docker kill 2>/dev/null || true
    docker ps -q 2>/dev/null | head -20 | xargs -r docker kill 2>/dev/null || true

    echo "Done."
    exit 0
fi

# Configuration
TASKS_FILE="${1:-scripts/tasks_30.txt}"
MAX_PARALLEL="${2:-${MAX_PARALLEL:-4}}"
MAX_ATTEMPTS="${3:-${MAX_ATTEMPTS:-1}}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
MODE="${MODE:-e2e}"
AGENT="${AGENT:-claude-code}"
AWS_PROFILE="${AWS_PROFILE:-bedrock-profile1}"
AWS_REGION="${AWS_REGION:-us-west-2}"
TIMEOUT="${TIMEOUT:-5400}"

# Set output directory based on agent if not specified
if [ -z "${AGENT_OUTPUT_DIR:-}" ]; then
    if [ "$AGENT" = "claude-code" ]; then
        AGENT_OUTPUT_DIR="agent_output_claude"
    else
        AGENT_OUTPUT_DIR="agent_output"
    fi
fi

# Set prompt style based on agent
if [ "$AGENT" = "openhands" ]; then
    PROMPT_STYLE="no-test"
else
    PROMPT_STYLE="iterative"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Cleanup function
CLEANUP_DONE=0
cleanup() {
    if [ "$CLEANUP_DONE" = "1" ]; then
        return
    fi
    CLEANUP_DONE=1

    echo ""
    echo "[$(date +%H:%M:%S)] Received shutdown signal, cleaning up..."

    # Kill all child processes
    pkill -P $$ 2>/dev/null || true

    # Kill run_agent processes started by us
    pkill -f "run_agent.py.*--aws-profile $AWS_PROFILE" 2>/dev/null || true

    # Kill Docker containers
    docker ps -q --filter "name=claude-agent" 2>/dev/null | xargs -r docker kill 2>/dev/null || true

    echo "[$(date +%H:%M:%S)] Cleanup complete"
    exit 130
}

trap cleanup SIGTERM SIGINT SIGHUP

# Validate task file
if [ ! -f "$TASKS_FILE" ]; then
    echo "ERROR: Task file not found: $TASKS_FILE"
    exit 1
fi

TOTAL=$(wc -l < "$TASKS_FILE" | tr -d ' ')

echo "=========================================="
echo "Batch Runner"
echo "=========================================="
echo "Agent: $AGENT"
echo "Prompt style: $PROMPT_STYLE"
echo "Tasks file: $TASKS_FILE ($TOTAL tasks)"
echo "Max parallel: $MAX_PARALLEL"
echo "Max attempts: $MAX_ATTEMPTS"
echo "Timeout: ${TIMEOUT}s ($((TIMEOUT/60))m)"
echo "Model: $BEDROCK_MODEL_ID"
echo "Mode: $MODE"
echo "AWS Profile: $AWS_PROFILE"
echo "Output dir: $AGENT_OUTPUT_DIR"
echo "=========================================="
echo ""

# Function to run a single task
run_task() {
    local task="$1"
    local task_safe="${task//\//_}"

    echo "[$(date +%H:%M:%S)] Starting: $task"

    # Run agent
    local output
    output=$(python3 "$SCRIPT_DIR/run_agent.py" "$task" \
        --agent "$AGENT" \
        --prompt-style "$PROMPT_STYLE" \
        --mode "$MODE" \
        --max-attempts "$MAX_ATTEMPTS" \
        --timeout "$TIMEOUT" \
        --aws-profile "$AWS_PROFILE" \
        --aws-region "$AWS_REGION" \
        --bedrock-model-id "$BEDROCK_MODEL_ID" \
        --agent-output "$AGENT_OUTPUT_DIR" 2>&1) || true

    local exit_code=$?

    # Extract result from output
    if echo "$output" | grep -q 'Status: SUCCESS'; then
        echo "[$(date +%H:%M:%S)] SUCCESS: $task"
    else
        echo "[$(date +%H:%M:%S)] FAILED:  $task"
    fi

    # Show stage summary
    echo "$output" | grep -E "Attempt [0-9]+: S[0-9]" | sed 's/^/    /' || true

    return $exit_code
}

export -f run_task
export SCRIPT_DIR AGENT_OUTPUT_DIR MODE MAX_ATTEMPTS AWS_PROFILE AWS_REGION BEDROCK_MODEL_ID AGENT PROMPT_STYLE TIMEOUT

# Run tasks in parallel
echo "Starting parallel execution..."
echo ""

START_TIME=$(date +%s)

cat "$TASKS_FILE" | xargs -P "$MAX_PARALLEL" -I {} bash -c 'run_task "$@"' _ {}

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
echo "=========================================="
echo "Batch complete! (${DURATION}s / $((DURATION/60))m)"
echo "=========================================="

# Summarize results
python3 -c "
import json
import os

tasks_file = '$TASKS_FILE'
output_dir = '$AGENT_OUTPUT_DIR'

# Read task list
with open(tasks_file) as f:
    tasks = [line.strip() for line in f if line.strip() and not line.startswith('#')]

success = 0
failed = 0
partial = 0

print('')
print('=== RESULTS ===')

for task in tasks:
    task_safe = task.replace('/', '_')
    task_dir = os.path.join(output_dir, task_safe)

    status = 'NO_RESULT'
    details = ''

    if os.path.exists(task_dir):
        runs = sorted([d for d in os.listdir(task_dir) if os.path.isdir(os.path.join(task_dir, d))], reverse=True)
        if runs:
            summary_path = os.path.join(task_dir, runs[0], 'summary.json')
            if os.path.exists(summary_path):
                with open(summary_path) as f:
                    data = json.load(f)
                status = data.get('status', 'error').upper()
                duration = data.get('duration_minutes', 0)
                attempts = data.get('attempts', [])

                # Check for partial success (found A bug but not THE bug)
                for a in attempts:
                    if a.get('agent_success') and not a.get('gt_success'):
                        status = 'PARTIAL'
                        break

                # Build stage summary
                if attempts:
                    att = attempts[-1]
                    stages = []
                    for s in ['stage1', 'stage2', 'stage3', 'stage4']:
                        if att.get(s):
                            stages.append(f'{s[-1]}:{att[s][:1].upper()}')
                    details = f'({duration:.1f}m) [{\" \".join(stages)}]'

    if status == 'SUCCESS':
        success += 1
        print(f'  [OK] {task} {details}')
    elif status == 'PARTIAL':
        partial += 1
        print(f'  [~~] {task} {details} (found A bug)')
    else:
        failed += 1
        print(f'  [XX] {task} {details}')

print('')
print('=== SUMMARY ===')
print(f'Total: {len(tasks)} | Success: {success} | Partial: {partial} | Failed: {failed}')
print(f'Results in: {output_dir}')
"
