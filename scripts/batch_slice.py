#!/usr/bin/env python3
"""Batch run slicing on all tasks in parallel with progress tracking."""

import subprocess
import shutil
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = Path(__file__).parent
TASKS_FILE = SCRIPT_DIR / "tasks_30_small.txt"
OUTPUT_DIR = Path("/tmp/codeql_slices")
CONTEXT_DIR = Path("/tmp/slice_contexts")

MAX_WORKERS = 8  # Increased for faster execution


def run_single_task(task):
    """Run slicing for a single task. Returns (task, status, size_kb, time_s, error)."""
    start = time.time()
    try:
        result = subprocess.run(
            ["python3", str(SCRIPT_DIR / "build_codeql_in_docker.py"),
             "--task", task,
             "--output-dir", str(OUTPUT_DIR)],
            capture_output=True,
            text=True,
            timeout=1800
        )

        task_dir = OUTPUT_DIR / task.replace("/", "_")
        context_file = task_dir / "context.txt"
        elapsed = time.time() - start

        if context_file.exists():
            dest = CONTEXT_DIR / f"{task.replace('/', '_')}.txt"
            shutil.copy(context_file, dest)
            size_kb = context_file.stat().st_size / 1024
            return (task, "SUCCESS", size_kb, elapsed, "")
        else:
            error = result.stdout[-500:] + result.stderr[-500:]
            return (task, "FAILED", 0, elapsed, error)

    except subprocess.TimeoutExpired:
        return (task, "TIMEOUT", 0, time.time() - start, "")
    except Exception as e:
        return (task, "ERROR", 0, time.time() - start, str(e))


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_WORKERS

    tasks = [t.strip() for t in TASKS_FILE.read_text().splitlines() if t.strip()]

    OUTPUT_DIR.mkdir(exist_ok=True)
    CONTEXT_DIR.mkdir(exist_ok=True)

    print(f"Running {len(tasks)} tasks with {workers} parallel workers")
    print(f"Output: {CONTEXT_DIR}")
    print("=" * 70)

    results = []
    completed = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {executor.submit(run_single_task, task): task for task in tasks}

        for future in as_completed(future_to_task):
            task, status, size_kb, elapsed, error = future.result()
            results.append((task, status, size_kb, elapsed))
            completed += 1

            if status == "SUCCESS":
                print(f"[{completed:2d}/{len(tasks)}] ✓ {task}: {size_kb:.1f}KB ({elapsed:.0f}s)")
            else:
                print(f"[{completed:2d}/{len(tasks)}] ✗ {task}: {status} ({elapsed:.0f}s)")
                if error and 'error' in error.lower():
                    for line in error.split('\n'):
                        if 'error' in line.lower():
                            print(f"         {line.strip()[:70]}")
                            break

    total_time = time.time() - start_time

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    success = [(t, sz) for t, s, sz, _ in results if s == "SUCCESS"]
    failed = [(t, s) for t, s, _, _ in results if s != "SUCCESS"]

    print(f"Success: {len(success)}/{len(tasks)} ({len(success)/len(tasks)*100:.0f}%)")
    print(f"Total time: {total_time:.0f}s ({total_time/60:.1f}m)")
    print(f"Context files: {CONTEXT_DIR}")

    if success:
        sizes = [sz for _, sz in success]
        print(f"\nSize stats: min={min(sizes):.1f}KB, max={max(sizes):.1f}KB, avg={sum(sizes)/len(sizes):.1f}KB")
        print("\nSuccessful tasks:")
        for task, size_kb in sorted(success):
            print(f"  {task}: {size_kb:.1f}KB")

    if failed:
        print("\nFailed tasks:")
        for task, status in sorted(failed):
            print(f"  {task}: {status}")


if __name__ == "__main__":
    main()
