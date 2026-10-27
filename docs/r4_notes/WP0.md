# WP0: round 4 foundation

Shared scaffolding the other round 4 work packages build on. Apart from the maker-checker fields,
nothing changes for users yet: the new settings, hooks and stubs do nothing until WP1 to WP6 fill
them.

## What changed
- **M-Pesa Suite Settings, Money Out tab**: two new sections.
  - *Payout Safety*: `refuse_administrator_payouts` ("Administrator May Not Send Payouts or Approve
    Accounts", on by default) and `b2c_approved_phones_only` ("Pay Employees by B2C Only to
    Approved Phones", off).
  - *Expense Claim Payees*: `claim_payable_workflow_states` (one per line; blank = any submitted,
    approved claim), `allow_claim_payees` (off), `claim_payee_max_amount` and
    `claim_payee_daily_cap` (0 = no cap, the default), `suggest_supplier_after` (3).
- **permissions.py**: new `PREPARE_ROLES` (M-Pesa Manager, Accounts Manager, Accounts User, HR
  Manager, HR User). New `administrator_refused(user=None)` and `require_named_user()`: the shared
  Administrator login is refused while the setting is on (a site not yet migrated reads it as on);
  installs, migrations and patches are never refused, bench tests are. Not called anywhere yet
  (WP1 calls it). The existing role sets are unchanged (WP1 changes them).
- **maker-checker** (`money_out/maker_checker.py`): the person sending a payout can no longer
  change, in the submitting save, the B2B request's employee, employee account, salary slip,
  invoice, order, expense claim, advance, receiver shortcode, account number or command; the
  document (reference DocType and name) of a B2C Payment Disbursement row; or the party and
  document of a B2C Disbursement Request. The Administrator exemption stays (WP1 removes it).
- **hooks.py**:
  - `app_include_js` is a list: `utils.js` and a new `mpsa_send.js` (stub, WP1).
  - Form scripts (stubs) for Expense Claim, Employee Advance (WP3), Salary Slip (WP5), Sales Order,
    Customer (WP6); list scripts (stubs) for Expense Claim, Employee Advance (WP3) and Salary Slip
    (WP5). Each list stub says how to extend HRMS's `frappe.listview_settings[...]` without
    replacing it (Frappe loads ours after HRMS's).
  - Connections hooks for Expense Claim, Employee Advance, Salary Slip, Sales Order, Sales
    Invoice, Customer, Payment Entry, Journal Entry, Payment Request, POS Invoice, Bank
    Transaction and Bank Account, one function per DocType in `money_out/connections.py` (Frappe
    passes only `data` to these hooks, on v15 and v16, never the DocType). They return the
    dashboard unchanged for now (WP6, WP3).
  - `before_cancel` on Expense Claim and Employee Advance to
    `money_out/employee_documents.before_cancel` (no-op, WP3).
  - `boot_session = mpsa_suite.boot.boot_session`: `frappe.boot.mpsa` carries `can_prepare`,
    `can_pay`, `can_approve`, `can_collect`, `can_configure`, `can_see_salaries`, from the role sets
    and `salary_access.can_see_salaries`. The shared Administrator login gets no `can_pay` or
    `can_approve` while the setting refuses it. No new whitelisted function.
- **Custom fields** (`migrate.create_custom_hrms_fields`, made on every migrate): `expense_claim`
  (Link Expense Claim) and `employee_advance` (Link Employee Advance) on Mpesa B2B Payment Request,
  after `payroll_entry` (the salary slip's pair), indexed, shown for an employee request with
  neither of the other two documents.
- **`money_out/send_confirm.py`**: the Send via M-Pesa / Approve contract, signatures only
  (`prepare_send`, `confirm_send`, `assert_deliberate`, `deliberate`). WP1 builds it.
- `docs/ROLES_AND_PERMISSIONS.md`: the generated role-set table lists `PREPARE_ROLES`.

## Behaviour changes for users
- Maker-checker: a payer who edits the destination, the employee or the paid document of a B2B
  request, or the document or party of a B2C payout, in the same save that sends it, is refused
  ("was changed while being sent"). Save the change first; another person then sends it.
- Nothing else is visible yet.

## Migration notes
- No patch. `after_migrate` makes the two B2B custom fields; `ensure_suite_settings` gives the new
  settings their defaults on sites that already have M-Pesa Suite Settings
  (`refuse_administrator_payouts` = 1, the caps 0, `suggest_supplier_after` 3).
- `refuse_administrator_payouts` is on after the migrate, but nothing reads it until WP1. Sites
  that send payouts as Administrator must give their payers named logins before WP1 ships.

## Tests
- Unit: `tests/unit/test_r4_foundation.py` (named-login rule, PREPARE_ROLES, desk flags,
  maker-checker fields, hooks and stubs, settings JSON, B2B custom fields);
  `tests/unit/test_payroll_schedule.py` now expects the two new B2B fields. 1724 passed.
- Bench (not run here): `mpsa_suite/tests/test_r4_foundation.py`, with named test users (M-Pesa
  Payer, M-Pesa Manager, Accounts User, HR User): settings and custom fields after migrate, the
  index on the new links, the named-login rule, desk flags, maker-checker on real documents, form
  and list scripts loaded after HRMS's, Connections building on every hooked DocType.
