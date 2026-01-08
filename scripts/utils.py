"""
Shared utilities for e2e-cyber-bench scripts.

Docker container management, LLM configuration, and common operations.
"""

import subprocess
import os
from pathlib import Path

import boto3


def copy_to_container(container_id, src_path, dst_path, file_list=None):
    """
    Copy file(s) or directory to a container.

    Args:
        container_id: Docker container ID
        src_path: Source path (file or directory)
        dst_path: Destination path in container
        file_list: If provided, copy only these files from src_path directory

    Returns:
        None. Raises exception on failure (unless file_list with missing files).
    """
    src_path = Path(src_path)

    if file_list:
        # Copy specific files from directory
        for file_name in file_list:
            full_src = src_path / file_name
            if full_src.exists():
                cmd = ["docker", "cp", str(full_src), f"{container_id}:{dst_path}/{file_name}"]
                subprocess.run(cmd, capture_output=True, text=True)
    elif src_path.is_dir():
        # Copy entire directory
        cmd = ["docker", "cp", str(src_path) + "/.", f"{container_id}:{dst_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to copy {src_path}: {result.stderr}")
    else:
        # Copy single file
        cmd = ["docker", "cp", str(src_path), f"{container_id}:{dst_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Failed to copy {src_path}: {result.stderr}")


def start_container(build_image, output_dir=None, trajectory_dir=None):
    """Start a Docker container and return its ID."""
    cmd = ["docker", "run", "-d", "--sysctl", "net.ipv6.conf.all.disable_ipv6=0"]
    if output_dir:
        cmd.extend(["-v", f"{output_dir}:/output"])
    if trajectory_dir:
        cmd.extend(["-v", f"{trajectory_dir}:/agent_trajectory"])
    cmd.extend([build_image, "tail", "-f", "/dev/null"])
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def cleanup_container(container_id):
    """Remove a container."""
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], check=False, capture_output=True)


def exec_run(container_id, command, description=None, timeout=600, env=None, verbose=True):
    """
    Execute command in container.

    Args:
        container_id: Docker container ID
        command: Shell command to run
        description: Human-readable description (printed if verbose)
        timeout: Command timeout in seconds
        env: Environment variables dict
        verbose: Whether to print description

    Returns:
        tuple: (exit_code, stdout, stderr)
    """
    if verbose and description:
        print(f"  {description}")
    cmd = ["docker", "exec"]
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([container_id, "timeout", str(timeout), "bash", "-c", command])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    return result.returncode, result.stdout, result.stderr


def call_llm(prompt, model="bedrock", bedrock_model_id=None, aws_region="us-west-2", aws_profile="bedrock-profile", max_tokens=2000):
    """
    Call LLM with a prompt and return the response.

    Args:
        prompt: The prompt to send
        model: "bedrock" or "openai"
        bedrock_model_id: Bedrock model ID (required if model="bedrock")
        aws_region: AWS region for Bedrock
        aws_profile: AWS profile name for Bedrock
        max_tokens: Maximum tokens in response

    Returns:
        str: The model's response text, or None on error
    """
    try:
        if model == "bedrock":
            session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
            client = session.client("bedrock-runtime")

            response = client.converse(
                modelId=bedrock_model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
            )
            return response["output"]["message"]["content"][0]["text"]
        else:
            # OpenAI
            import openai
            client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            return response.choices[0].message.content
    except Exception as e:
        print(f"LLM call failed: {e}")
        return None


def get_llm_env(model, bedrock_model_id=None, aws_region="us-west-2", aws_profile="bedrock-profile"):
    """
    Get LLM environment variables for OpenHands.

    Args:
        model: "bedrock" or "openai"
        bedrock_model_id: Bedrock model ID (required if model="bedrock")
        aws_region: AWS region for Bedrock
        aws_profile: AWS profile name for credentials

    Returns:
        tuple: (env_dict, llm_model_string)
    """
    base_env = {
        "RUNTIME": "local",
        "LOG_ALL_EVENTS": "true",
        "SAVE_TRAJECTORY_PATH": "/agent_trajectory",
        "RUN_AS_OPENHANDS": "false",
        "SKIP_DEPENDENCY_CHECK": "1",
        "AGENT_ENABLE_PROMPT_EXTENSIONS": "false",
        "AGENT_ENABLE_BROWSING": "false",
        "ENABLE_BROWSER": "false",
        # Retry settings for rate limiting
        "LLM_NUM_RETRIES": "10",
        "LLM_RETRY_MIN_WAIT": "15",
        "LLM_RETRY_MAX_WAIT": "120",
        "LLM_RETRY_MULTIPLIER": "2",
    }

    if model == "bedrock":
        try:
            session = boto3.Session(profile_name=aws_profile)
            credentials = session.get_credentials()
            frozen_credentials = credentials.get_frozen_credentials()
            aws_access_key = frozen_credentials.access_key
            aws_secret_key = frozen_credentials.secret_key
            aws_session_token = frozen_credentials.token
        except Exception as e:
            print(f"Failed to get AWS credentials: {e}")
            aws_access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
            aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
            aws_session_token = os.getenv("AWS_SESSION_TOKEN", "")

        llm_model = f"bedrock/{bedrock_model_id}"
        env = {
            **base_env,
            "LLM_MODEL": llm_model,
            "LLM_AWS_ACCESS_KEY_ID": aws_access_key,
            "LLM_AWS_SECRET_ACCESS_KEY": aws_secret_key,
            "LLM_AWS_REGION_NAME": aws_region,
            "LLM_DROP_PARAMS": "true",
            "LLM_TEMPERATURE": "0.0",
            "AWS_ACCESS_KEY_ID": aws_access_key,
            "AWS_SECRET_ACCESS_KEY": aws_secret_key,
            "AWS_REGION_NAME": aws_region,
        }
        if aws_session_token:
            env["AWS_SESSION_TOKEN"] = aws_session_token
            env["LLM_AWS_SESSION_TOKEN"] = aws_session_token
        return env, llm_model
    else:
        llm_model = "openai/gpt-4.1"
        llm_api_key = os.getenv("OPENAI_API_KEY")
        env = {
            **base_env,
            "LLM_API_KEY": llm_api_key,
            "LLM_MODEL": llm_model,
        }
        return env, llm_model
