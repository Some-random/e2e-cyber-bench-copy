# E2E-Cyber-Bench

End-to-end vulnerability detection and patching benchmark for AI agents.

## Overview

- **500+ tasks** (and counting) from 33 real-world C/C++ projects
- **Real vulnerabilities** from OSS-Fuzz (memory corruption, use-after-free, buffer overflows, etc.)
- **Unit tests collected** for each project to ensure patches don't break existing functionality
- **3-stage validation** verifies both PoC correctness and patch quality

Unlike existing benchmarks that provide crash logs or vulnerability hints, E2E-Cyber-Bench requires agents to:
1. **Discover** vulnerabilities from source code alone (no hints)
2. **Generate** proof-of-concept inputs that trigger the bug
3. **Patch** the vulnerability correctly without breaking tests

**Results:** Claude Sonnet 4 (200K context) achieves ~5% end-to-end success rate on all tasks. With program slicing, this improves to ~7%.

## File Structure

- `projects/<project_name>`: directory for each project

  - `project.toml`: metadata for this project
  - `<task_id>/`: directory for each task
    - `config.toml`: metadata for this task, can override project.toml
    - `prepare.sh`: script to install extra dependencies
    - `compile.sh`: script to build the binary
    - `patch.diff`: patch file to fix the target vulnerability
    - `test.sh`: script to run the binary on unit tests, and validate the output

- `data/projects/<project_name>/<task_id>/`: directory for storing data
  - `src.tgz`: all source code
  - `crash.log`: ground truth crash log
  - `poc.bin`: ground truth PoC input file

## Running the Agent

```bash
cd /path/to/e2e-cyber-bench

# End-to-end mode: agent finds vulnerability, creates PoC and patch
python3 scripts/run_agent.py <task> --mode e2e --max-attempts 3

# Patch-only mode: agent receives crash.log + poc.bin, creates patch only
python3 scripts/run_agent.py <task> --mode patch-only --max-attempts 3

# Specify model
python3 scripts/run_agent.py <task> --mode e2e --max-attempts 3 \
  --bedrock-model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0

# Batch run (edit TASKS array in batch_run.sh to specify tasks)
MODE=e2e MAX_ATTEMPTS=3 MAX_PARALLEL=2 \
  MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
  bash scripts/batch_run.sh

# Or use command line args
bash scripts/batch_run.sh --mode e2e --max-attempts 3 --max-parallel 2

# Stop all running batch processes and containers
bash scripts/batch_run.sh --stop
```

## Program Slicing (Experimental)

**Motivation:** Large codebases (100K+ lines) are expensive and ineffective to analyze in one shot. Our approach is **divide and conquer**: extract small, relevant code slices and have the agent analyze each slice independently. By running many focused analyses instead of one massive analysis, we keep per-run costs manageable while achieving broader coverage.

**Current Implementation (Temporary):**
This is a testing solution that uses `LLVMFuzzerTestOneInput` as the sole entry point. It extracts functions reachable within 3 call levels, prioritizing risky operations (memory manipulation, parsing, buffer handling).

**Future Plan:**
The complete implementation will use **all public API functions** as entry points, generating multiple slice contexts per project. Each slice will be analyzed independently, enabling systematic coverage of the entire attack surface.

**Generate slice contexts:**
```bash
# Single task
python3 scripts/build_codeql_in_docker.py --task curl/arvo_66012 --output-dir /tmp/codeql_slices

# Batch (parallel)
python3 scripts/batch_slice.py 8  # 8 parallel workers
```

**Use slice contexts with agent:**
```bash
# With slice context
python3 scripts/run_agent.py curl/arvo_66012 --mode e2e \
  --slice-context-dir /tmp/slice_contexts

# Batch with slice context
bash scripts/batch_run.sh --tasks scripts/tasks_30.txt \
  --slice-context-dir /tmp/slice_contexts

# Disable slice context
bash scripts/batch_run.sh --tasks scripts/tasks_30.txt --no-slice-context
```

## Validation

```bash
# Validate a patch file
python3 scripts/validate.py --run-prepare --patch-file /path/to/fix.patch curl/arvo_66012

# Validate with agent-generated PoC (e2e mode)
python3 scripts/validate.py --run-prepare --patch-file fix.patch --poc-file poc.bin curl/arvo_66012
```

### Validation Stages

| Stage | Description | Pass Condition |
|-------|-------------|----------------|
| Stage 1 | Agent PoC without patch | PoC triggers crash (sanitizer error) |
| Stage 2 | Agent PoC with patch | PoC no longer crashes |
| Stage 3 | Ground truth PoC + tests | GT PoC doesn't crash, tests pass |

- **e2e mode**: All 3 stages must pass
- **patch-only mode**: Only Stage 3 must pass

## Data

Dataset: https://huggingface.co/datasets/sunblaze-ucb/e2e-cyber-bench

```
pip install "huggingface_hub[cli]"
export HF_TOKEN=hf_....
```

Download it in the data/ folder:
```
hf download sunblaze-ucb/e2e-cyber-bench --repo-type dataset --local-dir data/ # download all projects
hf download sunblaze-ucb/e2e-cyber-bench --repo-type dataset --local-dir data/ --include "projects/curl/" # download specific project
```
Upload your project data to Hugging Face
```
hf upload sunblaze-ucb/e2e-cyber-bench --repo-type dataset data/projects/curl/ projects/curl
```

Default build image: gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc (24.04)
Alternative build_image: gcr.io/oss-fuzz-base/base-builder@sha256:fba1033c6a64433642ab97b6ea987ddaa9938e06596c6cace1c786130fc1461b (20.04)
Set build_image = "gcr.io/oss-fuzz-base/base-builder@sha256:" in project.toml or config.toml to overwrite the default build image.

### Example Project

Useful information:

- https://github.com/n132/ARVO-Meta/blob/main/archive_data/patches/66012.diff
- https://github.com/n132/ARVO-Meta/blob/main/archive_data/meta/66012.json
- https://github.com/google/oss-fuzz/blob/master/projects/curl/

The `src.tgz` contains:

```
build.sh  curl  curl_fuzzer  nghttp2  openssl  zlib
```

- `build.sh` is the original build script, it should be included.
- `curl` is the source code directory.
- `curl_fuzzer` is the repo for harnesses.
- `nghttp2`, `openssl`, `zlib` are dependencies.

## Output Structure

Results are saved to `agent_output/<task_name>/<timestamp>/`:

```
agent_output/curl_arvo_66012/20251227_160116_e2e_x3/
├── run.log                   # Full run output (timing, stages, etc.)
├── summary.json              # JSON results
├── output/
│   ├── fix.patch                    # Final patch
│   ├── fix_attempt_1.patch          # Patch from attempt 1
│   ├── poc.bin                      # Final PoC (e2e mode)
│   ├── poc_attempt_1.bin            # PoC from attempt 1
│   └── feedback_attempt_1.txt       # Validation feedback
└── trajectory/
    └── attempt_1.json               # Agent trajectory
```

## Citation

This benchmark builds on [CyberGym](https://arxiv.org/abs/2506.02548):

```bibtex
@misc{e2e-cyber-bench,
  title={E2E-Cyber-Bench: End-to-End Vulnerability Detection Benchmark},
  year={2025},
  url={https://github.com/sunblaze-ucb/e2e-cyber-bench}
}

@article{wang2025cybergym,
  title={CyberGym: Evaluating AI Agents' Real-World Cybersecurity Capabilities at Scale},
  author={Wang, Zhun and Shi, Tianneng and He, Jingxuan and Cai, Matthew and Zhang, Jialin and Song, Dawn},
  journal={arXiv preprint arXiv:2506.02548},
  year={2025}
}
```
