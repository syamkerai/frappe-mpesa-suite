# WP5: salaries by M-Pesa from Salary Slips (one-off and selected slips)

Salaries are normally paid from the bank. A company can now also pay a single salary (one-off or
off-cycle) or a set of ticked salary slips by M-Pesa: by B2C to a phone, or by B2B to a bank
account (through the bank's paybill), a paybill or a till, always to an approved Employee M-Pesa
Account. Both build an M-Pesa Payment Schedule, so the schedule's rules apply unchanged: another
person pays it with Pay Selected in M-Pesa Payments Due (maker-checker, never before Pay On, the
5-second confirm), and the salary entries are booked as for the Payroll Entry's schedule.

## What changed
- **Salary Slip form** (`public/js/salary_slip.js`), on a submitted slip, for who may see salaries:
  - headline "M-Pesa: paid · in flight · planned · left" (and "paid outside M-Pesa" when a bank or
    cash entry paid part of it), with the slip's problems (withheld, bank entry on the payroll...);
  - **Pay by M-Pesa** (when something is left, nothing stops it, and the user may prepare): a dialog
    with the employee's approved accounts of every type (the default preselected, each with what
    stops it being paid now), Pay From (B2C for a phone, B2B for the rest), Pay On and the amount
    left in whole shillings. It makes a one-line schedule and opens it. Who may not read Employee
    M-Pesa Accounts (their DocPerms: HR, M-Pesa Manager; e.g. an Accounts User who sees salaries
    through M-Pesa Payer or a custom Salary Slip read) is offered the default account only, its
    number masked ("247247 / ******6789", "2547****0111"), and the server refuses them any other;
  - a button per schedule that plans the slip, for who may read the schedule (not HR).
- **Salary Slip list** (`public/js/salary_slip_list.js`, extends HRMS's list settings): Actions >
  **Pay Selected Slips by M-Pesa**: one schedule, a line per slip to its employee's default
  approved account of any type. Every slip left out is listed with its reason (the answer and the
  schedule's Not Included): not found, not readable, not submitted or cancelled, not in a
  submitted Payroll Entry, withheld, employee not active, no approved account or no default among
  several, nothing left, already planned on another schedule (named; for all **or part** of the
  salary: one salary is never split over two live schedules this way), bank entry on the payroll,
  B2C amount limits, Pay From missing. The answer also gives the breakdown of what is left; the
  schedule's Not Included keeps the reason only, cut to fit its column (Small Text, 65,535 bytes:
  "... and N more (listed when the schedule was made)"). At most 500 slips; one company per
  schedule.
- **Payroll Entry form** (`public/js/payroll_entry.js`, one separate `refresh` handler at the end):
  headline "M-Pesa: paid · in flight · planned · left (n of m slips)" and every schedule naming its
  slips, for who may see salaries (also HR); the names are links only for who may read the
  schedule (`frappe.model.can_read`), plain text for HR.
- **`money_out/payroll.py`**:
  - `from_salary_slip(slip, payee_account=None, b2b_settings=None, b2c_settings=None,
    planned_date=None)`: one-line schedule, purpose Payroll, header `payroll_entry` empty (it never
    takes the Payroll Entry's one live schedule); amount `whole_shillings(slip_left_to_pay.left)`.
    A slip partly planned on another schedule: the one-off plans the rest (an off-cycle top-up);
    Pay Selected Slips leaves such a slip out instead (see above).
  - `from_salary_slips(slips, b2b_settings=None, b2c_settings=None, planned_date=None)`.
    **Decision:** the schedule names the Payroll Entry only when the ticked slips are *all* the
    submitted slips of one Payroll Entry that has no live schedule: it is then that payroll's own
    schedule, exactly as the Payroll Entry's button would make it (its form shows it, its button
    refuses a second one). Otherwise it names none, so a few slips never take the payroll's one
    live schedule and the Payroll Entry's button stays usable for the rest. The cancel guards do
    not depend on it: `_schedules` finds every schedule whose lines name the payroll's slips.
  - `salary_slip_status(salary_slip)` (Salary Slip headline and the dialog's choices, from
    `owed.slip_left_to_pay` and `salary_slip_paid`) and `payroll_salary_totals(payroll_entry)`
    (`slip_figures`: the same reading for all the payroll's slips in five grouped queries, not a
    query loop per slip).
  - `_line_for(header, payroll, slip, day, account=None, left=None)`: the optional `account` (the
    one-off's chosen account), without it the employee's default, as before; the optional `left`
    (what the caller read by a locking read), without it read here. Routing by the account's type
    (phone -> B2C line, bank account / paybill / till -> B2B line) is unchanged and covers every
    type: verified by unit tests for all four types on the Payroll Entry builder too (plan D.1).
- **Double-pay guards**: a slip planned elsewhere has nothing left, so the one-off is refused with
  the other schedule's name and the bulk lists it; the Payroll Entry's builder then leaves the slip
  out ("nothing is left to pay"); the Payroll Entry cannot be cancelled while any schedule's line
  names its slips. All three builders (`from_payroll_entry`, `from_salary_slip`,
  `from_salary_slips`) take the payroll's schedule lock, then each slip's own lock (R4), and read
  what each slip has left ONCE, by a locking read under them, and plan that amount; the payroll's
  live schedule is read by a locking read too (`live_schedule(..., for_update=True)`). On
  MariaDB's REPEATABLE READ a plain read sees the snapshot taken before the locks, so a schedule,
  one-off or payout another worker committed meanwhile is now counted by every builder (before
  this review, `from_payroll_entry` and the whole-payroll check read plain).
- **Speed** (the bulk, and the Payroll Entry's button): the selected slips are read in one query
  and the user's read on them in one `frappe.get_list` (role, user permissions, own and shared
  slips; HRMS and ERPNext register no `has_permission` hook for Salary Slip on v15 or v16), not a
  full document load per slip (`has_permission(doc=...)` loads the slip with its child tables);
  what is left is read once per slip, not three or four times; each Payroll Entry's bank or cash
  entries once per request, not three times per slip (`_bank_entries_once` / `frappe.local`, read
  by `owed.payroll_bank_entries`). The schedule's own validation of each line on insert is
  unchanged (`schedule.py`).
- **Who** (server side, `require_salary_preparer` / `require_salary_reader`):
  - prepare (both builders): `PREPARE_ROLES` (not M-Pesa Payer, not System Manager), who may see
    salaries (`salary_access.can_see_salaries`), with create on M-Pesa Payment Schedule (its
    DocPerms: Accounts Manager, Accounts User, M-Pesa Manager) and read on each slip (HRMS: HR User,
    HR Manager). In practice: an Accounts User or Manager who is also HR (or reads Salary Slips),
    or an M-Pesa Manager who reads slips. An HR-only user is refused (no schedule rights); the
    buttons are hidden from them (`frappe.boot.mpsa.can_prepare`, `can_see_salaries`, and
    `can_create` on the schedule).
  - the headlines: who may see salaries, with read on the slip / Payroll Entry.
  - `from_payroll_entry` now also needs salary visibility (it builds a schedule listing every
    salary of the payroll). With HRMS's defaults nothing changes (only HR Manager reads Payroll
    Entries); a site that gave Accounts read on Payroll Entry needs an HR role (or Salary Slip
    read) for the Accounts users who prepare payroll schedules.
- `permission_matrix.py`: rows for the four new endpoints and the two helpers; the
  `from_payroll_entry` rows name salary visibility. `docs/ROLES_AND_PERMISSIONS.md` regenerated.

## Settings added
None.

## Behaviour changes for users
- Salary Slip: an M-Pesa headline and a Pay by M-Pesa button (submitted slips with something left,
  for who may prepare and see salaries). Salary Slip list: Actions > Pay Selected Slips by M-Pesa.
- Payroll Entry: an M-Pesa headline with the schedules (links for who may read them).
- Pay Selected Slips leaves out a slip planned on another live schedule for part of its salary
  (named); a one-off (Pay by M-Pesa) still plans the rest of such a slip.
- A payout or schedule committed by someone else while a schedule is being built is counted: the
  line plans what is left now (before: the one-off and the list refused "more than is left to pay
  now"; the Payroll Entry's button could plan a salary twice, refused later at Pay Selected).
- Pay by M-Pesa offers only the default account, masked, to who may not read Employee M-Pesa
  Accounts.
- Preparing the Payroll Entry's schedule needs salary visibility (see above).
- Nothing is sent by these buttons: they make planned schedule lines; an M-Pesa Payer (not the
  preparer) pays them with Pay Selected.

## Migration notes
- No patch, no new field, no DocType change. `schedule.py` is unchanged. `owed.py`: one small hunk
  in `payroll_bank_entries` (the per-request read-once, only while a builder runs).
- An off-cycle salary still needs a Payroll Entry (the salary entry clears its payroll payable):
  run a one-employee Payroll Entry in HRMS, then Pay by M-Pesa on its slip.

## Tests
- Unit: new `tests/unit/test_salary_slip_payouts.py` (47): the line per account type for the
  one-off and the Payroll Entry builder; the chosen account checked; refusals with reasons; both
  double-pay directions, the locking re-read and the lock order; the Payroll Entry's cancel; the
  list's reasons, one company, the 500 cap, the whole-payroll rule; who may prepare (Accounts alone,
  HR alone, Payer, System Manager, no slip read) and who may see the figures; both headlines and
  `slip_figures` against `owed.slip_left_to_pay` slip by slip, in a bounded number of queries; the
  scripts call whitelisted endpoints behind the boot flags. `tests/unit/test_payroll_schedule.py`:
  the Payroll Entry button's preparer also holds HR User. Review fixes (+9, 56 in the file): a
  payout and a schedule committed meanwhile seen by every builder's locking read (also the Payroll
  Entry's), the live schedule's locking read, the Payroll Entry builder's slip locks, a slip partly
  planned elsewhere left out of the list, Not Included cut to fit (500 long reasons), the grouped
  reads (no per-slip document load, one locking read per slip, one bank-entry read per payroll),
  the default-only masked account for who may not read accounts, schedule links only for who may
  read the schedule. 1786 passed (WP0 1724).
- Bench (written, not run): new `money_out/test_salary_slip_payouts.py` with named users (Accounts
  User + HR Manager prepares, M-Pesa Payer pays; Accounts-only, HR-only, Payer and System Manager
  refused as preparers): one-off by B2C and by B2B (a non-default bank account) through Pay
  Selected, booked, payable cleared; three selected slips with phone, bank account and till paid in
  one Pay Selected, the headlines after it; a whole payroll selected is the payroll's schedule;
  double-pay refusals both ways (and after closing the payroll schedule); a slip partly planned on
  the payroll's schedule left out of selected slips while a one-off pays the rest; an Accounts
  User + M-Pesa Payer (no Employee M-Pesa Account read, the slip shared with them) offered the
  masked default only and refused the other account; Payroll Entry and slip
  cancel guards; Administrator refused at Pay Selected (needs WP1's `require_named_user` there).

## Depends on other work packages (merge notes)
- The bench test with an employee's **till** (`test_three_selected_slips_one_schedule_mixed_routes_
  one_pay_selected`) needs WP2 (r4-wp2: B2B types for employees in `employee_accounts` and the
  Employee profile's types); without it the test skips itself.
- The Administrator test needs WP1's `require_named_user` at Pay Selected.
- WP2's unit test `test_payroll_pays_a_paybill_or_till_default_by_b2b` (tests/unit/
  test_payee_accounts.py) calls `from_payroll_entry` as `{"Accounts User"}` only: after the merge
  give it `{"Accounts User", "HR User"}` (salary visibility, as in test_payroll_schedule.py).
- WP1's fix round changes `from_payroll_entry`'s `require(FINANCE_ROLES)` to
  `require(PREPARE_ROLES)`; this branch adds `require_salary_reader()` on the next line: keep both.
  Then regenerate `docs/ROLES_AND_PERMISSIONS.md` after all role-matrix merges.
- `owed.py`: this branch's hunk is inside `payroll_bank_entries`; WP3's hunks are elsewhere.
