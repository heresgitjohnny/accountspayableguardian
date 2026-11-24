"""AgentCore harness lifecycle hooks (Lambda targets return allow/deny).

guardrail_hook : before_invocation + after_tool_call on every harness.
                 Runs Amazon Bedrock Guardrails (ApplyGuardrail) over the
                 incoming messages and over every tool result, so instructions
                 hidden in invoice text or email bodies are caught before the
                 model reasons over them. Fails closed on truncated context.
payment_hook   : before_tool_call on the Payment harness. Independent re-check
                 of the auto-pay limit before pay_invoice runs.
"""
import json
import os

import boto3

from common import log, AUTO_PAY_LIMIT_CENTS

bedrock_rt = boto3.client("bedrock-runtime")


def _texts(obj):
    if isinstance(obj, str):
        return [obj]
    return [json.dumps(obj, default=str)]


def _apply_guardrail(text):
    resp = bedrock_rt.apply_guardrail(
        guardrailIdentifier=os.environ["GUARDRAIL_ID"],
        guardrailVersion=os.environ["GUARDRAIL_VERSION"],
        source="INPUT",
        content=[{"text": {"text": text[:20000]}}],
    )
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return None
    hits = []
    for a in resp.get("assessments", []):
        for f in (a.get("contentPolicy") or {}).get("filters", []):
            if f.get("action") == "BLOCKED":
                hits.append(f"content:{f.get('type')}")
        for t in (a.get("topicPolicy") or {}).get("topics", []):
            if t.get("action") == "BLOCKED":
                hits.append(f"topic:{t.get('name')}")
        for w in (a.get("wordPolicy") or {}).get("customWords", []):
            if w.get("action") == "BLOCKED":
                hits.append(f"word:{w.get('match')}")
    return hits or ["guardrail intervened"]


def _is_truncated(ctx):
    if ctx.get("truncated"):
        return True
    return any(isinstance(v, dict) and v.get("truncated") for v in ctx.values())


def guardrail_hook(event, _ctx):
    ev = event.get("event")
    ctx = event.get("context") or {}
    if _is_truncated(ctx):
        return {"decision": "deny", "reason": "Context truncated; cannot be fully screened."}
    if ev == "before_invocation":
        payload = [c.get("text", "") for m in ctx.get("messages", []) for c in m.get("content", [])]
        text = "\n".join(payload)
    elif ev == "after_tool_call":
        text = "\n".join(_texts(ctx.get("toolResult", "")))
    else:
        return {"decision": "allow"}
    if not text.strip():
        return {"decision": "allow"}
    hits = _apply_guardrail(text)
    if hits:
        log.warning(json.dumps({"event": "GUARDRAIL_DENY", "hook": ev, "harness": event.get("harnessArn"),
                                "tool": ctx.get("toolName"), "hits": hits}))
        return {"decision": "deny",
                "reason": f"Guardrail blocked {ev} content ({', '.join(hits)[:300]}). Route to human review."}
    return {"decision": "allow"}


def payment_hook(event, _ctx):
    ctx = event.get("context") or {}
    tool = str(ctx.get("toolName", ""))
    if not tool.endswith("pay_invoice"):
        return {"decision": "allow"}
    tool_input = ctx.get("toolInput") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            return {"decision": "deny", "reason": "Unparseable pay_invoice input"}
    # Tool input may be flat or nested under a body key depending on target type.
    flat = {**tool_input, **(tool_input.get("body") or {})} if isinstance(tool_input.get("body"), dict) else tool_input
    try:
        cents = int(flat.get("amount_cents"))
    except (TypeError, ValueError):
        return {"decision": "deny", "reason": "pay_invoice requires integer amount_cents"}
    if cents > AUTO_PAY_LIMIT_CENTS:
        return {"decision": "deny",
                "reason": f"{cents} cents exceeds the auto-pay limit. Call request_approval instead."}
    return {"decision": "allow"}
