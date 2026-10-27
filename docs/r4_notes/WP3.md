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
    document still on its way, whoever it pays. That covers B2C rows and requests naming it, B2B
    requests with `expense_claim` / `employee_advance`, draft Payment and Journal Entries, and
    planned schedule lines. With `for_update` it reads them under the document's lock
    (`b2c-pay:<dt>:<dn>`).
  - New in `owed`: `EMPLOYEE_DOC_FIELDS`, `B2B_DOC_FIELDS`, `employee_document_of`,
    `linked_employee_document`.
- **The payable gate** (`money_out/employee_documents.payable_problem(doc)`). Every pay path asks
  it: the buttons, the list, B2C rows and requests, B2B requests, and the B2C submit guard. It
  refuses a document that is:
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
  - Get References lists only payable claims and advances, and takes the employee's approved phone.
- **Approved-phone lock** (plan C.9):
  - The row has two new fields, `payee_account_doctype` and `payee_account` (read only, appended at
    the end of the JSON). The row's validate sets them from Mobile / AC No.: the employee's
    approved phone account with that number.
  - While M-Pesa Suite Settings > Pay Employees by B2C Only to Approved Phones is on, an Employee
    row must pay an approved phone account of that employee that can be paid now (not on hold,
    not blocked).
  - Rows a payment schedule makes are left to their line's check. Rows with `claim_payee` set are
    skipped (WP4).
  - A B2C Disbursement Request made by hand is held to the same rule, with the payable gate and
    self-pay.
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
  - These count as salary-free, and the DocType rights apply (Accounts roles see them):
    - B2B requests with `expense_claim` / `employee_advance`;
    - B2C rows and requests (and their status queries) whose reference is a claim or advance;
    - B2C disbursements paying against Expense Claim / Employee Advance;
    - schedule lines for them.
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
- An Accounts User without an HR role now sees B2B and B2C payouts of claims and advances (not
  salaries).
- A claim or advance with a payout paid or on its way cannot be cancelled.

## Migration notes
- No patch. Migrate adds the two new row fields from the JSON.
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
  Manager), or give their preparers both roles. See the notes for the main session.

## Tests
- Unit: 1801 passed (1724 before; 77 new).
  - `tests/unit/test_employee_documents.py` (61 tests):
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
    - cancel guard, `salary_access` narrowing, the fresh-install switch, wiring.
  - `tests/unit/test_employee_pay_js.py` (12 tests, node): buttons only to who the server accepts,
    the headline, both dialogs, and the list action extending HRMS's settings.
  - Updated for the intended changes: `test_fix3.py` and `test_fix_b2creq.py` (B2B is now asked
    about for a claim), `test_r4_foundation.py` (`employee_documents.py` now has endpoints).
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
  - claim payouts visible to Accounts, salaries not.

  All sends go through `send_confirm.send_for_test` (WP1).
