# Trident: round 4 upgrade and first live payments

A step-by-step checklist for upgrading Trident Plumbers LTD to the round 4 M-Pesa Suite and making
its first live payments with it.
- What round 4 changes: [SUITE_CHANGES.md](../SUITE_CHANGES.md), "Round 4", with its UPGRADE part.
- Who may do what: [ROLES_AND_PERMISSIONS.md](ROLES_AND_PERMISSIONS.md).
- What the October live test found: [../../docs/TRIDENT_LIVE_TEST_HANDOVER.md](../../docs/TRIDENT_LIVE_TEST_HANDOVER.md).
- What a read-only pre-flight of the live site (28 Oct 2026) predicts the upgrade will change:
  [../../docs/ROUND4_PREFLIGHT_TRIDENT.md](../../docs/ROUND4_PREFLIGHT_TRIDENT.md). Its findings are
  folded into the steps below.

Trident today:
- Frappe 15.122, with the app still `frappe_mpsa_payments` (release C);
- shortcode **985401**, a paybill with B2C and B2B only (no STK, no C2B);
- Transaction Limit 100;
- site permission rules (Custom DocPerm) on seven M-Pesa DocTypes;
- 12 System Managers, all of them also Accounts Managers;
- an active Payment Entry workflow;
- Expense Claims that require a project.

How to read each step: **Who** does it, **Do** what, **Check** what must be true before the next
step. Tick the box when done. Stop at the first surprise: do not resend money, check the statement
first, and ask the M-Pesa Manager.

## The people

Fill in names before you start. One person may hold several of these jobs, but the code refuses:
- a person sending a payout they prepared or last changed;
- a person approving a payee account they entered, or one that pays themselves;
- anyone paying their own claim or salary.

| Job | Roles on the site | Name |
|---|---|---|
| IT | System Manager (configuration only: sends nothing, approves nothing) | |
| M-Pesa Manager | M-Pesa Manager (approves payee accounts, M-Pesa settings). Not the payer | |
| Payer (one or two) | M-Pesa Payer | |
| Preparer | Accounts Manager + HR User. Accounts Manager makes B2C and B2B drafts and schedules; HR User reads claims and sees salaries | |
| HR | HR Manager or HR User (enters employees' M-Pesa accounts and claims). Only HR Manager makes Payroll Entries on Trident (E3) | |
| Expense Approver | Expense Approver (approves claims and their payees) | |
| Accountant | Accounts Manager (entries, reconciliation, the cost-center decision) | |
| Pilot staff | two or three employees with a Safaricom line, for the test claims | |
| Test employee | an employee with a test salary, for the one-off salary | |
| Casual payees | two people who are **not** employees, with a Safaricom line and their consent | |

## A. Before the upgrade (a day or more ahead)

- [ ] **A1. Write down who pays.**
  - Who: the finance lead, with IT.
  - Do: fill in the table above.
  - Check: the payer and the M-Pesa Manager are different people, and the preparer is not the
    payer.
- [ ] **A2. Named logins.**
  - Who: IT.
  - Do: give every person in the table their own login. Nobody does M-Pesa work as Administrator
    any more: after the upgrade the shared login is refused for sends and approvals.
  - Check: each person can log in.
- [ ] **A3. Give the roles now, before the upgrade.**
  - Who: IT.
  - Do: User > Roles: tick M-Pesa Payer for the payers, M-Pesa Manager for the M-Pesa Manager, and
    Accounts Manager + HR User for the preparer. These roles already exist on release C, so they
    can be given today.
  - Why now: after the migrate, System Manager and Administrator no longer send or approve. Without
    named payers nobody could pay.
  - Check: each user's role list; each user logs out and in.
- [ ] **A4. Project access.**
  - Who: IT.
  - Do: Trident's "User Project Access Control" Server Script hides every project document (Expense
    Claims too) from users without project rights. Give the payer, the preparer, the M-Pesa Manager
    and the Expense Approver `__All Project Access__` or the projects they handle.
  - Check: logged in as each of them, a claim with a project opens.
- [ ] **A5. Clean up the service logins (handover F6), before the deploy.**
  - Who: IT.
  - Why before: today no named person holds M-Pesa Payer or M-Pesa Manager; only two service
    logins do. `asset-hub-reader@` holds both with System Manager, Accounts Manager and HR Manager,
    and has an API key: the morning after it would be the one login that can send money and
    approve payee accounts (the Administrator refusal does not cover it).
  - Do:
    - take the M-Pesa roles off `asset-hub-reader@` (it also holds System Manager and Accounts
      Manager: keep only what that connector needs);
    - take Agent Manager off `mpesa-integration@`, which keeps only what ROLES_AND_PERMISSIONS.md
      section 7 names (Accounts User, M-Pesa Manager).
  - Check: no shared or service login holds M-Pesa Payer, and nobody keeps
    `mpesa-integration@`'s password.
- [ ] **A6. Note the starting balances.**
  - Who: the Accountant.
  - Do:
    1. Run Get Account Balance.
    2. Write down GL `1.4.7.2 - Mpesa-985401 - TPL` and the wallet (Working + Utility).
    3. List any open M-Pesa drafts.
  - Check: the gap between the GL and the wallet is the known **253** (153 opening + 100 portal
    test, handover F12), or the balance-matching entry for it is posted.
- [ ] **A7. Back up.**
  - Who: IT.
  - Do: a Frappe Cloud backup with files; download it and note the time.
  - Check: the files are downloaded and open.

## B. Deploy and migrate

- [ ] **B1. Rename first.**
  - Who: IT.
  - Do: Trident still runs `frappe_mpsa_payments`, and round 4 is `mpsa_suite`. Do the rename
    hand-over (Stage B) first, in the same window or before it
    ([RENAME_SPIKE.md](RENAME_SPIKE.md) and the rename plan).
  - Check: the site's apps list `mpsa_suite`, and the old app is gone as the rename plan says.
- [ ] **B2. Deploy round 4.**
  - Who: IT.
  - Do: deploy the release with round 4. Frappe Cloud runs the migrate.
  - Check:
    - the migrate finished without an error;
    - Error Log has "M-Pesa Suite: this site's own permission rules differ from the suite's" (this
      is expected);
    - the M-Pesa Bank Paybill list has 43 rows;
    - M-Pesa Suite Settings > Money Out shows "Payout Safety" and "Expense Claim Payees", with
      "Administrator May Not Send Payouts or Approve Accounts" ticked;
    - both supplier accounts, MSA-00001 and MSA-00002, show red "Blocked: Approve Again", each with a
      comment, and one alert went to the Notify Role (E2);
    - Mpesa Settings 985401 shows "Submit B2C Accounting Entries" ticked and read-only (C3).
- [ ] **B3. Clear the cache.**
  - Who: IT.
  - Do: the site's Clear Cache on Frappe Cloud (or `bench --site <site> clear-cache`). Everyone
    reloads the desk; people whose roles changed log out and in.
  - Check: the Expense Claim form shows an "M-Pesa" group under Connections.

## C. Permissions

- [ ] **C1. Check Permissions and Reset, straight after B3, before any other step.**
  - Who: IT or the M-Pesa Manager.
  - Why first: while a DocType has site rules, Frappe uses only those rules. Trident's rules on B2C
    Payment Disbursement, B2C Disbursement Request and Mpesa Settings name no M-Pesa role, and the
    server refuses the old ones. Until the Reset nobody can send a B2C payout, HR User and Expense
    Approver cannot make a "Pay employee (phone)" draft, and the M-Pesa Manager cannot open Mpesa
    Settings (so D1 waits for C1). B2B requests and Supplier M-Pesa Accounts have no site rules:
    round 4's rights apply to them from the migrate.
  - Do:
    1. M-Pesa Suite Settings > Permissions > **Check Permissions**. It lists the M-Pesa DocTypes
       that have site rules (Payment Gateway belongs to the payments app and is left alone). "The
       site grants, beyond the suite" shows, for example, HR Manager, Accounts Manager and System
       Manager submitting B2C.
    2. Tick those DocTypes and press **Reset Selected**.
    3. Super admin need not be added back: its 5 holders keep reading through System Manager or
       Accounts Manager, and adding it back would restore its delete right on B2C requests.
  - Check: each reset left a comment on M-Pesa Suite Settings, Check Permissions shows no
    differences, and a payer opens a B2C disbursement.
- [ ] **C2. Who reads claims.**
  - Who: IT.
  - Do: Trident's Expense Claim rules give HR Manager, HR User and Expense Approver; the preparer's
    HR User covers it, and HR User may now prepare claim payouts by phone and by bank / paybill /
    till. Employee Advance: Trident has none, so nothing to do. If advances will be paid later,
    the Expense Approver can prepare them (Expense Approver is a preparer since the final pass), or
    give the preparer read on Employee Advance. Expense Approvers also see supplier payouts on
    both payout lists (no salaries); decide whether that is acceptable for Trident's approvers.
  - Check: the preparer sees the M-Pesa buttons on an approved, unpaid claim.
- [ ] **C3. M-Pesa entries skip the Payment Entry workflow.**
  - Who: the Accountant.
  - Know: Trident's Payment Entries go through Draft, Pending Approval, Not Banked and Banked (all
    drafts) to Approved. The money of an M-Pesa payout has left already, so the suite posts every
    entry it makes at once: the Payment Entry of a B2C or B2B payout is submitted straight into
    **Approved** (Frappe sets the workflow's submitted state), and its charge Journal Entry is
    submitted. "Submit B2C Accounting Entries" on Mpesa Settings is always on and read-only now; the
    migrate switched it on. Make Entry posts the same way, so it is for someone who may submit
    Payment Entries; the button is hidden from others.
    - The entry is made already submitted, in one save: the workflow's Draft state never runs, so
      the approvers get no Workflow Action for it (none is left Open) and no Draft email.
    - A claim or invoice shows Paid as soon as its payout is booked; nobody approves M-Pesa entries.
    - A booking that cannot be submitted (a validation of the entry) is not left as a draft: the
      payout shows why it is not booked, is tried again every 15 minutes, and Make Entry (B2B) books
      it. The Notify Role is alerted once per payout and reason (B2C payouts too).
    - A payout paid on a day in a closed Accounting Period, in books closed by a Period Closing
      Voucher, or on or before Accounts Frozen Till (the last two bind Administrator too), cannot be
      booked on that day. B2B: press Make Entry and give
      a Posting Date in an open period; the entry's remark says "Paid on X, posted on Y." B2C: the
      alert says to make the entry by hand, dated in an open period, with the M-Pesa receipt as
      Reference No (a Payment Entry allocated to the claim or invoice; a salary's Journal Entry with
      the receipt as Reference Number and the employee as party): the suite counts it as the
      payout's entry. The charge's Journal Entry waits the same way (Reference Number CHG-<receipt>).
    - Drafts from the October test stay as they are: approve them through the workflow.
  - Do: tell the Payment Creators and Payment Approvers that M-Pesa entries no longer arrive in
    Pending Approval.
  - Check: the October drafts are approved or deliberately left, and the approvers know. Before the
    first payouts, check that no Accounting Period is closed for this month and that Accounts Frozen
    Till is before today.
- [ ] **C4. Try the roles.**
  - Who: IT, with each person.
  - Do: nothing is sent in this step; only look at the buttons.
    - The payer sees **Send via M-Pesa** on a draft someone else prepared.
    - A System Manager sees no Send via M-Pesa or Approve.
    - Administrator gets "Use your own login" if it presses Send.
    - The payout lists offer no Actions > Submit.
  - Check: as described; see ROLES_AND_PERMISSIONS.md section 7 for the full table.

## D. Settings

- [ ] **D1. Transaction Limit.**
  - Who: the M-Pesa Manager.
  - Do: after C1 (the M-Pesa Manager cannot open Mpesa Settings before it): Mpesa Settings 985401,
    raise Transaction Limit from 100 to the value finance agrees. First the Accountant confirms
    whether HR-EXP-2026-00011, the one old claim that is submitted, approved, unpaid and not Is
    Paid, is still owed (it may have been paid outside the ERP): it shows the M-Pesa pay buttons, and
    the new limit makes it payable.
  - Check: saved. Every B2C row above it is now refused at save and at send (F7), so 100 would
    refuse almost everything.
- [ ] **D2. Hold hours, Notify Role, Integration User.**
  - Who: the M-Pesa Manager.
  - Do: on M-Pesa Suite Settings:
    - set "Hold New Supplier M-Pesa Accounts (Hours)" back to **24** (it was 0 for the test);
    - keep "Hold New Employee M-Pesa Accounts (Hours)" at 24;
    - confirm the Notify Role reaches people who act on alerts: it is Accounts Manager, which 16
      logins hold, `asset-hub-reader@` among them. Unless the M-Pesa Manager also holds Accounts
      Manager, they miss the alerts (the migrate's block alert included): change the Notify Role or
      tell them;
    - keep "Who Sends a Payout Must Not Be Who Prepared It" on;
    - keep "Allow B2B to Numbers Not Saved on a Supplier" off.
  - Check: each change left a comment on the settings and an alert to the Notify Role.
- [ ] **D3. Approved phones only: not yet.**
  - Who: the M-Pesa Manager.
  - Do: leave "Pay Employees by B2C Only to Approved Phones" off until E4.
  - Check: it is off (that is how the upgrade leaves it).
- [ ] **D4. Merchants and casual payees.**
  - Who: the M-Pesa Manager, with HR.
  - Do:
    1. Switch on "Allow Paying Merchants and Casual Payees from Expense Claims".
    2. Set the two caps (0 = no cap). For the pilot, a cap a little above the test amounts is
       sensible.
    3. HR ticks "Paid to Merchants or Casual Payees by M-Pesa" on the Expense Claim Types the pilot
       uses.
    4. Leave "Expense Claim Workflow States That May Be Paid" blank: Trident runs no Workflow on
       claims.
  - Check: a draft claim of such a type shows the "Pay Merchants and Payees by M-Pesa" section.
- [ ] **D5. Cost centers (F5).**
  - Who: the Accountant.
  - Do: agree how payout entries book now:
    - a B2C or B2B Payment Entry books to the paid document's cost center (a claim's, an
      invoice's), else Mpesa Settings' **Project - TPL**, else the company default **Main - TPL**,
      and carries the document's project. In the October test the B2C entry took Main - TPL.
    - both rows of a charge Journal Entry go on Project - TPL;
    - a salary Journal Entry goes on the Payroll Entry's cost center.
    There is no setting yet to put every M-Pesa entry on Project - TPL.
  - Check: the Accountant agrees, or asks for that setting before the pilot.
- [ ] **D6. Bank paybill list.**
  - Who: the M-Pesa Manager.
  - Do: open M-Pesa Bank Paybill. For the banks Trident's staff and suppliers use, check the paybill
    and the account-number rule against a real account number:
    - Oriental: M-Oriental Bank, 986500, 13 digits;
    - I&M: 542542, 14 digits;
    - KCB, Equity and Co-op: no pattern.
    A wrong pattern refuses valid accounts: edit the row. Leave the four disabled rows (HBZ, Sumac,
    Choice, Kingdom) off unless the bank confirms the number.
  - Check: the rows you need are enabled and right.

## E. Accounts to approve (a day before the payments: new accounts wait 24 hours)

- [ ] **E1. Pilot staff phones.**
  - Who: HR enters; the M-Pesa Manager approves.
  - Do:
    1. HR makes an Employee M-Pesa Account, type **Phone (B2C)**, for each pilot employee and the
       test employee, with proof (for example a screenshot of the M-Pesa registration name).
    2. HR fills "Name M-Pesa Shows" as registered and saves.
    3. The M-Pesa Manager (not the person who entered it, not the employee) presses **Approve**,
       reads the summary and confirms after 5 seconds.
  - Check: each account is Approved with its hold time, and the Employee form shows it as the
    default.
- [ ] **E2. Supplier accounts (day 1, with E1).**
  - Who: the M-Pesa Manager; the Accountant; a second M-Pesa Manager.
  - Do: Astral Industries Ltd has two approved accounts, MSA-00001 (Paybill 208911) and MSA-00002
    (Till 835811). The migrate blocked **both** ("Blocked: Approve Again"): MSA-00001 was approved
    with the Administrator login although a named user entered it, and MSA-00002 was entered and
    approved by Administrator (handover F3). A blocked account cannot be paid or made the default.
    1. An M-Pesa Manager cancels both (only M-Pesa Manager may cancel these accounts now).
    2. The Accountant amends both (they normally become MSA-00001-1 and MSA-00002-1).
    3. An M-Pesa Manager who did not amend them approves both.
    4. After the 24-hour hold (D2), the Accountant presses **Make Default** on the paybill account.
    Any other account the migrate blocked is redone the same way. F2 and F3 move to the day after.
  - Check: the Supplier form shows no red "none is the default" headline, and the Supplier M-Pesa
    Account list shows no red "Blocked" indicator.
- [ ] **E3. The test employee's salary.**
  - Who: an HR Manager (Trident lets only HR Manager make Payroll Entries).
  - Do: make a one-employee Payroll Entry for the test employee (a small test salary) and submit
    its Salary Slip. Make no bank entry for this payroll.
  - Check: the slip is submitted, and the payroll has no Bank Entry.
- [ ] **E4. Switch on approved phones only.**
  - Who: the M-Pesa Manager.
  - Do: once every employee who will be paid by B2C has an approved phone, switch on "Pay Employees
    by B2C Only to Approved Phones". For the pilot it may stay off until after F5.
  - Check: a B2C draft to a pilot employee shows the approved account on its row.

## F. First live payments (small amounts, one at a time)

Each payment: the preparer prepares, a payer who did not prepare it sends, and the Accountant runs
the checks in G before the next one. Amounts KES 10 to 100.

- [ ] **F1. A small B2C: an Expense Claim to the employee's phone.**
  - Who: HR or the preparer makes the claim; the Expense Approver approves; the preparer prepares;
    a payer sends.
  - Do:
    1. A pilot employee's claim of KES 100 with a project, saved with **Is Paid off**, approved and
       submitted.
    2. The preparer: M-Pesa > **Pay employee (phone)**, which opens a draft B2C Payment
       Disbursement.
    3. A payer opens it and presses **Send via M-Pesa**. The summary shows the employee, the masked
       phone, 100, charge 0 (band 100 = 0) and who prepared it. Confirm after 5 seconds.
  - Check:
    - the B2C request is Paid with a receipt, and the name check is fine on the account;
    - a Payment Entry was made to the employee on **3.1.4 Petty Cash Accruals**, referencing the
      claim, with its project and cost center, submitted and **Approved** (C3);
    - the claim shows Paid and its headline "left 0".
- [ ] **F2. A small B2B: a Purchase Invoice to a paybill (handover R2-3).**
  - Who: the preparer; a payer.
  - Do:
    1. A test Purchase Invoice of Astral Industries Ltd for KES 50.
    2. The preparer: M-Pesa > **Pay via M-Pesa (B2B)**, to the amended paybill account (E2), once
       its hold has passed; save.
    3. A payer: Send via M-Pesa.
  - Check:
    - Paid, with a receipt;
    - "208911 - ..." matches the account;
    - charge 3 stated;
    - the Payment Entry was **booked automatically** on the invoice, submitted and Approved (F1: no
      permission error, project or not);
    - the CHG-<receipt> line and its charge Journal Entry are made, both rows on Project - TPL;
    - the invoice's outstanding fell by 50.
- [ ] **F3. An Expense Claim paid to a till (merchant pay).**
  - Who: HR (or the claimant) enters; the Expense Approver approves; the preparer; a payer.
  - Do:
    1. A pilot employee's claim of a ticked type, saved unpaid. In M-Pesa Payees, one line: Pay To
       **Till (Buy Goods)**, the merchant's name, the till number, KES 50, the expense line it pays.
       Use a merchant till, or Astral's 835811 once its amended account (E2) is approved and past
       its hold: an approved supplier account is noted and not capped.
    2. The Expense Approver approves and submits the claim.
    3. The preparer: **Pay Merchants and Payees**, which makes one draft B2B request.
    4. A payer: Send via M-Pesa.
  - Check:
    - the request is Paid;
    - within seconds the Transaction Status answer fills the receiver ("835811 - ..."), the charge
      and the time (F2.4);
    - the payee line shows Paid, its receipt and a matching name;
    - a Payment Entry reimburses the claim to the claimant on its payable account;
    - the charge line and its Journal Entry are made.
- [ ] **F4. A casual batch of 2.**
  - Who: HR enters; the Expense Approver; the preparer; a payer.
  - Do:
    1. A claim with two **Phone (B2C)** payee lines for the two casual payees (not employees: an
       employee's phone is refused), KES 10 to 20 each. Typing them or **Paste Payees** both work.
    2. Approve and submit.
    3. The preparer: **Pay Merchants and Payees**, which makes one B2C disbursement with two rows.
    4. A payer sends it once.
  - Check:
    - both rows are Paid, each line with its receipt and name;
    - the claim is reimbursed by the sum;
    - the charges follow the bands.
    If one fails: **Retry Failed Payees**, and the line is never paid twice.
- [ ] **F5. A one-off salary on the test employee.**
  - Who: the preparer (Accounts Manager + HR User); a payer.
  - Do:
    1. The preparer: on the test employee's Salary Slip, M-Pesa > **Pay by M-Pesa**. Pick the
       approved phone (B2C), Pay From 985401, Pay On today. That makes a one-line M-Pesa Payment
       Schedule.
    2. A payer: **M-Pesa Payments Due**, tick the line, **Pay Selected**, read the summary, Confirm
       after 5 seconds.
  - Check:
    - the line is Paid with a receipt;
    - a salary Journal Entry debits **3.1.3.1 Salary Payable** and credits the M-Pesa (Utility)
      account, with at most 0.50 round-off, on the Payroll Entry's cost center;
    - the slip's headline shows it paid;
    - an Accounts-only user does not see this payout.

## G. Reconciliation checks (the Accountant, after each payment and at the end of the day)

- [ ] **G1. Balance.** Refresh Balances on M-Pesa Payments Due (or Get Account Balance). The new
  Mpesa Balance Snapshot says **Agrees** (or Explained). A **Gap** is alerted: find what it is (a
  portal payment, a funding) before the next payment.
- [ ] **G2. GL against the wallet.** GL 1.4.7.2 less the wallet (Working + Utility) is still the
  gap from A6. It must not grow.
- [ ] **G3. Statement lines.** If Create Bank Transactions is on, the Bank Reconciliation Tool for
  M-Pesa 985401 shows each payout and each CHG line Reconciled once its entries are submitted.
- [ ] **G4. Entries.** Each payout's Accounting Entry is "Entry Made", submitted and Approved. The
  "B2B Paid, Entry Not Made" shortcut in the M-Pesa workspace is empty.
- [ ] **G5. Logs.** Mpesa Match Log has no "Charge not stated" row and no unexplained "not sent by
  the ERP" row. Error Log has no M-Pesa error (ignore connector "Document Creation Error" rows,
  F10).
- [ ] **G6. Documents.** The claims show Paid, the invoice's outstanding fell, and the slip shows
  paid.
- [ ] **G7. Who did what.** On every payout and approval, the preparer and the sender (or the
  enterer and the approver) are different named people, and none is Administrator.

## H. After the pilot

- [ ] **H1.** M-Pesa Manager: switch on "Pay Employees by B2C Only to Approved Phones" once the
  employees paid by B2C have approved phones (E4).
- [ ] **H2.** M-Pesa Manager: after about 10 clean payouts, set "If No Integration User" to
  **Refuse**.
- [ ] **H3.** Accountant: post the balance-matching entry for the 253 if it is still open.
- [ ] **H4.** Finance lead: train the other payers and preparers. Tell staff to save claims they
  want paid by M-Pesa as not Is Paid. They may list merchants on the claim instead of paying from
  their own phones, which saves the "M-pesa Charges" claim lines.
- [ ] **H5.** IT: if the upgrade fails before any live payment, restore the backup from A7. After
  live payments, never restore: fix forward, because a restore loses payment records.
