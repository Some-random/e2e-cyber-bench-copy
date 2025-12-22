# e2e-cyber-bench

Data: https://huggingface.co/datasets/sunblaze-ucb/e2e-cyber-bench

```
pip install "huggingface_hub[cli]"
export HF_TOKEN=hf_....
```

Download it in the data/ folder:
```
hf download sunblaze-ucb/e2e-cyber-bench --repo-type dataset --local-dir data/ # download all projects
hf download sunblaze-ucb/e2e-cyber-bench --repo-type dataset --local-dir data/ --include "projects/curl/" # download specific project
```
Upload your project data to huggingfaco
```
hf upload sunblaze-ucb/e2e-cyber-bench --repo-type dataset data/projects/curl/ projects/curl
```

Default build image: gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc (24.04)
Alternative build_image: gcr.io/oss-fuzz-base/base-builder@sha256:fba1033c6a64433642ab97b6ea987ddaa9938e06596c6cace1c786130fc1461b (20.04)
Set build_image = "gcr.io/oss-fuzz-base/base-builder@sha256:" in project.toml or config.toml to overwrite the default build image.

File structure:

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

## Example

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

```bash
# validate the vulnerable version and the patched version
# each would run about 15min
python3 scripts/validate.py --run-prepare --run-cleanup curl/arvo_66012
python3 scripts/validate.py --run-prepare --run-cleanup --apply-patch curl/arvo_66012
```

```bash
# run agent
python3 scripts/run_agent.py --run-prepare --run-cleanup curl/arvo_66012
```
