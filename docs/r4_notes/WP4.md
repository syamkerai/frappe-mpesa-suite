# WP4: pay merchants and casual payees straight from an Expense Claim

"Petty cash without cash": a claim can pay the till, paybill or bank account of a merchant, or
the phones of casual workers, directly by M-Pesa, instead of the employee paying from their own
phone and being reimbursed. The books stay a reimbursement of the claim. Plan C.5, C.6 and C.7
(casual batches). Generic: which expense types may be paid this way is data (Expense Claim
Type), and the limits are settings.

## What changed
- **New child DocType `M-Pesa Claim Payee`** (module Mpsa Suite), the claim's **M-Pesa Payees**
  table. Each line: Pay To (`Till (Buy Goods)`, `Paybill`, `Bank Account (via Bank Paybill)`,
  `Phone (B2C)`), the payee's name as the claimant gives it, the till / paybill number, the
  account number, the bank (Link to `M-Pesa Bank Paybill`; its paybill is copied, never typed),
  the phone, the amount, and the expense line it pays (picked from the claim's saved lines; the
  claim's only line when it has one). Read only, written by the server: Pays To, Expense Claim
  Type, status (Draft, Queued, In Flight, Paid, Failed, Cancelled), the payout, the receipt, paid
  on, sent on, why it failed, the name M-Pesa paid, whether it matches, the name check note, the
  approved supplier account it turned out to be, and a hidden destination key (indexed). The
  status and result fields are allow-on-submit and read only; everything else is frozen with the
  claim. A line saved or deleted on its own (REST) is refused: it changes with its claim.
- **Custom fields** (`migrate.create_claim_payee_fields`, every migrate, HRMS sites):
  - Expense Claim: a section **Pay Merchants and Payees by M-Pesa** (after the expense lines,
    hidden until the form finds the Suite switch on, or the claim has lines), an HTML summary and
    the table `mpesa_payees`.
  - Expense Claim Type: `allow_mpesa_payees`, **Paid to Merchants or Casual Payees by M-Pesa**.
- **`claim_payee`** (Data, read only, indexed), the last field of **Mpesa B2B Payment Request**
  and of **B2C Payment Disbursement Reference**: the payee line a payout pays.
- **`money_out/claim_payees.py`**:
  - Checks on every save of the claim and at its submit (the approval), `doc_events` validate:
    - The Suite switch must be on; an Is Paid claim and a non-KES claim pay no payee (the claim's
      currency on v16, its company's default currency on v15, whose claim has none).
    - A draft and its approval start every line afresh: Draft, no payout, receipt or result (an
      amend copies them from the cancelled claim; the API could set them).
    - Every line names the expense line it pays, and that line's Expense Claim Type must allow
      payees (the claim's other lines may be of any type). A draft with several expense lines is
      warned; the approval refuses a line without one. An amended claim's line takes the same
      expense line of the new claim (row number and type); one that is gone is warned on the
      draft and picked again before the approval (a one-line claim's line is taken).
    - The lines of an expense line add up to no more than its sanctioned amount (a draft's
      claimed amount); all lines to no more than the claim owes (grand total less reimbursed).
      Whatever they leave is reimbursed to the employee as usual.
    - Per type: a phone kept as 2547... (a non-Safaricom prefix is warned, not refused); a till
      or paybill 5 to 7 digits, a paybill with its account number; a bank account at an enabled
      bank paybill, letters and digits, fitting the bank's account number pattern; a plain Paybill
      that is an enabled bank paybill is refused ("pick Pay To Bank Account and <bank>"); never
      one of the company's own shortcodes; whole shillings (a phone KES 10 to 250,000).
    - Never an employee's destination: the claimant's own or any employee's phone (Employee cell
      number, Employee M-Pesa Accounts, approved or not), M-Pesa account (till, paybill, bank) or
      bank account number (Employee's Bank A/C No. or an account's), nor a paybill or bank line
      whose account number is an employee's phone (many bank paybills credit the account linked
      to that phone). The other employee is not named.
    - Caps (when above 0): Most per Payment refused per line; Most per Day: a claim whose phone
      payees (paid in one batch) are above it is refused, a claim whose payees in all are above
      it is warned. A destination that is a supplier's approved Supplier M-Pesa Account is not
      capped and is noted on the line.
    - The same destination twice is warned.
    - A rejected claim: no check; its lines become Cancelled at submit.
  - `check_b2b(doc)` and `check_b2c_row(parent, row)`: a payout of a line pays the line's claim
    on its claimant, to the line's destination (a draft B2B request takes it from the line), at
    most the line's amount, and the line is not paid, on its way or about to be sent by another
    payout (a second draft is allowed; whichever is sent first takes the line); a B2C row is of
    the claim's company. At the send, under the claim's lock: the switch,
    `employee_documents.payable_problem`, the destination still no employee's, the per-payment
    cap and the day's cap (per company: lines sent today to destinations that are not approved
    supplier accounts, by payouts that did not fail, plus this payout; a B2C batch counts all its
    rows). While the day's cap is on, it is counted under a per-company lock
    (`claim-payee-day:<company>`, taken before the claim's) by a locking read, so two claims of a
    company sent at the same moment never both pass it. The line then shows In Flight with its
    payout and sent time.
  - `check_supplier_request(doc)`: a B2B request that pays no employee (a supplier's) and names a
    payee line is refused, on save and at the send (`b2b.validate_request`,
    `b2b.check_ready_to_send`).
  - `sync_lines`: each line's status and result follow its payouts (B2B requests and B2C rows
    that name it); the furthest one decides. A line paid twice is flagged and alerted.
    `on_payout_result(doctype, name)` runs it after a B2B Result, after a B2B request's ack (one
    M-Pesa refuses at once fails its line there and then) and after a B2C row's result; safe to
    call again (a till's name that comes later is checked then).
  - Name check: the number in front of the name M-Pesa paid must be the line's (a masked phone's
    shown digits must agree); the name must match the claimant's name for the payee; a bank
    account is compared with its bank's Name M-Pesa Shows (only the number while the bank has
    none). A mismatch: a Comment on the claim, an Error Log and an alert to the Notify Role, once.
    A till's result names no one: "Name not checked (till)" until the Transaction Status answer
    fills it.
  - Supplier suggestion: a till, paybill or bank account paid `suggest_supplier_after` times (3)
    from claims, and no supplier's approved account, is offered on the claim and on the line as a
    new Supplier M-Pesa Account, prefilled (the user picks the supplier).
  - `check_retry(request)`: a failed B2C payout of a payee line retried by its own buttons
    (B2C Disbursement Request Retry, and the disbursement's Retry Failed Payments, which calls
    it) is a send like the first: refused once the line has another payout paid, on its way or
    about to be sent (`retry_problem`: Retry Failed Payees may have paid it since), then the
    send's checks (switch, payable, never an employee's phone, caps).
  - `paid_in_parts(doctype, name)` / `others_taken_off(...)`: an Expense Claim with payee lines is
    paid by several payouts at once (a B2B request per merchant, a B2C batch, the rest to the
    employee). B2C's double-payout guards then take its other open payouts (drafts of merchants'
    B2B requests included, after the merge) off what it owes instead of refusing the send as a
    "Duplicate Payout"; each line's own double payment stays refused by `check_b2b` /
    `check_b2c_row`.
- **Endpoints** (all POST, all checked on the server):
  - `pay_payees(claim, b2b_settings, b2c_settings)` (**Pay Merchants and Payees**):
    PREPARE_ROLES and read on the claim. Draft payouts for the lines with none: one B2B request
    per merchant line (employee = the claimant, expense_claim = the claim, claim_payee = the
    line), one B2C Payment Disbursement for all the phone lines (rows on the claimant and the
    claim, partyb = the phone). The shortcode is the one picked, or the company's only B2B / B2C
    one. Under the claim's lock; what the lines ask must fit what the claim has left to pay
    (`owed.employee_doc_left_to_pay`). Made with ignore_permissions after these checks; nothing
    is sent. A Comment on the claim names the drafts.
  - `retry_payees(claim, ...)` (**Retry Failed Payees**): the same for failed lines only.
  - `pay_claims(claims, b2c_settings)` (the Expense Claim list's **Pay Casual Payees by
    M-Pesa**): one B2C batch for the unpaid or failed phone lines of the ticked claims (at most
    200), of one company and one payable account; PREPARE_ROLES and read on each claim.
  - `claim_state(claim)`: what the form shows (the switch, lines brought up to date, totals,
    suggestions, why the claim cannot be paid, a reimbursed-total gap). Read on the claim (create
    for a claim not saved yet).
  - `parse_payees(text, file_url, claim, employee, existing)` (**Paste Payees**): pasted rows
    (name, phone, amount; tab or comma; a header row in any order) or a CSV / XLSX upload, checked
    row by row (a name, a Safaricom number, amount above 0 in whole shillings within B2C's limits,
    never an employee's phone, no phone twice in the list or on the claim). Adds nothing: the
    form appends the good rows. Write on the claim (create before its first save).
  - `repair_reimbursed(claim)` (**Repair Reimbursed Total**): ADMIN_ROLES and read on the claim.
    HRMS's own `update_reimbursed_amount` under the claim's lock, with a Comment.
- **hooks.py**: `doctype_js["Expense Claim"]` and `doctype_list_js["Expense Claim"]` are lists
  (WP3's script first, then `expense_claim_payees.js` / `expense_claim_payees_list.js`); Frappe
  15 and 16 load every file of a list (`frappe.desk.form.meta.get_code_files_via_hooks`, and
  `frappe.append_hook` keeps a list). Expense Claim `doc_events`: `validate`
  (`validate_claim`), `before_update_after_submit` (`keep_results`: a save after submit keeps
  the server's status and result) and `on_cancel` (`on_cancel_claim`: unpaid lines Cancelled).
- Call hunks: `b2b._validate_employee_request` and `b2b._check_employee_ready` call
  `claim_payees.check_b2b`; `b2b._record_result` calls `on_payout_result` after the status is
  written; `B2CPaymentDisbursementReference.validate` calls `check_b2c_row` for a row with
  `claim_payee`; `b2c_response_service.update_b2c_reference_status` calls `on_payout_result`.
  Review round: `b2b.validate_request` and `b2b.check_ready_to_send` call
  `check_supplier_request` on their supplier path; `common.apply_ack` calls `on_payout_result`
  for a B2B request (logged, never raised); `B2CDisbursementRequest.retry_failed_payment` calls
  `check_retry` after `validate_still_owed` (it marks the line In Flight: last, before the send);
  `validate_still_owed` and
  `B2CPaymentDisbursement.validate_no_double_payout` call `paid_in_parts` / `others_taken_off`
  (the latter adds them to the advances it takes off).
- **Desk**: `public/js/expense_claim_payees.js` (fields by type, the bank picker filling the
  paybill, the expense line picker, the "Payees: paid / in flight / failed / left" summary,
  name-check and suggestion notes, the buttons gated by `frappe.boot.mpsa.can_prepare` /
  `can_approve` and the claim's state) and `public/js/expense_claim_payees_list.js` (extends
  `frappe.listview_settings["Expense Claim"]`, never replaces it).
- `permission_matrix.py`: the six endpoints; `docs/ROLES_AND_PERMISSIONS.md` regenerated.

## Settings added
None. Uses WP0's `allow_claim_payees` (off), `claim_payee_max_amount` and `claim_payee_daily_cap`
(0 = no cap, the default: the user's decision 3) and `suggest_supplier_after` (3; 0 = never).

## Behaviour changes for users
- Nothing changes until an M-Pesa Manager switches on Allow Paying Merchants and Casual Payees
  from Expense Claims and HR ticks Paid to Merchants or Casual Payees by M-Pesa on the expense
  types that may be paid this way.
- Then: the claimant (or HR / Accounts) lists the payees on the draft claim, or pastes them; the
  expense approver approves them with the claim; a preparer presses Pay Merchants and Payees;
  an M-Pesa Payer who did not prepare the payouts sends each with Send via M-Pesa.
- A claim with payee lines cannot be saved with an employee's phone, till, paybill or bank account
  as a payee (nor an employee's phone as a paybill or bank account number), nor with lines above
  what the claim owes.
- Review round:
  - A claim's casual payees (B2C) are sent while its merchants' B2B requests are drafts or on
    their way, and the employee's own reimbursement of the rest too: B2C counts those payouts in
    what the claim owes instead of refusing with "Duplicate Payout". Claims without payee lines,
    and invoices, orders, advances and salary slips, are refused as before.
  - The Retry button of a failed casual payee's payout (and Retry Failed Payments on its batch)
    is refused once Retry Failed Payees paid that payee by another payout, and otherwise checks
    the switch, the claim, the phone and the caps like the first send.
  - A merchant's request M-Pesa refuses at once shows its line Failed straight away (Retry
    Failed Payees offers it) instead of In Flight until the claim is opened.
  - Amend of a cancelled claim with payee lines keeps the lines as drafts on the new claim's own
    expense lines; nothing to re-enter.

## Migration notes
- No patch. The migrate makes the custom fields and syncs the new DocType and the two `claim_payee`
  fields (their JSONs' `modified` is 2026-10-27 21:00: keep the later timestamp in the merge).
- Review round: no new field or setting. Its hunks in `b2b.py`, `common.py`,
  `b2c_disbursement_request.py` and `b2c_payment_disbursement.py` merge cleanly onto payouts-r4
  as it stands (WP3's B2B count for claims in B2C's guards included; checked with
  `git merge-tree`). The day's cap takes a new named lock, `claim-payee-day:<company>`, only
  while `claim_payee_daily_cap` is above 0.
- Astral cut-over (later, not here): move the `Astral Casual Payee` rows into `M-Pesa Payees`
  (Phone lines) and tick the four casual Expense Claim Types; disable Astral's own casual-payee
  scripts first.

## Tests
- Unit: `tests/unit/test_claim_payees.py` (127 tests, on b2b_fixture's SQLite site): keys; each
  type's details; the claim's checks (switch, type, expense line, sums, caps at 0 and above 0,
  approved supplier destinations, employee destinations, duplicates, rejected claims); check_b2b
  and check_b2c_row (destination, amount, claim, another payout, caps and the day's cap at the
  send, a locking read under the claim's lock); payout states; sync and name checks (bank, till
  later, masked phone, mismatch once); suggestions; the drafts; retry; the list batch; the paste
  parser; keep_results, the cancel, the form state and the repair; hooks, JSON and migrate fields.
  `tests/unit/test_r4_foundation.py` reads list values in doctype_js / doctype_list_js and the
  Expense Claim `before_cancel` key. 1857 passed (WP0 1724).
- Review round (unit, 141 in the file; 1871 passed in all): a draft or its approval clears copied
  results; an amended claim's expense lines; the v15 currency; an employee's phone as an account
  number; the day's cap under the company's lock by a locking read; a supplier request naming a
  payee line; a B2C row of another company; a payee's own retry checked like a send and refused
  once the line is paid again (and both retry buttons call it under the claim's lock, after
  what the claim owes is checked and before anything is sent); an ack refusal failing its line;
  B2C's double-payout guard and
  retry guard counting a payee claim's other payouts in the amount (test_fix3's and
  test_fix_b2creq's fixtures).
- Review round (bench, written, not run): a failed payee's old payout refused by its own retry
  once Retry Failed Payees paid the line; a mixed claim's phones paid and retried while its till's
  request is still a draft, then the till, the claim reimbursed 4,300; an amended claim's payees
  start as drafts on its own expense line.
- Bench (written, not run here): `money_out/test_claim_payees.py` with named users (an Expense
  Approver submits, an HR User prepares, an M-Pesa Payer sends through
  `send_confirm.send_for_test`): a till, a paybill and a bank account paid and the claim
  reimbursed by Payment Entries to the claimant; a till's name checked when given later and a
  mismatch commented; maker-checker and a changed destination refused before post(); a failed
  line retried and never paid twice; a project claim's entry carries the project; a casual batch
  (one paid, one failed, one name mismatch) and its retry; two claims in one list batch; employee
  phones, a type without payees, sums, caps and the switch refused at save.
