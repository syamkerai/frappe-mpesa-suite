# WP3: Expense Claims and Employee Advances paid by M-Pesa (B2C and B2B)

An approved Expense Claim or a submitted Employee Advance that still owes can now be paid to its
employee by M-Pesa from its own form or from the list. The payout goes to the employee's approved
phone (B2C) or to their approved bank account, paybill or till (B2B). It is booked as a Payment
Entry that HRMS accepts, so the claim or advance shows Paid. Plan C.1-4 and C.7-10; payee-account
design section 2 ("B2C row") and section 3.

## What changed
- **What a claim owes** (`utils/payouts.py`, `owed.py`):
  - A claim owes what HRMS says it owes (`get_outstanding_amount_for_claim`, v15 and v16): the
    sanctioned amount plus taxes, less its advances and what was reimbursed. This equals its
    `grand_total` less reimbursed.
  - A claim that its advances cover now owes 0. It used to read as owing the whole sanctioned
    amount.
  - An advance owes `advance_amount - paid_amount`. v16's Partially Paid owes the rest.
  - Get References on a B2C Payment Disbursement (`config.py`) offers that amount
    (`grand_total`, not `total_claimed_amount`), and also lists v16's Partially Paid claims.
  - `owed.b2b_open` knows the claim and advance links (the old `DOC_FIELDS[...]` KeyError is
    gone).
  - New `owed.employee_doc_left_to_pay(dt, dn, ...)`: HRMS's outstanding less every payout of the
    document still on its way, whoever it pays. That covers B2C disbursement rows and Paid B2C
    requests naming it, B2B requests with `expense_claim` / `employee_advance`, draft Payment and
    Journal Entries, and planned schedule lines. It returns `_dict(owed, parts, left)`.
  - By default (`for_update=True`, review fix) it reads them under the document's lock
    (`b2c-pay:<dt>:<dn>`, held until commit), with locking reads, as the contract with WP4 says.
    Only the form's headline (`document_status`) passes `for_update=False`; B2B's draft save
    passes it too (checked again under the lock at the send).
  - New in `owed`: `EMPLOYEE_DOC_FIELDS`, `B2B_DOC_FIELDS`, `employee_document_of`,
    `linked_employee_document`.
- **The payable gate** (`money_out/employee_documents.payable_problem(doc)`). Every pay path asks
  it: the buttons, the list, B2C rows, B2B requests, the B2C submit guard and the retry of a failed
  B2C payout. It refuses a document that is:
  - not submitted, or cancelled;
  - a claim that is not Approved, was paid at entry (Is Paid), or has no Payable Account;
  - an advance with no Advance Account, or with an exchange rate other than 1;
  - not in KES (a v15 claim takes its company's currency), or whose account is not in KES;
  - in a workflow state not listed in M-Pesa Suite Settings > Expense Claim Workflow States That
    May Be Paid. This applies only when the list is set and the DocType has an active Workflow.
    It applies to Employee Advance too: an advance under a workflow must be in a listed state;
  - owing nothing.
- **B2B for a claim or advance** (`b2b.py`):
  - The request's employee is filled in from the document.
  - The request pays one of a Salary Slip, an Expense Claim or an Employee Advance, never two.
  - The amount is at most what `employee_doc_left_to_pay` leaves, checked on save, again at
    submit under the document's lock, and again right before sending.
  - The employee must be the document's, and the company the document's.
  - The destination is one of the employee's approved bank, paybill or till accounts. The only
    exception is a request whose `claim_payee` is set (a payee of the claim, which WP4 checks).
  - A Payer may send it, because the document caps it. A request that names no document stays
    for a manager only (new `b2b.NO_DOCUMENT` message).
  - Nobody pays themselves.
  - `lock_document` takes the claim's or advance's lock first.
  - A supplier request that names a claim or advance is refused.
- **Booking** (`b2b_entries.py`):
  - A Paid request for a claim or advance is booked automatically by a Payment Entry built by
    hand (`build_employee_document_entry`):
    - party Employee: the document's employee, whoever M-Pesa paid;
    - paid to the claim's own Payable Account, or the advance's Advance Account, which ERPNext
      checks against the reference;
    - paid from the shortcode's GL (Working);
    - the receipt as Reference No, and the M-Pesa date;
    - cost center and project from `money_out.common.payout_cost_center` /
      `document_dimensions` (WP7's helpers).
  - The document takes what HRMS says it still owes. Anything it cannot take stays on the entry,
    unallocated, and the request goes to Needs Checking with a comment and an alert.
  - Entry Type in Make Entry learns Expense Claim and Employee Advance (new `expense_claim` /
    `employee_advance` parameters). Moving a payment to another document is for managers only.
    A claim's payout cannot be booked as a salary.
  - The Make Entry dialog on the request form (`mpesa_b2b_payment_request.js`, review fix) offers
    them too: Entry Type Salary Slip / Expense Claim / Employee Advance / Journal Entry for a
    payment to an employee, the request's own document preselected, and an Expense Claim /
    Employee Advance link limited to the employee's submitted ones. Before, the dialog offered only
    a Journal Entry for a claim's payout, which HRMS never counts against the claim.
  - Classify: a Payment or Journal Entry allocated to the document is on target. That includes
    HRMS's Make Payment Entry and Make Bank Entry.
  - `entry_facts` returns the two links.
  - Why not HRMS `get_payment_entry_for_employee` (module `hrms.overrides.employee_payment_entry`,
    signature `(dt, dn, party_amount=None, bank_account=None, bank_amount=None)` on v15 and v16)?
    It takes its bank account from the document's Mode of Payment before the account it is given,
    so a claim that names a Mode of Payment would be paid from the wrong account. Without an
    account for the company, it throws (v16 refuses outright, and v15 throws in ERPNext's lookup).
    The entry is therefore built by hand, as for suppliers. It uses the same party account rule
    (`get_party_account`: payable / advance account), and ERPNext's validate fills the reference
    details through HRMS's `set_missing_ref_details`.
- **B2C** (accounting and the disbursement):
  - `payment_entry_service`: a row for a claim or advance is booked to the document's employee and
    its Payable / Advance Account, not the disbursement's `paid_to`. The project comes from WP7's
    `document_dimensions` hunk in the same function.
  - The disbursement's submit guard counts open B2B requests for the claim or advance. Its retry
    guard (`validate_still_owed`) counts them too.
  - At submit, under the lock, it runs `payable_problem` again.
  - It refuses self-pay: the sender may not be a row's employee, nor the employee of the claim or
    advance a row pays.
  - A row paying a claim or advance pays its own employee: Party Type Employee and the document's
    employee (review fix). A Supplier row, or any other party type, or another employee, is
    refused. The row is booked to the claimant as their reimbursement whoever M-Pesa paid, so a
    row to anyone else would pay an unchecked number and mark the claim paid. Merchants and other
    people are paid as payees of the claim (WP4).
  - Retry (on the request, or Retry Failed Payments on the disbursement) checks again, under the
    document's lock, what the send checked (`employee_documents.check_b2c_retry`, review fix):
    the claim or advance may still be paid, the employee's phone is still an approved account
    that can be paid now while the lock is on (rows with `claim_payee` skipped), and the sender is
    neither the payee employee nor the document's employee. This applies to every B2C retry to an
    employee, salaries included.
  - Get References lists only payable claims and advances, and takes the employee's approved phone.
- **Approved-phone lock** (plan C.9):
  - The row has two new fields, `payee_account_doctype` and `payee_account` (read only, no copy,
    appended at the end of the JSON). The row's validate sets them from Mobile / AC No. on every
    save: the employee's approved phone account with that number.
  - `payee_account` holds the account's name in a Data field, not a Dynamic Link (review fix).
    Frappe refuses a link to a cancelled submittable document before any validate runs, so a
    phone account cancelled after a draft was made (or copied by an amend, which copies no-copy
    fields too) would have blocked the save, the amend and Send via M-Pesa with "Cannot link
    cancelled document". Now the save replaces the stale name, and with the lock on the row is
    refused with the plain "not a phone approved" message.
  - While M-Pesa Suite Settings > Pay Employees by B2C Only to Approved Phones is on, an Employee
    row must pay an approved phone account of that employee that can be paid now (not on hold,
    not blocked).
  - Rows a payment schedule makes are left to their line's check. Rows with `claim_payee` set are
    skipped (WP4).
  - A B2C Disbursement Request made by hand is held to the same rule and to self-pay. It may not
    pay an Expense Claim or Employee Advance at all (review fix): nothing caps it by what the
    document has left or counts it against the document while it is on its way, so a second
    payout of the claim could pass, or it could pay more than the claim owes or another
    employee. Claims and advances are paid by a B2C Payment Disbursement (the buttons, the list,
    Get References).
  - `maker_checker` watches the row's `payee_account`.
  - The receiver-name check now also runs for a row that names its approved account
    (`b2c_response_service`), not only for schedule rows.
- **Buttons and headline** (`public/js/expense_claim.js`, `employee_advance.js`, new shared
  `employee_pay.js`, loaded with `frappe.require`):
  - On a submitted claim or advance, users with `can_prepare` or `can_pay` see the headline
    "M-Pesa: paid X · in flight Y · left Z", with any drafts waiting to be sent and why the
    document cannot be paid. It comes from one call (`employee_documents.document_status`).
  - Preparers get, under M-Pesa:
    - **Pay employee (phone)**: a draft B2C Payment Disbursement of one row, the approved phone,
      or the Cell Number while the lock is off;
    - **Pay employee (bank / paybill / till)**: a dialog of the employee's approved B2B accounts,
      with numbers masked and the default preselected. It makes a draft B2B request.
  - Each button shows only when the document is payable, something is left in whole shillings,
    and the user can create that payout.
  - The server checks `PREPARE_ROLES`, read on the document, create on the payout, the gate and
    the amount left. It makes drafts only: sending is Send via M-Pesa (WP1).
- **The list** (`expense_claim_list.js`, `employee_advance_list.js`):
  - Actions > Pay by M-Pesa on the ticked documents (`employee_documents.prepare_bulk`, at most
    200 at a time). It prepares one draft B2C Payment Disbursement for the employees paid by phone,
    one row per document. It prepares a draft B2B request per document whose employee's default
    account is a bank account, paybill or till.
  - Documents it cannot pay are listed with the reason: not payable, nothing left, another
    company, no approved phone, above the B2C limit.
  - HRMS's own list settings are extended, never replaced.
- **Not salaries** (`salary_access.py`, decision D4 narrowed, plan C.8):
  - These count as salary-free, and the DocType rights apply, for a user who reads that claim or
    advance DocType by a role other than the employee's own (review fix: before, anyone with the
    payout DocType's rights saw them):
    - B2B requests with `expense_claim` / `employee_advance`;
    - B2C rows and requests (and their status queries) whose reference is a claim or advance;
    - B2C disbursements paying against Expense Claim / Employee Advance;
    - schedule lines for them.
  - To a user who cannot read Expense Claim (or Employee Advance) they stay hidden like salaries:
    `salary_access.reads_documents` / `unread_documents`, used by `has_permission`,
    `query_conditions` and `report_condition`. HRMS gives read on claims to HR Manager, HR User and
    Expense Approver, and on advances to Expense Approver; the DocType level is checked, not each
    claim (HRMS has no per-claim permission hook; a site's User Permissions are not reflected).
  - Salary Slips, payroll schedules and employee payouts with no document stay hidden.
  - The generated D4 paragraph says so.
- **Cancel guard** (`employee_documents.before_cancel`, WP0's hook): a claim or advance is not
  cancelled while any payout of it is Paid (also on a cancelled payout) or on its way. That
  covers:
  - B2B requests, drafts included;
  - B2C requests;
  - rows of a submitted disbursement not sent yet;
  - schedule lines sent.

  The message names them.
- **New sites**: `install.after_install` switches `b2c_approved_phones_only` on. Migrate does not
  touch it.
- `permission_matrix.ENDPOINTS`: `document_status` (perm); `prepare_b2c`, `prepare_b2b` and
  `prepare_bulk` (`PREPARE_ROLES` and perm). The generated part of `docs/ROLES_AND_PERMISSIONS.md`
  is regenerated.

## Settings added
None. This work package uses WP0's `b2c_approved_phones_only` and `claim_payable_workflow_states`.

## Behaviour changes for users
- Claims and advances get M-Pesa buttons and a headline. The list gets Pay by M-Pesa. Each payout
  is still sent one by one, with Send via M-Pesa, by a Payer who did not prepare it.
- A claim that its advances cover is no longer offered for payment (it owes nothing).
- A B2B request may pay an Expense Claim or an Employee Advance, and a Payer may send it.
- With the lock on (new sites), a B2C payout to an employee goes only to an approved phone, never
  to a typed number or the Employee's Cell Number.
- A payer cannot send a payout of their own claim or advance, by B2C or B2B.
- A user who reads Expense Claims (or Employee Advances) by a role other than Employee, without
  seeing salaries (an Expense Approver, or an Accounts role the site gave read), now sees the B2B
  and B2C payouts of claims (or advances). An Accounts User or M-Pesa Viewer who reads no claims
  does not.
- A B2C row paying a claim or advance pays the document's own employee only.
- A B2C Disbursement Request made by hand cannot pay a claim or advance.
- A retry of a failed B2C payout to an employee is refused when the claim may no longer be paid,
  the phone is no longer an approved account (lock on), or the sender is that employee.
- Make Entry on a claim's or advance's B2B payout offers the claim or advance itself.
- A claim or advance with a payout paid or on its way cannot be cancelled.

## Migration notes
- No patch. Migrate adds the two new row fields from the JSON. A site that already migrated
  WP3's first build (where `payee_account` was a Dynamic Link) keeps the same varchar column:
  migrate only changes its type in the DocType.
- `b2c_approved_phones_only` stays as the site has it on upgrade (off). Switch it on once your
  employees' phones are approved.
- A B2C draft saved before the upgrade names no approved account. If its phone matches one, the
  first send derives it, and maker-checker reads that as "changed while being sent". Save the
  draft once more, then another person sends it.
- Who sees the buttons depends on the site's rights. HRMS lets HR Manager, HR User and Expense
  Approver read claims, and only Employee and Expense Approver read advances. The suite lets
  HR Manager (not HR User) create B2C Payment Disbursements, and Accounts roles create B2B
  requests. So by default:
  - an HR Manager can pay a claim by phone;
  - nobody can both read a claim and create a B2B request, and nobody in `PREPARE_ROLES` can read
    an advance.

  Sites grant read on Expense Claim / Employee Advance to their Accounts roles (Role Permission
  Manager), or give their preparers both roles. See the notes for the main session. The same
  grant is what lets an Accounts role without an HR role see the claim and advance payouts.

## Tests
- Unit: 1816 passed (1724 before the build; 1801 after it; 15 more with the review fixes).
  - `tests/unit/test_employee_documents.py` (75 tests):
    - owed math, Get References, and left to pay under the lock;
    - the payable gate, workflow states included;
    - B2B validation: one document, cap, account, `claim_payee` skip, Payer vs manager, self-pay,
      send re-check;
    - Payment Entry party account and project, the unallocated rest, classify, and Make Entry
      (move rules, no salary booking);
    - the B2C booking account and the cross-guard;
    - row and request phone lock, and its payee skip;
    - self-pay, Get References phone, the name check, the maker-checker field;
    - `document_status`, the drafts from both buttons, bulk prepare;
    - cancel guard, `salary_access` narrowing, the fresh-install switch, wiring;
    - review fixes: a claim row to a Supplier, another party type or another employee refused; a
      hand-made request for a claim or advance refused; a retry by the claimant, after the phone
      account was cancelled, or of a claim no longer approved refused, and a payee row's retry
      left to `claim_payees`; the row's account a plain name, replaced on save; the lock taken by
      default and not by the headline; claim payouts shown only to who reads claims (HRMS's
      readers, a site's grant, the Employee role not counting).
  - `tests/unit/test_employee_pay_js.py` (13 tests, node): buttons only to who the server accepts,
    the headline, both dialogs, the list action extending HRMS's settings, and the B2B request's
    Make Entry dialog offering the claim or advance.
  - Updated for the intended changes: `test_fix3.py` and `test_fix_b2creq.py` (B2B is now asked
    about for a claim; a claim's retry runs `check_b2c_retry`), `test_r4_foundation.py`
    (`employee_documents.py` now has endpoints).
- Bench (written, not run): `money_out/test_employee_documents.py`, with named users (HR
  Manager; Accounts User + HR User; Expense Approver + Accounts User; Accounts User alone; M-Pesa
  Payer):
  - a real claim paid by B2C to the approved phone, booked on the claim's own payable and marked
    Paid, with the name check on the row's account;
  - the lock refusing a typed number, and self-pay refused;
  - a claim paid by B2B with Payment Entry project made mandatory, booked and marked Paid;
  - an advance by B2B, paid_amount through HRMS;
  - a no-document request still manager-only;
  - bulk prepare;
  - the cancel guard (also on a cancelled-but-Paid request);
  - claim payouts visible to Accounts User + Expense Approver, not to an Accounts User alone, and
    salaries to neither;
  - a claim row turned into a Supplier row refused, and a draft whose phone account was cancelled
    since still saves (no "Cannot link cancelled document"), then the lock refuses it plainly;
  - a hand-made B2C request for a claim refused.

  All sends go through `send_confirm.send_for_test` (WP1).
