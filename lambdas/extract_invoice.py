"""Textract AnalyzeExpense on a new invoice PDF -> Invoices table.

The vendor_id comes from the S3 object metadata written by ingest_email, i.e.
from the *authenticated sender*, never from text printed on the invoice.
Writing the row (status EXTRACTED) fires the table stream that starts the agents.
"""
import json
from urllib.parse import unquote_plus

import boto3

from common import log, table, to_cents, now_iso

s3 = boto3.client("s3")
textract = boto3.client("textract")

FIELD_MAP = {
    "INVOICE_RECEIPT_ID": "vendor_invoice_number",
    "PO_NUMBER": "po_number",
    "INVOICE_RECEIPT_DATE": "invoice_date",
    "VENDOR_NAME": "vendor_name_on_invoice",
    "DUE_DATE": "due_date",
}
AMOUNT_TYPES = ("AMOUNT_DUE", "TOTAL")
BANK_HINTS = ("account", "bsb", "iban", "swift", "routing", "bank")


def _summary_fields(resp):
    fields = []
    for doc in resp.get("ExpenseDocuments", []):
        for f in doc.get("SummaryFields", []):
            ftype = f.get("Type", {}).get("Text", "")
            label = (f.get("LabelDetection") or {}).get("Text", "")
            value = (f.get("ValueDetection") or {}).get("Text", "")
            conf = (f.get("ValueDetection") or {}).get("Confidence", 0)
            fields.append((ftype, label, value, conf))
    return fields


def _process(bucket, key):
    head = s3.head_object(Bucket=bucket, Key=key)
    meta = head.get("Metadata", {})
    invoice_id = meta["invoice-id"]

    resp = textract.analyze_expense(Document={"S3Object": {"Bucket": bucket, "Name": key}})
    item = {
        "invoice_id": invoice_id,
        "vendor_id": meta["vendor-id"],
        "sender": meta.get("sender", ""),
        "pdf_s3_key": key,
        "email_body_key": meta.get("body-key", ""),
        "source_key": meta.get("source-key", ""),
        "received_at": meta.get("received-at", now_iso()),
        "extracted_at": now_iso(),
        "status": "EXTRACTED",
    }
    other, bank_bits = {}, []
    amount_conf = 0
    for ftype, label, value, conf in _summary_fields(resp):
        if ftype in FIELD_MAP and value and FIELD_MAP[ftype] not in item:
            item[FIELD_MAP[ftype]] = value.strip()[:100]
        elif ftype in AMOUNT_TYPES and value and "amount_cents" not in item:
            cents = to_cents(value)
            if cents is not None:
                item["amount_cents"] = cents
                amount_conf = conf
        elif value:
            lbl = (label or ftype or "field").strip()[:60]
            if any(h in lbl.lower() for h in BANK_HINTS):
                bank_bits.append(f"{lbl}: {value}"[:120])
            elif len(other) < 25:
                other[lbl] = value.strip()[:300]
    if bank_bits:
        item["invoice_bank_details"] = " | ".join(bank_bits)[:400]
    item["other_fields"] = other
    item["amount_confidence"] = int(amount_conf)
    if "po_number" in item:
        item["po_number"] = item["po_number"].upper().replace(" ", "")

    table("INVOICES_TABLE").put_item(Item=item, ConditionExpression="attribute_not_exists(invoice_id)")
    log.info(json.dumps({"event": "EXTRACTED", "invoice_id": invoice_id,
                         "amount_cents": item.get("amount_cents"), "po": item.get("po_number")}))
    return invoice_id


def handler(event, _ctx):
    out = []
    for rec in event.get("Records", []):
        out.append(_process(rec["s3"]["bucket"]["name"], unquote_plus(rec["s3"]["object"]["key"])))
    return out
