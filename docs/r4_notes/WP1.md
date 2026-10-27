# WP1: send safety and roles

Only named payers send money, and only through one deliberate step: **Send via M-Pesa** on a
payout (**Approve** on a payee account) replaces Submit, with a server token. System Manager
configures the suite but no longer sends, approves or sees salaries. Plan section 2.A; handover
F3 and F6; `docs/ROLES_AND_PERMISSIONS.md` G17 to G21, decisions D5 and D6.

## What changed
- **Role sets** (`permissions.py`): `PAYOUT_ROLES` = M-Pesa Manager, M-Pesa Payer;
  `ADMIN_ROLES` = M-Pesa Manager; `SALARY_ROLES` = M-Pesa Manager, M-Pesa Payer, HR Manager,
  HR User. `CONFIG_ROLES`, `COLLECT_ROLES`, `FINANCE_ROLES` and `PREPARE_ROLES` unchanged.
- **DocType rights** (`permission_matrix.py` and the JSON; tightened by the review fixes below):
  System Manager reads only (`rRep`) B2C Disbursement Request, B2C Payment Disbursement, Mpesa B2B
  Payment Request, Mpesa Reversal Request, M-Pesa Payment Schedule and Supplier M-Pesa Account,
  and has no row on Employee M-Pesa Account. Full rights on the settings DocTypes stay. The
  generated part of `docs/ROLES_AND_PERMISSIONS.md` is regenerated.
- **Named login**: `permissions.require_named_user()` runs first in the `before_submit` of the
  four payouts, in both B2C retries, in Pay Selected (`prepare_payment`, `confirm_payment`), in
  Receiver Name Confirmed (`party_accounts.confirm_receiver_name`) and in Send via M-Pesa. The
  Administrator exemptions in maker-checker and in Pay Selected's maker-checker and one-off rule
  are gone. Not touched here (WP2): `party_accounts.approve` and `employee_accounts`
  (`own_payment_problem`, `is_manager`). `salary_access` keeps Administrator's view.
- **Send via M-Pesa** (`money_out/send_confirm.py`):
  - `prepare_send(doctype, name, modified=None)` (whitelisted, POST): a named login,
    `PAYOUT_ROLES` (`ADMIN_ROLES` for the two account DocTypes), submit permission on the document
    and a saved draft (a form older than the saved document is refused). It returns `rows`
    (receiver, party, the approved account's expected name, destination with the phone masked
    `2547****5678`, paybill and account, till, or bank and account, amount, the charge estimate
    from the B2C Charge Bands, the document paid), `total`, `charge` / `charge_note`, `pay_from` and
    its shortcode, `prepared_by`, `changed_by`, `entered_by` and `proof` (accounts), `problems`
    (maker-checker would refuse you; a scheduled line; for accounts: you entered it, your own
    account, no proof) and a `token`, or no token when there are problems.
  - The token is random, kept in the cache for 600 seconds and bound to the user, the DocType,
    the name, the document's `modified`, a fingerprint of the amounts and one of the destination
    (maker-checker's fields of who is paid, from where and for which document; the account's
    destination fields).
  - `confirm_send(doctype, name, token)` (whitelisted, POST) checks everything again under the
    document's row lock. The token is refused if it is used before 5 seconds (it is not used up),
    if it is another user's or another document's, or if it is expired or already used. Each
    token is used once, whatever happens next. A change of `modified`, amount or destination is
    refused. It then submits inside `deliberate`.
  - `assert_deliberate(doc)`: second line of the four payouts' `before_submit`. It refuses any
    submit outside `confirm_send` or `deliberate` with a plain message ("... is sent with the Send
    via M-Pesa button on its form ..."). That covers the list's Actions > Submit, REST,
    `frappe.client.submit`, `run_doc_method` and the form's own Submit. Installs, migrates and
    patches pass; tests do not.
  - `deliberate(reason, doc=None)`: a context manager for server flows that are the deliberate
    step themselves. With `doc` only that document passes; it nests and always ends with its block.
    Wrapped now: Pay Selected's B2B submits and its B2C disbursement (`schedule._pay_line`,
    `_pay_b2c_group`), a sent disbursement's B2C requests (`create_b2c_request`) and the Sandbox
    Lab's three payout submits.
  - Two new ENDPOINTS entries (`via:_sendable`) and a HELPERS entry.
- **Reversal maker-checker**: `maker_checker.PAID` watches the reversal's register, receipt,
  amount and shortcode, and `before_submit` calls `maker_checker.check`. Whoever pressed Make
  Reversal does not send it. `maker_checker.preview(doc, user)` gives the summary's message.
- **Desk** (`public/js/mpsa_send.js`, loaded on every desk page): on a saved draft of the six
  DocTypes, Frappe's Submit becomes Send via M-Pesa or Approve for users with
  `frappe.boot.mpsa.can_pay` / `can_approve`. Others see no button. The dialog shows the summary,
  keeps Confirm disabled for 5 seconds with a countdown, then confirms and reloads the form. The
  list views of the six DocTypes no longer offer Actions > Submit. Frappe's
  `ListView.prototype.get_actions_menu_items` is wrapped for these DocTypes only, and their own
  list settings are untouched. The DocTypes' own `.js` files are untouched.
- **M-Pesa Suite Settings**: a save that changes any of these posts a comment on the settings and
  sends an alert to the Notify Role (`party_accounts.alert`), naming who changed what, from what
  to what:
  - "Who Sends a Payout Must Not Be Who Prepared It"
  - "Administrator May Not Send Payouts or Approve Accounts"
  - one-off B2B
  - either hold-hours field
  - the Integration User

  The first save on install posts nothing.
- `boot.py`: the flags follow the new sets, so System Manager gets no `can_pay`, `can_approve`,
  `can_prepare` or `can_see_salaries`. Only the docstring changed.

## Review fixes (branch r4-fix-wp1, on c507634)
- **System Manager is configuration only** (decision 1): read only (`rRep`, the matrix's `READ`)
  on the four payouts, M-Pesa Payment Schedule and Supplier M-Pesa Account; no DocPerm at all on
  Employee M-Pesa Account (employees' salary phones and bank accounts: no list, form, report,
  export or `accounts_for_employee`). The six JSONs' System Manager rows changed, Employee M-Pesa
  Account's was removed; all seven `modified` are 2026-10-27 22:00 (migrate re-imports the
  DocPerms; Frappe replaces a DocType's DocPerm rows on import). Settings, Mpesa Settings, the
  bank paybills, API clients, webhooks and C2B URLs keep their rows.
- **Prepare endpoints** `schedule.move_date`, `plan_again`, `close_schedule` and
  `payroll.from_payroll_entry` check `PREPARE_ROLES` (was `FINANCE_ROLES`, which holds System
  Manager); their DocPerm checks stay. The ENDPOINTS / HELPERS rows say so.
- **Administrator in the statement checks**: Plan Again of a line whose payout may have been paid
  (`schedule._named_manager`, used by `plan_again` and `_check_b2c_not_paid`) and the maybe-paid
  branches of the B2B and B2C requests' `before_cancel` call `require_named_user()` before the
  manager check: `require()` lets Administrator through and `frappe.get_roles("Administrator")`
  holds M-Pesa Manager. With the setting on, the shared login gets "Use your own login".
- **M-Pesa Suite Settings alerts**: `notify_role`, `b2c_approved_phones_only`,
  `allow_claim_payees`, `claim_payee_max_amount` and `claim_payee_daily_cap` are watched;
  Currency values read "0", "5,000", "12,500.50". The alert goes to the Notify Role before the
  save and the one after it (`party_accounts.alert(..., roles=[old, new])`): on_update reads the
  new one from the database, so a save that also changed the role used to alert only the new one.
- **Send via M-Pesa summary of bank accounts**: for a "Bank Account (via Bank Paybill)" on either
  account DocType (and for a B2B request paying one), the destination names the bank (its M-Pesa
  Bank Paybill row's institution name, else the ERPNext Bank) and each row carries
  `account_holder` (`account_holder_name`), shown under the destination by `mpsa_send.js`.
- **M-Pesa Payments Due** (`m_pesa_payments_due.js`): Pay Selected shows for
  `frappe.boot.mpsa.can_pay`, Move Date and Plan Again for `can_prepare` with write on the
  schedule; Refresh Balances stays on the finance roles. The hard-coded payer roles (with System
  Manager) are gone.
- **Bench tests**:
  - `money_in/test_fix2_b2c2.py`, `test_fix_b2creq.py`: retries run as `t.payer()` (made in
    `setUpClass`); new tests: the shared Administrator login is refused for both retries
    (`require_named_user` unchanged). These were the 22 errors at c507634.
  - `money_out/test_payment_schedule.py`: the not-due console submit reloads the request before
    its second submit (the refused first submit left `modified` changed in memory only, hence
    TimestampMismatchError: the bench FAIL at c507634); the send-began cancel and Plan Again run
    as a named M-Pesa Manager, and Administrator is refused for both.
  - `money_out/test_b2b_documents.py`: maybe-paid cancels as `t.manager()`, Administrator refused;
    new: Send via M-Pesa on a direct B2C Payment Disbursement (summary from its real rows, plain
    submit refused, the queued `create_b2c_request` job run as the payer makes and submits the B2C
    request) and on a direct B2C Disbursement Request.
  - `tests/test_roles_permissions.py`: System Manager reads but creates, writes and deletes no
    payout, schedule or supplier account, has no access to Employee M-Pesa Account, and is refused
    by `accounts_for_employee`, Move Date, Plan Again, Close Schedule and the payroll schedule;
    `confirm_send` through `/api/method` is refused for another user and before 5 seconds (the
    token is kept).
  - `mpesa_reversal_request/test_mpesa_reversal_request.py`: Make Reversal's user gets no token;
    another payer sends it with Send via M-Pesa.
  - `m_pesa_suite_settings/test_m_pesa_suite_settings.py`: a save that changes the Notify Role and
    the casual-payee switches alerts the old and the new role.
- **Unit**: `test_send_confirm.py` (bank rows for suppliers, employees and approvals; the new
  watched settings, Currency, the old and new Notify Role; AST: no Administrator comparison in
  `schedule.py`, `_named_manager` starts with `require_named_user()`, each maybe-paid
  `require(ADMIN_ROLES)` follows it), `test_schedule.py` (System Manager refused by the three
  schedule endpoints; Administrator refused by Plan Again), `test_fix_payroll.py` (Administrator
  refused by the B2C cancel and Plan Again), `test_money_out_flow.py` (B2B cancel),
  `test_payroll_schedule.py` (System Manager refused by the payroll schedule), and the new
  `test_r4_send_gates_js.py` (node: the Payments Due buttons per role and flag, the account holder
  in the Send via M-Pesa summary). 2284 unit tests pass (2252 at c507634). The bench tests are
  written, not run here.
- **Migration**: a site whose own rules (Custom DocPerm) give System Manager more on these
  DocTypes keeps them until Check Permissions > Reset; the server checks above refuse it anyway
  for the schedule endpoints, sends and approvals. Give whoever must prepare payouts an Accounts or
  HR role.
- **Not here**: the Third-party account warning that `validate_account` shows only at the
  submit (WP2's `party_accounts`); docs other than this note and `ROLES_AND_PERMISSIONS.md`.

## Settings added
None. This work package uses `refuse_administrator_payouts`, which WP0 added (on by default and
on after migrate).

## Behaviour changes for users
- System Manager can no longer:
  - send a payout, use Pay Selected or retry a payout
  - approve a payee account
  - do the other `ADMIN_ROLES` work: C2B/Pull registration by hand, settling Unknown payouts,
    reconciling by hand, the Sandbox Lab
  - see salary payouts

  Give named people M-Pesa Payer or M-Pesa Manager.
- The shared Administrator login is refused for sends, approvals, Pay Selected, retries and
  Receiver Name Confirmed ("Use your own login") while the Suite setting is on.
- A payout is sent only with Send via M-Pesa: a summary first, then Confirm after 5 seconds. A
  payee account is approved only with Approve. The list's Actions > Submit is gone from these
  six lists. Scripts, REST calls and integrations that submitted payouts are refused.
- Maker-checker now covers reversals and Administrator.
- Changing a payout-safety setting alerts the Notify Role.

## Migration notes
- No patch. The DocPerm change for System Manager ships in the JSON and needs a migrate. On a
  site whose own rules (Custom DocPerm) override these DocTypes, the server refuses System
  Manager anyway (`PAYOUT_ROLES`). Check Permissions > Reset aligns the rights.
- Before the deploy, give payers and approvers named logins. Sites that still pay as
  Administrator must switch "Administrator May Not Send Payouts or Approve Accounts" off.
- Merge notes:
  - The six DocType JSONs' `modified` is bumped to 2026-10-27 20:00. WP2 also edits the account
    JSONs; keep the later timestamp and both changes.
  - The four payout controllers have new imports and a new first two lines in `before_submit`.
  - `party_accounts.confirm_receiver_name` has one small hunk with a local import, so that WP2's
    edits to `approve` and the module imports do not conflict.

## For the other work packages
- **WP2**:
  - Call `send_confirm.assert_deliberate(doc)` (after `require_named_user()`) in
    `party_accounts.approve`, and drop its Administrator exemption.
  - `prepare_send` already offers Approve for both account DocTypes. Its preview repeats approve's
    rules (you entered it, your own account, proof): keep them in step.
  - If new account types (bank via bank paybill) add fields, `_account_rows` and `_b2b_rows` read
    `bank` / `shortcode` / `account_number` today.
- **WP3, WP4, WP5** (and any server flow that submits a payout): submit inside
  `with send_confirm.deliberate("<what>", doc):`, after the payer's own checks. A bulk pay is one
  prepared batch, then one Send via M-Pesa on it. Never call `doc.submit()` on a payout outside
  `deliberate`: the unit test `test_every_server_submit_of_a_payout_is_a_deliberate_step` lists
  the files it scans; add yours.
- **Bench tests**:
  - `money_in/testing.py` has `t.named_user(email, *roles)`, `t.payer()`, `t.manager()` (call
    them in `setUpClass`, before its commit) and `t.send_as(user, doc)`.
  - `t.send_as` presses Send via M-Pesa or Approve and Confirm on a saved draft as `user`, without
    the 5-second wait, through `send_confirm.send_for_test(doc)`, which works only under
    `frappe.flags.in_test`.
  - Prepare as Administrator or a preparer, and send as `t.payer()`, or as `t.manager()` for a
    payment no document caps. A plain `doc.submit()` on a payout now raises "Send via M-Pesa".
  - To test a check that runs after the gate, wrap it in `send_confirm.deliberate("test", doc)`.

## Tests
- **Unit**: 1817 passed (1724 before, 93 new), with `python -m pytest tests/unit -q`. New:
  - `tests/unit/test_send_confirm.py`:
    - the summary for B2B, B2C rows, a till or typed number, and accounts
    - the token's lifecycle: what it is bound to, 5 seconds, the TTL, single use, the wrong user,
      the wrong document, changes to modified, amount or destination
    - roles, the Administrator refusal, submit permission, drafts only
    - `assert_deliberate` and `deliberate`
    - AST checks: each payout's `before_submit` starts with `require_named_user()` and
      `assert_deliberate(self)`; retries, Pay Selected and Receiver Name Confirmed call it; no
      Administrator exemption is left in maker-checker or Pay Selected; every server submit of a
      payout is inside `deliberate`
    - reversal maker-checker, the System Manager desk flags, the settings alerts
  - `tests/unit/test_access_control.py`: the new role sets, and Administrator is checked by
    maker-checker.
  - Updated for the gate: `test_money_out_flow.py` (B2B submit), `test_schedule.py` (schedule-line
    and not-due submits), `test_r4_foundation.py` (send_confirm now has endpoints).
- **Bench** (written, not run here):
  - `money_out/test_b2b_documents.py`: every send goes through `t.send_as(payer)`; new tests:
    only Send via M-Pesa sends (plain submit and list bulk Submit refused, nothing posted),
    Administrator cannot send, a payer who prepared a request gets no token.
  - `money_out/test_payment_schedule.py`: the not-due route test as a named payer with the gate;
    new test: Administrator cannot Pay Selected.
  - `money_out/test_payroll_payouts.py`: employee account approval and the no-slip B2B through
    `t.send_as` (M-Pesa Manager).
  - `tests/test_roles_permissions.py`: System Manager refused for sends and approvals; no plain
    submit and no Administrator; the `prepare_send` route by role.
  - `mpsa_suite/doctype/mpesa_reversal_request/test_mpesa_reversal_request.py`: System Manager
    not a submitter, reversal maker-checker.
  - `mpsa_suite/doctype/m_pesa_suite_settings/test_m_pesa_suite_settings.py`: the settings
    comment and alert.
  - Not changed: `money_out/test_statement.py`, which submits no payouts (its payouts are written
    with `db_insert`).
