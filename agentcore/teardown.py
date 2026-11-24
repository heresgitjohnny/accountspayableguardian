#!/usr/bin/env python3
"""Delete the AgentCore resources created by provision.py (run before cdk destroy)."""
import argparse
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")


def quiet(fn, **kw):
    try:
        fn(**kw)
    except ClientError as e:
        print(f"  skip: {e.response['Error']['Code']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", default="cdk-outputs.json")
    args = ap.parse_args()
    out = json.load(open(args.outputs)).get("ApGuardianStack")
    ctl = boto3.client("bedrock-agentcore-control", region_name=out["Region"])
    ssm = boto3.client("ssm", region_name=out["Region"])
    state = json.load(open(STATE_FILE))

    for role, h in state.get("harnesses", {}).items():
        print(f"Deleting harness {role}")
        quiet(ctl.delete_harness, harnessId=h["id"])
        quiet(ssm.delete_parameter, Name=f"{out['SsmPrefix']}/harness/{role}")
    for name, tid in state.get("targets", {}).items():
        print(f"Deleting target {name}")
        quiet(ctl.delete_gateway_target, gatewayIdentifier=state["gatewayId"], targetId=tid)
    time.sleep(10)
    if state.get("gatewayId"):
        print("Deleting gateway")
        quiet(ctl.delete_gateway, gatewayIdentifier=state["gatewayId"])
    for name, pid in state.get("policies", {}).items():
        print(f"Deleting policy {name}")
        quiet(ctl.delete_policy, policyEngineId=state["policyEngineId"], policyId=pid)
    time.sleep(10)
    if state.get("policyEngineId"):
        print("Deleting policy engine")
        quiet(ctl.delete_policy_engine, policyEngineId=state["policyEngineId"])
    os.remove(STATE_FILE)
    print("AgentCore resources removed. Now run: cdk destroy")


if __name__ == "__main__":
    main()
