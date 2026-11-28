#!/usr/bin/env bash
# Bundle a recent boto3 so Lambdas can call the AgentCore harness APIs.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build/boto3-layer && mkdir -p build/boto3-layer/python
python3 -m pip install --quiet --upgrade "boto3>=1.40" -t build/boto3-layer/python
echo "boto3 layer built in build/boto3-layer"
