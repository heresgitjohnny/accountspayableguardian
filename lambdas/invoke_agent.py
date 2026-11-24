"""Invoices-table stream (INSERT only) -> Supervisor harness.

Only the invoice ID is sent to the agent. Untrusted invoice and email content
reaches the agents solely through tool results, where the after_tool_call
guardrail hook inspects it.

Fail-closed: if the agents finish without either paying the invoice or opening
an exception, this function opens an exception so a human looks at it.
"""
import json

from common import log, table, open_exception
import harness_client


def _process(invoice_id):
    prompt = (f"A new vendor invoice has arrived. invoice_id={invoice_id}. "
              "Coordinate the intake, matching and payment specialists to process it according to your rules.")
    try:
        result = harness_client.invoke("supervisor", prompt, actor_id=invoice_id)
    except Exception as e:  # agent unavailable -> human
        log.exception("supervisor invocation failed")
        return open_exception(invoice_id, f"Agent run failed: {e}", source="invoke_agent")

    invoices = table("INVOICES_TABLE")
    invoices.update_item(
        Key={"invoice_id": invoice_id},
        UpdateExpression="SET agent_summary = :s, agent_stop_reason = :r, agent_hooks = :h",
        ExpressionAttributeValues={":s": result["text"][-3000:], ":r": str(result["stop_reason"]),
                                   ":h": json.dumps(result["hooks"])[:3000]},
    )
    inv = invoices.get_item(Key={"invoice_id": invoice_id})["Item"]
    log.info(json.dumps({"event": "AGENT_DONE", "invoice_id": invoice_id, "status": inv.get("status"),
                         "stop_reason": result["stop_reason"], "hooks": result["hooks"]}))
    if inv.get("status") not in ("PAID", "EXCEPTION", "REJECTED"):
        reason = "Agents did not complete processing"
        if result["stop_reason"] == "hook_stopped":
            denied = [h for h in result["hooks"] if h.get("decision") == "deny"]
            reason = f"Stopped by safety hook: {denied[:1]}"
        return open_exception(invoice_id, reason, source="invoke_agent")
    return {"invoice_id": invoice_id, "status": inv.get("status")}


def handler(event, _ctx):
    out = []
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            continue
        invoice_id = rec["dynamodb"]["Keys"]["invoice_id"]["S"]
        out.append(_process(invoice_id))
    return out
