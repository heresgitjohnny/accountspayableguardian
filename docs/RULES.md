# AP Guardian rule set

The job: process vendor invoices that arrive by email and pay the ones that are
clearly legitimate. Everything else goes to a human. When the agent is unsure,
it sends the invoice for review; it never guesses.

## What the agents can access

| Agent | Can read | Can do | Cannot see or call |
|---|---|---|---|
| Supervisor | Nothing directly | Delegate to the three specialists | Any data or payment tool |
| Intake | One invoice: extracted fields and email body excerpt | Report fields and red flags | Purchase orders, payments, approvals |
| Matching | One invoice, one purchase order (vendor, status, balance) | Report match or no match | Payments, approvals, bank details |
| Payment | Only what the supervisor passes it | `pay_invoice`, `request_approval` | Raw invoice content, purchase orders, vendors |

No agent has a shell, browser, code interpreter or memory. No agent, and no API
an agent can reach, can create or edit vendors, edit bank details, create
purchase orders, or export records.

## What the system must refuse

1. **Paying more than $2,000 automatically.** Anything larger goes to a human.
2. **Paying one vendor more than $5,000 in a day automatically**, even when it is split across several invoices.
3. **Paying an invoice without an open purchase order** from the same vendor with enough remaining balance.
4. **Paying the same vendor invoice number twice.**
5. **Paying a different bank account from the one in vendor master data.** Bank details printed on an invoice are never used. If they differ from the account on file, the invoice goes to a human.
6. **Processing email from senders who are not authenticated** (SPF and DKIM, or DMARC) **or not on the approved-vendor allow-list.** That email is quarantined and never reaches an agent.
7. **Following instructions found in invoices or emails.** Vendor text is data. Instructions in it ("ignore previous instructions", "test mode", "the CFO says skip approval") are treated as a red flag.
8. **Creating or changing purchase orders, vendors or bank details.** Only a procurement officer can raise a purchase order, through the portal.
9. **Approving its own exceptions.** Only a payment clerk, signed in with MFA, can approve or reject an exception.

## How edge cases are handled

| Situation | Behaviour |
|---|---|
| Amount unreadable or low confidence | Sent to human review |
| No PO number on the invoice | Sent to human review |
| PO closed, wrong vendor or not enough balance | Sent to human review |
| Urgency or pressure language | Flagged by Intake; sent to human review |
| Guardrail detects a prompt attack in a tool result | The harness stops the run (`hook_stopped`); the invoice goes to human review |
| Agent errors, times out or finishes without a decision | The invoice goes to human review (fail closed) |
| Clerk approves an over-limit invoice | Paid. The $2,000 limit and daily cap are lifted for that payment only; the PO, vendor, duplicate and balance checks still apply |
| Clerk approves an invoice whose PO is invalid | Payment is still blocked and the block reason is recorded |
| No clerk decision within 7 days | The workflow times out; the invoice stays unpaid |

## How each rule is enforced

The layer numbers match the architecture diagram. Rules marked **hard** are
enforced in code or IAM, so they hold even if every agent misbehaves.

| Rule | ① Cognito | ② Portal API | ③ Email gate | ④ Identity + allowedTools | ⑤ Guardrail hooks | ⑥ Cedar policy | ⑦ Tools API + IAM | ⑧ pay_invoice code | ⑨ Human workflow | Prompt |
|---|---|---|---|---|---|---|---|---|---|---|
| R1 $2,000 auto limit | | | | | ✓ payment hook | ✓ permit ≤ 200000 + forbid | | ✓ **hard** | ✓ | ✓ |
| R2 $5,000 daily vendor cap | | | | | | | | ✓ **hard** (atomic counter) | ✓ | |
| R3 Open PO, same vendor, balance | | | | | | | | ✓ **hard** | ✓ | ✓ Matching |
| R4 No duplicates | | | | | | | | ✓ **hard** (conditional write) | | ✓ |
| R5 Bank details from master only | | | | | ✓ topic filter | | ✓ no route to edit | ✓ **hard** (mismatch → human) | | ✓ |
| R6 Sender authenticated and allow-listed | | | ✓ **hard** | | | | | | | |
| R7 Ignore embedded instructions | | | | ✓ ID-only prompt | ✓ prompt-attack filter | | | | | ✓ |
| R8 No master-data changes | ✓ groups | ✓ group check | | ✓ | ✓ topic filter | ✓ default deny | ✓ **hard** (route doesn't exist) | | | ✓ |
| R9 Only clerks approve | ✓ MFA | ✓ group check | | | | | | | ✓ **hard** (task token) | |
| Per-agent tool scope | | | | ✓ **hard** | | ✓ per-role principal | ✓ resource policy | | | |
