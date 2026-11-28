"""Shared helpers for AP Guardian Lambda functions.

Everything in this module is deterministic code: no LLM calls. The payment
rules in `pay_invoice` are the last line of defence and are enforced here no
matter what the agents, the gateway policy or the hooks decided upstream.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

ddb = boto3.resource("dynamodb")
ddb_client = boto3.client("dynamodb")

AUTO_PAY_LIMIT_CENTS = int(os.environ.get("AUTO_PAY_LIMIT_CENTS", "200000"))      # $2,000
DAILY_VENDOR_CAP_CENTS = int(os.environ.get("DAILY_VENDOR_CAP_CENTS", "500000"))  # $5,000


def table(env_name):
    return ddb.Table(os.environ[env_name])


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def to_cents(value, lenient=False):
    """Convert a money value (str/float/Decimal/int dollars) to integer cents."""
    if value is None:
        return None
    text = str(value)
    if lenient:
        # Textract returns values like "A$1,000,000.00" or "AUD 1,900.00". Dot decimals only.
        m = re.search(r"\d[\d,]*(?:\.\d+)?|\.\d+", text)
        if not m:
            return None
        text = m.group(0)
    try:
        d = Decimal(text.replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return int((d * 100).to_integral_value())


def json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(f"not serialisable: {type(o)}")


def http(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=json_default),
    }


def parse_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Exceptions (human review) - every "not sure" path ends here.
# ---------------------------------------------------------------------------
_CODE_TEXT = {
    "OVER_AUTO_LIMIT": "The invoice is over the $2,000 automatic payment limit.",
    "DAILY_VENDOR_CAP": "Paying it would take this vendor over the $5,000 daily automatic payment limit.",
    "DUPLICATE_INVOICE": "This vendor invoice number has already been processed.",
    "UNKNOWN_PO": "The purchase order on the invoice does not exist.",
    "PO_NOT_OPEN": "The purchase order is not open.",
    "VENDOR_MISMATCH": "The purchase order belongs to a different vendor than the sender.",
    "PO_BALANCE_EXCEEDED": "The invoice is more than the remaining balance on the purchase order.",
    "VENDOR_NOT_APPROVED": "The vendor is not on the approved list.",
    "BANK_DETAILS_MISMATCH": "The bank details on the invoice do not match the account on file for this vendor.",
    "NO_AMOUNT": "The invoice amount could not be read from the PDF.",
    "AMOUNT_MISMATCH": "The amount the agent tried to pay does not match the invoice total.",
    "WRONG_STATE": "The invoice was already being handled (paid or in review).",
    "ALREADY_PAID": "The invoice has already been paid.",
    "UNKNOWN_INVOICE": "The invoice could not be found.",
}
_GUARD_TEXT = {
    "topic:BankDetailChange": "The email or invoice asks to change or redirect bank account details.",
    "topic:AgentOverride": "The email or invoice contains text trying to instruct the automated system (for example to skip approval or ignore its rules).",
    "topic:MasterDataChange": "The email or invoice asks for vendors or purchase orders to be created, changed or exported.",
    "content:PROMPT_ATTACK": "The email or invoice contains text that looks like an attempt to manipulate the AI agents.",
}


def friendly_reason(raw):
    """Turn technical failure text into a sentence a payment clerk can act on."""
    raw = str(raw or "").strip()
    notes = []
    if "Amount could not be read" in raw:
        notes.append("The amount could not be read from the PDF.")
    if "No PO number found" in raw:
        notes.append("No purchase order number was found on the invoice.")

    if "Guardrail blocked" in raw or "safety hook" in raw:
        hits = []
        for kind, name in re.findall(r"\b(topic|content|word):([A-Za-z_]+)", raw):
            msg = _GUARD_TEXT.get(f"{kind}:{name}")
            if msg and msg not in hits:
                hits.append(msg)
        body = " ".join(hits) if hits else "The safety checks flagged content in the email or invoice."
        return (f"Held for review by the safety checks. {body} "
                "Open the PDF and check the sender before approving. " + " ".join(notes)).strip()[:900]
    if raw.startswith("Agent run failed"):
        return "The automated review could not run because of a system error. A clerk needs to review this invoice. " + " ".join(notes)
    if raw.startswith("Agents did not complete processing"):
        return ("The automated review stopped before reaching a decision, so a clerk needs to review this invoice. "
                + " ".join(notes)).strip()

    text = re.sub(r"^Agent:\s*", "", raw)
    found = [c for c in _CODE_TEXT if c in text]
    if found:
        text = " ".join(_CODE_TEXT[c] for c in found)
        if "Agent:" in raw:
            text = "Sent for review by the payment agent. " + text
    return (text + (" " + " ".join(notes) if notes else "")).strip()[:900]


def open_exception(invoice_id, reason, source="system"):
    """Start the approval workflow for an invoice. Idempotent per invoice.

    `reason` may be technical text. The clerk sees a friendly version and the
    original is kept as exception_detail.
    """
    sfn = boto3.client("stepfunctions")
    detail = str(reason)[:900]
    reason = friendly_reason(detail)
    # Claim the invoice synchronously so concurrent callers cannot open two reviews
    # and a paid invoice can never be re-opened.
    try:
        table("INVOICES_TABLE").update_item(
            Key={"invoice_id": invoice_id},
            UpdateExpression="SET #s = :e, exception_reason = :r, exception_detail = :d",
            ConditionExpression="attribute_exists(invoice_id) AND NOT #s IN (:paid, :e)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":e": "EXCEPTION", ":paid": "PAID", ":r": str(reason)[:900], ":d": detail},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {"status": "ALREADY_PAID_OR_IN_REVIEW", "invoice_id": invoice_id}
        raise
    try:
        sfn.start_execution(
            stateMachineArn=os.environ["APPROVAL_STATE_MACHINE_ARN"],
            name=f"{invoice_id}-{uuid.uuid4().hex[:6]}",
            input=json.dumps({"invoice_id": invoice_id, "reason": str(reason)[:900], "source": source}),
        )
    except ClientError as e:
        log.error("failed to open exception for %s: %s", invoice_id, e)
        raise
    return {"status": "EXCEPTION_OPENED", "invoice_id": invoice_id, "reason": str(reason)[:900]}


def exception_already_open(invoice_id):
    item = table("EXCEPTIONS_TABLE").get_item(Key={"exception_id": invoice_id}).get("Item")
    return item is not None and item.get("status") == "PENDING"


# ---------------------------------------------------------------------------
# Payment core
# ---------------------------------------------------------------------------
class PaymentRejected(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _bank_mismatch(invoice, vendor):
    """True when the invoice quotes bank details that differ from vendor master.

    Bank details are NEVER taken from an invoice. If an invoice mentions an
    account that is not the one on file, that is the classic payment-redirect
    fraud signal, so we stop and hand to a human.
    """
    quoted = (invoice.get("invoice_bank_details") or "").replace(" ", "")
    if not quoted:
        return False
    on_file_last4 = str(vendor.get("bank_account_last4", ""))
    return bool(on_file_last4) and on_file_last4 not in quoted


def pay_invoice(invoice_id, po_number, claimed_amount_cents, approved_by=None):
    """Validate and record a (simulated) payment atomically.

    approved_by is set only by the human approval workflow. When it is None the
    auto-pay limit and the daily vendor cap apply.
    """
    invoices = table("INVOICES_TABLE")
    pos = table("POS_TABLE")
    vendors = table("VENDORS_TABLE")

    inv = invoices.get_item(Key={"invoice_id": invoice_id}).get("Item")
    if not inv:
        raise PaymentRejected("UNKNOWN_INVOICE", "No such invoice id")
    if inv.get("status") == "PAID":
        raise PaymentRejected("ALREADY_PAID", "Invoice already paid")
    expected_status = "EXCEPTION" if approved_by else "EXTRACTED"
    if inv.get("status") != expected_status:
        raise PaymentRejected("WRONG_STATE",
                              f"Invoice is {inv.get('status')}; only {expected_status} invoices can be paid on this path")

    actual_cents = int(inv.get("amount_cents") or 0)
    if actual_cents <= 0:
        raise PaymentRejected("NO_AMOUNT", "Invoice amount could not be extracted")
    if int(claimed_amount_cents) != actual_cents:
        raise PaymentRejected("AMOUNT_MISMATCH",
                              f"Requested {claimed_amount_cents} cents but invoice total is {actual_cents} cents")

    if approved_by is None and actual_cents > AUTO_PAY_LIMIT_CENTS:
        raise PaymentRejected("OVER_AUTO_LIMIT",
                              f"Invoice exceeds auto-pay limit of {AUTO_PAY_LIMIT_CENTS} cents")

    po = pos.get_item(Key={"po_number": po_number}).get("Item")
    if not po:
        raise PaymentRejected("UNKNOWN_PO", "Purchase order not found")
    if po.get("status") != "OPEN":
        raise PaymentRejected("PO_NOT_OPEN", f"PO status is {po.get('status')}")
    if po.get("vendor_id") != inv.get("vendor_id"):
        raise PaymentRejected("VENDOR_MISMATCH", "PO vendor does not match the authenticated sender's vendor")
    if int(po.get("amount_remaining_cents", 0)) < actual_cents:
        raise PaymentRejected("PO_BALANCE_EXCEEDED", "Invoice exceeds remaining PO balance")

    vendor = vendors.get_item(Key={"vendor_id": inv["vendor_id"]}).get("Item")
    if not vendor or vendor.get("status") != "APPROVED":
        raise PaymentRejected("VENDOR_NOT_APPROVED", "Vendor is not on the approved list")
    if approved_by is None and _bank_mismatch(inv, vendor):
        raise PaymentRejected("BANK_DETAILS_MISMATCH",
                              "Invoice quotes bank details that differ from vendor master; human verification required")

    vendor_id = inv["vendor_id"]
    vinv = inv.get("vendor_invoice_number") or invoice_id
    payment_id = f"PAY-{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    day = today()
    cap_remaining = DAILY_VENDOR_CAP_CENTS - actual_cents

    ledger_name = os.environ["LEDGER_TABLE"]
    items = [
        # 0: duplicate guard on the vendor's own invoice number
        {"Put": {
            "TableName": ledger_name,
            "Item": {"pk": {"S": f"VENDOR#{vendor_id}"}, "sk": {"S": f"VINV#{vinv}"},
                     "invoice_id": {"S": invoice_id}, "created_at": {"S": ts}},
            "ConditionExpression": "attribute_not_exists(pk)",
        }},
        # 1: rolling daily total per vendor (cap only enforced for auto-pay)
        {"Update": {
            "TableName": ledger_name,
            "Key": {"pk": {"S": f"VENDOR#{vendor_id}"}, "sk": {"S": f"DAILY#{day}"}},
            "UpdateExpression": "SET total_cents = if_not_exists(total_cents, :zero) + :amt",
            **({} if approved_by else {
                "ConditionExpression": "attribute_not_exists(total_cents) OR total_cents <= :capleft"}),
            "ExpressionAttributeValues": {
                ":zero": {"N": "0"}, ":amt": {"N": str(actual_cents)},
                **({} if approved_by else {":capleft": {"N": str(cap_remaining)}}),
            },
        }},
        # 2: draw down the PO balance
        {"Update": {
            "TableName": os.environ["POS_TABLE"],
            "Key": {"po_number": {"S": po_number}},
            "UpdateExpression": "SET amount_remaining_cents = amount_remaining_cents - :amt",
            "ConditionExpression": "amount_remaining_cents >= :amt AND #s = :open",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":amt": {"N": str(actual_cents)}, ":open": {"S": "OPEN"}},
        }},
        # 3: mark invoice paid exactly once
        {"Update": {
            "TableName": os.environ["INVOICES_TABLE"],
            "Key": {"invoice_id": {"S": invoice_id}},
            "UpdateExpression": "SET #s = :paid, payment_id = :pid, paid_at = :ts, approved_by = :ab",
            "ConditionExpression": "#s = :expected",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":paid": {"S": "PAID"}, ":pid": {"S": payment_id},
                                          ":ts": {"S": ts}, ":ab": {"S": approved_by or "auto"},
                                          ":expected": {"S": expected_status}},
        }},
        # 4: ledger entry. Payee comes from vendor master, never from the invoice.
        {"Put": {
            "TableName": ledger_name,
            "Item": {"pk": {"S": f"VENDOR#{vendor_id}"}, "sk": {"S": f"PAY#{ts}#{invoice_id}"},
                     "payment_id": {"S": payment_id}, "invoice_id": {"S": invoice_id},
                     "po_number": {"S": po_number}, "amount_cents": {"N": str(actual_cents)},
                     "payee_bank_ref": {"S": str(vendor.get("bank_ref", "on-file"))},
                     "approved_by": {"S": approved_by or "auto"}, "created_at": {"S": ts},
                     "record_type": {"S": "PAYMENT"}},
        }},
    ]
    reasons = {0: ("DUPLICATE_INVOICE", "This vendor invoice number has already been processed"),
               1: ("DAILY_VENDOR_CAP", f"Payment would exceed the daily cap of {DAILY_VENDOR_CAP_CENTS} cents for this vendor"),
               2: ("PO_BALANCE_EXCEEDED", "Invoice exceeds remaining PO balance"),
               3: ("WRONG_STATE", "Invoice changed state while paying (already paid or in review)")}
    try:
        ddb_client.transact_write_items(TransactItems=items)
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            for idx, r in enumerate(e.response.get("CancellationReasons", [])):
                if r.get("Code") == "ConditionalCheckFailed" and idx in reasons:
                    raise PaymentRejected(*reasons[idx])
        raise
    log.info(json.dumps({"event": "PAYMENT", "payment_id": payment_id, "invoice_id": invoice_id,
                         "amount_cents": actual_cents, "approved_by": approved_by or "auto"}))
    return {"status": "PAID", "payment_id": payment_id, "invoice_id": invoice_id,
            "amount_cents": actual_cents, "approved_by": approved_by or "auto"}
