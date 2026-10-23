# Payouts design: B2B from Purchase Invoices and Orders, pay first, payment schedule, employees and payroll

Draft, 2026-10-23. Merged by a judge from three independent designs, each checked against the code and the ERPNext/HRMS source.

Status: Phases 1, 2 and 3 are built on mpsa-suite (SUITE_CHANGES.md, "Payouts Phase 1", "Payouts Phase 2" and "Payouts Phase 3"). Phases 1 and 2 passed their bench tests on the v15 and v16 benches. Phase 3 is unit-tested; its bench tests (mpsa_suite/money_out/test_payroll_payouts.py) are written but not yet run. Phase 4 is not built.

## Decisions taken (2026-10-23)

- Branch: build on `mpsa-suite` (package `mpsa_suite`); Trident does its rename sooner and then gets these features; Astral after its own rename.
- Trident has its own shortcode with B2B and B2C approved, separate from Astral's: the Trident production test uses it.
- A payment with a supplier but no invoice or order books automatically as a supplier advance (today's behaviour); "Make the entry later" is the opt-in.
- Schedule maker-checker: on by default, with a Suite setting to turn it off for one-person teams.
- No withholding deduction in Phase 1. Balances under KES 1 left by whole-shilling payments stay outstanding (an opt-in write-off may come in Phase 2).
- Testing is local only (unit tests, plus bench tests on the WSL v15 and v16 benches); GitHub CI is not used. HRMS is installed on both local benches for Phase 3 (v15.64.5 and v16.52.0).
- HRMS is required (hooks.required_apps = erpnext, hrms): both target sites run it. Nothing is built or tested for a site without HRMS.

## Phases

PHASE 1: first production test, Trident pilot first, then Astral.

What ships:
- Guard fixes:
  - re-validation at submit and at send (closes the typed-paybill bypass and the stale owed, limit and own-shortcode checks);
  - B2B and B2C see each other;
  - draft and cancelled entries keep the document blocked;
  - supplier, PI and PO checks before any money leaves;
  - Integration User booking runs as Administrator;
  - the Mode of Payment GL swap is fixed;
  - payment-term allocation;
  - late Result on a cancelled request;
  - receipt-on-another-entry and JE receipt checks;
  - v16 remarks.
- Purchase Order link and advance booking.
- "Pay via M-Pesa (B2B)" buttons on PI and PO, and M-Pesa sections in their Connections.
- "When Paid" choice and "Accounting Entry" state.
- Make Entry and Check Entry Again.
- Work-list shortcut and list indicators.
- 15-minute refresh and retry sweep, with a reminder.
- after_migrate backfill. No patch.
Suggested split, as three reviewable PRs:
1. guards and the owed helper with the B2C cross-guard;
2. the booking engine with entry state, sweep and Make Entry;
3. Purchase Order support plus UI.

Needs from you for Phase 1:
- Answers to decisions 1, 3 and 4.
- A shortcode with B2B approved and use_b2b ticked for the live test. Trident had no paybill as of GO_LIVE 22 Oct, so the Trident pilot is sandbox-only unless it gets one, and the first live KES 10 tests would be on Astral after Trident passes.
- The Working sub-account mapped to a Bank Account (or accept the shortcode's own), and the Charges Account set.
- Whether "Submit B2C Accounting Entries" is on. It also decides draft or submitted B2B entries.
- One supplier with an approved Supplier M-Pesa Account (proof, second approver, hold passed).
- A KES test PI and test PO.
- The names of the Payers and Accounts Users.

PHASE 2: M-Pesa Payment Schedule and the "M-Pesa Payments Due" report, for suppliers and no-party payees (B2B).

What ships:
- Manual lines, with dates movable before payment.
- The date rule enforced in every server path.
- Pay Selected, with a per-party and per-shortcode summary and balances. The Confirm button is disabled for 5 seconds in the browser, and the server also refuses earlier.
- Deferred, throttled sending with a send_started_on marker and the interrupted-send sweep.
- Plan Again for failed lines.
- Approximate available balances.

Needs from you for Phase 2:
- Decision 2 (maker-checker).
- Decision 5 (invoice cents), if you want the write-off option.
- Whether to set "Ask Safaricom for Balances Every (Minutes)", e.g. 30, so the balance cards are fresh.
- A 3-line test schedule with one line dated tomorrow.

PHASE 3: employees and payroll.

What ships:
- Employee M-Pesa Account (phone for B2C, bank paybill plus account number for B2B) with maker-checker approval.
- Employee B2B requests.
- B2C phone lines through B2C Payment Disbursement, with partyb locked to the approved phone.
- A Payroll Entry button that builds one schedule per payroll.
- A salary Journal Entry that clears the Payroll Entry's payroll payable, with round-off.
- A guard that stops cancelling a Payroll Entry with paid M-Pesa salaries.

Needs from you for Phase 3:
- Each company's Payroll Settings (employee-wise accounting on or off, rounding).
- Whether payrolls mix bank-paid and M-Pesa-paid staff (HRMS hides Make Bank Entry once any bank JE references the payroll).
- Which banks' paybills and account formats are used, and employees' proof documents.
- Who approves employee accounts.
- A 2-employee test payroll.
- (Done: HRMS v15 and v16 are on the local benches, so no CI is needed for the HRMS check.)

PHASE 4 (later, as needed):
- Initiated-B2B Transaction Status.
- An Allocate button.
- "Book to an account" mode.
- A hold account.
- Refund and reversal recording.
- Per-invoice account numbers.
- Withholding deductions.
- MMF to Utility top-up.
- Daily limits.

## Design

BASE AND GRAFTS
- Base: Design 1's Phase 1 shape.
  - One document still sends money: Mpesa B2B Payment Request.
  - Everything is form-based.
  - Every check runs again at submit and again at send.
  - Rows Paid before this release are never booked automatically.
- From Design 3: one shared "left to pay" helper; plain entry states; the earlier-payout acknowledgement; the Make Entry dialog; the after_migrate backfill; the phase order; the deferred-send marker; the 5-second confirm checked on the server; the Due report.
- From Design 2: the partial-allocation commitment formula; the payment-term rows; the salary Journal Entry rules; the v15/v16 remark and field rules.
- Branch: build on mpsa-suite, package mpsa_suite. Paths below are mpsa_suite/…; on astral-suite the same files sit under frappe_mpsa_payments/.
- No phase adds a patch. tests/unit/test_rename_safeguards.py::test_r8_registered_patches_are_the_eight_of_release_c pins release C's patches. New columns come from our doctype JSON. HRMS fields come from after_migrate custom fields. Defaults come from ensure_suite_settings/backfill_single_defaults.

FACTS THAT DECIDED DISAGREEMENTS (all verified in source)
F1. Frappe runs validate and before_submit on submit, and also on insert with docstatus 1.
- check_if_latest calls check_docstatus_transition(0), which sets _action="submit" (frappe v15 document.py:861-895, 1145-1150).
- The B2B controller runs validate_request only while docstatus==0 (mpesa_b2b_payment_request.py:14-16). That is the bypass.
- API payloads cannot set flags: "flags" is reserved in base_document.py:118.
F2. ERPNext refuses any on_hold Purchase Invoice in a Payment Entry, whatever its release_date (v15 PE:639-643). So the pre-send check uses on_hold, not invoice_is_blocked().
F3. A PO with a term-based template needs payment_term on each row on both versions (term_based_allocation_enabled_for_reference covers PO, v15 PE:323-335).
- v15 falls back to the None key for orders (v15 PE:371).
- v16 splits a PO only when allocate_payment_based_on_payment_terms is set, uses Payment Schedule.outstanding and skips terms of 0.1 or less (v16 PE:2528-2572).
- ERPNext's own term outstanding is payment_amount − paid_amount (v15 PE:3513).
F4. Moving a PO advance onto a PI reduces the PO row and adds a PI row carrying advance_voucher_type/no = the PO. Rows left at 0 are dropped (v15 utils.py:676-753).
F5. PE.set_missing_values never re-derives paid_from (v15 PE:428-478). get_payment_entry swaps in the Mode of Payment account (v15 JE:1497-1498). So we build the Payment Entry by hand.
F6. Payment Entry custom_remarks exists on both versions. Journal Entry: v16 hides user_remark and has custom_remark (v16 JSON). apply_tds (v16) and apply_tax_withholding_amount (v15) default to 0, so withholding applies only if set (v16 tax_withholding_entry.py:1098). We never set them.
F7. frappe v16 refuses SQL-function strings in get_all fields (v16 query.py:2234). New aggregates use frappe.db.sql.
F8. Already indexed: PE.reference_no (LOOKUP_INDEXES); JE.cheque_no (ERPNext JSON); B2B purchase_invoice and transaction_id (B2B JSON).
F9. Supplier.is_blocked_for exists on 15.123 and 16.51 but cannot be confirmed on Trident's 15.122. Use it when present, else read on_hold/hold_type/release_date ourselves.
F10. B2B sends carry no OriginatorConversationID (daraja/money_out.b2b_payload). An Unknown B2B with no ack cannot be asked about by Transaction Status, so only the statement settles it.
F11. B2C's guard takes named_lock "b2c-pay:<dt>:<dn>", held until commit (money_in/common.py:271-294; b2c_payment_disbursement.py:157), and refuses when any "others" exist.

=== RULES EVERY FLOW KEEPS ===
R1. Sent once.
- Direct path: lock_for_send as today; never raise after post().
- Schedule path (Phase 2): send_deferred with a send_started_on marker committed before the HTTP call. A marked request that is still Not Initiated becomes Unknown and is never resent.
R2. Ack and Result rules are unchanged: the ack sets Initiated only; only the Result sets Paid/Failed; a re-sent Result is ignored.
- A late Result on a cancelled Unknown request is recorded, because the money left.
- It is never booked automatically: staff get "Needs Checking" and an alert.
- It keeps counting against its document.
R3. Owed is checked under the per-document lock "b2c-pay:<dt>:<dn>" with locking reads.
- At draft without the lock, at submit and at send with it.
- It counts B2B, B2C, planned schedule lines, draft entries and order advances.
- B2C's guard counts B2B too.
R4. Lock order everywhere:
  1. the document named lock;
  2. named_lock("book:<receipt>");
  3. payout rows (B2B requests, B2C rows) FOR UPDATE;
  4. the document row FOR UPDATE, last.
- The booking job reads the request's document first, without a lock, then takes the locks in that order.
- left_to_pay reads payouts before the document row, as B2C does.
R5. One live entry per receipt.
- Before booking, Make Entry and the sweep's relink, look under named_lock("book:<receipt>") with locking reads for:
  - a live Payment Entry with reference_no = receipt and payment_type Pay;
  - a live Journal Entry with cheque_no = receipt.
- block_duplicate_receipt stays as it is.
R6. Every entry is posted:
- on the payout's own Bank Account GL: statement.payout_bank_account(statement.load(B2B, name)), so the line and the entry can never use different GLs;
- on posting_date = reference/cheque date = the M-Pesa completion date;
- with the receipt as reference_no / cheque_no;
- for the payout amount without the charge. The charge keeps its CHG-<receipt> Journal Entry.
R7. The destination always comes from an approved account. One-off destinations need allow_one_off_b2b and ADMIN_ROLES, now checked at submit and at send too.
R8. Only a SUBMITTED entry allocated to the document reduces what the document owes. A draft entry, a cancelled entry or an unallocated entry keeps the payout counted.
R9. The automatic booking job runs as Administrator through money_in.common.as_user, as B2C does. Make Entry runs as the session user with ERPNext's normal permissions.
R10. Every handler respects its existing Suite switch. Mpesa Settings.submit_b2c_accounting_entries still decides between a draft and a submitted B2B entry (R-d).

=== PHASE 1 (first production test): B2B from PI/PO, pay first, make the entry later, guard fixes ===

1. Mpesa B2B Payment Request JSON (bump "modified")
- purchase_order: Link Purchase Order, search_index, after purchase_invoice, depends_on eval:!doc.purchase_invoice. Description: "Optional. Paid as an advance on the order; the invoice made from it picks it up under Get Advances."
- entry_mode: Select "Book automatically\nMake the entry later", label "When Paid", default "Book automatically".
  - Description: "Book automatically: the Payment Entry is made when M-Pesa confirms (an advance to the supplier if no invoice or order is linked). Make the entry later: nothing is booked; press Make Entry when you know what it paid for."
  - Blank is treated as Book automatically.
- checked_earlier_payouts: Check, no_copy. Label: "I checked the earlier M-Pesa payments listed above: none of them paid this invoice". The JS shows it only when amount_left returns earlier payouts.
- New section "Accounting Entry":
  - payment_entry (moved here);
  - journal_entry: Link Journal Entry;
  - entry_status: Select "\nEntry Not Made\nDraft Entry\nNeeds Checking\nEntry Made", label "Accounting Entry", in_list_view, in_standard_filter;
  - entry_note: Small Text;
  - entry_attempted_on: Datetime, hidden;
  - entry_alerted_on: Datetime, hidden.
  - All of these are read_only, allow_on_submit and no_copy.
- supplier, purchase_invoice and purchase_order stay non-allow_on_submit. Only server code fills a blank one after payment, with db_set.

2. M-Pesa Suite Settings JSON (Money Out tab): entry_reminder_days (Int, default 3; 0 = never). After this many days, "Paid, entry not made" alerts notify_role once per request.

3. NEW mpsa_suite/money_out/owed.py: what a document still needs paying
- DOC_FIELDS = {"Purchase Invoice": "purchase_invoice", "Purchase Order": "purchase_order"}.
- doc_lock(dt, dn) = money_in.common.named_lock(f"b2c-pay:{dt}:{dn}"). Same key as B2C.
- document_problems(dt, values) returns plain messages:
  - PI: not submitted; a debit note (is_return); not KES (currency or party_account_currency); "PINV-… is on hold. Release the hold before paying" (on_hold, F2).
  - PO: not submitted; status On Hold / Closed / Completed; not KES; flt(per_billed, 2) > 0 gives "PO-… already has invoices: pay the Purchase Invoice instead".
- supplier_hold(supplier): is_blocked_for("Payments") when the method exists; else on_hold and hold_type in (All, Payments) and (no release_date or today <= release_date). Returns "ACME is on hold for payments until 30 Oct", or None.
- allocated_on(dt, dn, receipt): one SQL. It adds up:
  - per.allocated_amount over SUBMITTED Payment Entries with reference_no = receipt whose row is (dt, dn), or whose advance_voucher_type/no = (dt, dn) (behind has_column, as booked_docstatus does);
  - submitted Journal Entry rows with cheque_no = receipt and reference (dt, dn): debit − credit.
- b2b_open(dt, dn, exclude=None, for_update=False):
  - Raw SQL on the indexed link field, FOR UPDATE on MariaDB.
  - Rows with docstatus < 2 and status in (Not Initiated, Initiated, Unknown) count their full amount. Drafts count, as today.
  - Rows with docstatus in (1, 2) and status Paid count amount − allocated_on(...) when that is > 0.005.
  - Returns _dict(parent, allocated_amount, request_status), the shape B2C's guards already sum and name.
- b2c_open(dt, dn): b2c_payment_disbursement.open_payouts(dt, dn) + b2c_disbursement_request.paid_without_entry(dt, dn).
- draft_entries(dt, dn, skip_receipts): draft Payment Entry Reference rows allocated to the document, and draft Journal Entry rows referencing it (debit). An entry is skipped when its reference_no or cheque_no is a receipt already counted above. Each is named in the message.
- order_advances(pi): for each distinct purchase_order on the PI's items, add up:
  - submitted PE rows still referencing the PO;
  - submitted JE debit rows referencing the PO;
  - b2b_open(PO);
  - b2c_open(PO).
  This is the advance not yet moved onto an invoice (F4).
- planned_lines(dt, dn, exclude_line): Phase 2. Planned schedule lines with no request.
- left_to_pay(dt, dn, *, exclude_request=None, exclude_line=None, for_update=False) returns _dict(owed, parts, left).
  - owed = b2c_payment_disbursement.current_outstanding(dt, dn, for_update), read LAST (R4).
  - left = max(0, owed − Σparts).
  - Each part has a label and links, e.g. "MB2B-… Initiated 2,000", "draft Payment Entry ACC-PAY-0050 1,000", "advance still on PO-0007 5,000 (allocate it with Get Advances first)".
  - The PO-advance part is subtracted, not refused. This follows ERPNext practice, where get_advance_entries offers a PO's advances on its invoices.
- earlier_payouts(supplier): money the supplier may already have received for this bill. Lists:
  - Paid B2B requests of the supplier with no PI/PO whose entry is not made or is an unallocated advance;
  - submitted Payment Entries for the supplier with unallocated_amount > 0 whose reference_no is a B2B receipt.

4. mpsa_suite/money_out/b2b.py (changed)
- linked_document(doc) returns (dt, dn). Throws "Link an invoice or an order, not both."
- validate_request(doc, at_submit=False) does, in order:
  1. Supplier and company from the linked document (throw if a typed supplier differs).
  2. supplier_accounts.apply_destination.
  3. Today's field checks.
  4. _check_settings: apis.require(B2B); not our own shortcode; transaction_limit; Mpesa Settings.company (custom field, migrate.py:81) must equal doc.company, filled from it when blank.
  5. No supplier forces entry_mode = "Make the entry later".
  6. supplier_hold.
  7. document_problems.
  8. Amount must not exceed left_to_pay(exclude_request=doc.name). At submit this runs inside doc_lock with for_update=True. The message gives the breakdown.
  9. When a PI is linked and earlier_payouts exist: at draft an orange msgprint listing them; at submit throw unless checked_earlier_payouts is ticked.
- validate_against_invoice stays as an alias of the new document check, for the existing tests.
- check_ready_to_send(doc): non-mutating, used by send_b2b right after lock_for_send. It runs require(PAYOUT_ROLES), check_before_send, _check_settings on the stored values, supplier_hold, document_problems and the locked left_to_pay check. It only throws, and always before post(). This is defence in depth for server code that uses flags.ignore_validate.
- send_b2b: lock_for_send, then check_ready_to_send, then unchanged.
- handle_result: unchanged, except the Paid db_set also writes entry_status "Entry Not Made" and entry_note "Booking the entry…", and (review round 1) entry_attempted_on, so a booking job that is lost or fails before it commits is still booked by the sweep. The existing enqueue of book_payment_entry stays (same dotted name, so queued jobs and the sandbox lab keep working).
- book_payment_entry(name) = b2b_entries.book_automatically(name), then statement.after_payout.
- amount_left(doctype, name): whitelisted; checks read permission. Returns supplier, company, eligible Mpesa Settings (use_b2b=1, company, initiator set), the left_to_pay parts and left, document_problems, supplier_hold, earlier_payouts and the default approved account.

5. Controller (mpesa_b2b_payment_request.py)
- validate: unchanged.
- before_submit: require(PAYOUT_ROLES); validate_request(self, at_submit=True); status = "Not Initiated".
- on_submit: send_b2b (Phase 2: skip when flags.mpesa_send_later).
- before_cancel: unchanged. (Review round 1: an Initiated request may be cancelled by ADMIN_ROLES, as an Unknown one, for a Result that never came; Phase 1 has no B2B status query. A later Result is recorded as R2 says.)
- (Review round 1) before_update_after_submit: the server-written fields (status, ack and Result fields, transaction_id, payment_entry, journal_entry, entry_*) are read back from the database with a locking read, as B2C does for its rows. Frappe does not enforce read_only on the server.
- (Review round 1) on_trash: refused for a request that may have reached M-Pesa (Initiated, Paid, Unknown, or a conversation_id or transaction_id). A cancelled Paid request keeps counting (R2), and a late Result needs the row to match.

6. supplier_accounts.check_before_send
When supplier_mpesa_account is empty, throw "Pick the supplier's approved M-Pesa account" unless allow_one_off_b2b is on, and then require(ADMIN_ROLES). Today it returns early.

7. NEW mpsa_suite/money_out/b2b_entries.py: booking and Make Entry
- receipt_entries(receipt, for_update) returns live Payment Entries (reference_no, payment_type Pay) and Journal Entries (cheque_no) carrying the receipt. Each has docstatus, party, credit on the payout GL, unallocated_amount and its allocations.
- classify(doc, entries) returns (state, entry, note):
  - not Paid: blank.
  - No entry: Entry Not Made. On a cancelled request: Needs Checking, "M-Pesa paid this after the request was cancelled. Check the statement and that <doc> was not paid again, then make the entry (managers only)."
  - Two or more entries: Needs Checking, naming them.
  - Not on target: Needs Checking, "Receipt X is on ACC-PAY-… (on PINV-B / not allocated): allocate it to PINV-A or correct it." On target means:
    - a request with a document: a PE row on (dt, dn) or advance_voucher (dt, dn), or a JE row reference (dt, dn);
    - a request without a document: a PE whose party is the supplier, or any JE.
  - A draft on target: Draft Entry, "Submit ACC-PAY-…".
  - Submitted, but crediting another GL or another amount than the payout: Needs Checking, "ACC-PAY-… pays from <GL> for <amt>; the M-Pesa line is <amt> on <GL>, so it will not reconcile."
  - Submitted with unallocated money while the document still owes: Needs Checking, "KES r is not allocated: allocate it to PINV-… (Get Advances or Payment Reconciliation)."
  - Otherwise Entry Made. When the document owes 0 the note is "KES r is an unallocated supplier advance".
  - (Review round 1) For a request linked to a document, unallocated money while the document owes 0 is Needs Checking, not Entry Made: the document took the money some other way (a possible double payment). The note booking wrote, which names the reason, is kept while it names the same amount. Entry Made with an unallocated supplier advance is only for requests with no document.
- set_entry(doc, state, note, entry): db_set, logging to the Error Log only when the note changes.
- refresh_entry(doc):
  - If the linked entry is cancelled or missing: clear the link and add a comment "Payment Entry X was cancelled". (Review round 1: the request also switches to "Make the entry later", so the sweep does not book the same allocation again.)
  - Classify, link the found entry, set the state.
  - If the state changed: statement.after_payout(B2B, name).
- book_automatically(name):
  - Read the document and receipt without a lock; take doc_lock (if a document is linked), then named_lock(book:<receipt>), then the request row lock; reload.
  - Require Paid and a receipt. Set entry_attempted_on.
  - Any entry carrying the receipt: refresh_entry and stop.
  - docstatus 2: Needs Checking plus an alert (once).
  - entry_mode later: Entry Not Made, "Paid on <date>. Press Make Entry when you know what this paid for."
  - No supplier: Entry Not Made, "Paid to a number not saved on a supplier: press Make Entry."
  - supplier_hold: Entry Not Made, "<supplier> is on hold for payments in ERPNext. Release the hold; the entry is tried again every 15 minutes."
  - Otherwise, inside savepoint mpesa_b2b_pe and as_user("Administrator"): build_payment_entry; insert; submit when submit_b2c_accounting_entries is on; link it; classify.
  - On error: roll back to the savepoint. Entry Not Made, "Could not book automatically: <last line>. It is tried again every 15 minutes for 30 days, or press Make Entry. Do not make a Payment Entry for it by hand." Log once, alert once.
- build_payment_entry(doc, dt, dn, supplier, amount):
  - Header: frappe.new_doc; payment_type Pay; company; party_type Supplier; party.
  - Accounts:
    - paid_from = the payout GL (R6);
    - paid_to = PI.credit_to for a PI, else erpnext.accounts.party.get_party_account("Supplier", supplier, company). ERPNext's set_liability_account moves PO and unreferenced PEs to the advance account when the company books advances separately.
    - Refuse when either account is not KES.
  - Amounts: paid_amount = received_amount = amount; both exchange rates 1.
  - Links and reference:
    - bank_account = the payout Bank Account;
    - mode_of_payment = mode_of_payment_for(settings), a label only (F5);
    - reference_no = receipt; posting_date = reference_date = completion date.
  - cost_center = the document's cost_center, else Mpesa Settings.cost_center, else the company default. Copy each fieldname from get_accounting_dimensions() (no arguments, which works on both versions) from the document header.
  - custom_remarks = 1; remarks = "M-Pesa B2B <command> to <shortcode> (<request>), receipt <R>[, for <doc>]".
  - Do not set the withholding flags (F6).
  - references = reference_rows(dt, dn, amount).
- reference_rows(dt, dn, amount):
  - Read the document FOR UPDATE.
  - Return [] with a note when it can no longer take money:
    - PI: cancelled, on_hold, a return, or outstanding ≤ 0;
    - PO: closed, on hold, fully billed (per_billed ≥ 100), or owed ≤ 0.
    The PE is then an unallocated supplier advance, the state is Needs Checking and notify_role gets an alert (possible double payment).
  - (Review round 1) The same when the document can take only part of the payout: the problem "<dn> could take only KES a of this payment, so KES r is an unallocated supplier advance. Check that the supplier was not paid twice." gives Needs Checking, a comment and an alert, in book_automatically and in Make Entry.
  - cap = PI outstanding_amount, or PO (rounded_total or grand_total) − advance_paid.
  - alloc = flt(min(amount, cap), 2).
  - If the template has allocate_payment_based_on_payment_terms: one row per Payment Schedule term ordered by due_date then idx. Each takes min(flt(payment_amount − paid_amount, 2), flt(outstanding, 2), remaining), with payment_term set. Terms ≤ 0.1 are skipped (F3).
  - Otherwise one row.
  - What is not allocated stays on the same PE as unallocated (ERPNext keeps it as a supplier advance). PE.validate's set_missing_ref_details(force=True) reloads the reference details itself.
- build_journal_entry(doc, account, cost_center, remark):
  - voucher_type Bank Entry; company; posting_date = cheque_date = completion date; cheque_no = receipt.
  - Rows: Dr account (with cost_center); Cr the payout GL with bank_account set.
  - Remarks: user_remark = text, and each row's user_remark = text. When the meta has custom_remark (v16): custom_remark = 1 and remark = text.
- make_entry(name, kind, supplier=None, purchase_invoice=None, purchase_order=None, account=None, cost_center=None, submit=1, remark=None): whitelisted, POST.
  - Permissions:
    - require(FINANCE_ROLES) plus frappe.has_permission on Payment Entry or Journal Entry "create", with throw;
    - submit only if the user may submit, else leave a draft;
    - ADMIN_ROLES for a cancelled request.
  - Locks in R4 order. The request must be Paid with a receipt.
  - If an entry already carries the receipt: "Receipt X is already on <entry> (<where>): open it, or press Check Entry Again."
  - Kinds:
    - Purchase Invoice: submitted, KES, not on hold, supplier matches. Allocates min(amount, left_to_pay(exclude_request=name, for_update=True)).
    - Purchase Order (advance): submitted, per_billed 0, status allowed.
    - Supplier Advance: needs a supplier.
    - Journal Entry: account in the request's company, not a group, not Receivable/Payable (those use the Payment Entry kinds), not the payout GL; cost_center required for P&L accounts. Bank or Cash accounts are allowed, for a transfer to your own bank paybill.
  - The entry is inserted (and submitted) as the session user.
  - The request gets db_set of supplier, purchase_invoice or purchase_order where blank. A different existing value needs ADMIN_ROLES and a comment. It also gets the entry link and the state.
  - Then statement.after_payout(B2B, name), which feeds idempotently and reconciles the line now.
- refresh_entry_now(name): whitelisted, FINANCE_ROLES. The "Check Entry Again" button.
- refresh_entries(limit=100): cron */15.
  - One SQL selects Paid requests (docstatus 1 or 2) with a transaction_id where entry_status != 'Entry Made' OR the linked entry's docstatus != 1, oldest entry_attempted_on first.
  - Per row, in a savepoint, with a commit per row: refresh_entry.
  - Then book_automatically only when all hold: entry_mode is automatic; docstatus 1; entry_attempted_on is set (rows from before this release are never auto-booked); created within 30 days (RETRY_WINDOW_DAYS); no entry carries the receipt.
  - Alert once (entry_alerted_on) when a request is still not Entry Made after entry_reminder_days, using supplier_accounts._alert.
- backfill_entry_status(): called from migrate.after_migrate; idempotent. Paid rows with a blank entry_status are classified only, never booked.

8. statement.py
- OWN_FIELDS[B2B] += "purchase_order". load() already skips missing fields.
- description() uses purchase_invoice or purchase_order or supplier.
- feed() and sweep_missing() also accept docstatus 2 for B2B rows with status Paid, so money paid after a cancel still gets its line and charge. B2C is unchanged.
- charge_entry also sets custom_remark/remark on v16.

9. B2C sees B2B
- b2c_payment_disbursement.validate_no_double_payout: inside the existing named lock, for Purchase Invoice and Purchase Order, others += owed.b2b_open(dt, dn, for_update=True).
- b2c_disbursement_request.validate_still_owed: the same addition.
- Review round 1: for a Purchase Invoice both guards also take owed.order_advances(dn, for_update=True) off what it owes, read before the document row, as left_to_pay does.
- Review round 1: both guards refuse a Purchase Order with per_billed > 0 (owed.billed_order, the same message as document_problems). Paying the order's invoice does not change the order's advance_paid, so without this B2C could pay an order whose invoice B2B already paid.
- Review round 2: kept strict. A further B2C advance on a partly invoiced order is refused too (a change from release C, noted in SUITE_CHANGES); paying only its unbilled part is not built.

10. UI
- NEW public/js/purchase_invoice.js: M-Pesa > "Pay via M-Pesa (B2B)".
  - Shown when docstatus 1, outstanding > 0, KES, not on_hold, and frappe.model.can_create("Mpesa B2B Payment Request").
  - Calls frappe.new_doc with {purchase_invoice, remarks: "<name> <bill_no>"}.
- NEW public/js/purchase_order.js: M-Pesa > "Pay Advance via M-Pesa (B2B)" when docstatus 1, per_billed 0, status allowed, KES.
- A draft PI made from a PO that holds an advance shows the headline "PO-… has KES Y paid in advance: press Get Advances Paid to use it".
- Register both in hooks.doctype_js.
- mpesa_b2b_payment_request.js:
  - set_query for mpesa_settings (use_b2b 1, company) and purchase_order (docstatus 1, supplier, per_billed 0, status not in Closed/On Hold/Completed).
  - On PI/PO change, or on a new form that has one: call amount_left; fill supplier, company, the only shortcode and amount = floor(left).
  - Blue headline: "Left to pay X: outstanding A − in progress B − draft entries C − order advance D".
  - Orange list of earlier payouts, and show checked_earlier_payouts.
  - When Paid and entry_status != Entry Made: orange or red headline with entry_note, and the buttons "Make Entry" (dialog with read-only facts: amount, date, receipt, paid to, GL, charge JE; then Entry Type, Supplier, PI/PO filtered by supplier, Account, Cost Center, Remark, "Submit the entry now") and "Check Entry Again".
- NEW mpesa_b2b_payment_request_list.js indicators: "Paid, no entry" (orange), "Paid, draft entry" (orange), "Paid, check entry" (red), "Paid" (green).
- Workspace m_pesa.json and workspace_sidebar/m_pesa.json: shortcut "B2B Paid, Entry Not Made" with stats_filter status=Paid and entry_status in (Entry Not Made, Draft Entry, Needs Checking).
- hooks.override_doctype_dashboards: "Purchase Invoice" and "Purchase Order" point to NEW money_out/document_dashboards.py, which appends {"label": "M-Pesa", "items": ["Mpesa B2B Payment Request"]}. ERPNext's default fieldnames purchase_invoice and purchase_order match our link fields.

11. Permissions
- DocPerms unchanged.
- Submit and send: PAYOUT_ROLES, checked at submit and at send.
- Drafts: Accounts User.
- Make Entry and Check Entry Again: FINANCE_ROLES plus ERPNext create/submit permission.
- ADMIN_ROLES: cancelling Unknown requests, one-off destinations, entries for a cancelled Paid request, moving a payout to another document.
- Purchase roles: no B2B access.

=== PHASE 2: M-Pesa Payment Schedule and Due list (suppliers and no-party payees, B2B only) ===
- NEW "M-Pesa Payment Schedule": not submittable; MPS-.YY.-.MM.-.####; track_changes.
  - Fields: title, company, b2b_settings (and b2c_settings in P3), purpose (General/Payroll), notes, lines, read-only totals (planned / in progress / paid), computed status (Open / Partly Paid / Paid / Closed), excluded (P3).
  - Permissions:
    - create/write: Accounts User, Accounts Manager, M-Pesa Manager, System Manager;
    - read: M-Pesa Payer, M-Pesa Viewer.
  - Close sets the remaining Planned lines to Cancelled. on_trash is refused once any line has left Planned.
- NEW child "M-Pesa Payment Schedule Line":
  - planned_date (reqd, "Pay On");
  - party_type (Supplier, and Employee in P3), party, payee_name;
  - payee_account_doctype (hidden) and payee_account (Dynamic Link to the approved account);
  - route (B2B Paybill / B2B Till; B2C Phone in P3), read-only, copied from the account;
  - destination (read-only);
  - one-off fields (no party only: allow_one_off_b2b and ADMIN);
  - amount (whole KES);
  - reference_doctype (Purchase Invoice / Purchase Order; Salary Slip in P3) and reference_name;
  - entry_mode; remarks;
  - system fields, read-only and always restored from the database on save: status (Planned / Sending / Sent / Paid / Failed / Unknown / Cancelled), request, receipt, paid_on, entry_status, last_problem, prepared_by, approved_by, approved_on, batch, attempts.
  - "Overdue" is computed, never stored.
- B2B request gains payment_schedule (Link, ro, no_copy, search_index), schedule_line (Data, hidden, ro, no_copy, search_index) and send_started_on (Datetime, hidden, ro, aos, no_copy). schedule_line is accepted only when flags.mpesa_from_schedule is set.
- NEW money_out/schedule.py:
  - validate_schedule:
    - rows past Planned cannot be changed or deleted ("Row 4 was sent to M-Pesa on 23 Oct; it cannot be changed");
    - a planned_date change must be today or later;
    - destinations are copied from the account;
    - over-planning against a document is checked with left_to_pay(exclude_line=row);
    - soft problems go to last_problem.
  - move_date(lines, date): Accounts roles; Planned only; today or later; adds a comment with the old and new dates.
  - plan_again(line): only for Failed lines. An Unknown line needs an admin to settle and cancel its request first.
  - line_problem(line, for_update, payer, snapshot) returns:
    - "Not due until <date>";
    - not Planned; account unusable or changed; limits; left_to_pay;
    - supplier or document problems; the API switched off;
    - maker-checker (see the user decision);
    - changed since review;
    - soft warning for no-document lines: "ACME was paid KES X by M-Pesa on <date> outside this schedule".
  - assert_line_due(request): FOR UPDATE on the line; planned_date ≤ getdate(nowdate()) in the site time zone. Called in B2B before_submit, send_b2b and send_deferred (and the B2C send path in P3). Every route (button, bulk, API, console) is therefore blocked before the planned day.
  - prepare_payment(lines) (PAYOUT_ROLES) returns:
    - a summary per party (party, number of lines, amount);
    - totals per shortcode and route, and the grand total;
    - balances with their age, and shortfall warnings;
    - "not included" lines with reasons;
    - a token cached for 10 minutes, bound to the user: {issued_at, (line, amount, planned_date, destination, modified)}.
  - confirm_payment(token) (PAYOUT_ROLES, POST):
    - the token is single-use under named_lock("pay-token:<t>"), at least 5 seconds old (server side; the Confirm button is also disabled 5 seconds in JS), same user;
    - per line, in a savepoint: parent then line FOR UPDATE; line_problem(for_update, snapshot);
    - insert the B2B request with flags.mpesa_from_schedule and mpesa_send_later, then submit (before_submit re-validates under doc_lock);
    - the line goes to Sending with request, approved_by, approved_on and batch;
    - bump the parent's modified;
    - then enqueue_after_commit send_batch on the long queue.
  - send_batch: b2b.send_deferred per request, with send_interval_seconds (default 2) between them, committing per request.
  - send_deferred(name):
    1. Row lock. Require docstatus 1, Not Initiated and no send_started_on. Set send_started_on; commit.
    2. check_ready_to_send and assert_line_due. If a check fails: status Failed, "Not sent: <reason>", commit.
    3. post, apply_ack, commit, sync_line.
  - Sweep in money_in.events.every_five_minutes:
    - Not Initiated with send_started_on older than 10 minutes becomes Unknown: "Sending was interrupted after it may have reached M-Pesa. Check the statement before paying again."
    - Schedule requests with no marker: re-enqueued while younger than 1 hour; older ones are cancelled and the line goes back to Planned.
  - sync_line(request) is called from apply_ack, handle_result and refresh_entry, and by the sweep. It mirrors status, receipt and entry_status and never raises.
- NEW Script Report "M-Pesa Payments Due" (frappe_mpsa_payments-style report folder, checkboxColumn):
  - Filters: Company; Show (Due today and overdue / Next 7 days / In progress / All); Route; Pay from.
  - Columns: Pay On with an overdue badge, Schedule, Payee, Route, Destination, Document and its left to pay, Amount, Status, Problem, Prepared by.
  - report_summary cards per shortcode: Working (B2B) and, in P3, Utility (B2C) balance with its age, and the selected total.
  - Buttons: Pay Selected (prepare, confirm dialog, confirm), Move Date, Refresh Balances (FINANCE_ROLES; existing get_account_balance).
- wallet/balances.available(settings, sub_account) returns {amount, as_of, pending, after}:
  - the newest of the Mpesa Settings.account_balance AVAILABLE column (index 3), the latest B2B debit_account_balance (available column) and the latest B2C *_avlbl_funds;
  - minus payouts in flight created after that time;
  - labelled approximate.
- Planned lines count in owed.left_to_pay, so a document on a schedule cannot also be paid directly beyond what is left.
- Built (2026-10-23), where it differs from the above:
  - Maker-checker reads two fields: prepared_by (who added the line) and changed_by (who last changed it, moved its date or planned it again). The payer must be neither. Administrator is exempt, as in Supplier M-Pesa Account approval.
  - Over-planning on save counts this schedule's lines from the form and every other schedule's planned lines (left_to_pay(exclude_schedule)), so two new rows for one invoice are caught together. The check at pay time uses exclude_line.
  - confirm_payment takes the document's lock first, then the schedule row, then the line (R4), lines in schedule and row order. The line is marked Sending with its request before the request is submitted, so assert_line_due also checks that the request is the line's own.
  - send_b2b refuses a schedule request after the date check: only send_deferred sends one, because only it commits the marker first. on_submit never sends a schedule request inline.
  - sync_line runs from a queued job when apply_ack, handle_result or refresh_entry calls it (sync_line_later), so a Result never waits on or deadlocks with schedule rows. The sweep and send_deferred's failure path call it inline. It re-raises a deadlock or lock wait instead of swallowing it.
  - send_deferred undoes a failed check with a savepoint (a full rollback only after a deadlock). The sweep re-sends an unstarted request only once it is 5 minutes old, so it does not race the queue.
  - A schedule whose lines were only planned or cancelled may be deleted. Any line sent, or any request naming the schedule, refuses the delete.
  - Pay Selected also refuses a line when the supplier got M-Pesa payments with no invoice after the summary. The summary lists the earlier ones, and confirming ticks checked_earlier_payouts.
  - The report also filters by schedule (Pay Due Lines) and has Plan Again for failed lines.
  - Not built: the write-off of balances under KES 1 (decision 5, optional). B2C's own guards do not count planned lines; a line for a document B2C paid meanwhile is refused at Pay Selected by left_to_pay.
  - Review round 1: a line saved or deleted on its own (REST PUT or DELETE on the row, frappe.client.save) is refused by the line's own validate and on_trash, which Frappe calls only then; saving or deleting the schedule never calls them. validate_schedule refuses a row carrying another schedule's line name (Frappe updates rows by name only, so it would move that line and reset it).
  - Review round 1: send_deferred claims with a locking read, then takes the document's lock before the request and line rows (R4), and marks a failed check Failed only while the request is still Not Initiated with its own marker. lock_for_send reloads with a locking read. send_deferred returns whether it posted; send_batch waits the interval only after a posted request.
  - Review round 1: confirm queues send_batch under job id mpesa_schedule_send_<batch>; the sweep re-queues unstarted requests under their batch's job id with deduplicate, so not while that job is queued or running.
  - Review round 1: a marked request still Not Initiated may have reached M-Pesa. before_cancel decides on the database's server fields (locking read) and lets only ADMIN_ROLES cancel such a request, keeping it Unknown with the INTERRUPTED message; on_trash refuses it; a line whose request was cancelled mid-send another way is planned again by a manager only.
  - Review round 1: the confirm summary has one row per payee and destination, with the paybill or till, the route and the supplier's ID (or "not saved on a supplier"). The Payments Due report clears its ticks on every redraw (frappe-datatable keeps them by row number across refresh).

=== PHASE 3: Employees and payroll ===
- NEW submittable "Employee M-Pesa Account" (MEA-.#####), mirroring Supplier M-Pesa Account (maker-checker approval with proof, hold hours, is_default, name_mismatch).
  - account_type: Phone (B2C) or Bank Paybill (B2B).
  - Phone: expected_name is the name registered on M-Pesa, checked against the B2C ReceiverPartyPublicName.
  - Bank paybill: shortcode, account_number, bank. expected_name is the BANK's public name. It confirms the destination only; who holds the account rests on the proof, the second approver and the hold.
  - The generic logic moves to money_out/party_accounts.py with a profile per account doctype. supplier_accounts keeps thin wrappers.
  - Supplier M-Pesa Account optionally gains "Phone (B2C)" for sole traders (B2B refuses it).
- B2B request gains employee and employee_mpesa_account (exclusive with supplier). salary_slip and payroll_entry are added as hrms-only custom fields in after_migrate. JE Account gets the custom field mpesa_b2b_payment_request.
- B2C lines are grouped into one B2C Payment Disbursement per (schedule, b2c_settings, party_type, reference doctype, payroll_entry):
  - rows carry mpesa_schedule_line and partyb from the approved account; the row validate locks partyb to it;
  - inserted and submitted with ignore_permissions after the PAYOUT check;
  - B2C's own before_submit guard and enqueue send them;
  - services/b2c_request_service.create_b2c_request runs assert_line_due.
- Payroll Entry button "Create M-Pesa Payment Schedule" (public/js/payroll_entry.js, hrms only) calls schedule.from_payroll_entry:
  - at most one live schedule per payroll; refused while a non-suite Bank/Cash JE (draft or submitted) exists for it, and every payment of one of its salaries checks that again (Pay Selected, a B2B salary's submit and send, B2C's guards, the job that makes a B2C payout and its send);
  - one line per submitted, non-Withheld Salary Slip of an Active employee of the same company;
  - amount = rounded_total − salary_slip_paid, in whole shillings;
  - route from the default approved Employee M-Pesa Account;
  - employees with no account are listed in excluded with the reason.
- Salary Journal Entry, for both routes, used only for schedule-made payouts so existing B2C bookings stay as they are:
  - Bank Entry; cheque_no = receipt; posting_date = cheque_date = completion date;
  - Dr Payroll Entry.payroll_payable_account for net_pay, reference Payroll Entry, cost_center = Payroll Entry cost center;
  - party Employee only when Payroll Settings.process_payroll_accounting_entry_based_on_employee is on;
  - the rounding difference (at most 0.50) to Company.round_off_account / round_off_cost_center;
  - Cr the payout GL (Working for B2B, Utility for B2C);
  - tagged so salary_slip_paid does not count it twice. salary_slip_paid also counts Paid B2B salary requests.
- Payroll Entry before_cancel (doc_event) is refused while M-Pesa salaries for it are in flight or Paid.
- Built (2026-10-23), where it differs from the above:
  - Amounts are whole shillings rounded down (never more than the slip has left), as B2C's own guards need. With rounded totals (HRMS's default) the round-off row is at most 0.50; with "Disable Rounded Total" on it is under 1.00. The salary entry debits the payroll payable with what the salary still has on it (net pay less what was paid before: Paid payouts and bank entries) when the payout pays all that is left to the shilling; a part payment debits what was paid.
  - The B2C route needs a Salary Slip on the line (a B2C Payment Disbursement pays documents). A bank paybill line with no slip is a B2B request that waits for Make Entry (Make Entry gains "Salary Slip" for employee requests).
  - A phone line keeps its B2C payout in new line fields (b2c_disbursement, b2c_row, b2c_request); request stays the B2B request. The schedule's Payroll Entry is a Dynamic Link (payroll_doctype, payroll_entry) in the DocType JSON, so it needs no HRMS custom field; the B2C disbursement gains payment_schedule and its row mpesa_schedule_line, both JSON.
  - New Suite setting "Hold New Employee M-Pesa Accounts (Hours)" (default 24). Employee accounts are approved by the same roles as supplier accounts (ADMIN_ROLES); HR Manager and HR User may enter them.
  - The Payroll Entry button needs read on the Payroll Entry (HRMS gives it to HR Manager) and a finance role that may create a schedule; that user is the lines' preparer.
  - The job that makes each B2C payout (create_b2c_request) checks the line again (disbursement still submitted, due, its own, no other payout for the row, the slip still payable, the approved phone still usable) and fails the row, nothing sent, when it may not be paid. It then commits the B2C Disbursement Request, submitted but not sent, with the row's link, and only then sends it (schedule.send_b2c): as B2B's send_deferred (R1), the send commits a send_started_on marker on the request before the call and checks everything again (assert_b2c_line_due). The 5-minute sweep makes a marked request still Not Initiated after 10 minutes Status Unknown (a Transaction Status check settles it; the line is Unknown, never Failed). A line still Sending an hour after Pay Selected is failed only when nothing can have been sent: its row has no payout, or its payout never started (no marker). A retry of a scheduled B2C payout is refused: Plan Again and Pay Selected.
  - A scheduled B2C payout that may have reached M-Pesa (Initiated, Status Unknown, or its send began) is cancelled only by an M-Pesa or Accounts Manager (its before_cancel, as B2B's) and keeps its status; a Paid one is never cancelled or deleted. Its line is then Unknown and only a manager plans it again. A cancelled request that never reached M-Pesa fails its line as before.
  - Review round 1: account approval refuses whoever created the account, entered or last changed its details (entered_by, checked again at submit) and the employee; one phone or bank account is one employee's; nobody pays themselves (line, B2B request, approval); a payment to an employee with no Salary Slip is submitted and paid by a manager; salary lines, payroll schedules and employee B2B requests are shown only to who may read Salary Slips, pay them or manage (money_out.salary_access), and M-Pesa Viewer and Payer lose read on Employee M-Pesa Account; B2C's guards also take off planned schedule lines (R3); a Salary Slip with M-Pesa payments Paid or on their way cannot be cancelled.
  - The name check of a phone runs when its row first shows Paid; a mismatch blocks the account as for suppliers.
  - The Payroll Entry cancel is also refused while a payment schedule plans its slips (HRMS could not delete them); the message says to delete that schedule first.
  - Not built: "Phone (B2C)" on Supplier M-Pesa Account (optional); Sandbox Lab scenarios for employees.

=== PHASE 4 (as needed) ===
- Transaction Status for Initiated B2B (by the ack's IDs).
- An "Allocate to Invoice" button (reconcile_against_document).
- A "Book to an account" entry mode.
- A hold or clearing account option.
- Recording refunds and reversals.
- A per-payment account number for suppliers whose paybill needs the invoice number.
- Withholding-tax deductions (if needed).
- MMF to Utility top-up.
- Daily limits.

## Test plan

UNIT TESTS (fake_frappe, tests/unit/)

test_b2b_owed.py
- left_to_pay arithmetic for a PI and a PO with each kind of pending money:
  - draft B2B request, Initiated, Unknown;
  - Paid with no entry;
  - Paid with a draft entry;
  - Paid with a cancelled entry;
  - Paid with an unallocated entry;
  - Paid on a cancelled request;
  - Paid with its PO advance moved to the PI (advance_voucher_no);
  - B2C open_payouts plus paid_without_entry;
  - a draft manual Payment Entry, and one carrying a counted receipt (skipped);
  - order advance on the PI's PO;
  - planned lines (P2).
- document_problems wording, including a PI on hold with a past release_date, which must still be refused.
- supplier_hold: is_blocked_for present and absent.
- Lock key equals B2C's ("b2c-pay:<dt>:<dn>").
- Reads happen in R4 order: payouts before the document row.

test_b2b_entries.py
- reference_rows:
  - plain;
  - capped, with the remainder left unallocated;
  - term-based in due order, using min(payment_amount − paid_amount, outstanding), skipping terms ≤ 0.1, with flt 2;
  - a document that can no longer take money gives no rows plus an alert.
- build_payment_entry:
  - paid_from stays the payout GL when the PI has a mode_of_payment;
  - posting and reference date = completion date;
  - custom_remarks set;
  - dimensions copied;
  - withholding flags untouched.
- classify: every state (none, several, off-target, draft, other GL or amount, unallocated while owed, made, cancelled request).
- book_automatically branches:
  - make the entry later; no supplier; supplier on hold; docstatus 2;
  - an entry already carries the receipt;
  - an error is recorded once;
  - a re-run books nothing.
- make_entry:
  - each kind;
  - refused when the receipt is on another entry;
  - role and permission refusals;
  - admin needed to move to another document;
  - JE fields: cheque_no, cheque_date, custom_remark only when the meta has it.
- refresh_entries:
  - legacy rows (no entry_attempted_on) are never booked;
  - the 30-day window;
  - one reminder only.
- backfill is idempotent.

test_money_out_flow.py (update and add)
- before_submit re-validates; insert with docstatus 1 runs it.
- check_ready_to_send refuses, with no HTTP call, when:
  - no account and one-off is off;
  - the destination was changed;
  - the limit is exceeded;
  - the company differs;
  - the PI went on hold after save.
- handle_result writes entry_status.
- A Result on a cancelled request: recorded, never booked.
- b2b_row fixtures gain supplier_mpesa_account.

test_payout_statement.py
- Lines are made for docstatus-2 Paid B2B requests.
- purchase_order appears in the description.
- Mock b2b_entries.book_automatically.

test_supplier_accounts.py
- check_before_send with no account.

P2: test_schedule.py
- Date rule (yesterday, today, tomorrow) with the site time zone mocked, at confirm_payment, B2B before_submit, send_b2b and send_deferred.
- Sent rows cannot change; system fields are restored.
- move_date rules; maker-checker.
- Token: single-use, refused under 5 seconds, user-bound, refused after a line changes.
- Summary grouping.
- Interrupted-send sweep: with the marker the request goes Unknown; without the marker it is re-enqueued or re-planned.

P3: test_employee_accounts.py, test_salary_entry.py
- Party only with employee-wise accounting on; the round-off row; payroll payable taken from the Payroll Entry; remark fields per version.

BENCH TESTS (CI matrix v15 and v16; hrms is installed on both, ci.yml:86,97)

money_out/test_b2b_documents.py
- PI Paid from a replayed Result:
  - plain PI;
  - PI with mode_of_payment set to a Cash MoP (paid_from must be the M-Pesa GL);
  - term-based PI, part payment;
  - PI paid by bank between send and Result (unallocated PE, Needs Checking, alert);
  - PI put on hold after send.
- Supplier on hold: submit refused before any HTTP call.
- Booking as a restricted Integration User (Accounts User and M-Pesa Manager only) succeeds, because the job runs as Administrator.
- PO:
  - an advance PE raises PO.advance_paid;
  - a PI made from the PO, Get Advances, PI submitted: still Entry Made, PO left_to_pay 0;
  - the PI's left_to_pay correct, and the order advance subtracted before allocation;
  - term-based PO;
  - a company with book_advance_payments_in_separate_party_account.
- Submit switch off: draft PE keeps the PI blocked. Cancel the PE: the commitment returns and the line is unreconciled. Make Entry; reconcile_run reconciles.
- Pay first:
  - Make the entry later: payout line, charge line and charge JE made; the line stays unreconciled;
  - make_entry for each kind (PI, PO, supplier advance, JE to an expense account) reconciles the line, with the receipt on exactly one entry;
  - a second make_entry is refused.
- An earlier-payout warning blocks submit until ticked.
- Cross-guard: an open B2C payout on the PI blocks a B2B submit, and an Initiated B2B blocks a B2C disbursement submit.
- frappe.client-style insert with docstatus 1 and a typed paybill is refused.
- A JE made by the Bank Reconciliation Tool carrying the receipt is linked by the sweep.

Existing bench tests
- money_out/test_statement.py and wallet/test_wallet.py stay green.

P2 bench
- A future line is refused through confirm_payment, a direct submit carrying schedule_line and a console send.
- A 3-line pay with mocked Daraja.
- A stale schedule form save is refused.

P3 bench
- Payroll schedule with employee-wise accounting on and off: payroll payable clears exactly.
- Payroll Entry cancel is blocked.
- Withheld and Left employees are excluded.

SANDBOX LAB AND LIVE
- New scenarios: b2b_pay_order and b2b_pay_first_then_entry, beside the existing start/check_b2b_pay.
- Live (staff, KES 10 each, on a shortcode with B2B approved):
  - (1) pay a test PI;
  - (2) pay a test PO in advance, then make the PI and Get Advances;
  - (3) pay first with "Make the entry later", then Make Entry against a PI.
  - For each, check the PE or JE (GL, date, receipt), the reconciled payout line and the CHG- line.

## Risks

- Stricter checks at submit and send will refuse things that pass today. Examples: a Payer submitting a one-off paybill an admin saved; an amount above what is left once B2C payouts, draft entries and order advances are counted; a supplier or invoice on hold; a Mpesa Settings record of another company. This is intended, but the Sandbox Lab and the unit fixtures (b2b_row without an account) must be updated, and staff need to be told.
- On a direct submit, the per-document lock and the request row lock are held through the Daraja HTTP call, because the inline send is unchanged. A second payer on the same document waits up to 10 seconds and then gets 'Could not acquire lock'. Nothing is sent twice, but the message must say another payment for that document is being sent.
  - Built (review round 1): the message is "Another payment for <doctype> <name> is being sent or booked. Try again in a minute." (owed.doc_lock, and B2C's guards on the same key).
  - Review round 1, checked on v15 and v16: the locks are not in fact held through the call. post() first logs the request with Frappe's create_request_log, which commits. That commit ends the submit's transaction before requests.post: its row locks go, and the per-document lock is released with it (named_lock releases after commit). The request is then committed as submitted and Not Initiated, so it already counts as open for every other payer. A second payer waits only while the checks before the send run.
  - Should a later Frappe stop committing there, the locks would be held through the call. A Result that meets a lock held past MariaDB's lock wait, or a deadlock, is recorded again from a job (b2b.handle_result, up to 5 tries), and its payload goes to the Error Log if it never is. Its booking job may time out on the document lock; the Result's entry_attempted_on lets the sweep book it within 15 minutes.
- Existing gap on the direct path, not fixed in Phase 1: if the worker process dies after post() and before commit, the submit rolls back and the record of the send is lost. Only the Phase 2 schedule path gets the committed send_started_on marker.
  - Review round 1: the submit itself is committed by create_request_log before the call (above), so what is lost is the ack. The request stays Not Initiated (it counts as open, but anyone may cancel it), and its Result is not matched (only an Unknown request is matched by ref); it is kept as "Mpesa B2B Result (unmatched)". Phase 2's marker turns such a request into Unknown.
- The hand-built Payment Entry skips get_payment_entry extras: early-payment discounts and Payment Request allocation. It copies only header accounting dimensions. Sites relying on these need a check on v15 and v16.
- Term-based invoices and orders depend on ERPNext keeping Payment Schedule paid_amount and outstanding current. Hand-edited schedules can leave part of the payment unallocated. That is safe, but the invoice stays partly unpaid until someone allocates the rest.
- When the document can no longer take the money at booking time (paid elsewhere, put on hold, closed), or can take only part of it (review round 1), what it cannot take is booked as an unallocated supplier advance and marked Needs Checking with an alert. The books stay right, but a real double payment to the supplier is only caught if staff act on the alert.
- Paid requests from before this release are only classified, never booked automatically. Staff must clear any old 'Paid, no Payment Entry' cases with Make Entry.
- Entries post on the M-Pesa completion date. A closed Accounting Period or a frozen-accounts date (v15 Accounts Settings acc_frozen_upto, v16 Company accounts_frozen_till_date) makes booking fail. The request then shows Entry Not Made with ERPNext's message, and an admin must open the period. There is deliberately no date override.
- Entries staff make outside Make Entry reconcile only when they carry the receipt, use the payout's GL and exclude the KES 2 charge. The sweep flags a mismatch as Needs Checking but cannot fix it. A Payment Entry made by hand without the receipt, after a failed automatic booking, can still be followed by a successful retry. That retry is caught as an unallocated advance with an alert, not silently.
- Supplier.is_blocked_for may not exist on Trident's ERPNext 15.122, so the code needs the fallback. On the release date itself, the fallback (blocked while today <= release_date) refuses one day longer than ERPNext 15.123 does.
- An Unknown B2B (no ack) cannot be settled by Transaction Status, because B2B sends carry no OriginatorConversationID. Staff must check the statement. Phase 2's interrupted-send rule creates such Unknowns by design: they are safe, but manual.
- A Paid payout without a submitted entry on its document blocks that document indefinitely, by design. An old payout booked by hand without the receipt keeps blocking until its entry carries the receipt.
- The Phase 2 balance is approximate: Account Balance answers asynchronously, Utility is funded by manual portal transfers, and charges come off Utility on Astral. Paying more than the real balance makes Safaricom fail the extra lines. No money is lost, but the batch partly fails.
- HRMS's Payroll Entry cancel cancels every JE referencing it. Make Bank Entry disappears once any M-Pesa salary JE exists, which affects mixed payrolls. Whole-shilling pay against an unrounded payable needs the round-off row.
- For an employee bank-paybill B2B, the result names the bank, not the employee. Wrong-account risk rests on the proof document, the second approver and the hold period.
- Release and rename: new whitelisted paths are under mpsa_suite. If Phase 1 is cherry-picked to a frappe_mpsa_payments release for Trident before its rename, the JS xcall paths differ per branch. No phase may add a patch: test_r8 pins release C's eight.

## Decisions for the user

- **When a B2B payment has no Purchase Invoice or Purchase Order but has a supplier (pay first), what should happen when M-Pesa confirms it?**
  - Book it automatically as a supplier advance (today's behaviour); staff tick 'Make the entry later' when they don't yet know what it paid for
  - Default to 'Make the entry later' for every payment without an invoice or order; nothing is booked until someone presses Make Entry
  - Recommendation: Book it automatically as a supplier advance. The money that left is then always in the books on the right supplier. ERPNext's Get Advances offers it on the invoice when it arrives. Existing users see no change. 'Make the entry later' is always one click away, and it is forced when there is no supplier.
- **Payment schedules: must the person who presses Pay on a line be someone other than the person who prepared or last changed it?**
  - Yes, on by default, with a Suite setting to turn it off for one-person teams
  - No, any M-Pesa Payer or Manager may pay any due line
  - Recommendation: Yes, on by default with the setting. It is the only maker-checker on bulk payouts, and the confirm summary already records who approved and paid. Turn it off at Trident only if one person does both jobs.
- **Where should Phase 1 ship?**
  - mpsa-suite (package mpsa_suite), in the first release after the rename hand-over; Trident pilots it after its rename
  - Also cherry-pick it to a frappe_mpsa_payments release after C on astral-suite, so Trident can pilot it before the rename (allowed because it adds no patch)
  - Recommendation: mpsa-suite, as the release plan says for post-release-C work. Cherry-pick only if the B2B pilot cannot wait for Trident's rename, and then keep the two copies identical apart from paths.
- **Do you deduct withholding tax (e.g. withholding VAT or WHT) when you pay suppliers?**
  - No: Phase 1 pays and books the full amount entered (withholding flags left off)
  - Yes: Phase 2 adds a withholding deduction row so the invoice is cleared gross while M-Pesa pays the net amount
  - Recommendation: No for the Phase 1 pilot. Tell us if any supplier is paid net of withholding, and the deduction row moves into Phase 2.
- **M-Pesa pays whole shillings, so invoices with cents keep a balance under KES 1 (e.g. 0.40). What should happen to it?**
  - Leave it outstanding on the invoice (ERPNext standard; Phase 1 behaviour)
  - Phase 2 setting: write off a balance under KES 1 to the company Round Off account automatically when M-Pesa pays the rest
  - Recommendation: Leave it in Phase 1, and add the write-off as an opt-in setting in Phase 2, default off. Turn it on if partly paid invoices with a few cents left bother your team.
