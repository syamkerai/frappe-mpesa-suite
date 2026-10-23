# Licensing inventory

Prepared 2026-10-19 for Astral's legal review. Not legal advice.

Astral M-Pesa Suite is a fork of [navariltd/frappe-mpsa-payments](https://github.com/navariltd/frappe-mpsa-payments),
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

## Files that came from upstream and are still in the app (109)

| File | Lines changed since fork | Current lines |
|---|---:|---:|
| `frappe_mpsa_payments/accounting/journal_service.py` | 1 | 64 |
| `frappe_mpsa_payments/accounting/payment_entry_service.py` | 1 | 63 |
| `frappe_mpsa_payments/connectors/abstract.py` | 0 | 15 |
| `frappe_mpsa_payments/connectors/base_connector.py` | 12 | 324 |
| `frappe_mpsa_payments/connectors/factory.py` | 1 | 18 |
| `frappe_mpsa_payments/connectors/mpesa/b2c_connector.py` | 132 | 325 |
| `frappe_mpsa_payments/desktop_icon/m_pesa.json` | 0 | 21 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/b2c.py` | 6 | 33 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/base_class.py` | 0 | 79 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/m_pesa_api.py` | 626 | 1145 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/mpesa_quick_pay.py` | 7 | 378 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/mpesa_response_handler.py` | 93 | 413 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/payment_entry.py` | 170 | 536 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/process_request.py` | 0 | 54 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/sales_invoice.py` | 188 | 284 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/test_m_pesa_api.py` | 54 | 63 |
| `frappe_mpsa_payments/frappe_mpsa_payments/api/test_payment_entry.py` | 63 | 28 |
| `frappe_mpsa_payments/frappe_mpsa_payments/connectors/connectors.py` | 103 | 252 |
| `frappe_mpsa_payments/frappe_mpsa_payments/custom/sales_invoice_payment.json` | 0 | 99 |
| `frappe_mpsa_payments/frappe_mpsa_payments/dashboard_chart/mpesa_trend/mpesa_trend.json` | 0 | 31 |
| `frappe_mpsa_payments/frappe_mpsa_payments/dashboard_chart/total_incoming_(c2b)/total_incoming_(c2b).json` | 0 | 31 |
| `frappe_mpsa_payments/frappe_mpsa_payments/dashboard_chart/total_incoming_(express)/total_incoming_(express).json` | 0 | 31 |
| `frappe_mpsa_payments/frappe_mpsa_payments/dashboard_chart/total_outgoing_(b2c)/total_outgoing_(b2c).json` | 0 | 31 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_disbursement_request/b2c_disbursement_request.js` | 0 | 25 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_disbursement_request/b2c_disbursement_request.json` | 98 | 490 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_disbursement_request/b2c_disbursement_request.py` | 23 | 51 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_disbursement_request/test_b2c_disbursement_request.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/b2c_payment_disbursement.js` | 4 | 725 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/b2c_payment_disbursement.json` | 6 | 310 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/b2c_payment_disbursement.py` | 247 | 925 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/config.py` | 7 | 53 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/encoding_credentials.py` | 0 | 87 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement/test_b2c_payment_disbursement.py` | 474 | 165 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement_reference/b2c_payment_disbursement_reference.json` | 34 | 194 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement_reference/b2c_payment_disbursement_reference.py` | 24 | 65 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/b2c_payment_disbursement_reference/test_b2c_payment_disbursement_reference.py` | 137 | 68 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/custom_exceptions.py` | 0 | 40 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.js` | 0 | 8 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.json` | 68 | 304 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register.py` | 150 | 374 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register/mpesa_c2b_payment_register_list.js` | 0 | 139 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register/test_mpesa_c2b_payment_register.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.js` | 20 | 18 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.json` | 0 | 111 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.py` | 61 | 124 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_c2b_payment_register_url/test_mpesa_c2b_payment_register_url.py` | 177 | 92 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_conversion_rate/mpesa_conversion_rate.json` | 0 | 49 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_conversion_rate/mpesa_conversion_rate.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_express_request/mpesa_express_request.js` | 0 | 179 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_express_request/mpesa_express_request.json` | 53 | 457 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_express_request/mpesa_express_request.py` | 59 | 285 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_express_request/test_mpesa_express_request.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.js` | 0 | 8 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.json` | 0 | 59 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_public_key_certificate/mpesa_public_key_certificate.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_public_key_certificate/test_mpesa_public_key_certificate.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_reconciliation_priority/mpesa_reconciliation_priority.json` | 0 | 56 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_reconciliation_priority/mpesa_reconciliation_priority.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/account_balance.html` | 0 | 32 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/mpesa_connector.py` | 90 | 162 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/mpesa_settings.js` | 15 | 434 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/mpesa_settings.json` | 104 | 478 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/mpesa_settings.py` | 247 | 831 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/mpesa_settings_list.js` | 0 | 168 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_settings/test_mpesa_settings.py` | 334 | 392 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/payment_gateway/payment_gateway.js` | 0 | 6 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/payment_gateway/payment_gateway.json` | 0 | 51 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/payment_gateway/payment_gateway.py` | 0 | 8 |
| `frappe_mpsa_payments/frappe_mpsa_payments/doctype/payment_gateway/test_payment_gateway.py` | 0 | 9 |
| `frappe_mpsa_payments/frappe_mpsa_payments/frappe_mpsa_payments_dashboard/mpesa_transactions_overview_dashboard/mpesa_transactions_overview_dashboard.json` | 0 | 39 |
| `frappe_mpsa_payments/frappe_mpsa_payments/migrate.py` | 185 | 418 |
| `frappe_mpsa_payments/frappe_mpsa_payments/number_card/total_disbursed_b2c/total_disbursed_b2c.json` | 0 | 26 |
| `frappe_mpsa_payments/frappe_mpsa_payments/number_card/total_received_c2b/total_received_c2b.json` | 0 | 24 |
| `frappe_mpsa_payments/frappe_mpsa_payments/patches/c2b_payment_register.py` | 0 | 22 |
| `frappe_mpsa_payments/frappe_mpsa_payments/patches/payment_entry_custom_fields.py` | 0 | 60 |
| `frappe_mpsa_payments/frappe_mpsa_payments/patches/sales_invoice_patch.py` | 0 | 17 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/b2c_disbursement_summary/b2c_disbursement_summary.js` | 2 | 106 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/b2c_disbursement_summary/b2c_disbursement_summary.json` | 0 | 34 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/b2c_disbursement_summary/b2c_disbursement_summary.py` | 0 | 198 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.js` | 0 | 82 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.json` | 0 | 37 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_payment_summary_group_by_customer/c2b_payment_summary_group_by_customer.py` | 0 | 223 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_reconciliation_report/c2b_reconciliation_report.js` | 0 | 57 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_reconciliation_report/c2b_reconciliation_report.json` | 0 | 37 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/c2b_reconciliation_report/c2b_reconciliation_report.py` | 0 | 137 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/stk_push_request_status/stk_push_request_status.js` | 2 | 68 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/stk_push_request_status/stk_push_request_status.json` | 0 | 37 |
| `frappe_mpsa_payments/frappe_mpsa_payments/report/stk_push_request_status/stk_push_request_status.py` | 0 | 162 |
| `frappe_mpsa_payments/frappe_mpsa_payments/workspace/m_pesa/m_pesa.json` | 24 | 246 |
| `frappe_mpsa_payments/hooks.py` | 76 | 286 |
| `frappe_mpsa_payments/public/js/payment_entry.js` | 0 | 30 |
| `frappe_mpsa_payments/public/js/payment_request.js` | 0 | 77 |
| `frappe_mpsa_payments/public/js/sales_invoice.js` | 0 | 460 |
| `frappe_mpsa_payments/public/js/utils.js` | 38 | 775 |
| `frappe_mpsa_payments/services/b2c_postprocess_service.py` | 156 | 170 |
| `frappe_mpsa_payments/services/b2c_pull_transaction_scheduler_service.py` | 0 | 28 |
| `frappe_mpsa_payments/services/b2c_request_service.py` | 43 | 53 |
| `frappe_mpsa_payments/services/b2c_response_service.py` | 33 | 60 |
| `frappe_mpsa_payments/services/b2c_scheduler_service.py` | 48 | 71 |
| `frappe_mpsa_payments/utils/definitions.py` | 0 | 52 |
| `frappe_mpsa_payments/utils/doctype_names.py` | 2 | 9 |
| `frappe_mpsa_payments/utils/encoding_initiator_password.py` | 0 | 69 |
| `frappe_mpsa_payments/utils/helpers.py` | 0 | 16 |
| `frappe_mpsa_payments/utils/utils.py` | 85 | 455 |
| `frappe_mpsa_payments/workspace_sidebar/m_pesa.json` | 4 | 150 |
| `frappe_mpsa_payments/www/mpesa/stkpush.css` | 0 | 533 |
| `frappe_mpsa_payments/www/mpesa/stkpush.html` | 32 | 221 |
| `frappe_mpsa_payments/www/mpesa/stkpush.js` | 0 | 123 |
| `frappe_mpsa_payments/www/mpesa/stkpush.py` | 9 | 117 |

## Removed upstream features (2026-10-19)

Stanbic payouts (Stanbic Settings, Stanbic Sort Code, Stanbic connector, PesaLink), B2C payouts
against Loans, the POS Invoice / POS page / KlikPOS STK integration, the Event Booking hook, and
the Mpesa Payment Reconciliation tool (Mpesa Draft Payments, Mpesa Payments Invoices). The patch
`patches/remove_unused_upstream_features.py` drops their DocTypes on migrate.
