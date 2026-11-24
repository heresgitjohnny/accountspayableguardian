"""Thin wrapper around InvokeHarness that collects the streamed reply."""
import os
import uuid

import boto3
from botocore.config import Config

_ssm = boto3.client("ssm")
_rt = boto3.client("bedrock-agentcore", config=Config(read_timeout=900, connect_timeout=10,
                                                      retries={"max_attempts": 2}))
_cache = {}


def harness_arn(role):
    """Harness ARNs are written to SSM by agentcore/provision.py."""
    if role not in _cache:
        name = f"{os.environ['SSM_PREFIX']}/harness/{role}"
        _cache[role] = _ssm.get_parameter(Name=name)["Parameter"]["Value"]
    return _cache[role]


def invoke(role, text, session_id=None, actor_id="ap-guardian"):
    resp = _rt.invoke_harness(
        harnessArn=harness_arn(role),
        runtimeSessionId=session_id or f"{role}-{uuid.uuid4()}",
        actorId=actor_id,
        messages=[{"role": "user", "content": [{"text": text}]}],
    )
    chunks, stop_reason, hooks = [], None, []
    for event in resp["stream"]:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                chunks.append(delta["text"])
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
        elif "hookEvent" in event:
            h = event["hookEvent"]
            hooks.append({k: h.get(k) for k in ("name", "type", "decision", "reason")})
        elif any(k in event for k in ("internalServerException", "validationException", "runtimeClientError")):
            raise RuntimeError(f"harness stream error: {event}")
    return {"text": "".join(chunks), "stop_reason": stop_reason, "hooks": hooks}
