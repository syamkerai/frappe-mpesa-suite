# WP6: M-Pesa buttons and Connections on the sales side, Connections everywhere, JS fixes

Every ERPNext / HRMS form the suite pays from or collects for, and the suite's own forms, now list
the M-Pesa records that reference them under Connections. Submitted Sales Orders and Sales
Invoices can ask the customer's phone for payment through the existing Payment Request path;
Payment Requests can resend and check their prompt. Purchase Invoices and Orders show what M-Pesa
has on its way and can be added to a payment schedule. Buttons show only to who the server accepts.

## What changed
- **Connections ("M-Pesa" group)**, `money_out/connections.py` (one function per DocType, as hooked
  by WP0) and the existing `supplier_dashboard.py`, `document_dashboards.py` and the two dashboard
  functions of `payroll.py`, all through one helper, `connections.add_group`:

  | Form | M-Pesa records listed (by their field) |
  |---|---|
  | Expense Claim, Employee Advance | B2C Disbursement Request (`reference_name` + type), B2C Payment Disbursement (its rows), B2B request (`expense_claim` / `employee_advance`), Payment Schedule (its lines) |
  | Salary Slip | B2B request (`salary_slip`), B2C request (+ type), B2C payout rows, schedule lines |
  | Sales Order, Sales Invoice | C2B receipts matched to it (`matched_reference_*`), Express Requests, Payment Intents, Match Log (`reference_*`) |
  | POS Invoice | C2B receipts (`pos_invoice`), Express Requests |
  | Customer | C2B receipts, Match Log (`customer`) |
  | Payment Request | Express Requests (its prompts) |
  | Payment Entry | C2B receipt, B2B request (`payment_entry`), Express Request, Match Log, Statement Row (`entry` + type), B2C payout (by the entry's own reference rows) |
  | Journal Entry | B2B request (its own rows, else `journal_entry`), B2C payout (its own rows), Statement Row |
  | Bank Transaction | C2B receipt, Express Request, B2C request, B2B request, Statement Row (`bank_transaction`) |
  | Bank Account | Mpesa Settings, Statement Import (`bank_account`) |
  | Supplier | its M-Pesa accounts, B2B requests, B2C requests (`party` + type), B2C payout rows, schedule lines |
  | Purchase Invoice / Order | B2B requests, B2C requests (+ type), B2C payout rows, schedule lines |
  | Employee | its M-Pesa accounts, B2B requests, B2C requests (+ type), B2C payout rows, schedule lines |
  | Payroll Entry | Payment Schedule (+ `payroll_doctype`), B2B salary requests, B2C payout rows (`payroll_entry`) |

  Suite forms got `<doctype>_dashboard.py` files: Mpesa Settings (what each shortcode sent,
  received and reconciled), B2C Disbursement Request (status queries, its payout, statement line),
  Mpesa B2B Payment Request (Payment / Journal Entry, statement line, the schedule that sent it),
  Mpesa C2B Payment Register (reversals, match log, entry, statement line), Mpesa Express Request
  (match log, pay link, statement line), Mpesa Payment Intent (its prompts, webhook deliveries),
  Supplier / Employee M-Pesa Account (B2B requests and schedule lines paid to it), M-Pesa Payment
  Schedule (also the B2C requests its phone lines became).
- **Request Payment** (Sales Order, submitted non-POS Sales Invoice, group M-Pesa): a dialog with
  the customer's phone (the document's contact mobile, else the customer's), the amount (what is
  still owed, editable down, never above) and the shortcode when the company has several. New
  `money_in/collect.py`: `collect_options` and `request_payment` (POST). It makes ERPNext's own
  Payment Request (`make_payment_request`, Phone channel, the company's M-Pesa Payment Gateway
  Account) for the amount and submits it, which prompts the phone through Mpesa Settings.
  `request_for_payment`: no new STK route. The server checks COLLECT_ROLES, read on the order or
  invoice, create on Payment Request, a submitted KES document (not a return, not POS, an order not
  Closed / On Hold / Completed / fully billed), at most what is owed less what open Payment
  Requests still ask for, at most the shortcode's Transaction Limit (250,000 at most), a Kenyan
  mobile number, at most 5 prompts a minute per user, and no new prompt while the document's last
  one is under 90 seconds old and unanswered. A draft Payment Request of the document must be
  submitted or deleted first. Pay links (Payment Intents) need an API client, so they are not
  offered from the desk.
- **Payment Request form**: *Resend M-Pesa Prompt* (the last prompt again through
  `www.mpesa.stkpush.retry_stkpush`, which refuses a paid or just-sent one; a new prompt through
  `create_new_request` when the last was paid or none was made) and *Check M-Pesa Status*
  (`m_pesa_api.check_transaction_status`), for COLLECT_ROLES, on submitted M-Pesa requests.
- **Customer form**: "M-Pesa received in 90 days: KES ... (n payments)" from its booked C2B
  receipts (read only, for who can read them).
- **Purchase Invoice / Order**: *Add to M-Pesa Payment Schedule* (pick an open schedule of the
  company or start a new one; the line is a Supplier line for the document, for what is left to
  pay; the schedule form saves and checks it) for who may create schedules, and the headline
  "M-Pesa: KES x on its way, KES y planned on a payment schedule. Left to pay: KES z." from
  `b2b.amount_left`, for who can read B2B requests.
- **List indicators**: B2C Payment Disbursement, B2C Disbursement Request (Status Unknown,
  Initiated, Paid but not booked...), Mpesa Reversal Request, and Mpesa C2B Payment Register
  (Unmatched, Customer Matched, Matched, Booked, On Invoice).
- **Buttons shown only to who the server accepts** (`frappe.boot.mpsa`): Sales Invoice STK /
  C2B buttons and the payments-row STK button, Express Request actions (`can_collect`; Reconcile
  `can_approve`), Reverse Payment and Check Status with M-Pesa (`can_pay` or `can_approve`),
  Retry Payment / Retry Failed Transactions (`can_pay`), Supplier "Add M-Pesa Account" (create
  on Supplier M-Pesa Account, not just read).
- **JS fixes**: `utils.js` "Request Payment" in the C2B dialog no longer throws on the invoice desk
  (it asks for the Mode of Payment there); `mpesa_express_request.js` showed an undefined `msg`
  after a re-prompt, and added one more realtime listener on every refresh;
  `b2c_payment_disbursement.js` never set Paid To for a DocType without its own default account,
  and never sent "Less Than Amount" (a key written twice); `sales_invoice.js` lost the dead
  `insert_payment_entry` (it hard-coded "Cash") and its Fetch Mpesa Payments list shows only the
  invoice company's draft receipts of the customer or of no customer yet.

## How Connections are counted, verified on both versions
Read on the benches (`frappe/desk/notifications.py`, `frappe/model/meta.py`,
`frappe/desk/form/meta.py`, `form/dashboard.js`, `list_view.js`; Frappe 15 at d2efa79, 16 at
4515014):
- The count filter is `{field: name}`; a field only a child table has is matched in the first child
  table that has it (v15 `get_filter`, v16 the query builder), so payout rows and schedule lines
  work as plain fields.
- `dynamic_links` are keyed by the field, not by the source, and filter only sources whose parent
  has the type field. So `reference_name -> [Expense Claim, reference_doctype]` narrows the B2C
  request and leaves HRMS's Payment / Journal Entry links unchanged; on Sales Order / Invoice and
  Purchase Invoice / Order it also narrows ERPNext's Payment Request link to that DocType, and on
  Employee HRMS's Bank Account link to party type Employee (both more exact). The list a click opens
  applies the type filter only where a field (or child field) exists.
- Counts are `frappe.get_all` (no permission checks) on v15 and v16; v16's `get_list` path is used
  only for a field several child tables of the source share, which none of ours does. The document
  must be readable on both. The Connections themselves are cached once per DocType for all users.
  So, only inside the counting request, a source the user cannot read, and salary DocTypes on
  Employee, Payroll Entry, Salary Slip, Expense Claim and Employee Advance for who may not see
  salaries, are counted on `name` (no count). Bank Transaction and Mpesa Settings counts are not
  masked (one statement line the reader already sees; a per-shortcode total).
- Internal-only sources (a payout found by the document's own rows) name `name` as their external
  field, so the fallback count is 0 and a click opens an empty list, not every record.
- Not expressible in Connections (one field per source): the B2B / B2C charge line's Bank
  Transaction (`charge_bank_transaction`), Mpesa Settings' sub-account bank accounts, the payment
  schedule on Mpesa Settings (two shortcode fields), C2B receipts put on an invoice by POS quick pay
  (`sales_invoice`; the Sales Invoice lists matched ones), a B2C request's Payment / Journal Entry
  (they reference the payout: its Accounting Entries).

## Settings added
None.

## Behaviour changes for users
- Users without COLLECT_ROLES no longer see the Sales Invoice STK / C2B buttons or the Express
  Request actions; users who are not payers (or M-Pesa admins) no longer see Reverse Payment, Check
  Status with M-Pesa or the Retry buttons; read-only roles no longer see "Add M-Pesa Account" on a
  Supplier. The server refused them all already.
- Request Payment needs Payment Request create permission (Accounts User and Accounts Manager by
  default): give Sales User or M-Pesa Collector that right in Role Permission Manager if cashiers
  should ask for payment from orders. The PR prompt needs the payments app, as every Payment Request
  prompt does.

## Migration notes
- No patch, no new field. Clear the cache after deploying (Connections are cached per DocType).
- The new endpoints are in `permission_matrix.ENDPOINTS` and the regenerated
  `docs/ROLES_AND_PERMISSIONS.md`.

## Tests
- Unit: `tests/unit/test_r4_connections.py` (the count filter and table Frappe uses for every
  dashboard / source pair, transcribed for v15 and v16, against the real DocType JSON and migrate's
  custom fields; ERPNext / HRMS links kept; click filters; masking only while counting),
  `tests/unit/test_r4_collect.py` (allow-list, roles, permissions, owed / held, limit, phone,
  shortcode, rate limit, recent prompt, drafts, the Payment Request made), and
  `tests/unit/test_r4_buttons_js.py` (node: list indicators, button gating, the dialogs, the JS
  fixes). `test_fix2_stk2`'s Express Request harness and WP0's Connections test were updated.
- Bench (written, not run here): `mpsa_suite/money_out/test_connections.py` (counts on Supplier for
  payout rows, schedule lines and B2C requests by party type; Sales Invoice receipts, prompts and
  matches; every hooked form's fields exist; salary masking while counting; suite forms) and
  `mpsa_suite/money_in/test_collect.py` (Payment Request made and submitted for an order and an
  invoice, owed, limit, recent prompt, roles, rate limit), as named users.
