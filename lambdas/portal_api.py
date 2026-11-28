"""Accounts Portal API (API Gateway + Cognito authorizer). No LLM in this path.

Separate Lambdas / IAM roles:
  create_po        POST /api/pos                          procurement group only
  decide_exception POST /api/exceptions/{id}/decision     ap-clerk group only
  read_only        GET  /api/pos | /api/invoices | /api/invoices/{id}
                        /api/exceptions | /api/ledger     any signed-in user
"""
import json
import os
import random
import re
import string

import boto3
from boto3.dynamodb.conditions import Attr

from common import http, parse_body, table, now_iso, to_cents, log

MAX_PO_CENTS = 100_000_000  # $1,000,000


def _claims(event):
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def _groups(event):
    g = _claims(event).get("cognito:groups", "")
    if isinstance(g, list):
        return set(g)
    return set(x.strip() for x in str(g).strip("[]").replace(",", " ").split() if x.strip())


def _who(event):
    c = _claims(event)
    return c.get("email") or c.get("cognito:username") or c.get("sub", "unknown")


# --------------------------------------------------------------------------- create_po
def create_po(event, _ctx):
    if "procurement" not in _groups(event):
        return http(403, {"error": "procurement group required"})
    body = parse_body(event)
    if not isinstance(body, dict):
        return http(400, {"error": "JSON body required"})
    vendor_id = str(body.get("vendor_id", ""))
    cents = to_cents(body.get("amount"))
    description = str(body.get("description", ""))[:500]
    if not cents or cents <= 0 or cents > MAX_PO_CENTS:
        return http(400, {"error": "amount must be between 0.01 and 1,000,000"})
    vendor = table("VENDORS_TABLE").get_item(Key={"vendor_id": vendor_id}).get("Item")
    if not vendor or vendor.get("status") != "APPROVED":
        return http(400, {"error": "vendor not approved"})

    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    po_number = f"PO-{now_iso()[:10].replace('-', '')}-{suffix}"
    item = {"po_number": po_number, "vendor_id": vendor_id, "amount_total_cents": cents,
            "amount_remaining_cents": cents, "description": description, "status": "OPEN",
            "created_by": _who(event), "created_at": now_iso()}
    table("POS_TABLE").put_item(Item=item, ConditionExpression="attribute_not_exists(po_number)")

    emailed = False
    email_error = None
    sender = os.environ.get("PO_FROM_ADDRESS", "")
    if not sender:
        email_error = "PO_FROM_ADDRESS is not set on the Lambda (redeploy with -c po_from_address=...)"
    elif not vendor.get("contact_email"):
        email_error = "vendor has no contact_email"
    else:
        try:
            boto3.client("ses").send_email(
                Source=sender,
                Destination={"ToAddresses": [vendor["contact_email"]]},
                Message={
                    "Subject": {"Data": f"Purchase Order {po_number}"},
                    "Body": {"Text": {"Data": (
                        f"Purchase Order: {po_number}\nVendor: {vendor.get('name')}\n"
                        f"Amount: ${cents / 100:,.2f}\nDescription: {description}\n\n"
                        f"Please quote {po_number} on your invoice and email it to "
                        f"{os.environ.get('INVOICE_INBOX', 'our AP inbox')}.")}},
                })
            emailed = True
        except Exception as e:
            log.warning("PO email failed: %s", e)
            email_error = str(e)[:300]
    return http(201, {**item, "emailed": emailed, "email_error": email_error})


# --------------------------------------------------------------------------- vendors
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def add_vendor(event, _ctx):
    """POST /api/vendors  (procurement only). Creates an APPROVED vendor."""
    if "procurement" not in _groups(event):
        return http(403, {"error": "procurement group required"})
    body = parse_body(event)
    if not isinstance(body, dict):
        return http(400, {"error": "JSON body required"})
    name = str(body.get("name", "")).strip()[:120]
    email = str(body.get("contact_email", "")).strip().lower()[:200]
    last4 = str(body.get("bank_account_last4", "")).strip()
    raw_domains = body.get("sender_domains", "")
    if isinstance(raw_domains, str):
        raw_domains = raw_domains.replace(";", ",").split(",")
    domains = sorted({str(d).strip().lower() for d in raw_domains if str(d).strip()})
    if not name:
        return http(400, {"error": "name is required"})
    m = _EMAIL_RE.match(email)
    if not m:
        return http(400, {"error": "valid contact_email is required"})
    if not re.fullmatch(r"\d{4}", last4):
        return http(400, {"error": "bank_account_last4 must be exactly 4 digits"})
    if not domains or len(domains) > 10 or not all(_DOMAIN_RE.match(d) for d in domains):
        return http(400, {"error": "sender_domains must be 1-10 valid domains, e.g. acme.com"})

    vendor_id = "V-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    item = {"vendor_id": vendor_id, "name": name, "status": "APPROVED", "sender_domains": domains,
            "contact_email": email, "bank_account_last4": last4, "bank_ref": f"{vendor_id}-on-file-****{last4}",
            "created_by": _who(event), "created_at": now_iso()}
    table("VENDORS_TABLE").put_item(Item=item, ConditionExpression="attribute_not_exists(vendor_id)")
    log.info("VENDOR_ADDED %s by %s", vendor_id, _who(event))
    return http(201, {k: item[k] for k in ("vendor_id", "name", "status", "contact_email", "sender_domains")})


def remove_vendor(event, _ctx):
    """DELETE /api/vendors/{id}  (procurement only). Refused while the vendor has open POs."""
    if "procurement" not in _groups(event):
        return http(403, {"error": "procurement group required"})
    vendor_id = (event.get("pathParameters") or {}).get("id", "")
    vt = table("VENDORS_TABLE")
    if not vt.get_item(Key={"vendor_id": vendor_id}).get("Item"):
        return http(404, {"error": "vendor not found"})
    open_pos = table("POS_TABLE").scan(
        FilterExpression=Attr("vendor_id").eq(vendor_id) & Attr("status").eq("OPEN"),
        ProjectionExpression="po_number").get("Items", [])
    if open_pos:
        return http(409, {"error": f"vendor has {len(open_pos)} open purchase order(s); close them first"})
    vt.delete_item(Key={"vendor_id": vendor_id})
    log.info("VENDOR_REMOVED %s by %s", vendor_id, _who(event))
    return http(200, {"vendor_id": vendor_id, "removed": True})


# --------------------------------------------------------------------------- decide_exception
def decide_exception(event, _ctx):
    if "ap-clerk" not in _groups(event):
        return http(403, {"error": "ap-clerk group required"})
    exc_id = (event.get("pathParameters") or {}).get("id", "")
    body = parse_body(event) or {}
    decision = str(body.get("decision", "")).upper()
    # PROCEED = proceed to payment, DECLINE = decline, DELETE = remove from the queue.
    decision = {"PROCEED": "APPROVED", "DECLINE": "REJECTED"}.get(decision, decision)
    if decision not in ("APPROVED", "REJECTED", "DELETE"):
        return http(400, {"error": "decision must be PROCEED, DECLINE or DELETE"})
    exc = table("EXCEPTIONS_TABLE").get_item(Key={"exception_id": exc_id}).get("Item")
    if not exc:
        return http(404, {"error": "exception not found"})
    who = _who(event)
    sfn = boto3.client("stepfunctions")

    if decision == "DELETE":
        return _delete_exception(exc, exc_id, who, sfn)

    if exc.get("status") not in ("PENDING", "DECIDING"):
        return http(409, {"error": f"this item is already {exc.get('status')}"})
    # Claim it first so a double-click cannot send the token twice. A stuck DECIDING item can be retried.
    table("EXCEPTIONS_TABLE").update_item(
        Key={"exception_id": exc_id}, UpdateExpression="SET #s = :d",
        ConditionExpression="#s IN (:p, :d)", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "DECIDING", ":p": "PENDING"})
    payload = {"decision": decision, "decided_by": who, "note": str(body.get("note", ""))[:300]}
    try:
        try:
            sfn.send_task_success(taskToken=exc["task_token"], output=json.dumps(payload))
        except (sfn.exceptions.TaskDoesNotExist, sfn.exceptions.TaskTimedOut, sfn.exceptions.InvalidToken):
            # The workflow has ended or timed out. Run the same final step directly so the item never gets stuck.
            log.warning("task token no longer valid for %s; finalizing directly", exc_id)
            resp = boto3.client("lambda").invoke(
                FunctionName=os.environ["FINALIZE_FN"], InvocationType="RequestResponse",
                Payload=json.dumps({"invoice_id": exc.get("invoice_id", exc_id), "decision": payload}).encode())
            if resp.get("FunctionError"):
                raise RuntimeError(resp["Payload"].read().decode()[:300])
    except Exception:
        table("EXCEPTIONS_TABLE").update_item(
            Key={"exception_id": exc_id}, UpdateExpression="SET #s = :p",
            ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":p": "PENDING"})
        raise
    return http(200, {"exception_id": exc_id, "decision": decision, "decided_by": who})


def _delete_exception(exc, exc_id, who, sfn):
    """Remove an item from the queue. A paid invoice is never touched; otherwise it is marked DELETED."""
    invoice_id = exc.get("invoice_id", exc_id)
    if exc.get("status") in ("PENDING", "DECIDING") and exc.get("task_token"):
        try:  # end the waiting workflow; ignore if it has already ended
            sfn.send_task_failure(taskToken=exc["task_token"], error="DeletedByClerk", cause=f"Deleted by {who}")
        except Exception as e:
            log.info("task token already closed for %s: %s", exc_id, e)
    try:
        table("INVOICES_TABLE").update_item(
            Key={"invoice_id": invoice_id},
            UpdateExpression="SET #s = :d, deleted_by = :w, deleted_at = :t",
            ConditionExpression="attribute_exists(invoice_id) AND NOT #s IN (:paid)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "DELETED", ":paid": "PAID", ":w": who, ":t": now_iso()})
    except Exception as e:
        log.info("invoice %s not marked deleted: %s", invoice_id, e)
    table("EXCEPTIONS_TABLE").delete_item(Key={"exception_id": exc_id})
    log.info(json.dumps({"event": "EXCEPTION_DELETED", "exception_id": exc_id, "by": who}))
    return http(200, {"exception_id": exc_id, "decision": "DELETE", "decided_by": who})


# --------------------------------------------------------------------------- read-only
def _scan(env_name, limit=200, **kw):
    return table(env_name).scan(Limit=limit, **kw).get("Items", [])


def read_only(event, _ctx):
    path = event.get("resource", "")
    params = event.get("pathParameters") or {}
    if path == "/api/pos":
        return http(200, sorted(_scan("POS_TABLE"), key=lambda x: x.get("created_at", ""), reverse=True))
    if path == "/api/invoices":
        keep = ("invoice_id", "vendor_id", "vendor_invoice_number", "po_number", "amount_cents",
                "status", "received_at", "exception_reason", "payment_id", "source_key")
        rows = [{k: i.get(k) for k in keep} for i in _scan("INVOICES_TABLE")]
        return http(200, sorted(rows, key=lambda x: x.get("received_at") or "", reverse=True))
    if path == "/api/invoices/{id}":
        inv = table("INVOICES_TABLE").get_item(Key={"invoice_id": params.get("id", "")}).get("Item")
        if not inv:
            return http(404, {"error": "not found"})
        url = boto3.client("s3").generate_presigned_url(
            "get_object", Params={"Bucket": os.environ["PDFS_BUCKET"], "Key": inv["pdf_s3_key"],
                                  "ResponseContentType": "application/pdf"},
            ExpiresIn=300)
        return http(200, {**inv, "pdf_url": url, "pdf_url_expires_in": 300})
    if path == "/api/exceptions":
        rows = _scan("EXCEPTIONS_TABLE")
        for r in rows:
            r.pop("task_token", None)
        return http(200, sorted(rows, key=lambda x: x.get("created_at", ""), reverse=True))
    if path == "/api/ledger":
        rows = _scan("LEDGER_TABLE", FilterExpression=Attr("record_type").eq("PAYMENT"))
        return http(200, sorted(rows, key=lambda x: x.get("created_at", ""), reverse=True))
    if path == "/api/vendors":
        keep = ("vendor_id", "name", "status", "contact_email", "sender_domains")
        return http(200, [{k: v.get(k) for k in keep} for v in _scan("VENDORS_TABLE")])
    return http(404, {"error": "unknown route"})
