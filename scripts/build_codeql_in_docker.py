#!/usr/bin/env python3
"""
Build CodeQL database inside Docker, run program slicing on host.

This script reduces a full codebase (~100K+ lines) to just the code reachable
from the fuzzer entry point (~500-2000 lines), making it easier for an LLM
to analyze and find vulnerabilities.

Steps:
1. Build CodeQL database inside Docker (needs OSS-Fuzz build environment)
2. Copy database + source to host
3. Run CodeQL call graph query to find all reachable functions
4. Extract those functions into a single context.txt file
"""

import argparse
import subprocess
import shutil
import tempfile
import json
from pathlib import Path
from collections import defaultdict

from utils import start_container, cleanup_container, copy_to_container


SCRIPT_DIR = Path(__file__).parent
PROJ_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJ_DIR / "data" / "projects"
PROJECTS_DIR = PROJ_DIR / "projects"
CODEQL_DIR = PROJ_DIR / "codeql"
CODEQL_PATH = CODEQL_DIR / "codeql"
QLPACK_DIR = PROJ_DIR / "codeql_queries"

DEFAULT_BUILD_IMAGE = "gcr.io/oss-fuzz-base/base-builder:latest"

# Maximum depth for call graph traversal (to limit output size)
MAX_CALL_DEPTH = 3

# Maximum output size in bytes (150KB - reasonable for LLM context)
MAX_OUTPUT_SIZE = 150 * 1024

# Risky function patterns - prioritize these in output
RISKY_PATTERNS = [
    'memcpy', 'memmove', 'memset', 'strcpy', 'strncpy', 'strcat', 'strncat',
    'sprintf', 'snprintf', 'vsprintf', 'vsnprintf', 'sscanf', 'fscanf',
    'gets', 'fgets', 'read', 'fread', 'recv', 'recvfrom',
    'malloc', 'calloc', 'realloc', 'free', 'alloc',
    'parse', 'decode', 'deserialize', 'unpack', 'extract',
    'copy', 'move', 'write', 'put', 'set', 'append', 'insert',
    'buffer', 'buf', 'len', 'size', 'length', 'count', 'num', 'index', 'offset',
]

# CodeQL query: Find functions reachable within MAX_CALL_DEPTH levels.
# Query returns function name, file, start/end lines, and depth.
# Risky detection is done in Python based on function name and body.
CALL_GRAPH_QUERY = """
/**
 * @name Call graph from fuzzer entry point (depth-limited)
 * @kind table
 */
import cpp

// Direct call relationship
predicate directCall(Function caller, Function callee) {
  exists(FunctionCall fc |
    fc.getEnclosingFunction() = caller and
    fc.getTarget() = callee
  )
}

// Depth-limited reachability
predicate reachableAtDepth(Function entry, Function target, int depth) {
  depth = 1 and directCall(entry, target)
  or
  depth > 1 and depth <= MAX_DEPTH and
  exists(Function mid |
    reachableAtDepth(entry, mid, depth - 1) and
    directCall(mid, target)
  )
}

from Function entry, Function target, int depth
where
  entry.getName() = "LLVMFuzzerTestOneInput"
  and (
    (target = entry and depth = 0)
    or
    reachableAtDepth(entry, target, depth)
  )
select
  target.getName() as function_name,
  target.getFile().getAbsolutePath() as file_path,
  target.getLocation().getStartLine() as start_line,
  target.getBlock().getLocation().getEndLine() as end_line,
  depth
""".replace("MAX_DEPTH", str(MAX_CALL_DEPTH))


def exec_in_container(container_id, command, timeout=600):
    """Execute command in container without timeout wrapper (for CodeQL compatibility)."""
    cmd = ["docker", "exec", container_id, "bash", "-c", command]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def copy_from_container(container_id, src, dst):
    """Copy file/dir from container to host."""
    cmd = ["docker", "cp", f"{container_id}:{src}", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def run_codeql_query(db_path, query_text):
    """Run a CodeQL query and return results as list of dicts."""
    query_file = QLPACK_DIR / "_slice_query.ql"
    query_file.write_text(query_text)
    bqrs_file = None

    try:
        bqrs_file = tempfile.mktemp(suffix='.bqrs')
        cmd = [
            str(CODEQL_PATH), "query", "run",
            "--database", str(db_path),
            "--output", bqrs_file,
            str(query_file)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  CodeQL query failed: {result.stderr[:500]}")
            return []

        cmd = [str(CODEQL_PATH), "bqrs", "decode", "--format=json", bqrs_file]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  BQRS decode failed: {result.stderr[:500]}")
            return []

        data = json.loads(result.stdout)
        results = []
        if '#select' in data and 'tuples' in data['#select']:
            columns = data['#select'].get('columns', [])
            col_names = [c.get('name', f'col{i}') for i, c in enumerate(columns)]
            for row in data['#select']['tuples']:
                results.append(dict(zip(col_names, row)))
        return results

    finally:
        query_file.unlink(missing_ok=True)
        if bqrs_file:
            Path(bqrs_file).unlink(missing_ok=True)


def find_fuzzer_harness(src_dir):
    """
    Find the fuzzer harness file containing LLVMFuzzerTestOneInput.

    Returns (harness_path, harness_dir) or (None, None) if not found.

    The harness location varies by project:
    - fuzzing/foo_fuzzer.c
    - fuzz/fuzz_target.cpp
    - tests/fuzzer.cc
    - src/fuzz.cxx
    """
    src_dir = Path(src_dir)
    skip_dirs = {'codeql_db', '.git', 'aflplusplus', 'honggfuzz', 'libfuzzer', 'fuzztest'}

    for pattern in ['**/*.c', '**/*.cpp', '**/*.cc', '**/*.cxx']:
        for f in src_dir.glob(pattern):
            # Skip build artifacts and fuzzer engine directories
            if any(skip in f.parts for skip in skip_dirs):
                continue
            try:
                if 'LLVMFuzzerTestOneInput' in f.read_text():
                    return f, f.parent
            except:
                continue
    return None, None


def extract_function_lines(file_path, start_line):
    """
    Extract a function body starting from start_line using brace counting.

    CodeQL gives us the function declaration line, but we need to find where
    the function body ends by counting braces. This handles nested braces
    in the function body correctly.

    Returns (lines_with_numbers, actual_start, actual_end)
    """
    try:
        with open(file_path) as f:
            lines = f.readlines()

        # Start a few lines before for context (includes comments, annotations)
        context_before = 3
        start = max(0, start_line - context_before - 1)

        # Find function end by counting braces
        brace_count = 0
        found_open = False
        actual_end = start_line

        for i in range(start_line - 1, min(len(lines), start_line + 500)):
            line = lines[i]
            for ch in line:
                if ch == '{':
                    brace_count += 1
                    found_open = True
                elif ch == '}':
                    brace_count -= 1
            if found_open and brace_count == 0:
                actual_end = i + 1
                break

        # Format with line numbers
        numbered = [f"{i+1:4d}: {lines[i].rstrip()}" for i in range(start, actual_end)]
        return '\n'.join(numbered), start + 1, actual_end

    except Exception as e:
        return f"// Error reading {file_path}: {e}", start_line, start_line


def run_slicing(src_dir, db_path, output_file, path_remap_from, path_remap_to):
    """
    Run CodeQL-based program slicing with depth limit and risky function priority.

    1. Find the fuzzer harness
    2. Run CodeQL query to get reachable functions (depth-limited)
    3. Prioritize risky functions, limit total output size
    4. Write to output file
    """
    src_dir = Path(src_dir)

    harness, harness_dir = find_fuzzer_harness(src_dir)
    if not harness:
        print("  ERROR: Could not find LLVMFuzzerTestOneInput")
        return False

    print(f"  Found harness: {harness.relative_to(src_dir)}")

    print(f"  Running CodeQL call graph query (max depth={MAX_CALL_DEPTH})...")
    results = run_codeql_query(db_path, CALL_GRAPH_QUERY)

    # Process results - separate risky vs non-risky, apply path remapping
    risky_funcs = []
    normal_funcs = []

    for r in results:
        file_path = r.get('file_path', '')

        # Skip system headers
        if not file_path or file_path.startswith('/usr/'):
            continue

        # Remap container paths to host paths
        if path_remap_from and file_path.startswith(path_remap_from):
            file_path = path_remap_to + file_path[len(path_remap_from):]

        func_info = {
            'name': r.get('function_name', ''),
            'file': file_path,
            'start': r.get('start_line', 0),
            'depth': r.get('depth', 0),
            'is_risky': False,  # Will be set based on name/body patterns
        }

        # Check function name against RISKY_PATTERNS
        name_lower = func_info['name'].lower()
        if any(p in name_lower for p in RISKY_PATTERNS):
            func_info['is_risky'] = True
            risky_funcs.append(func_info)
        else:
            normal_funcs.append(func_info)

    # Sort: risky first (by depth), then normal (by depth)
    risky_funcs.sort(key=lambda x: x['depth'])
    normal_funcs.sort(key=lambda x: x['depth'])

    all_funcs = risky_funcs + normal_funcs
    print(f"  Found {len(all_funcs)} functions ({len(risky_funcs)} risky, {len(normal_funcs)} normal)")

    # Build output with size limit
    output = []
    output.append("=" * 70)
    output.append("SLICED CONTEXT FOR VULNERABILITY ANALYSIS")
    output.append("=" * 70)
    output.append(f"Source directory: {src_dir}")
    output.append(f"Fuzzer harness: {harness.relative_to(src_dir)}")
    output.append(f"Max call depth: {MAX_CALL_DEPTH}")
    output.append(f"Risky functions: {len(risky_funcs)}, Normal: {len(normal_funcs)}")
    output.append("")

    # Always include full harness first
    output.append("-" * 70)
    output.append(f"FILE: {harness.relative_to(src_dir)}")
    output.append("(FUZZER ENTRY POINT)")
    output.append("-" * 70)
    output.append(harness.read_text())
    output.append("")

    current_size = len('\n'.join(output))
    total_lines = 0
    included_funcs = 0
    skipped_funcs = 0
    seen = set()

    # Process functions in priority order (risky by depth, then normal by depth)
    # Extract function bodies and check size limit per-function
    included = []  # List of (func_info, body, start, end)

    for func in all_funcs:
        key = (func['file'], func['name'], func['start'])
        if key in seen:
            continue
        seen.add(key)

        # Skip harness file - already included
        if str(harness) == func['file']:
            continue

        body, start, end = extract_function_lines(func['file'], func['start'])
        if not body:
            continue

        # Estimate size of this function's output
        marker = " [RISKY]" if func['is_risky'] else ""
        func_header = f"\n// Lines {start}-{end}: {func['name']}{marker} (depth={func['depth']})\n"
        func_size = len(func_header) + len(body)

        # Check size limit
        if current_size + func_size > MAX_OUTPUT_SIZE:
            skipped_funcs += 1
            continue

        current_size += func_size
        total_lines += end - start
        included_funcs += 1
        included.append((func, body, start, end))

    # Group included functions by file for cleaner output
    funcs_by_file = defaultdict(list)
    for func, body, start, end in included:
        funcs_by_file[func['file']].append((func, body, start, end))

    # Output grouped by file
    for file_path in sorted(funcs_by_file.keys()):
        funcs = funcs_by_file[file_path]

        try:
            rel_path = Path(file_path).relative_to(src_dir)
        except ValueError:
            rel_path = Path(file_path).name

        output.append("")
        output.append("-" * 70)
        risky_names = [f['name'] for f, _, _, _ in funcs if f['is_risky']]
        normal_names = [f['name'] for f, _, _, _ in funcs if not f['is_risky']]
        output.append(f"FILE: {rel_path}")
        if risky_names:
            output.append(f"RISKY: {', '.join(risky_names)}")
        if normal_names:
            output.append(f"Other: {', '.join(normal_names)}")
        output.append("-" * 70)

        # Sort by line number within file
        for func, body, start, end in sorted(funcs, key=lambda x: x[2]):
            marker = " [RISKY]" if func['is_risky'] else ""
            output.append(f"\n// Lines {start}-{end}: {func['name']}{marker} (depth={func['depth']})")
            output.append(body)

    output.append("")
    output.append("=" * 70)
    output.append(f"END OF SLICED CONTEXT")
    output.append(f"Included: {included_funcs} functions, ~{total_lines} lines")
    if skipped_funcs > 0:
        output.append(f"Skipped: {skipped_funcs} functions (size limit {MAX_OUTPUT_SIZE//1024}KB)")
    output.append("=" * 70)

    Path(output_file).write_text('\n'.join(output))
    return True


def build_codeql_db(task, output_dir, build_image=DEFAULT_BUILD_IMAGE):
    """Build CodeQL database inside Docker, copy out to host."""
    project, issue = task.split("/")
    task_data = DATA_DIR / project / issue
    task_proj = PROJECTS_DIR / project / issue

    src_tgz = task_data / "src.tgz"
    if not src_tgz.exists():
        print(f"  ERROR: No source tarball at {src_tgz}")
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    container_id = None
    try:
        print("  Starting container...")
        container_id = start_container(build_image)
        print(f"  Container: {container_id[:12]}")

        # Copy and extract source
        copy_to_container(container_id, src_tgz, "/src/src.tgz")
        print("  Extracting source...")
        exec_in_container(container_id, "cd /src && tar xzf src.tgz && rm src.tgz")

        # Copy scripts and CodeQL
        copy_to_container(container_id, task_proj / "compile.sh", "/src/compile.sh")
        if (task_proj / "prepare.sh").exists():
            copy_to_container(container_id, task_proj / "prepare.sh", "/src/prepare.sh")
        copy_to_container(container_id, CODEQL_DIR, "/opt/codeql")

        # Run prepare.sh if exists (install dependencies)
        if (task_proj / "prepare.sh").exists():
            print("  Running prepare.sh...")
            code, stdout, stderr = exec_in_container(
                container_id,
                "export DEBIAN_FRONTEND=noninteractive && bash /src/prepare.sh",
                timeout=600
            )
            if code != 0:
                print(f"  WARNING: prepare.sh failed (continuing anyway): {stderr[-200:]}")

        # Run CodeQL build
        print("  Building CodeQL database...")
        build_cmd = """
export PATH=/opt/codeql:$PATH

# Find repo directory (where .git is, or where source files are)
REPO_DIR=$(find /src -maxdepth 2 -type d -name ".git" 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
if [ -z "$REPO_DIR" ]; then
    REPO_DIR=$(find /src -maxdepth 2 \\( -name "*.c" -o -name "*.cpp" -o -name "*.h" \\) 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
    [ -z "$REPO_DIR" ] && REPO_DIR="/src"
fi

/opt/codeql/codeql database create /src/codeql_db \
    --language=cpp \
    --source-root="$REPO_DIR" \
    --overwrite \
    --command="bash /src/compile.sh" 2>&1

if [ -d /src/codeql_db/db-cpp ]; then
    echo "BUILD_SUCCESS"
else
    echo "BUILD_FAILED"
fi
"""
        code, stdout, stderr = exec_in_container(container_id, build_cmd, timeout=1800)

        if "BUILD_SUCCESS" not in stdout + stderr:
            print(f"  ERROR: CodeQL build failed")
            print(f"  stdout: {stdout[-1000:] if stdout else 'empty'}")
            print(f"  stderr: {stderr[-1000:] if stderr else 'empty'}")
            return None

        # Copy database and source to host
        db_path = output_dir / "codeql_db"
        src_path = output_dir / "src"

        if db_path.exists():
            shutil.rmtree(db_path)
        if src_path.exists():
            shutil.rmtree(src_path)

        print("  Copying database to host...")
        if not copy_from_container(container_id, "/src/codeql_db", str(db_path)):
            print("  ERROR: Failed to copy database")
            return None

        print("  Copying source to host...")
        if not copy_from_container(container_id, "/src", str(src_path)):
            print("  ERROR: Failed to copy source")
            return None

        return db_path

    except Exception as e:
        print(f"  ERROR: {e}")
        return None

    finally:
        cleanup_container(container_id)


def main():
    parser = argparse.ArgumentParser(description='Build CodeQL DB in Docker, slice on host')
    parser.add_argument('--task', required=True, help='Task path (e.g., hiredis/arvo_28777)')
    parser.add_argument('--output-dir', default='./codeql_output', help='Output directory')
    parser.add_argument('--build-image', default=DEFAULT_BUILD_IMAGE)

    args = parser.parse_args()

    print("=" * 60)
    print(f"Building and slicing: {args.task}")
    print("=" * 60)

    output_dir = Path(args.output_dir) / args.task.replace("/", "_")

    # Step 1: Build CodeQL database in Docker
    db_path = build_codeql_db(args.task, output_dir, args.build_image)
    if not db_path:
        print("  FAILED: Could not build CodeQL database")
        return 1

    print(f"  SUCCESS: Database at {db_path}")

    # Step 2: Run slicing on host
    print("\nRunning slicing...")
    src_base = output_dir / "src"

    # Find harness to determine source directory
    harness, harness_dir = find_fuzzer_harness(src_base)
    if not harness:
        print("  ERROR: Could not find fuzzer harness")
        return 1

    # Path remap: container paths -> host paths
    path_remap_from = "/src/"
    path_remap_to = str(src_base) + "/"

    slice_file = output_dir / "context.txt"
    success = run_slicing(src_base, db_path, slice_file, path_remap_from, path_remap_to)

    if success and slice_file.exists():
        lines = slice_file.read_text().count('\n')
        print(f"  SUCCESS: {lines} lines -> {slice_file}")
        return 0
    else:
        print("  FAILED: Slicing failed")
        return 1


if __name__ == '__main__':
    exit(main())
