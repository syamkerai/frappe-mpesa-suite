# Plan: paying supplier tills: the Petty Cash Phone (main) and Pay via Staff (strict)

Companion to [BULK_PAYOUT_PLAN.md](BULK_PAYOUT_PLAN.md). Both are built in the same round, on branch `r4-5-bulk-payout`.

## Context
Some suppliers are paid only at their till: petrol stations, hardware shops. The suite's B2B till payments are not reliable for these, and many staff buy from the same supplier.

Your decisions (13 Nov 2026):
- **The normal and best method is the company's petty cash phone.** It is topped up by M-Pesa from the company shortcode, and the cash handler pays the tills from it. Each till payment is recorded on its PO or PI with the M-Pesa receipt from the phone's SMS.
- **Pay via Staff** (money to a chosen staff member's own phone) **stays strict**: only you and Janki may use it.

## 1. The Petty Cash Phone (main method)

### Setup: new DocType "M-Pesa Petty Cash Phone"
- **Fields:**
  - company and phone (2547…, unique);
  - holder: the cash handler (Employee, and their User);
  - petty cash account: a Cash-type GL account, e.g. "Petty Cash M-Pesa 0722… - AIL";
  - default cost center;
  - top-up limit: the most one top-up may be;
  - float ceiling: the most the phone should hold;
  - enabled.
- **Approval:** an M-Pesa Manager or Accounts Manager approves the phone, as Employee M-Pesa Accounts are approved today. The name M-Pesa shows for the number is checked on its first top-up. Once approved, the phone number is locked; changing it needs a new approval.
- A company may have several phones (one per site, say: Isinya, Mombasa).

### Top up
- **Top Up by M-Pesa** on the phone's form:
  - amount, defaulting to the ceiling minus the phone's book balance;
  - refused above the top-up limit, or when the book balance would pass the ceiling.
  - It makes one B2C payout to the phone. That is a **party-less** B2C row, the same new branch as the bulk sheet's rows. An M-Pesa Payer sends it with the usual Send via M-Pesa (maker-checker as in settings).
  - The phone's **holder can't send a top-up to their own phone.**
- **Booking:** one Journal Entry per top-up when paid: debit the petty cash account, credit the shortcode's Utility GL, with the receipt as `cheque_no`. The bank statement line reconciles one to one (the existing `statement.entries_for` rule).
- **Delays and failures:** as for every B2C payout today: late Results, Transaction Status queries for payouts that never got one, Retry Failed only after M-Pesa says failed, and the unbooked sweep.

### Spends: the till payments
- **Paid from Petty Cash Phone** is a button on a submitted Purchase Order and on an unpaid Purchase Invoice. Its dialog asks for:
  - the petty cash phone (defaults to the one the user holds, or the company's only one);
  - **"Paste the M-Pesa SMS"**: the confirmation text is read for the receipt, the amount, the till or paybill name, the date and time, and the phone's new balance (e.g. "UKD1ABC2DE Confirmed. Ksh1,500.00 paid to SHELL KILIMANI. on 13/11/26 at 10:15 AM. New M-PESA balance is Ksh8,500.00"). The fields can also be typed by hand.
  - an optional transaction charge (a paybill's), and an optional screenshot attachment.
- **It books a submitted Payment Entry:**
  - Pay, to the supplier, from the petty cash account, for the amount;
  - `reference_no` = the receipt, `reference_date` = the SMS date;
  - against the PO (an advance) or allocated to the PI;
  - a charge goes to the charges account as a deduction.
- **Checks:**
  - the receipt is used only once;
  - the amount is no more than the PO or PI still owes;
  - the amount is no more than the phone's book balance (refused, so that the phone's record must be complete);
  - the till or paybill name on the SMS is shown next to the supplier's name, so a mismatch is visible;
  - only the phone's holder or Accounts roles may record.
- **The PO and PI** get an M-Pesa section "Paid from petty cash phone" listing each Payment Entry, its receipt and the phone.

### Watching the phone (in the shared daily "needs attention" alert)
For each phone:
- **book balance** (top-ups minus recorded spends);
- **the last SMS balance** (from the latest recorded SMS), with any difference from the book balance flagged: a spend not recorded, or a typo;
- days since the last top-up and since the last spend;
- a phone holding money with no spend recorded for `petty_cash_alert_days` (default 7).

## 2. Pay via Staff (strict: you and Janki only)
- **What it does:** as first planned. **Pay via Staff** on a submitted PO or an unpaid PI creates a submitted Employee Advance:
  - for a chosen staff member, to their approved Employee M-Pesa Account (never a typed phone);
  - with custom fields `mpesa_purchase_order` / `mpesa_purchase_invoice` and `mpesa_supplier`.
  It is paid through the existing Employee Advance B2C path.
- **Who may use it:** only the users listed in the new M-Pesa Suite Settings field **Pay via Staff Users**. That is a table of Users, which you fill with yourself and Janki. Empty means nobody.
  - The listed users may make the advance and send it.
  - The button shows only to them; the server refuses everyone else, both at the advance and at the send.
- **Self-pay:** a listed user still can't pay an advance to themselves (the existing `check_not_own_b2c`).
- **Settlement:** when the supplier's PI is submitted, `settle_staff_advances(pi)` posts one Journal Entry:
  - debit the supplier against the PI;
  - credit the employee advance account against the Employee Advance;
  - capped at the PI's outstanding.
  A manual **Settle Staff Advances** button does the same. An overpaid advance goes to HRMS Return/Claim.
- **Follow-up:** advances paid more than `staff_advance_settle_days` ago (default 3) and not settled appear in the daily alert.

## Shared with the bulk payout sheet
- **The party-less B2C branch** (bulk sheet rows and petty-cash top-ups):
  - a row link on the B2C Payment Disbursement Reference (`bulk_payout_row`, `petty_cash_phone`);
  - a booking dispatch per reference type (the bulk sheet's journal; one top-up journal per payout);
  - the `booked_docstatus` branches.
- `money_out/self_pay.py`: the self-pay helper.
- **The daily "needs attention" alert:** one job, three sections (bulk sheets, petty cash phones, staff advances).

## Settings (M-Pesa Suite Settings)
- `pay_via_staff_users`: a Table MultiSelect of User (child DocType "M-Pesa Pay via Staff User"). Empty means nobody.
- `staff_advance_settle_days`: default 3.
- `petty_cash_alert_days`: default 7.

Defaults are filled in on upgraded sites by `install.backfill_single_defaults`.

## Files
- **New:**
  - `mpsa_suite/mpsa_suite/doctype/m_pesa_petty_cash_phone/` (json, py, js, bench test);
  - `mpsa_suite/mpsa_suite/doctype/m_pesa_pay_via_staff_user/` (child table);
  - `money_out/petty_cash.py`: approve, `top_up`, `record_spend` (whitelisted), `read_sms`, book balance, the alert section;
  - `money_out/pay_via_staff.py`: `make_advance`, `settle_staff_advances`, the alert section;
  - `public/js/purchase_mpesa_tills.js`: the PO and PI buttons, through hooks `doctype_js`;
  - unit tests `tests/unit/test_r45_petty_cash.py` and `test_r45_pay_via_staff.py`; bench tests next to the modules.
- **Changed:**
  - `migrate.py`: the Employee Advance custom fields and the PO/PI section;
  - `hooks.py`: doctype_js, the Purchase Invoice `on_submit` settle event, the daily scheduler entry;
  - the Suite Settings JSON;
  - `permission_matrix.py` and `docs/ROLES_AND_PERMISSIONS.md`:
    - Petty Cash Phone: create/approve by M-Pesa Manager or Accounts Manager;
    - top up: preparer roles;
    - record spend: the holder or Accounts roles;
    - Pay via Staff endpoints: the listed users only;
  - `SUITE_CHANGES.md`.

## Verification
- **Unit tests:**
  - SMS reading: Safaricom's till, paybill and send-money formats, and wrong text;
  - spend checks: receipt used once, amount above what is owed, amount above the book balance, holder or role;
  - top-up limits and the holder's own phone;
  - the Pay via Staff user list, at the button, the advance and the send;
  - the settle journal lines and capping;
  - the alert sections.
- **Bench tests on v15 and v16:**
  - set up and approve a phone;
  - top up → send (`send_for_test`) → Result Paid → journal posted and the statement line reconciled;
  - record two spends on a PO and a PI from pasted SMS texts → Payment Entries, the PO advance and the PI paid;
  - the book balance after the spends; an SMS balance mismatch flagged;
  - a failed top-up, then Retry;
  - Pay via Staff refused for a user not on the list and allowed for a listed one, then the PI settle journal.
- **Rollout:**
  - Trident pilot: set up its petty cash phone, one top-up, one petrol purchase recorded from its SMS;
  - then Astral, one phone per site.
