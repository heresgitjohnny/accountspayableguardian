"""AgentCore Gateway Lambda target used by the Supervisor to call specialists.

Tools: ask_intake_agent, ask_matching_agent, ask_payment_agent.
Each specialist is a separate harness with its own execution role, its own
allowedTools list and its own Cedar permissions.
"""
import re

from common import log
import harness_client

ROLE_FOR_TOOL = {
    "ask_intake_agent": "intake",
    "ask_matching_agent": "matching",
    "ask_payment_agent": "payment",
}
INVOICE_ID_RE = re.compile(r"^INV-[0-9a-f]{12}$")


def handler(event, context):
    tool_full = context.client_context.custom["bedrockAgentCoreToolName"]
    tool = tool_full.split("___", 1)[-1]
    role = ROLE_FOR_TOOL.get(tool)
    if not role:
        return {"error": f"unknown tool {tool}"}

    invoice_id = str(event.get("invoice_id", ""))
    if not INVOICE_ID_RE.match(invoice_id):
        return {"error": "invoice_id must look like INV-xxxxxxxxxxxx"}
    instruction = str(event.get("instruction", ""))[:1500]
    po_number = str(event.get("po_number", ""))[:40]

    text = f"invoice_id={invoice_id}\n"
    if po_number:
        text += f"po_number={po_number}\n"
    text += f"Supervisor request: {instruction}"
    try:
        result = harness_client.invoke(role, text, actor_id=invoice_id)
    except Exception as e:
        log.exception("specialist failed")
        return {"agent": role, "error": str(e)[:500]}
    return {"agent": role, "result": result["text"][-4000:], "stop_reason": result["stop_reason"],
            "hooks": result["hooks"]}
