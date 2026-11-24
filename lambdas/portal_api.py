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
    sender = os.environ.get("PO_FROM_ADDRESS", "")
    if sender and vendor.get("contact_email"):
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
    return http(201, {**item, "emailed": emailed})


# --------------------------------------------------------------------------- decide_exception
def decide_exception(event, _ctx):
    if "ap-clerk" not in _groups(event):
        return http(403, {"error": "ap-clerk group required"})
    exc_id = (event.get("pathParameters") or {}).get("id", "")
    body = parse_body(event) or {}
    decision = str(body.get("decision", "")).upper()
    if decision not in ("APPROVED", "REJECTED"):
        return http(400, {"error": "decision must be APPROVED or REJECTED"})
    exc = table("EXCEPTIONS_TABLE").get_item(Key={"exception_id": exc_id}).get("Item")
    if not exc or exc.get("status") != "PENDING":
        return http(404, {"error": "no pending exception"})
    who = _who(event)
    # Claim it first so a double-click cannot send the token twice.
    table("EXCEPTIONS_TABLE").update_item(
        Key={"exception_id": exc_id}, UpdateExpression="SET #s = :d",
        ConditionExpression="#s = :p", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":d": "DECIDING", ":p": "PENDING"})
    boto3.client("stepfunctions").send_task_success(
        taskToken=exc["task_token"],
        output=json.dumps({"decision": decision, "decided_by": who, "note": str(body.get("note", ""))[:300]}))
    return http(200, {"exception_id": exc_id, "decision": decision, "decided_by": who})


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
        keep = ("vendor_id", "name", "status")
        return http(200, [{k: v.get(k) for k in keep} for v in _scan("VENDORS_TABLE")])
    return http(404, {"error": "unknown route"})
