# WP2: payee accounts with many destinations, the bank paybill list, defaults and routing

A supplier or an employee can now hold approved M-Pesa accounts of four kinds: a phone, a
paybill (with its account number), a till, or a bank account paid through the bank's M-Pesa
paybill. Each is still one record with its own proof, second-person approval and hold period. One
account is the party's default. The suite ships the paybills of the top Kenyan banks.

## What changed
- **Account types** (Supplier M-Pesa Account and Employee M-Pesa Account): `Phone (B2C)`,
  `Paybill`, `Till (Buy Goods)` and `Bank Account (via Bank Paybill)` on both. Supplier keeps the
  default `Paybill`, Employee keeps `Phone (B2C)`. Employee's old `Bank Paybill (B2B)` is renamed
  to `Bank Account (via Bank Paybill)` on migrate.
- **New fields on both DocTypes**: `phone` (new on Supplier), `bank_paybill` (Link to M-Pesa Bank
  Paybill, labelled Bank), `bank` (ERPNext Bank, fetched from the bank paybill, read only; new on
  Supplier), `account_holder_name`, `needs_reapproval` and `reapproval_reason` (read only, set by
  the server), `destination_key` (hidden, indexed). `is_default` is read only once approved.
  `expected_name` is copied from the bank for a bank account. The link search shows each
  account's type and number.
- **New DocType M-Pesa Bank Paybill** (module Mpsa Suite, named by `code`): institution, type
  (Bank, Microfinance Bank, Sacco, Other), optional ERPNext Bank, paybill (unique, 5 to 7 digits,
  never one of this company's own shortcodes), account number label, rule and optional pattern
  (a regular expression enforced on payee bank accounts), the name M-Pesa shows (exactly as
  Safaricom shows it, e.g. "KCB Paybill AC"), the name M-Pesa showed last, enabled, source,
  verified on. Track changes is on. System Manager and M-Pesa Manager create and change rows;
  Accounts Manager and User, HR Manager and User, Purchase Manager, M-Pesa Payer and M-Pesa Viewer
  read them. A shipped row cannot be deleted (disable it instead).
- **Shipped list** `mpsa_suite/money_out/data/bank_paybills.json`: 43 institutions from the
  2026-10-27 research (the top 50 Kenyan institutions; 7 had no paybill or only a low-confidence
  one and are left out). 39 high-confidence rows ship enabled, 4 medium-confidence rows (HBZ,
  Sumac MFB, Choice MFB, Kingdom Bank) ship disabled and marked "Unverified". Only KCB ships a
  confirmed Safaricom name ("KCB Paybill AC", seen in a live result); the others leave it empty
  until a payment shows it. Eight banks whose own wording fixes the account number length ship a
  pattern (Family, Paramount, Access, I&M, M-Oriental, Premier, Standard Chartered, CIB).
- **`money_out/bank_paybills.py`**: `plan_seed` (pure) and `sync_shipped`, run after install and on
  every migrate: a missing row is inserted, or a row an admin made for the same code or paybill is
  adopted as it is; a field changes only while it still holds what the previous release shipped
  (an admin's edit is kept, with a Comment saying what the release ships); a row marked `retired`
  is disabled unless an admin changed it. A row that fails is logged and never stops the migrate.
  A change of a row's paybill (by an admin or a release) blocks every approved account that
  copied the old number (`needs_reapproval`, a Comment, an alert to the Notify Role with a count
  and no names), and changing it back unblocks them.
- **Validation per type** (`party_accounts.validate_account`):
  - Phone: kept as 2547.../2541...; a number that does not start like Safaricom's gets an orange
    warning (not refused: ported numbers keep their prefix).
  - Paybill and till: 5 to 7 digits, never the company's own; a paybill needs its account number;
    a plain Paybill whose number is an enabled bank paybill is refused ("pick Bank Account and
    <bank>"), unless its account number is a business's code#reference (see Review fixes).
  - Bank account: the bank must be enabled; the paybill and the name M-Pesa shows are copied from
    it; the account number is kept without spaces and dashes, letters and digits only, at most 20,
    and must fit the bank's pattern when it has one; the account holder defaults to the party's
    name, and a different name gets a "third-party account" warning.
  - An employee paid to a business paybill or till gets an orange warning.
  - Copying the bank's paybill or name again at approval is nobody's change: the person who
    entered the account stays who entered it. A paybill that changed on the bank's row since the
    draft was saved is refused at approval (see Review fixes).
- **Uniqueness** on `destination_key`, across both DocTypes: the same destination for the same
  party is refused; for another employee (on an employee's account), refused; for another
  supplier, warned; between a supplier and an employee, a phone or bank account is refused (the
  ghost-vendor pattern) and a paybill or till warned. An employee is named only to who may see
  salaries ("an employee" otherwise). A shared bank paybill never collides: the key includes the
  account number.
- **Name check after payment**: the number M-Pesa puts in front of the name (also after an
  en-dash) must be the account's shortcode, or its phone when not masked. A bank account is
  checked against its bank's current Safaricom name; while the bank has none, only the number is
  checked, the name seen is kept on the bank row and M-Pesa Managers are alerted to confirm it. A
  till's result names no one: its form says "Name not checked (till)" (the Transaction Status fix
  is WP7's F2.4).
- **Defaults**: one per party. When a second account is approved and the first was the default
  only because it was the only one, the first is ticked. Cancelling is never refused; the one
  account left becomes the default, and with two or more left none is guessed. A blocked account
  (name mismatch, bank paybill changed) cannot be the default; one on hold can. **Make Default**
  (a button and a whitelisted method on both DocTypes) replaces ticking the box after approval:
  Supplier: M-Pesa Manager, Accounts Manager and User, Purchase Manager (not System Manager: see
  Review fixes); Employee: ADMIN_ROLES and HR Manager; a named login for both. It leaves a
  Version and a Comment, and an employee's change alerts the Notify Role (no names). A save after
  submit that changes `is_default` is refused. Lines and draft requests keep the account they
  name.
- **Approval** (`party_accounts.approve`): the Administrator exemption is gone; the approval calls
  `permissions.require_named_user()` and `send_confirm.assert_deliberate(doc)`. The Administrator
  exemptions in `employee_accounts.own_payment_problem` and `is_manager` are gone too.
- **Payment-time routing**:
  - B2B request: supplier and employee accounts of type Paybill, Till or Bank (the command follows
    the type); a phone account is refused for both parties; `check_before_send` compares the
    command too. An employee B2B is no longer bank-only. `salary_left` and `amount_left` pick only
    B2B accounts.
  - Payment schedule: a supplier line takes the supplier's default (or only) B2B account and
    refuses a supplier phone ("pay a supplier's phone with a B2C Payment Disbursement");
    `_send_problem` refuses an account whose type no longer fits the line's route. B2C rows Pay
    Selected makes carry `payee_account_doctype` / `payee_account` once the B2C row has those
    fields (WP3 adds them; guarded by `has_field`).
  - Payroll: the employee's default of any type: a phone by B2C, a bank account, paybill or till by
    B2B. Messages say "bank, paybill or till account" and "press Make Default".
- **Forms**: both account forms show the fields of the chosen type, fill a bank account from its
  bank (paybill, name, the bank's own label and rule for the account number), offer Make Default,
  and show "blocked: approve again" and "pick a default" headlines. Supplier and Employee forms
  list all four types and show a red "none is the default" headline. The B2B request and the
  payment schedule pick B2B accounts and set the command by type. Workspace M-Pesa lists M-Pesa
  Bank Paybill.
- `permission_matrix.py`: the new DocType's rights, the two `make_default` endpoints, and
  `accounts_for_supplier` now goes through `party_accounts.accounts_for`.
  `docs/ROLES_AND_PERMISSIONS.md` regenerated.

## Settings added
None. (The bank paybill list is data; WP0's settings are unchanged.)

## Behaviour changes for users
- HR and Accounts pick a bank from a list for a bank account and type only the account number;
  the paybill is not typed any more. A KCB, Equity, Co-op... paybill typed as a plain Paybill is
  refused.
- A party with two or more approved accounts and no default is shown in red; payroll leaves such
  an employee out until someone presses Make Default.
- Ticking Default on an approved account no longer works: use Make Default.
- The shared Administrator login cannot approve payee accounts while M-Pesa Suite Settings
  refuses it (on by default), and is no longer exempt from the "a different person approves" rule.
- A bank whose paybill an M-Pesa Manager changes blocks the accounts approved on the old number
  until they are cancelled, amended and approved again.

## Migration notes
- No patch. `migrate.after_migrate` (which `after_install` also runs) calls
  `sync_payee_accounts()`: the bank paybill sync, then `party_accounts.after_migrate()` (rename the
  Employee type `Bank Paybill (B2B)`, link bank accounts saved before the list to the bank row with
  the same paybill, fill `destination_key` where empty). All idempotent.
- Existing approved Paybill and Till accounts (Trident's MSA-00001/2 shape) keep their type and
  stay payable; nothing in them is re-validated.
- Sites should check the shipped numbers for the banks they use (M-Pesa Bank Paybill list) and
  enable the four unverified rows only after checking them with the bank.

## Tests
- Unit (fake_frappe): new `tests/unit/test_payee_accounts.py` (types on both DocTypes, keys and
  uniqueness, defaults D1-D5 and Make Default roles, channel picks, B2B for both parties, name and
  number checks, blocked accounts, named-user approval, the migrate step, schedule and payroll
  routes) and `tests/unit/test_bank_paybills.py` (the shipped file, the sync plan, the sync on a
  fake site, row validation and roles, delete refusal, a paybill change). Updated
  `test_employee_accounts.py`, `test_supplier_accounts.py` and `test_payroll_schedule.py` for the
  new types and keys. 1836 passed (WP0 1724).
- Bench (written, not run): new `money_out/test_payee_accounts.py` (the shipped list and a second
  sync, an admin's edit kept by a release, CONFIG-only changes and the delete refusal, HR entry and
  named-manager approval with Administrator refused, a bank paybill typed as a Paybill, an
  employee's phone refused for a supplier without naming the employee, Make Default roles and
  Version, a paybill change blocking and unblocking, Paybill/Till/old-bank rows after migrate, a
  supplier bank account paid by B2B with "522522 - KCB Paybill AC" matching). Sends and approvals
  run as named users inside `send_confirm.deliberate`. `money_out/test_payroll_payouts.py`
  updated for the renamed type.

## Review fixes (branch r4-fix-wp2, from c507634)
- **Make Default is not System Manager's** (decision 1): the supplier profile's `default_roles` is
  now M-Pesa Manager, Accounts Manager, Accounts User and Purchase Manager (FINANCE_ROLES without
  System Manager). `make_default` also calls `require_named_user()`, so the shared Administrator
  login cannot change where a supplier's payments or an employee's salary go; the form hides the
  button from it. The `make_default` rows in `permission_matrix.py` and
  `docs/ROLES_AND_PERMISSIONS.md` say so.
- **Make Default from the form works for HR and Accounts**: the button called `frm.call`, which
  sends the whole document to `run_doc_method`; Frappe (v15 and v16) then runs `check_if_latest`,
  which for a submitted document is an update after submit and needs submit permission: only
  M-Pesa Manager holds it, so HR Manager, Accounts User and Purchase Manager were refused before
  `make_default` ran. Both forms now call `run_doc_method` by name (`dt`, `dn`), which checks read
  only; `make_default` keeps its own role, named-user and read checks.
- **A save after approval never unblocks an account**: `needs_reapproval`, `reapproval_reason`,
  `name_mismatch`, `last_receiver_name` and `last_paid_on` lost Allow on Submit (the code writes them
  with `frappe.db.set_value` only), and `before_update_after_submit` (`refuse_default_edit`) refuses
  a change to any of them as well as to `is_default`, also when a site sets Allow on Submit back
  with Customize Form. Who could do it before: whoever holds submit on the account (M-Pesa
  Manager, and the shared Administrator login), skipping the second approver of a changed bank
  paybill and the named Receiver Name Confirmed step. Roles without submit (Accounts User, HR)
  were already refused by Frappe's own submit-permission check on an update after submit.
- **A bank paybill changed after the draft was saved is not approved unseen**: at approval
  (`before_submit`) the paybill is copied again from the bank's row, after Approve's summary and
  token showed the saved draft. When the row's paybill differs from the saved draft's, the
  approval is refused ("The bank's paybill changed from X to Y since this account was saved: save
  it again and press Approve again"). Saving the draft again copies the new number (still nobody's
  change), and the summary then shows it.
- **A collection reference at a bank paybill stays a Paybill**: a Paybill whose number is an
  enabled bank paybill and whose account number has `#` (a business's code and the payer's
  reference, e.g. "Lipa na I&M" 542542 with 888888#house10) is accepted with an orange warning
  instead of refused; without `#` it is still refused ("pick Bank Account"). A bank account number
  with `#` is refused with a pointer to the Paybill type.
- Tests: unit (`tests/unit/test_payee_accounts.py`): Make Default refused to System Manager and to
  the shared Administrator login, no button for either; a save after approval refused for each of
  the five fields, and an empty field sent back passes; the JSON fields without Allow on Submit;
  the collection reference at I&M (Paybill accepted with a warning, Bank Account pointed to
  Paybill, a plain number still refused, a 14-digit I&M account accepted); the approval refused
  after a bank-row paybill change and accepted once saved again. Bench (written, not run):
  `money_out/test_payee_accounts.py`: Approve through `send_confirm` (`t.send_as`) as a named
  M-Pesa Manager after a bank-row paybill change (refused, then approved at the new number after a
  save); Make Default through Frappe's router (`/api/method/run_doc_method` with `dt`/`dn`) as an
  Accounts User and an HR Manager, refused to a System Manager and to Administrator; a save after
  approval refused (HR and Accounts User by Frappe, an M-Pesa Manager by the hook, through
  `doc.save` and `frappe.client.set_value`); an employee's till paid by B2B (BusinessBuyGoods, no
  account number, the request's name as Daraja's AccountReference, no name check).
  `money_out/test_payroll_payouts.py`: a payroll salary to an employee's Paybill default, sent by
  Pay Selected as BusinessPayBill with the account number and booked by `salary_entries.for_b2b`.
