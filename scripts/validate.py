import argparse
import subprocess
import sys
from pathlib import Path

import tomli


def copy_dir_to_container(container_id, src_path, dst_path):
    """Copy a directory to a container using docker cp command."""
    src_path = Path(src_path)

    # Use docker cp to copy the directory
    cmd = ["docker", "cp", str(src_path) + "/.", f"{container_id}:{dst_path}"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise Exception(f"Failed to copy {src_path} to container: {result.stderr}")


def exec_run_checked(container_id, command, description, timeout=600):
    """Execute a command in a container and check the exit code."""
    print(f"Running: {description}")

    # Use docker exec to run the command
    cmd = [
        "docker",
        "exec",
        container_id,
        "timeout",
        str(timeout),
        "bash",
        "-c",
        command,
    ]
    result = subprocess.run(cmd, text=True, timeout=timeout + 10)

    if result.returncode != 0:
        raise Exception(f"{description} failed (exit code {result.returncode})")


def main():
    parser = argparse.ArgumentParser(
        description="Validate an e2e-cyber-bench project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s curl/arvo_66012
  %(prog)s curl/arvo_66012 --apply-patch
  %(prog)s curl/arvo_66012 --no-prepare --no-cleanup
        """,
    )

    parser.add_argument("task_path", help="Path to the project (e.g., curl/arvo_66012)")

    parser.add_argument(
        "--apply-patch",
        action="store_true",
        default=False,
        help="Apply patch before compiling (default: False)",
    )

    parser.add_argument(
        "--patch-file",
        default=None,
        help="Path to external patch file to apply (instead of default patch.diff)",
    )

    parser.add_argument(
        "--run-prepare",
        action="store_true",
        default=False,
        help="Run prepare.sh (default: False, i.e., prepare is skipped by default)",
    )

    parser.add_argument(
        "--run-cleanup",
        action="store_true",
        default=False,
        help="Run automatic cleanup of Docker container (default: False, i.e., cleanup is skipped by default)",
    )

    parser.add_argument(
        "--data-dir",
        default="./data/projects",
        help="Data directory path (default: ./data/projects)",
    )

    parser.add_argument(
        "--script-dir",
        default="./projects",
        help="Script directory path (default: ./projects)",
    )

    parser.add_argument(
        "--default-build-image",
        default="gcr.io/oss-fuzz-base/base-builder@sha256:8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc",
        help="Default Docker build image to use",
    )

    args = parser.parse_args()

    # Derive run_prepare from run_prepare flag
    run_prepare = args.run_prepare
    auto_cleanup = args.run_cleanup

    container_id = None

    try:
        # Load config.toml and project.toml
        script_path = Path(args.script_dir) / args.task_path
        config_toml_path = script_path / "config.toml"
        project_toml_path = script_path / "../project.toml"
        config = tomli.loads(project_toml_path.read_text())
        config.update(tomli.loads(config_toml_path.read_text()))

        # Start Docker container
        build_image = config.get("build_image", args.default_build_image)
        print(f"Starting Docker container from image: {build_image}")
        cmd = ["docker", "run", "-d", build_image, "tail", "-f", "/dev/null"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = result.stdout.strip()
        print(f"Container ID: {container_id[:12]}")

        # Copy data files to container
        data_path = Path(args.data_dir) / args.task_path
        print(f"Copying data from {data_path} to container:/src")
        if data_path.exists():
            copy_dir_to_container(container_id, data_path, "/src")

        # Copy script files to container
        print(f"Copying scripts from {script_path} to container:/src")
        if script_path.exists():
            copy_dir_to_container(container_id, script_path, "/src")

        # Extract source tarball
        exec_run_checked(
            container_id, "tar xf /src/src.tgz -C /src", "Extracting source tarball"
        )

        # Run prepare.sh if requested
        if run_prepare:
            exec_run_checked(
                container_id, "bash -eux /src/prepare.sh", "Running prepare.sh"
            )

        # Apply patch if requested
        if args.apply_patch or args.patch_file:
            repo_path = "/src/" + config["repo_to_patch"]
            if args.patch_file:
                # Copy custom patch file to container
                patch_path = Path(args.patch_file).absolute()
                print(f"Copying custom patch from {patch_path} to container:/src/custom.patch")
                cmd = ["docker", "cp", str(patch_path), f"{container_id}:/src/custom.patch"]
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                patch_file = "/src/custom.patch"
            else:
                patch_file = "/src/patch.diff"

            # Try applying patch with different strip levels (-p1, -p2, -p3)
            # This handles cases where agent generates patches with extra directory prefixes
            patch_applied = False
            for strip_level in [1, 2, 3]:
                try:
                    exec_run_checked(
                        container_id,
                        f"cd {repo_path} && git apply -p{strip_level} {patch_file}",
                        f"Applying patch (strip level {strip_level})",
                    )
                    patch_applied = True
                    break
                except Exception as e:
                    if strip_level == 3:
                        raise Exception(f"Failed to apply patch with strip levels 1-3: {e}")
                    print(f"  Strip level {strip_level} failed, trying next...")

        # Run compile.sh
        exec_run_checked(
            container_id, "bash -eux /src/compile.sh", "Running compile.sh"
        )

        # Run poc
        if args.apply_patch or args.patch_file:
            # Patched version: expect PoC to NOT crash
            exec_run_checked(
                container_id, "bash -eux /src/run_poc.sh", "Running PoC",
            )

        else:
            # Vulnerable version: expect PoC to crash
            exec_run_checked(
                container_id, "bash -eux /src/run_poc.sh || exit 0 && exit 1", "Running PoC",
            )

        # Run test.sh
        exec_run_checked(
            container_id, "bash -eux /src/test.sh", "Running test.sh", timeout=1800
        )

        print("\n✓ Validation completed successfully!")

    except subprocess.CalledProcessError as e:
        print(f"\n✗ Docker command error: {e}", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(1)

    except Exception as e:
        print(f"\n✗ Validation failed with error: {e}", file=sys.stderr)
        sys.exit(1)

    finally:
        # Cleanup Docker container
        if container_id and auto_cleanup:
            try:
                print(f"\nCleaning up container {container_id[:12]}")
                subprocess.run(["docker", "rm", "-f", container_id], check=False)
            except Exception as e:
                print(f"Warning: Failed to cleanup container: {e}", file=sys.stderr)
        elif container_id:
            print(f"\nContainer {container_id[:12]} left running (cleanup disabled)")


if __name__ == "__main__":
    main()
