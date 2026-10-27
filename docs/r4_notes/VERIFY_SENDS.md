# Round 4 verify pass, group sends

Fixes K4, K6, K7, K8, K10 and K11 from the verify pass of `payouts-r4` (81cc821), branch `r4-verify-sends`. What a user notices, one finding per section.

## K4: a bank paybill changed back
- Some payee bank accounts were approved with the shared Administrator login (while the site refuses it), or by the person who entered them. If such an account was already blocked by a bank paybill change, the upgrade skipped it. Changing the paybill back used to unblock it, so it could be paid.
- Now that account stays blocked. Its reason changes to the approval problem ("Approved with the shared Administrator login..." or "Approved by the person who entered it..."). A comment on the account says the paybill is back but the account stays blocked. Cancel, amend and have a named M-Pesa Manager approve it, as for any account the upgrade blocked.
- Accounts approved by a second named person are unblocked as before, with the comment "it can be paid again".
- Accounts approved before approvals were recorded are also unblocked as before.
- A site that allows Administrator approvals (the Suite setting off) also unblocks accounts Administrator approved.

## K6: reversing a B2B salary entry
- Reverse Journal Entry on the salary Journal Entry of a B2B salary payout now saves. Before, it was refused with "M-Pesa pays salaries of <payroll>... would pay them twice", although a reversal pays nobody. ERPNext's reversal drops the entry's M-Pesa tag.
- The guard now ignores any reversal, and any row that credits the payroll payable, whoever made the entry. A bank entry that debits the payroll payable is still checked. That is how HRMS's Make Bank Entry posts on v15 and v16 (per employee, or for the whole payroll). While M-Pesa pays salaries of the payroll, such an entry is still refused as before.

## K7: the payee day cap, said first and at the send
- On a B2C payout of an Expense Claim's casual payees, Send via M-Pesa and the send now count the day's cap the same way. Rows paid to a supplier's approved Supplier M-Pesa Account are left out. The check is whether that account is approved now, not when the claim was approved.
- Before, the send used the account recorded when the claim was approved, and the summary used the account approved now:
  - A row whose supplier account was disabled after the claim's approval was counted by the summary but not by the send.
  - A row whose supplier account was approved after the claim got a token, and then Confirm failed on the day cap.
- Now a disabled account's row counts towards the cap in both places. A newly approved account's row counts in neither.

## K8: a hand-made B2C Disbursement Request to an employee
- Send via M-Pesa on such a request now gives an M-Pesa Payer no token. The summary says why: "A payment to an employee with no Salary Slip, Expense Claim or Employee Advance is paid by an M-Pesa Manager."
- Before, the token was issued, and Confirm used it up and then failed with the same message.
- An M-Pesa Manager still gets a token. A request to a supplier is unchanged.
- When the payer is also the employee being paid, "You are <employee>: another person must pay you" comes first, as at the send.

## K10: the roles document
These are changes to `docs/ROLES_AND_PERMISSIONS.md` only. The code already worked this way after the final pass. Testers following the checklist no longer record the new rights as failures.
- The role table now says:
  - HR Manager and HR User prepare claim payouts on both routes (Pay employee (phone), and Pay employee (bank / paybill / till)). They prepare advance payouts where the site lets them read Employee Advances.
  - HR Manager and HR User see supplier payouts.
  - Expense Approver prepares (never sends) the payouts of the claims and advances it reads: Pay employee, Pay Merchants and Payees, and Pay by M-Pesa on an advance. It sees claim, advance and supplier payouts, never salary payouts.
  - An M-Pesa Manager can mark a claim's payee line settled outside M-Pesa.
- "Who does what" has a new Expense Approver column and a new row: settle a Draft or Failed payee line of a submitted claim outside M-Pesa. That action is M-Pesa Manager only, and the shared Administrator login is refused while the Suite setting is on. "HR has no rights on B2B requests" is gone.
- In the checklist:
  - For Expense Approver, Pay Merchants and Payees, Pay employee and Pay by M-Pesa on an Employee Advance are now under Must work. Send via M-Pesa and salary payouts stay under Must be refused.
  - The HR row names HR User for both Pay employee buttons.
  - The M-Pesa Manager row lists settling a payee line outside M-Pesa.
- Claim and advance payouts are said not to be salary data.

## K11: Resend M-Pesa Prompt on a Payment Request
- The Payment Request form now asks the server which paid prompts are still being booked. It shows "M-Pesa prompt X was paid (receipt) and is being booked..." and hides Resend M-Pesa Prompt only when the server lists one.
- Before, the form trusted only the prompt's Reconciled tick. That tick is never set on two kinds of prompt:
  - a prompt booked through its C2B register;
  - a prompt whose receipt was reversed (the money went back to the customer).

  On those, the form showed "is being booked" forever and never offered Resend.
- Now Resend shows again on those, as the server already allowed.
- New endpoint `mpsa_suite.money_in.payment_request.prompts_being_booked`, for COLLECT_ROLES who can read the Payment Request. It is registered in the permission matrix, and the generated table in the roles document has been regenerated.

## Tests
- Unit: 2770 passed.
  - `tests/unit/test_r4_verify_sends.py` (new) covers:
    - the paybill coming back for Administrator-approved, self-approved, properly approved and unrecorded approvals, with the Suite setting on and off;
    - a reversal, and a row that credits the payable, passing the guard, while HRMS's entry and debit rows are still refused;
    - the send's batch total with a supplier account disabled after the claim's approval, and with one approved since;
    - the B2C summary's day cap using the send's own total;
    - the manager rule on a hand-made B2C request (Payer, Manager, own payment first, supplier);
    - `prompts_being_booked`: a C2B-booked receipt, a reversed one, roles and read.
  - `tests/unit/test_r4_buttons_js.py`: the form asks the server only when a prompt looks paid and not reconciled; it shows Resend when the server lists nothing, and the intro when it lists a prompt.
- Bench, written and not run: `money_out/test_payroll_payouts.py::TestPayroll::test_a_reversal_of_a_b2b_salary_entry_saves`. It reverses the B2B salary entry with ERPNext's `make_reverse_journal_entry` and checks that the reversal has no M-Pesa tag, credits the payroll payable and saves. HRMS's bank entry for the same payroll is still refused.
