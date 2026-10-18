# M-Pesa Suite — changes on top of navariltd/frappe-mpsa-payments

Branch `astral-suite`, based on upstream `version-16` (newest features: STK pay page, POS quick pay,
bundled Safaricom certs, Pull Transactions fixes). Package name stays `frappe_mpsa_payments` so all
callback URLs already registered with Safaricom keep working and `bench migrate` upgrades in place.

## Phase 1 — Foundation (this change)

**Runs on Frappe/ERPNext v15 and v16**
- `pyproject.toml`: `frappe >=15,<17`, `erpnext >=15,<17`.
- `compat.py`: cache accessor, typed Single reads, `in_test()`, lazy `IntegrationTestCase`.
- CI: unit tests on Python 3.10–3.14 + bench tests on `version-15`/py3.11 and `version-16`/py3.14.
- Removed `frappe.db.commit()` from `Mpesa Settings.on_update` (not allowed inside document hooks on v16).

**One Daraja client** (`daraja/client.py`)
- All four token paths (builder connector, B2C connector, `get_token`, legacy connector) share one
  Redis-cached token with a 120 s safety margin, 10 s/30 s timeouts.
- Tokens are no longer written to `Mpesa Settings`; `Authorization` headers are redacted in
  Integration Request logs.

**Callback security** (`daraja/callbacks.py`, new **M-Pesa Suite Settings**)
- Every Safaricom-facing endpoint (STK, C2B validation/confirmation, B2C result, balance,
  transaction status, queue timeout, legacy verify, pull) goes through `@safaricom_callback`:
  optional IP allow-list, optional URL token `?t=…`, and runs as a configurable **Integration User**
  instead of switching the request to Administrator.
- Rejections are counted on the settings page (rate-limited writes).
- Every callback URL we generate now carries the token. Pull registration (permanent at
  Safaricom) is IP-checked only.

**Closed guest access**
- `get_mpesa_draft_c2b_payments`, `get_draft_pos_invoice`, `get_outstanding_invoices`: were open
  to anonymous users (customer names, phone hashes, invoices) → login + read permission.
- `Mpesa Express Request` `create_new_request` / `retry_stkpush` / `get_request_status` /
  `get_stkpush_defaults`: anonymous STK prompts to any phone, and a 30-second blocking poll →
  login + document permission. (Public checkout returns in Phase 3 via signed pay-page tokens.)

**Role gates** (`permissions.py`, new roles M-Pesa Manager / Payer / Collector / Viewer)
- Direct API calls to STK push / STK query need a collect role (incl. Sales User, Accounts User).
- Balance, transaction status, pull need a finance role; pull registration needs an admin role.
- Calls reached through a permission-checked document flow (e.g. Payment Request submit) are
  unchanged.

**B2C security credential fix** (`Mpesa Settings`)
- Upstream encrypted the STK *passkey* with OAEP for B2C and never regenerated. Now:
  RSA-PKCS1v15 of the *initiator password*, regenerated when the password is retyped, the
  environment flips, or on the new **Regenerate Security Credential** button.

**Accounting mapping on Mpesa Settings** (ERPNext custom fields): Bank Account, Cost Center,
M-Pesa Charges Account — replaces hard-coded `Mpesa-208911 - AIL` / `LIKE '%code%'` lookups in
later phases.

**Shared phone helper** (`utils/phone.py`): one normaliser (2547…/2541…), local form, Safaricom
SHA-256 MSISDN hash, network detection, masking.

## Phase 2 — Money in (C2B, STK, matching, booking, bank transactions, reconciliation)

Package `frappe_mpsa_payments/money_in/`. Every piece is behind its own switch on the
**Money In** tab of M-Pesa Suite Settings; with all switches off nothing changes.

| Switch | What it does | Astral Server Script it replaces |
|---|---|---|
| Create Bank Transaction for Every Receipt | C2B + STK receipts → submitted Bank Transaction on the shortcode's mapped Bank Account (one per receipt, MariaDB lock), 5-min sweep for misses | Register To Bank Transaction · Auto Reconcile pass 0 |
| Block Duplicate Receipts on M-Pesa Bank Accounts | Bank Transaction before_insert guard | BT Duplicate Guard · Duplicate BT Sweeper |
| Set Clearance Date When Every Bank Leg Is Matched | only adds clearance when all Bank GL legs are allocated | AIL BT Sync Voucher Clearance |
| Auto-Reconcile M-Pesa Bank Transactions | statement line ↔ Payment Entry via ERPNext `reconcile_vouchers`, exact amount | Auto Reconcile passes 1–2 |
| Use M-Pesa Receipt as Payment Entry Reference No | MPC2B-… → receipt | PE Reference To Receipt |
| Block a Second Payment Entry for the Same Receipt | runs *after* the reference swap (fixes the script-order race) | PE Duplicate Receipt Guard |
| Fill Missing Cost Center on M-Pesa Payment Entries | | PE Cost Center Autofill |
| Clear Clearance Date on Amended PE / JE | | AIL PE / JE Amendment Clear Clearance |
| Match and Book Receipts Automatically (+ 5 sub-switches) | existing PE link → document reference (PR/SO/SI/Quotation/Customer) → invoice-number repair → customer name → payer phone hash → orphan STK; books only on one candidate; Mpesa Match Log | Self Heal Unmatched · C2B No-Space Name Matcher · Payer Phone Matcher · Register Link Backfill · SO Checker pass 0 · Invoice Collection Allocator |
| Book Receipts Against Sales Orders and Release Paid Orders | sums receipts across all PRs of the order + direct references (cancelled excluded); when paid, applies the configured Workflow actions **as the configured approver** (`apply_workflow`, roles enforced) or submits; books receipts as advances | SO M-Pesa Payment Checker (without its workflow bypass, hard-coded approver, cancelled-register counting or Error Log deletion) |
| Normalise Phone on M-Pesa Payment Requests | 07… form | Normalize Mpesa Phone on Payment Request |
| Cancel Earlier Unpaid Prompt When Re-prompting | real `cancel()` / delete, not a docstatus flip | MPesa PR Cleanup Before Reprompt |
| Poll Safaricom for Unanswered STK Prompts | STK Query for prompts 2–60 min old; marks Completed / Failed with reason; never books money | (new) |
| Mark Payment Requests Paid When Settled | PR → Paid and STK prompt → Completed with receipt (db writes, so the prompt's duplicate-C2B check can't cancel the register) | AIL Mpesa Feed - Status Sync |

Upstream fixes in the C2B register:
- Payment Entry `reference_no` = M-Pesa receipt, posting/reference date = transaction date (not run date).
- Allocation capped at the invoice outstanding / order balance; the excess stays as an advance.
- Matched document (from the matcher) wins over the raw account reference.
- Company / Mode of Payment fall back to Mpesa Settings (Buy Goods tills had no Register URL row).
- Cost center from the shortcode's Accounting Mapping.
- Confirmations are idempotent per receipt (Safaricom re-sends).

New DocType **Mpesa Match Log**: every matched / booked / ambiguous / unmatched / error decision,
written once per receipt and result.

Tests: `money_in/test_money_in.py` (bootstraps its own KES company via the setup wizard, so it
runs on v15 and v16).

## Phase 3 — External Payments API, pay page, signed webhooks

Package `frappe_mpsa_payments/gateway/`, full reference in [docs/PAYMENTS_API.md](docs/PAYMENTS_API.md).

- **Mpesa API Client**: one record per chatbot or shop. It holds the API key and an encrypted secret
  (shown once), the paybill to prompt from, per-action permissions, max amount, rate limit, IP
  allow-list, webhook URL with its signing secret, and the hosts allowed for callback and return URLs.
- **Mpesa Payment Intent**: one payment the client asked for. Its name is the M-Pesa account
  number, so it settles through whichever message arrives: the STK callback, the C2B
  confirmation, a Pull sweep, or a receipt code the customer typed (`verify_receipt`).
  Paid intents linked to a Sales Order or Invoice are booked through the money-in pipeline.
- Endpoints: `create_prompt`, `create_payment_link`, `get_status`, `resend_prompt`, `cancel`,
  `verify_receipt`, `list_payments`.
  - Requests carry `X-Mpesa-Client-Key` / `X-Mpesa-Client-Secret` headers.
  - Responses use a JSON envelope with real HTTP status codes.
  - Idempotency keys prevent a repeated call from prompting twice.
  - A receipt can pay only one intent.
- **Public pay page** `/pay/<token>`: amount, description, phone box, live status, paybill fallback.
  It is rate-limited and capped at 5 prompts per payment, and shows nothing beyond the masked phone.
- **Webhooks**: `payment.succeeded|failed|expired|cancelled`, signed
  `X-Mpesa-Signature: t=…,v1=HMAC-SHA256`, retried with backoff for about a day, and logged in
  **Mpesa Webhook Delivery** with a *Send Now* button.
- Every-minute job settles prompts whose callback never came and expires stale intents.

## Rollout at Astral (staging copy first)
1. `bench get-app` this branch, `bench --site <site> migrate`. Existing records keep their data
   (fields upstream removed from the JSON are recreated as custom fields with the same names).
2. Open **M-Pesa Suite Settings**: set Integration User (System User with Accounts User + Sales User
   + M-Pesa Manager), keep enforcement off.
3. Fill **Accounting Mapping** on each Mpesa Settings (208911, 4134019, 4139683).
4. On each B2C settings record press **Regenerate Security Credential**, then run a balance query.
5. Turn on **Only Accept Callbacks From Allowed IPs**, watch the rejection counter for a day.
6. Turn on **Require Token on STK / B2C / …** (new requests carry the token immediately).
7. Re-register C2B URLs (Mpesa C2B Payment Register URL → save), then turn on the C2B token.
8. Astral's Server Scripts keep running; they are retired one by one in Phases 2–4.
9. Money In, one row of the table above at a time: disable the Server Script, turn on the switch,
   watch Mpesa Match Log and the Bank Reconciliation Tool for a day. Before the prepaid-orders
   switch, set **Approve Paid Orders As** to a user holding Sales Approver Primary + Secondary.
   Note: a fully paid order can still be stopped by the customer's credit limit; that shows as an
   Error row in Mpesa Match Log.
10. On upgrade, switches whose default is on (both duplicate-receipt guards, receipt as PE
    reference, cost-center fill, amendment clearance, phone normalising, the matcher's
    sub-switches) are switched on by migrate. They do the same thing as Astral's scripts and are
    safe to run alongside them. The master switches (statement lines, matching, prepaid orders,
    reconciliation, STK polling, status sync) stay off.
11. The first time **Match and Book Receipts Automatically** is on, it will book every draft
    receipt from the look-back window (45 days) that it can place unambiguously, including any
    left for manual review. Lower the look-back first if that is not wanted.
