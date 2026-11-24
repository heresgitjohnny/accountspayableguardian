"""Layer 3: email gate. Triggered by SES writing a raw email to the landing bucket.

Checks sender authentication and the vendor allow-list, then splits the email:
  * PDF attachments -> Invoice PDFs bucket (tagged with the *authenticated* vendor)
  * plain-text body -> Email body bucket (clean emails only)
Anything that fails is copied to quarantine/ and never reaches an agent.

This function has no permission to invoke agents.
"""
import email
import email.policy
import email.utils
import json
import os
import re
import uuid
from urllib.parse import unquote_plus

import boto3

from common import log, table, now_iso

s3 = boto3.client("s3")

MAX_PDFS = 3
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_BODY_CHARS = 8000


def _auth_ok(msg):
    """Read the TOP-MOST Authentication-Results header (the one SES prepends).

    Headers lower in the message can be forged by the sender, so only the first
    one is trusted.
    """
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False, "no Authentication-Results header"
    top = str(results[0]).lower()
    spf = re.search(r"\bspf=(\w+)", top)
    dkim = re.search(r"\bdkim=(\w+)", top)
    dmarc = re.search(r"\bdmarc=(\w+)", top)
    if dmarc and dmarc.group(1) == "pass":
        return True, "dmarc=pass"
    if spf and dkim and spf.group(1) == "pass" and dkim.group(1) == "pass":
        return True, "spf+dkim pass"
    return False, f"auth failed: {top[:200]}"


def _verdicts_ok(msg):
    for h in ("X-SES-Spam-Verdict", "X-SES-Virus-Verdict"):
        v = msg.get(h)
        if v is not None and str(v).strip().upper() != "PASS":
            return False, f"{h}={v}"
    return True, "ok"


def _vendor_for_domain(domain):
    # Small vendor master; scan is fine for a demo. Use a GSI in production.
    vendors = table("VENDORS_TABLE").scan().get("Items", [])
    for v in vendors:
        if v.get("status") == "APPROVED" and domain in [d.lower() for d in v.get("sender_domains", [])]:
            return v
    return None


def _quarantine(bucket, key, reason):
    qkey = "quarantine/" + key.split("/", 1)[-1]
    s3.copy_object(Bucket=bucket, Key=qkey, CopySource={"Bucket": bucket, "Key": key},
                   Metadata={"reason": reason[:200]}, MetadataDirective="REPLACE")
    log.warning(json.dumps({"event": "QUARANTINED", "key": key, "reason": reason}))
    return {"status": "QUARANTINED", "reason": reason}


def _process(bucket, key):
    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    ok, why = _auth_ok(msg)
    if not ok:
        return _quarantine(bucket, key, why)
    ok, why = _verdicts_ok(msg)
    if not ok:
        return _quarantine(bucket, key, why)

    from_addr = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
    domain = from_addr.split("@")[-1] if "@" in from_addr else ""
    vendor = _vendor_for_domain(domain)
    if not vendor:
        return _quarantine(bucket, key, f"sender domain not on vendor allow-list: {domain}")

    body_part = msg.get_body(preferencelist=("plain",))
    body_text = body_part.get_content()[:MAX_BODY_CHARS] if body_part else ""
    message_id = uuid.uuid4().hex
    body_key = f"bodies/{message_id}.txt"
    s3.put_object(Bucket=os.environ["BODIES_BUCKET"], Key=body_key, Body=body_text.encode(),
                  ContentType="text/plain")

    pdfs = [p for p in msg.iter_attachments() if p.get_content_type() == "application/pdf"]
    if not pdfs:
        return _quarantine(bucket, key, "no PDF invoice attached")

    created = []
    for part in pdfs[:MAX_PDFS]:
        data = part.get_content()
        if len(data) > MAX_PDF_BYTES:
            log.warning("skipping oversized attachment %s", part.get_filename())
            continue
        invoice_id = f"INV-{uuid.uuid4().hex[:12]}"
        s3.put_object(
            Bucket=os.environ["PDFS_BUCKET"], Key=f"invoices/{invoice_id}.pdf", Body=data,
            ContentType="application/pdf",
            Metadata={"invoice-id": invoice_id, "vendor-id": vendor["vendor_id"],
                      "sender": from_addr, "body-key": body_key, "source-key": key,
                      "received-at": now_iso()},
        )
        created.append(invoice_id)
    log.info(json.dumps({"event": "INGESTED", "key": key, "vendor_id": vendor["vendor_id"], "invoices": created}))
    return {"status": "INGESTED", "invoices": created}


def handler(event, _ctx):
    out = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = unquote_plus(rec["s3"]["object"]["key"])
        try:
            out.append(_process(bucket, key))
        except Exception as e:  # fail closed
            log.exception("ingest failed")
            out.append(_quarantine(bucket, key, f"processing error: {e}"))
    return out
