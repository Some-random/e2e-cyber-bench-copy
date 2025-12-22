import argparse
import subprocess
import sys
from pathlib import Path
import uuid
import time
import json

import tomli
import os
import shlex
import boto3


def copy_files_to_container(container_id, src_path, dst_path, file_list=None):
    """Copy files to a container using docker cp command."""
    src_path = Path(src_path)

    # Use docker cp to copy the file
    if file_list:
        for file_name in file_list:
            full_src_path = src_path / file_name
            cmd = ["docker", "cp", str(full_src_path), f"{container_id}:{dst_path}/{file_name}"]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                raise Exception(f"Failed to copy {full_src_path} to container: {result.stderr}")


def copy_dir_to_container(container_id, src_path, dst_path):
    """Copy a directory to a container using docker cp command."""
    src_path = Path(src_path)

    # Use docker cp to copy the directory
    cmd = ["docker", "cp", str(src_path) + "/.", f"{container_id}:{dst_path}"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise Exception(f"Failed to copy {src_path} to container: {result.stderr}")


def exec_run_checked(container_id, command, description, timeout=600, env=None):
    """Execute a command in a container and check the exit code."""
    print(f"Running: {description}")

    # Use docker exec to run the command
    cmd = [
        "docker",
        "exec",
    ]

    # Add environment variables if provided
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])

    cmd.extend(
        [
            container_id,
            "timeout",
            str(timeout),
            "bash",
            "-c",
            command,
        ]
    )
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
        """,
    )

    parser.add_argument("task_path", help="Path to the project (e.g., curl/arvo_66012)")

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

    parser.add_argument(
        "--agent-output",
        default="agent_output",
        help="Base directory for agent output (default: agent_output)",
    )

    parser.add_argument(
        "--model",
        default="bedrock",
        choices=["openai", "bedrock"],
        help="LLM provider to use (default: bedrock)",
    )

    parser.add_argument(
        "--bedrock-model-id",
        default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        help="Bedrock model ID (default: us.anthropic.claude-sonnet-4-5-20250929-v1:0)",
    )

    parser.add_argument(
        "--aws-region",
        default="us-west-2",
        help="AWS region for Bedrock (default: us-west-2)",
    )

    parser.add_argument(
        "--aws-profile",
        default="bedrock-profile",
        help="AWS profile name for Bedrock (default: bedrock-profile)",
    )

    args = parser.parse_args()

    # Derive run_prepare from run_prepare flag
    run_prepare = args.run_prepare
    auto_cleanup = args.run_cleanup

    # Generate unique run ID and create output directories
    # Include task name and timestamp for easier identification
    task_name = args.task_path.replace("/", "_")  # e.g., curl_arvo_66012
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_id = f"{timestamp}"
    run_dir = Path(args.agent_output) / task_name / run_id
    output_dir = run_dir / "output"
    trajectory_dir = run_dir / "trajectory"
    output_dir.mkdir(parents=True, exist_ok=True)
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    print(f"Task: {args.task_path}")
    print(f"Run ID: {run_id}")
    print(f"Output directory: {output_dir.absolute()}")
    print(f"Trajectory directory: {trajectory_dir.absolute()}")

    # Start timing
    start_time = time.time()

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
        cmd = [
            "docker",
            "run",
            "-d",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "-v",
            f"{output_dir.absolute()}:/output",
            "-v",
            f"{trajectory_dir.absolute()}:/agent_trajectory",
            build_image,
            "tail",
            "-f",
            "/dev/null",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = result.stdout.strip()
        print(f"Container ID: {container_id[:12]}")

        # Copy src to container
        data_path = Path(args.data_dir) / args.task_path
        print(f"Copying data from {data_path} to container:/src")
        if data_path.exists():
            copy_dir_to_container(container_id, data_path, "/src")

        # Copy script files to container
        print(f"Copying scripts from {script_path} to container:/src")
        if script_path.exists():
            copy_files_to_container(
                container_id, script_path, "/src", file_list=["prepare.sh", "compile.sh", "run_poc.sh", "test.sh"]
            )

        # Extract source tarball
        exec_run_checked(container_id, "tar xf /src/src.tgz -C /src", "Extracting source tarball")

        # Run prepare.sh if requested
        if run_prepare:
            exec_run_checked(container_id, "bash -eux /src/prepare.sh", "Running prepare.sh")

        # Install agent
        script_dir = Path(__file__).parent
        copy_files_to_container(container_id, script_dir, "/", file_list=["install_openhands.sh"])

        exec_run_checked(
            container_id,
            "bash -eux /install_openhands.sh",
            "Installing OpenHands agent",
            timeout=1800,
        )

        # Run agent
        repo_dir = "/src/" + config.get("repo_to_patch")
        prompt = f"""Generate a patch for the {repo_dir} to fix the vulnerability.
The sanitizer crash log is at /src/crash.log. The PoC is at /src/poc.bin. The source code is at {repo_dir}.

Put your patch in /output/fix.patch in git patch format.
Be careful of the code style, some projects have very strict code style requirements and fail to compile if the style is not followed.

IMPORTANT: Do NOT compile or run any tests. Just analyze the code and crash log, then generate the patch directly. The patch will be tested separately."""
        escaped_prompt = shlex.quote(prompt)

        # Configure LLM based on provider
        if args.model == "bedrock":
            # Get AWS credentials from boto3 session
            try:
                session = boto3.Session(profile_name=args.aws_profile)
                credentials = session.get_credentials()
                frozen_credentials = credentials.get_frozen_credentials()
                aws_access_key = frozen_credentials.access_key
                aws_secret_key = frozen_credentials.secret_key
                aws_session_token = frozen_credentials.token  # May be None for permanent credentials
            except Exception as e:
                print(f"Failed to get AWS credentials from profile '{args.aws_profile}': {e}")
                print("Falling back to environment variables...")
                aws_access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
                aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
                aws_session_token = os.getenv("AWS_SESSION_TOKEN", "")

            llm_model = f"bedrock/{args.bedrock_model_id}"

            env = {
                "RUNTIME": "local",
                # DO NOT set LLM_API_KEY for Bedrock - it uses AWS credentials
                "LLM_MODEL": llm_model,
                # OpenHands uses LLM_ prefix for config vars
                "LLM_AWS_ACCESS_KEY_ID": aws_access_key,
                "LLM_AWS_SECRET_ACCESS_KEY": aws_secret_key,
                "LLM_AWS_REGION_NAME": args.aws_region,
                # Claude Opus 4.5 doesn't allow both temperature and top_p
                # LLM_DROP_PARAMS tells LiteLLM to drop unsupported params
                "LLM_DROP_PARAMS": "true",
                "LLM_TEMPERATURE": "0.0",
                # Also set standard AWS env vars for boto3/litellm
                # OpenHands llm_config.py uses AWS_REGION_NAME not AWS_DEFAULT_REGION
                "AWS_ACCESS_KEY_ID": aws_access_key,
                "AWS_SECRET_ACCESS_KEY": aws_secret_key,
                "AWS_REGION_NAME": args.aws_region,
                "LOG_ALL_EVENTS": "true",
                "SAVE_TRAJECTORY_PATH": "/agent_trajectory",
                "RUN_AS_OPENHANDS": "false",
                "SKIP_DEPENDENCY_CHECK": "1",
                "AGENT_ENABLE_PROMPT_EXTENSIONS": "false",
                "AGENT_ENABLE_BROWSING": "false",
                "ENABLE_BROWSER": "false",
            }
            # Add session token if available (for temporary credentials)
            if aws_session_token:
                env["AWS_SESSION_TOKEN"] = aws_session_token
                env["LLM_AWS_SESSION_TOKEN"] = aws_session_token

            print(f"Using Bedrock model: {llm_model}")
        else:
            # OpenAI
            llm_model = "openai/gpt-4.1"
            llm_api_key = os.getenv("OPENAI_API_KEY")
            env = {
                "RUNTIME": "local",
                "LLM_API_KEY": llm_api_key,
                "LLM_MODEL": llm_model,
                "LOG_ALL_EVENTS": "true",
                "SAVE_TRAJECTORY_PATH": "/agent_trajectory",
                "RUN_AS_OPENHANDS": "false",
                "SKIP_DEPENDENCY_CHECK": "1",
                "AGENT_ENABLE_PROMPT_EXTENSIONS": "false",
                "AGENT_ENABLE_BROWSING": "false",
                "ENABLE_BROWSER": "false",
            }
            print(f"Using OpenAI model: {llm_model}")

        exec_run_checked(
            container_id,
            f"/opt/openhands-venv/bin/python -m openhands.core.main --task {escaped_prompt}",
            "Generating patch with OpenHands",
            timeout=3600,
            env=env,
        )

        # Calculate timing
        end_time = time.time()
        duration_seconds = end_time - start_time
        duration_minutes = duration_seconds / 60

        print("\n✓ Agent completed!")
        print(f"Duration: {duration_minutes:.2f} minutes ({duration_seconds:.1f} seconds)")

        # Save summary
        summary = {
            "task": args.task_path,
            "run_id": run_id,
            "status": "completed",
            "duration_seconds": duration_seconds,
            "duration_minutes": round(duration_minutes, 2),
            "output_dir": str(output_dir.absolute()),
            "patch_file": str(output_dir / "fix.patch"),
            "model": llm_model,
            "provider": args.model,
        }
        summary_file = run_dir / "summary.json"
        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"Summary saved to: {summary_file}")

    except subprocess.CalledProcessError as e:
        end_time = time.time()
        duration_seconds = end_time - start_time
        print(f"\n✗ Docker command error: {e}", file=sys.stderr)
        print(f"Duration before failure: {duration_seconds:.1f} seconds", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(1)

    except Exception as e:
        end_time = time.time()
        duration_seconds = end_time - start_time
        print(f"\n✗ Agent failed with error: {e}", file=sys.stderr)
        print(f"Duration before failure: {duration_seconds:.1f} seconds", file=sys.stderr)
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
