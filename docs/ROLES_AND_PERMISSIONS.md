# Roles and permissions

Branch `roles-permissions` (cut from `mpsa-suite` at 6c46a3d), 2026-10-23. Covers every way into
the `mpsa_suite` app: DocType permissions, whitelisted endpoints, the 70 old-path entries, reports,
workspace, charts, document events and jobs that act as another user, and the form buttons.

The rules are data in [`mpsa_suite/permission_matrix.py`](../mpsa_suite/permission_matrix.py); the
role sets of the server checks are in [`mpsa_suite/permissions.py`](../mpsa_suite/permissions.py).
`tests/unit/test_permission_matrix.py` fails when the JSON, the code or the generated part of this
document drifts from them, and when a whitelisted function has no server-side check.

## 1. The roles

| Role | Job | Created by |
|---|---|---|
| System Manager | Site administrator (IT). Can do everything, including changing permissions. | Frappe |
| M-Pesa Manager | M-Pesa administrator: settings, Daraja secrets, shortcodes, C2B/Pull registration, approves payee accounts, releases held receipts, settles Unknown payouts, resets M-Pesa permissions. | the suite (`install.SUITE_ROLES`) |
| M-Pesa Payer | Sends payouts (B2C, B2B, reversals) from documents others prepared; retries failed ones. | the suite |
| M-Pesa Collector | Sends STK prompts and attaches received payments to invoices. | the suite |
| M-Pesa Viewer | Reads M-Pesa transactions and reports. Changes nothing, sees no secrets. | the suite |
| Accounts User | Prepares payouts (drafts), books and reconciles entries, collects. | ERPNext |
| Accounts Manager | As Accounts User, plus booking work (C2B register cancel, statement imports). Prepares payouts; does not send them or do M-Pesa admin work (decisions D1, D2). | ERPNext |
| HR Manager / HR User | Prepare B2C payroll disbursements; an M-Pesa Payer sends them (D1). | ERPNext |
| Sales User | Collects: STK prompts, C2B receipts on invoices. | ERPNext |
| Purchase Manager / User | Proposes / reads supplier M-Pesa accounts. No B2B access. | ERPNext |

Separation of duties the code enforces:

- **Payee destinations** (Supplier M-Pesa Account): anyone listed may enter one as a draft; only
  `ADMIN_ROLES` may approve it (submit), never the person who entered it, with proof attached and
  a hold period before first use (`money_out/supplier_accounts.py:80`). Payments use only approved
  accounts; a one-off destination needs the Suite switch and `ADMIN_ROLES`, checked at submit and
  again at send.
- **Payment schedules** (payouts Phase 2): the person who presses Pay must differ from the one who
  prepared or last changed the line, behind a Suite switch on by default (`docs/PAYOUTS_DESIGN.md`).
- **Paying** needs `PAYOUT_ROLES` on the server at submit for B2B, B2C and reversals, and again
  at send for B2B. Who sends a direct payout must not be who prepared it (D3); a scheduled line
  is checked the same way at Pay Selected.
- **Secrets** (Daraja credentials, the callback token, API client secrets) are visible to
  `CONFIG_ROLES` only: System Manager and M-Pesa Manager.

## 2. Audit findings, ranked

Line numbers are on this branch. "Fixed" means fixed here, with tests.

| # | Risk | Finding | Where | Status |
|---|---|---|---|---|
| G1 | **High** | **Role gates skipped on Frappe's v2 API.** `require_for_direct_call` recognised a direct call only by a path ending `.<function>`. `/api/v2/method/<DocType>/<function>` runs any whitelisted name the DocType's controller module holds, imported ones too, and its path ends `/<function>`. Any logged-in user, portal users included, could send STK prompts from any shortcode to any phone (`/api/v2/method/Mpesa Express Request/initiate_stk_push`), query balances and transaction status, and make the one-way Pull registration at Safaricom with a callback URL of their choosing (`/api/v2/method/Mpesa Settings/register_pull_transaction`). | `permissions.py:71`; reached through `mpesa_express_request.py:13`, `mpesa_settings.py:42`; targets `m_pesa_api.py:193`, `:255`, `:339`, `mpesa_settings.py:498`, `:704` | Fixed: every route counts (v1 path and `cmd`, v2 method and DocType-method, `run_doc_method`) |
| G2 | **High** | **Credentials readable below M-Pesa Manager.** On Mpesa Settings, Consumer Key, Initiator Name, Security Credential (the encrypted initiator password Daraja accepts in place of it) and Access Token were level-0 fields. Accounts Manager could read, export, print and email them; Accounts User and M-Pesa Viewer could read them. On both live sites the site's own rules give read and export to Accounts Manager, Accounts User and other local roles. | `mpesa_settings.json` | Fixed: nine credential fields on permission level 1, held by System Manager and M-Pesa Manager only; export, print and email removed below them; the browser no longer reads those fields (`mpesa_c2b_payment_register_list.js:68`, `mpesa_settings.js`) and the server checks the initiator instead |
| G3 | **High (live)** | **The live sites do not use the app's permissions.** Both have Custom DocPerm rows on B2C Disbursement Request, B2C Payment Disbursement, Mpesa C2B Payment Register, Mpesa C2B Payment Register URL, Mpesa Express Request, Mpesa Settings and Payment Gateway. Frappe then ignores the app's rules for those DocTypes entirely, so JSON hardening alone never reaches production. | live sites (section 5) | Fixed in code: detected and reported on every migrate (one Error Log per change), never deleted; a Manager-only **Check Permissions** dialog compares them with the suite and resets chosen DocTypes; credential fields stay reachable for the site's editors (`access.py`). The reset itself is a rollout step (section 7) |
| G4 | **High** | **Many people can send money.** Submitting a B2C document sends money. Its submit permission is held by Accounts Manager, HR Manager and System Manager, and nothing on the server checks a role at B2C submit (B2B and reversals do). On the live sites about 15 people per site hold one of those roles. | `b2c_payment_disbursement.py:47`, `b2c_disbursement_request.py:28`, their JSON | Fixed after decisions D1 to D3 (section 9): only the pay roles send, checked at submit, by someone other than who prepared it |
| G5 | Medium | **Form buttons ran for anyone who can read.** `run_doc_method` asks only for read. So M-Pesa Viewer could start a Safaricom balance query (`get_account_balance_info`), a reader could re-send an STK prompt (`initiate_request`), any reader of a B2C disbursement could pull other documents' outstanding amounts past their permissions (`get_outstanding_reference_documents`, `allocate_amount_to_references`) or rewrite its status (`update_disbursement_status`), and a refused retry (`retry_failed_payments`) was only logged. | `mpesa_settings.py:337`, `mpesa_express_request.py:135`, `b2c_payment_disbursement.py:287`, `:305`, `:383`, `:889` | Fixed |
| G6 | Medium | **Role gates were never tested on Frappe 15.** `frappe.only_for` returns early whenever `flags.in_test` is set on v15, so the suite's `require()` and nine direct `only_for` calls let everyone through in v15 bench tests. | `permissions.py` `require` | Fixed: `require()` checks roles itself; every `only_for` replaced (a unit test keeps it out) |
| G7 | Medium | **Roles that cannot do their job.** M-Pesa Payer is in `PAYOUT_ROLES` but had no permission on the B2C DocTypes, nor read on C2B registers (`make_reversal` needs it); M-Pesa Collector had no permission on Mpesa Express Request; M-Pesa Manager none on B2C, C2B registers, C2B URL registration or the certificates; M-Pesa Viewer could not read B2C, STK or C2B records; the reports left out the suite roles. | DocType and Report JSON | Fixed (matrix below) |
| G8 | Low | **Helpers open to every logged-in user** (portal users too), leaking set-up data or request status: Mode of Payment accounts, payment gateways, POS M-Pesa shortcodes, STK defaults (read with ignore_permissions), any DocType's field list, an STK request's status and redirect. | `utils.py:672`, `sales_invoice.py:195`, `:231`, `mpesa_quick_pay.py:9`, `mpesa_express_request.py:353`, `mpesa_settings.py:643`, `www/mpesa/stkpush.py:103` | Fixed: each checks the permission of the form that uses it |
| G9 | Low | **A guest endpoint that is not a callback.** `convert_amount_to_kes` loads any Mpesa Settings record for a guest (exchange rates). Its old path was guest at release C, and R4 keeps the flag. | `utils.py:683` | Fixed: the flag stays, but an HTTP call needs read on Mpesa Express Request (guests have none) |
| G10 | Low | `check_transaction_status` did not check that the user may read the request, and its errors were swallowed. | `m_pesa_api.py:255` | Fixed (checked before the try) |
| G11 | Info (live) | Trident has no Integration User, so Safaricom callbacks run as Administrator (policy Warn). | M-Pesa Suite Settings | Rollout step 2 |
| G12 | Info | Workspace, charts and number cards are public. Their data goes through `frappe.get_list`, so each user sees only what their permissions allow; links hide DocTypes the user cannot read. The `Payment Gateway` DocType in this app's folder comes from upstream and belongs to the payments app: left as it is, never reset by the suite. | `workspace/m_pesa`, `dashboard_chart/*`, `number_card/*` | No change |
| G13 | Info | Supplier M-Pesa Account grants share to Accounts User, M-Pesa Payer and Purchase Manager. Frappe 15 and 16 let a user share only rights they hold, and approving needs `ADMIN_ROLES` on the server, so sharing cannot approve an account. | `supplier_m_pesa_account.json` | No change (payouts Phase 3 mirrors this DocType) |

Checked and found sound (no change):

- **Safaricom callbacks** (19 endpoints) all go through `@safaricom_callback`: IP allow-list and URL
  token (each switchable in M-Pesa Suite Settings), then run as the Integration User. Result
  handlers trust the `ref` on the URL only next to a valid token (`money_out/api.py`).
- **Payments API** (`gateway/v1.py`): client key and secret (HMAC compare), per-client IP allow-list,
  rate limit and per-action flags; clients see only their own payments. **Pay page**: a 32-character
  token, rate limited per IP and per payment, shows no customer data beyond the masked phone.
- **The 70 old paths** in `override_whitelisted_methods` map to the same function under the new name
  with the same guest setting (R4 tests); they inherit every check of their target.
- **B2B, reversals, supplier accounts, Make Entry**: role and document checks at submit and again at
  send; secrets never leave the server; `before_update_after_submit` restores server-owned fields.
- **Sandbox Lab**: needs the `mpesa_sandbox_lab` site switch, `ADMIN_ROLES` and sandbox records only.

## 3. Target model

1. **Least privilege.** Each role gets the rights its job needs (matrix in section 4).
2. **Server first.** Every whitelisted endpoint checks on the server: a role set, the user's
   permission on the document, or both. The browser only hides buttons.
3. **Secrets** are on permission level 1 or in Password fields readable by `CONFIG_ROLES` only.
   Below M-Pesa Manager nobody changes settings, shortcodes or registrations at Safaricom.
4. **M-Pesa Viewer** reads, reports and prints; it exports transactions, never settings.
5. **Guests** reach only Safaricom callbacks, the Payments API (client credentials) and the pay page.
6. **Separation of duties** as in section 1, with the decisions of section 9.
7. **System work** that acts beyond the session user is listed and justified in section 6.
8. **A site's own permission rules win** in Frappe; the suite reports them and an M-Pesa admin
   chooses to reset them (section 7). Nothing is deleted automatically.

### Role Profiles

`access.ensure_role_profiles` creates these once on install/migrate and never changes them again
(an admin may edit them). **On Frappe 15 (Trident today) a Role Profile replaces all of a user's
roles**, so give a profile only to people whose whole job it describes; for everyone else tick
the M-Pesa role in the user's Roles list. Frappe 16 allows several profiles per user.

## 4. Who may do what

Generated from `permission_matrix.py`; do not edit by hand (section 10).

<!-- BEGIN GENERATED: permission_matrix -->
### DocType rights by role

Letters: `r` read, `w` write, `c` create, `s` submit, `x` cancel, `a` amend, `d` delete, `R` report, `e` export, `p` print, `m` email, `h` share. `L1:` is permission level 1 (the credential fields). A blank cell is no access.

| DocType | SysMgr | M-Pesa Mgr | Payer | Collector | Viewer | Acc Mgr | Acc User | HR Mgr | HR User | Sales User | Pur Mgr | Pur User |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B2C Disbursement Request | `rwcsxadRepmh` | `rwcsxadRepmh` | `rwsRp` |  | `rRep` | `rwcadRepmh` | `rwcRepmh` | `rwcadRepmh` | `rwcRepmh` |  |  |  |
| B2C Payment Disbursement | `rwcsxadRepmh` | `rwcsxadRepmh` | `rwsRp` |  | `rRep` | `rwcadRepmh` |  | `rwcadRepmh` |  |  |  |  |
| M-Pesa Payment Schedule | `rwcdRepmh` | `rwcdRepmh` | `rRep` |  | `rRep` | `rwcdRepmh` | `rwcdRepmh` |  |  |  |  |  |
| M-Pesa Suite Settings | `rwcpmh` | `rwcpmh` |  |  |  |  |  |  |  |  |  |  |
| Mpesa API Client | `rwcdRepmh` | `rwcdRepmh` |  |  |  |  |  |  |  |  |  |  |
| Mpesa B2B Payment Request | `rwcsxadRepmh` | `rwcsxadRepmh` | `rwcsxadRepmh` |  | `rRep` | `rwcadRepmh` | `rwcdRep` |  |  |  |  |  |
| Mpesa B2C Status Query | `rdRep` | `rRep` | `rRep` |  | `rRep` | `rRep` | `rRep` |  |  |  |  |  |
| Mpesa Balance Snapshot | `rwdRep` | `rwdRep` |  |  | `rRep` | `rRep` | `rRep` |  |  |  |  |  |
| Mpesa C2B Payment Register | `rwdRepmh` | `rwsxaRepmh` | `rRp` | `rRp` | `rRep` | `rwsxaRepmh` | `rwsRepmh` |  |  | `rwsRepmh` |  |  |
| Mpesa C2B Payment Register URL | `rwcdRepmh` | `rwcdRepmh` |  |  | `rRp` | `rRepmh` | `rRepmh` |  |  | `rRepmh` |  |  |
| Mpesa Express Request | `rwcsdRepmh` | `rwcsRepmh` |  | `rwcsRp` | `rRep` | `rwcsRepmh` | `rwcsRepmh` |  |  | `rwcsRepmh` |  |  |
| Mpesa Match Log | `rwcdRep` | `rwcdRep` |  |  | `rRep` | `rRep` | `rRep` |  |  |  |  |  |
| Mpesa Payment Intent | `rwcdRepmh` | `rwcdRepmh` |  | `rRep` | `rRep` | `rRep` | `rRep` |  |  |  |  |  |
| Mpesa Public Key Certificate | `rwcdpmh` | `rwc` |  |  |  |  |  |  |  |  |  |  |
| Mpesa Reversal Request | `rwcsxadRepmh` | `rwcsxadRepmh` | `rwcsxadRepmh` |  | `rRep` | `rwcadRepmh` | `rRep` |  |  |  |  |  |
| Mpesa Sandbox Test Run | `rwcdRepmh` | `rwcdRepmh` |  |  |  |  |  |  |  |  |  |  |
| Mpesa Settings | `rwcdRepmh L1:rw` | `rwcdRepmh L1:rw` | `r` | `r` | `r` | `rR` | `rR` |  |  |  |  |  |
| Mpesa Statement Import | `rwcdRep` | `rwcdRep` |  |  | `rRep` | `rwcdRep` | `rRep` |  |  |  |  |  |
| Mpesa Statement Row | `rwdRep` | `rwdRep` |  |  | `rRep` | `rRep` | `rRep` |  |  |  |  |  |
| Mpesa Webhook Delivery | `rwcdRepmh` | `rwcdRepmh` |  |  |  |  |  |  |  |  |  |  |
| Payment Gateway | `rwcd` |  |  |  |  |  |  |  |  |  |  |  |
| Supplier M-Pesa Account | `rwcsxadRepmh` | `rwcsxadRepmh` | `rwcaRepmh` |  | `rRep` | `rwcadRepmh` | `rwcaRepmh` |  |  |  | `rwcaRepmh` | `rRep` |

### Role sets of the server checks (mpsa_suite/permissions.py)

| Set | Roles |
|---|---|
| `ADMIN_ROLES` | System Manager, M-Pesa Manager |
| `COLLECT_ROLES` | System Manager, M-Pesa Manager, M-Pesa Collector, Accounts Manager, Accounts User, Sales Manager, Sales User |
| `CONFIG_ROLES` | System Manager, M-Pesa Manager |
| `FINANCE_ROLES` | System Manager, M-Pesa Manager, Accounts Manager, Accounts User |
| `PAYOUT_ROLES` | System Manager, M-Pesa Manager, M-Pesa Payer |

### Report roles

| Report | Roles |
|---|---|
| B2C Disbursement Summary | System Manager, Accounts Manager, HR Manager, M-Pesa Manager, M-Pesa Payer, M-Pesa Viewer |
| C2B Payment Summary Group by Customer | System Manager, Accounts User, Accounts Manager, Sales User, M-Pesa Manager, M-Pesa Collector, M-Pesa Viewer |
| C2B Reconciliation Report | System Manager, Accounts User, Accounts Manager, Sales User, M-Pesa Manager, M-Pesa Collector, M-Pesa Viewer |
| M-Pesa Payments Due | System Manager, Accounts Manager, Accounts User, M-Pesa Manager, M-Pesa Payer, M-Pesa Viewer |
| STK Push Request Status | System Manager, Accounts User, Accounts Manager, Sales User, M-Pesa Manager, M-Pesa Collector, M-Pesa Viewer |

### Role Profiles the suite creates

| Role Profile | Roles |
|---|---|
| M-Pesa Finance Clerk | Accounts User, M-Pesa Collector, M-Pesa Viewer |
| M-Pesa Paying Officer | Accounts User, M-Pesa Payer, M-Pesa Viewer |
| M-Pesa Approver | Accounts User, M-Pesa Manager, M-Pesa Viewer |
| M-Pesa Cashier | Sales User, M-Pesa Collector |
| M-Pesa Auditor | M-Pesa Viewer |

### Whitelisted endpoints

Every endpoint checks on the server. `direct`: the role gate applies to HTTP calls; document flows reach the function after their own checks.

| Endpoint | Who | Server check |
|---|---|---|
| `access.permission_report` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `access.reset_permissions` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `gateway.v1.cancel` | API client (own payments) | api |
| `gateway.v1.create_payment_link` | API client with can_create_links | api |
| `gateway.v1.create_prompt` | API client with can_prompt | api |
| `gateway.v1.get_status` | API client (own payments) | api |
| `gateway.v1.list_payments` | API client (own payments) | api |
| `gateway.v1.page_prompt` | Pay page token holder (guest, rate limited) | page |
| `gateway.v1.page_status` | Pay page token holder (guest, rate limited) | page |
| `gateway.v1.resend_prompt` | API client with can_prompt | api |
| `gateway.v1.verify_receipt` | API client with can_verify_receipts | api |
| `money_in.c2b_verify.release` | CONFIG_ROLES | roles:RELEASE_ROLES |
| `money_out.api.b2b_queue_timeout` | Safaricom (guest) | callback |
| `money_out.api.b2b_result` | Safaricom (guest) | callback |
| `money_out.api.b2c_status_result` | Safaricom (guest) | callback |
| `money_out.api.check_b2c_status` | PAYOUT_ROLES or ADMIN_ROLES + read request | roles:PAYOUT_OR_ADMIN, perm |
| `money_out.api.make_reversal` | PAYOUT_ROLES or ADMIN_ROLES + read register | roles:PAYOUT_OR_ADMIN, perm |
| `money_out.api.reversal_queue_timeout` | Safaricom (guest) | callback |
| `money_out.api.reversal_result` | Safaricom (guest) | callback |
| `money_out.api.status_queue_timeout` | Safaricom (guest) | callback |
| `money_out.b2b.amount_left` | Read the invoice / order | perm |
| `money_out.b2b.order_advances` | Read the order | perm |
| `money_out.b2b_entries.entry_facts` | FINANCE_ROLES + read request | roles:FINANCE_ROLES, perm |
| `money_out.b2b_entries.make_entry` | FINANCE_ROLES + create PE/JE + read request | roles:FINANCE_ROLES, perm |
| `money_out.b2b_entries.refresh_entry_now` | FINANCE_ROLES + read request | roles:FINANCE_ROLES, perm |
| `money_out.schedule.close_schedule` | FINANCE_ROLES + write the schedule | roles:FINANCE_ROLES, perm |
| `money_out.schedule.confirm_payment` | PAYOUT_ROLES, the user the summary was made for | roles:PAYOUT_ROLES |
| `money_out.schedule.move_date` | FINANCE_ROLES + write the schedule | roles:FINANCE_ROLES, perm |
| `money_out.schedule.plan_again` | FINANCE_ROLES + write the schedule | roles:FINANCE_ROLES, perm |
| `money_out.schedule.prepare_payment` | PAYOUT_ROLES + read the schedule | roles:PAYOUT_ROLES, perm |
| `mpsa_suite.api.b2c.b2c_results_callback` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.account_balance_result` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.balance_query_callback` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.bulk_pull_transactions` | System Manager | roles:System Manager |
| `mpsa_suite.api.m_pesa_api.c2b_verification_result` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.check_transaction_status` | COLLECT_ROLES + read request | direct, roles:COLLECT_ROLES, perm |
| `mpsa_suite.api.m_pesa_api.confirmation` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.get_account_balance` | FINANCE_ROLES | direct, roles:FINANCE_ROLES |
| `mpsa_suite.api.m_pesa_api.handle_queue_timeout` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.handle_transaction_status_result` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.initiate_stk_push` | COLLECT_ROLES | direct, roles:COLLECT_ROLES |
| `mpsa_suite.api.m_pesa_api.pull_registration_result` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.pull_transactions` | FINANCE_ROLES | direct, roles:FINANCE_ROLES |
| `mpsa_suite.api.m_pesa_api.stk_push_callback` | Safaricom (guest) | callback |
| `mpsa_suite.api.m_pesa_api.validation` | Safaricom (guest) | callback |
| `mpsa_suite.api.mpesa_quick_pay.get_mpesa_payments` | Read Mpesa C2B Payment Register | perm |
| `mpsa_suite.api.mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile` | Read Mpesa C2B Payment Register | perm |
| `mpsa_suite.api.mpesa_quick_pay.process_mpesa` | COLLECT_ROLES + write invoice | roles:COLLECT_ROLES, perm |
| `mpsa_suite.api.mpesa_quick_pay.update_mpesa_after_invoice_submission` | COLLECT_ROLES + write invoice | roles:COLLECT_ROLES, perm |
| `mpsa_suite.api.payment_entry.get_outstanding_invoices` | Read Sales Invoice | perm |
| `mpsa_suite.api.payment_entry.initiate_payment_entry_stk` | Submit the Payment Entry | perm |
| `mpsa_suite.api.sales_invoice.get_mop_from_payment_gateway` | Read Payment Request | perm |
| `mpsa_suite.api.sales_invoice.get_payment_gateway_from_mop` | Read Payment Request | perm |
| `mpsa_suite.api.sales_invoice.get_stk_amount` | Read the invoice | via:_check_payment_row_parent |
| `mpsa_suite.api.sales_invoice.initiate_invoice_stk_push` | COLLECT_ROLES + write invoice | roles:COLLECT_ROLES, perm |
| `mpsa_suite.api.sales_invoice.initiate_row_stk_push` | COLLECT_ROLES + write invoice | roles:COLLECT_ROLES, via:_check_payment_row_parent |
| `mpsa_suite.api.sales_invoice.validate_stk_push_eligibility` | Read the invoice | perm |
| `mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request.B2CDisbursementRequest.retry_failed_payment` | PAYOUT_ROLES | roles:PAYOUT_ROLES |
| `mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.allocate_amount_to_references` | Create / write the disbursement | via:_check_can_prepare |
| `mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.get_outstanding_reference_documents` | Create / write the disbursement | via:_check_can_prepare |
| `mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.retry_failed_payments` | PAYOUT_ROLES | roles:PAYOUT_ROLES |
| `mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.update_disbursement_status` | Submit B2C Payment Disbursement | direct, perm |
| `mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings.MPesaSuiteSettings.regenerate_callback_token` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings.MPesaSuiteSettings.reset_rejection_counter` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.mpesa_api_client.mpesa_api_client.MpesaAPIClient.generate_new_secret` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.mpesa_api_client.mpesa_api_client.MpesaAPIClient.rotate_webhook_secret` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.mpesa_c2b_payment_register_url.mpesa_c2b_payment_register_url.MpesaC2BPaymentRegisterURL.register_again` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.MpesaExpressRequest.initiate_request` | COLLECT_ROLES + write the request | direct, roles:COLLECT_ROLES, perm |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.MpesaExpressRequest.reconcile_payment` | ADMIN_ROLES + write the request | roles:ADMIN_ROLES, perm |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.create_new_request` | Create Mpesa Express Request | perm |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.get_request_status` | Read the request | perm |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.get_stkpush_defaults` | Create Mpesa Express Request | perm |
| `mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.retry_stkpush` | Write the request | perm |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.MpesaSettings.get_account_balance_info` | FINANCE_ROLES | roles:FINANCE_ROLES |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.MpesaSettings.regenerate_security_credential` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.bulk_register_pull_transactions` | System Manager | roles:System Manager |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.get_doctype_fields` | Write Mpesa Settings | perm |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.process_balance_info` | Safaricom (guest) | callback |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.pull_transaction_callback` | Safaricom (guest) | callback |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.register_pull_transaction` | ADMIN_ROLES | direct, roles:ADMIN_ROLES |
| `mpsa_suite.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status` | FINANCE_ROLES | direct, roles:FINANCE_ROLES |
| `mpsa_suite.doctype.mpesa_statement_import.mpesa_statement_import.MpesaStatementImport.import_statement` | Write the import | perm |
| `mpsa_suite.doctype.mpesa_webhook_delivery.mpesa_webhook_delivery.MpesaWebhookDelivery.retry_now` | CONFIG_ROLES | roles:CONFIG_ROLES |
| `mpsa_suite.doctype.supplier_m_pesa_account.supplier_m_pesa_account.SupplierMPesaAccount.confirm_receiver_name` | ADMIN_ROLES | via:confirm_receiver_name |
| `mpsa_suite.doctype.supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier` | Read Supplier M-Pesa Account | perm |
| `sandbox_lab.api.add_scenarios` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.check_now` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.ping` | Sandbox Lab health check (guest) | callback |
| `sandbox_lab.api.replay_callback` | ADMIN_ROLES + read the document | via:require_lab, perm |
| `sandbox_lab.api.reset_case` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.run_case` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.run_health_check` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.run_ready` | ADMIN_ROLES + write the run | via:_run |
| `sandbox_lab.api.simulate_c2b` | ADMIN_ROLES | via:require_lab |
| `utils.utils.convert_amount_to_kes` | Read Mpesa Express Request | direct, perm |
| `utils.utils.get_mode_of_payment_account` | Write B2C Payment Disbursement | perm |
| `www.mpesa.stkpush.check_payment_status` | Read the request | perm |
| `www.mpesa.stkpush.initiate_stk_push` | Write the request | via:_check_can_push |
| `www.mpesa.stkpush.retry_stkpush` | Write the request | via:_check_can_push |
<!-- END GENERATED: permission_matrix -->

## 5. Live sites (read on 2026-10-23, read-only)

Counts only; no names are kept here. Trident and Astral were read separately through their own
connectors.

| | Trident (Frappe 15.122, frappe_mpsa_payments release C) | Astral (Navari app) |
|---|---|---|
| Enabled system users | 166 | 117 |
| Hold Accounts Manager / HR Manager / System Manager (any) | 17 | 15 |
| Accounts Manager / HR Manager / System Manager | 15 / 15 / 11 | 14 / 14 / 10 |
| HR Manager without Accounts Manager, System Manager or an M-Pesa pay role | 2 | 1 |
| Suite roles held | 1 user each (Manager, Payer, Collector, Viewer) | none (not created yet) |
| Local roles in M-Pesa permission rules | Super admin | B2C User (10 users, submits B2C today), Super Admin, Admin, Accountant, Expense Approver Secondary |
| DocTypes on the site's own rules | 7 (see G3) | 7 (see G3) |
| Integration User | not set (callbacks as Administrator) | n/a |
| Role Profiles | 9 local ones (Accounts, Admin, HR, ...) | 13 local ones |

What the site rules give today that the suite would not (examples): Accounts User and Accounts
Manager read and **export** Mpesa Settings, credentials included; on Trident, HR Manager submits
B2C and nobody below System Manager may submit an STK request; on Astral, B2C User submits B2C,
and Accounts Manager, HR Manager and System Manager only read B2C. After deploy, the Check
Permissions dialog lists the exact differences per site.

## 6. Work that runs beyond the session user

Every `ignore_permissions`, `frappe.set_user` and `as_user` outside tests, by purpose. None is
reachable without one of the checks in section 4 first, or a Safaricom callback that passed
`@safaricom_callback`, or a scheduled job.

| Purpose | Where | Why it is needed |
|---|---|---|
| Callbacks run as the Integration User | `daraja/callbacks.py:153` | Safaricom posts as Guest; work runs as a configured user (Administrator until one is set, G11), restored after. |
| Recording Safaricom results and integration requests | `m_pesa_api.py:134`, `:791`, `:894`; `mpesa_settings.py:539`, `:604`; `mpesa_response_handler.py:86`; `connectors/base_connector.py:62`; `connectors/mpesa/b2c_connector.py:62`, `:314`; `mpsa_suite/connectors/connectors.py:44`; `utils/helpers.py:16`; `money_out/b2c_status.py:67` | System records of a request already authorised (or of a verified callback); the Integration User and the requester need no rights on Integration Request or result logs. |
| Booking money already received (C2B/STK) | `money_in/booking.py:64`, `receipts.py:61`, `c2b_verify.py:124`, `bank_transaction.py:99`, `common.py:366`, `payment_request.py:53`; `utils/utils.py:274`, `:379`, `:526`, `:649`; `mpesa_c2b_payment_register.py:431`; `mpesa_express_request.py:203` | Automatic, after verification and duplicate checks, each behind its Suite switch; staff need no rights on the ledgers for received money to be booked. |
| Booking money already paid out | `money_out/b2b_entries.py:341` (as Administrator, PAYOUTS_DESIGN R9), `accounting/payment_entry_service.py:22` (B2C, as Administrator), `accounting/journal_service.py:68`, `money_out/statement.py:500`, `:577` (reconcile as Administrator: ERPNext checks the session user's write), `wallet/statement_import.py:204`, `:530`, `:565`, `wallet/balances.py:44` | The payout was authorised at submit; its entry must not fail because the callback's user lacks accounting rights. Make Entry by staff runs with their own permissions. |
| Payouts of an authorised disbursement | `b2c_payment_disbursement.py:985` | Inserts the B2C request rows of a disbursement someone with submit permission sent. |
| Prompts after a check | `m_pesa_api` callers: `payment_entry.py:149`, `:539`; `sales_invoice.py:47`, `:167`; `mpesa_quick_pay.py:197`, `:328`; `mpesa_settings.py:111`, `:306`; `www/mpesa/stkpush.py:83`, `:94`; `gateway/intents.py:158`, `:314`, `:755`; `gateway/webhooks.py:49` | Each runs after the caller's own check (COLLECT_ROLES plus write on the invoice, submit on the Payment Entry, write on the request, or an authenticated API client) and creates the STK request the user may not create directly. |
| Releasing a paid Sales Order | `money_in/sales_order.py:112` | Runs the workflow as the "Approve Paid Orders As" user an admin configured. |
| Set-up | `install.py:22`, `:31`; `migrate.py:566`; `access.py:52`, `:93`; `utils/utils.py:27`, `:147`; `mpesa_settings.py:479` | Install and migrate (no session user), or the gateway, account and Mode of Payment an admin's Mpesa Settings save creates. |
| Reading settings for a request | `mpesa_express_request.py:359` | STK defaults; now needs create permission on Mpesa Express Request first. |
| Dead code | `utils/utils.py:61` `save_access_token` | No caller (tokens live in Redis). |

## 7. Live rollout (Desk only, Trident first, then Astral)

Frappe Cloud gives no shell: everything below is done in Desk by a System Manager. Keep the two
sites apart: do Trident completely, check it, then Astral after its own rename hand-over.

**Before the deploy**

1. Decide who pays (M-Pesa Payer), who approves (M-Pesa Manager) and who only reads. On both sites
   about 15 people send money today through Accounts Manager or HR Manager; after the deploy only
   the people given M-Pesa Payer or M-Pesa Manager will. Write the list down per site.
2. Integration User (G11): User > New: a System User such as `mpesa-integration@<company>` with
   roles **Accounts User** and **M-Pesa Manager** only, a long random password nobody keeps, no
   other access. M-Pesa Suite Settings > Callback Security: set **Integration User** to it, save,
   watch a few callbacks arrive (Mpesa Express Request / C2B register appear), then set **If No
   Integration User** to **Refuse**.
3. Astral only: its own role **B2C User** pays B2C today (10 users). Give **M-Pesa Payer** to each
   of them who should keep paying (User > Roles), before step 6.

**Deploy** the release (Frappe Cloud runs the migrate). The migrate creates the Role Profiles,
gives the site's Mpesa Settings editors access to the credential fields (level 1), and writes one
Error Log "M-Pesa Suite: this site's own permission rules differ from the suite's".

**After the deploy**

4. Assign roles: User > (the person) > Roles: tick the M-Pesa roles from step 1. Use a Role
   Profile (M-Pesa Finance Clerk, Paying Officer, Approver, Cashier, Auditor) only for people who
   have no other roles on Frappe 15. Each user must log out and in again.
5. M-Pesa Suite Settings > Permissions > **Check Permissions**: read the differences. Anyone
   listed under "the site grants, beyond the suite" (with the number of users) loses that right on
   reset unless step 3/4 gave them the matching M-Pesa role.
6. Tick the DocTypes and **Reset Selected** (M-Pesa Manager or System Manager). Each reset is
   recorded as a comment on M-Pesa Suite Settings. To undo, a System Manager re-creates the rules
   in Role Permissions Manager; nothing else is lost.
7. Run the checks below with one test user per role (or Administrator's Impersonate).

**Checks per role** (tick each; a refused action shows "Not permitted" or a permission message)

| Role | Must work | Must be refused |
|---|---|---|
| M-Pesa Viewer | M-Pesa workspace; lists and reports of STK, C2B, B2C, B2B, reversals; Mpesa Settings opens | Mpesa Settings shows no Consumer Key, Initiator, Security Credential or Access Token; Get Account Balance; any New/Submit/Cancel; Export of Mpesa Settings |
| M-Pesa Collector | New Mpesa Express Request and Submit (a KES 1 prompt to your own phone, by the user); quick pay on a draft invoice | Mpesa Settings credentials; submitting B2C or B2B; Supplier M-Pesa Account approval |
| Accounts User (Finance Clerk) | Draft B2B request and B2C request; a payment schedule line and Move Date; Make Entry / Check Entry Again; Get Account Balance; transaction status | Submitting a B2B request and Pay Selected (need a pay role); approving a Supplier M-Pesa Account; Mpesa Settings credentials or edit |
| M-Pesa Payer | Submit a B2B request an Accounts User prepared (sandbox, or KES 10 sent by the user); Pay Selected on M-Pesa Payments Due for a line someone else prepared; submit a B2C disbursement; retry a failed B2C | Pay Selected on a line they prepared or last changed (maker-checker on); editing a schedule; approving a Supplier M-Pesa Account; editing Mpesa Settings; seeing credentials |
| M-Pesa Manager | Credentials visible and editable; Regenerate Security Credential; approve a Supplier M-Pesa Account someone else entered; release a held C2B receipt; Check Permissions | Approving an account they entered themselves |
| Portal / website user | nothing M-Pesa | `/api/v2/method/Mpesa Express Request/initiate_stk_push` and `/api/v2/method/Mpesa Settings/get_account_balance?name=<record>` answer 403 |
| Guest (logged out) | the pay page `/pay/<token>`; Safaricom callbacks keep arriving | `/api/method/mpsa_suite.utils.utils.convert_amount_to_kes?currency=USD&amount=1` answers 403 |

Then on Astral, repeat steps 1 to 7 after its rename hand-over.

## 8. Behaviour changes for current users

On every site, at once (server checks, whatever the site's own permission rules):

- **Accounts Manager and HR Manager can no longer send money**: submitting a B2C disbursement or
  request, a B2B request or a reversal, retrying a B2C payout, Pay Selected and B2C status queries
  need M-Pesa Payer, M-Pesa Manager or System Manager (D1).
- **Accounts Manager can no longer approve payee accounts** or do the other `ADMIN_ROLES` work (D2).
- **Maker-checker** (D3): a payer cannot send a B2B request or B2C disbursement they created or
  last saved, or change it while sending. A one-person team turns the switch off.

On a site using the suite's own rules (new installs, and DocTypes after a reset):

- Accounts Manager, Accounts User, M-Pesa Viewer: no longer see the nine credential fields of
  Mpesa Settings; Accounts Manager loses export, print and email of Mpesa Settings.
- M-Pesa Viewer: can no longer start a balance query; can now read B2C, STK and C2B records.
- M-Pesa Payer: can now read, edit and submit (send) B2C documents and read C2B registers.
- M-Pesa Collector: can now create and submit STK requests and read C2B registers.
- M-Pesa Manager: full access to B2C documents, C2B registers, C2B URL registration, certificates.
- Accounts Manager and HR Manager: no submit or cancel on B2C, B2B, reversal or Supplier M-Pesa
  Account documents; Accounts Manager: C2B URL registrations read-only, no Sandbox Lab.
- Reports: M-Pesa Manager, the matching suite roles and M-Pesa Viewer can open them.

On every site, at once, whatever its own rules:

- The credential fields move to level 1: roles that only read Mpesa Settings under the site's rules
  stop seeing them; roles that edit it keep them (rows added by the migrate).
- Logged-in users without the matching role are refused on the v2 routes (G1) and on the helpers
  (G8); readers can no longer run the form buttons of G5.
- The B2C disbursement form's helper needs write on B2C Payment Disbursement; the Payment Request
  form's helpers need read on Payment Request; the Express Request form's conversion needs read on
  it. Their normal users have these.

## 9. Decisions taken (the user, 2026-10-23)

- **D1. Who sends money** (submit B2C payouts, B2B payments, reversals; retry; Pay Selected):
  M-Pesa Payer, M-Pesa Manager and System Manager only. `PAYOUT_ROLES` lost Accounts Manager;
  B2C Payment Disbursement and B2C Disbursement Request now check `PAYOUT_ROLES` at submit, as B2B
  and reversals do. Accounts Manager and HR Manager keep preparing (create, edit, amend, delete
  drafts) but have no submit or cancel on money-out DocTypes.
- **D2. M-Pesa admin work** (`ADMIN_ROLES`: approve Supplier M-Pesa Accounts, one-off destinations,
  register C2B URLs and Pull, cancel Unknown payouts, entries for cancelled or moved payouts,
  reconcile a prompt by hand, the Sandbox Lab): M-Pesa Manager and System Manager only
  (`ADMIN_ROLES` = `CONFIG_ROLES`). Accounts Manager reads C2B URL registrations, proposes
  supplier accounts as drafts and no longer has the Sandbox Lab.
- **D3. Maker-checker on direct payouts:** who submits a B2B request, a B2C Payment Disbursement
  or a direct B2C Disbursement Request must not be who created it or last saved it, and the
  submitting save must not change what is paid or where (`money_out/maker_checker.py`). It shares
  M-Pesa Suite Settings' switch with the payment schedule, now labelled "Who Sends a Payout Must Not
  Be Who Prepared It" (on by default; off only for a one-person team). Not checked: Administrator,
  requests a schedule makes (checked at Pay Selected), and the B2C requests a checked disbursement
  creates.

## 10. Changing a permission

1. Change `mpsa_suite/permission_matrix.py` (and `permissions.py` for a role set).
2. Change the DocType or Report JSON to match and bump its `modified`.
3. Refresh the generated part of this document:
   `python -c "import sys; sys.path[:0]=['tests/unit','.']; import test_permission_matrix as t; t.write_doc()"`
4. `python -m pytest tests/unit -q`, then the bench role tests
   (`mpsa_suite/tests/test_roles_permissions.py`) on v15 and v16.

A new whitelisted function needs an `ENDPOINTS` entry naming its check; a new DocType needs a
`DOCTYPES` entry. No patch: site-side work goes in `access.after_migrate` (release C's eight
patches are pinned by `test_rename_safeguards.py`).
