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
    left in whole shillings. It makes a one-line schedule and opens it;
  - a button per schedule that plans the slip.
- **Salary Slip list** (`public/js/salary_slip_list.js`, extends HRMS's list settings): Actions >
  **Pay Selected Slips by M-Pesa**: one schedule, a line per slip to its employee's default
  approved account of any type. Every slip left out is listed with its reason (the answer and the
  schedule's Not Included): not found, not readable, not submitted or cancelled, not in a
  submitted Payroll Entry, withheld, employee not active, no approved account or no default among
  several, nothing left, already planned on another schedule (named), bank entry on the payroll,
  B2C amount limits, Pay From missing. At most 500 slips; one company per schedule.
- **Payroll Entry form** (`public/js/payroll_entry.js`, one separate `refresh` handler at the end):
  headline "M-Pesa: paid · in flight · planned · left (n of m slips)" and links to every schedule
  naming its slips, for who may see salaries (also HR, who cannot read the schedule).
- **`money_out/payroll.py`**:
  - `from_salary_slip(slip, payee_account=None, b2b_settings=None, b2c_settings=None,
    planned_date=None)`: one-line schedule, purpose Payroll, header `payroll_entry` empty (it never
    takes the Payroll Entry's one live schedule); amount `whole_shillings(slip_left_to_pay.left)`.
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
  - `_line_for(header, payroll, slip, day, account=None)`: the optional `account` (the one-off's
    chosen account); without it the employee's default, as before. Routing by the account's type
    (phone -> B2C line, bank account / paybill / till -> B2B line) is unchanged and covers every
    type: verified by unit tests for all four types on the Payroll Entry builder too (plan D.1).
- **Double-pay guards**: a slip planned elsewhere has nothing left, so the one-off is refused with
  the other schedule's name and the bulk lists it; the Payroll Entry's builder then leaves the slip
  out ("nothing is left to pay"); the Payroll Entry cannot be cancelled while any schedule's line
  names its slips. Both builders take the payroll's schedule lock (the one `from_payroll_entry`
  takes) and each slip's own lock, and re-read what is left by a locking read before inserting,
  so a payout or schedule committed meanwhile is counted.
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
- Payroll Entry: an M-Pesa headline with links to the schedules.
- Preparing the Payroll Entry's schedule needs salary visibility (see above).
- Nothing is sent by these buttons: they make planned schedule lines; an M-Pesa Payer (not the
  preparer) pays them with Pay Selected.

## Migration notes
- No patch, no new field, no DocType change. `schedule.py` is unchanged.
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
  the Payroll Entry button's preparer also holds HR User. 1777 passed (WP0 1724).
- Bench (written, not run): new `money_out/test_salary_slip_payouts.py` with named users (Accounts
  User + HR Manager prepares, M-Pesa Payer pays; Accounts-only, HR-only, Payer and System Manager
  refused as preparers): one-off by B2C and by B2B (a non-default bank account) through Pay
  Selected, booked, payable cleared; three selected slips with phone, bank account and till paid in
  one Pay Selected, the headlines after it; a whole payroll selected is the payroll's schedule;
  double-pay refusals both ways (and after closing the payroll schedule); Payroll Entry and slip
  cancel guards; Administrator refused at Pay Selected (needs WP1's `require_named_user` there).
