# Setup and runbook notes

Notes for setting up a shortcode and checking a live site. They come from Trident's live test of
27 Oct 2026 (docs/TRIDENT_LIVE_TEST_HANDOVER.md) and hold for every site.

## Setup

**Payment Gateway Account stays "Is Default" (F9).** Saving Mpesa Settings creates the shortcode's
Payment Gateway Account, and the Mode of Payment that B2C and B2B payouts use is built from it. When
it is a company's only gateway account, ERPNext makes it the default again on every save, so the box
cannot be unticked. On a shortcode that only pays out (no STK, no C2B) this does no harm while nobody
makes Payment Requests. Leave it.

**The Integration User needs no document access (F1).** Safaricom's callbacks run as the Integration
User (M-Pesa Suite Settings, Callback Security). Every booking they start runs as Administrator or
without permission checks: B2B and B2C Payment Entries, STK payments, C2B receipts, statement lines
and their reconciliation. So a User Permission on Project, Cost Center or Company (Trident gives one
to most users) no longer stops a payment from being booked. Give the Integration User no roles beyond those
docs/ROLES_AND_PERMISSIONS.md names, and no User Permissions are needed for it.

**Limits (F7).** Mpesa Settings, Transaction Limit (0 = no limit), and M-Pesa Suite Settings, Minimum
B2C Payout, now cap every B2C payout. They are checked when a B2C Payment Disbursement row is saved
and again when each payout is sent, as they already were for B2B and the payment schedule. A limit
lowered for a test (Trident used 100) refuses every larger payout until it is raised.

**Cost centers (F5).**
- A payout's Payment Entry (B2C or B2B) books to the paid document's cost center, then Mpesa
  Settings' Cost Center, then the company default. It carries the document's project and accounting
  dimensions, so a payable clears on the cost center and project it was booked on.
- A Safaricom charge has no document. Both rows of its Journal Entry book to the shortcode's cost
  center.
- A salary payout's Journal Entry books to the Payroll Entry's cost center.

## Daily checks (runbook)

**Error Log (F10).**
- Connector dry runs (`validate_only`) write "Document Creation Error" rows; they are not the app's.
  When checking for "no M-Pesa errors", exclude that title.
- Look at titles containing M-Pesa, Mpesa, B2C, C2B or STK.
- An answered Transaction Status query no longer writes an Error Log, whatever the answer. Only a
  result that answers no query of ours is logged ("M-Pesa Transaction Status result rejected").

**Till (Buy Goods) payments (F2).**
- What a till's Result lacks: Safaricom's Result names no receiver, states no charge and gives no
  time. The suite asks Safaricom for the receipt's Transaction Status at once and fills all three,
  usually within a second; Completed At is flagged "estimated" until then.
- If the answer is late: the 5-minute sweep asks again (up to 3 times in 48 hours), and anyone with
  a payout or admin role can press **Check Status** on the request.
- If the charge is still missing: the charge is the fall in the Working balance since the balance
  before, when that is certain. Otherwise the Mpesa Match Log has a "Charge not stated" row: take
  the charge from the statement and book it by hand.

**Check Transaction Status on Mpesa Settings (F11).** It answers for any receipt of the shortcode:
- a customer payment is recorded as a C2B receipt;
- a B2C or B2B payout shows its status, receiver, charge and time (and a B2B gets what its Result
  left out);
- a payment made from the shortcode that no ERP payout holds is a warning, with a Mpesa Match Log
  row. Accounts book it.

**Balance snapshots (F12).** With balances asked for on a schedule (M-Pesa Suite Settings, Ask
Safaricom for Balances Every) and no statement imported, each Mpesa Balance Snapshot is compared with
the sub-account's balance before it:
- **Agrees**: the ERP's payouts, charges and receipts in between explain the movement.
- **Gap**: they do not. The note gives both movements and the difference, and Notify Role is
  alerted. Usually it is a payment made on the M-Pesa portal, a funding, or a sweep to the bank. Book
  it, or import the statement that holds it.
- **Explained**: the same amount moved the other way on another sub-account in the same result. That
  is a transfer between them (Working and Utility). Book an Internal Transfer only if the two
  sub-accounts post to different Bank Accounts.
- **Warning** (Reserved): money held by Safaricom while no ERP payout is in flight is a payment
  pending outside the ERP. Find out what it is before the next payout test.
