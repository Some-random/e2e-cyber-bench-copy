#!/usr/bin/env bash
set -euo pipefail

# Add any additional dependency installation commands here
# Dependencies for integration tests since this is a redis client library
apt-get update && apt install redis-server
redis-server &
