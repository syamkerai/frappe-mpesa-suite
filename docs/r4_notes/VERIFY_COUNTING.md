# Round 4 verify pass, counting group (K1, K2, K13, K14)

Fixes from the verify pass of `payouts-r4` (81cc821), branch `r4-verify-counting`. What a user notices, one finding per section.

## K1: a B2C payout of a claim or advance booked only in part
- A B2C payout of an Expense Claim or Employee Advance can be booked in part. This happens when the document was paid another way while the payout was on its way, for example a bank Payment Entry. Its Payment Entry then takes only what HRMS still says is owed, and keeps the rest unallocated.
- That unallocated rest now still counts against the document, by amount, as a B2B payout's rest already did. If staff then cancel the other entry, HRMS shows the document owing that amount again. Pay employee (phone or bank), the list's Pay by M-Pesa, a B2C or B2B send, and the retry of a failed B2C payout now see the money as already paid. They offer nothing, or refuse. Before, they offered it, and the employee could be paid twice.
- The count drops by itself once the rest is allocated to the document on that same entry, for example with Payment Reconciliation.
- A payout booked whole is unchanged. So are payouts of invoices, orders and salary slips.

## K2: a payee line paid outside M-Pesa
- A submitted Expense Claim has a new button, M-Pesa > Settle Payee Outside M-Pesa. It shows only to a named M-Pesa Manager who can read the claim, and only while a payee line is Draft or Failed. It still shows when paying merchants and payees is switched off.
- The manager picks the row and says how it was paid, for example the bank Payment Entry. The row becomes Cancelled. Its note reads "Settled outside M-Pesa by <user>: <reason>", and a Comment on the claim records it.
- Its amount is no longer held back from the employee: Pay employee and the payout checks offer the claim's rest again. Before, a line that M-Pesa would never pay held the employee's share for good.
- M-Pesa never pays a settled row afterwards:
  - Pay Merchants and Payees and Retry Failed Payees skip it;
  - a copy of its old request is refused at save and at send;
  - the retry of its failed B2C payout is refused.
- Settling is refused while any payout of the row is queued (a draft too), on its way, paid, or in doubt. In doubt includes an Unknown request a manager cancelled. Other roles are refused, and so is the shared Administrator login while M-Pesa Suite Settings refuses it (the default).

## K13: cancelling a claim while a payee payout may have paid
- An Expense Claim can no longer be cancelled while a payout of one of its payee lines may have paid. This covers a request a manager cancelled while it was Unknown or Initiated, which its line still shows as In Flight. The message names that payout.
- Before, such a claim could be cancelled and amended. The amended claim then paid the merchant a second time.
- The claim can be cancelled once M-Pesa's answer says the payout failed, from a status query or a late Result. Employee Advances and claims without payee lines are unchanged.

## K14: Cancel on a claim or advance whose M-Pesa payout failed
- Cancel on an Expense Claim or Employee Advance form no longer lists the document's M-Pesa payouts in Frappe's "linked with the following submitted documents" dialog. These are B2C Payment Disbursements, B2C Disbursement Requests and B2B requests, and the dialog could never cancel them.
- A claim or advance whose only payout failed is now cancelled from the form, to correct a mistyped phone, till or account and amend it. Before, the dialog either said "You do not have permissions to cancel all linked documents", or offered Cancel All, which then failed.
- The server still refuses the cancel while a payout is paid or on its way, with its own message.
