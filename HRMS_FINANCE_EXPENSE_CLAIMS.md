# HRMS → IMPACGO Finance (prepared by the Finance team, 17 Nov 2026)

Three things link HRMS to IMPACGO Finance:
- **Expense claims:** approved claims post to Finance automatically.
- **Assets:** HRMS assets link to Finance's fixed-asset register, and HRMS reports who holds each one.
- **Connection:** each customer connects its own Finance under **Administration → Integrations → IMPACGO Finance**.

HRMS's approval flow is unchanged.

## Connecting a customer (each company, its own key)

1. **In Finance** (the customer's admin, signed in to *their* Finance):
   1. Go to **Administration → API Keys**.
   2. Create a key for the HRMS connector user.
   3. Copy it: Finance shows it once and stores only a hash.

   The connector needs these permissions: `fin.integration.submit`, `fin.integration.view`, `fin.integration.reverse` and `fin.account.view`. For the Impacgo company, `scripts/ops/hrms_expense_setup.py` in the Finance backend creates the *Expense Claims* journal name (its `EXP-` series) and grants these permissions.
2. **In HRMS:**
   1. Go to **Administration → Integrations → IMPACGO Finance**.
   2. Enter Finance's address (`https://…`) and paste the key.
   3. **Test connection** checks that the key works and that it belongs to **this same company** in Finance. HRMS and Finance share the tenant, so employees, accounts and assets are the same records. A key from another company is refused, and its name is shown.
   4. **Save**. Then pick the bank account claims are paid from, the employee-advances account, the claim-taxes account and the default business unit.
3. **Using the key:**
   - The key is stored **encrypted** in the company's tenant schema (`hcm_finance_link_settings.api_key_encrypted`). It is encrypted with `TENANT_EMAIL_ENCRYPTION_KEY`, the same master key as the tenant email secrets.
   - It is **never returned** by the API or shown again; the screen shows only its last four characters.
   - To replace it, paste a new one. **Disconnect** deletes it.
   - While a company is disconnected or paused, its new changes wait in the queue and are sent once it is connected again.

The HRMS server needs only `TENANT_EMAIL_ENCRYPTION_KEY`. A company with no saved settings can still use the earlier one-company environment settings (see *Settings* below); saved settings win over them.

## Configuration in HRMS (once connected)

| Where | Field | Meaning |
|---|---|---|
| Travel & Expenses → Expense Categories | `hcm_expense_claim_types.gl_account_id` | The expense GL account each claim row of the category is **debited** to. Several categories may share one account. Picked from Finance's chart of accounts. |
| Employee profile → Payroll → IMPACGO Finance | `core_employees.payable_account_id` | The GL account that holds what the company owes the employee. An approved claim **credits** it. The employee is **not** a supplier in Finance. |
| Assets → Create / Edit Asset → Finance Fixed Asset | `hcm_asset_inventory.finance_asset_id` | The fixed asset in Finance's register this HRMS asset is. One HRMS asset per Finance asset. |

## What happens to a claim

| Claim in HRMS | In Finance |
|---|---|
| **Pending** (submitted, waiting for approval) | A **draft** journal (not in the ledger). If the claim changes while pending, the draft is replaced. |
| **Approved** | The journal is **posted**, numbered by Finance's *Expense Claims* journal name (its own series, `EXP-2627-00001` …). |
| Back to **draft** / **sent back** / **rejected** | The draft is discarded (it never reached the ledger). |
| **Marked paid** | Dr employee's payable account / Cr bank, on the clearance date. |
| **Un-marked** | That payment journal is reversed. |

The posted journal of a claim (example: Flight ₹10,000, Hotel ₹5,000, Taxi ₹1,000):

| Account | Debit | Credit |
|---|---|---|
| Flight's GL (e.g. 5024 Travel Expenses) | 10,000 | |
| Hotel's GL | 5,000 | |
| Taxi's GL | 1,000 | |
| Taxes & charges (only if the claim has any) | | |
| Employee Advances (only if an advance was applied) | | advance |
| **The employee's payable account** | | **16,000** |

Expense lines carry the business unit of the employee's department, or the default business unit if the department has none. Finance requires a business unit on its expense accounts.

## Currencies & FX (from IMPACGO Finance; HRMS keeps no rates)

HRMS had no exchange rates. It kept only a fixed currency list and a company base currency, and it never converted amounts. Once a company is connected, Finance's **Currency & FX** becomes the single source of currencies and rates:

- **Claim form:**
  - The currency list is limited to Finance's active currencies.
  - A foreign amount shows "≈ ₹… at IMPACGO Finance's rate (1 USD = 83.5 INR, date)".
  - If Finance has no rate or no such currency yet, the form says so.
- **Posting:**
  - A claim in another currency posts in Finance's **base currency**. HRMS reads Finance's rate for the posting date (the latest rate on or before it, or the inverse pair if only that is recorded) and converts with it.
  - Each line is rounded to the paisa, and the employee's payable line takes any rounding, so the journal balances exactly with no Exchange Gain/Loss line.
  - Each line's description keeps the original amount and the rate, e.g. "Flight · USD 420.00 @ 83.5".
  - The draft and the posted journal use the same rate.
- **On the claim:** the Finance section shows "1 USD = 83.5 INR" and the amount in INR.
- **Payment:** posts in the base currency for exactly what the employee is owed, so their payable account clears to zero.
- **When Finance can't convert:** a currency or rate missing in Finance is named on the claim ("add it under Finance > Currency & FX, then retry").
- **Base currency:** Finance's base currency is the one claims post in. The Integrations page warns when it differs from HRMS's payroll currency (General Settings > default currency); HRMS does not change it by itself.

Why the base currency: lines sent to Finance *in* a foreign currency are stored in its ledger with unrounded base amounts (e.g. 2,784.725), and a rounding difference needs an Exchange Gain/Loss line that may require dimensions. Posting already-converted base amounts avoids both. Finance remains the only source of the rate.

The claim's Accounting tab shows its Finance journal: draft or posted, the voucher, the payment voucher, and Finance's reason when it refused.

## What happens to an asset (no ledger effect)

Finance keeps the books: cost, depreciation and disposal. HRMS keeps who holds the asset and reports it to Finance (`PUT /api/fin/fixed-assets/{id}/custodian`, a Finance endpoint that lets the connector set the holder and nothing else).

| In HRMS | In Finance |
|---|---|
| Asset linked to a Finance fixed asset, not yet assigned | Nothing changes: the holder Finance already records stays. |
| Assigned to an employee | The Finance asset's holder becomes that employee. |
| Returned | The holder is cleared. |
| Linked to another Finance asset, unlinked, or deleted | The holder HRMS had reported on the old Finance asset is cleared. |

Asset Details shows the Finance asset's code, the holder Finance records, its cost and its book value on each depreciation book. These are read from Finance; HRMS stores none of these numbers.

## How it works

- **Queueing:** any change of a claim (status, paid, amounts) or of an asset's assignment or link queues it in `hcm_finance_outbox`, in the same transaction. A session hook does this, so the claim form, the approvals inbox and any other path are all covered.
- **Sending:** a background sender serves every tenant schema with a connected company, using **each company's own address and key**. It finds them on every round, so connecting a customer needs no restart. It brings Finance in line with the document as it is **now**. Finance's idempotency keys mean a resend never posts twice.
- **Refusals:** when Finance refuses, the reason is shown on the claim or asset (`finance_error`) and in `GET /api/finance-link/outbox?status=failed`. Examples: a category with no GL account, an employee with no payable account, a closed period. Fix the cause, then `POST /api/finance-link/outbox/{id}/retry`.
- **Status:** `GET /api/finance-link/status` shows whether the company is connected, which categories have no GL account, how many employees have no payable account, and queue counts.
- **Older claims:** `POST /api/finance-link/queue-existing` queues claims saved before the link (pending and approved).

## Files

| File | Change |
|---|---|
| `app/finance_link.py` | New: per-company connection, rules, session hooks, sender, API (`/api/finance-link/...`). |
| `db/2026_11_17_finance_expense_claims.sql` | Additive: the GL columns, the claim's `finance_*` fields, the asset link columns, `hcm_finance_link_settings`, `hcm_finance_outbox`. Run with `db/run_hrms_migration.py`. |
| `app/models.py`, `app/schemas.py`, `app/travel_expense.py`, `app/routers/travel_expense_config.py`, `app/routers/assets.py`, `app/main.py` | The new fields, the category GL account, the asset link, the claim's Finance fields in its output, the router and the sender at startup. |
| `tests/test_finance_link.py` | 31 tests, no database or network needed. |

Frontend (HRMS web): Integrations → IMPACGO Finance, the Expense Category GL picker, the employee's payable account (Payroll tab), the claim's Finance section, and the asset form's Finance Fixed Asset picker plus Asset Details.

## Settings (HRMS server)

```
TENANT_EMAIL_ENCRYPTION_KEY=<Fernet key; required to store any company's Finance key>
FINANCE_DELIVER_EVERY=30                   # seconds between sender rounds
FINANCE_SENDER=on                          # off = no background sender (e.g. a worker-only node runs it)
```

Optional, the earlier one-company set-up (used only by a company with no saved settings):

```
FINANCE_URL=https://finance.impacgo.com
FINANCE_API_KEY=<HRMS connector key, sent privately; never commit it>
FINANCE_TENANT=finance
FINANCE_COMPANY_ID=<the HRMS company it serves>
FINANCE_JOURNAL_NAME=Expense Claims
FINANCE_BANK_ACCOUNT_ID=…  FINANCE_ADVANCE_ACCOUNT_ID=…  FINANCE_CLAIM_TAX_ACCOUNT_ID=…  FINANCE_BUSINESS_UNIT_ID=…
```

What the Finance team did on its side:
- **Migration 0152:** gateway drafts.
- **Custodian endpoint:** `PUT /api/fin/fixed-assets/{id}/custodian`. The connector's `fin.integration.submit` allows it.
- **`scripts/ops/hrms_expense_setup.py`:** creates the *Expense Claims* journal name and grants the connector's permissions.

## Known limits

- **Payroll and project budgets** still never convert currencies. A freelancer whose rate is in another currency is still paid outside payroll, and project budgets keep their own currency. Converting these at Finance's rates would change pay, so it needs a decision first.
- **Shared tenant only:** HRMS and Finance must share the tenant, i.e. the same company and employee records. Test connection checks this.
- **Asset recovery deductions** (salary deductions for lost assets) are not sent. They belong with the payroll integration.
- **Finance-side issue seen in testing:** a journal sent to Finance with foreign-currency lines is stored with unrounded base amounts, and its automatic rounding line to Exchange Gain/Loss fails when that account requires a business unit. HRMS avoids this by sending base amounts; Finance's own foreign-currency entry may still hit it.

## Proven on 17 Nov 2026

Tested on a local copy of the shared `finance` tenant: HRMS and Finance APIs running locally, with Finance's own company and employees.

**Claims:**
- A pending claim held a draft, and editing it replaced the draft.
- Approval posted `EXP-2627-00001`.
- Marking it paid posted `EXP-2627-00002`; un-marking it reversed that payment.
- A rejected claim's draft was discarded.
- The missing setup was named, and a retry posted after the fix.

**Assets:**
- Linking an unassigned asset left Finance's existing holder alone.
- Linking a Finance asset already linked to another HRMS asset was refused (409).
- Assign, return, relink and delete each updated Finance's holder as in the table above.
- Nothing was posted to the ledger.

**Currencies & FX:**
- A USD 718.85 claim (420.00 + 265.50 + 33.35) was held as a draft at Finance's rate of 83.5.
- On approval it posted `EXP-2627-00009` in INR: 35,070.00 + 22,169.25 + 2,784.72 = Cr Employee Payable 60,023.97, with no gain/loss line, and the ledger balanced to the paisa.
- Payment `EXP-2627-00010` paid exactly 60,023.97, and the employee's payable for the claim was 0.00.

**Connection** (HRMS running with **no** `FINANCE_*` environment):
- A wrong key was refused, and so was another company's key (its company name was shown).
- Plain http to a real host was refused.
- After saving, the key was stored only encrypted and never returned.
- Saving without a key kept the stored one.
- The pickers and the background sender worked through the company's own key.
- Disconnect deleted the key.
