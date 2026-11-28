#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
. .venv/bin/activate
python agentcore/teardown.py --outputs cdk-outputs.json || true
cdk destroy ApGuardianStack --force
