# Plan: M-Pesa Bulk Payout (casual wages and overtime, employees allowed)

## Context
At Astral, a bulk claim (HR-EXP-2026-07292-1: 69 people, KES 247,652) could only half be paid.
- The suite's Expense Claim casual payees refuse every employee or staff phone.
- 32 of the 69 were payroll employees paid overtime.
- Before the 8 Nov cut-over, Astral's own scripts paid such lists without limits.

You want a dedicated sheet:
- casual wages and overtime in bulk, to casuals and employees alike;
- a project and cost center per line, with its expense type for the accounting entry;
- easy paste or upload.

Generic for both sites, Trident first.

Decisions taken (13 Nov 2026):
- **Safety:** employees and casuals are all payable. The only block is self-pay: whoever prepares, approves or sends a sheet cannot be paid on it, by phone or by M-Pesa name. Name mismatches after payment are flagged, as today.
- **Account per row:** the Expense Claim Type (e.g. (J005) Casual Salaries, (J021) Overtime Expense), resolved to its account for the company. A sheet default fills each row; any row can change it.
- **Approval:** an M-Pesa Manager or Accounts Manager **submits** the sheet. An M-Pesa Payer then prepares and sends the payout (maker-checker per settings, now off).
- **Booking:** **one bulk Journal Entry per sheet, posted after the payouts finish.**
  - It books only the rows M-Pesa paid.
  - Debit lines are grouped by expense account, cost center, project and accounting dimensions; the credit goes to the shortcode's M-Pesa (Utility) GL.
  - Failed rows are never booked. A row paid later on retry gets its own small Journal Entry.

## Design

### New DocTypes (module Mpsa Suite, submittable, track changes)
- **M-Pesa Bulk Payout** (naming `MBP-.YY.-.MM.-.####`).
  - Header fields: company, posting_date, purpose (title), mpesa_settings (B2C shortcode), defaults (expense_type, cost_center, project), totals (rows, amount, paid, failed), status, amended_from.
  - Status: Draft → To Pay → Paying → Paid / Partly Paid → Cancelled.
- **M-Pesa Bulk Payout Row** (child table). Fields:
  - who and how much: payee_name, phone (stored 2547…), amount;
  - accounting: expense_type, expense_account (read-only, from the type and company), cost_center, project;
  - employee (read-only; filled when the phone is an employee's, for reporting and PAYE follow-up);
  - remarks;
  - results, read-only and allow_on_submit: status (Draft/Queued/In Flight/Paid/Failed/Cancelled), payout, receipt, paid_on, receiver_name, name note and verdict, error_message, journal_entry;
  - names: mpesa_name, mpesa_name_from, typed_name (round 4.3).
- Add the row DocType to the `accounting_dimension_doctypes` hook, so ERPNext adds the active dimensions as row fields (Astral has Department active) and the Journal Entry carries them.

### Flow
1. **Draft.** The preparer (Accounts User, HR User, M-Pesa Manager) adds rows with **Paste Rows**.
   - Paste from Excel, upload CSV/XLSX, or download a template.
   - It reuses the paste reader from claims (`read_rows`). Optional columns are Expense Type, Project and Cost Center; missing values come from the sheet defaults.
   - On save, each row is checked:
     - a payable Safaricom number;
     - whole shillings, 10 to 250,000;
     - an expense type with an account for the company;
     - a cost center of the company and an open project;
     - the same phone twice with the same expense type is warned about.
   - A phone M-Pesa paid before takes M-Pesa's name, locked (round 4.3), employees included.
   - Self-pay: the session user's phones (User mobile and phone, their Employee's cell number and Employee M-Pesa Accounts) are refused.
2. **Submit = approval** by a named M-Pesa Manager or Accounts Manager. All checks run again. Self-pay is checked for the approver too, by phone and by name.
3. **Pay by M-Pesa** (form button, `PREPARE_ROLES`). It creates one draft **B2C Payment Disbursement** for the Draft rows:
   - reference_doctype "M-Pesa Bulk Payout", reference_name the sheet;
   - a new row link field `bulk_payout_row` on the B2C Payment Disbursement Reference;
   - **no party**.
   An M-Pesa Payer then uses the existing **Send via M-Pesa** confirm screen, which checks self-pay for the sender. **Retry Failed** makes a new batch for the Failed rows. **Settle Outside M-Pesa** (manager, with a note) cancels a failed row.
4. **Results.** A hook after `update_b2c_reference_status` copies each row's status, receipt, paid time and receiver name onto the sheet row. It runs the name check (comment and alert on a mismatch) and refreshes the sheet status.
5. **Bulk Journal Entry.**
   - When no row of the sheet is Queued or In Flight, `book_sheet` (under the sheet's lock) posts one submitted Journal Entry for every Paid row that has no journal yet.
   - Debit lines: grouped by expense account, cost center, project and dimensions.
   - Credit: the shortcode's Utility GL (`b2c_utility_gl`, else paid_from).
   - Remarks list the sheet and the count; each row stores the journal.
   - The existing 15-minute unbooked sweep also books sheets left waiting.
   - A retry that succeeds later is booked by the same rule (a new, small JE).
6. **Bank reconciliation.** Each payout still gets its own statement line and its charge Journal Entry (`statement.feed`).
   - A new branch in `reconcile_withdrawal`/`entries_for`: a payout of a bulk-payout row allocates **its row amount** from the sheet's journal.
   - ERPNext v15 and v16 allow one voucher to be allocated across many bank transactions (`bank_transaction.allocate_payment_entries`, `get_clearance_details`).
7. **Cancel:** only while no row is Queued, In Flight or Paid. The Payment Entries and Journal Entries are left alone.

### Safaricom delays and failed entries (your 13 Nov request)
The rule throughout: **a row is sent again only when M-Pesa has said it failed.** A row whose fate is unknown is asked about, never re-sent, because the money may already have moved.

**Delays from Safaricom**
- **Row states shown on the sheet:**
  - Queued: in a batch, not sent yet.
  - Sent, waiting for M-Pesa: an acknowledgement, no Result yet. The row shows "since HH:MM".
  - Checking with M-Pesa: no acknowledgement or a transport error (B2C "Status Unknown").
  - Paid, Failed or Cancelled.
- **Late Results:** a Result arriving minutes or hours late is applied whenever it comes (the existing callback, locked, a repeated final Result ignored). The row hook then updates the sheet.
- **No Result at all:**
  - The existing sweep `b2c_scheduler_service.check_unsettled_payouts` sends a Transaction Status query (`b2c_status.start_query`, cooldown 2 min) after the set minutes, and its answer settles the row.
  - The sheet gets a **Check with M-Pesa Now** button (M-Pesa Manager or Payer) that queries all its waiting rows. It respects the cooldown.
- **The bulk journal is not held hostage by one stuck row:**
  - it posts when nothing is waiting;
  - or, after a set time (default 2 hours, a Suite setting), it posts the Paid rows anyway;
  - a row that settles later gets its own small journal.
- **Queue timeouts and acknowledgements that arrive before we recorded the send** are handled by the existing `handle_queue_timeout` and `retry_waiting_results`. Nothing new is needed; the row just shows Checking.
- **Big sheets (70+ rows):**
  - each row is its own B2C request, queued as a background job (as today);
  - a Safaricom connect error fails the row fast (Failed, so it can be retried);
  - a read timeout makes it Checking (never re-sent).

**Float before sending**
- The send screen shows the shortcode's Utility balance (latest balance snapshot or query) against the sheet total and warns when it is short.
- "Insufficient funds" Results are grouped in one message: "top up, then Retry Failed".

**Failed payouts need retrying**
- **Retry Failed** (preparer roles): one new batch for the rows M-Pesa failed.
  - Each attempt is a new request with a new conversation ID, through the existing `retry_failed_payment` path and its checks.
  - Each row keeps its attempt count and last error. `daraja/money_out.failure_hint` explains the common codes in plain words: number not on M-Pesa, limit exceeded, insufficient funds, invalid receiver.
- **Correct Phone and Retry** (M-Pesa Manager only, on a Failed row, with a note): the change is logged in a comment, self-pay and the name check run again, then the row is retried.
- **Mark as Not Paid** (M-Pesa Manager, for a row stuck at Checking after the status query found nothing, having checked the M-Pesa statement): the row becomes Failed and can be retried. This is the existing "manager cancels an unknown request" pattern.
- **Settle Outside M-Pesa** (manager, with a note): the row is closed and no payout is made. It is left out of the bulk journal, because its cost is booked where it was paid.

**Failed accounting entries need retrying**
- If the bulk journal fails to post (closed period, frozen account, missing account or cost center), the rows stay **Paid, not booked**.
  - The sheet shows the reason (`closed_period_hint`).
  - The existing sweep `retry_unbooked_payouts` tries again every 15 minutes for 30 days.
  - **Book Now** (M-Pesa Manager or Accounts Manager) retries at once after the cause is fixed.
  - The Notify Role gets an alert.
- A journal is never posted twice: the sheet lock, plus each row's `journal_entry` link checked before posting.
- If a statement line doesn't reconcile automatically, it stays open for manual matching. The sheet lists paid rows whose bank line is not reconciled yet.

**Follow-up**
- A daily alert to the Notify Role lists sheets that are part paid, failed, or waiting more than 24 hours, with counts and amounts.
- The sheet's status reads e.g. "Partly Paid: 64 paid, 3 failed, 2 waiting".

### Changes to existing B2C machinery (each guarded by the new reference type, so current flows are unchanged)
- `b2c_payment_disbursement.py`:
  - `validate_party_type` (DISB:312) and the row party check (REF:18-23; ED:543 `row_mismatch`) allow an empty party when reference_doctype is M-Pesa Bulk Payout;
  - add the type to the JS and `DOCTYPE_CONFIGS` only if it is ever picked by hand (not needed for Pay by M-Pesa).
- `b2c_payment_disbursement_reference.py:70-81`: a branch that checks the row link (sheet submitted, same phone and amount, row not paid or on its way).
- `services/b2c_postprocess_service.py`:
  - `_book` (POST:89-106): for the new type, defer to `bulk_payout.book_sheet`;
  - `booked_docstatus` (POST:300-367): the row's journal submitted means booked;
  - `closed_period_hint`.
- `services/b2c_response_service.py:84-86`: call `bulk_payout.on_payout_result`.
- `money_out/statement.py` `entries_for`/`reconcile_withdrawal`: the partial-allocation branch above.
- `money_out/send_confirm.py`:
  - show the sheet's rows (name, phone, amount, project) in the send summary;
  - the self-pay check for the sender.
- `money_out/claim_payees.py` `mpesa_names` (CP:1007): include payouts of the new type, so later name lookups learn those names.

### New module `money_out/bulk_payout.py`
- validate and clean rows; resolve accounts;
- self-pay checks, reusing `employee_destinations().employee_of` and the user's phones, `staff_matches`/`involved_users` and `own_payment_problem`;
- `parse_rows` (whitelisted), `pay`, `retry_failed`, `settle_row`;
- `on_payout_result`, `book_sheet`, sheet status.

Reused from claims:
- `read_rows` and `parse_amount`; the line-state helpers `b2c_state` and `deciding_payout`;
- `name_check`, `mpesa_names`, `name_from`;
- `utils.phone` (`payable_msisdn`, `network_of`) and `utils.privacy`;
- `owed.doc_lock`, `common.payout_cost_center`, `wallet.accounts.b2c_utility_gl`.

Booking is modelled on `b2b_entries.build_journal_entry` and `_journal_account`. Journal Entry Account rows get project and dimensions, which no suite journal sets today.

### UI
- Form JS:
  - Paste Rows dialog: shared code with the claim dialog, without the expense-line parts;
  - a summary bar with paid, failed and left, by project;
  - buttons Pay by M-Pesa, Retry Failed and Settle Outside M-Pesa;
  - a link to the journal.
- List view: indicators by status. Workspace link on the M-Pesa workspace.

### Permissions
- `permission_matrix.py` DOCTYPES entry for the new DocType: create/write for the preparer roles; submit, cancel and amend for M-Pesa Manager and Accounts Manager; read for M-Pesa Payer.
- ENDPOINTS for each whitelisted function.
- Regenerate docs/ROLES_AND_PERMISSIONS.md; `test_permission_matrix.py` enforces the match.

### Before this: finish round 4.4's paste reader
The second review's open findings in `read_rows` are shared with this sheet:
- thousands joined only on comma-split text;
- "total" only as a whole header;
- a phone with text still ends the header search;
- the paid-row guard also without a header, and for In Flight, Queued and Cancelled.

Fix, test and commit them on `r4-4-bulk-casual` first. The sheet builds on that branch, in a new branch `r4-5-bulk-payout`.

## Built in the same round: paying supplier tills
The companion plan is [PAY_VIA_STAFF_PLAN.md](PAY_VIA_STAFF_PLAN.md), revised 13 Nov to your decisions:
- **The Petty Cash Phone is the main method.**
  - It is topped up by M-Pesa from the shortcode (a party-less B2C payout, one Journal Entry each, reconciled one to one).
  - The cash handler pays tills from it, and each spend is recorded on its PO or PI from the pasted M-Pesa SMS (a Payment Entry from the petty cash account).
  - A daily check compares the book balance with the balance on the last SMS.
- **Pay via Staff** (an Employee Advance to a staff member's own approved number) is kept, **strict: only the users listed in the settings (you and Janki).**

Both are built on branch `r4-5-bulk-payout`.

What they share (built once, first):
- **The party-less B2C branch:** a row link on the B2C Payment Disbursement Reference (`bulk_payout_row`, `petty_cash_phone`), a booking dispatch per reference type, and the `booked_docstatus` branches.
- **`money_out/self_pay.py`**: the self-pay helper.
  - It finds the phones and names of a user: the User's mobile and phone, and their Employee's cell number and Employee M-Pesa Accounts (`employee_destinations().employee_of`).
  - It checks them, by phone and by M-Pesa name, against the people being paid (`staff_matches`/`involved_users`).
  - The bulk sheet uses it at save, submit and send. Pay via Staff uses it with the existing `check_not_own_b2c`.
- **One daily "needs attention" alert** (a scheduler job, to the Notify Role) with three sections:
  - bulk sheets that are part paid, failed, or waiting more than 24 hours;
  - petty cash phones: the book balance against the last SMS balance, and money left unspent for `petty_cash_alert_days`;
  - staff advances paid more than `staff_advance_settle_days` (default 3) ago and not yet settled.
- **New M-Pesa Suite Settings:**
  - `bulk_payout_book_after_hours` (default 2: post the paid rows while one row waits);
  - `pay_via_staff_users` (the users allowed; empty means nobody);
  - `staff_advance_settle_days` (default 3);
  - `petty_cash_alert_days` (default 7).
  All are filled in on upgraded sites by `install.backfill_single_defaults`.
- **Payment delays, Transaction Status queries and Retry Failed** for top-ups and advances reuse the existing B2C paths. Nothing new is needed there.

Order of work for the round:
1. The paste-reader fixes (round 4.4).
2. The party-less B2C branch, `self_pay.py` and the daily alert job.
3. The bulk payout sheet.
4. The Petty Cash Phone: setup, top-up, spends from SMS.
5. Pay via Staff, strict.
6. Unit and bench tests for all three on v15 and v16.
7. One review of the whole round.
8. Ask to push.
9. Trident pilot: a small sheet, the petty cash phone with one top-up and one petrol purchase from its SMS. Then Astral.

## Critical files
- New:
  - `mpsa_suite/mpsa_suite/doctype/m_pesa_bulk_payout/` (json, py, js, _list.js, bench test);
  - `mpsa_suite/mpsa_suite/doctype/m_pesa_bulk_payout_row/`;
  - `mpsa_suite/money_out/bulk_payout.py`;
  - `tests/unit/test_r45_bulk_payout.py`.
- Changed:
  - `doctype/b2c_payment_disbursement/b2c_payment_disbursement.py`;
  - `doctype/b2c_payment_disbursement_reference/*.py`, plus its new field in `migrate.py` or the JSON;
  - `services/b2c_postprocess_service.py`, `services/b2c_response_service.py`;
  - `money_out/statement.py`, `money_out/send_confirm.py`, `money_out/claim_payees.py` (mpesa_names, shared paste);
  - `public/js/expense_claim_payees.js` (shared paste dialog), `hooks.py` (`accounting_dimension_doctypes`);
  - `permission_matrix.py`, `docs/ROLES_AND_PERMISSIONS.md`, `SUITE_CHANGES.md`, the workspace JSON.

## Verification
- **Unit tests** (tests/unit, fake frappe):
  - row checks and self-pay, for preparer, approver and sender, by phone and by name;
  - account resolution;
  - Journal Entry grouping by account, cost center, project and dimensions;
  - "book only when nothing is in flight"; late-retry journals;
  - the no-party B2C branch; `booked_docstatus`; reconcile allocation;
  - the permission matrix. Then the full unit suite.
- **Bench tests on v15 and v16**, with the private runner (run43.sh-style, throwaway site, netguard), in one full scenario:
  - a sheet with casual and employee rows over 2 projects and 2 cost centers is approved, paid, and gets Results (some Paid, one Failed);
  - one journal books the paid rows with the right grouped lines and dimensions;
  - the statement lines reconcile with partial allocations;
  - a retry gets a second journal; cancel is refused once paid;
  - self-pay is refused at save, submit and send.
  - **delay and failure cases**, with Results handed over as Safaricom posts them:
    - a late Result hours after the send;
    - no Result, then a Transaction Status answer;
    - a transport error, after which the row stays Checking and is never re-sent;
    - "insufficient funds" on 3 rows, then a top-up and Retry Failed;
    - a journal that fails on a closed period, then Book Now;
    - the 2-hour rule posting the paid rows while one row waits.
  Then the full suite.
- **Review:** one independent reviewer at max effort on the diff, before asking to push.
- **Rollout:**
  - push only with your OK; you deploy to Trident and run `bench migrate`;
  - pilot a small sheet (2–3 rows) on Trident; check the journal and the bank reconciliation;
  - then Astral.
  - At Astral, the 32 staff on HR-EXP-2026-07292-1 (KES 121,368) move to a sheet. First lower the claim's lines #1, #2, #3 and #5 by their amounts, so nothing is booked twice.

## Notes
- **PAYE:** overtime paid to employees outside payroll is not taxed by payroll. The row's Employee link lets payroll or the accountant pick it up (a report by employee).
- **Builders and reviewers:** builders run at xhigh effort, reviewers at max (your CLAUDE.md rule).
