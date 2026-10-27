# WP7: Trident live-test fixes

The fixes for what Trident's live M-Pesa test on 27 Oct 2026 found (docs/TRIDENT_LIVE_TEST_HANDOVER.md,
plan section 2.F): F1, F2.1 to F2.4, F4, F5, F7, F11 and F12. F9 and F10 need no code; they are
notes in `docs/OPERATIONS_NOTES.md`. F8 was already right.

## What changed
- **F1, bookings as Administrator.** The STK booking job (`money_in/stk_verify.book`) and the C2B
  register's auto-submit (`after_insert`) now run as Administrator, as the B2B booking and the
  2-minute matcher already did. Before, they ran as the callback's Integration User: ERPNext's
  `get_payment_entry` and an invoice's submit check that user's permission, so an order or invoice
  hidden by a User Permission (Trident: Project) failed on the first try. Each failure left an Error
  Log and waited for the sweep. Staff reconciling by hand keep their own permissions.
- **F2.1, a till's balance.** A Buy Goods Result sends only
  `{Amount={CurrencyCode=KES, MinimumAmount=189400, BasicAmount=1894.00}}`. It is now stored as
  `Working Account|KES|1894.00|1894.00` (B2B always pays from Working). The available-funds figure
  and the charge then use it. The Integration Request keeps the raw text. Helpers:
  `daraja/money_out.basic_amount` and `b2b_balance_text`.
- **F2.2, a charge Safaricom did not state.** A B2B with blank `DebitPartyCharges` gets its charge
  from the fall in the Working balance: the Paid B2B just before (or a later Working Account
  Balance) less this Result's balance less the amount. Live: 1,947 to 1,894 for 50 is 3. Same
  sanity rules as B2C: no other B2B in flight or without a balance in between, a charge from 1 to
  100 in whole shillings. A charge that is stated, including a stated 0, still wins. When neither
  the balance nor a status query can give it, the Mpesa Match Log gets one row ("Charge not
  stated"). The statement sweep keeps looking for a blank charge; it skips only a stated 0.
- **F2.3, completed time.** A Result with no `TransCompletedTime` (a till) is dated when it arrived.
  It is flagged with the new hidden field `completed_time_estimated` on Mpesa B2B Payment Request
  (a server field, so a save after submit cannot change it). The status query replaces it with
  Safaricom's `FinalisedTime`.
- **F2.4, Transaction Status for B2B** (new `money_out/b2b_status.py`):
  - What starts a query: a Paid Result with no receiver name or no charge queries its receipt from
    a job (`ask_later`). A 5-minute sweep (`money_in.events.every_five_minutes`) asks again while
    something is missing: at most 3 queries, 10 minutes apart, within 48 hours. There is also a
    **Check Status** button on Paid, Initiated and Unknown requests, through the new endpoint
    `money_out.api.check_b2b_status`, which needs a PAYOUT or ADMIN role and read on the request.
  - The query is a **Mpesa B2C Status Query**. It now has `b2b_request`, `debit_party_charges` and
    `debit_account_type` fields, and `b2c_request` is mandatory only when there is no
    `b2b_request`. The query goes through the existing `b2c_status_result` and
    `status_queue_timeout` callbacks.
  - When an answer counts: it must be Completed for the payout's amount, its `ReceiptNo` must be
    the payout's receipt (the top-level `TransactionID` is a placeholder), its own ConversationID
    or OriginatorConversationID must be the payout's, and the `CreditPartyName` shortcode must be
    the payout's receiver. Any other answer is Unresolved and changes nothing.
  - What the answer fills: only blank fields (receiver name, charge, and the time when it was
    blank or estimated). Then the receiver-name check runs, through the existing
    `record_receiver_name`, and the charge line and charge Journal Entry are made.
  - A request whose Result never came (Initiated or Unknown) is settled by the answer exactly as
    its Result would have been: Paid and booked, or Failed. A late real Result is then ignored.
- **F4.** A B2C Payment Disbursement row whose document owes nothing (paid, or not submitted) is
  refused at save: "Row #n: <doctype> <name> has nothing left to pay." An amended copy of a row
  that was sent already still saves.
- **F5, one cost-center rule** (`money_out/common.payout_cost_center`):
  - The order is the paid document's cost center, then Mpesa Settings' Cost Center, then the
    company default. A cost center of another company is skipped.
  - B2C Payment Entries now follow it. Before, they always took the company default. They also
    copy the document's project and accounting dimensions (`document_dimensions`). B2B Payment
    Entries use the same helper and now copy the project too.
  - Both rows of a charge Journal Entry now take the shortcode's cost center. Before, the credit
    row had none.
  - Both rows of a B2C salary Journal Entry take the Payroll Entry's cost center, else the helper.
- **F7.** Mpesa Settings' Transaction Limit (0 = no limit) and M-Pesa Suite Settings' Minimum B2C
  Payout (never below 10) now cap every B2C payout. They are checked on each B2C Payment
  Disbursement row at save, and again on each payout when it is sent. Before, only payment
  schedule lines were capped, and the Minimum B2C Payout was never read.
- **F11, Check Transaction Status on Mpesa Settings and the C2B register list.**
  - Event names: Python now publishes `mpesa_transaction_status_update`, the event the desk
    listens for (constant `m_pesa_api.STATUS_EVENT`). It goes to the person who asked (the owner
    of the query's Integration Request), not to the Integration User. Every outcome is
    published, so the waiting dialog closes. An ack is published as "queued" and leaves the
    dialog open.
  - Payout receipts: a receipt that belongs to a B2C or B2B payout is answered with the payout's
    status, receiver, charge and time, and a B2B gets what its Result left out. It is no longer
    rejected or logged as an error.
  - Money the ERP did not send: a receipt paid from our shortcode with no ERP payout is flagged
    as a warning and in the Mpesa Match Log. This is how the KES 100 portal payment would have
    shown.
  - Error Log: only results that answer no query of ours are logged.
  - The race on the second live query: the ack's commit came after Safaricom's answer (0.4 s).
    The ResultURL now carries `ref=<the query's Integration Request>`. The handler reads that
    request with a locking read, which waits for the ack. A result whose ack is not recorded yet
    stays Queued and is tried again every two minutes for 10 minutes
    (`m_pesa_api.retry_waiting_status_results`, from `money_in.events.every_two_minutes`).
    `b2c_status.send_query` (B2C and B2B) commits the query before sending it and again right
    after the ack. `results.find_request` matches by IDs read again under the lock.
  - Mpesa Settings form: its realtime listeners are no longer added again on every refresh.
- **F12, balance snapshots.**
  - New fields on Mpesa Balance Snapshot: `available`, `reserved`, `previous_snapshot`,
    `erp_movement` and `warning`.
  - With no statement imported, a balance is compared with the sub-account's balance before it
    (within 7 days). The ERP's records must explain the movement: Paid B2B with their charges
    leave Working, Paid B2C with their charges (booked, else the B2C Charge Bands) leave Utility,
    and C2B and STK receipts arrive in the sub-account that collects. An unexplained difference is
    a **Gap** with a note, and Notify Role is alerted. The same difference in opposite directions
    on two sub-accounts of one result is a transfer between them: Explained.
  - A reserved amount while no ERP payout is in flight gives a warning and an alert.
  - The check waits while a B2B charge may still come from its status query.

## Settings added
None. The new behaviour uses existing settings: Transaction Limit, Minimum B2C Payout, Notify
Role, B2C Charge Bands and the Mpesa Settings Cost Center.

## Behaviour changes for users
- B2C Payment Disbursement refuses at save:
  - a row whose document owes nothing (before, only Submit refused it);
  - a row above the shortcode's Transaction Limit or below the Minimum B2C Payout.
- Sends refuse the same amounts. On a site with Transaction Limit 100 (Trident during the test),
  B2C above 100 is now refused: raise the limit before paying more.
- B2C Payment Entries book to the paid document's cost center (or the shortcode's), not the
  company default, and carry the document's project. Charge Journal Entries put both rows on the
  shortcode's cost center. If accounts want every M-Pesa entry on the shortcode's cost center,
  that needs a setting (not built).
- Till payments: the receiver, charge and completed time appear about a second after the Result,
  from the status query. The receiver-name check now covers tills. A wrong till blocks its
  account as a wrong paybill does.
- Mpesa Settings, Check Transaction Status: the dialog closes with the answer. A payout receipt
  shows the payout's details instead of "was not paid to".
- Balance snapshots without a statement show Agrees, Gap or Explained instead of "Cannot Check".
  A Gap alerts Notify Role.

## Migration notes
- No patch. The migrate syncs the new fields on Mpesa B2B Payment Request, Mpesa B2C Status
  Query and Mpesa Balance Snapshot. Existing rows read 0 or blank: an older till payment is
  not flagged as estimated.
- Status queries already sent before the release have no `ref` on their ResultURL. Their results
  are still matched by the ack's ConversationID, as before.
- The 5-minute B2B status sweep asks only about payouts Paid in the last 48 hours. Older till
  payments with a blank charge are logged once in the Mpesa Match Log for Accounts.
- Other work packages: `salary_entries.build` (schedule salary Journal Entries) still uses the
  Payroll Entry's cost center, else the company default. It does not use `payout_cost_center` yet.

## Tests
- Unit, 1797 passed (baseline 1724):
  - new `tests/unit/test_trident_fixes.py` covers F1, F2.1, F2.2, F2.3, F4, F5 and F7;
  - new `tests/unit/test_b2b_status.py` covers F2.4 and F11 with the live 35rkf224m5 answer, plus
    a JS/Python event-name contract test;
  - new `tests/unit/test_balance_movement.py` covers F12 with Trident's live balances;
  - new `tests/unit/rich_db.py` is a fake DB with list filters and ordering;
  - `fake_frappe.FakeDB` gained a no-op `commit`;
  - `test_payout_statement.py` now expects a blank B2B charge to be looked for, and only a stated
    0 to give no charge line. `test_fix2_b2c2.py` checks both salary JE rows' cost center.
- Bench (not run here). None of them submits or approves as Administrator: requests are written
  as their submit and ack leave them.
  - `money_out/test_trident_fixes.py`:
    - F1, an Integration User hidden by a User Permission on Project;
    - F2.2 to F2.4, a till Result, its status query and answer: name check, CHG line and JE, and
      a mismatching name blocking the account;
    - F5, cost centers and project;
    - F11, the live answer through `process_mpesa_integration_request`;
    - F4 and F7.
  - `money_in/test_integration_user.py`: F1, an STK payment on a Sales Order with a project the
    callback user cannot see books on the first attempt; a C2B receipt is auto-submitted for a
    callback user with no document rights.
  - `wallet/test_balance_movement.py`: F12, Agrees, Gap and the reserved warning.
