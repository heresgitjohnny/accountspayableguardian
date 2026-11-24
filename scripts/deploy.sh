#!/usr/bin/env bash
# One-shot deploy: CDK stack -> AgentCore gateway/policies/harnesses -> seed data.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_REGION:=us-east-1}"
export AWS_REGION CDK_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

command -v cdk >/dev/null || { echo "Install the CDK CLI: npm install -g aws-cdk"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install --quiet -r requirements.txt
python -c "import boto3,sys; c=boto3.client('bedrock-agentcore-control',region_name='$AWS_REGION'); sys.exit(0 if hasattr(c,'create_harness') else 'boto3 too old: pip install -U boto3')"

scripts/build_layer.sh
cdk bootstrap "aws://$CDK_DEFAULT_ACCOUNT/$AWS_REGION"
cdk deploy ApGuardianStack --require-approval never --outputs-file cdk-outputs.json "$@"
python agentcore/provision.py --outputs cdk-outputs.json
python scripts/seed_data.py --outputs cdk-outputs.json

echo
echo "Portal: $(python -c "import json;print(json.load(open('cdk-outputs.json'))['ApGuardianStack']['PortalUrl'])")"
echo "Next: create users (see README 'Create portal users'), then run: python scripts/stress_test.py"
