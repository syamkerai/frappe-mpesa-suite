app_name = "mpsa_suite"
app_title = "Mpsa Suite"
app_publisher = "Astral Industries Limited"
app_description = "M-Pesa C2B, STK, B2C, B2B, bank feeds and reconciliation (Frappe/ERPNext v15 + v16)"
app_email = "finance@astralind.com"
app_license = "agpl-3.0"

# Apps
# ------------------

# Salaries are paid from HRMS Payroll Entries and Salary Slips: both sites run HRMS.
required_apps = ["erpnext", "hrms"]


fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [
            ["is_system_generated", "=", 0],
            ["module", "=", "Mpsa Suite"],
        ],
    },
]


# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
    {
        "name": "mpsa_suite",
        "logo": "/assets/mpsa_suite/m-pesa.png",
        "title": "Mpsa Suite",
        "route": "/app/m-pesa",
    }
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/mpsa_suite/css/mpsa_suite.css"
# mpsa_send.js: the "Send via M-Pesa" / "Approve" confirmation every payout and payee account form uses
# ?v=<content hash>: /assets is cached as immutable for a year, so a changed script needs a new URL
# (assets.py). Private import name: Frappe collects every public name in hooks.py as a hook.
from mpsa_suite.assets import versioned as _versioned  # noqa: E402

# mpsa_known_names.js: the name M-Pesa gave each number before, as a badge where payouts are typed and approved
app_include_js = [_versioned("utils.js"), _versioned("mpsa_send.js"), _versioned("mpsa_known_names.js")]

# include js, css files in header of web template
# web_include_css = "/assets/mpsa_suite/css/mpsa_suite.css"
# web_include_js = "/assets/mpsa_suite/js/mpsa_suite.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "mpsa_suite/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
doctype_js = {
    "Sales Invoice": "public/js/sales_invoice.js",
    "Payment Request": "public/js/payment_request.js",
    "Payment Entry": "public/js/payment_entry.js",
    # Send Payment Proof on a paid payout (money_out.payment_print, R4.1)
    "B2C Disbursement Request": ["public/js/b2c_status_check.js", "public/js/payment_proof.js"],
    "B2C Payment Disbursement": "public/js/payment_proof.js",
    "Mpesa B2B Payment Request": "public/js/payment_proof.js",
    "Mpesa C2B Payment Register": "public/js/c2b_reversal.js",
    "Integration Request": "public/js/c2b_verification.js",
    "Supplier": "public/js/supplier.js",
    "Purchase Invoice": "public/js/purchase_invoice.js",
    "Purchase Order": "public/js/purchase_order.js",
    # HRMS only: a form that does not exist on the site never loads its script
    "Payroll Entry": "public/js/payroll_entry.js",
    "Employee": "public/js/employee.js",
    # a list of scripts: Frappe 15 and 16 load each (frappe.desk.form.meta.get_code_files_via_hooks)
    "Expense Claim": ["public/js/expense_claim.js", "public/js/expense_claim_payees.js"],
    "Employee Advance": "public/js/employee_advance.js",
    "Salary Slip": "public/js/salary_slip.js",
    "Sales Order": "public/js/sales_order.js",
    "Customer": "public/js/customer.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# Loaded after the DocType's own list script (HRMS's, where it has one): each extends
# frappe.listview_settings[doctype], never replaces it.
doctype_list_js = {
    "Expense Claim": ["public/js/expense_claim_list.js", "public/js/expense_claim_payees_list.js"],
    "Employee Advance": "public/js/employee_advance_list.js",
    "Salary Slip": "public/js/salary_slip_list.js",
}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "mpsa_suite/public/icons.svg"

# What the desk may offer this user (bootinfo.mpsa): buttons show only to who the server accepts
boot_session = "mpsa_suite.boot.boot_session"

# Prepare ERPNext test data the version-appropriate way (v15 setup wizard / v16 bootstrap)
before_tests = "mpsa_suite.money_in.testing.before_tests"

# Public pay page for Payment Intents: /pay/<token>
website_route_rules = [{"from_route": "/pay/<token>", "to_route": "pay"}]

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment: the 80 mm payment prints read only through these
# (permission-checked, phones masked: money_out.payment_print)
jinja = {
    "methods": [
        "mpsa_suite.money_out.payment_print.mpsa_payment_advice",
        "mpsa_suite.money_out.payment_print.mpsa_payout_proof",
    ],
}

# Installation
# ------------

# Rebuilds Frappe's module map when it lacks mpsa_suite (a Redis cache from before `bench get-app`):
# Frappe 15's install-app would otherwise sync no DocType and fail in after_install.
before_install = "mpsa_suite.install.before_install"
after_install = "mpsa_suite.install.after_install"
# Runs last in install-app: files everything still under the old module "Frappe Mpsa Payments"
# under "Mpsa Suite", so nothing is left for an uninstall of the old app to delete (R1).
after_sync = "mpsa_suite.handover.after_sync"

# Uninstallation
# ------------

# Refuses every uninstall of this app unless site_config has mpsa_allow_uninstall = 1 (R2).
# Frappe runs an app's own before_uninstall even when it is missing from installed_apps.
before_uninstall = "mpsa_suite.uninstall_guard.before_uninstall"
# after_uninstall = "mpsa_suite.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "mpsa_suite.utils.before_app_install"
# after_app_install = "mpsa_suite.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "mpsa_suite.utils.before_app_uninstall"
# after_app_uninstall = "mpsa_suite.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "mpsa_suite.notifications.get_notification_config"

# Permissions
# -----------
# Salaries (what each employee is paid, to which phone or bank account) only for who may read
# Salary Slips, who pays or administers them, and the record's creator (decision D4,
# money_out.salary_access)
_SALARY_DOCTYPES = (
    "M-Pesa Payment Schedule",
    "B2C Payment Disbursement",
    "B2C Disbursement Request",
    "Mpesa B2B Payment Request",
    "Mpesa B2C Status Query",
)
permission_query_conditions = {dt: "mpsa_suite.money_out.salary_access.query_conditions" for dt in _SALARY_DOCTYPES}
has_permission = {dt: "mpsa_suite.money_out.salary_access.has_permission" for dt in _SALARY_DOCTYPES}
# their Versions too (System Manager reads every Version): salary_access.version_*
permission_query_conditions["Version"] = "mpsa_suite.money_out.salary_access.version_query_conditions"
has_permission["Version"] = "mpsa_suite.money_out.salary_access.version_has_permission"
# the known M-Pesa names (round 4.5): a name seen on salary payouts only, for who may see salaries
permission_query_conditions["M-Pesa Known Name"] = "mpsa_suite.money_out.known_names.query_conditions"
has_permission["M-Pesa Known Name"] = "mpsa_suite.money_out.known_names.has_permission"

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Money-in pipeline. Every handler checks its own switch in M-Pesa Suite Settings,
# so nothing changes until a switch is turned on.
doc_events = {
    "Mpesa C2B Payment Register": {
        "after_insert": "mpsa_suite.money_in.events.register_after_insert",
    },
    "Bank Transaction": {
        "before_insert": "mpsa_suite.money_in.bank_transaction.guard_duplicate",
        "on_submit": "mpsa_suite.money_in.bank_transaction.sync_voucher_clearance",
        "on_update_after_submit": "mpsa_suite.money_in.bank_transaction.sync_voucher_clearance",
    },
    "Payment Entry": {
        "before_insert": "mpsa_suite.money_in.payment_entry.before_insert",
        "before_validate": "mpsa_suite.money_in.payment_entry.before_validate",
        # a claim's or advance's paid total checked again after each of its payments (R4.1)
        "on_submit": "mpsa_suite.money_out.employee_documents.check_paid_total_later",
        # an entry a B2B request links can still be cancelled, or deleted as a draft
        "on_cancel": [
            "mpsa_suite.money_out.b2b_entries.allow_cancel",
            "mpsa_suite.money_out.employee_documents.check_paid_total_later",
        ],
        "on_trash": "mpsa_suite.money_out.b2b_entries.unlink_deleted",
    },
    "Journal Entry": {
        "before_insert": "mpsa_suite.money_in.payment_entry.journal_before_insert",
        # HRMS: no bank or cash entry for a payroll while M-Pesa pays salaries of it (money_out.payroll)
        "validate": "mpsa_suite.money_out.payroll.guard_bank_entry",
        "on_submit": "mpsa_suite.money_out.employee_documents.check_paid_total_later",
        "on_cancel": [
            "mpsa_suite.money_out.b2b_entries.allow_cancel",
            "mpsa_suite.money_out.employee_documents.check_paid_total_later",
        ],
        "on_trash": "mpsa_suite.money_out.b2b_entries.unlink_deleted",
    },
    "Payment Request": {
        "before_validate": "mpsa_suite.money_in.payment_request.before_validate",
        "before_insert": "mpsa_suite.money_in.payment_request.before_insert",
    },
    # HRMS: no cancel while M-Pesa salaries of the payroll (or of the slip) are paid or on their way
    "Payroll Entry": {
        "before_cancel": "mpsa_suite.money_out.payroll.before_cancel",
    },
    "Salary Slip": {
        "before_cancel": "mpsa_suite.money_out.payroll.slip_before_cancel",
    },
    # HRMS: no cancel while an M-Pesa payout of the claim or advance is paid or on its way
    "Expense Claim": {
        "before_cancel": "mpsa_suite.money_out.employee_documents.before_cancel",
        # merchants and casual payees paid straight from the claim (money_out.claim_payees)
        "validate": "mpsa_suite.money_out.claim_payees.validate_claim",
        "before_update_after_submit": "mpsa_suite.money_out.claim_payees.keep_results",
        "on_cancel": "mpsa_suite.money_out.claim_payees.on_cancel_claim",
    },
    "Employee Advance": {
        "before_cancel": "mpsa_suite.money_out.employee_documents.before_cancel",
        "on_cancel": "mpsa_suite.money_out.employee_documents.on_cancel",  # its failed payouts do not hold it
    },
    # HRMS: a mistyped Kenyan Cell Number refused when it is typed (Pay employee (phone), HR's SMS)
    "Employee": {
        "validate": "mpsa_suite.money_out.employee_accounts.check_cell_number",
    },
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "cron": {
        "*/15 * * * *": [
            "mpsa_suite.services.b2c_scheduler_service.update_b2c_disbursement_statuses",
            "mpsa_suite.services.b2c_postprocess_service.retry_unbooked_payouts",
            "mpsa_suite.services.b2c_scheduler_service.check_unsettled_payouts",
            "mpsa_suite.money_out.b2b_entries.refresh_entries",
        ],
        "*/2 * * * *": ["mpsa_suite.money_in.events.every_two_minutes"],
        "*/5 * * * *": [
            "mpsa_suite.money_in.events.every_five_minutes",
            "mpsa_suite.wallet.balances.run",
        ],
        "* * * * *": [
            "mpsa_suite.gateway.jobs.every_minute",
            "mpsa_suite.sandbox_lab.runner.check_waiting",
        ],
    },
    "hourly": [
        "mpsa_suite.services.b2c_pull_transaction_scheduler_service.run_hourly_pull_transactions"
    ],
    # one "needs attention" alert to the Notify Role, from every section below (money_out.attention)
    "daily": ["mpsa_suite.money_out.attention.daily_attention"],
}

# M-Pesa Suite's own hooks
# ------------------------
# DocTypes whose B2C payouts pay people no ERPNext party names (a bulk payout sheet's rows, a
# petty cash phone's top-ups): {DocType: dotted path of its handler}. The handler checks, books
# and reconciles its own rows (money_out.partyless); each feature adds its line.
mpsa_partyless_payouts = {}
# The daily "needs attention" alert's sections: dotted paths of functions returning (title, [lines])
# (money_out.attention); each feature adds its own.
mpsa_attention_sections = []

# Testing
# -------

# before_tests = "mpsa_suite.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "mpsa_suite.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "mpsa_suite.task.get_dashboard_data"
# }
override_doctype_dashboards = {
    "Supplier": "mpsa_suite.money_out.supplier_dashboard.get_data",
    "Purchase Invoice": "mpsa_suite.money_out.document_dashboards.get_data",
    "Purchase Order": "mpsa_suite.money_out.document_dashboards.get_data",
    "Employee": "mpsa_suite.money_out.payroll.get_employee_dashboard_data",
    "Payroll Entry": "mpsa_suite.money_out.payroll.get_dashboard_data",
    # Frappe passes only ``data``, not the DocType: one function per DocType (money_out.connections)
    "Expense Claim": "mpsa_suite.money_out.connections.get_expense_claim_data",
    "Employee Advance": "mpsa_suite.money_out.connections.get_employee_advance_data",
    "Salary Slip": "mpsa_suite.money_out.connections.get_salary_slip_data",
    "Sales Order": "mpsa_suite.money_out.connections.get_sales_order_data",
    "Sales Invoice": "mpsa_suite.money_out.connections.get_sales_invoice_data",
    "Customer": "mpsa_suite.money_out.connections.get_customer_data",
    "Payment Entry": "mpsa_suite.money_out.connections.get_payment_entry_data",
    "Journal Entry": "mpsa_suite.money_out.connections.get_journal_entry_data",
    "Payment Request": "mpsa_suite.money_out.connections.get_payment_request_data",
    "POS Invoice": "mpsa_suite.money_out.connections.get_pos_invoice_data",
    "Bank Transaction": "mpsa_suite.money_out.connections.get_bank_transaction_data",
    "Bank Account": "mpsa_suite.money_out.connections.get_bank_account_data",
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]
# A Paid B2B request links its Payment / Journal Entry. Cancelling the entry from the desk must not
# offer "Cancel All" (a Paid request cannot be cancelled); b2b_entries.allow_cancel lets the entry
# go. A Purchase Invoice or Order a request links then gets Frappe's plain "is linked" refusal.
auto_cancel_exempted_doctypes = ["Mpesa B2B Payment Request"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["mpsa_suite.utils.before_request"]
# after_request = ["mpsa_suite.utils.after_request"]

# Job Events
# ----------
# before_job = ["mpsa_suite.utils.before_job"]
# after_job = ["mpsa_suite.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"mpsa_suite.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }


after_migrate = [
    "mpsa_suite.mpsa_suite.migrate.after_migrate",
    "mpsa_suite.handover.after_migrate",
]

# Hand-over from the old package name (see handover.py)
# ------------------------------------------------------
# Uninstalling frappe_mpsa_payments: moves what is left in its modules to mpsa_suite first.
# Uninstalling mpsa_suite: refused unless mpsa_allow_uninstall = 1 (second check of R2).
before_app_uninstall = "mpsa_suite.handover.before_app_uninstall"

# Old /api/method/frappe_mpsa_payments.* paths keep working: Safaricom holds registered C2B and Pull
# URLs and in-flight callback URLs, integrators and open pay pages call the old paths.
# Guest (callback / pay page / Payments API) entries first.
override_whitelisted_methods = {
    "frappe_mpsa_payments.frappe_mpsa_payments.api.b2c.b2c_results_callback": "mpsa_suite.mpsa_suite.api.b2c.b2c_results_callback",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.account_balance_result": "mpsa_suite.mpsa_suite.api.m_pesa_api.account_balance_result",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.balance_query_callback": "mpsa_suite.mpsa_suite.api.m_pesa_api.balance_query_callback",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.c2b_verification_result": "mpsa_suite.mpsa_suite.api.m_pesa_api.c2b_verification_result",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.confirmation": "mpsa_suite.mpsa_suite.api.m_pesa_api.confirmation",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_queue_timeout": "mpsa_suite.mpsa_suite.api.m_pesa_api.handle_queue_timeout",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_transaction_status_result": "mpsa_suite.mpsa_suite.api.m_pesa_api.handle_transaction_status_result",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.pull_registration_result": "mpsa_suite.mpsa_suite.api.m_pesa_api.pull_registration_result",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.stk_push_callback": "mpsa_suite.mpsa_suite.api.m_pesa_api.stk_push_callback",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.validation": "mpsa_suite.mpsa_suite.api.m_pesa_api.validation",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.process_balance_info": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.process_balance_info",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.pull_transaction_callback": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.pull_transaction_callback",  # guest
    "frappe_mpsa_payments.gateway.v1.cancel": "mpsa_suite.gateway.v1.cancel",  # guest
    "frappe_mpsa_payments.gateway.v1.create_payment_link": "mpsa_suite.gateway.v1.create_payment_link",  # guest
    "frappe_mpsa_payments.gateway.v1.create_prompt": "mpsa_suite.gateway.v1.create_prompt",  # guest
    "frappe_mpsa_payments.gateway.v1.get_status": "mpsa_suite.gateway.v1.get_status",  # guest
    "frappe_mpsa_payments.gateway.v1.list_payments": "mpsa_suite.gateway.v1.list_payments",  # guest
    "frappe_mpsa_payments.gateway.v1.page_prompt": "mpsa_suite.gateway.v1.page_prompt",  # guest
    "frappe_mpsa_payments.gateway.v1.page_status": "mpsa_suite.gateway.v1.page_status",  # guest
    "frappe_mpsa_payments.gateway.v1.resend_prompt": "mpsa_suite.gateway.v1.resend_prompt",  # guest
    "frappe_mpsa_payments.gateway.v1.verify_receipt": "mpsa_suite.gateway.v1.verify_receipt",  # guest
    "frappe_mpsa_payments.money_out.api.b2b_queue_timeout": "mpsa_suite.money_out.api.b2b_queue_timeout",  # guest
    "frappe_mpsa_payments.money_out.api.b2b_result": "mpsa_suite.money_out.api.b2b_result",  # guest
    "frappe_mpsa_payments.money_out.api.b2c_status_result": "mpsa_suite.money_out.api.b2c_status_result",  # guest
    "frappe_mpsa_payments.money_out.api.reversal_queue_timeout": "mpsa_suite.money_out.api.reversal_queue_timeout",  # guest
    "frappe_mpsa_payments.money_out.api.reversal_result": "mpsa_suite.money_out.api.reversal_result",  # guest
    "frappe_mpsa_payments.money_out.api.status_queue_timeout": "mpsa_suite.money_out.api.status_queue_timeout",  # guest
    "frappe_mpsa_payments.sandbox_lab.api.ping": "mpsa_suite.sandbox_lab.api.ping",  # guest
    "frappe_mpsa_payments.utils.utils.convert_amount_to_kes": "mpsa_suite.utils.utils.convert_amount_to_kes",  # guest
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.bulk_pull_transactions": "mpsa_suite.mpsa_suite.api.m_pesa_api.bulk_pull_transactions",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.check_transaction_status": "mpsa_suite.mpsa_suite.api.m_pesa_api.check_transaction_status",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.get_account_balance": "mpsa_suite.mpsa_suite.api.m_pesa_api.get_account_balance",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.initiate_stk_push": "mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.pull_transactions": "mpsa_suite.mpsa_suite.api.m_pesa_api.pull_transactions",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.get_mpesa_payments": "mpsa_suite.mpsa_suite.api.mpesa_quick_pay.get_mpesa_payments",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile": "mpsa_suite.mpsa_suite.api.mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.process_mpesa": "mpsa_suite.mpsa_suite.api.mpesa_quick_pay.process_mpesa",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay.update_mpesa_after_invoice_submission": "mpsa_suite.mpsa_suite.api.mpesa_quick_pay.update_mpesa_after_invoice_submission",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.get_outstanding_invoices": "mpsa_suite.mpsa_suite.api.payment_entry.get_outstanding_invoices",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.initiate_payment_entry_stk": "mpsa_suite.mpsa_suite.api.payment_entry.initiate_payment_entry_stk",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_mop_from_payment_gateway": "mpsa_suite.mpsa_suite.api.sales_invoice.get_mop_from_payment_gateway",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_payment_gateway_from_mop": "mpsa_suite.mpsa_suite.api.sales_invoice.get_payment_gateway_from_mop",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.get_stk_amount": "mpsa_suite.mpsa_suite.api.sales_invoice.get_stk_amount",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.initiate_invoice_stk_push": "mpsa_suite.mpsa_suite.api.sales_invoice.initiate_invoice_stk_push",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.initiate_row_stk_push": "mpsa_suite.mpsa_suite.api.sales_invoice.initiate_row_stk_push",
    "frappe_mpsa_payments.frappe_mpsa_payments.api.sales_invoice.validate_stk_push_eligibility": "mpsa_suite.mpsa_suite.api.sales_invoice.validate_stk_push_eligibility",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request.mpesa_express_request.create_new_request": "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.create_new_request",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request.mpesa_express_request.get_request_status": "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.get_request_status",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request.mpesa_express_request.get_stkpush_defaults": "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.get_stkpush_defaults",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request.mpesa_express_request.retry_stkpush": "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.retry_stkpush",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.bulk_register_pull_transactions": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.bulk_register_pull_transactions",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.get_doctype_fields": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.get_doctype_fields",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.register_pull_transaction": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.register_pull_transaction",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status": "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier": "mpsa_suite.mpsa_suite.doctype.supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier",
    "frappe_mpsa_payments.money_in.c2b_verify.release": "mpsa_suite.money_in.c2b_verify.release",
    "frappe_mpsa_payments.money_out.api.check_b2c_status": "mpsa_suite.money_out.api.check_b2c_status",
    "frappe_mpsa_payments.money_out.api.make_reversal": "mpsa_suite.money_out.api.make_reversal",
    "frappe_mpsa_payments.sandbox_lab.api.add_scenarios": "mpsa_suite.sandbox_lab.api.add_scenarios",
    "frappe_mpsa_payments.sandbox_lab.api.check_now": "mpsa_suite.sandbox_lab.api.check_now",
    "frappe_mpsa_payments.sandbox_lab.api.replay_callback": "mpsa_suite.sandbox_lab.api.replay_callback",
    "frappe_mpsa_payments.sandbox_lab.api.reset_case": "mpsa_suite.sandbox_lab.api.reset_case",
    "frappe_mpsa_payments.sandbox_lab.api.run_case": "mpsa_suite.sandbox_lab.api.run_case",
    "frappe_mpsa_payments.sandbox_lab.api.run_health_check": "mpsa_suite.sandbox_lab.api.run_health_check",
    "frappe_mpsa_payments.sandbox_lab.api.run_ready": "mpsa_suite.sandbox_lab.api.run_ready",
    "frappe_mpsa_payments.sandbox_lab.api.simulate_c2b": "mpsa_suite.sandbox_lab.api.simulate_c2b",
    "frappe_mpsa_payments.utils.utils.get_mode_of_payment_account": "mpsa_suite.utils.utils.get_mode_of_payment_account",
    "frappe_mpsa_payments.www.mpesa.stkpush.check_payment_status": "mpsa_suite.www.mpesa.stkpush.check_payment_status",
    "frappe_mpsa_payments.www.mpesa.stkpush.initiate_stk_push": "mpsa_suite.www.mpesa.stkpush.initiate_stk_push",
    "frappe_mpsa_payments.www.mpesa.stkpush.retry_stkpush": "mpsa_suite.www.mpesa.stkpush.retry_stkpush",
}
