# Astral M-Pesa Suite — changes on top of navariltd/frappe-mpsa-payments

Maintained by Astral Industries Limited. Licence and attribution: [NOTICE](NOTICE),
[docs/LICENSING.md](docs/LICENSING.md).

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

## Go-live hardening: audit 2026-10-18 "Still open" items 1–10 (uncommitted)

Follows [docs/AUDIT_2026-10-18.md](../docs/AUDIT_2026-10-18.md). **`bench migrate` is needed**: it adds
new fields and settings, and creates indexes on core tables.

**Callbacks are confirmed with Safaricom before any booking (items 1, 2, 4)**
- *C2B* (switch **Verify C2B Confirmations with Transaction Status**, on by default): a confirmation
  for a shortcode that is not in Mpesa Settings is dropped. Every other confirmation is held as an
  Integration Request (service `Mpesa C2B Verification`, Request ID = receipt). A Transaction Status
  query is sent for it, and the register is created only when the result has the same receipt,
  status Completed, the same amount and one of our shortcodes. The result must also carry the
  ConversationID of our query. So registers only hold confirmed money (or money Pull fetched from
  Safaricom), and matching, statement lines and the gateway need no change. A confirmation that
  can't be confirmed is re-queried every 10 minutes, 3 times at most, and then marked Failed with an
  Error Log. The hourly Pull import brings in real receipts anyway. An M-Pesa Manager can press
  **Release Without Safaricom Confirmation** on the Integration Request, which records who did it
  and why. This needs the Initiator and Security Credential on every Mpesa Settings, and the
  initiator must have the Transaction Status role.
- The manual **Fetch transaction** result (`handle_transaction_status_result`) now accepts only the
  answer to a query we sent, for the receipt we asked about, paid to our shortcode.
- *STK* (switch **Verify STK Callbacks with STK Query**, on by default): the callback endpoint only
  stores the payload (`callback_payload`, Verification Status *Pending*) and queues
  `money_in.stk_verify.settle`. That job sends an STK Query for the same CheckoutRequestID and checks
  the paid amount against the prompt. Only then does it set Completed and the receipt, and it
  commits **before** booking. Booking runs under a savepoint. A failed booking is rolled back,
  logged, and retried by the 2-minute sweep (3 attempts). A contradiction gives *Mismatch*, and an
  unanswered query after 6 hours gives *Unverified*. Neither is booked. A prompt is never marked
  Completed from an unconfirmed callback, so the gateway can't be fooled either.
- `stk_poll` skips prompts whose callback arrived; the settle job owns those.

**Callbacks without an Integration User (item 3)**: new setting **If No Integration User**.
*Warn* (default) runs as Administrator and writes one Error Log an hour. *Refuse* rejects the
callback and counts it. Set an Integration User, then switch to Refuse.

**Locks and indexes (items 5, 6)**
- `money_in.common.named_lock` now holds MariaDB `GET_LOCK` until the transaction commits or rolls
  back. It used to release at the end of the block, before the insert was committed. It is
  re-entrant within one transaction.
- Indexes (search_index Property Setter plus the index itself, on every migrate):
  Bank Transaction `reference_number`, `transaction_id`, Payment Entry `reference_no`, and
  Integration Request `request_id`. The duplicate checks stay locking reads, but they now lock only
  the receipt's rows instead of scanning the table.
- One insert path for registers, `money_in.receipts.insert_register`, used by C2B confirmations,
  Pull imports and Transaction Status results. It takes the same `c2b:<receipt>` lock, does a
  locking duplicate check, and inserts under a savepoint. Pull commits after each receipt.
- The Payment Entry duplicate-receipt guard takes `book:<receipt>`, the same key the gateway uses,
  and reads with `FOR UPDATE`.

**Hooks (item 7)**: register `on_cancel` raises when the Payment Entry can't be cancelled, instead of
logging it and leaving the PE live. The `after_insert` auto-submit and the `on_submit`
reconciliation run under savepoints, and the register survives as a draft. The commits inside
`create_and_reconcile_payment_reconciliation` and `handle_successful_transaction` are removed. The
Loan Disbursement service no longer commits, and the PE/JE/Loan services re-raise after logging.

**B2C (items 8, 9)**
- `handle_successful_payment` books under a savepoint and records failures on the B2C request
  (**Accounting Error**). A payout counts as **Accounting Booked** only when its PE/JE/Loan
  Disbursement is *submitted*. `retry_unbooked_payouts` runs every 15 minutes and retries paid
  payouts from the last 30 days.
- The disbursement's reference **Outstanding** is read-only and recalculated from the document
  (`utils/payouts.py`). On submit, under a per-document lock, a document can't be paid past what
  it owes, or while another disbursement's payout for it is in flight or paid but not yet booked.
- A B2C send that times out after the connection opened is **Status Unknown** (new status), not
  *Not Initiated*. Nothing can resend it. `check_unsettled_payouts` runs every 15 minutes and
  starts a B2C Transaction Status query (`money_out.b2c_status`, from the Daraja money-out session)
  for payouts that are Status Unknown after 5 minutes, or Initiated with no result after 60 minutes,
  at most once an hour per payout. Only a connect timeout (never sent) becomes *Failed* and can be
  retried. An HTTP 5xx without a Daraja error code is also Status Unknown.

**Permissions (item 10)**: `get_unallocated_payments` needs read on Payment Entry and the customer, and
uses `get_list`. `process_mpesa_c2b_customer_credit` and `process_mpesa_c2b_reconciliation` need
write on Payment Reconciliation and on the invoice(s) and entries, and check that each entry is a
submitted receipt from that customer. **Mpesa Settings** write and create are limited to System
Manager and M-Pesa Manager. Accounts Manager can read and export, Accounts User and M-Pesa Viewer
can read. Sites with customised permissions (Role Permission Manager) keep theirs, so check them.

**Rollout additions**
- Before migrating, give every Mpesa Settings an Initiator with the Transaction Status role, or turn
  the C2B verification off. Otherwise confirmations wait for Pull or a manager's release.
- Watch Integration Requests with service `Mpesa C2B Verification` and status Failed, and Express
  Requests with Verification Status Mismatch or Unverified.
- nginx must overwrite `X-Forwarded-For` (`proxy_set_header X-Forwarded-For $remote_addr;`) before
  the IP allow-list means anything (audit "Still open" #2, which needs no code change).

Tests: `tests/unit/test_verify.py` (decisions, pure) and `money_in/test_verification.py` (bench:
held C2B, forged amount, foreign ConversationID, unknown shortcode, forged/confirmed/short STK, re-sent
callback, lock held until rollback).

## Daraja money-out APIs: Reversal, B2B, Transaction Status for B2C (uncommitted)

The top three missing APIs from `docs/AUDIT_2026-10-18.md`, built to `docs/daraja_api_reference.md`
§9, §11 and §13. New package `frappe_mpsa_payments/money_out/`, plus pure request/result helpers
in `daraja/money_out.py`. `bench migrate` creates the three new DocTypes.

**Shared rules** (the same as B2C payouts)
- A request is sent only from a submitted document whose status is `Not Initiated`, under a row
  lock. The amount must be whole shillings; it is refused, never rounded.
- Safaricom's ack sets `Initiated`. Only the Result callback sets the final status.
- If the connection never opened, the status is `Failed`. If the request may have reached
  Safaricom (read timeout, dropped connection), the status is `Unknown`. Nothing is resent
  automatically.
- Each API has its own Result URL and its own QueueTimeOut URL
  (`money_out.api.<x>_result` / `<x>_queue_timeout`). Both carry the callback token and
  `?ref=<document>`. A queue timeout only ticks **Queue Timed Out** and never sets a final
  status.
- Results are matched by the ack's ConversationID / OriginatorConversationID. `ref` is used
  only when no ack arrived, and only if the IDs don't contradict it. A re-sent result is
  ignored once a final result is recorded.
- ResultCode is compared as a string (B2B sends `"0"`, Reversal sends `0` or `"R000001"`).
  `ResultParameter` may be a list, a single dict, or missing.
- Tokens come from `daraja/client.py`, with one refresh on a token error. Request bodies are
  logged through `scrub_payload`. Initiator and SecurityCredential come from Mpesa Settings,
  so **Regenerate Security Credential** is needed first.

**Mpesa Reversal Request** (`MRV-…`)
- Linked to an **Mpesa C2B Payment Register**. The C2B form has an Actions → **Reverse Payment**
  button that creates a draft. The receipt and amount are copied from the register, and the
  amount must be the whole payment.
- The shortcode is found from the register, and there can be only one live reversal per
  receipt.
- Submit and cancel need a payout or admin role: System Manager, M-Pesa Manager, M-Pesa Payer
  or Accounts Manager. Accounts User can only read.
- Result `0` sets **Reversed** and `R000001` sets **Already Reversed**; any other code sets
  **Failed**. A successful reversal adds a comment on the register. **Any Payment Entry booked
  from that receipt is not touched: cancel or reverse it by hand.**

**Mpesa B2B Payment Request** (`MB2B-…`): BusinessPayBill or BusinessBuyGoods
- Pays a supplier's paybill (account number required) or till. It can link a Supplier and a
  submitted KES Purchase Invoice. The amount can't exceed what is left to pay, counting other
  unbooked requests for the same invoice.
- The shortcode's transaction limit is enforced, and paying our own shortcode is refused.
- Submit needs PAYOUT_ROLES. Accounts User can prepare drafts.
- A request can be cancelled when it is Not Initiated or Failed. An **Unknown** request can be
  cancelled only by an admin, after checking the statement. Redo a failed payment with
  cancel + amend.
- Paid enqueues a Payment Entry (Pay, Supplier) from the shortcode's mapped Bank Account. It is
  allocated to the Purchase Invoice if one is linked, and submitted when **Submit B2C Accounting
  Entries** is on in Mpesa Settings. It is idempotent: an existing PE with the receipt as
  reference is linked instead. A booking failure is logged and commented, and the payment
  stays Paid.

**Transaction Status for B2C** (**Mpesa B2C Status Query**, `MB2CQ-…`)
- B2C Disbursement Request → Actions → **Check Status with M-Pesa** (payout/admin roles). It is
  shown for submitted M-Pesa payouts still in `Not Initiated`, `Initiated` or `Status Unknown`, meaning the
  request was sent but no result has come back.
- The query uses the receipt if known, otherwise `OriginalConversationID` = our
  OriginatorConversationID. Only one open query is allowed per payout every 2 minutes.
- The result changes the payout only while it is unsettled:
  - `TransactionStatus = Completed`, with the same amount, a receipt, and the same
    OriginatorConversationID, sets **Paid**. `result_code = "0"` makes a late real Result a
    no-op, and the same `update_b2c_reference_status` job books the payout.
  - `Failed` / `Declined` / `Expired` / `Cancelled` sets **Failed**, so **Retry Payment**
    (new OriginatorConversationID) becomes available.
  - Anything else ("not found" right after sending, pending, amount mismatch) is **Unresolved**,
    and the payout is left alone. Closes audit item 9.

**Hooks**: two `doctype_js` lines (B2C status button, C2B Reverse button). No doc_events or
scheduler changes.

**Tests**
- `tests/unit/test_money_out.py`: request bodies, result parsing, and status decisions.
- `tests/unit/test_money_out_flow.py`: the full send → ack → result flow on an in-memory
  `frappe` stub (`tests/unit/fake_frappe.py`). It covers row lock, Unknown on timeout, token
  retry, re-sent results, ID mismatch, queue timeouts, the status-query rules and the role
  gates.
- `pytest tests/unit`: 140 pass. Small bench tests sit next to each DocType. **None of this has
  run on a bench or against the Daraja sandbox yet.**

**Unverified / to check in sandbox**
- Whether Transaction Status accepts our OriginatorConversationID as `OriginalConversationID`
  (some integrations need Safaricom's ConversationID).
- The `TransactionStatus` strings other than "Completed".
- Whether BusinessBuyGoods needs an `AccountReference` (the reference doc says paybill only, and
  we follow it).
- Reversal `ReceiverParty` for a till: we send the Mpesa Settings business shortcode.
- `create_request_log` commits on v15. That happens before the HTTP call, the same as B2C today.

## Bench-tested on v15 and v16, fixes and Mpesa Settings setup guide (2026-10-19, uncommitted)

All bench test modules pass on Frappe/ERPNext v15 (15.123) and v16 (16.52), with `payments`
installed, after `bench migrate` of a site installed from the previous commit (indexes, new
fields and the Suite Settings switch defaults verified). `tests/unit`: 162 pass.

Fixes found by running on a bench:
- `b2c_disbursement_request.py`: `...permissions` -> `....permissions` (install-app crashed).
- `money_in/c2b_verify.send_query` (in the go-live hardening work, committed with it): result URLs built with `get_url()`; `build_callback_url` needs a
  web request, and this runs in background jobs, so no C2B verification query was ever sent.
  Needs `host_name` in site_config (as B2C and money_out already do).
- `utils.create_payment_gateway_account` / `create_mode_of_payment`: ERPNext v16 names the
  Payment Gateway Account after the company, so every Mpesa Settings save failed on v16. Both are
  now company-aware; saving without a Company no longer crashes ("Mode of Payment None"), and a
  Company set later gets its Mode of Payment account row.
- `Mpesa C2B Payment Register`: the shortcode's Mpesa Settings company wins over the user's default
  company that `insert()` pre-fills (multi-company sites booked to the wrong company).
- `gateway/intents.py`: "STK callback" is not an Mpesa Match Log method; booking-failure logs were lost.
- `api/payment_entry.get_outstanding_invoices`: empty list (not a crash) when there is no party account.
- `B2C Payment Disbursement Reference.is_valid_receiver_contact`: accepts 07../01../+254 forms (it
  rejected a valid 07 number unless already converted); `validate_mandatory_fields` reference check
  moved out of the loop.
- `Mpesa C2B Payment Register URL`: URLs from `get_url()`, status Failed on connection errors, and a
  **Register URLs Again** button (needed after host_name or the callback token changes).
- `Mpesa Settings`: test-mode balance / STK branches log the request like the live call does.

Mpesa Settings form: a collapsible **Setup Guide** (once-per-site steps, where each credential comes
from in sandbox and live, what to do after saving, how to test, common errors) and "where to get
it" help on every field. Business Shortcode is now visible in sandbox (STK prompts send it there
too); Till Number shows for Buy Goods or sandbox.

Tests: stale upstream tests rewritten against current code (B2C disbursement and reference, C2B
URL, api); Mpesa Settings fixtures fixed (paybill type, company, POS opening entry, POS Invoice mode
on v16); suites no longer leak callback-enforcement settings into each other.

## Unused upstream features removed, app rebranded to Astral (2026-10-19, uncommitted)

Checked on Astral's live site before removing: no Stanbic Settings and no Stanbic payouts, no B2C
payouts against Loans, no POS payment by M-Pesa (8 POS-mode Sales Invoices and 3 POS Invoices,
none paid by phone), and the Mpesa Payment Reconciliation tool was never installed.

**Removed**
- Stanbic: the `stanbic` module (Stanbic Settings, Stanbic Sort Code), `connectors/stanbic`, the
  Stanbic Mobile / PesaLink payment types, and the bank fields on B2C requests and rows
  (`stanbic_settings`, `dbs_reference_id`, `next_execution_date`, `bank_*`). Also removed: the
  Bank `pesa_link_bank_code` field. B2C now pays from Mpesa Settings only.
- B2C against Loans: `accounting/loan_disbursement_service.py`, the Loan option in disbursement
  forms and reports, and the Loan Disbursement custom field.
- POS: the point-of-sale page script, `pos_invoice.js`, the KlikPOS STK endpoints, the legacy
  `verify_transaction` callback and its POS tests, the POS Invoice "Request for Payment" button and
  POS Settings field, and the POS branch of the quick-pay dialog.
- Event Booking branch in `handle_successful_transaction`.
- Mpesa Payment Reconciliation tool (with Mpesa Draft Payments and Mpesa Payments Invoices), its
  endpoints (`get_unallocated_payments`, `process_mpesa_c2b_reconciliation`,
  `process_mpesa_c2b_customer_credit`, `submit_mpesa_payment`, ...), the `is_manual_reconciliation`
  global flag, and its workspace links.
- On migrate, patch `remove_unused_upstream_features` deletes those DocTypes, their custom fields
  and the empty Stanbic module. POS Invoice `mpesa_receipt_number` is kept because it holds data.

**Kept**: the Sales Invoice desk tools (STK on an invoice or payment row, quick-pay dialog for
received M-Pesa payments), Payment Request STK (all 411 of Astral's prompts), and the
Payment Gateway DocType.

**Rebrand**: the app title is **Astral M-Pesa Suite**, publisher and author are Astral Industries
Limited, the README is rewritten, a NOTICE file is added, and the workspace no longer carries
Navari's test users. The Python package, module and DocType names don't change, because
Safaricom callback URLs and existing data depend on them. Navari's copyright headers and the AGPL
licence stay, as the licence requires. `docs/LICENSING.md` lists every file that came from upstream
for legal review.

## Sandbox Test Lab and one-shortcode accounts (2026-10-19)

**M-Pesa Sandbox Test Lab** (`sandbox_lab/`, DocTypes Mpesa Sandbox Test Run / Test Case; guide in
`docs/SANDBOX_TESTING.md`). Runs every flow against Daraja's sandbox and checks what the app did:
22 scenarios over health, C2B (exact / mistyped / unknown reference, duplicate, forged without
token, forged receipt, wrong shortcode), STK (paid, cancelled, forged "paid" while open, duplicate,
status check, no token), B2C (paid and booked once, duplicate result, settled payout not
re-queried), B2B (typed paybill refused, approved account paid, duplicate result) and reversal.
Tools: Simulate a C2B Payment, Replay a Callback (with or without token), Health Check. Checks run
every minute until Passed / Failed / timeout. Works only with site config `mpesa_sandbox_lab: 1`,
Sandbox-ticked Mpesa Settings and M-Pesa admin roles, so it can never use live keys.

**One shortcode for everything.** Safaricom's M-PESA Business One Account lets one number collect
(Paybill / STK) and pay out (B2C, B2B); collections and B2C both use the Utility account.
- `settings_for_shortcode` prefers a collecting record over a B2C-only one when several records
  share a number (then the oldest), instead of whichever the database returned first.
- C2B register auto-reconcile and its after-insert lookup now use that resolver: they matched
  `business_shortcode` only, so Buy Goods tills never matched, and `_reconcile_payment` crashed
  when no record was found.
- Saving an Mpesa Settings whose shortcode another record uses shows which record books receipts.
- Setup guide: one record per shortcode; the API operator needs a role per API.

## Daraja docs verified first-hand (2026-10-19, uncommitted)

Hermes read Safaricom's own Daraja pages and answered the 31 open questions
(`../docs/DARAJA_VERIFIED_2026-10-19.md`). `../docs/daraja_api_reference.md` now marks each item
verified, not stated, or contradicted by Safaricom's own pages. Code changed where a fact differed:

- **Banned words in callback URLs.** Daraja refuses callback URLs containing mpesa, M-Pesa,
  safaricom, exe, exec, cmd, sql or query, and the FAQ states this for every callback. Three
  upstream URLs broke it, so they are no longer sent; the old endpoints stay so results for requests
  already sent still land:
  - Account Balance ResultURL `balance_query_callback` → `m_pesa_api.account_balance_result`.
  - Pull Transactions register CallBackURL `doctype.mpesa_settings…pull_transaction_callback` →
    `m_pesa_api.pull_registration_result` (affects new registrations only).
  - B2C ResultURL loses the unused `?provider=Mpesa`.
  - `daraja.guard.callback_url_problems` checks HTTPS (production) and banned words. C2B URL
    registration now refuses to send a URL that fails, because production registration is
    one-time. A unit test scans every `@safaricom_callback` endpoint, and another fails if the app
    sends a legacy URL again.
- **C2B validation reply** is now exactly the documented shape: `{"ResultCode": "0"}` to accept, and
  `C2B00016` ("Other Error") when the IP or token check fails, instead of the undocumented `1`.
- **Amount limits.** STK refuses more than KES 250,000. B2C refuses amounts outside 10–250,000
  before sending (it only checked for whole shillings).
- **B2B Buy Goods sends AccountReference.** Daraja's BuyGoods body lists it and doesn't mark it
  optional. Paybill still requires the supplier's account number; a till without one gets the
  request's name.
- **OAuth tokens.** Each token request revokes the previous token. Token fetches now run under a
  Redis lock, so workers that miss the cache at the same moment reuse one token instead of revoking
  each other's. A token-error retry drops the cached token only if it is still the one Safaricom
  refused (`invalidate_token(rejected=…)`), so it no longer throws away a fresher token another
  worker just fetched. If Redis can't lock, the fetch goes ahead without the lock, as before.
- Checked and already right: the 12 published callback IPs are inside the default allow-list; RSA
  PKCS#1 v1.5; STK till mapping; every documented STK result code; B2C v3 with
  OriginatorConversationID; Reversal `RecieverIdentifierType` 11 and R000001; only C2B receipts can
  be reversed; Transaction Status `OriginalConversationID`; Account Balance column order
  (Current|Available|Reserved|Uncleared); `500.003.1001` treated as already registered; Pull paging
  by page number.
- Sandbox guide: one app per product if the portal won't combine C2B and B2C; Short code 2 is
  B2B's Party B; STK test money auto-reverses at 6am.

Still open with Safaricom (pages conflict or are silent): STK Timestamp time zone; what happens to
an AccountReference over 12 characters; one app for C2B and B2C; `BusinessPayToBulk` vs
`BusinessTransferFromMMFToUtility`; B2B from a Business One Account or a till; Pull offset
semantics; the QueueTimeOut payload.

## Per-API switches on Mpesa Settings (2026-10-19, uncommitted)

One shortcode can collect and pay out (M-PESA Business One Account), so the single required
upstream **API Type** is replaced by one switch per API, under **APIs this record uses**:
Receive C2B, Send STK Prompts, B2C Payouts, B2B Supplier Payments, Reversals
(`frappe_mpsa_payments/daraja/apis.py`).

- **What a switch gates:** what the app *sends*. STK (Express Request, `initiate_stk_push`,
  Mpesa API Client), C2B URL registration, B2C payouts, B2B payments and Reversals each refuse
  with "does not have … ticked" when their switch is off: when the draft is saved, and again
  just before sending. Payments Safaricom reports are always recorded, whatever the switches say.
  Transaction Status and Account Balance are read-only and have no switch, because C2B and B2C
  verification depend on Transaction Status.
- **Migration** (patch `mpesa_settings_api_switches`): each record gets the switch for its old
  API Type, plus every API it was already used for. That means C2B for any record with a C2B
  Register URL, and B2C, B2B or Reversals for any record with a request of that kind. Everything
  else starts off. **Old API Type** stays on the form, read-only. A new record created by code that
  still sets `api_type` gets the matching switch.
- **Booking received money:** when several records share a shortcode, the one with Receive C2B or
  Send STK ticked wins (was: anything not "MPesa B2C"), then the oldest.
- **Sandbox Party A:** instead of a new `sandbox_party_a` field, the sandbox test shortcode
  (600xxx) already kept in **Till Number** (used for C2B registration, Pull and Status) is now also
  Party A for B2C, B2B, Reversal and Balance (`apis.initiator_party_a`). Live records still send
  the Business Shortcode, so nothing changes in production. One sandbox record can hold 174379 for
  STK and 600xxx for everything else.
- Saving a record needs at least one switch. Ticking a money-out switch without an Initiator shows
  a warning. The Sandbox Lab health check lists each record's switches and flags any its tests need.
- Not done: a separate Working Account bank mapping for B2B and B2C top-ups (B2B debits the Working
  account, collections and B2C use Utility).
- Rehearsed on a restored copy of Astral's live site (2026-10-20): Navari 15.3.1 replaced by the
  suite, `migrate` clean, no rows or columns lost. The rehearsal showed 4134019 (old type "MPesa
  B2C") had sent 21 STK prompts and receives C2B, so the patch also keeps STK for any record with
  Express Requests and C2B for any record with C2B receipts on its shortcode. Result there:
  4139683 = B2C; 4134019 = C2B + STK + B2C; 208911 = C2B + STK.

## Fixes found by the first Sandbox Lab run (2026-10-20, uncommitted)

Ran the lab against Daraja's sandbox from a local v15 bench behind an ngrok tunnel. 10 tests
passed, including STK end to end on a real phone: paid and booked once, unanswered prompt books
nothing, a re-sent callback changes nothing, the status check leaves it booked once, and a forged
"paid" callback's receipt is never booked. The run found these bugs:

- **C2B URL registration marked a success as Failed.** The sandbox's v2 registerurl answers
  `ResponseCode "00000000"`, not the documented `"0"`. New `daraja.guard.is_zero_code` accepts any
  all-zero code; used on every "request accepted" check (registerurl, B2C/B2B/Reversal/Status acks,
  C2B verification, lab simulate). Result codes in callbacks are unchanged.
- **Account Balance and STK prompts crashed outside a web request** ("object is not bound").
  `build_callback_url` read the request URI through `get_request_site_address(True)`; it now uses
  `get_url()`, so background jobs and scripts work.
- **An STK prompt made on a Sales Invoice was paid but never booked.** Upstream appended a
  payments row to the submitted invoice and saved it, which Frappe refuses
  (UpdateAfterSubmitError). It now books a Payment Entry against the invoice (receipt as
  reference, one per receipt, allocation capped at what is owed). Astral's live prompts all use
  Payment Requests (326 of 326), so this path had never run there.
- **Every successful B2C payout failed to record.** The result handler copied the payout status
  ("Paid") onto the Integration Request, whose status cannot be "Paid", so the save failed and the
  whole callback rolled back: the payout stayed Initiated and was never booked. This came from
  upstream's version-16 branch (`status=req.status`); Navari 15.3.1, which Astral runs live, used
  "Completed". It now logs Completed/Failed, and `update_integration_request` maps any other value.
- **A supplier account whose receiver name did not match could still be paid.** After a B2B payment
  whose M-Pesa receiver name differs from the approved supplier name, the account is now blocked:
  new B2B requests to it are refused with the reason until an M-Pesa manager checks with the
  supplier and presses **Receiver Name Confirmed** (optionally adopting the reported name).
- Lab: the C2B arrival checks ignore the lab's own `ZZ` forged receipts (the forged-receipt test
  uses the same invoice number), and `post_callback` commits before posting, so a forged callback
  for a prompt made a moment earlier no longer waits on an uncommitted row.

Because the sandbox initiator stayed locked, B2B, B2C, C2B and Reversal were also run on the lab site
with only Safaricom's side simulated: its documented ack, and its verified result / confirmation
samples posted through the public URL. Everything else ran for real. B2B: payload, Paid, Payment
Entry against the Purchase Invoice (draft or submitted per setting), re-sent result, failure,
receiver-name alert and block. B2C: payload, Paid, row and disbursement status, Payment Entry,
re-sent result, failure. C2B: exact reference, mistyped reference repaired, unknown reference left
for review, duplicate confirmation. Reversal: Reversed, second reversal refused, R000001.

Sandbox behaviour seen (not app faults): the shared test initiator answered 8006 "API password
is locked", so Balance, B2B, B2C and Reversal cannot pass until Safaricom unlocks it. C2B simulate
is accepted but confirmations never arrive, because test shortcodes like 600584 are shared and
Safaricom delivers to whoever registered URLs last. The sandbox sometimes completes an STK prompt
by itself. Safaricom's firewall (Incapsula) blocks bursts of calls with a 403 page; a retry after a
pause works. These flows get their proof in the KES 1-10 live checks at go-live.

## Full code review: 28 confirmed fixes (2026-10-21, 50987de)

A multi-agent review of the whole app found 38 suspected defects. Independent verifiers refuted
10 and confirmed 28. Each fix has tests and was checked by adversarial verifiers. A cross-check
and two more rounds then closed the gaps found between areas. Unit tests: 301 before, 813 now;
the full bench suites (255 tests) pass on v15 and v16, and a KES 10 STK prompt on a Payment
Request was paid on a real phone through the Sandbox Lab and booked once. **Run `bench migrate`**: this adds a Match Log method,
makes the webhook URL Small Text, changes the Mpesa Settings help text, adds an index on B2C
Disbursement Request.reference_name, marks the payout row's result fields no_copy, and runs two
patches (`drop_text_search_index_setters`, `relink_b2c_reference_rows`).

**Money in**
- **C2B confirmations register again with verification on (critical).** Safaricom posts the
  Transaction Status result as JSON, so the `?ir=` on the Result URL never reached the handler and
  every verified confirmation failed after three queries. The handler now reads `ir` from the URL,
  or finds the held request by the result's exact ConversationID.
- **Guest callbacks cannot use SQL wildcards.** A forged ConversationID of `%` used to match staff
  "Fetch transaction" queries and held C2B confirmations. Callback IDs must now be Safaricom-shaped
  (`guard.daraja_id`), and the stored ack must name them exactly (`guard.names_id`). Account
  Balance results touch only balance queries. Check Transaction Status registers a receipt only
  when TransactionStatus is Completed and the money reached one of our shortcodes.
- **A receipt is booked once.** An STK receipt that also arrives as C2B is booked once, decided on
  committed rows with locking reads on both sides. The orphan-STK matcher adopts only prompts
  Safaricom never answered. A cancelled or deleted register is never re-imported by Pull,
  Transaction Status or held confirmations (amend or restore it to re-book).
- **Refunded money is never booked.** A receipt with a live reversal is refused by the register's
  `before_submit` (every path: hand, matcher, prepaid orders, POS), by STK booking (with a locking
  read, so a reversal being sent is waited for), by POS quick pay, and by the Payments API. The
  reversal is found by its register or by the receipt.
- **POS money is not counted twice.** POS quick pay no longer lists or accepts drafts that are
  reversed, already booked, or a payment intent's receipt. An STK booking refuses, instead of
  cancelling, a register a cashier put on an invoice.
- **Payment Requests are prompted once.** A Payment Request above the Transaction Limit (at most
  250,000) is refused before any prompt goes out. Before, it was split into prompts that Safaricom
  refuses while the first is pending. **At go-live, raise 4134019's Transaction Limit from 150,000
  to 250,000.** A paid prompt books what it collected. The cents a whole-shilling prompt cannot ask
  go to Round Off, and a Sales Order billed in the meantime is booked against its invoices.
- **Re-prompts.** Sending a prompt again is refused while the request is paid, its callback is
  undecided, or the last prompt is under 90 seconds old. A re-prompt resets the old settlement under
  a lock, and the status poller, give-up timer and booking retries count from the latest prompt.
- **Hourly Pull recovers outages:** the last 2 hours, then 47 hours (Safaricom keeps 48).
- **Prepaid orders** refused by ERPNext (for example the credit limit) stay drafts, are logged once
  with a hint, and are retried with backoff. Real bugs get an Error Log with the traceback.
- Register amount, receipt and shortcode cannot be edited once saved. The `search_index` Property
  Setter on Bank Transaction's Small Text Reference Number, which made every Customize Form and
  Custom Field save on Bank Transaction fail, is removed and no longer recreated on migrate; its
  prefix index stays, so the duplicate check still uses it. Unconfirmed STK callbacks reach the
  Match Log ("STK Callback").

**Money out (B2C, B2B, Reversal)**
- **"Check Latest Payment Status" no longer overwrites rows.** It used to save the browser's stale
  copy over the payout jobs' request links, so paid payouts could never be booked. A patch relinks
  rows whose link was wiped, and every Result rewrites it.
- **Each payout is booked by its own M-Pesa receipt.** Two rows of one disbursement for the same
  invoice each get an entry, where before the second was marked booked with none.
- **No double payouts.** A Salary Slip's owed amount goes down by every B2C payout against it.
  Retry and submit both take the per-document lock and refuse when the document was paid since,
  when another payout is open, or when a Paid payout has no live entry (entry cancelled, Cancel All,
  older than 30 days). Rows must be whole shillings, 10 to 250,000.
- An advance paid against a Purchase Order still counts as booked after ERPNext moves it onto the
  invoice made from the order (Get Advances, Payment Reconciliation), so the next advance on the
  order is not refused. Desk **Duplicate** no longer copies a row's request link and result
  (no_copy; amend still keeps them), and a link copied any other way never hides another
  disbursement's live payout.
- Salary Slip Journal Entries carry the receipt as Reference No, so ERPNext can submit the Bank
  Entry. Failed results reach the row (queued after commit).
- **Results:** a forged Result without IDs no longer settles an acked B2B, Reversal or status
  request. `ref` is read from the URL and trusted only next to the callback token. A reversal of a
  receipt an STK prompt booked says the entry must be cancelled by hand, and one on a POS or Sales
  Invoice names the invoice whose M-Pesa payment must be returned.
- **Daraja tokens:** reads always go to Redis, so a v15 cache miss no longer mints a token per call
  and revokes other workers' tokens. A refused token is dropped only if Redis still holds it.

**Payments API**
- A `callback_url` over 140 characters no longer stops the payment from becoming paid. A webhook
  that cannot be queued no longer blocks the status change.
- A second payment on a paid intent is booked on account, and staff are told. A paybill receipt is
  booked with the receiving shortcode's settings. Money paid to another company's number is left
  for staff.
- Reversed receipts and receipts already on a POS or Sales Invoice are `claimed` and refused.
  See docs/PAYMENTS_API.md.

**Go-live checks added by this review**
- Before retiring the AIL Mpesa Feed Pull Sweep, run one manual Pull over a busy 47-hour window.
  Imported plus skipped must equal Safaricom's TotalRecords; this proves OffSetValue is a page
  number.
- Watch the first C2B confirmations. Verification now fails closed when a Transaction Status result
  has no CreditPartyName; such receipts arrive through Pull or a manager's release, with an Error Log.
- Prepaid orders: the "Approve Paid Orders As" user needs Sales Creator, Sales Approver Primary,
  Sales Approver Secondary and the Accounts Settings Credit Controller role.

## Upgrade rehearsal 2 on the Astral copy (2026-10-21)

`astral.local` (restored live copy, Frappe 15.121.3 / ERPNext 15.121.6, 17 apps, scheduler
paused) moved from 8fb4b49 to 75de2f1. Backup `20261021_153208` was taken first. `migrate` took
40s and lost no rows or columns. Payment Entries, GL, Journal Entries, Bank Transactions and every
M-Pesa table are unchanged, and the Mpesa Settings switches are as before. The two new patches did
exactly their job:
- `drop_text_search_index_setters` removed the one setter on Bank Transaction's Small Text
  Reference Number, and `ensure_lookup_indexes` did not put it back. Every index stayed. Customize
  Form and Custom Field saves on Bank Transaction work again; before, they failed with
  "Fieldtype Small Text ... cannot be indexed".
- `relink_b2c_reference_rows` restored the one wiped row link (Expense Claim HR-EXP-2026-06055,
  KES 2,344; its Payment Entry exists).

The M-Pesa scheduled jobs then ran once each with outbound network blocked, rather than switching
on the whole scheduler, which would also run the 16 other apps and Astral's server scripts. None
failed. The accounting sweep matched 412 Paid payouts of the last 30 days to the Payment Entries
Astral's current flow had made, and created no new entries. It also found these fixes:
- **Nothing more is booked for a payout whose receipt is already on another entry.** Astral's
  flow made some payout Payment Entries as unallocated advances, or on the invoice made from the
  paid order; a Bank Reconciliation Journal Entry can also carry the receipt. The sweep tried to
  book again every 15 minutes, and Payment Entry's duplicate-receipt guard refused each time.
  Now the payout gets one Error Log and a note for its case: "not allocated: allocate it to
  <document>", or "it is on <document>: if that is what this payout paid, nothing more is
  needed". There were 7 such payouts on Astral. The payout stays unbooked and is re-checked
  quietly each pass. It is booked once that entry stops carrying the receipt, for example after
  it is cancelled as another payout's, or once an entry for the document carries it. The
  double-payout guard keeps counting it meanwhile, so the document cannot be paid again.
- **A row paid before the suite, without its receipt, takes its Paid request's receipt.** The
  receipt is written to the row before any entry is made.
- **Error logging works on v15.** v15 stores a single-line first argument of `frappe.log_error`
  as the Error Log title (140 characters), so a long message, such as a whole C2B payload or an
  exception text, made the log itself fail. Calls now pass `title=` and `message=`, and a unit test
  checks every call.

## First live B2B payment (2026-10-22)

Safaricom approved B2B on all three shortcodes. From the Sandbox Lab with the production keys
(entered by the user in a hidden form), KES 10 BusinessPayBill went from 4139683 to Astral's own
paybill 208911, account B2B-TEST, through an approved Supplier M-Pesa Account:
- First try: Failed, ResultCode 1 "The balance is insufficient". **B2B debits the Working (MMF)
  account, which was KES 0; the B2C float sits in Utility.** The result was recorded as Failed with
  the reason, and nothing was booked.
- After the user moved funds into Working: **Paid**, receipt UJMTM632YM. Safaricom reported the
  receiver as "208911 - ASTRAL INDUSTRIES LIMITED", which matches the expected name, so the account
  stays unblocked. Charge KES 2 ("Business Pay Bill Charge"); Working balance after KES 1,488.
  The Payment Entry carries the receipt and booking it again creates no duplicate. On the lab it
  needed a Bank Account mapped on the Mpesa Settings record first.
- To a bank: KES 10 to KCB paybill 522522 (the user's own account, entered and sent by the user)
  was **Paid**, receipt UJMTM6331C. The receiver was reported as "522522 - KCB Paybill AC", which
  matches the expected "KCB". Charge KES 2; Working balance after KES 1,476. The Payment Entry was
  booked automatically.

**Go-live checks for B2B**
- Keep a float in each paying shortcode's **Working** account (B2C keeps using Utility).
- Map the **Bank Account** (Accounting Mapping) on every Mpesa Settings record that pays by B2B.
- Approve each supplier's paybill or till as a Supplier M-Pesa Account (proof attached, approved by
  a second person), with the name M-Pesa shows as the expected name.
