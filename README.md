# e2e-cyber-bench

Dataset: https://huggingface.co/datasets/sunblaze-ucb/e2e-cyber-bench

Default base builder: gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc

File structure:

- `projects/<project_name>`: directory for each project
  - `project.toml`: metadata for this project
  - `<task_id>/`: directory for each task
    - `config.toml`: metadata for this task, can override project.toml
    - `prepare.sh`: script to install extra dependencies
    - `compile.sh`: script to build the binary
    - `patch.diff`: patch file to fix the target vulnerability
    - `test.sh`: script to run the binary on unit tests

- `data/projects/<project_name>/<task_id>/`: directory for storing data
  - `src.tgz`: all source code
  - `crash.log`: ground truth crash log
  - `poc.bin`: ground truth PoC input file

## Example

Useful information:

- https://github.com/n132/ARVO-Meta/blob/main/archive_data/patches/66012.diff
- https://github.com/n132/ARVO-Meta/blob/main/archive_data/meta/66012.json
- https://github.com/google/oss-fuzz/blob/master/projects/curl/


```bash
python3 scripts/validate.py --run-prepare --run-cleanup curl/arvo_66012
python3 scripts/validate.py --run-prepare --run-cleanup --apply-patch curl/arvo_66012
```