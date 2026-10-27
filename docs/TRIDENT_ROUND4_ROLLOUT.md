# Trident: round 4 upgrade and first live payments

A step-by-step checklist for upgrading Trident Plumbers LTD to the round 4 M-Pesa Suite and making
its first live payments with it.
- What round 4 changes: [SUITE_CHANGES.md](../SUITE_CHANGES.md), "Round 4", with its UPGRADE part.
- Who may do what: [ROLES_AND_PERMISSIONS.md](ROLES_AND_PERMISSIONS.md).
- What the October live test found: [../../docs/TRIDENT_LIVE_TEST_HANDOVER.md](../../docs/TRIDENT_LIVE_TEST_HANDOVER.md).

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
| HR | HR Manager or HR User (enters employees' M-Pesa accounts and claims) | |
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
- [ ] **A5. Clean up the service logins (handover F6).**
  - Who: IT.
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
      "Administrator May Not Send Payouts or Approve Accounts" ticked.
- [ ] **B3. Clear the cache.**
  - Who: IT.
  - Do: the site's Clear Cache on Frappe Cloud (or `bench --site <site> clear-cache`). Everyone
    reloads the desk; people whose roles changed log out and in.
  - Check: the Expense Claim form shows an "M-Pesa" group under Connections.

## C. Permissions

- [ ] **C1. Check Permissions and Reset.**
  - Who: IT or the M-Pesa Manager.
  - Do:
    1. M-Pesa Suite Settings > Permissions > **Check Permissions**. It lists the M-Pesa DocTypes
       that have site rules (Payment Gateway belongs to the payments app and is left alone). "The
       site grants, beyond the suite" shows, for example, HR Manager, Accounts Manager and System
       Manager submitting B2C.
    2. Tick those DocTypes and press **Reset Selected**.
    3. If the site wants its "Super admin" read back, add it in Role Permissions Manager.
  - Check: each reset left a comment on M-Pesa Suite Settings, and Check Permissions shows no
    differences (or only the Super admin rows you added back).
- [ ] **C2. Who reads claims.**
  - Who: IT.
  - Do: Trident's Expense Claim rules give HR Manager, HR User and Expense Approver; the preparer's
    HR User covers it. Employee Advance: Trident has none, so nothing to do. If advances will be
    paid later, give the preparer read on Employee Advance.
  - Check: the preparer sees the M-Pesa buttons on an approved, unpaid claim.
- [ ] **C3. Decide the Payment Entry workflow.**
  - Who: the Accountant.
  - Do: Trident's Payment Entries go Draft > Pending Approval (Payment Creator) > Approved (Payment
    Approver). While Mpesa Settings 985401 has "Submit B2C Accounting Entries" off (today), the
    M-Pesa Payment Entries are made as drafts and go through that workflow.
    - The claim or invoice shows Paid only once its entry is approved.
    - The draft keeps the document counted, so it cannot be paid twice meanwhile.
  - Decide: keep it off for the pilot (recommended), or switch it on after about 10 clean payouts
    (handover section 6).
  - Check: the decision is written down, and the Payment Approvers know M-Pesa entries will arrive.
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
  - Do: Mpesa Settings 985401: raise Transaction Limit from 100 to the value finance agrees.
  - Check: saved. Every B2C row above it is now refused at save and at send (F7), so 100 would
    refuse almost everything.
- [ ] **D2. Hold hours, Notify Role, Integration User.**
  - Who: the M-Pesa Manager.
  - Do: on M-Pesa Suite Settings:
    - set "Hold New Supplier M-Pesa Accounts (Hours)" back to **24** (it was 0 for the test);
    - keep "Hold New Employee M-Pesa Accounts (Hours)" at 24;
    - confirm the Notify Role reaches people who act on alerts;
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
- [ ] **E2. Supplier accounts.**
  - Who: the Accountant; the M-Pesa Manager.
  - Do: Astral Industries Ltd has two approved accounts, MSA-00001 (Paybill 208911) and MSA-00002
    (Till 835811).
    1. The Accountant presses **Make Default** on MSA-00001.
    2. Recommended: MSA-00002 was entered **and** approved by Administrator (handover F3).
       Cancel and amend it, so a named person enters it and the M-Pesa Manager approves it.
  - Check: the Supplier form shows no red "none is the default" headline.
- [ ] **E3. The test employee's salary.**
  - Who: HR.
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
      claim, with its project and cost center (a draft in the PE workflow, C3);
    - once the entry is approved, the claim shows Paid and its headline "left 0".
- [ ] **F2. A small B2B: a Purchase Invoice to a paybill (handover R2-3).**
  - Who: the preparer; a payer.
  - Do:
    1. A test Purchase Invoice of Astral Industries Ltd for KES 50.
    2. The preparer: M-Pesa > **Pay via M-Pesa (B2B)**, to MSA-00001; save.
    3. A payer: Send via M-Pesa.
  - Check:
    - Paid, with a receipt;
    - "208911 - ..." matches the account;
    - charge 3 stated;
    - the Payment Entry was **booked automatically** on the invoice (F1: no permission error,
      project or not);
    - the CHG-<receipt> line and its charge Journal Entry are made, both rows on Project - TPL;
    - the invoice's outstanding fell by 50.
- [ ] **F3. An Expense Claim paid to a till (merchant pay).**
  - Who: HR (or the claimant) enters; the Expense Approver approves; the preparer; a payer.
  - Do:
    1. A pilot employee's claim of a ticked type, saved unpaid. In M-Pesa Payees, one line: Pay To
       **Till (Buy Goods)**, the merchant's name, the till number, KES 50, the expense line it pays.
       Use a merchant till, or Astral's 835811, which is an approved supplier account and so is
       noted and not capped.
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
- [ ] **G4. Entries.** Each payout's Accounting Entry is "Entry Made" (or a draft waiting in the
  Payment Entry workflow). The "B2B Paid, Entry Not Made" shortcut in the M-Pesa workspace is
  empty.
- [ ] **G5. Logs.** Mpesa Match Log has no "Charge not stated" row and no unexplained "not sent by
  the ERP" row. Error Log has no M-Pesa error (ignore connector "Document Creation Error" rows,
  F10).
- [ ] **G6. Documents.** The claims show Paid (after their entries are approved), the invoice's
  outstanding fell, and the slip shows paid.
- [ ] **G7. Who did what.** On every payout and approval, the preparer and the sender (or the
  enterer and the approver) are different named people, and none is Administrator.

## H. After the pilot

- [ ] **H1.** M-Pesa Manager: switch on "Pay Employees by B2C Only to Approved Phones" once the
  employees paid by B2C have approved phones (E4).
- [ ] **H2.** Accountant and M-Pesa Manager: after about 10 clean payouts, decide "Submit B2C
  Accounting Entries" (C3), and set "If No Integration User" to **Refuse**.
- [ ] **H3.** Accountant: post the balance-matching entry for the 253 if it is still open.
- [ ] **H4.** Finance lead: train the other payers and preparers. Tell staff to save claims they
  want paid by M-Pesa as not Is Paid. They may list merchants on the claim instead of paying from
  their own phones, which saves the "M-pesa Charges" claim lines.
- [ ] **H5.** IT: if the upgrade fails before any live payment, restore the backup from A7. After
  live payments, never restore: fix forward, because a restore loses payment records.
