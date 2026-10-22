# Licensing inventory

Prepared 2026-10-19 for Astral's legal review. Not legal advice.

Mpsa Suite (called "Astral M-Pesa Suite", Python package `frappe_mpsa_payments`, until the
rename of 2026-10-22) is a fork of [navariltd/frappe-mpsa-payments](https://github.com/navariltd/frappe-mpsa-payments),
forked at upstream commit `dfa9a23` (2026-08-18). The upstream code is **AGPL-3.0**.
So, unless Navari Limited grants other terms:

- Navari's copyright lines must stay on the files below, along with the AGPL licence text
  (`license.txt`). Copyright notices on files that came from Frappe Technologies stay too.
- The app as a whole (Navari's code plus Astral's changes) can only be distributed, or offered
  to users over a network, under the AGPL, with its source code available to those users.
  Astral owns the copyright in its own additions, but cannot make the combined app proprietary.
- Making the app Astral's exclusive property would need one of: a written licence or
  assignment from Navari Limited and its contributors; or a clean-room rewrite of every file
  below by people who have not copied it (the files Astral wrote from scratch are not listed).

Counts are lines changed since the fork (`git diff --numstat dfa9a23`) against the file's
current length. They show how far each file has moved from upstream; a heavily changed file
still counts as a derivative work.

**Paths after the rename (2026-10-22, branch `mpsa-suite`).** The list below gives each file's
path on this branch. Its path before the rename is the same with `frappe_mpsa_payments` in place
of each `mpsa_suite` folder (the dashboard folder was `frappe_mpsa_payments_dashboard`). Each of
the 109 rows was checked on its own: the old path exists at the last old-name release
(`02d94a0`) and the new path exists on this branch. The counts were not re-measured; they are
the 2026-10-19 figures, taken under the old paths. The rename itself (commit `0937c31`) changed
only the app's names inside these files, apart from `hooks.py` (lookup table for the old URLs,
hand-over hooks) and the `modified` timestamp of 14 JSON records. The later rename safeguards
also changed `hooks.py`, `mpsa_suite/mpsa_suite/migrate.py` and the patches
`c2b_payment_register.py` and `payment_entry_custom_fields.py`. None of this changes which files
came from upstream.

## Files that came from upstream and are still in the app (109)

| File | Lines changed since fork | Current lines |
|---|---:|---:|
| `mpsa_suite/accounting/journal_service.py` | 1 | 64 |
| `mpsa_suite/accounting/payment_entry_service.py` | 1 | 63 |
| `mpsa_suite/connectors/abstract.py` | 0 | 15 |
| `mpsa_suite/connectors/base_connector.py` | 12 | 324 |
| `mpsa_suite/connectors/factory.py` | 1 | 18 |
| `mpsa_suite/connectors/mpesa/b2c_connector.py` | 132 | 325 |
| `mpsa_suite/desktop_icon/m_pesa.json` | 0 | 21 |
| `mpsa_suite/mpsa_suite/api/b2c.py` | 6 | 33 |
| `mpsa_suite/mpsa_suite/api/base_class.py` | 0 | 79 |
| `mpsa_suite/mpsa_suite/api/m_pesa_api.py` | 626 | 1145 |
| `mpsa_suite/mpsa_suite/api/mpesa_quick_pay.py` | 7 | 378 |
| `mpsa_suite/mpsa_suite/api/mpesa_response_handler.py` | 93 | 413 |
| `mpsa_suite/mpsa_suite/api/payment_entry.py` | 170 | 536 |
| `mpsa_suite/mpsa_suite/api/process_request.py` | 0 | 54 |
| `mpsa_suite/mpsa_suite/api/sales_invoice.py` | 188 | 284 |
| `mpsa_suite/mpsa_suite/api/test_m_pesa_api.py` | 54 | 63 |
| `mpsa_suite/mpsa_suite/api/test_payment_entry.py` | 63 | 28 |
| `mpsa_suite/mpsa_suite/connectors/connectors.py` | 103 | 252 |
| `mpsa_suite/mpsa_suite/custom/sales_invoice_payment.json` | 0 | 99 |
| `mpsa_suite/mpsa_suite/dashboard_chart/mpesa_trend/mpesa_trend.json` | 0 | 31 |
| `mpsa_suite/mpsa_suite/dashboard_chart/total_incoming_(c2b)/total_incoming_(c2b).json` | 0 | 31 |
| `mpsa_suite/mpsa_suite/dashboard_chart/total_incoming_(express)/total_incoming_(express).json` | 0 | 31 |
| `mpsa_suite/mpsa_suite/dashboard_chart/total_outgoing_(b2c)/total_outgoing_(b2c).json` | 0 | 31 |
| `mpsa_suite/mpsa_suite/doctype/b2c_disbursement_request/b2c_disbursement_request.js` | 0 | 25 |
| `mpsa_suite/mpsa_suite/doctype/b2c_disbursement_request/b2c_disbursement_request.json` | 98 | 490 |
| `mpsa_suite/mpsa_suite/doctype/b2c_disbursement_request/b2c_disbursement_request.py` | 23 | 51 |
| `mpsa_suite/mpsa_suite/doctype/b2c_disbursement_request/test_b2c_disbursement_request.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/b2c_payment_disbursement.js` | 4 | 725 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/b2c_payment_disbursement.json` | 6 | 310 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/b2c_payment_disbursement.py` | 247 | 925 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/config.py` | 7 | 53 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/encoding_credentials.py` | 0 | 87 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement/test_b2c_payment_disbursement.py` | 474 | 165 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement_reference/b2c_payment_disbursement_reference.json` | 34 | 194 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement_reference/b2c_payment_disbursement_reference.py` | 24 | 65 |
| `mpsa_suite/mpsa_suite/doctype/b2c_payment_disbursement_reference/test_b2c_payment_disbursement_reference.py` | 137 | 68 |
| `mpsa_suite/mpsa_suite/doctype/custom_exceptions.py` | 0 | 40 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.js` | 0 | 8 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.json` | 68 | 304 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.py` | 150 | 374 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register_list.js` | 0 | 139 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register/test_mpesa_c2b_payment_register.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.js` | 20 | 18 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.json` | 0 | 111 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.py` | 61 | 124 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_c2b_payment_register_url/test_mpesa_c2b_payment_register_url.py` | 177 | 92 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_conversion_rate/mpesa_conversion_rate.json` | 0 | 49 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_conversion_rate/mpesa_conversion_rate.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_express_request/mpesa_express_request.js` | 0 | 179 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_express_request/mpesa_express_request.json` | 53 | 457 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_express_request/mpesa_express_request.py` | 59 | 285 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_express_request/test_mpesa_express_request.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.js` | 0 | 8 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.json` | 0 | 59 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_public_key_certificate/test_mpesa_public_key_certificate.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_reconciliation_priority/mpesa_reconciliation_priority.json` | 0 | 56 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_reconciliation_priority/mpesa_reconciliation_priority.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/account_balance.html` | 0 | 32 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/mpesa_connector.py` | 90 | 162 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/mpesa_settings.js` | 15 | 434 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/mpesa_settings.json` | 104 | 478 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/mpesa_settings.py` | 247 | 831 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/mpesa_settings_list.js` | 0 | 168 |
| `mpsa_suite/mpsa_suite/doctype/mpesa_settings/test_mpesa_settings.py` | 334 | 392 |
| `mpsa_suite/mpsa_suite/doctype/payment_gateway/payment_gateway.js` | 0 | 6 |
| `mpsa_suite/mpsa_suite/doctype/payment_gateway/payment_gateway.json` | 0 | 51 |
| `mpsa_suite/mpsa_suite/doctype/payment_gateway/payment_gateway.py` | 0 | 8 |
| `mpsa_suite/mpsa_suite/doctype/payment_gateway/test_payment_gateway.py` | 0 | 9 |
| `mpsa_suite/mpsa_suite/mpsa_suite_dashboard/mpesa_transactions_overview_dashboard/mpesa_transactions_overview_dashboard.json` | 0 | 39 |
| `mpsa_suite/mpsa_suite/migrate.py` | 185 | 418 |
| `mpsa_suite/mpsa_suite/number_card/total_disbursed_b2c/total_disbursed_b2c.json` | 0 | 26 |
| `mpsa_suite/mpsa_suite/number_card/total_received_c2b/total_received_c2b.json` | 0 | 24 |
| `mpsa_suite/mpsa_suite/patches/c2b_payment_register.py` | 0 | 22 |
| `mpsa_suite/mpsa_suite/patches/payment_entry_custom_fields.py` | 0 | 60 |
| `mpsa_suite/mpsa_suite/patches/sales_invoice_patch.py` | 0 | 17 |
| `mpsa_suite/mpsa_suite/report/b2c_disbursement_summary/b2c_disbursement_summary.js` | 2 | 106 |
| `mpsa_suite/mpsa_suite/report/b2c_disbursement_summary/b2c_disbursement_summary.json` | 0 | 34 |
| `mpsa_suite/mpsa_suite/report/b2c_disbursement_summary/b2c_disbursement_summary.py` | 0 | 198 |
| `mpsa_suite/mpsa_suite/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.js` | 0 | 82 |
| `mpsa_suite/mpsa_suite/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.json` | 0 | 37 |
| `mpsa_suite/mpsa_suite/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.py` | 0 | 223 |
| `mpsa_suite/mpsa_suite/report/c2b_reconciliation_report/c2b_reconciliation_report.js` | 0 | 57 |
| `mpsa_suite/mpsa_suite/report/c2b_reconciliation_report/c2b_reconciliation_report.json` | 0 | 37 |
| `mpsa_suite/mpsa_suite/report/c2b_reconciliation_report/c2b_reconciliation_report.py` | 0 | 137 |
| `mpsa_suite/mpsa_suite/report/stk_push_request_status/stk_push_request_status.js` | 2 | 68 |
| `mpsa_suite/mpsa_suite/report/stk_push_request_status/stk_push_request_status.json` | 0 | 37 |
| `mpsa_suite/mpsa_suite/report/stk_push_request_status/stk_push_request_status.py` | 0 | 162 |
| `mpsa_suite/mpsa_suite/workspace/m_pesa/m_pesa.json` | 24 | 246 |
| `mpsa_suite/hooks.py` | 76 | 286 |
| `mpsa_suite/public/js/payment_entry.js` | 0 | 30 |
| `mpsa_suite/public/js/payment_request.js` | 0 | 77 |
| `mpsa_suite/public/js/sales_invoice.js` | 0 | 460 |
| `mpsa_suite/public/js/utils.js` | 38 | 775 |
| `mpsa_suite/services/b2c_postprocess_service.py` | 156 | 170 |
| `mpsa_suite/services/b2c_pull_transaction_scheduler_service.py` | 0 | 28 |
| `mpsa_suite/services/b2c_request_service.py` | 43 | 53 |
| `mpsa_suite/services/b2c_response_service.py` | 33 | 60 |
| `mpsa_suite/services/b2c_scheduler_service.py` | 48 | 71 |
| `mpsa_suite/utils/definitions.py` | 0 | 52 |
| `mpsa_suite/utils/doctype_names.py` | 2 | 9 |
| `mpsa_suite/utils/encoding_initiator_password.py` | 0 | 69 |
| `mpsa_suite/utils/helpers.py` | 0 | 16 |
| `mpsa_suite/utils/utils.py` | 85 | 455 |
| `mpsa_suite/workspace_sidebar/m_pesa.json` | 4 | 150 |
| `mpsa_suite/www/mpesa/stkpush.css` | 0 | 533 |
| `mpsa_suite/www/mpesa/stkpush.html` | 32 | 221 |
| `mpsa_suite/www/mpesa/stkpush.js` | 0 | 123 |
| `mpsa_suite/www/mpesa/stkpush.py` | 9 | 117 |

## Removed upstream features (2026-10-19)

Stanbic payouts (Stanbic Settings, Stanbic Sort Code, Stanbic connector, PesaLink), B2C payouts
against Loans, the POS Invoice / POS page / KlikPOS STK integration, the Event Booking hook, and
the Mpesa Payment Reconciliation tool (Mpesa Draft Payments, Mpesa Payments Invoices). The patch
`patches/remove_unused_upstream_features.py` drops their DocTypes on migrate.
