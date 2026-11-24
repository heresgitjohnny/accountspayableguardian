#!/usr/bin/env python3
"""Seed demo vendors and purchase orders.

Vendors are master data: there is no API or agent tool that can create or
change them, so they are loaded here by an operator. POs would normally be
raised in the portal; the seed POs exist so the stress tests are repeatable.

  python scripts/seed_data.py               # create if missing
  python scripts/seed_data.py --reset       # reset PO balances, daily caps, test invoices
"""
import argparse
import json
from datetime import datetime, timezone

import boto3

VENDORS = [
    {"vendor_id": "V-ACME", "name": "Acme Office Supplies", "status": "APPROVED",
     "sender_domains": ["acme-supplies.example"], "contact_email": "accounts@acme-supplies.example",
     "bank_account_last4": "4321", "bank_ref": "ACME-on-file-****4321"},
    {"vendor_id": "V-BOLT", "name": "Bolt Industrial Parts", "status": "APPROVED",
     "sender_domains": ["bolt-industrial.example"], "contact_email": "billing@bolt-industrial.example",
     "bank_account_last4": "7788", "bank_ref": "BOLT-on-file-****7788"},
    {"vendor_id": "V-SHADY", "name": "Suspended Vendor Pty", "status": "SUSPENDED",
     "sender_domains": ["shady-vendor.example"], "contact_email": "x@shady-vendor.example",
     "bank_account_last4": "0000", "bank_ref": "SUSPENDED"},
]
POS = [
    ("PO-SEED-0001", "V-ACME", 1_000_000, "Stationery and printer supplies FY27"),
    ("PO-SEED-0002", "V-ACME", 2_000_000, "Office furniture fit-out"),
    ("PO-SEED-0003", "V-BOLT", 1_000_000, "Maintenance spare parts"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", default="cdk-outputs.json")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    out = json.load(open(args.outputs))["ApGuardianStack"]
    ddb = boto3.resource("dynamodb", region_name=out["Region"])
    vendors, pos = ddb.Table(out["VendorsTable"]), ddb.Table(out["PosTable"])
    now = datetime.now(timezone.utc).isoformat()

    for v in VENDORS:
        vendors.put_item(Item=v)
    for po_number, vendor_id, cents, desc in POS:
        item = {"po_number": po_number, "vendor_id": vendor_id, "amount_total_cents": cents,
                "amount_remaining_cents": cents, "description": desc, "status": "OPEN",
                "created_by": "seed-script", "created_at": now}
        if args.reset:
            pos.put_item(Item=item)
        else:
            try:
                pos.put_item(Item=item, ConditionExpression="attribute_not_exists(po_number)")
            except ddb.meta.client.exceptions.ConditionalCheckFailedException:
                pass
    if args.reset:
        ledger = ddb.Table(out["LedgerTable"])
        for it in ledger.scan().get("Items", []):
            ledger.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
        for name in ("InvoicesTable", "ExceptionsTable"):
            t = ddb.Table(out[name])
            key = "invoice_id" if name == "InvoicesTable" else "exception_id"
            for it in t.scan().get("Items", []):
                t.delete_item(Key={key: it[key]})
        print("Reset ledger, invoices, exceptions and PO balances.")
    print(f"Seeded {len(VENDORS)} vendors and {len(POS)} POs.")


if __name__ == "__main__":
    main()
