# Astral M-Pesa Suite — changes on top of navariltd/frappe-mpsa-payments

Maintained by Astral Industries Limited. Licence and attribution: [NOTICE](NOTICE),
[docs/LICENSING.md](docs/LICENSING.md).

Branch `astral-suite`, based on upstream `version-16` (newest features: STK pay page, POS quick pay,
bundled Safaricom certs, Pull Transactions fixes). Package name stays `frappe_mpsa_payments` so all
callback URLs already registered with Safaricom keep working and `bench migrate` upgrades in place.

## Round 4.1: fixes from Trident's live test (2026-10-28, branch r4-1-fixes)

Round 4 went live on Trident Plumbers on 2026-10-28: nine payouts, clean books, and the defects below.
Generic and settings-driven like the rest: Trident first, Astral on the same code. **`bench migrate`
is needed**: new fields on M-Pesa Claim Payee and M-Pesa Suite Settings, one patch and nine print
formats; then clear the cache (new desk scripts).

**1. A claim's reimbursed total is never short again (money)**
- Live: HR-EXP-2026-00712 showed Total Amount Reimbursed 70 instead of 90. Two B2C rows of one
  batch were booked 2 s apart, each in its own job. HRMS sums the claim's entries with a plain read,
  and that read used a snapshot taken before the lock, so it missed the other booking. It was
  repaired by hand.
- Every booking now takes the lock of the document it pays (Expense Claim, Employee Advance,
  invoice, order or slip) as the first statement of its transaction. That covers the B2C payout
  job, the B2B Result job, Make Entry, Check Entry Again and the 15-minute entry sweep, whose
  savepoint now comes after the lock. What was read to find the document is committed away first,
  and only when nothing is written and no lock is held (`owed.booking_lock`, `owed.fresh_snapshot`).
  A booking waits up to 60 s for the lock, then the sweep retries it.
- After every submitted or cancelled Payment Entry or Journal Entry that pays a claim or advance, a
  job runs once the entry is committed (`employee_documents.check_paid_total`). It takes the lock
  first and recounts the total from the entries:
  - a claim's Total Amount Reimbursed, with HRMS's own `update_reimbursed_amount`;
  - an advance's Paid Amount, with HRMS's own `set_total_advance_paid`.
  When the total changes, a Comment on the document says so.
- Safety net: while a claim's total disagrees with its entries, nothing more is prepared or sent for
  it, to the claimant or a payee. `payable_problems` says why, so every pay path refuses.
- The claim form shows the gap to preparers and payers as well ("ask an M-Pesa Manager to repair
  it") and hides Pay Merchants and Payees. Managers keep the Repair Reimbursed Total button.

**2. Staff are never paid as claim payees (controls)**
- Live, three payouts to staff got through:
  - the payer's own phone (the payer has no Employee record);
  - the claimant's second SIM;
  - the claimant's own bank account through KCB 522522 (HR-EXP-2026-00713; the Employee had no
    Bank A/C No).
- New staff destinations, refused on save, in a pasted list and at the send:
  - the Mobile No and Phone of every enabled System User, Employee or not; the session user's
    own number reads "your own phone";
  - an employee's ERPNext Bank Accounts (bank account number and IBAN).
- The name M-Pesa paid is checked against staff names: the claimant's, the payout's preparer's,
  its sender's (new `sent_by` on the line) and, while the new setting "Block Payees M-Pesa Paid
  Under an Employee's Name" is on (default on), every Employee's. Both names need at least two
  words.
  - After a payout: a match flags the line as a staff or self payment (`staff_check` Blocked,
    `staff_note`). A Comment on the claim, an Error Log and an alert go to the M-Pesa Managers,
    except whoever prepared or sent it and the claimant.
  - At the send: a name M-Pesa paid that number under before refuses the send. This covers other
    claim lines, B2C payouts to the phone, and B2B payouts to the till, or the paybill and
    account. The send is rolled back, so a job queued at once flags the line and alerts.
  - Bank accounts are left out (M-Pesa shows the bank's name).
- A blocked destination is refused for every claim payee until an M-Pesa Manager clears it with
  **Clear Staff Flag** on the claim (`claim_payees.clear_staff_flag`). The manager must be named
  and may not be the claimant or anyone who prepared or sent a payout to it; the reason is recorded
  on the lines and claims. A cleared destination is not flagged again.
- A bank-account line is "Not Verified" by name, never a match (new `name_verdict`: Matches, Does
  Not Match, Not Verified). M-Pesa names the bank, never who holds the account. A wrong bank still
  raises the alarm. The patch `claim_payee_name_verdict` gives lines paid earlier their verdict:
  bank lines that read as a match, like HR-EXP-2026-00713's, become Not Verified.
- The claim form shows a mismatch in red and a name not verified in orange.

**3. Texts that said maker-checker when it was off**
- New boot flag `maker_checker`. The pay dialogs and the "prepared" comment say "an M-Pesa Payer
  who did not prepare it sends it" only while maker-checker is on, otherwise "an M-Pesa Payer
  sends it". This covers Pay employee, Pay by M-Pesa, Pay Merchants and Payees and Pay Casual
  Payees.
- Pay employee (phone) no longer refuses while the claim's own payee batch is a draft. Its unpaid
  payee lines are already held back from the employee's share (`draft_b2c(..., payees=False)`).

**4. Visibility and privacy**
- The reimbursed-gap warning is shown to preparers and payers (item 1).
- Phones are masked (2547****0111, as the send summary shows them) in name-mismatch notes, comments,
  alerts and Error Logs, in the staff-flag messages, in settle comments and in send refusals.

**5. Small fixes**
- The migrate's alert about blocked payee accounts was never delivered (no Notification Log after
  the 12:42 migrate). It is now made in the migrate itself (`party_accounts.alert(now=True)`).
- A claim line paid to a supplier's approved account (MSA-00002-1) left the request's Supplier
  M-Pesa Account empty and the account's Last Paid On / Last Receiver Name unset. Once paid, the
  request names the account and the account records who M-Pesa paid; a mismatch blocks it, as
  for a supplier's own payout. A draft copy of such a request is cleared, not refused.
- `docs/TRIDENT_ROUND4_ROLLOUT.md` C3: each auto-booked Payment Entry keeps one Open "Cancel"
  Workflow Action in the Approved state, as hand-approved ones do. That is expected.

**6. Statuses: nothing stays Initiated, and part-paid shows**
- A B2C Payment Disbursement's status was set only by the 15-minute job, which gave up after four
  runs (`retry_count <= 3`). A batch whose last payouts finished later stayed Initiated.
  - Each payout's result now queues `b2c_batch_status.sync_status` after its commit. It locks the
    batch and sets its status from the rows.
  - The 15-minute job looks at every batch of the last 30 days that is not Paid, with no cap.
  - It also gives a row its request's result when the row was never told.
  - A batch is Partially Paid once a row is paid. One with failed rows and the rest still on their
    way stays Initiated (it read Partially Paid before, though nothing was paid).
- A B2B request whose Result never came was only checked by hand. The 5-minute sweep now asks
  Safaricom's Transaction Status about it, and the answer settles it as the Result would have:
  - Unknown after 5 minutes, Initiated after an hour;
  - once an hour, for 7 days.
- HRMS 15 has no Partially Paid for claims and advances. Their lists now show **Partly Paid** for a
  document HRMS shows Unpaid that was paid in part. HRMS 16 says Partially Paid itself.

**7. 80 mm thermal prints of what M-Pesa paid (asked during this round)**
- Nine standard Print Formats in module Mpsa Suite, "M-Pesa 80mm <DocType>". They work with
  Frappe's own Print, PDF (80 mm wide, no margins) and Email (attach the print). None is made the
  default print format: sites keep their own.
  - A **payment advice** for Purchase Invoice, Purchase Order, Expense Claim, Employee Advance and
    Salary Slip:
    - the party, total, paid and balance due;
    - every payment made against the document: M-Pesa B2B and B2C payouts (failed ones too) with
      the name and number M-Pesa paid, the receipt and the status, plus other Payment Entries;
    - for a claim, its merchant and casual payee lines (status, receipt, the name M-Pesa paid and
      the name verdict) and the claimant's own share.
  - A **proof of payment** for an Mpesa B2B Payment Request, a B2C Payment Disbursement (a block per
    row), a B2C Disbursement Request and a Payment Entry. Each payout reads like M-Pesa's
    confirmation SMS ("UJSSN5D5CE Confirmed. KES 50.00 paid to ... on 28/10/26 at 1:57 PM"). It also
    shows:
    - the shortcode it left from (Mpesa Settings);
    - the charge (the booked charge entry, else Safaricom's stated charge, else the B2C Charge Bands
      estimate, marked "est.");
    - the document it paid;
    - who prepared it and who sent it.
- All data comes from `money_out.payment_print`, registered as Jinja methods in `hooks.py`. The
  templates (`templates/print/mpsa_*_80mm.html`) read nothing themselves.
  - The reader's read permission is checked first.
  - A salary's amounts are hidden from whoever may not see salaries.
  - Phones are masked (2547****0111), and long account numbers too (****6789).
  - The name M-Pesa paid is split on its first " - " only.
  - A Payment Entry carrying a payout's receipt is printed once, as that payout.
  - Paid and balance come from the entries booked, not from a stored total: HRMS's count for a
    claim, the Payment Ledger for an invoice, allocations for an order or advance, Paid payouts
    for a slip.
- The look follows Astral's own Expense Claim 80 mm format (black title bar, boxed party, tables,
  amount box, balance due, status badge, signatures, print time). It is black and white only for
  thermal printers, 9 to 14 px, with the Letter Head image if one is set. It keeps nothing
  Astral-specific (no hard-coded shortcode, no custom fields).
- **Send Payment Proof** on a Paid (or Partially Paid) payout opens Frappe's email composer,
  addressed to the party (the supplier's email, or the employee's preferred, company or personal
  email) with the 80 mm proof selected. The user presses Send; nothing is sent automatically.

**Tests**
- Unit: 2925 pass (`python -m pytest tests/unit -q`), 76 more than 2849. New: `test_r41_claims.py`,
  `test_r41_employee_docs.py`, `test_r41_print.py`, `test_r41_js.py`; updated where behaviour changed
  on purpose (bank verdict, boot flags, the patch count).
- Bench: four new tests in `money_out/test_claim_payees.py`. One books two payouts of one claim in
  two connections, each its own committed transaction, the first held open while the second starts:
  Total Amount Reimbursed is 9000, not 7000. The others cover the gap's refusal and its repair job,
  a staff till flagged, blocked and cleared, and a batch's Partially Paid. Every 80 mm format is
  rendered for a paid document or payout through the print preview's own renderer
  (`testing.renders`, `printview.get_html_and_style`; `frappe.get_print` also passed on v15, while the
  throwaway v16 bench has no built desk assets for Frappe's print page): claim and
  payouts in `test_claim_payees`, invoice, order, B2B and Payment Entry in `test_b2b_documents`,
  advance in `test_employee_documents`, salary slip in `test_salary_slip_payouts`.
- Bench, full `run-tests --app mpsa_suite` on fresh v15 and v16 sites (ERPNext, payments, HRMS),
  2026-10-28, on the tree of this commit: **515 tests on each version, all pass, none skipped**
  (510 at 9a9c8ae plus 5 new). The race test was not run against the code before the fix; it is
  built so that the second booking starts while the first is held open, as on Trident.

**Deferred**
- A claim's casual payee paid by B2C to a supplier's approved phone records the account but does not
  name it on the B2C row (`payee_account`): that row field drives the employee phone lock.
- A B2B request still Initiated after 7 days of status queries is left to an M-Pesa Manager (Settle
  Unknown); no alert yet.
- Payment proofs are not emailed automatically after a payout. A follow-up could add a setting to
  send the proof to the supplier on Paid, once suppliers' email addresses are known to be right.
- The 80 mm formats were checked by rendering them (unit and bench tests), not yet printed on a
  thermal printer: print one of each on Trident's printer.

## Round 4: Send via M-Pesa, named payers, payee accounts, claims, merchants and salaries (2026-10-27, branch payouts-r4)

Plan: [../docs/PAYOUTS_R4_PLAN.md](../docs/PAYOUTS_R4_PLAN.md). Design: [docs/PAYOUTS_DESIGN.md](docs/PAYOUTS_DESIGN.md),
"Round 4". Roles and the user's decisions D5 to D9: [docs/ROLES_AND_PERMISSIONS.md](docs/ROLES_AND_PERMISSIONS.md).
Trident's upgrade and first live payments: [docs/TRIDENT_ROUND4_ROLLOUT.md](docs/TRIDENT_ROUND4_ROLLOUT.md).
What each work package changed, file by file: `docs/r4_notes/WP0.md` to `WP7.md`.

Runs on Frappe/ERPNext v15 and v16 with HRMS. **`bench migrate` is
needed**: new DocTypes, new fields, custom fields on HRMS DocTypes and changed DocType
permissions. No patch. **Read the UPGRADE part below before deploying**: after the migrate the
shared Administrator login and System Manager no longer send money or approve payee accounts.

**Only named people send money (decision 1)**
- **Send** a payout (B2B, B2C, reversals), use Pay Selected or retry a failed payout: M-Pesa Payer
  and M-Pesa Manager only (`PAYOUT_ROLES`).
- **Approve** a supplier or employee payee account, and the other M-Pesa admin work (C2B/Pull
  registration by hand, settling Unknown payouts, reconciling by hand, the Sandbox Lab): M-Pesa
  Manager only (`ADMIN_ROLES`).
- **Prepare** payouts: new `PREPARE_ROLES` = M-Pesa Manager, Accounts Manager, Accounts User,
  HR Manager, HR User, each within the rights it has on the payout DocType it makes.
- **System Manager configures only**: settings, Daraja secrets, the bank paybill list, Check
  Permissions. It reads payouts, schedules and Supplier M-Pesa Accounts but creates and changes
  none of them, has no access to Employee M-Pesa Accounts and no longer sees salary payouts.
- **The shared Administrator login** is refused ("Use your own login") for sends, approvals, Pay
  Selected, retries, Receiver Name Confirmed, Make Default, cancelling a payout that may have been
  paid and planning its line again. New setting "Administrator May Not Send Payouts or Approve
  Accounts", on by default and on after the migrate. Installs, migrations and scheduled jobs are
  not affected.
- **Maker-checker** now watches every field that decides who is paid, how much, from where and
  for which document, also covers reversals (whoever pressed Make Reversal does not send it) and
  no longer exempts Administrator.
- **Alerts**: changing a payout-safety setting is noted on M-Pesa Suite Settings and sent to the
  Notify Role (the role before the save and the one after it). The settings are maker-checker,
  the Administrator refusal, one-off B2B, the hold hours, the Integration User, the Notify Role,
  approved phones only, and the claim payee switch and caps.

**Send via M-Pesa replaces Submit (decision 2)**
- On a saved draft B2B request, B2C Payment Disbursement, B2C Disbursement Request or Mpesa
  Reversal Request, a payer sees **Send via M-Pesa** where Submit was. On a Supplier or Employee
  M-Pesa Account an M-Pesa Manager sees **Approve**. Other users see neither.
- The summary shows each receiver and destination (a phone masked as 2547****5678, a paybill and
  account, a till, or a bank, account and account holder), the amount, the charge estimate from
  the B2C Charge Bands, Pay From, who prepared and last changed it, and anything that would make
  the send fail. **Confirm** unlocks after 5 seconds.
- The server token is bound to the user, the document as saved, its amounts and its destination.
  It works from 5 seconds to 10 minutes, once.
- Every other way to submit is refused: the list's Actions > Submit (no longer offered on these six
  lists), REST, `frappe.client.submit`, `run_doc_method`, the form's own Submit. Pay Selected and
  the jobs of a disbursement already sent are deliberate steps of their own.

**Payee accounts: many destinations per party, one default (decision 4)**
- Supplier M-Pesa Account and Employee M-Pesa Account both take four types: **Phone (B2C)**,
  **Paybill** (with its account number), **Till (Buy Goods)** and **Bank Account (via Bank
  Paybill)**. One record per destination, each with its proof, a second approver and the hold
  period, as before. Employee's "Bank Paybill (B2B)" is renamed on migrate. Existing Paybill and
  Till accounts keep their type and stay payable (Trident's MSA-00001 and MSA-00002).
- A bank account: pick the bank from the list and type only the account number (letters and
  digits). The paybill and the name M-Pesa shows come from the bank's row. An account holder who
  is not the party gets a "third-party account" warning.
- A bank's paybill typed as a plain Paybill (KCB, Equity, Co-op...) is refused: "pick Bank Account".
  The exception is a business's collection reference (`code#reference`), which stays a Paybill with
  a warning.
- A phone that does not start like a Safaricom line is warned, not refused (ported numbers keep
  their prefix). A paybill or till has 5 to 7 digits and is never the company's own shortcode.
- The same destination twice:
  - refused for the same party, and for two employees;
  - between a supplier and an employee: refused for a phone or bank account (the ghost-vendor
    pattern), warned for a paybill or till;
  - for two suppliers: warned.
  An employee is named in these messages only to people who may see salaries.
- **One default per party.** With two or more approved accounts one must be the default: the
  Supplier or Employee form shows a red headline, and payroll leaves such an employee out.
  **Make Default** (a button, logged with a Version and a Comment) replaces ticking the box after
  approval. Supplier: M-Pesa Manager, Accounts Manager, Accounts User, Purchase Manager. Employee:
  M-Pesa Manager, HR Manager. A named login either way. A blocked account cannot be the default.
- **At payment time** the payer may pick any other approved account of the right kind. B2B (to a
  supplier or an employee) pays a Paybill, Till or Bank Account; B2C pays a Phone. Payroll pays
  each employee to their default of any type (a phone by B2C, the others by B2B).
- **After payment** the number in front of the name M-Pesa shows must be the account's.
  - A bank account is checked against its bank's Safaricom name. Only KCB's ships ("KCB Paybill
    AC"). For other banks, the name the first payment shows is kept on the bank's row and M-Pesa
    Managers are asked to confirm it.
  - Tills are checked from the Transaction Status answer (F2.4).
  - A mismatch blocks the account until an M-Pesa Manager presses Receiver Name Confirmed.
- A save after approval can change neither the default nor a block.

**The bank paybill list**
- New DocType **M-Pesa Bank Paybill** (M-Pesa workspace). Each row has the institution, its type,
  the paybill, how the account number is written, an optional account-number pattern, the name
  M-Pesa shows, enabled, the source and the date verified. Changes are tracked. System Manager and
  M-Pesa Manager edit it. Accounts, HR, Purchase Manager, M-Pesa Payer and M-Pesa Viewer read it.
  A shipped row cannot be deleted (disable it instead).
- **Shipped: 43 of the top 50 Kenyan institutions**: commercial banks, Postbank, the mortgage
  finance institution and the largest microfinance banks. Each number was found by two
  independent searches; the 7 with no reliable paybill are left out.
  - 39 rows ship enabled. Four medium-confidence rows ship disabled: HBZ, Sumac MFB, Choice MFB
    and Kingdom Bank.
  - Eight banks ship an account-number pattern taken from their own wording: Family 12 digits,
    Paramount 12, Access 13, I&M 14, M-Oriental 13, Premier 10, Standard Chartered 13, CIB 10.
- Every migrate adds missing rows. It changes a field only while that field still holds what the
  previous release shipped. An admin's edit is kept, with a comment saying what the release ships.
- **If a bank's paybill changes** (an admin's edit or a release), every approved account that
  copied the old number is blocked until it is cancelled, amended and approved again. The Notify
  Role is told how many accounts are blocked. Changing the number back unblocks them. An approval
  is refused if the bank's paybill changed after the draft was saved. Money never follows an edit
  without a second approval.

**Expense Claims and Employee Advances paid by M-Pesa (B2C and B2B)**
- A submitted, approved claim (or a submitted advance) that still owes shows the headline
  "M-Pesa: paid · in flight · left". Under M-Pesa, two buttons make drafts:
  - **Pay employee (phone)**: a draft B2C Payment Disbursement to the employee's approved phone;
  - **Pay employee (bank / paybill / till)**: a draft B2B request to one of the employee's approved
    B2B accounts, with the default preselected.
- **Actions > Pay by M-Pesa** on the claim and advance lists (up to 200 ticked documents) makes one
  B2C disbursement for the documents paid by phone and one B2B request for each document paid to
  a bank, paybill or till. Documents it cannot pay are listed with the reason.
- **What a claim owes** is HRMS's own outstanding: grand total less what was reimbursed, with its
  advances taken off. A claim its advances cover now owes 0. An advance owes its amount less what
  was paid. Every payout of the document still on its way counts against it (B2C, B2B, draft
  entries, schedule lines), under one lock shared by every path.
- **Payable only when** the document is submitted and Approved, is not Is Paid, has a payable
  (advance) account and is in KES. A site that runs a Workflow on the DocType can also list the
  states that may be paid ("Expense Claim Workflow States That May Be Paid").
- **B2B for a claim or advance** is booked as a Payment Entry that HRMS accepts. The party is the
  employee and the account is the claim's own payable account (or the advance account). The entry
  references the claim, is paid from the Working GL and carries the claim's project and cost
  center. The claim then shows Paid. A Payer may send it, because the document caps it.
- **Nobody pays their own claim or advance.** A B2C row for a claim pays the claim's own employee
  only. A B2C Disbursement Request made by hand cannot pay a claim or advance. A retry checks the
  claim, the phone and the sender again.
- **"Pay Employees by B2C Only to Approved Phones"** (new setting): while on, B2C to an employee
  goes only to an approved phone account, never to a typed number or the Employee's Cell Number. It
  is on for a new site and stays off on an upgrade until the site switches it on.
- A claim or advance with a payout paid or on its way cannot be cancelled.
- **Not salary data (decision 5).** Claim and advance payouts are shown, by the payout DocType's
  rights, to whoever reads Expense Claims (or Employee Advances) through a role other than
  Employee. Everyone else sees them no more than salaries.

**Merchants and casual payees paid straight from a claim ("petty cash without cash")**
- **Off until switched on.** An M-Pesa Manager switches on "Allow Paying Merchants and Casual
  Payees from Expense Claims". HR ticks "Paid to Merchants or Casual Payees by M-Pesa" on each
  Expense Claim Type that may be paid this way.
- **The claim gets an M-Pesa Payees table.** Each line has:
  - Pay To: Till, Paybill and account number, Bank Account at a listed bank, or Phone;
  - the payee's name as the claimant gives it;
  - the amount;
  - the expense line it pays.

  **Paste Payees** takes rows of name, phone and amount, or a CSV / XLSX file.
- **Checks on every save and at approval:**
  - the lines of an expense line add up to no more than its sanctioned amount, and all the lines
    to no more than the claim owes (the rest is reimbursed to the employee as usual);
  - a payee is never an employee's phone, till, paybill or bank account, and an employee's phone is
    never used as a paybill or bank account number;
  - amounts are whole shillings, within the caps.
- **Caps (decision 3)**: "Most per Payment to a Payee That Is Not an Approved Account" and "Most
  per Day to Such Payees, per Company", both 0 = no cap, the default. A supplier's approved account
  is not capped.
- **Who does what:**
  1. The claimant (or HR / Accounts) lists the payees.
  2. The expense approver approves them with the claim.
  3. A preparer presses **Pay Merchants and Payees**: one draft B2B request per merchant line, one
     B2C disbursement for all the phone lines.
  4. An M-Pesa Payer who did not prepare them sends each with Send via M-Pesa.

  The claim list's **Pay Casual Payees by M-Pesa** puts the phone lines of several claims in one
  batch. **Retry Failed Payees** prepares the failed lines again. A line is never paid twice.
- **Books**: each payout is a reimbursement of the claim (a Payment Entry to the claimant on the
  claim's payable account), the money having gone to the merchant.
- **Each line** shows its status, receipt and the name M-Pesa paid. A name that does not match is
  commented on the claim and alerted.
  - A till, paybill or bank account paid 3 times from claims (setting "Suggest an Approved
    Supplier Account After...") is suggested as a Supplier M-Pesa Account.
  - **Repair Reimbursed Total** (M-Pesa Manager) runs HRMS's own sum again.

**Salaries from Salary Slips: one-off and selected slips**
- **One salary**: a submitted Salary Slip shows "M-Pesa: paid · in flight · planned · left". **Pay
  by M-Pesa** (a one-off or off-cycle salary) offers any approved account of the employee (the
  default preselected), Pay From and Pay On, and makes a one-line M-Pesa Payment Schedule.
- **Several salaries**: the Salary Slip list's Actions > **Pay Selected Slips by M-Pesa** makes
  one schedule with a line per slip to its employee's default account (up to 500 slips, one
  company). Each slip left out is listed with its reason.
- **Paying them**: an M-Pesa Payer who did not prepare the lines pays them with Pay Selected on
  M-Pesa Payments Due. Never before Pay On, with the 5-second confirm. They are booked as payroll
  schedules are: the salary Journal Entry clears the payroll payable. A salary planned on one
  schedule is never planned on a second one.
- **Who prepares**: a `PREPARE_ROLES` user who may see salaries and create schedules. In practice
  that is an Accounts User or Manager who also holds HR User or HR Manager. A user who may not
  read Employee M-Pesa Accounts is offered the default account only, its number masked.
- Payroll Entry shows a headline with what M-Pesa paid, has in flight and planned, and its
  schedules. Its own Create M-Pesa Payment Schedule now also needs salary visibility.
- An off-cycle salary still needs a Payroll Entry: run a one-employee Payroll Entry, then Pay by
  M-Pesa on its slip.

**Buttons and Connections**
- **Connections**: every form the suite pays from or collects for lists its M-Pesa records under
  "M-Pesa":
  - HR: Expense Claim, Employee Advance, Salary Slip, Payroll Entry, Employee;
  - buying: Supplier, Purchase Invoice and Order;
  - selling: Sales Order and Invoice, POS Invoice, Customer, Payment Request;
  - accounts: Payment Entry, Journal Entry, Bank Transaction, Bank Account;
  - the suite's own forms: Mpesa Settings, the payouts, the C2B register, Express Request, Payment
    Intent, the payee accounts and the schedules.

  Who may not see salaries gets no count of salary records.
- **Buttons show only to people the server accepts** (`frappe.boot.mpsa` flags):
  - STK and C2B buttons: collectors;
  - Retry: payers;
  - Reverse Payment and Check Status: payers and M-Pesa Managers;
  - the prepare buttons on Purchase Invoice and Order: `PREPARE_ROLES`, so System Manager and a
    payer without a preparing role no longer see them.
- Purchase Invoice and Order: **Add to M-Pesa Payment Schedule**, and the headline "M-Pesa: KES x on
  its way, KES y planned on a payment schedule. Left to pay: KES z."
- List indicators on B2C disbursements and requests, reversals and the C2B register. Customer:
  "M-Pesa received in 90 days".
- B2B request: **Check Status** on Paid, Initiated and Unknown requests. Make Entry offers the
  Expense Claim or Employee Advance it paid.

**Request Payment from Sales Orders and Sales Invoices**
- A submitted Sales Order (not invoiced, even in part) or a non-POS Sales Invoice gets M-Pesa >
  **Request Payment**. The dialog takes the customer's phone, the amount still owed (it may be
  lowered) and the shortcode. It makes and submits ERPNext's own Payment Request (Phone channel),
  which prompts the phone.
- The server checks:
  - COLLECT_ROLES, read on the document, and create on Payment Request (Accounts User and Manager
    by default);
  - KES, and no more than is owed less the open requests, and no more than the shortcode's
    Transaction Limit;
  - a Kenyan mobile number;
  - at most 5 prompts a minute per user, no new prompt while the document's last one is under 90
    seconds old, and one prompt at a time per document.
- Payment Request: **Resend M-Pesa Prompt** and **Check M-Pesa Status**.

**Trident live-test fixes** ([../docs/TRIDENT_LIVE_TEST_HANDOVER.md](../docs/TRIDENT_LIVE_TEST_HANDOVER.md))

| # | What changed |
|---|---|
| F1 | The STK booking and the C2B auto-submit run as Administrator, as the B2B and B2C bookings do. An Integration User hidden from a project no longer stops a booking. |
| F2.1 | A till Result's `{Amount=…BasicAmount=…}` balance is read as the Working balance. |
| F2.2 | A charge Safaricom does not state is taken from the fall in the Working balance, once no status query can state it. Otherwise Mpesa Match Log gets a "Charge not stated" row. |
| F2.3 | A Result with no completed time is dated when it arrived, flagged "estimated" until the status answer. |
| F2.4 | A B2B Result that lacks the receiver or the charge gets a Transaction Status query, asked again by a 5-minute sweep and by Check Status. The answer fills the receiver, charge and time, runs the name check (tills too) and makes the charge line. It also settles an Initiated or Unknown request. |
| F3, F6 | Named payers, Send via M-Pesa and System Manager configuration only (above). F6's role clean-up is a rollout step. |
| F4 | A B2C row whose document owes nothing is refused at save. |
| F5 | One cost-center rule for every payout entry: the paid document's, then Mpesa Settings', then the company default. B2C and B2B entries copy the document's project and dimensions. Both rows of a charge Journal Entry go on the shortcode's cost center; salary Journal Entries on the Payroll Entry's. |
| F7 | Transaction Limit and Minimum B2C Payout cap every B2C row, at save and at send. |
| F8 | No change: the charge Safaricom states is used, never a fixed figure. |
| F9, F10 | Setup and runbook notes in [docs/OPERATIONS_NOTES.md](docs/OPERATIONS_NOTES.md). |
| F11 | Check Transaction Status on Mpesa Settings answers payout receipts with the payout's status, receiver, charge and time, and the dialog closes. Money the ERP did not send is flagged. A salary receipt is described only to who may see salaries. |
| F12 | Each balance snapshot compares each sub-account's movement with what the ERP recorded: Agrees, Gap (alerted) or Explained. A Reserved amount is shown and alerted. |

**Desk fixes**
- Request Payment in the C2B dialog no longer throws on the invoice desk.
- Express Request: the undefined `msg` after a re-prompt is gone. Express Request and Mpesa
  Settings no longer add a realtime listener on every refresh.
- The status event Python publishes is now the one the desk listens for.
- B2C disbursement: Paid To is set for DocTypes without their own default account, and "Less Than
  Amount" is sent.
- Sales Invoice's Fetch Mpesa Payments lists only the company's draft receipts of that customer.

**Settings added** (M-Pesa Suite Settings, Money Out tab)

| Setting | Default | What it does |
|---|---|---|
| Administrator May Not Send Payouts or Approve Accounts | On (also after the migrate) | Sends, approvals, Pay Selected, retries, Receiver Name Confirmed and Make Default need a person's own login. |
| Pay Employees by B2C Only to Approved Phones | Off on an upgrade, on for a new site | B2C to an employee only to an approved phone account. |
| Expense Claim Workflow States That May Be Paid | Blank (any submitted, approved claim) | One state per line, for sites that run a Workflow on Expense Claim (or Employee Advance). |
| Allow Paying Merchants and Casual Payees from Expense Claims | Off | The M-Pesa Payees table on claims. |
| Most per Payment to a Payee That Is Not an Approved Account | 0 = no cap | Per payee line. |
| Most per Day to Such Payees, per Company | 0 = no cap | Per company and day. |
| Suggest an Approved Supplier Account After This Many Payments to the Same Till/Paybill | 3 (0 = never) | Suggests turning a merchant paid often into a supplier account. |

Also new: "Paid to Merchants or Casual Payees by M-Pesa" on Expense Claim Type (off), and the
DocTypes M-Pesa Bank Paybill and M-Pesa Claim Payee (the claim's table).

Existing settings that do more now:
- Transaction Limit and Minimum B2C Payout: every B2C row (F7).
- Notify Role: the round 4 alerts.
- Mpesa Settings' Cost Center: payout entries (F5).

**Behaviour changes users will notice**
- **Who sends:**
  - System Manager and the shared Administrator login no longer send, approve, Pay Selected or
    retry. System Manager no longer sees salary payouts or Employee M-Pesa Accounts.
  - Accounts and HR prepare; M-Pesa Payers send; M-Pesa Managers approve accounts.
- **How payouts are sent:**
  - Payouts are sent with Send via M-Pesa (summary, Confirm after 5 seconds), payee accounts
    approved with Approve.
  - The list's Actions > Submit is gone from these lists. Scripts and integrations that submitted
    payouts are refused.
- **Entries are posted at once:** every Payment and Journal Entry the suite makes for a payout,
  its charge or a statement transfer is submitted when it is made, and under a Payment Entry
  workflow it lands in the workflow's approved state (Frappe sets it on submit). Nobody approves
  M-Pesa entries: the money has left already. "Submit B2C Accounting Entries" (Mpesa Settings) and
  "Submit Transfer and Funding Entries" (M-Pesa Suite Settings) are always on and read-only. Make
  Entry posts too, so it is for a user who may submit the entry (the button is hidden from others).
  Each entry is inserted already submitted, in one save, so the workflow's Draft state never runs:
  no Workflow Action is left Open for its approvers and no Draft email goes out. A payout paid on a
  day in a closed Accounting Period, in books closed by a Period Closing Voucher, or on or before
  Accounts Frozen Till cannot be booked on that day: Make Entry takes a later Posting Date (B2B),
  and a B2C payout's alert says to make its entry by hand with the receipt, which the suite counts.
- **Payee accounts:** a bank account is picked from the bank list; the default changes only with
  Make Default; a party with several approved accounts and no default shows red and is left out of
  payroll.
- **Claims and advances:**
  - Claims and advances get M-Pesa buttons. A claim covered by its advances owes nothing.
  - Nobody pays their own claim, and a claim with a payout on its way cannot be cancelled.
  - Accounts users who do not read Expense Claims no longer see claim payouts.
- **B2C:**
  - A row above the Transaction Limit, below the Minimum B2C Payout or for a document that owes
    nothing is refused at save. On a site whose limit was lowered for a test (Trident: 100), raise
    it.
  - A B2C draft saved before the upgrade is refused once with "was changed while being sent" (its
    approved account is filled at the first send). Save it again, then another person sends it.
- **Entries (F5):** B2C Payment Entries now book to the paid document's cost center (or the
  shortcode's) and carry its project, not the company default. Charge Journal Entries put both
  rows on the shortcode's cost center. Tell the accountant.
- **Tills:** the receiver, charge and completed time appear shortly after the Result. A wrong till
  now blocks its account, as a wrong paybill does.
- **Balances:** snapshots show Agrees, Gap or Explained. A Gap alerts the Notify Role.
- **Buttons** are hidden from people the server would refuse.
- **Payroll:** preparing a payroll schedule needs salary visibility (an HR role, or read on Salary
  Slip).

**UPGRADE**

*Before migrating*
1. **Back up** the site (database and files) and keep the download.
2. **Give named people the roles, before the upgrade.** After the migrate neither the Administrator
   login nor System Manager can send or approve. Each person who sends gets their own login and
   M-Pesa Payer. At least one person gets M-Pesa Manager to approve payee accounts, and that should
   not be the payer. Preparers keep Accounts User or Manager, or HR User or Manager. Give the
   people who prepare salary schedules an HR role as well as their Accounts role. Write the list
   down. A site still on Navari's original `frappe_mpsa_payments` (Astral) has no M-Pesa roles
   until the suite is installed: see *Astral* below.
3. **Claims and advances need read.** Whoever prepares payouts of claims or advances needs read
   on Expense Claim and Employee Advance. HRMS gives that read only to HR and Expense Approver
   roles (advances: Expense Approver only). Since the final pass HR User and HR Manager may
   prepare claim payouts, and Expense Approver is a preparer (it is the only default role that
   reads advances). Expense Approvers then also see supplier payouts on the payout lists, never
   salaries: decide whether that suits the site. An Accounts-only preparer still needs read on
   claims (Role Permission Manager).
4. **Scripts and drafts.** Find the Client Scripts, Server Scripts and integrations that submit
   payouts (for example one that inserts and submits a B2C Payment Disbursement in one call): they
   are refused and must use Send via M-Pesa. Also disable scheduled scripts that book Payment
   Entries for paid payouts or change payout statuses: the suite does both, and two bookers can
   make a second Payment Entry for one receipt. A B2C draft saved before the upgrade must be saved
   once more before it is sent. Pause payouts for the deploy window if drafts may be in flight.
5. **Note the site's own permission rules** (Custom DocPerm) on the M-Pesa DocTypes. A role such
   as "Super admin" that you want to keep must be added back after the reset.

*Deploying*
1. A site still on the old app name `frappe_mpsa_payments` does its rename hand-over first
   ([docs/RENAME_SPIKE.md](docs/RENAME_SPIKE.md)).
2. Deploy this branch and run the migrate. On Frappe Cloud the deploy runs it.
3. **Clear the cache**: `bench --site <site> clear-cache`, or the site's Clear Cache action on Frappe
   Cloud. Connections are cached per DocType. Then everyone reloads the desk, and logs out and in
   if their roles changed.

*After migrating*
1. **M-Pesa Suite Settings > Permissions > Check Permissions, then Reset, before anything else**:
   read the differences, then **Reset** the M-Pesa DocTypes it lists. While a DocType has site
   rules, Frappe uses only those rules, so the round 4 roles (M-Pesa Payer, M-Pesa Manager, the new
   preparers) have no rights on it until the Reset, and the server refuses the old roles: on
   Trident and Astral nobody can send a B2C payout, and the M-Pesa Manager cannot open Mpesa
   Settings, until then. Add back a site role only if it is still needed: one re-added with its old
   rights can bring back rights the server then refuses, or a delete right.
2. **Assign the roles** from step 2 if not done yet. Take the M-Pesa roles off shared or service
   logins: the Integration User needs only what
   [docs/ROLES_AND_PERMISSIONS.md](docs/ROLES_AND_PERMISSIONS.md) section 7 names.
   **Redo blocked accounts:** the migrate blocks every approved Supplier or Employee M-Pesa
   Account that was approved by its own maker, or with the Administrator login even when a named
   user entered it (comment on each, one alert per DocType to the Notify Role: check that the
   M-Pesa Manager receives it). The lists show them red. A blocked account is neither paid nor made
   the default: cancel, amend and have a named M-Pesa Manager approve each one, then wait its hold
   (Trident: MSA-00001 and MSA-00002).
3. **Raise the Transaction Limit** on each Mpesa Settings to the real value (B2C is now capped by it
   at save and at send). Set the hold hours back to 24 if they were lowered for a test.
3a. **Entries post at once.** The migrate switches "Submit B2C Accounting Entries" on, on every
   Mpesa Settings, and "Submit Transfer and Funding Entries" on. Tell the accountant and the
   Payment Entry workflow's approvers: M-Pesa entries no longer arrive as drafts for review. Drafts
   the suite made before the upgrade stay drafts: submit or approve them. An entry that cannot be
   submitted is not left as a draft: its payout shows why, the sweep tries again, and Make Entry
   books it (for users who may submit Payment Entries; others do not see the button). The Notify
   Role (M-Pesa Suite Settings) is alerted once per payout and reason, B2C payouts too.
   A payout paid on a day that is in a closed Accounting Period, or on or before Accounts Frozen
   Till (which binds Administrator, whom the automatic booking runs as), cannot be booked on that
   day:
   - B2B: its note says so; press Make Entry and give a Posting Date in an open period. The entry
     keeps the payout's day as its Reference Date, and its remark says "Paid on X, posted on Y."
   - B2C (no Make Entry): the payout's accounting error and alert say so; make its entry by hand,
     dated in an open period, with the M-Pesa receipt as Reference No: a Payment Entry allocated to
     the payout's document, or for a salary a Journal Entry with the receipt as Reference Number and
     the employee as party on the payroll payable row. The suite counts it as the payout's entry and
     marks the payout booked on its next pass.
   - The charge's Journal Entry waits too (the payout's own statement line is kept; a Mpesa Match
     Log says why): make it by hand with Reference Number CHG-<receipt>, dated in an open period,
     and the suite uses it; or reopen the period and the sweep books it.
4. **Approve employees' phone accounts**: HR enters each Employee M-Pesa Account with its proof, an
   M-Pesa Manager approves it, and it is payable after the hold (24 hours by default).
5. **Decide "Pay Employees by B2C Only to Approved Phones"**: switch it on once the employees paid
   by B2C have approved phones. Until then B2C to an employee may use the Employee's Cell Number.
   It is switched on by the install only on a new site with no payout history; an upgraded or
   renamed site keeps it off until you decide.
5a. **Payroll: one way per Payroll Entry.** HRMS's Make Bank Entry (and any Bank or Cash Entry on
   a Payroll Entry) is refused while M-Pesa salaries of that payroll are paid, on their way or
   planned. Pay a payroll through M-Pesa or through the bank, or split it into separate Payroll
   Entries. Tell HR: the button still shows, and the refusal comes when it is pressed.
6. **Check the bank paybill list** for the banks your staff and suppliers use:
   - the paybill numbers;
   - the eight account-number patterns (Family, Paramount, Access, I&M, M-Oriental, Premier,
     Standard Chartered, CIB) against real account numbers of those banks, since a wrong pattern
     refuses valid accounts;
   - the four disabled rows, which you enable only after checking them with the bank.
7. **Defaults**: on each supplier or employee with two or more approved accounts, press Make
   Default on one.
8. **Claims**:
   - set the workflow states that may be paid, if the site runs a Workflow on claims: list every
     submitted state in which claims are paid today (Astral pays in "Pending Attachment" and in
     "Submitted");
   - review the old approved, unpaid claims and advances: after the upgrade each one shows
     "M-Pesa: ... left" and the Pay buttons. Close, cancel or mark what was paid outside the ERP,
     so nobody pays history;
   - decide merchant and casual payees (the switch, the Expense Claim Types, the caps);
   - tell staff to save claims they want paid by M-Pesa as not Is Paid.
9. **Cost centers (F5)**: tell the accountant which cost center payout entries now use (the paid
   document's, then the shortcode's, then the company default). There is no setting yet to put
   every M-Pesa entry on the shortcode's cost center.
10. **First payments**: small amounts, one route at a time, with the checks in
    [docs/TRIDENT_ROUND4_ROLLOUT.md](docs/TRIDENT_ROUND4_ROLLOUT.md) (generic apart from Trident's
    names).

*Astral (still on Navari's `frappe_mpsa_payments`)*

A read-only pre-flight of the live site (28 Oct 2026; `docs/ROUND4_PREFLIGHT_ASTRAL.md` in the
folder above this repository) found that Astral cannot follow the Trident checklist as it is:
1. **Three steps, not one.** Install the suite under the old name first (Stage A), give M-Pesa
   Payer and M-Pesa Manager to named people (the roles exist only from then), rename (Stage B),
   then deploy round 4. Deployed in one window, nobody can send between the migrate and the moment
   the roles are given and the Reset is done: pause payouts for the window.
2. **Astral's own Pay buttons stop.** Eight Client Scripts call three API Server Scripts
   (`astral_b2c_quick_pay`, `astral_b2c_bulk_pay`, `astral_casual_bulk_pay`) that insert and submit
   a B2C Payment Disbursement in one call; every payout of the last 30 days went this way. Round 4
   refuses that submit: disable them at the deploy and show staff the suite's buttons.
3. **Two scheduled scripts duplicate the suite:** "AIL B2C - Payment Entry Backstop" submits Payment
   Entries for paid payouts, and "AIL B2C - Payout Watchdog" changes stuck payouts' status. Disable
   both at the deploy. `astral_casual_cancel_failed` cancels B2C documents itself: retire it with
   Astral's casual flow.
4. **Two people per payout.** About 18 payouts a day, each made and sent today by the same person,
   now need a second named M-Pesa Payer (maker-checker is on after the migrate).
5. **B2C User stops sending at the migrate.** Astral's site rules give B2C User (10 people) the only
   create, submit and cancel rights on the two B2C DocTypes. Reset them straight after the migrate,
   give M-Pesa Payer to those who keep paying, and do not add B2C User back.
6. **Claims are read through Astral's own roles** (Admin, Super Admin, the Expense Approver,
   Expense Creator and Expense User roles, Payment User), not HRMS's: HR roles and Expense
   Approver do not read claims there. Check the chosen preparers against that.
7. **Workflow states:** list `Pending Attachment` and `Submitted` (step 8).
8. **Old claims and advances:** 347 claims and 367 advances, most from before 21 Jul 2026, will show
   the pay buttons: review them before the deploy (step 8).
9. **Cost center:** the "AIL Mpesa Feed - PE Cost Center Autofill" Server Script fills Payment Entry
   cost centers; the suite now sets them from the paid document. Decide whether it stays.
10. Do not send the stale draft request B2C-26-08-000015: its disbursement is already Paid.

### Round 4 final review (42 findings, fixed in r4-final-a/b/c)

Six reviewers traced each flow end to end on the merged branch (claims, merchant and casual
payees, salaries, payee accounts, roles, the Trident findings) against the Frappe, ERPNext and HRMS
sources of v15 and v16. Four findings were high, 19 medium and 19 low. What users notice:

- **Claims and advances:**
  - A claim's approved merchant and casual payee lines that are not paid yet are held back from the
    employee's share: Pay employee, the list's Pay by M-Pesa, B2B to the employee and the B2C send
    and retry. The headline shows them "for payees".
  - The B2C send and retry take off draft Payment and Journal Entries on the claim or advance, as
    Prepare and B2B already did. A claim can no longer be paid by M-Pesa while a bank payment of it
    waits in draft.
  - Failed payouts and draft B2B requests no longer block cancelling a claim or advance. Paid
    payouts and payouts on their way still do.
  - A payee line's payout must pay the whole line. A Paybill payee that is a bank's paybill, or
    whose account number is an employee's bank account number, is refused.
  - The Administrator login cannot approve a claim that has payee lines.
  - A B2C payout the claim can no longer take in full is booked with the claim taking what it
    still owes; the rest stays unallocated with a comment and an alert (before, the booking failed
    for 30 days).
  - Claim and advance Connections now count their payouts for who reads them by a role other than
    Employee, without salary visibility. Status queries of a claim's B2B payout follow the same rule.
- **Roles:** HR User and HR Manager prepare (never send) claim payouts on both routes. Expense
  Approver is a preparer and the only default role that can prepare an advance's payout; it sees
  supplier, claim and advance payouts on the payout lists, never salaries.
- **Salaries:**
  - Get References for Salary Slips, saving a disbursement with slip rows, and B2B to an employee
    for a slip or for no document need salary visibility and read on the slip or Payroll Entry.
  - A hand-made B2C request cannot pay a Salary Slip; one to an employee with no document is sent by
    an M-Pesa Manager only.
  - A user who may not see salaries cannot add, change or remove a salary line on a schedule.
  - HRMS's Make Bank Entry is refused for a payroll that M-Pesa pays (UPGRADE step 5a).
  - Versions of salary records and of every Employee M-Pesa Account are hidden from who may not
    see salaries, System Manager included.
  - A schedule's salary Journal Entry puts one cost center on both rows (the Payroll Entry's, else
    the shortcode's, else the company default).
- **Payee accounts:**
  - An approved account can be cancelled after it was paid; Amend starts unblocked.
  - A request that names no account pays the party's default only. A default on hold or blocked
    refuses the request with that reason instead of paying another account.
  - The migrate blocks accounts approved by their maker or with Administrator (UPGRADE, after
    migrating, step 2).
  - Supplier B2C rows pay only the supplier's approved phone account when it has one; Get
    References fills in the default, and the receiver name is checked against it.
  - Approve lists "Check before you confirm" warnings: a third party's bank account, an employee
    paid to a business paybill or till.
  - The account lists show Blocked (red), Default (green) and Approved (blue).
- **Send via M-Pesa:**
  - A claim payee's payout shows the payee as Receiver, the claim and line, and the bank's name.
  - The dialog refuses up front, with the reason, where the server would refuse after the countdown
    (self-pay, no-document employee B2B by a non-manager, payees switched off, payee caps).
- **Make Entry** needs a named M-Pesa Manager to book a request paid after cancel, or to move a
  paid payout to another document. A payee line's or schedule line's payout is never moved.
- **Reversals:** an Unknown reversal is cancelled only by a named M-Pesa Manager. A new reversal of
  a receipt whose cancelled reversal was later answered Reversed is refused.
- **Payment Requests:** a prompt that is paid but not yet booked is never resent or superseded; the
  form says it is being booked.
- **Balances:** C2B receipts the ERP reversed no longer show as a Gap. A payout counts as in flight
  only within 24 hours of the balance.
- **Settings:** changing the claim workflow states that may be paid leaves a comment and alerts the
  Notify Role. With those states set, an advance under its own workflow is gated only by the listed
  states its workflow has.

### Round 4 verification (15 findings, fixed in r4-verify-counting/reads/sends)

The final pass was itself reviewed (six reviewers and a completeness critic, every finding checked by
independent skeptics): 19 findings, 15 confirmed (6 medium, 9 low), 4 refuted. Notes per fix:
docs/r4_notes/VERIFY_COUNTING.md, VERIFY_READS.md, VERIFY_SENDS.md. What users notice:

- **Claims and advances:**
  - A B2C claim or advance payout booked in part keeps its unallocated rest counted, as a B2B one
    does. Cancelling the other (bank) entry no longer makes that money look owed again.
  - **Settle Payee Outside M-Pesa** (named M-Pesa Manager, not on their own claim): a Draft or
    Failed payee line of a submitted claim that was paid another way is Cancelled with the reason
    and a comment. Its amount is the employee's again, and M-Pesa never pays it.
  - A claim is not cancelled while a payee line's payout may have paid, a cancelled Unknown request
    included. Cancel on a claim or advance no longer lists its M-Pesa payouts in Frappe's Cancel
    All dialog, so one whose payouts all failed is cancelled from the form.
- **Phones:** an employee with approved phones is paid by B2C only to the one that can be paid now,
  also while "Pay Employees by B2C Only to Approved Phones" is off: a default on hold or blocked is
  never passed over for the Cell Number, and that number typed in by hand is refused too.
- **Suppliers:** a hand-made B2C request, and a Retry, pay a supplier with approved phones only to
  one of them that can be paid now; a cancelled account (a stolen SIM) stops the retry. A
  hand-made B2C request must pay exactly what its disbursement row says (party, number, amount,
  document).
- **Reads:**
  - A B2B draft names an invoice, order, claim or advance only for who may read it, when the link
    is new or changed; saving or amending a colleague's request is not refused. This includes M-Pesa
    Payers: one who prepares a request for a Purchase Invoice needs read on it (an Accounts role,
    or the invoice shared with them).
  - Get References on a B2C disbursement needs read on what it lists and lists only the documents
    the user may read: an Expense Approver no longer sees every claim and advance with phones.
  - Salary Slip rows are refused before the slip is read, so net pay cannot be probed.
- **Accounts:** a bank paybill changed back no longer unblocks an account approved by its maker or
  with Administrator.
- **Payroll:** reversing M-Pesa's own B2B salary entry saves; a bank entry that pays the payroll is
  still refused.
- **Send via M-Pesa:** the payee day cap is counted the same way in the summary and at the send; a
  hand-made B2C request to an employee gives an M-Pesa Payer no token (manager only), with the
  reason.
- **Payment Requests:** the form shows "being booked" only for prompts the server counts as paid and
  not booked, so a reversed or C2B-booked prompt no longer hides Resend.

### M-Pesa entries posted at once (2026-10-28)

The suite's entries are never drafts for review. A payout's Payment Entry (B2C or B2B, claims and
advances included), its salary or charge Journal Entry, and a statement import's transfer entry are
submitted when they are made; under a Payment Entry workflow, Frappe puts the entry in the
workflow's first submitted state (Approved on Trident). The two switches that used to leave them as
drafts are read-only and always on: each settings form keeps them on, and every migrate switches
them on (`migrate.post_entries_at_once`). Make Entry posts the entry it makes, so a user who may not
submit it is refused instead of getting a draft, and the form hides the button from them. Drafts
made before stay as they are: the suite still counts them and waits for staff to submit them.

After the review of 437c33e:
- **One save, submitted.** Every entry the suite posts goes through `accounting.posting.insert_entry`:
  the draft's save methods run on it first (before_validate, validate, before_save, so ERPNext's
  draft-only steps still run: a Payment Entry's advance account when the company books advances
  separately, a Journal Entry's against accounts), then it is inserted with docstatus 1, which
  Frappe runs as a submit and saves straight in the workflow's approved state. An insert then a
  submit ran the workflow's Draft state on the insert: an Open Workflow Action for the Draft
  state's approvers that the submit never completed, and the Draft state's email. The sites: the
  B2B booking and Make Entry, the B2C payouts' Payment Entries (`api.payment_entry`) and salary
  Journal Entries (`accounting.journal_service`, `money_out.salary_entries`), the charge Journal
  Entries (`money_out.statement`), the statement import's transfers (`wallet.statement_import`)
  and the money-in Payment Entries of paid STK prompts (`utils.utils`; a Payment Request's entry is
  built with ERPNext's `create_payment_entry(submit=False)`, whose own default inserts then submits;
  a draft Payment Entry a prompt paid is submitted in one save too). A unit check keeps suite code
  from calling ERPNext helpers that insert then submit an entry. A switch still read off (before the migrate) inserts
  a draft as before.
- **A closed or frozen day.** A payout paid on a day in a closed Accounting Period, in books closed
  by a Period Closing Voucher (ERPNext's "Books have been closed till the period ending on ..."), or
  on or before Accounts Frozen Till (`posting.period_closed`), cannot be booked on that day, not
  even by Administrator. Make Entry takes an optional Posting Date (the payout's day or later; the entry's
  remark says both days). The B2B booking's note and alert say to use it; a B2C payout's
  accounting error and alert say to make its entry by hand with the receipt as Reference No, dated
  in an open period, which `booked_docstatus` counts (a Payment Entry allocated to the document, or
  a salary Journal Entry naming the employee).
- **A charge entry that fails waits on its own.** The charge Journal Entry and the charge line's
  links are made under a savepoint of their own: when ERPNext refuses the entry, the payout's own
  statement line and links stay, and so does the confirmed charge (M-Pesa Charge, kept outside the
  savepoint, so a retry the balances can no longer confirm books the charge the line was made for).
  One Mpesa Match Log per reason says why (the sweep's retries refresh it; a later, different
  reason gets its own row), and the sweep books the charge once it can, instead of rolling the
  payout's line back and logging an Error every 5 minutes. A charge line the suite cannot confirm
  and that has no Journal Entry is logged (Ambiguous) instead of being linked quietly.
- **B2C booking failures are alerted** to the Notify Role once per payout and reason, as B2B's are.
- The migrate's switch of "Submit Transfer and Funding Entries" leaves the settings' modified as it
  was.

Tests: `tests/unit/test_r4_post_at_once.py`; on the bench, an entry made under an active Payment
Entry workflow (Draft > Pending Approval > Approved) is submitted in Approved with no Open Workflow
Action and the invoice's outstanding falls at once, and a payout paid in a closed Accounting Period
is booked by Make Entry on a later Posting Date while its charge waits (`test_b2b_documents`).

**Known limits** (after the final review)
- An Integration Request's raw data of a B2C or B2B result holds the amount and receiver name
  (salaries too), and System Manager reads Integration Requests. Error Logs likewise.
- A Payment Request made on a Sales Order before it was invoiced is not counted by a later prompt on
  the invoice (as in ERPNext). Resend is hidden while any prompt is paid and not reconciled, even
  if its money was reversed later; request a new prompt from the order or invoice.
- **Claims and advances:**
  - Who reads Expense Claims sees claim payouts by DocType, not claim by claim: a site's User
    Permissions on claims are not reflected.
  - Accounts-only users see no claim buttons until the site gives them read on claims.
  - Payment schedule lines for claims and advances are not built.
- **Suppliers:** Purchase Invoice and Purchase Order have no "Pay to supplier phone (B2C)" button
  yet; the server side (approved phone accounts on B2C rows) is done.
- **Salaries:** HR-only users cannot prepare salary schedules (the schedule's rights are Accounts
  and M-Pesa Manager). HRMS still shows Make Bank Entry on a payroll M-Pesa pays.
- **Balances and names:** settlement sweeps can still show as a Gap. Only KCB's Safaricom name is
  confirmed in the bank list.
- **Desk:**
  - The desk scripts were checked with node, not in a browser on v15 and v16: check the buttons on
    the site.
  - Pay links (Payment Intents) are not offered from the desk.
  - Payment Entry's STK button is not hidden from non-collectors (the server checks Payment Entry
    submit).

Tests: 2849 unit tests pass at 9a9c8ae (`python -m pytest tests/unit -q`). Bench, full `run-tests
--app mpsa_suite` on fresh v15 and v16 sites with ERPNext, payments and HRMS: 510 tests at 9a9c8ae,
all pass on both versions, the verification tests included. `test_claim_payees` (merchant and
casual payees) first ran on a real site in this round: its setUpClass had failed at every earlier head.

## Phase 1 — Foundation (this change)

**Runs on Frappe/ERPNext v15 and v16**
- `pyproject.toml`: `frappe >=15,<17`, `erpnext >=15,<17`.
- `compat.py`: cache accessor, typed Single reads, `in_test()`, lazy `IntegrationTestCase`.
- CI: unit tests on Python 3.10–3.14 + bench tests on `version-15`/py3.11 and `version-16`/py3.14.
- Removed `frappe.db.commit()` from `Mpesa Settings.on_update` (not allowed inside document hooks on v16).

**One Daraja client** (`daraja/client.py`)
- All four token paths (builder connector, B2C connector, `get_token`, legacy connector) share one
  Redis-cached token with a 120 s safety margin, 10 s/30 s timeouts.
- Tokens are no longer written to `Mpesa Settings`; `Authorization` headers are redacted in
  Integration Request logs.

**Callback security** (`daraja/callbacks.py`, new **M-Pesa Suite Settings**)
- Every Safaricom-facing endpoint (STK, C2B validation/confirmation, B2C result, balance,
  transaction status, queue timeout, legacy verify, pull) goes through `@safaricom_callback`:
  optional IP allow-list, optional URL token `?t=…`, and runs as a configurable **Integration User**
  instead of switching the request to Administrator.
- Rejections are counted on the settings page (rate-limited writes).
- Every callback URL we generate now carries the token. Pull registration (permanent at
  Safaricom) is IP-checked only.

**Closed guest access**
- `get_mpesa_draft_c2b_payments`, `get_draft_pos_invoice`, `get_outstanding_invoices`: were open
  to anonymous users (customer names, phone hashes, invoices) → login + read permission.
- `Mpesa Express Request` `create_new_request` / `retry_stkpush` / `get_request_status` /
  `get_stkpush_defaults`: anonymous STK prompts to any phone, and a 30-second blocking poll →
  login + document permission. (Public checkout returns in Phase 3 via signed pay-page tokens.)

**Role gates** (`permissions.py`, new roles M-Pesa Manager / Payer / Collector / Viewer)
- Direct API calls to STK push / STK query need a collect role (incl. Sales User, Accounts User).
- Balance, transaction status, pull need a finance role; pull registration needs an admin role.
- Calls reached through a permission-checked document flow (e.g. Payment Request submit) are
  unchanged.

**B2C security credential fix** (`Mpesa Settings`)
- Upstream encrypted the STK *passkey* with OAEP for B2C and never regenerated. Now:
  RSA-PKCS1v15 of the *initiator password*, regenerated when the password is retyped, the
  environment flips, or on the new **Regenerate Security Credential** button.

**Accounting mapping on Mpesa Settings** (ERPNext custom fields): Bank Account, Cost Center,
M-Pesa Charges Account — replaces hard-coded `Mpesa-208911 - AIL` / `LIKE '%code%'` lookups in
later phases.

**Shared phone helper** (`utils/phone.py`): one normaliser (2547…/2541…), local form, Safaricom
SHA-256 MSISDN hash, network detection, masking.

## Phase 2 — Money in (C2B, STK, matching, booking, bank transactions, reconciliation)

Package `frappe_mpsa_payments/money_in/`. Every piece is behind its own switch on the
**Money In** tab of M-Pesa Suite Settings; with all switches off nothing changes.

| Switch | What it does | Astral Server Script it replaces |
|---|---|---|
| Create Bank Transaction for Every Receipt | C2B + STK receipts → submitted Bank Transaction on the shortcode's mapped Bank Account (one per receipt, MariaDB lock), 5-min sweep for misses | Register To Bank Transaction · Auto Reconcile pass 0 |
| Block Duplicate Receipts on M-Pesa Bank Accounts | Bank Transaction before_insert guard | BT Duplicate Guard · Duplicate BT Sweeper |
| Set Clearance Date When Every Bank Leg Is Matched | only adds clearance when all Bank GL legs are allocated | AIL BT Sync Voucher Clearance |
| Auto-Reconcile M-Pesa Bank Transactions | statement line ↔ Payment Entry via ERPNext `reconcile_vouchers`, exact amount | Auto Reconcile passes 1–2 |
| Use M-Pesa Receipt as Payment Entry Reference No | MPC2B-… → receipt | PE Reference To Receipt |
| Block a Second Payment Entry for the Same Receipt | runs *after* the reference swap (fixes the script-order race) | PE Duplicate Receipt Guard |
| Fill Missing Cost Center on M-Pesa Payment Entries | | PE Cost Center Autofill |
| Clear Clearance Date on Amended PE / JE | | AIL PE / JE Amendment Clear Clearance |
| Match and Book Receipts Automatically (+ 5 sub-switches) | existing PE link → document reference (PR/SO/SI/Quotation/Customer) → invoice-number repair → customer name → payer phone hash → orphan STK; books only on one candidate; Mpesa Match Log | Self Heal Unmatched · C2B No-Space Name Matcher · Payer Phone Matcher · Register Link Backfill · SO Checker pass 0 · Invoice Collection Allocator |
| Book Receipts Against Sales Orders and Release Paid Orders | sums receipts across all PRs of the order + direct references (cancelled excluded); when paid, applies the configured Workflow actions **as the configured approver** (`apply_workflow`, roles enforced) or submits; books receipts as advances | SO M-Pesa Payment Checker (without its workflow bypass, hard-coded approver, cancelled-register counting or Error Log deletion) |
| Normalise Phone on M-Pesa Payment Requests | 07… form | Normalize Mpesa Phone on Payment Request |
| Cancel Earlier Unpaid Prompt When Re-prompting | real `cancel()` / delete, not a docstatus flip | MPesa PR Cleanup Before Reprompt |
| Poll Safaricom for Unanswered STK Prompts | STK Query for prompts 2–60 min old; marks Completed / Failed with reason; never books money | (new) |
| Mark Payment Requests Paid When Settled | PR → Paid and STK prompt → Completed with receipt (db writes, so the prompt's duplicate-C2B check can't cancel the register) | AIL Mpesa Feed - Status Sync |

Upstream fixes in the C2B register:
- Payment Entry `reference_no` = M-Pesa receipt, posting/reference date = transaction date (not run date).
- Allocation capped at the invoice outstanding / order balance; the excess stays as an advance.
- Matched document (from the matcher) wins over the raw account reference.
- Company / Mode of Payment fall back to Mpesa Settings (Buy Goods tills had no Register URL row).
- Cost center from the shortcode's Accounting Mapping.
- Confirmations are idempotent per receipt (Safaricom re-sends).

New DocType **Mpesa Match Log**: every matched / booked / ambiguous / unmatched / error decision,
written once per receipt and result.

Tests: `money_in/test_money_in.py` (bootstraps its own KES company via the setup wizard, so it
runs on v15 and v16).

## Phase 3 — External Payments API, pay page, signed webhooks

Package `frappe_mpsa_payments/gateway/`, full reference in [docs/PAYMENTS_API.md](docs/PAYMENTS_API.md).

- **Mpesa API Client**: one record per chatbot or shop. It holds the API key and an encrypted secret
  (shown once), the paybill to prompt from, per-action permissions, max amount, rate limit, IP
  allow-list, webhook URL with its signing secret, and the hosts allowed for callback and return URLs.
- **Mpesa Payment Intent**: one payment the client asked for. Its name is the M-Pesa account
  number, so it settles through whichever message arrives: the STK callback, the C2B
  confirmation, a Pull sweep, or a receipt code the customer typed (`verify_receipt`).
  Paid intents linked to a Sales Order or Invoice are booked through the money-in pipeline.
- Endpoints: `create_prompt`, `create_payment_link`, `get_status`, `resend_prompt`, `cancel`,
  `verify_receipt`, `list_payments`.
  - Requests carry `X-Mpesa-Client-Key` / `X-Mpesa-Client-Secret` headers.
  - Responses use a JSON envelope with real HTTP status codes.
  - Idempotency keys prevent a repeated call from prompting twice.
  - A receipt can pay only one intent.
- **Public pay page** `/pay/<token>`: amount, description, phone box, live status, paybill fallback.
  It is rate-limited and capped at 5 prompts per payment, and shows nothing beyond the masked phone.
- **Webhooks**: `payment.succeeded|failed|expired|cancelled`, signed
  `X-Mpesa-Signature: t=…,v1=HMAC-SHA256`, retried with backoff for about a day, and logged in
  **Mpesa Webhook Delivery** with a *Send Now* button.
- Every-minute job settles prompts whose callback never came and expires stale intents.

## Rollout at Astral (staging copy first)
1. `bench get-app` this branch, `bench --site <site> migrate`. Existing records keep their data
   (fields upstream removed from the JSON are recreated as custom fields with the same names).
2. Open **M-Pesa Suite Settings**: set Integration User (System User with Accounts User + Sales User
   + M-Pesa Manager), keep enforcement off.
3. Fill **Accounting Mapping** on each Mpesa Settings (208911, 4134019, 4139683).
4. On each B2C settings record press **Regenerate Security Credential**, then run a balance query.
5. Turn on **Only Accept Callbacks From Allowed IPs**, watch the rejection counter for a day.
6. Turn on **Require Token on STK / B2C / …** (new requests carry the token immediately).
7. Re-register C2B URLs (Mpesa C2B Payment Register URL → save), then turn on the C2B token.
8. Astral's Server Scripts keep running; they are retired one by one in Phases 2–4.
9. Money In, one row of the table above at a time: disable the Server Script, turn on the switch,
   watch Mpesa Match Log and the Bank Reconciliation Tool for a day. Before the prepaid-orders
   switch, set **Approve Paid Orders As** to a user holding Sales Approver Primary + Secondary.
   Note: a fully paid order can still be stopped by the customer's credit limit; that shows as an
   Error row in Mpesa Match Log.
10. On upgrade, switches whose default is on (both duplicate-receipt guards, receipt as PE
    reference, cost-center fill, amendment clearance, phone normalising, the matcher's
    sub-switches) are switched on by migrate. They do the same thing as Astral's scripts and are
    safe to run alongside them. The master switches (statement lines, matching, prepaid orders,
    reconciliation, STK polling, status sync) stay off.
11. The first time **Match and Book Receipts Automatically** is on, it will book every draft
    receipt from the look-back window (45 days) that it can place unambiguously, including any
    left for manual review. Lower the look-back first if that is not wanted.

## Go-live hardening: audit 2026-10-18 "Still open" items 1–10 (uncommitted)

Follows [docs/AUDIT_2026-10-18.md](../docs/AUDIT_2026-10-18.md). **`bench migrate` is needed**: it adds
new fields and settings, and creates indexes on core tables.

**Callbacks are confirmed with Safaricom before any booking (items 1, 2, 4)**
- *C2B* (switch **Verify C2B Confirmations with Transaction Status**, on by default): a confirmation
  for a shortcode that is not in Mpesa Settings is dropped. Every other confirmation is held as an
  Integration Request (service `Mpesa C2B Verification`, Request ID = receipt). A Transaction Status
  query is sent for it, and the register is created only when the result has the same receipt,
  status Completed, the same amount and one of our shortcodes. The result must also carry the
  ConversationID of our query. So registers only hold confirmed money (or money Pull fetched from
  Safaricom), and matching, statement lines and the gateway need no change. A confirmation that
  can't be confirmed is re-queried every 10 minutes, 3 times at most, and then marked Failed with an
  Error Log. The hourly Pull import brings in real receipts anyway. An M-Pesa Manager can press
  **Release Without Safaricom Confirmation** on the Integration Request, which records who did it
  and why. This needs the Initiator and Security Credential on every Mpesa Settings, and the
  initiator must have the Transaction Status role.
- The manual **Fetch transaction** result (`handle_transaction_status_result`) now accepts only the
  answer to a query we sent, for the receipt we asked about, paid to our shortcode.
- *STK* (switch **Verify STK Callbacks with STK Query**, on by default): the callback endpoint only
  stores the payload (`callback_payload`, Verification Status *Pending*) and queues
  `money_in.stk_verify.settle`. That job sends an STK Query for the same CheckoutRequestID and checks
  the paid amount against the prompt. Only then does it set Completed and the receipt, and it
  commits **before** booking. Booking runs under a savepoint. A failed booking is rolled back,
  logged, and retried by the 2-minute sweep (3 attempts). A contradiction gives *Mismatch*, and an
  unanswered query after 6 hours gives *Unverified*. Neither is booked. A prompt is never marked
  Completed from an unconfirmed callback, so the gateway can't be fooled either.
- `stk_poll` skips prompts whose callback arrived; the settle job owns those.

**Callbacks without an Integration User (item 3)**: new setting **If No Integration User**.
*Warn* (default) runs as Administrator and writes one Error Log an hour. *Refuse* rejects the
callback and counts it. Set an Integration User, then switch to Refuse.

**Locks and indexes (items 5, 6)**
- `money_in.common.named_lock` now holds MariaDB `GET_LOCK` until the transaction commits or rolls
  back. It used to release at the end of the block, before the insert was committed. It is
  re-entrant within one transaction.
- Indexes (search_index Property Setter plus the index itself, on every migrate):
  Bank Transaction `reference_number`, `transaction_id`, Payment Entry `reference_no`, and
  Integration Request `request_id`. The duplicate checks stay locking reads, but they now lock only
  the receipt's rows instead of scanning the table.
- One insert path for registers, `money_in.receipts.insert_register`, used by C2B confirmations,
  Pull imports and Transaction Status results. It takes the same `c2b:<receipt>` lock, does a
  locking duplicate check, and inserts under a savepoint. Pull commits after each receipt.
- The Payment Entry duplicate-receipt guard takes `book:<receipt>`, the same key the gateway uses,
  and reads with `FOR UPDATE`.

**Hooks (item 7)**: register `on_cancel` raises when the Payment Entry can't be cancelled, instead of
logging it and leaving the PE live. The `after_insert` auto-submit and the `on_submit`
reconciliation run under savepoints, and the register survives as a draft. The commits inside
`create_and_reconcile_payment_reconciliation` and `handle_successful_transaction` are removed. The
Loan Disbursement service no longer commits, and the PE/JE/Loan services re-raise after logging.

**B2C (items 8, 9)**
- `handle_successful_payment` books under a savepoint and records failures on the B2C request
  (**Accounting Error**). A payout counts as **Accounting Booked** only when its PE/JE/Loan
  Disbursement is *submitted*. `retry_unbooked_payouts` runs every 15 minutes and retries paid
  payouts from the last 30 days.
- The disbursement's reference **Outstanding** is read-only and recalculated from the document
  (`utils/payouts.py`). On submit, under a per-document lock, a document can't be paid past what
  it owes, or while another disbursement's payout for it is in flight or paid but not yet booked.
- A B2C send that times out after the connection opened is **Status Unknown** (new status), not
  *Not Initiated*. Nothing can resend it. `check_unsettled_payouts` runs every 15 minutes and
  starts a B2C Transaction Status query (`money_out.b2c_status`, from the Daraja money-out session)
  for payouts that are Status Unknown after 5 minutes, or Initiated with no result after 60 minutes,
  at most once an hour per payout. Only a connect timeout (never sent) becomes *Failed* and can be
  retried. An HTTP 5xx without a Daraja error code is also Status Unknown.

**Permissions (item 10)**: `get_unallocated_payments` needs read on Payment Entry and the customer, and
uses `get_list`. `process_mpesa_c2b_customer_credit` and `process_mpesa_c2b_reconciliation` need
write on Payment Reconciliation and on the invoice(s) and entries, and check that each entry is a
submitted receipt from that customer. **Mpesa Settings** write and create are limited to System
Manager and M-Pesa Manager. Accounts Manager can read and export, Accounts User and M-Pesa Viewer
can read. Sites with customised permissions (Role Permission Manager) keep theirs, so check them.

**Rollout additions**
- Before migrating, give every Mpesa Settings an Initiator with the Transaction Status role, or turn
  the C2B verification off. Otherwise confirmations wait for Pull or a manager's release.
- Watch Integration Requests with service `Mpesa C2B Verification` and status Failed, and Express
  Requests with Verification Status Mismatch or Unverified.
- nginx must overwrite `X-Forwarded-For` (`proxy_set_header X-Forwarded-For $remote_addr;`) before
  the IP allow-list means anything (audit "Still open" #2, which needs no code change).

Tests: `tests/unit/test_verify.py` (decisions, pure) and `money_in/test_verification.py` (bench:
held C2B, forged amount, foreign ConversationID, unknown shortcode, forged/confirmed/short STK, re-sent
callback, lock held until rollback).

## Daraja money-out APIs: Reversal, B2B, Transaction Status for B2C (uncommitted)

The top three missing APIs from `docs/AUDIT_2026-10-18.md`, built to `docs/daraja_api_reference.md`
§9, §11 and §13. New package `frappe_mpsa_payments/money_out/`, plus pure request/result helpers
in `daraja/money_out.py`. `bench migrate` creates the three new DocTypes.

**Shared rules** (the same as B2C payouts)
- A request is sent only from a submitted document whose status is `Not Initiated`, under a row
  lock. The amount must be whole shillings; it is refused, never rounded.
- Safaricom's ack sets `Initiated`. Only the Result callback sets the final status.
- If the connection never opened, the status is `Failed`. If the request may have reached
  Safaricom (read timeout, dropped connection), the status is `Unknown`. Nothing is resent
  automatically.
- Each API has its own Result URL and its own QueueTimeOut URL
  (`money_out.api.<x>_result` / `<x>_queue_timeout`). Both carry the callback token and
  `?ref=<document>`. A queue timeout only ticks **Queue Timed Out** and never sets a final
  status.
- Results are matched by the ack's ConversationID / OriginatorConversationID. `ref` is used
  only when no ack arrived, and only if the IDs don't contradict it. A re-sent result is
  ignored once a final result is recorded.
- ResultCode is compared as a string (B2B sends `"0"`, Reversal sends `0` or `"R000001"`).
  `ResultParameter` may be a list, a single dict, or missing.
- Tokens come from `daraja/client.py`, with one refresh on a token error. Request bodies are
  logged through `scrub_payload`. Initiator and SecurityCredential come from Mpesa Settings,
  so **Regenerate Security Credential** is needed first.

**Mpesa Reversal Request** (`MRV-…`)
- Linked to an **Mpesa C2B Payment Register**. The C2B form has an Actions → **Reverse Payment**
  button that creates a draft. The receipt and amount are copied from the register, and the
  amount must be the whole payment.
- The shortcode is found from the register, and there can be only one live reversal per
  receipt.
- Submit and cancel need a payout or admin role: System Manager, M-Pesa Manager, M-Pesa Payer
  or Accounts Manager. Accounts User can only read.
- Result `0` sets **Reversed** and `R000001` sets **Already Reversed**; any other code sets
  **Failed**. A successful reversal adds a comment on the register. **Any Payment Entry booked
  from that receipt is not touched: cancel or reverse it by hand.**

**Mpesa B2B Payment Request** (`MB2B-…`): BusinessPayBill or BusinessBuyGoods
- Pays a supplier's paybill (account number required) or till. It can link a Supplier and a
  submitted KES Purchase Invoice. The amount can't exceed what is left to pay, counting other
  unbooked requests for the same invoice.
- The shortcode's transaction limit is enforced, and paying our own shortcode is refused.
- Submit needs PAYOUT_ROLES. Accounts User can prepare drafts.
- A request can be cancelled when it is Not Initiated or Failed. An **Unknown** request can be
  cancelled only by an admin, after checking the statement. Redo a failed payment with
  cancel + amend.
- Paid enqueues a Payment Entry (Pay, Supplier) from the shortcode's mapped Bank Account. It is
  allocated to the Purchase Invoice if one is linked, and submitted when **Submit B2C Accounting
  Entries** is on in Mpesa Settings. It is idempotent: an existing PE with the receipt as
  reference is linked instead. A booking failure is logged and commented, and the payment
  stays Paid.

**Transaction Status for B2C** (**Mpesa B2C Status Query**, `MB2CQ-…`)
- B2C Disbursement Request → Actions → **Check Status with M-Pesa** (payout/admin roles). It is
  shown for submitted M-Pesa payouts still in `Not Initiated`, `Initiated` or `Status Unknown`, meaning the
  request was sent but no result has come back.
- The query uses the receipt if known, otherwise `OriginalConversationID` = our
  OriginatorConversationID. Only one open query is allowed per payout every 2 minutes.
- The result changes the payout only while it is unsettled:
  - `TransactionStatus = Completed`, with the same amount, a receipt, and the same
    OriginatorConversationID, sets **Paid**. `result_code = "0"` makes a late real Result a
    no-op, and the same `update_b2c_reference_status` job books the payout.
  - `Failed` / `Declined` / `Expired` / `Cancelled` sets **Failed**, so **Retry Payment**
    (new OriginatorConversationID) becomes available.
  - Anything else ("not found" right after sending, pending, amount mismatch) is **Unresolved**,
    and the payout is left alone. Closes audit item 9.

**Hooks**: two `doctype_js` lines (B2C status button, C2B Reverse button). No doc_events or
scheduler changes.

**Tests**
- `tests/unit/test_money_out.py`: request bodies, result parsing, and status decisions.
- `tests/unit/test_money_out_flow.py`: the full send → ack → result flow on an in-memory
  `frappe` stub (`tests/unit/fake_frappe.py`). It covers row lock, Unknown on timeout, token
  retry, re-sent results, ID mismatch, queue timeouts, the status-query rules and the role
  gates.
- `pytest tests/unit`: 140 pass. Small bench tests sit next to each DocType. **None of this has
  run on a bench or against the Daraja sandbox yet.**

**Unverified / to check in sandbox**
- Whether Transaction Status accepts our OriginatorConversationID as `OriginalConversationID`
  (some integrations need Safaricom's ConversationID).
- The `TransactionStatus` strings other than "Completed".
- Whether BusinessBuyGoods needs an `AccountReference` (the reference doc says paybill only, and
  we follow it).
- Reversal `ReceiverParty` for a till: we send the Mpesa Settings business shortcode.
- `create_request_log` commits on v15. That happens before the HTTP call, the same as B2C today.

## Bench-tested on v15 and v16, fixes and Mpesa Settings setup guide (2026-10-19, uncommitted)

All bench test modules pass on Frappe/ERPNext v15 (15.123) and v16 (16.52), with `payments`
installed, after `bench migrate` of a site installed from the previous commit (indexes, new
fields and the Suite Settings switch defaults verified). `tests/unit`: 162 pass.

Fixes found by running on a bench:
- `b2c_disbursement_request.py`: `...permissions` -> `....permissions` (install-app crashed).
- `money_in/c2b_verify.send_query` (in the go-live hardening work, committed with it): result URLs built with `get_url()`; `build_callback_url` needs a
  web request, and this runs in background jobs, so no C2B verification query was ever sent.
  Needs `host_name` in site_config (as B2C and money_out already do).
- `utils.create_payment_gateway_account` / `create_mode_of_payment`: ERPNext v16 names the
  Payment Gateway Account after the company, so every Mpesa Settings save failed on v16. Both are
  now company-aware; saving without a Company no longer crashes ("Mode of Payment None"), and a
  Company set later gets its Mode of Payment account row.
- `Mpesa C2B Payment Register`: the shortcode's Mpesa Settings company wins over the user's default
  company that `insert()` pre-fills (multi-company sites booked to the wrong company).
- `gateway/intents.py`: "STK callback" is not an Mpesa Match Log method; booking-failure logs were lost.
- `api/payment_entry.get_outstanding_invoices`: empty list (not a crash) when there is no party account.
- `B2C Payment Disbursement Reference.is_valid_receiver_contact`: accepts 07../01../+254 forms (it
  rejected a valid 07 number unless already converted); `validate_mandatory_fields` reference check
  moved out of the loop.
- `Mpesa C2B Payment Register URL`: URLs from `get_url()`, status Failed on connection errors, and a
  **Register URLs Again** button (needed after host_name or the callback token changes).
- `Mpesa Settings`: test-mode balance / STK branches log the request like the live call does.

Mpesa Settings form: a collapsible **Setup Guide** (once-per-site steps, where each credential comes
from in sandbox and live, what to do after saving, how to test, common errors) and "where to get
it" help on every field. Business Shortcode is now visible in sandbox (STK prompts send it there
too); Till Number shows for Buy Goods or sandbox.

Tests: stale upstream tests rewritten against current code (B2C disbursement and reference, C2B
URL, api); Mpesa Settings fixtures fixed (paybill type, company, POS opening entry, POS Invoice mode
on v16); suites no longer leak callback-enforcement settings into each other.

## Unused upstream features removed, app rebranded to Astral (2026-10-19, uncommitted)

Checked on Astral's live site before removing: no Stanbic Settings and no Stanbic payouts, no B2C
payouts against Loans, no POS payment by M-Pesa (8 POS-mode Sales Invoices and 3 POS Invoices,
none paid by phone), and the Mpesa Payment Reconciliation tool was never installed.

**Removed**
- Stanbic: the `stanbic` module (Stanbic Settings, Stanbic Sort Code), `connectors/stanbic`, the
  Stanbic Mobile / PesaLink payment types, and the bank fields on B2C requests and rows
  (`stanbic_settings`, `dbs_reference_id`, `next_execution_date`, `bank_*`). Also removed: the
  Bank `pesa_link_bank_code` field. B2C now pays from Mpesa Settings only.
- B2C against Loans: `accounting/loan_disbursement_service.py`, the Loan option in disbursement
  forms and reports, and the Loan Disbursement custom field.
- POS: the point-of-sale page script, `pos_invoice.js`, the KlikPOS STK endpoints, the legacy
  `verify_transaction` callback and its POS tests, the POS Invoice "Request for Payment" button and
  POS Settings field, and the POS branch of the quick-pay dialog.
- Event Booking branch in `handle_successful_transaction`.
- Mpesa Payment Reconciliation tool (with Mpesa Draft Payments and Mpesa Payments Invoices), its
  endpoints (`get_unallocated_payments`, `process_mpesa_c2b_reconciliation`,
  `process_mpesa_c2b_customer_credit`, `submit_mpesa_payment`, ...), the `is_manual_reconciliation`
  global flag, and its workspace links.
- On migrate, patch `remove_unused_upstream_features` deletes those DocTypes, their custom fields
  and the empty Stanbic module. POS Invoice `mpesa_receipt_number` is kept because it holds data.

**Kept**: the Sales Invoice desk tools (STK on an invoice or payment row, quick-pay dialog for
received M-Pesa payments), Payment Request STK (all 411 of Astral's prompts), and the
Payment Gateway DocType.

**Rebrand**: the app title is **Astral M-Pesa Suite**, publisher and author are Astral Industries
Limited, the README is rewritten, a NOTICE file is added, and the workspace no longer carries
Navari's test users. The Python package, module and DocType names don't change, because
Safaricom callback URLs and existing data depend on them. Navari's copyright headers and the AGPL
licence stay, as the licence requires. `docs/LICENSING.md` lists every file that came from upstream
for legal review.

## Sandbox Test Lab and one-shortcode accounts (2026-10-19)

**M-Pesa Sandbox Test Lab** (`sandbox_lab/`, DocTypes Mpesa Sandbox Test Run / Test Case; guide in
`docs/SANDBOX_TESTING.md`). Runs every flow against Daraja's sandbox and checks what the app did:
22 scenarios over health, C2B (exact / mistyped / unknown reference, duplicate, forged without
token, forged receipt, wrong shortcode), STK (paid, cancelled, forged "paid" while open, duplicate,
status check, no token), B2C (paid and booked once, duplicate result, settled payout not
re-queried), B2B (typed paybill refused, approved account paid, duplicate result) and reversal.
Tools: Simulate a C2B Payment, Replay a Callback (with or without token), Health Check. Checks run
every minute until Passed / Failed / timeout. Works only with site config `mpesa_sandbox_lab: 1`,
Sandbox-ticked Mpesa Settings and M-Pesa admin roles, so it can never use live keys.

**One shortcode for everything.** Safaricom's M-PESA Business One Account lets one number collect
(Paybill / STK) and pay out (B2C, B2B); collections and B2C both use the Utility account.
- `settings_for_shortcode` prefers a collecting record over a B2C-only one when several records
  share a number (then the oldest), instead of whichever the database returned first.
- C2B register auto-reconcile and its after-insert lookup now use that resolver: they matched
  `business_shortcode` only, so Buy Goods tills never matched, and `_reconcile_payment` crashed
  when no record was found.
- Saving an Mpesa Settings whose shortcode another record uses shows which record books receipts.
- Setup guide: one record per shortcode; the API operator needs a role per API.

## Daraja docs verified first-hand (2026-10-19, uncommitted)

Hermes read Safaricom's own Daraja pages and answered the 31 open questions
(`../docs/DARAJA_VERIFIED_2026-10-19.md`). `../docs/daraja_api_reference.md` now marks each item
verified, not stated, or contradicted by Safaricom's own pages. Code changed where a fact differed:

- **Banned words in callback URLs.** Daraja refuses callback URLs containing mpesa, M-Pesa,
  safaricom, exe, exec, cmd, sql or query, and the FAQ states this for every callback. Three
  upstream URLs broke it, so they are no longer sent; the old endpoints stay so results for requests
  already sent still land:
  - Account Balance ResultURL `balance_query_callback` → `m_pesa_api.account_balance_result`.
  - Pull Transactions register CallBackURL `doctype.mpesa_settings…pull_transaction_callback` →
    `m_pesa_api.pull_registration_result` (affects new registrations only).
  - B2C ResultURL loses the unused `?provider=Mpesa`.
  - `daraja.guard.callback_url_problems` checks HTTPS (production) and banned words. C2B URL
    registration now refuses to send a URL that fails, because production registration is
    one-time. A unit test scans every `@safaricom_callback` endpoint, and another fails if the app
    sends a legacy URL again.
- **C2B validation reply** is now exactly the documented shape: `{"ResultCode": "0"}` to accept, and
  `C2B00016` ("Other Error") when the IP or token check fails, instead of the undocumented `1`.
- **Amount limits.** STK refuses more than KES 250,000. B2C refuses amounts outside 10–250,000
  before sending (it only checked for whole shillings).
- **B2B Buy Goods sends AccountReference.** Daraja's BuyGoods body lists it and doesn't mark it
  optional. Paybill still requires the supplier's account number; a till without one gets the
  request's name.
- **OAuth tokens.** Each token request revokes the previous token. Token fetches now run under a
  Redis lock, so workers that miss the cache at the same moment reuse one token instead of revoking
  each other's. A token-error retry drops the cached token only if it is still the one Safaricom
  refused (`invalidate_token(rejected=…)`), so it no longer throws away a fresher token another
  worker just fetched. If Redis can't lock, the fetch goes ahead without the lock, as before.
- Checked and already right: the 12 published callback IPs are inside the default allow-list; RSA
  PKCS#1 v1.5; STK till mapping; every documented STK result code; B2C v3 with
  OriginatorConversationID; Reversal `RecieverIdentifierType` 11 and R000001; only C2B receipts can
  be reversed; Transaction Status `OriginalConversationID`; Account Balance column order
  (Current|Available|Reserved|Uncleared); `500.003.1001` treated as already registered; Pull paging
  by page number.
- Sandbox guide: one app per product if the portal won't combine C2B and B2C; Short code 2 is
  B2B's Party B; STK test money auto-reverses at 6am.

Still open with Safaricom (pages conflict or are silent): STK Timestamp time zone; what happens to
an AccountReference over 12 characters; one app for C2B and B2C; `BusinessPayToBulk` vs
`BusinessTransferFromMMFToUtility`; B2B from a Business One Account or a till; Pull offset
semantics; the QueueTimeOut payload.

## Per-API switches on Mpesa Settings (2026-10-19, uncommitted)

One shortcode can collect and pay out (M-PESA Business One Account), so the single required
upstream **API Type** is replaced by one switch per API, under **APIs this record uses**:
Receive C2B, Send STK Prompts, B2C Payouts, B2B Supplier Payments, Reversals
(`frappe_mpsa_payments/daraja/apis.py`).

- **What a switch gates:** what the app *sends*. STK (Express Request, `initiate_stk_push`,
  Mpesa API Client), C2B URL registration, B2C payouts, B2B payments and Reversals each refuse
  with "does not have … ticked" when their switch is off: when the draft is saved, and again
  just before sending. Payments Safaricom reports are always recorded, whatever the switches say.
  Transaction Status and Account Balance are read-only and have no switch, because C2B and B2C
  verification depend on Transaction Status.
- **Migration** (patch `mpesa_settings_api_switches`): each record gets the switch for its old
  API Type, plus every API it was already used for. That means C2B for any record with a C2B
  Register URL, and B2C, B2B or Reversals for any record with a request of that kind. Everything
  else starts off. **Old API Type** stays on the form, read-only. A new record created by code that
  still sets `api_type` gets the matching switch.
- **Booking received money:** when several records share a shortcode, the one with Receive C2B or
  Send STK ticked wins (was: anything not "MPesa B2C"), then the oldest.
- **Sandbox Party A:** instead of a new `sandbox_party_a` field, the sandbox test shortcode
  (600xxx) already kept in **Till Number** (used for C2B registration, Pull and Status) is now also
  Party A for B2C, B2B, Reversal and Balance (`apis.initiator_party_a`). Live records still send
  the Business Shortcode, so nothing changes in production. One sandbox record can hold 174379 for
  STK and 600xxx for everything else.
- Saving a record needs at least one switch. Ticking a money-out switch without an Initiator shows
  a warning. The Sandbox Lab health check lists each record's switches and flags any its tests need.
- Not done: a separate Working Account bank mapping for B2B and B2C top-ups (B2B debits the Working
  account, collections and B2C use Utility).
- Rehearsed on a restored copy of Astral's live site (2026-10-20): Navari 15.3.1 replaced by the
  suite, `migrate` clean, no rows or columns lost. The rehearsal showed 4134019 (old type "MPesa
  B2C") had sent 21 STK prompts and receives C2B, so the patch also keeps STK for any record with
  Express Requests and C2B for any record with C2B receipts on its shortcode. Result there:
  4139683 = B2C; 4134019 = C2B + STK + B2C; 208911 = C2B + STK.

## Fixes found by the first Sandbox Lab run (2026-10-20, uncommitted)

Ran the lab against Daraja's sandbox from a local v15 bench behind an ngrok tunnel. 10 tests
passed, including STK end to end on a real phone: paid and booked once, unanswered prompt books
nothing, a re-sent callback changes nothing, the status check leaves it booked once, and a forged
"paid" callback's receipt is never booked. The run found these bugs:

- **C2B URL registration marked a success as Failed.** The sandbox's v2 registerurl answers
  `ResponseCode "00000000"`, not the documented `"0"`. New `daraja.guard.is_zero_code` accepts any
  all-zero code; used on every "request accepted" check (registerurl, B2C/B2B/Reversal/Status acks,
  C2B verification, lab simulate). Result codes in callbacks are unchanged.
- **Account Balance and STK prompts crashed outside a web request** ("object is not bound").
  `build_callback_url` read the request URI through `get_request_site_address(True)`; it now uses
  `get_url()`, so background jobs and scripts work.
- **An STK prompt made on a Sales Invoice was paid but never booked.** Upstream appended a
  payments row to the submitted invoice and saved it, which Frappe refuses
  (UpdateAfterSubmitError). It now books a Payment Entry against the invoice (receipt as
  reference, one per receipt, allocation capped at what is owed). Astral's live prompts all use
  Payment Requests (326 of 326), so this path had never run there.
- **Every successful B2C payout failed to record.** The result handler copied the payout status
  ("Paid") onto the Integration Request, whose status cannot be "Paid", so the save failed and the
  whole callback rolled back: the payout stayed Initiated and was never booked. This came from
  upstream's version-16 branch (`status=req.status`); Navari 15.3.1, which Astral runs live, used
  "Completed". It now logs Completed/Failed, and `update_integration_request` maps any other value.
- **A supplier account whose receiver name did not match could still be paid.** After a B2B payment
  whose M-Pesa receiver name differs from the approved supplier name, the account is now blocked:
  new B2B requests to it are refused with the reason until an M-Pesa manager checks with the
  supplier and presses **Receiver Name Confirmed** (optionally adopting the reported name).
- Lab: the C2B arrival checks ignore the lab's own `ZZ` forged receipts (the forged-receipt test
  uses the same invoice number), and `post_callback` commits before posting, so a forged callback
  for a prompt made a moment earlier no longer waits on an uncommitted row.

Because the sandbox initiator stayed locked, B2B, B2C, C2B and Reversal were also run on the lab site
with only Safaricom's side simulated: its documented ack, and its verified result / confirmation
samples posted through the public URL. Everything else ran for real. B2B: payload, Paid, Payment
Entry against the Purchase Invoice (draft or submitted per setting), re-sent result, failure,
receiver-name alert and block. B2C: payload, Paid, row and disbursement status, Payment Entry,
re-sent result, failure. C2B: exact reference, mistyped reference repaired, unknown reference left
for review, duplicate confirmation. Reversal: Reversed, second reversal refused, R000001.

Sandbox behaviour seen (not app faults): the shared test initiator answered 8006 "API password
is locked", so Balance, B2B, B2C and Reversal cannot pass until Safaricom unlocks it. C2B simulate
is accepted but confirmations never arrive, because test shortcodes like 600584 are shared and
Safaricom delivers to whoever registered URLs last. The sandbox sometimes completes an STK prompt
by itself. Safaricom's firewall (Incapsula) blocks bursts of calls with a 403 page; a retry after a
pause works. These flows get their proof in the KES 1-10 live checks at go-live.

## Full code review: 28 confirmed fixes (2026-10-21, 50987de)

A multi-agent review of the whole app found 38 suspected defects. Independent verifiers refuted
10 and confirmed 28. Each fix has tests and was checked by adversarial verifiers. A cross-check
and two more rounds then closed the gaps found between areas. Unit tests: 301 before, 813 now;
the full bench suites (255 tests) pass on v15 and v16, and a KES 10 STK prompt on a Payment
Request was paid on a real phone through the Sandbox Lab and booked once. **Run `bench migrate`**: this adds a Match Log method,
makes the webhook URL Small Text, changes the Mpesa Settings help text, adds an index on B2C
Disbursement Request.reference_name, marks the payout row's result fields no_copy, and runs two
patches (`drop_text_search_index_setters`, `relink_b2c_reference_rows`).

**Money in**
- **C2B confirmations register again with verification on (critical).** Safaricom posts the
  Transaction Status result as JSON, so the `?ir=` on the Result URL never reached the handler and
  every verified confirmation failed after three queries. The handler now reads `ir` from the URL,
  or finds the held request by the result's exact ConversationID.
- **Guest callbacks cannot use SQL wildcards.** A forged ConversationID of `%` used to match staff
  "Fetch transaction" queries and held C2B confirmations. Callback IDs must now be Safaricom-shaped
  (`guard.daraja_id`), and the stored ack must name them exactly (`guard.names_id`). Account
  Balance results touch only balance queries. Check Transaction Status registers a receipt only
  when TransactionStatus is Completed and the money reached one of our shortcodes.
- **A receipt is booked once.** An STK receipt that also arrives as C2B is booked once, decided on
  committed rows with locking reads on both sides. The orphan-STK matcher adopts only prompts
  Safaricom never answered. A cancelled or deleted register is never re-imported by Pull,
  Transaction Status or held confirmations (amend or restore it to re-book).
- **Refunded money is never booked.** A receipt with a live reversal is refused by the register's
  `before_submit` (every path: hand, matcher, prepaid orders, POS), by STK booking (with a locking
  read, so a reversal being sent is waited for), by POS quick pay, and by the Payments API. The
  reversal is found by its register or by the receipt.
- **POS money is not counted twice.** POS quick pay no longer lists or accepts drafts that are
  reversed, already booked, or a payment intent's receipt. An STK booking refuses, instead of
  cancelling, a register a cashier put on an invoice.
- **Payment Requests are prompted once.** A Payment Request above the Transaction Limit (at most
  250,000) is refused before any prompt goes out. Before, it was split into prompts that Safaricom
  refuses while the first is pending. **At go-live, raise 4134019's Transaction Limit from 150,000
  to 250,000.** A paid prompt books what it collected. The cents a whole-shilling prompt cannot ask
  go to Round Off, and a Sales Order billed in the meantime is booked against its invoices.
- **Re-prompts.** Sending a prompt again is refused while the request is paid, its callback is
  undecided, or the last prompt is under 90 seconds old. A re-prompt resets the old settlement under
  a lock, and the status poller, give-up timer and booking retries count from the latest prompt.
- **Hourly Pull recovers outages:** the last 2 hours, then 47 hours (Safaricom keeps 48).
- **Prepaid orders** refused by ERPNext (for example the credit limit) stay drafts, are logged once
  with a hint, and are retried with backoff. Real bugs get an Error Log with the traceback.
- Register amount, receipt and shortcode cannot be edited once saved. The `search_index` Property
  Setter on Bank Transaction's Small Text Reference Number, which made every Customize Form and
  Custom Field save on Bank Transaction fail, is removed and no longer recreated on migrate; its
  prefix index stays, so the duplicate check still uses it. Unconfirmed STK callbacks reach the
  Match Log ("STK Callback").

**Money out (B2C, B2B, Reversal)**
- **"Check Latest Payment Status" no longer overwrites rows.** It used to save the browser's stale
  copy over the payout jobs' request links, so paid payouts could never be booked. A patch relinks
  rows whose link was wiped, and every Result rewrites it.
- **Each payout is booked by its own M-Pesa receipt.** Two rows of one disbursement for the same
  invoice each get an entry, where before the second was marked booked with none.
- **No double payouts.** A Salary Slip's owed amount goes down by every B2C payout against it.
  Retry and submit both take the per-document lock and refuse when the document was paid since,
  when another payout is open, or when a Paid payout has no live entry (entry cancelled, Cancel All,
  older than 30 days). Rows must be whole shillings, 10 to 250,000.
- An advance paid against a Purchase Order still counts as booked after ERPNext moves it onto the
  invoice made from the order (Get Advances, Payment Reconciliation), so the next advance on the
  order is not refused. Desk **Duplicate** no longer copies a row's request link and result
  (no_copy; amend still keeps them), and a link copied any other way never hides another
  disbursement's live payout.
- Salary Slip Journal Entries carry the receipt as Reference No, so ERPNext can submit the Bank
  Entry. Failed results reach the row (queued after commit).
- **Results:** a forged Result without IDs no longer settles an acked B2B, Reversal or status
  request. `ref` is read from the URL and trusted only next to the callback token. A reversal of a
  receipt an STK prompt booked says the entry must be cancelled by hand, and one on a POS or Sales
  Invoice names the invoice whose M-Pesa payment must be returned.
- **Daraja tokens:** reads always go to Redis, so a v15 cache miss no longer mints a token per call
  and revokes other workers' tokens. A refused token is dropped only if Redis still holds it.

**Payments API**
- A `callback_url` over 140 characters no longer stops the payment from becoming paid. A webhook
  that cannot be queued no longer blocks the status change.
- A second payment on a paid intent is booked on account, and staff are told. A paybill receipt is
  booked with the receiving shortcode's settings. Money paid to another company's number is left
  for staff.
- Reversed receipts and receipts already on a POS or Sales Invoice are `claimed` and refused.
  See docs/PAYMENTS_API.md.

**Go-live checks added by this review**
- Before retiring the AIL Mpesa Feed Pull Sweep, run one manual Pull over a busy 47-hour window.
  Imported plus skipped must equal Safaricom's TotalRecords; this proves OffSetValue is a page
  number.
- Watch the first C2B confirmations. Verification now fails closed when a Transaction Status result
  has no CreditPartyName; such receipts arrive through Pull or a manager's release, with an Error Log.
- Prepaid orders: the "Approve Paid Orders As" user needs Sales Creator, Sales Approver Primary,
  Sales Approver Secondary and the Accounts Settings Credit Controller role.

## Upgrade rehearsal 2 on the Astral copy (2026-10-21)

`astral.local` (restored live copy, Frappe 15.121.3 / ERPNext 15.121.6, 17 apps, scheduler
paused) moved from 8fb4b49 to 75de2f1. Backup `20261021_153208` was taken first. `migrate` took
40s and lost no rows or columns. Payment Entries, GL, Journal Entries, Bank Transactions and every
M-Pesa table are unchanged, and the Mpesa Settings switches are as before. The two new patches did
exactly their job:
- `drop_text_search_index_setters` removed the one setter on Bank Transaction's Small Text
  Reference Number, and `ensure_lookup_indexes` did not put it back. Every index stayed. Customize
  Form and Custom Field saves on Bank Transaction work again; before, they failed with
  "Fieldtype Small Text ... cannot be indexed".
- `relink_b2c_reference_rows` restored the one wiped row link (Expense Claim HR-EXP-2026-06055,
  KES 2,344; its Payment Entry exists).

The M-Pesa scheduled jobs then ran once each with outbound network blocked, rather than switching
on the whole scheduler, which would also run the 16 other apps and Astral's server scripts. None
failed. The accounting sweep matched 412 Paid payouts of the last 30 days to the Payment Entries
Astral's current flow had made, and created no new entries. It also found these fixes:
- **Nothing more is booked for a payout whose receipt is already on another entry.** Astral's
  flow made some payout Payment Entries as unallocated advances, or on the invoice made from the
  paid order; a Bank Reconciliation Journal Entry can also carry the receipt. The sweep tried to
  book again every 15 minutes, and Payment Entry's duplicate-receipt guard refused each time.
  Now the payout gets one Error Log and a note for its case: "not allocated: allocate it to
  <document>", or "it is on <document>: if that is what this payout paid, nothing more is
  needed". There were 7 such payouts on Astral. The payout stays unbooked and is re-checked
  quietly each pass. It is booked once that entry stops carrying the receipt, for example after
  it is cancelled as another payout's, or once an entry for the document carries it. The
  double-payout guard keeps counting it meanwhile, so the document cannot be paid again.
- **A row paid before the suite, without its receipt, takes its Paid request's receipt.** The
  receipt is written to the row before any entry is made.
- **Error logging works on v15.** v15 stores a single-line first argument of `frappe.log_error`
  as the Error Log title (140 characters), so a long message, such as a whole C2B payload or an
  exception text, made the log itself fail. Calls now pass `title=` and `message=`, and a unit test
  checks every call.

## First live B2B payment (2026-10-22)

Safaricom approved B2B on all three shortcodes. From the Sandbox Lab with the production keys
(entered by the user in a hidden form), KES 10 BusinessPayBill went from 4139683 to Astral's own
paybill 208911, account B2B-TEST, through an approved Supplier M-Pesa Account:
- First try: Failed, ResultCode 1 "The balance is insufficient". **B2B debits the Working (MMF)
  account, which was KES 0; the B2C float sits in Utility.** The result was recorded as Failed with
  the reason, and nothing was booked.
- After the user moved funds into Working: **Paid**, receipt UJMTM632YM. Safaricom reported the
  receiver as "208911 - ASTRAL INDUSTRIES LIMITED", which matches the expected name, so the account
  stays unblocked. Charge KES 2 ("Business Pay Bill Charge"); Working balance after KES 1,488.
  The Payment Entry carries the receipt and booking it again creates no duplicate. On the lab it
  needed a Bank Account mapped on the Mpesa Settings record first.
- To a bank: KES 10 to KCB paybill 522522 (the user's own account, entered and sent by the user)
  was **Paid**, receipt UJMTM6331C. The receiver was reported as "522522 - KCB Paybill AC", which
  matches the expected "KCB". Charge KES 2; Working balance after KES 1,476. The Payment Entry was
  booked automatically.

**Go-live checks for B2B**
- Keep a float in each paying shortcode's **Working** account (B2C keeps using Utility).
- Map the **Bank Account** (Accounting Mapping) on every Mpesa Settings record that pays by B2B.
- Approve each supplier's paybill or till as a Supplier M-Pesa Account (proof attached, approved by
  a second person), with the name M-Pesa shows as the expected name.

## Upgrade safety: C2B verification needs an initiator (2026-10-22)

"Verify C2B Confirmations" is on by default after the upgrade. It asks Safaricom's Transaction
Status about each paybill confirmation, which needs an Initiator and Security Credential on that
shortcode's Mpesa Settings. On Astral, 208911 (the main paybill) has no initiator, so every
confirmation would have been held and then marked "Cannot verify". Paybill payments would have
stopped appearing in the ERP until someone released them by hand. Now a shortcode without an
initiator registers its confirmations as they arrive, as Navari always did, and an Error Log
reminds staff (at most once a day) to add an initiator allowed to query Transaction Status.
Shortcodes with an initiator are verified as before. The Security Credential is regenerated
whenever someone types the initiator password on Mpesa Settings and saves, so a Safaricom-forced
password change needs no developer.

## Money out on the statement: payout and charge Bank Transactions (2026-10-22, uncommitted)

Money in already became a Bank Transaction per receipt within seconds. Money out now does too
(`money_out/statement.py`).

**What happens on each Paid payout**
- Right after a B2C result's job books the payout (`b2c_response_service.update_b2c_reference_status`,
  after `handle_successful_payment`), and after a B2B result's job books its Payment Entry
  (`b2b.book_payment_entry`), the payout gets a **withdrawal line** on its shortcode's Bank Account
  (`bank_account_for`: the Accounting Mapping, else the Mode of Payment's account). Reference and
  transaction ID are the M-Pesa receipt, the date is the completion time, the party is the
  employee or supplier.
- **Charge line** `CHG-<receipt>` (type "M-Pesa Charges") and a **Journal Entry** (Bank Entry,
  Cheque No `CHG-<receipt>`): Dr the shortcode's **M-Pesa Charges Account** (Mpesa Settings →
  Accounting Mapping, the existing `charges_account` field; cost center from the same section or
  the company default), Cr the shortcode's bank GL. Submitted when "Submit B2C Accounting Entries"
  is ticked on that Mpesa Settings, like the payout's own entry; otherwise a draft for review.
- **Reconciliation**: the payout line with its Payment Entry (Reference No = receipt; a salary
  payout's Journal Entry by Cheque No), the charge line with its Journal Entry. Only when exactly
  one submitted, uncleared entry carries the key, pays out of that bank GL and has exactly the
  line's amount. No entry: left quietly (the sweep tries again when one is booked). Another amount
  or two candidates: left, and listed in **Mpesa Match Log** (Ambiguous, Bank Reconciliation).
- **Sweep** every 5 minutes (`money_in.events.every_five_minutes`): makes lines a job missed
  and reconciles every unmatched withdrawal on an M-Pesa Bank Account, including lines Astral's
  scripts or a statement import made. Re-sent results and re-runs never make a second line or entry.

**The charge**
- B2B: Safaricom states it (`DebitPartyCharges`, e.g. "Business Pay Bill Charge|KES|2.00").
- B2C results carry no charge, only the wallet balances after the payout. Daraja's docs say charges
  go to the Charges Paid account; on Astral's 4139683 that balance is 0 on all 1,474 payouts and the
  charge comes off **Utility**, so the suite uses Utility + Charges Paid (right for either setup).
  Ordering the restored copy's payouts by completion time (then balance, for payouts completing in
  the same second) and taking the fall since the payout just before gives a charge for 1,428 of
  1,473; every one equals Safaricom's banded tariff: up to 1,500 → 5, 5,000 → 9, 20,000 → 11,
  above → 13. The other 45 follow a float top-up or a settlement sweep. (The portal statements
  imported later showed one more band: the 23 payouts of KES 100 or less carried no charge, so the
  default bands now start `100 = 0`.)
- So a B2C charge is the **B2C Charge Bands** setting (that tariff by default), checked against the
  balances: when no payout to that shortcode is still in flight, nothing else moved the wallet in
  between (no C2B receipt, no payout without a balance) and the fall is a whole-shilling amount
  between 0 and 100. **When the balances show a different charge, nothing is booked** and Mpesa
  Match Log says why (Ambiguous, "Payout Charge"). Empty bands: the charge the balances show is
  booked once they show it.
- For comparison, Astral's feed sorted every payout ever made by Utility balance, across top-ups:
  only 427 of its 1,473 payouts got a charge line, and 4 of those are wrong (48.02, 205.40, 219.02,
  and a 4 that should be 5).

**Settings** (M-Pesa Suite Settings → new **Money Out** tab)
- Lines follow the Money In switch, now "Create Bank Transaction for Every Receipt and Payout".
- "Book Safaricom Charges Automatically" (default on) and "Reconcile Payout and Charge Lines
  Automatically" (default on; also needs "Auto-Reconcile M-Pesa Bank Transactions").
- "B2C Charge Bands": `<payout up to KES> = <charge>` per line; checked on save.
- "Payout Lines From": payouts completed earlier are left alone. A patch
  (`payout_lines_from_upgrade_date`) sets it to the upgrade date on an existing site, so the first
  sweep at go-live does not book about a thousand charge entries into past (maybe closed) months.
  Move it back to fill older payouts on purpose (within Look Back (Days)).
- New read-only fields on B2C Disbursement Request and Mpesa B2B Payment Request: Bank Transaction,
  M-Pesa Charge, Charge Bank Transaction. Mpesa Match Log gets the method "Payout Charge".

**Next to Astral's scripts.** The suite uses the same keys as "AIL Mpesa Feed - B2C Payout To
Bank" and "B2C Voucher Complete" (receipt; `CHG-<receipt>` on the line and as the JE Cheque No),
looks for an existing line by receipt before making one (on the mapped account, and on any other
M-Pesa Bank Account: then it makes none and lists it), and uses an existing charge Journal Entry
as it is. A charge line their script made with another amount gets no entry from the suite (listed
in Match Log). Still disable both scripts when the switch is turned on.

**Small change in money in**: the duplicate check for a statement line now looks only at lines
moving money the same way. A B2B between two shortcodes mapped to one Bank Account puts the
same receipt on a withdrawal and a deposit; before, the second was silently dropped.

**Go-live**: on each paying Mpesa Settings set the M-Pesa Charges Account ("5.2.03.12 - (J015)
M-Pesa Charges - AIL") and a cost center, keep "Submit B2C Accounting Entries" as staff want, map the
Bank Account, then disable the two AIL B2C feed scripts and turn the switches on.

Tests: `tests/unit/test_payout_statement.py` (charge rules on Astral's real balances, switches,
reconciliation rules, both result jobs, settings, patch), bench `money_out/test_statement.py`
(B2B through the real booking job, B2C Payment Entry and salary Journal Entry, balances agreeing and
disputing the band, payouts in flight, Astral's own lines and entry reused, another M-Pesa account,
switches, missing charges account, sweep and Payout Lines From, both directions of one receipt).

## M-Pesa accounts: each wallet of a shortcode on its own statement (2026-10-22, uncommitted)

A shortcode is several wallets. Astral's 4139683 portal statements (Aug–Oct 2026) show how money
moves between them: the bank funds **MMF/Working** ("Deposit into MMF Control Account from
M-PESA", 19 deposits, KES 7.56M), staff move it to **Utility** on the portal ("Business Transfer
from MMF Account to Utility Account", 20 transfers), Utility pays B2C (1,999 salary payments, each
with its own "B2C Payment Charge" row) and Working pays B2B (with "Business Pay Bill Charge").
Daraja sends no callback for funding, transfers or sweeps to the bank; only the statement has them.

**Sub-accounts on Mpesa Settings (optional)**
- Accounting Mapping → Sub-Accounts maps Working (MMF), Merchant, Charges Paid, Organization
  Settlement or Float to a Bank Account of its own (an existing Bank Account and GL is fine).
- **Nothing changes until a row is added**: every sub-account left out posts to the shortcode's
  Bank Account, exactly as before, so a site coming from Navari's app keeps working as it is.
- The wallet that receives customer payments (Utility for a paybill, Merchant for a till) always
  stays on the shortcode's Bank Account, because receipts are booked to the Mode of Payment's
  account. A row that tries to move it is refused on save, as is a sub-account mapped twice or a
  Bank Account of another company.
- Routing: C2B and STK go to the receiving wallet; B2C payouts and their charges go to Utility;
  B2B payouts and their charges go to the account their result names, which is Working on live
  results. The B2B Payment Entry pays from that GL. A B2C entry from a till pays from Utility's GL
  when Utility has its own.
- Every line now records its wallet and Safaricom's completion time (new read-only Bank
  Transaction fields "M-Pesa Sub-Account" and "M-Pesa Completion Time"). Lines made earlier get
  them when an import finds them.

**Statement import** (new **Mpesa Statement Import**, M-Pesa workspace → M-Pesa Accounts)
- Attach the per-account export from the M-PESA Org Portal: `ORG_<shortcode>_<Account>_All_<date>.xls`,
  or .xlsx / .csv in the same layout. Press Import Statement; it runs in the background.
- The portal's combined export ("Statements for <shortcode> ...") has no account column, so it is
  refused with a message to export each account separately.
- Each row is handled by its kind:
  - **Payout or receipt:** the live feed's line is found by its receipt (or made), then reconciled
    as the live feed would.
  - **Charge:** the line `CHG-<receipt>` carries the charge Safaricom actually took, with its
    Journal Entry. The statement settles every charge exactly, including ones the tariff check
    left out.
  - **Transfer between two wallets:** a line on each side and one Internal Transfer Payment Entry,
    both lines reconciled. If both wallets share a Bank Account, nothing is booked because nothing
    left it.
  - **Funding from a bank, or a withdrawal to one:** the line, then the entry for that money:
    - First choice: the entry staff already booked. This is exactly one submitted Journal or
      Payment Entry moving that amount into (or out of) the wallet's GL within "Match Bank Lines
      Within (Days)" and not yet matched to a line on that account (Astral books funding as
      "Dr Mpesa-4139683 / Cr KCB, Transfer to B2C").
    - Otherwise: an Internal Transfer with the bank's own line, when exactly one unmatched bank
      line of that amount lies in that window.
    - Otherwise the note says why. For example: the entry of that amount is already matched to
      another line, which is probably an older hand-made top-up line for the same money. Or it
      went into the shortcode's main GL rather than the Working GL, and needs amending.
  - **A payout Safaricom charged nothing for** (KES 100 or less) whose old `CHG-` line says
    otherwise is left for staff to cancel, with the line and its entry named.
- Anything less certain gets its line and is left for staff. Every row's outcome is an **Mpesa
  Statement Row**: Line Made, Already There, Reconciled, Left for Staff (with the reason) or
  Skipped.
- Importing the same or an overlapping file again makes nothing twice and retries what was left,
  for example once the bank's statement is in.
- Tie-out on the import: the rows against the header totals, and the statement balance against
  the books of that wallet's GL on that date.
- Checked on Astral's real files (counts only, never committed):
  - Utility: 3,996 rows. MMF: 44 rows.
  - Every charge row has its payout row.
  - All 21 transfers appear on both statements with opposite signs.
  - The running balances have no breaks, and the totals match the headers.

**Balance checks** (new **Mpesa Balance Snapshot**)
- Every Account Balance result records each wallet's balance. The newest row of every imported
  statement is the anchor.
- A balance is compared with that anchor plus the wallet's lines since, by completion time. It is
  checked 10 minutes after it was taken, once no payout on that shortcode is in flight.
- Outcomes:
  - Agrees: the balance matches.
  - Gap: with the amount, meaning money moved that has no line yet.
  - Cannot Check: no statement imported yet, or lines without a completion time in between.
  - Explained: set when a later import closes a gap.
- "Ask Safaricom for Balances Every (Minutes)" on the new M-Pesa Accounts tab sends the query on a
  schedule. It is 0 (off) by default and needs an initiator with the Balance Query role.

**Also fixed**: live B2B results send `DebitPartyAffectedAccountBalance` ("Working Account|KES|…")
and `DebitAccountCurrentBalance`, not Daraja's documented `DebitAccountBalance`. Mpesa B2B Payment
Request's Debit Account Balance was empty on the two live payments; it now stores the account name
and balance.

Settings (M-Pesa Suite Settings → new tab **M-Pesa Accounts**): "Submit Transfer and Funding Entries"
(default on), "Match Bank Lines Within (Days)" (3), "Ask Safaricom for Balances Every (Minutes)" (0).

**Dry run on the Astral copy (astral.local, data to 2 Oct 2026)**

The real 4139683 MMF and Utility statements (31 Jul – 22 Oct) were imported in one transaction
and rolled back; table counts were identical before and after.

- As Astral is today (one Bank Account for 4139683, Working not mapped):
  - Transfers: the 21 transfers between the wallets were skipped, since nothing left the account.
  - Funding: 13 of the 19 fundings (KES 4.875M) were reconciled with the Journal Entries staff
    already book ("Transfer to B2C", Dr Mpesa-4139683 / Cr KCB or M Oriental).
  - Funding, 5 Aug: 2 fundings were left because their Journal Entries are already matched to the
    old hand-made "M-Pesa Float Top-Up" lines (ACC-BTN-2026-08768/08769), which are the same money.
  - Funding, after the copy: 4 fundings are later than the copy's data.
  - Payouts: 1,471 were found reconciled; 526 are after the copy's data.
  - Charges: 1,974 charges are right. 1,551 new charge lines and Journal Entries total KES 12,765
    (Aug 1,323, Sep 6,396, Oct 5,046) that Astral's feed never booked.
  - Wrong old charge lines, 4 left for staff: 48.02 and 219.02 on payouts Safaricom did not charge
    (entries ACC-JV-2026-05158 and -05280), and 4 and 205.40 where Safaricom charged 5.
- With Working on a Bank Account and GL of its own:
  - Transfers: the 21 transfers book Internal Transfers between the two GLs.
  - Funding: every funding is left, naming the Journal Entry that put the money into
    Mpesa-4139683 instead of Working's GL.
  - Importing those months as they stand would therefore move KES 7.55M into Working twice over:
    Working's GL would end at −7.55M and Mpesa-4139683 at +7.55M too much.

**To use it at Astral**
1. Go live as today, with Working not mapped. Import each wallet's statement (MMF first, then
   Utility), then cancel the duplicates and the four wrong charge lines the import lists.
2. To give Working its own Bank Account and GL, pick a cut-over date and post Working's opening
   balance with a Journal Entry. From that date book each funding Dr Working / Cr bank (the import
   then matches it), and import statements from that date on, not earlier months.
3. Optionally set the balance schedule (for example 60 minutes) once the initiator can query
   balances.

Tests:
- Unit, `tests/unit/test_statement_file.py`: the parser on synthetic files in the real layout,
  every kind rule, and refusal of the combined export.
- Unit, `tests/unit/test_wallet_helpers.py`: balance texts, Account Balance parsing, the
  receiving wallet and row keys.
- Bench, `wallet/test_wallet.py`:
  - mapping rules, and no change without a mapping;
  - B2B on Working and B2C on Utility;
  - a Working statement with funding paired to the bank's line and a transfer to Utility;
  - the Utility side of that transfer imported again;
  - exact charges and a payout made on the portal;
  - a transfer inside one Bank Account;
  - funding waiting for its bank line, and two candidate bank lines;
  - the combined export refused;
  - balance Gap closed by an import, Agrees, and Cannot Check.

## Rename to Mpsa Suite (2026-10-22, branch mpsa-suite)

The app is renamed: package `frappe_mpsa_payments` → `mpsa_suite`, title "Astral M-Pesa Suite" →
"Mpsa Suite", module "Frappe Mpsa Payments" → "Mpsa Suite". DocType names and tables, roles,
secrets, Payment Gateway names, the site URL and `/pay/<token>` stay. The `m_pesa_api` and
`mpesa_settings` path segments stay too. Frappe has no rename, so each site hands over from one
app to the other: install `mpsa_suite` next to the old app, then uninstall the old app. Notes and
the v16 test evidence: [docs/RENAME_SPIKE.md](docs/RENAME_SPIKE.md). The headings above keep the
old names as history.

**Rename commit (generated)**
- `scripts/do_rename.py` run on `02d94a0` (release C, `ee5b201`, adds only the R3 flag fix,
  which this branch replaces with R2, and a CI line this branch already has): folders and every code and
  config mention renamed (165 files, 739 replacements), the `/assets/mpsa_suite/` paths, the
  dashboard folder `mpsa_suite_dashboard/`, and `pyproject.toml` name `mpsa_suite` so both
  packages can sit in one bench.
- `modified` bumped on the 14 shipped Reports, Dashboard, Dashboard Charts, Number Cards,
  Workspace, Workspace Sidebar and Desktop Icon, so a plain migrate files them under the new module.
- **Old URLs keep working.** `override_whitelisted_methods` maps all 70 old
  `/api/method/frappe_mpsa_payments...` paths (29 guest: the 18 Safaricom callbacks, the Payments
  API, the pay page) to the same function in mpsa_suite. Safaricom holds the registered C2B and
  Pull URLs under the old paths, so the table stays for good.
- **Hand-over** (`mpsa_suite/handover.py`): `rehome()` files every record still under the old
  module (DocTypes, Custom Fields, Property Setters, Reports, site-made Server and Client Scripts,
  ...) and the Workspace / Desktop Icon `app` under mpsa_suite. It runs when the old app is
  uninstalled, before Frappe reads which modules to delete, and on every migrate. Any other
  non-empty module the old app owns (Navari's "Stanbic") is handed to mpsa_suite, not deleted.

**Safeguards (RENAME_PLAN.md R1–R9)**
- R1: `rehome()` also runs at the end of install-app (`after_sync`), so nothing is left in the
  old module once mpsa_suite is installed.
- R2: mpsa_suite refuses every uninstall of itself, whatever the data, unless site_config has
  `mpsa_allow_uninstall: 1`. Uninstalling it would drop every M-Pesa table, the settings and their
  secrets; it is never a rollback. The check runs even when mpsa_suite is missing from
  installed_apps.
- R3 stays in the old app only (added in `02d94a0`; in release C `ee5b201` its flag
  `mpsa_allow_old_uninstall` accepts only 1 or true).
- R4: a unit test holds the lookup table to release C: every whitelisted function of C has an
  entry, and each target exists, is whitelisted and keeps C's guest setting. A bench test imports
  every target.
- R5: the 6 Payment Entry STK fields are made on every migrate and at install; a fresh install
  used to miss them.
- R6: users' and System Settings' `default_app` move from the old app to mpsa_suite.
- R7: the rename script is kept in `scripts/`.
- R8: a patch whose old-name twin already ran (Patch Log, `skipped = 0`) does nothing, so a site
  that gets mpsa_suite without install-app (a restore, a manual fix) does not re-run them; Mpesa
  Settings' API switches are not rewritten.
- R9: CI runs on pushes to `mpsa-suite`. The linters stay on pull requests only, as on
  `astral-suite`.

**Review fixes**
- `before_install` rebuilds Frappe's module map when it lacks mpsa_suite. On a Frappe 15 bench
  whose Redis cache was warm before `bench get-app`, install-app otherwise synced no DocType and
  failed in after_install with "Module Mpsa Suite not found".
- The R4 bench test moved to `mpsa_suite/tests/`: Frappe 15's `run-tests --app` skipped it in the
  app folder itself.
- README, the Payments API, Sandbox Lab and licensing docs use the new names; the Payments API
  doc says the old base URL keeps working.

Shown on a throwaway Frappe v15 site: the hand-over with R1, R2, R3 and R5, nothing lost, the old
paths over HTTP, and the bench tests. Not yet shown: Frappe Cloud, a Navari-era site, R6 and
the R8 restore route on a real site (docs/RENAME_SPIKE.md, "Not shown yet").

## Payouts Phase 1: B2B from Purchase Invoices and Orders, pay first and make the entry later (2026-10-23)

Design: [docs/PAYOUTS_DESIGN.md](docs/PAYOUTS_DESIGN.md), Phase 1 and "Rules every flow keeps".
No patch: the new fields come from the DocType JSON, their defaults from the Suite settings
backfill, and old rows get their state in after_migrate.

**Pay a Purchase Invoice or a Purchase Order**
- A Purchase Invoice has M-Pesa > "Pay via M-Pesa (B2B)"; a Purchase Order has "Pay Advance via
  M-Pesa (B2B)". Both open a new B2B request with the supplier, company, shortcode, approved
  account and amount filled in. Both forms list the requests under Connections > M-Pesa.
- A request may link an invoice or an order, not both. An order is paid as an advance; the
  invoice made from it picks the advance up under Get Advances Paid. A draft invoice made from an
  order that holds an advance says so at the top.
- The request form shows what is left to pay and why: the outstanding, less payouts in progress
  (B2B and B2C), draft entries and advances still on the invoice's orders.
- Before an invoice is paid, M-Pesa money the supplier already got with no invoice is listed.
  Submitting is refused until someone ticks that none of it paid this invoice.

**Every check again at submit and at send**
- Submitting runs every check again, under the same per-document lock B2C uses. It used to run
  only on save, so an insert already marked submitted (the API) skipped them, typed paybill included.
- Sending checks once more, right before the money leaves, and changes nothing: the approved
  account, a one-off destination (switch and a manager), the shortcode's switch, limit and
  company, the supplier's hold, the document, and what is left to pay.
- What is left to pay counts B2B and B2C together: a B2B request in flight or Paid without a
  submitted entry on the document, B2C payouts, draft entries and order advances. B2C's own
  guards now count B2B requests too. Only a submitted entry allocated to the document frees it.
- Refused: an invoice on hold (whatever its release date), a debit note, not KES, not submitted;
  an order closed, on hold, completed or already invoiced; a supplier on hold for payments.

**When M-Pesa confirms: "When Paid"**
- "Book automatically" (the default) makes the Payment Entry at once: on the invoice (by payment
  term when the template says so), on the order, or as an advance to the supplier when nothing is
  linked. "Make the entry later" books nothing.
- The entry is built by hand: it always pays from the payout's own M-Pesa account (an invoice's
  Mode of Payment no longer swaps it), on the M-Pesa completion date, with the receipt as its
  reference and without the charge, so it reconciles with the payout's statement line. It is
  booked as Administrator, so a restricted Integration User does not stop it.
- If the invoice was paid (all or part of it) or put on hold before M-Pesa confirmed, what it
  cannot take is booked as an unallocated advance, marked "Needs Checking" and the Notify Role
  is alerted.
- The new Accounting Entry section says where each Paid payment stands: Entry Not Made, Draft
  Entry, Needs Checking or Entry Made, with a note in plain words. The list shows "Paid, no
  entry", "Paid, draft entry", "Paid, check entry" or "Paid". The M-Pesa workspace has a
  shortcut to the ones that need an entry.
- **Make Entry** (Accounts users with ERPNext's own permissions): against an invoice, an order,
  as a supplier advance, or as a Journal Entry to an expense or bank account. It is refused when
  an entry already carries the receipt. Moving a payment to another document or supplier, and
  an entry for a cancelled request, need a manager. **Check Entry Again** looks again.
- One entry per receipt: an entry staff made by hand with the receipt (Bank Reconciliation's
  Create Voucher too) is found and linked, never booked twice. An entry a request links can be
  cancelled, or deleted as a draft; the request then shows it and the payment counts against
  its document again.

**The 15-minute sweep**
- It looks again at every Paid request whose entry is not made, not submitted or changed. A
  failed automatic booking is tried again for 30 days. Requests paid before this release are only
  looked at, never booked.
- "Remind When a Paid B2B Has No Entry After (Days)" (Money Out tab, default 3, 0 = never) alerts
  the Notify Role once per request.

**Paid after it was cancelled**
- A late Result on a cancelled Unknown request is recorded, because the money left. It gets its
  statement line and charge, keeps counting against its document and is never booked
  automatically: it shows "Needs Checking" and the Notify Role is alerted. A manager makes the entry.

**Smaller fixes**
- Journal Entries on v16 (the charge entry and Make Entry) keep our remark.
- A payout's statement line names its order when it paid one.

Tests: unit tests for every rule (`tests/unit/test_b2b_owed.py`, `test_b2b_entries.py`, and
additions to the money-out, statement, supplier account and B2C guard tests; their SQL runs on
SQLite). Bench tests for the booking routes in `mpsa_suite/money_out/test_b2b_documents.py`.
Passed on the v15 and v16 benches (with the review fixes). Not built: the Sandbox Lab scenarios b2b_pay_order and
b2b_pay_first_then_entry.

**Review fixes (round 1)**
- B2C's guards (disbursement submit and retry) take off an advance still on the invoice's orders,
  as the B2B check does. A B2B advance on an order followed by a B2C payment of the invoice made
  from it, without Get Advances, is refused.
- A request is not counted against itself as an advance still on its own order (a draft moved
  from the order to its invoice, or Make Entry booking an order's payout on that invoice).
- The Result marks the request (entry_attempted_on), not the booking job. A job that is lost or
  fails before it commits (a lock timeout, Redis down) is booked by the next sweep.
- A possible double payment and a payment after the request was cancelled are always alerted.
  Only a failed automatic booking and the reminder share the once-per-request alert.
- When staff cancel or delete a request's entry, the request switches to "Make the entry later"
  and says so. The sweep no longer books it again on the same document.
- A second payer on a document that is being paid is told "Another payment for ... is being sent
  or booked. Try again in a minute." instead of a lock name.
- The desk's Cancel on a Payment or Journal Entry a request links no longer offers "Cancel All"
  (which failed on the Paid request): `auto_cancel_exempted_doctypes`. A Purchase Invoice or
  Order a request links gets Frappe's plain "is linked" refusal.
- The request's company always follows its invoice or order (Frappe fills a new request's
  company from the user's default). "Pay via M-Pesa (B2B)" passes the company too. Sending also
  refuses a stored company that is not the document's.
- Sandbox Lab b2b_pay ticks the earlier-payouts box for its own test supplier, and its check
  reads the Accounting Entry state (Payment or Journal Entry).
- An invoice paid in part another way before the Result (a bank payment, a debit note) is no
  longer booked silently as "Entry Made": the part it cannot take stays an unallocated advance,
  the request shows "Needs Checking" with the reason, gets a comment and the Notify Role is
  alerted. The same when Make Entry leaves part of a payment off the chosen invoice or order. A
  request linked to a document stays "Needs Checking" while any of it is unallocated; only a
  request with no document is "Entry Made" as a supplier advance.
- B2C refuses to pay a Purchase Order that already has invoices (disbursement submit and retry),
  as B2B does: paying an invoice does not change its order's advance paid, so the order would
  still look unpaid. Its invoices are paid instead.
- A save after submit (a form opened before the Result, the API, a script) keeps what the server
  wrote: status, the ack and Result fields, the receipt and the Accounting Entry fields.
- A cancelled request that may have reached M-Pesa (Initiated, Paid, Unknown, or with an ack or
  receipt) cannot be deleted: it is the record of that payment.
- A manager can cancel an Initiated request whose Result never came, after checking the
  statement, as for Unknown. A Result that comes later is still recorded and counts.
- A Result that meets a deadlock or a lock held past MariaDB's wait is recorded again from a job
  (up to 5 tries) instead of being lost. A Result handler error now logs the Result itself, so it
  can be replayed by hand.
- Sandbox Lab b2b_pay skips with the reason when sending is refused at submit.
- Bench tests: requests use the test company (ERPNext's v15 test setup makes another company
  the default); commits inside the code under test are replaced, so each test really rolls
  back; leftovers an interrupted run committed for the test supplier are removed first.

**Review fixes (round 2)**
- A change for B2C users: B2C, like B2B, no longer pays a further advance on a Purchase Order
  that has any invoice, even one for part of the order. Pay the Purchase Invoice instead: an
  advance still on its order is taken off what it owes. The release C bench test that paid a
  second advance on a partly invoiced order now expects this refusal.
- Bench test fix: an empty Accounting Entry status reads back as "", not None.

## Payouts Phase 2: M-Pesa Payment Schedule and the M-Pesa Payments Due report (2026-10-23)

Design: [docs/PAYOUTS_DESIGN.md](docs/PAYOUTS_DESIGN.md), Phase 2 and "Rules every flow keeps".
Suppliers and payees not saved on a supplier, B2B only. No patch: the new DocTypes, fields and
the report come from JSON, the two new settings from the Suite settings backfill.

**Accounts prepare, a Payer pays, nothing is paid automatically**
- New **M-Pesa Payment Schedule** (MPS-YY-MM-####): a title, the company, Pay From (B2B) and
  lines. Each line has Pay On, the supplier and its approved M-Pesa account (the default one
  when left empty), the amount in whole shillings, optionally the Purchase Invoice or Order it
  pays, When Paid and remarks. The paybill or till is copied from the approved account.
  Accounts users and managers create and change schedules; M-Pesa Payers and Viewers read them.
- A number not saved on a supplier (a utility's paybill) can be a line only when "Allow B2B to
  Numbers Not Saved on a Supplier" is on, and only a manager enters it or pays it.
- A line's Pay On cannot be before today when it is added or moved. Accounts move planned lines
  to another day (Move Date) until they are sent; each move is noted on the schedule with the
  old and new day.
- Once a line is sent it cannot be changed or deleted. The status, request, receipt and the
  other fields the server writes are always taken from the database on save.
- What is planned for an invoice or order counts in what it has left to pay: a schedule cannot
  plan more than is left, and a direct B2B request (or another schedule) cannot pay what a
  schedule has planned. The B2B request form's headline shows the planned amount.

**Pay Selected, with a 5-second confirm**
- New Script Report **M-Pesa Payments Due** (M-Pesa workspace and sidebar, and Pay Due Lines on
  the schedule): due today and overdue, the next 7 days, in progress or all, by company, route,
  Pay From and schedule. Each line shows its Pay On (with an Overdue badge), payee, route,
  paybill or till, document and what it has left, amount, status and why it cannot be paid now.
  Cards show each shortcode's Working balance (approximate, with its age), what is due, what you
  can pay and the selected total.
- Tick lines (or all of them) and press **Pay Selected**: a summary per payee and per shortcode
  and route, the total, the balance against the selected total with a warning when it is short
  or old, and the lines left out with the reason. **Confirm stays disabled for 5 seconds** with
  a countdown, and the server refuses a confirm under 5 seconds too: the summary's token is
  bound to the user, used once and lives 10 minutes. A line changed since the summary is refused.
- **Never before the planned day**: Pay Selected, the request's submit, a console send and the
  sending job all refuse a line before its Pay On (the site's date). Paying any time on the day
  is fine. A request that carries a schedule line is accepted only from Pay Selected.
- **Maker-checker**: who prepared or last changed a line (including moving its date) cannot pay
  it. "Who Pays a Scheduled Line Must Not Be Who Prepared It" (Money Out tab) is on by default;
  turn it off only when one person does both jobs.
- Confirm submits a B2B request per line (every B2B check again, under the document's lock) but
  sends nothing itself. A job sends them one at a time ("Seconds Between Scheduled Payments",
  default 2). Before each call to M-Pesa the request is marked and that is committed, so a
  request is never sent twice.
- The line follows its request: Sending, Sent, Paid (with the receipt and the accounting entry
  state), Failed or Unknown. A failed line is planned again with **Plan Again** and paid again
  with Pay Selected. An Unknown one may have been paid: a manager checks the statement and
  cancels its request first.
- **Refresh Balances** asks Safaricom for the shortcodes' balances (finance roles). **Close
  Schedule** cancels the planned lines; payments already sent carry on. A schedule with any
  payment sent cannot be deleted.

**The 5-minute sweep**
- A request whose send began more than 10 minutes ago and never recorded an answer becomes
  Unknown ("Sending was interrupted after it may have reached M-Pesa. Check the statement before
  paying again."), is never sent again, and the Notify Role is alerted.
- A confirmed request whose job never started is sent again within the hour; after an hour it
  is cancelled (it cannot have reached M-Pesa) and its line is planned again.
- Lines that missed news of their request are brought up to date.

**Approximate balances** (`wallet.balances.available`): the newest of the last Account Balance
result, the last B2B Result's balance and the last B2C Result's available funds, less the
payouts of that shortcode sent since. Labelled approximate everywhere.

Tests: `tests/unit/test_schedule.py` (the date rule on every route with the site's day mocked,
the schedule-line check, saved-line rules, double planning and direct payments, maker-checker on
and off, the token, the summary, confirm to send to Result to booking, Plan Again, Move Date,
Close, the sweep, balances, the report). Bench tests in
`mpsa_suite/money_out/test_payment_schedule.py`, passed on the v15 and v16 benches. Not
built: the write-off of balances under KES 1 (decision 5, optional); B2C phone lines, employees
and payroll (Phase 3).

**Review fixes (round 1)**
- A schedule line is changed or removed only on its schedule. Saving or deleting a line by
  itself (the REST API on the line, frappe.client.save) is refused, so the schedule's checks
  always run. A row carrying the name of another schedule's line is refused too.
- The confirm summary shows where each amount goes: one row per payee and paybill or till, with
  the route and the supplier's ID, or "not saved on a supplier".
- The Payments Due report clears its ticks whenever it redraws (after Pay Selected, Move Date,
  Plan Again, a filter or Refresh Balances), so only lines ticked on screen are paid or moved.
- A request whose send began and never recorded an answer may have been paid: only a manager
  cancels it, and it is kept as Unknown; it cannot be deleted. A line whose request was
  cancelled that way is planned again by a manager only.
- The sending job takes the invoice's or order's lock first, as every other payment path does,
  so two payments of one invoice no longer make the second fail with "being sent or booked". It
  claims a request with a locking read, and a failed check never overwrites an answer or a
  cancel committed meanwhile.
- The sweep re-queues requests not yet started only when their batch's job is no longer queued
  or running, and the job waits between payments only after one it actually sent.

## Payouts Phase 3: employees and payroll (2026-10-23)

Design: [docs/PAYOUTS_DESIGN.md](docs/PAYOUTS_DESIGN.md), Phase 3 and "Rules every flow keeps".
Salaries go to employees' banks through the bank's paybill (B2B) and to their phones (B2C), so
bulk salaries need no bank file. HRMS is required (hooks.py `required_apps = ["erpnext", "hrms"]`;
both sites run it). No patch: the new DocType and fields come from JSON, the HRMS fields from
after_migrate (also run by after_install), the new setting's default from the Suite settings
backfill.

**Employee M-Pesa Account** (MEA-#####, Employee > M-Pesa, M-Pesa workspace)
- An employee's approved destination: a **Phone (B2C)** (the Safaricom line in their name) or a
  **Bank Paybill (B2B)** (the bank's paybill, the employee's account number at that bank, the bank).
- Approved like a Supplier M-Pesa Account: one person enters it with the proof (HR roles may), a
  different manager (M-Pesa, Accounts or System Manager) submits it, it can be paid only after
  "Hold New Employee M-Pesa Accounts (Hours)" (M-Pesa Suite Settings, default 24), one default per
  employee, approved details change only by cancel and amend.
- After each payment the name M-Pesa paid is checked against "Name M-Pesa Shows": the name the
  phone is registered in, or the bank's name for a bank paybill (M-Pesa names the bank, not the
  account holder, so the proof and the second approver are what confirm whose account it is). A
  mismatch blocks the account until a manager presses Receiver Name Confirmed.
- The approval, checks and name check are now shared with Supplier M-Pesa Accounts
  (money_out/party_accounts.py); supplier_accounts keeps its names as thin wrappers.

**B2B to an employee**
- A B2B request may pay an employee instead of a supplier (never both): to the employee's
  approved bank paybill account only (a phone account is refused: phones are paid by B2C).
- With a Salary Slip (HRMS) it cannot pay more than the slip has left (its rounded total less
  what M-Pesa and bank entries paid, less payouts on their way and planned lines), checked again
  at submit and at send under the slip's lock, the same lock B2C takes. When M-Pesa confirms, the
  salary Journal Entry is booked. Without a slip it is "Make the entry later": Make Entry offers
  Salary Slip or Journal Entry.

**Employees on the M-Pesa Payment Schedule**
- A line may pay an employee: its route comes from the employee's default approved account. A
  bank paybill line is paid by a B2B request (as for suppliers). A phone line is paid by B2C: Pay
  Selected makes one B2C Payment Disbursement per schedule, B2C shortcode, party type, document
  type and Payroll Entry; each row carries its line and the approved phone, and the row refuses
  any other number or amount. B2C's own guard and jobs then make and send each payout.
- A phone line pays a Salary Slip, and the schedule gets **Pay From (B2C)**.
- Every Phase 2 rule holds on both routes: prepared by Accounts and paid by someone else
  (maker-checker), Pay Selected with the per-payee summary and the 5-second confirm, and never
  before the line's Pay On: Pay Selected, the job that makes each B2C payout and the B2C send all
  refuse earlier (the job fails the row, nothing sent, and the line can be planned again).
- A phone line follows its B2C payout: Sending, Sent, Paid (with its receipt and whether the
  salary entry is made), Failed or Unknown (a Transaction Status check settles it). A failed line
  is planned again with Plan Again; the B2C disbursement's own Retry is refused for it. A line
  still Sending an hour after Pay Selected (a lost job) is failed by the 5-minute sweep only when
  nothing can have been sent (see the review fixes below).
- The M-Pesa Payments Due report shows phone lines too, with a card for the shortcode's Utility
  balance beside the Working one, and filters by route B2C Phone.

**Pay a payroll: "Create M-Pesa Payment Schedule" on a submitted Payroll Entry** (HRMS only)
- Builds one schedule for the payroll: a line per submitted, not withheld Salary Slip of an active
  employee of the same company, for what the slip still has to pay in whole shillings, to the
  employee's default approved account, on the Pay On chosen in the dialog with Pay From (B2B) and
  (B2C). The schedule shows the Payroll Entry, each line its Salary Slip, and lists the employees
  left out with the reason (no approved account, several and none default, withheld, left, more
  than B2C pays at once, no shortcode picked). The Payroll Entry's Connections show it.
- At most one live (not closed) schedule per payroll, and none while a bank or cash entry not
  made by M-Pesa exists for it (HRMS's Make Bank Entry). Every payment of one of its salaries
  checks that again (see the review fixes below): HRMS offers Make Bank Entry until a bank entry
  references the payroll, and its entry may be a draft or name no employee.
- The button needs read on the Payroll Entry (HRMS gives it to HR Managers) and a finance role
  that may create a schedule (Accounts User or Manager, M-Pesa Manager). That person is the
  lines' preparer, so someone else pays them.

**The salary Journal Entry** (both routes, for salaries paid from a schedule or a B2B salary request)
- Bank Entry with the receipt as Cheque No, posted and dated on the M-Pesa completion date: Dr the
  Payroll Entry's payroll payable account (reference the Payroll Entry, its cost center) for what
  the salary still had on it, party Employee only when Payroll Settings books payroll per
  employee; the difference left by whole shillings (at most 0.50 with rounded totals) to the
  company's Round Off account; Cr the payout's own M-Pesa account (Working for B2B, Utility for
  B2C). A part payment debits what was paid. A salary paid twice is booked and flagged "Needs
  Checking" with an alert.
- Never booked twice: the B2B booking looks for the receipt under its lock; the B2C booking now
  also finds an entry with no employee on it by its receipt and its disbursement tag.
- What a Salary Slip has been paid now counts Paid B2B salaries too, and never counts their
  entries (tagged with the payout: new Journal Entry Account field "M-Pesa B2B Payment Request").
  B2C's guards (disbursement submit and retry) count B2B salaries on their way.
- B2C salaries paid by hand from a B2C Payment Disbursement keep their existing entry.

**A Payroll Entry with M-Pesa salaries cannot be cancelled**
- Refused while any M-Pesa salary of it is Paid or on its way (HRMS would cancel their entries and
  delete the slips they paid), and while a payment schedule still plans its slips (delete that
  schedule first; nothing was paid from it).

**Smaller changes**
- Payment schedule lines: Party Type Employee, route B2C Phone, document Salary Slip, and the
  read-only B2C Disbursement and B2C Request fields. The Payroll Entry, purpose and the left-out
  list of a schedule are set only by the Payroll Entry's button and kept on every save.
- Statement lines of an employee's B2B payout name the employee and the salary slip.
- HRMS sites get two custom fields on the B2B request: Salary Slip and Payroll Entry.

Tests: `tests/unit/test_employee_accounts.py`, `tests/unit/test_salary_entry.py`,
`tests/unit/test_payroll_schedule.py` (employee lines and their checks, both routes through Pay
Selected, the date rule and maker-checker on the B2C path, the number lock, partial failures, the
line following its B2C payout, Plan Again, the lost-job sweep, the Payroll Entry button and its
exclusions, the cancel guard, B2C's guard seeing B2B, the Utility card). HRMS is required
(`required_apps`), so nothing is tested for a site without it.
Bench tests in `mpsa_suite/money_out/test_payroll_payouts.py` (a 4-employee payroll paid by B2B
and B2C with employee-wise accounting on and off, never booked twice, a failed phone salary
planned again, the cancel guard): 357 bench tests pass on v15 (HRMS 15.64.5) and v16 (HRMS 16.52.0)
after review round 1. Not built: "Phone
(B2C)" on Supplier M-Pesa Account (optional); Sandbox Lab scenarios for employees.

### Payouts Phase 3 review fixes (round 1)

**Never paid both by bank and by M-Pesa**
- A salary is not paid by M-Pesa while its Payroll Entry has a bank or cash entry not made by
  M-Pesa, draft or submitted, whether or not it names the employee: checked on the schedule form,
  at Pay Selected, at a B2B salary's submit and send, by B2C's guards (submit and retry) and by
  the job that makes a scheduled B2C payout and its send. M-Pesa's own salary entries are tagged
  with their payout and do not count.

**A scheduled B2C payout is sent once (R1)**
- The job commits the B2C Disbursement Request, submitted but not sent, with its row's link, then
  sends it: the send commits a "Send Started On" marker on the request (new hidden field) before
  the call and checks the date, the row's and line's own payout and the approved phone again.
- The 5-minute sweep makes a marked request still Not Initiated after 10 minutes Status Unknown
  (a Transaction Status check settles it; its line is Unknown). The hour rule fails a line only
  when its row has no payout or its payout never started.
- Only an M-Pesa or Accounts Manager cancels a scheduled B2C payout that may have reached M-Pesa,
  and it keeps its status (Status Unknown when its send began); its line is then Unknown and only
  a manager plans it again. A Paid one is never cancelled or deleted. A cancelled disbursement
  sends nothing, a row is paid by one payout only, and a request typed by hand for a scheduled
  row is refused.
- B2C's guards take off lines a payment schedule plans for the document (R3), so a planned salary
  is paid by Pay Selected, not by a disbursement made by hand.

**Who approves, who pays, who sees**
- An M-Pesa account is approved by someone other than who created it, who entered or last
  changed its details (new field "Details Entered By", checked again at submit, where the details
  are cleaned and checked again too), and, for an employee's account, the employee. One phone or
  bank account is approved for one employee only.
- Nobody pays themselves (the employee's User ID): refused at Pay Selected and on a B2B request.
  A payment to an employee with no Salary Slip, which nothing caps, is submitted and paid by an
  M-Pesa or Accounts Manager.
- Salaries are shown only to who may read Salary Slips (HR Manager, HR User or a role given read
  on Salary Slip), pay them (M-Pesa Payer, Manager) or manage: payroll schedules and schedules
  with employee lines, B2B requests to employees and the report's employee lines. M-Pesa Viewer
  and M-Pesa Payer no longer read Employee M-Pesa Accounts.
- A Salary Slip cannot be cancelled while an M-Pesa payment of it is Paid (also a cancelled one)
  or on its way: an amended slip would show its whole net pay as unpaid again.

Tests: `tests/unit/test_fix_payroll.py`, `tests/unit/test_employee_accounts.py`,
`tests/unit/test_fix3.py`, `tests/unit/test_fix_b2creq.py`, `tests/unit/test_fix_payouts.py`.

planned again, the cancel guard; the employee B2B and approval tests also run on a site without
HRMS), not yet run on the v15 and v16 benches. Not built: "Phone (B2C)" on Supplier M-Pesa
Account (optional); Sandbox Lab scenarios for employees.

## Roles and permissions: strict, by role, checked on the server (2026-10-23, branch roles-permissions)

Audit, model, live rollout and the per-role checks: [docs/ROLES_AND_PERMISSIONS.md](docs/ROLES_AND_PERMISSIONS.md).
No patch: DocType and report permissions come from JSON; Role Profiles, credential-field access and
the report of a site's own rules run in `after_migrate` (`mpsa_suite/access.py`).

**Who may do what is data, and tested**
- `mpsa_suite/permission_matrix.py` lists every DocType's rights per role, the report roles, the
  credential fields and the server check behind every whitelisted endpoint. Unit tests fail when
  the JSON, the code or the doc drift from it, or when a whitelisted function has no check.
- The suite's `require()` checks roles itself: `frappe.only_for`, used before, lets everyone through
  in Frappe 15 bench tests, so no role gate had ever refused anyone there.

**Fixed**
- `/api/v2/method/<DocType>/<function>` ran the STK, balance, status and Pull registration functions
  without their role gate (any logged-in user, portal users too); every route is now recognised.
- Mpesa Settings credentials (Consumer Key, Initiator Name, Security Credential, Access Token and the
  passwords) are on permission level 1: System Manager and M-Pesa Manager only. No export, print or
  email of Mpesa Settings below them. The browser no longer reads those fields.
- Form buttons that ran for anyone who could read (balance query, STK re-send, B2C disbursement
  helpers and status) and helpers open to every logged-in user now check permissions.
- The suite roles get the DocType rights their jobs need (Payer on B2C, Collector on STK, Manager
  everywhere, Viewer read-only). Below M-Pesa Manager, Mpesa Settings can be picked in a Link field
  but not opened, listed or found in the sidebar.
- Frappe refuses a report row whose Link column the reader cannot read: M-Pesa Payments Due's
  hidden Currency column is now Data, so Payers and Viewers see their lines on Frappe 15 too. The
  four upstream reports show ERPNext documents and stay with the ERPNext roles that read them.
- A save of a submitted B2C payout through the form or the API keeps the status, send marker and
  result M-Pesa gave it. Before, one payer could save a paid scheduled payout as Failed or never
  sent, and the schedule would let it be paid again.

**Decided by the user**
- Only M-Pesa Payer, M-Pesa Manager and System Manager send money (B2C, B2B, reversals, retries,
  Pay Selected): B2C now checks this at submit. Accounts Manager and HR Manager prepare drafts.
- M-Pesa admin work (approving supplier and employee payee accounts, one-off destinations, C2B/Pull registration, settling
  Unknown payouts, the Sandbox Lab) is for M-Pesa Manager and System Manager only.
- Maker-checker on direct payouts: who sends a B2B request or B2C disbursement must not be who
  created or last saved it, nor change it while sending. One switch with the payment schedule:
  "Who Sends a Payout Must Not Be Who Prepared It" (on by default).

- Salary payouts (each employee's net pay, phone and bank account) are shown only to HR User and
  HR Manager, who read Salary Slips, to M-Pesa Payer, M-Pesa Manager and System Manager, and to
  whoever created the record: payroll schedules and their lines, B2C payouts and B2B requests to an
  employee, their status queries, Employee M-Pesa Accounts, and the salary rows of M-Pesa Payments
  Due and B2C Disbursement Summary. Accounts roles and M-Pesa Viewer see supplier payouts only.
  One rule in `money_out/salary_access.py` (payouts Phase 3's and this one, unified when merging
  it); a role a site gave read on Salary Slip sees them too.

**A site's own permission rules (Custom DocPerm)**
- Frappe uses them instead of the app's rules. Trident and Astral both have them on seven M-Pesa
  DocTypes. They are reported once per change (Error Log) and never deleted automatically. An M-Pesa
  admin compares and resets them in M-Pesa Suite Settings > Permissions > Check Permissions.
- Role Profiles M-Pesa Finance Clerk, Paying Officer, Approver, Cashier and Auditor are created once.
  On Frappe 15 a Role Profile replaces all of a user's roles: use them only for M-Pesa-only users.
