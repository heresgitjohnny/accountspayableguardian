# Deployment and operations guide

This guide takes you from an empty AWS account to a working AP Guardian, then
covers updates, email setup, testing and removal. The overview and architecture
are in the [README](../README.md). Diagrams: [`architecture.png`](architecture.png),
[`data-flow-simple.png`](data-flow-simple.png), [`data-flow.png`](data-flow.png).

Every command below assumes you are in the repository root with the virtual
environment active:

```bash
python3 -m venv .venv && . .venv/bin/activate    # first time only
export AWS_REGION=ap-southeast-2                  # your region
export CDK_DEFAULT_REGION=$AWS_REGION
```

Use the same region for every command. SES identities, the stack and the
AgentCore resources are all regional.

## 1. Prerequisites

| Need | Notes |
|---|---|
| AWS account and an IAM principal that can create IAM, Lambda, S3, DynamoDB, Cognito, CloudFront, API Gateway, Step Functions, SES, SNS, Bedrock and AgentCore resources | Administrator access is simplest for a first deployment |
| AWS CLI v2, signed in | `aws sts get-caller-identity` must work. SSO sessions expire, so refresh before long runs (`provision.py` takes several minutes). |
| Node.js 20+ and the CDK CLI | `npm install -g aws-cdk` |
| Python 3.12+ | The deploy script creates `.venv` |
| Bedrock model access | Enable your chosen Anthropic model in the Bedrock console. See [Choosing a model](#3-choose-a-model-optional) |
| A domain you control (for real email) | Needed for inbound invoices and verified PO sender addresses |

Regional notes:

- The region must offer Bedrock AgentCore harnesses, Textract and Bedrock Guardrails. For inbound email it must also offer SES email receiving.
- The CloudFront WAF is only created when the stack is in **us-east-1**. In other regions the portal has no WAF.

## 2. Configure

Edit `cdk.json` (recommended) or pass `-c key=value` to `cdk deploy`.

| Key | Purpose | Default |
|---|---|---|
| `prefix` | Resource name prefix. Also used in Cedar role matching. | `apguardian` |
| `cognito_domain_prefix` | Hosted UI domain prefix, globally unique | `<prefix>-<account id>` |
| `clerk_email` | SNS subscription for exception alerts (confirm the email SNS sends) | none |
| `inbox_address` | Address that receives invoices, for example `invoices@ap.example.com`. Creates the SES receipt rule. | none |
| `po_from_address` | SES-verified sender for PO emails to vendors | none (POs are created but not emailed) |

**Important:** `-c` values replace what is in `cdk.json`, and a deploy without a
value sets it back to empty. If you set these once in `cdk.json`, later deploys
cannot blank them by accident.

## 3. Choose a model (optional)

If `MODEL_ID` is empty the harnesses use the AgentCore recommended model. To pin
one, set an inference profile or model ID you can use in the region:

```bash
aws bedrock list-inference-profiles --region $AWS_REGION --max-results 100 \
  --query "inferenceProfileSummaries[].inferenceProfileId" --output text | tr '\t' '\n' | grep sonnet
export MODEL_ID=au.anthropic.claude-sonnet-4-6      # example for ap-southeast-2
```

`MODEL_ID` is read by `agentcore/provision.py` only. If you run `provision.py`
again without it, the harnesses go back to the default model.

## 4. First deployment

```bash
./scripts/deploy.sh                 # or: ./scripts/deploy.sh -c clerk_email=you@example.com
```

The script:

1. Builds a Lambda layer with a current boto3 (`scripts/build_layer.sh`).
2. Runs `cdk bootstrap` and `cdk deploy`. Outputs go to `cdk-outputs.json`.
3. Runs `agentcore/provision.py`: the Cedar policy engine, the gateway (IAM inbound, policy ENFORCE), the tool targets, six Cedar policies and four harnesses. Resource IDs go to `agentcore/state.json` and the harness ARNs to SSM.
4. Runs `scripts/seed_data.py`: two approved vendors, one suspended vendor and three seed POs.

`cdk-outputs.json` and `agentcore/state.json` contain account-specific IDs. They
are in `.gitignore`. Do not commit them.

## 5. Create portal users

```bash
POOL=$(python -c "import json;print(json.load(open('cdk-outputs.json'))['ApGuardianStack']['UserPoolId'])")

# procurement officer: raises POs and manages vendors
aws cognito-idp admin-create-user --user-pool-id $POOL --username buyer@example.com \
  --user-attributes Name=email,Value=buyer@example.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --username buyer@example.com --group-name procurement

# payment clerk: decides exceptions (Proceed to payment, Decline, Delete)
aws cognito-idp admin-create-user --user-pool-id $POOL --username clerk@example.com \
  --user-attributes Name=email,Value=clerk@example.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL --username clerk@example.com --group-name ap-clerk
```

Each user receives a temporary password by email, then sets a new one and
enrols a TOTP authenticator (MFA is required). Groups are read from the sign-in
token, so a user added to a group must sign out and in again.

Keep the duties separate: the same person in both groups can raise a PO and
approve its exception.

Open the `PortalUrl` value from `cdk-outputs.json`.

## 6. Email setup

### Outbound PO emails (`po_from_address`)

1. Verify the sender in SES, in the stack's region. A verified domain covers every address on it.
   ```bash
   aws ses verify-email-identity --email-address procurement@example.com --region $AWS_REGION
   ```
2. In SES sandbox mode you can only send to verified recipients. Verify each vendor contact email, or request production access in the SES console.
3. Set `po_from_address` in `cdk.json` and redeploy (section 7).

When a PO is created the portal says either "and emailed the vendor" or
"(email not sent: reason)".

### Inbound invoices (`inbox_address`)

1. In SES, verify the domain (or a subdomain such as `ap.example.com` if the main domain already receives mail).
2. Create an **MX record** for that domain:
   `10 inbound-smtp.<region>.amazonaws.com`, for example `inbound-smtp.ap-southeast-2.amazonaws.com`. An MX record on a whole domain redirects all of its mail, so use a subdomain if the domain is in use.
3. Set `inbox_address` in `cdk.json` (for example `invoices@ap.example.com`) and redeploy.
4. Activate the receipt rule set (CDK creates it but does not activate it). Only one rule set can be active per region:
   ```bash
   aws ses set-active-receipt-rule-set --rule-set-name apguardian-inbound --region $AWS_REGION
   ```
5. Add the sender's domain to the vendor (Vendors tab, "Invoice sender domains"). Mail from a domain that is not on an approved vendor is quarantined.

Test it:

```bash
aws s3 ls s3://<LandingBucket from cdk-outputs.json>/ --recursive --region $AWS_REGION
```

- A new object under `raw/` means SES received the email.
- An object under `quarantine/` means the email gate rejected it. The reason is in the `ingest_email` Lambda log (`QUARANTINED`): unauthenticated sender (needs SPF and DKIM, or DMARC, to pass), sender domain not on the vendor list, spam or virus verdict, or no PDF attached.
- The invoice then appears in the portal Invoices tab after extraction.

## 7. Updating an existing deployment

After pulling or editing code:

| What changed | Run |
|---|---|
| `lambdas/`, `portal/`, `infra/stack.py`, `cdk.json` | `cdk deploy ApGuardianStack --require-approval never --outputs-file cdk-outputs.json` |
| `agentcore/` (prompts, Cedar policies, tool lists), or you want a different model | `python agentcore/provision.py --outputs cdk-outputs.json` (set `MODEL_ID` first) |
| Both | CDK deploy first, then `provision.py` |

`provision.py` is idempotent: it updates policies and harnesses in place. After
a portal change, hard-refresh the browser (Cmd+Shift+R on macOS) and sign in again.

Offline tests (no AWS needed):

```bash
pip install pytest "moto[dynamodb,stepfunctions]" pypdf cedarpy
pytest -q tests/
```

## 8. Verify the deployment

1. Sign in as the procurement user. The Purchase orders tab should list the seed POs and offer approved vendors in the dropdown.
2. Run `python scripts/seed_data.py --reset`, then `python scripts/stress_test.py` (about 5 to 15 minutes). It writes `docs/stress_results.md`. See [STRESS_TESTS.md](STRESS_TESTS.md).
3. In the AgentCore console, open Harnesses and Observability to see traces of each agent turn, tool call and hook decision.
4. Lambda logs show structured events such as `QUARANTINED`, `GUARDRAIL_DENY`, `PAYMENT` and `EXCEPTION_FINALIZED`.

## 9. Operating notes

- **Exceptions queue.** Clerks see a plain-English reason for each item (hover over it for the technical detail). **Proceed to payment** runs the payment again with the over-limit checks lifted for that payment only. All other checks still apply. **Decline** leaves the invoice unpaid. **Delete** removes the item and marks the invoice deleted, and never touches a paid invoice.
- **Stuck items.** If a decision is clicked after its workflow has ended, the portal runs the final step directly, so items do not stay stuck in "Processing".
- **Vendors.** Adding a vendor lets anyone in `procurement` authorize an email sender domain. Treat that group as privileged.
- **Limits.** Auto-pay is limited to $2,000 per invoice and $5,000 per vendor per day. Change them with the `AUTO_PAY_LIMIT_CENTS` and `DAILY_VENDOR_CAP_CENTS` Lambda settings and the Cedar policies `05_*.cedar` and `06_*.cedar`. Keep all of these in step.
- **Payments are simulated.** They are written to a DynamoDB ledger. To connect a payment rail, replace the ledger write in `lambdas/common.py`.

## 10. Troubleshooting

See the table in the [README](../README.md#troubleshooting). Common first-run issues:

| Symptom | Fix |
|---|---|
| `ExpiredToken` partway through `provision.py` | Sign in again and re-run it. It is safe to repeat. |
| `provision.py` says boto3 is too old | `pip install -U boto3` inside `.venv` |
| Policy 06 "Overly Restrictive" finding | Harmless for IAM inbound auth. The policy is scoped to IAM principals. |
| Agents answer in plain text instead of calling tools | `allowedTools` must be in the `@ap_tools/<target>___<tool>` form. Re-run `provision.py`. |
| Deploy fails with "ReceiptRuleSet already exists" | Deactivate and delete the leftover rule set (see README troubleshooting), then redeploy. |

## 11. Remove everything

```bash
./scripts/destroy.sh
```

This deletes the harnesses, gateway and policies, then the CDK stack, and **all
data in it**. SES identities and DNS records you created yourself are not
removed.
