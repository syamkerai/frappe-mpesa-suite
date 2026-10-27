# Round 4 final review, area C: roles, send safety, platform, money in

Fixes from the final review of `payouts-r4` (56f8e74), branch `r4-final-c`. Behaviour changes, one per bullet.

## Roles: who prepares a claim's or advance's payout
- HR User and HR Manager now create (prepare, never send) Mpesa B2B Payment Requests, and HR User creates B2C Payment Disbursements, on the suite's own rights (`rwcadRepmh`, no submit or cancel). With HRMS's default rights alone, an HR User or HR Manager can now press Pay employee (phone) and Pay employee (bank / paybill / till) on an approved Expense Claim. Before, nobody could prepare a claim's B2B, and an HR User could prepare neither route.
- HRMS's Expense Approver is now a preparer (`PREPARE_ROLES`, the desk's `can_prepare`), with the same prepare rights on both payout DocTypes. HRMS lets only the Employee and the Expense Approver read Employee Advances, so this is the only default role that can prepare an advance's payout (B2C or B2B, the form buttons and the list's Pay by M-Pesa). It can also prepare claim payouts and Pay merchants and payees.
- An Expense Approver now sees the B2C Payment Disbursement and Mpesa B2B Payment Request lists. Salary payouts stay hidden from it (it is not in `SALARY_ROLES`). It sees supplier payouts and claim and advance payouts, as HR Manager already did for B2C disbursements.
- HR User and HR Manager now also see supplier B2B requests (they could already see B2C disbursements). Salary visibility is unchanged.
- Nothing is shipped on the HRMS DocTypes. The two payout DocType JSONs carry the new rows (`modified` 2026-10-27 23:30). A site whose own rules (Custom DocPerm) override these DocTypes, such as Trident, gets them only after Check Permissions > Reset.
- The generated part of `docs/ROLES_AND_PERMISSIONS.md` has a new Expense Approver column. `PREPARE_ROLES` lists it.

## Send via M-Pesa: who an Expense Claim payee payout pays
- A B2B request for an Expense Claim's merchant line (`claim_payee`), and each B2C row for a casual payee, now name the payee from the claim as the Receiver (its Payee Name). Before, they named the claimant.
- Party now reads "payee on <claim>, claimant <name>". For reads "Expense Claim <claim>, payee row #<n>".
- The destination of such a line now ends with "(payee of Expense Claim X, not an approved account)". Before, it read "(a number not saved on a supplier)". When the destination is a supplier's approved Supplier M-Pesa Account, it ends with "(a supplier's approved account, MSA-...)" and the account's expected name is shown.
- A bank-account payee line names its bank (the M-Pesa Bank Paybill row's institution name).
- The summary now gives no token, and says why first, when the send would refuse:
  - the payer is an employee being paid: the B2B employee, a B2C row's employee, the employee of a claim or advance a row pays, or a B2C request's employee;
  - an employee B2B request names no Salary Slip, Expense Claim or Employee Advance and the payer is no M-Pesa Manager;
  - paying claim payees is switched off;
  - a payee line is above the per-payment cap;
  - the company's payee day cap would be passed.

  Before, the token was issued and Confirm then failed.
- Approve on a payee account now lists what to check under "Check before you confirm", without stopping the approval:
  - a bank account whose holder is not the supplier or employee (a third party's account);
  - an employee account that is a business paybill or till.

  These warnings used to show only as a message at the save or the submit.

## Settings
- Changing "Expense Claim Workflow States That May Be Paid" (the payable gate of claims and advances), or clearing it, now posts a comment on M-Pesa Suite Settings and alerts the Notify Role, like the other payout-safety switches. The states read "Approved, Pending Attachment"; blank reads "not set".

## Install and the rename
- `install-app mpsa_suite` no longer switches on "Pay Employees by B2C Only to Approved Phones" on a site that still has `frappe_mpsa_payments` installed (the rename: Trident, Astral), or that has any B2C Disbursement Request or B2C Payment Disbursement. A fresh site still gets it on. Before, the rename switched it on, and every B2C payout to an employee without an approved phone account (claims, advances, payroll) was refused from day one.

## Reversals
- A reversal whose result is Unknown (M-Pesa may have returned the money) is now cancelled only by a named M-Pesa Manager. A Payer and the shared Administrator login are refused ("Use your own login"). The cancelled reversal stays Unknown, so a late Result still matches it.
- After such a cancel, only a named M-Pesa Manager can make a new reversal of that C2B receipt (Make Reversal, or a draft by hand). Others are told that M-Pesa may already have returned the payment.
- A new reversal is refused outright when a cancelled one was later answered Reversed or Already Reversed.
- The cancel now decides on the status the database holds (a form opened before the Result carries an older one).

## Payment Request prompts (money in)
- A Payment Request whose M-Pesa prompt was paid but is not booked yet no longer offers Resend M-Pesa Prompt. Not booked yet means Completed or with a receipt, not reconciled, and no submitted Payment Entry carries the receipt. The form says "M-Pesa prompt X was paid (receipt) and is being booked: no new prompt is sent until it is." Before, the button made a new prompt for the whole outstanding, and the customer could pay twice.
- On the server:
  - a new prompt for such a Payment Request asks at most what is left after the paid prompts, and is refused when nothing is left;
  - resending another prompt of it is refused when what it asks for is more than is left;
  - Request M-Pesa payment on the Sales Order or Sales Invoice counts the request as still asking for its amount, and never supersedes (cancels) it.
- A prompt whose receipt a reversal returned (Reversed, Already Reversed) does not count as being booked.

## Wallet balances (F12)
- The movement check of the sub-account that collects now subtracts the C2B receipts the ERP itself reversed: Mpesa Reversal Requests completed Reversed in the period, amount plus charge, cancelled ones whose late Result said Reversed included. Before, each such reversal showed as a Gap and alerted the Notify Role.
- A payout counts as in flight for the balance checks and the reserved-money warning only if it was sent or changed in the 24 hours before the balance. Before, one old payout still Initiated or Unknown (for example a B2B waiting weeks for a manager to check the statement) suppressed every reserved-money warning and delayed every check by a day.

## Check Transaction Status (F11)
- The answer for a payout receipt now follows the payout form's own visibility rule (`salary_access.has_permission`).
  - A B2B payout of an Expense Claim or Employee Advance is no longer answered as a salary.
  - A claim's or advance's payout (B2C or B2B) is hidden from an asker who cannot read that DocType: "This receipt is an ERP payout of an Expense Claim or Employee Advance, which you cannot read."
  - The payout's creator sees it.

## Integration to-do, area C
- Done here: the third-party account warning in the Approve summary. It already showed on save, since validate runs on a draft's save.
- Checked, nothing to change:
  - a bank account's Approve / Send summary names the bank paybill's institution (done by the WP1 review);
  - B2B request drafts and schedule lines are server-gated: System Manager has only `rRep` on both, and the schedule endpoints check `PREPARE_ROLES`;
  - System Manager has no DocPerm on Employee M-Pesa Account (the matrix test compares the JSON).
- Not area C: `connections.EMPLOYEE_DOCUMENTS` is area B. The three bench errors at 56f8e74 are in area A's tests.

## Tests
- Unit: 2672 passed (2623 at 56f8e74).
  - `tests/unit/test_r4_final_c.py` (new): the claim and advance preparers, no DocPerm on the HRMS DocTypes, the phone lock on the rename and on B2C history, supersede and the prompts whose money went back, the Unknown reversal's cancel and the reversal after it.
  - `test_send_confirm.py`: payee receivers for till, bank, paybill, a supplier's approved account and casual B2C rows; the payer, no-document, switch and cap previews; Approve warnings; the watched workflow states.
  - `test_b2b_status.py`: the claim payout's status answer.
  - `test_balance_movement.py`: a reversal in the movement, and an old Unknown payout.
  - `test_fix_stkpr.py` and `test_r4_collect.py`: the paid-unbooked prompt.
  - `test_r4_buttons_js.py`: no Resend, and the intro.
  - `test_r4_send_gates_js.py`: the warnings block.
  - `test_r4_foundation.py`: `PREPARE_ROLES` and the Expense Approver's flags.
- Bench (written, not run):
  - `tests/test_roles_permissions.py`: the Expense Approver is in the matrix check; a new test that each default reader of claims or advances passes every prepare gate.
  - `money_out/test_employee_documents.py`: a new test that HR User alone and Expense Approver alone prepare both routes for a claim, and the Expense Approver an advance's B2B. This is a small hunk in area A's file: its fixtures are there.
  - `doctype/mpesa_reversal_request/test_mpesa_reversal_request.py`: the Unknown cancel and the new reversal.
  - `doctype/m_pesa_suite_settings/test_m_pesa_suite_settings.py`: the workflow states alert.
  - `money_in/test_fix_stkpr.py`: a paid-unbooked prompt refuses a new prompt and a resend.
