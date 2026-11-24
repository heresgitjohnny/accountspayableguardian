"""Offline tests for the hard (deterministic) layers. Run: pytest -q tests/"""
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lambdas"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

ENV = {"VENDORS_TABLE": "vendors", "POS_TABLE": "pos", "INVOICES_TABLE": "invoices",
       "LEDGER_TABLE": "ledger", "EXCEPTIONS_TABLE": "exceptions", "AWS_DEFAULT_REGION": "us-east-1",
       "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "PDFS_BUCKET": "pdfs",
       "BODIES_BUCKET": "bodies", "APPROVAL_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:x"}
os.environ.update(ENV)


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.client("dynamodb")
        for name, keys in [("vendors", ["vendor_id"]), ("pos", ["po_number"]), ("invoices", ["invoice_id"]),
                           ("exceptions", ["exception_id"]), ("ledger", ["pk", "sk"])]:
            ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                             KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in zip(keys, ["HASH", "RANGE"])],
                             AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys])
        res = boto3.resource("dynamodb")
        res.Table("vendors").put_item(Item={"vendor_id": "V-ACME", "status": "APPROVED", "bank_account_last4": "4321",
                                            "sender_domains": ["acme-supplies.example"], "bank_ref": "on-file"})
        res.Table("vendors").put_item(Item={"vendor_id": "V-BOLT", "status": "APPROVED", "bank_account_last4": "7788",
                                            "sender_domains": ["bolt-industrial.example"]})
        for po, v, c in [("PO-SEED-0001", "V-ACME", 1_000_000), ("PO-SEED-0003", "V-BOLT", 1_000_000),
                         ("PO-SMALL-0001", "V-ACME", 50_000)]:
            res.Table("pos").put_item(Item={"po_number": po, "vendor_id": v, "amount_total_cents": c,
                                            "amount_remaining_cents": c, "status": "OPEN"})
        import importlib
        import common
        importlib.reload(common)
        yield res, common


def add_invoice(res, inv_id, cents, vendor="V-ACME", vinv=None, bank=None, status="EXTRACTED"):
    item = {"invoice_id": inv_id, "vendor_id": vendor, "amount_cents": cents, "status": status,
            "vendor_invoice_number": vinv or inv_id}
    if bank:
        item["invoice_bank_details"] = bank
    res.Table("invoices").put_item(Item=item)


def test_happy_path_pays_and_draws_down_po(aws):
    res, common = aws
    add_invoice(res, "INV-000000000001", 125_000)
    r = common.pay_invoice("INV-000000000001", "PO-SEED-0001", 125_000)
    assert r["status"] == "PAID"
    po = res.Table("pos").get_item(Key={"po_number": "PO-SEED-0001"})["Item"]
    assert po["amount_remaining_cents"] == 1_000_000 - 125_000
    assert res.Table("invoices").get_item(Key={"invoice_id": "INV-000000000001"})["Item"]["status"] == "PAID"


@pytest.mark.parametrize("cents,claimed,code", [
    (250_000, 250_000, "OVER_AUTO_LIMIT"),
    (100_000, 90_000, "AMOUNT_MISMATCH"),
])
def test_limit_and_amount_mismatch(aws, cents, claimed, code):
    res, common = aws
    add_invoice(res, "INV-000000000002", cents)
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000002", "PO-SEED-0001", claimed)
    assert e.value.code == code


def test_split_invoices_hit_daily_cap(aws):
    res, common = aws
    for i in range(3):
        add_invoice(res, f"INV-00000000010{i}", 190_000, vendor="V-BOLT", vinv=f"B-{i}")
    assert common.pay_invoice("INV-000000000100", "PO-SEED-0003", 190_000)["status"] == "PAID"
    assert common.pay_invoice("INV-000000000101", "PO-SEED-0003", 190_000)["status"] == "PAID"
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000102", "PO-SEED-0003", 190_000)
    assert e.value.code == "DAILY_VENDOR_CAP"
    # nothing partially applied for the rejected one
    po = res.Table("pos").get_item(Key={"po_number": "PO-SEED-0003"})["Item"]
    assert po["amount_remaining_cents"] == 1_000_000 - 380_000


def test_duplicate_vendor_invoice_number(aws):
    res, common = aws
    add_invoice(res, "INV-000000000201", 50_000, vinv="ACME-1001")
    add_invoice(res, "INV-000000000202", 50_000, vinv="ACME-1001")
    common.pay_invoice("INV-000000000201", "PO-SEED-0001", 50_000)
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000202", "PO-SEED-0001", 50_000)
    assert e.value.code == "DUPLICATE_INVOICE"


def test_bank_mismatch_blocks_auto_pay(aws):
    res, common = aws
    add_invoice(res, "INV-000000000301", 70_000, bank="BSB 033-000 Account 11112222")
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000301", "PO-SEED-0001", 70_000)
    assert e.value.code == "BANK_DETAILS_MISMATCH"


def test_bank_matching_on_file_is_fine(aws):
    res, common = aws
    add_invoice(res, "INV-000000000302", 70_000, bank="Account ending 4321")
    assert common.pay_invoice("INV-000000000302", "PO-SEED-0001", 70_000)["status"] == "PAID"


def test_vendor_mismatch_and_po_balance(aws):
    res, common = aws
    add_invoice(res, "INV-000000000401", 30_000)
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000401", "PO-SEED-0003", 30_000)
    assert e.value.code == "VENDOR_MISMATCH"
    add_invoice(res, "INV-000000000402", 60_000)
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000402", "PO-SMALL-0001", 60_000)
    assert e.value.code == "PO_BALANCE_EXCEEDED"


def test_human_approval_lifts_limit_but_not_other_rules(aws):
    res, common = aws
    add_invoice(res, "INV-000000000501", 650_000, status="EXCEPTION")
    r = common.pay_invoice("INV-000000000501", "PO-SEED-0001", 650_000, approved_by="clerk@example.com")
    assert r["status"] == "PAID" and r["approved_by"] == "clerk@example.com"
    add_invoice(res, "INV-000000000502", 30_000, status="EXCEPTION")
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000502", "PO-SEED-0003", 30_000, approved_by="clerk@example.com")
    assert e.value.code == "VENDOR_MISMATCH"


def test_invoice_in_review_cannot_be_auto_paid(aws):
    res, common = aws
    add_invoice(res, "INV-000000000601", 10_000, status="EXCEPTION")
    with pytest.raises(common.PaymentRejected) as e:
        common.pay_invoice("INV-000000000601", "PO-SEED-0001", 10_000)
    assert e.value.code == "WRONG_STATE"


# ---------------------------------------------------------------- email gate
def _parse(raw):
    import email
    import email.policy
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_email_gate_trusts_only_top_auth_header():
    import ingest_email
    from testkit import make_email, make_pdf
    pdf = make_pdf(["Invoice Number: X"])
    good = _parse(make_email("a@acme-supplies.example", "i@ap.example", "s", "b", pdf, auth="pass"))
    forged = _parse(make_email("a@acme-supplies.example", "i@ap.example", "s", "b", pdf, auth="fail",
                               forged_auth_header=True))
    assert ingest_email._auth_ok(good)[0] is True
    assert ingest_email._auth_ok(forged)[0] is False


def test_generated_pdf_is_valid():
    import io
    from pypdf import PdfReader
    from testkit import invoice_lines, make_pdf
    pdf = make_pdf(invoice_lines("Acme", "1", "ACME-1", "PO-SEED-0001", 1250.0, notes="Hello (world)"))
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "PO Number: PO-SEED-0001" in text and "Total: $1,250.00" in text


# ---------------------------------------------------------------- hooks
def test_payment_hook():
    import hooks
    allow = hooks.payment_hook({"context": {"toolName": "agent-tools___pay_invoice",
                                            "toolInput": {"amount_cents": 150000}}}, None)
    deny = hooks.payment_hook({"context": {"toolName": "agent-tools___pay_invoice",
                                           "toolInput": {"body": {"amount_cents": 650000}}}}, None)
    other = hooks.payment_hook({"context": {"toolName": "agent-tools___request_approval", "toolInput": {}}}, None)
    assert allow["decision"] == "allow" and deny["decision"] == "deny" and other["decision"] == "allow"


def test_guardrail_hook_fails_closed_on_truncation():
    os.environ.setdefault("GUARDRAIL_ID", "g")
    os.environ.setdefault("GUARDRAIL_VERSION", "1")
    import hooks
    r = hooks.guardrail_hook({"event": "after_tool_call",
                              "context": {"toolResult": {"truncated": True, "preview": "..."}}}, None)
    assert r["decision"] == "deny"


# ---------------------------------------------------------------- cedar
def test_cedar_policies_parse_and_decide():
    import glob
    import cedarpy
    gw = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw-1"
    texts = []
    for p in sorted(glob.glob(os.path.join(ROOT, "agentcore", "policies", "*.cedar"))):
        texts.append(open(p).read().replace("{GATEWAY_ARN}", gw).replace("{ACCOUNT}", "123456789012")
                     .replace("{PREFIX}", "apguardian"))
    policies = "\n".join(texts)

    def decide(role, action, amount=None, with_session=True):
        principal_id = f"arn:aws:sts::123456789012:assumed-role/apguardian-{role}-harness" + ("/session" if with_session else "")
        entities = [{"uid": {"type": "AgentCore::IamEntity", "id": principal_id},
                     "attrs": {"id": principal_id}, "parents": []},
                    {"uid": {"type": "AgentCore::Gateway", "id": gw}, "attrs": {}, "parents": []}]
        ctx = {"input": {} if amount is None else {"amount_cents": amount}}
        req = {"principal": f'AgentCore::IamEntity::"{principal_id}"',
               "action": f'AgentCore::Action::"{action}"',
               "resource": f'AgentCore::Gateway::"{gw}"', "context": ctx}
        return cedarpy.is_authorized(req, policies, entities).decision.name

    assert decide("payment", "agent-tools___pay_invoice", 150000) == "Allow"
    assert decide("payment", "agent-tools___pay_invoice", 650000) == "Deny"
    assert decide("payment", "agent-tools___pay_invoice") == "Deny"
    assert decide("intake", "agent-tools___pay_invoice", 100) == "Deny"
    assert decide("supervisor", "agent-tools___read_invoice") == "Deny"
    assert decide("supervisor", "delegate___ask_payment_agent") == "Allow"
    assert decide("matching", "agent-tools___lookup_po") == "Allow"
    assert decide("intake", "agent-tools___lookup_po") == "Deny"
    assert decide("payment", "agent-tools___request_approval") == "Allow"
    assert decide("payment", "agent-tools___pay_invoice", 150000, with_session=False) == "Allow"
    assert decide("intake", "agent-tools___read_invoice", with_session=False) == "Allow"


def test_open_exception_is_idempotent_and_never_reopens_paid(aws):
    res, common = aws
    sfn = boto3.client("stepfunctions")
    arn = sfn.create_state_machine(name="x", definition=json.dumps({"StartAt": "P", "States": {"P": {"Type": "Pass", "End": True}}}),
                                   roleArn="arn:aws:iam::123456789012:role/r")["stateMachineArn"]
    os.environ["APPROVAL_STATE_MACHINE_ARN"] = arn
    add_invoice(res, "INV-000000000701", 10_000)
    assert common.open_exception("INV-000000000701", "r1")["status"] == "EXCEPTION_OPENED"
    assert common.open_exception("INV-000000000701", "r2")["status"] == "ALREADY_PAID_OR_IN_REVIEW"
    add_invoice(res, "INV-000000000702", 10_000, status="PAID")
    assert common.open_exception("INV-000000000702", "r")["status"] == "ALREADY_PAID_OR_IN_REVIEW"
    assert len(sfn.list_executions(stateMachineArn=arn)["executions"]) == 1


def test_tools_api_rejection_is_reported_not_raised(aws):
    res, common = aws
    import importlib
    import tools_api
    importlib.reload(tools_api)
    add_invoice(res, "INV-000000000801", 900_000)
    r = tools_api.pay_invoice({"body": json.dumps({"invoice_id": "INV-000000000801", "po_number": "PO-SEED-0001",
                                                   "amount_cents": 900_000})}, None)
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["status"] == "REJECTED" and body["code"] == "OVER_AUTO_LIMIT"
    bad = tools_api.pay_invoice({"body": json.dumps({"invoice_id": "x", "po_number": "y", "amount_cents": 1})}, None)
    assert bad["statusCode"] == 400
