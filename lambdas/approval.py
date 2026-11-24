"""Human-in-the-loop approval workflow steps (Step Functions).

notify   : waitForTaskToken step. Records the exception (with its task token),
           and alerts the payment clerk by SNS. The clerk must log in to the
           portal (Cognito + MFA) to decide.
finalize : runs after the clerk decides. APPROVED -> pays via the same payment
           core (limit/cap lifted, every other rule still enforced).
"""
import json
import os

import boto3

from common import table, now_iso, log, pay_invoice, PaymentRejected

sns = boto3.client("sns")


def notify(event, _ctx):
    invoice_id = event["invoice_id"]
    inv = table("INVOICES_TABLE").get_item(Key={"invoice_id": invoice_id}).get("Item") or {}
    table("EXCEPTIONS_TABLE").put_item(Item={
        "exception_id": invoice_id,
        "invoice_id": invoice_id,
        "reason": event.get("reason", ""),
        "source": event.get("source", ""),
        "task_token": event["token"],
        "status": "PENDING",
        "amount_cents": inv.get("amount_cents", 0),
        "vendor_id": inv.get("vendor_id", ""),
        "po_number": inv.get("po_number", ""),
        "created_at": now_iso(),
    })
    sns.publish(
        TopicArn=os.environ["CLERK_TOPIC_ARN"],
        Subject=f"AP exception needs review: {invoice_id}",
        Message=(f"Invoice {invoice_id} needs a decision.\nReason: {event.get('reason')}\n"
                 f"Amount (cents): {inv.get('amount_cents')}\nLog in to the AP portal to approve or reject."),
    )
    return {"ok": True}


def finalize(event, _ctx):
    invoice_id = event["invoice_id"]
    decision = (event.get("decision") or {})
    verdict = decision.get("decision")
    who = decision.get("decided_by", "unknown")
    inv = table("INVOICES_TABLE").get_item(Key={"invoice_id": invoice_id}).get("Item") or {}
    result = {"invoice_id": invoice_id, "decision": verdict, "decided_by": who}
    if verdict == "APPROVED":
        try:
            result["payment"] = pay_invoice(invoice_id, inv.get("po_number", ""),
                                            int(inv.get("amount_cents", 0)), approved_by=who)
            final = "APPROVED_PAID"
        except PaymentRejected as e:
            result["payment_error"] = e.code
            final = "APPROVED_BUT_BLOCKED"
            table("INVOICES_TABLE").update_item(
                Key={"invoice_id": invoice_id}, UpdateExpression="SET block_reason = :b",
                ExpressionAttributeValues={":b": f"{e.code}: {e.detail}"})
    else:
        final = "REJECTED"
        table("INVOICES_TABLE").update_item(
            Key={"invoice_id": invoice_id}, UpdateExpression="SET #s = :r, rejected_by = :w",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":r": "REJECTED", ":w": who})
    table("EXCEPTIONS_TABLE").update_item(
        Key={"exception_id": invoice_id},
        UpdateExpression="SET #s = :s, decided_by = :w, decided_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": final, ":w": who, ":t": now_iso()})
    log.info(json.dumps({"event": "EXCEPTION_FINALIZED", **result}, default=str))
    return result
