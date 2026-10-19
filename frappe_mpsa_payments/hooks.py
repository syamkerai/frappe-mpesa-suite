app_name = "frappe_mpsa_payments"
app_title = "Astral M-Pesa Suite"
app_publisher = "Astral Industries Limited"
app_description = "M-Pesa C2B, STK, B2C, B2B, bank feeds and reconciliation (Frappe/ERPNext v15 + v16)"
app_email = "finance@astralind.com"
app_license = "agpl-3.0"
# required_apps = ["erpnext", "hrms", "lending"]

# Apps
# ------------------

# required_apps = []


fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [
            ["is_system_generated", "=", 0],
            ["module", "=", "Frappe Mpsa Payments"],
        ],
    },
]


# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
    {
        "name": "frappe_mpsa_payments",
        "logo": "/assets/frappe_mpsa_payments/m-pesa.png",
        "title": "Astral M-Pesa",
        "route": "/app/m-pesa",
    }
]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/frappe_mpsa_payments/css/frappe_mpsa_payments.css"
app_include_js = "/assets/frappe_mpsa_payments/js/utils.js"

# include js, css files in header of web template
# web_include_css = "/assets/frappe_mpsa_payments/css/frappe_mpsa_payments.css"
# web_include_js = "/assets/frappe_mpsa_payments/js/frappe_mpsa_payments.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "frappe_mpsa_payments/public/scss/website"

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
    "B2C Disbursement Request": "public/js/b2c_status_check.js",
    "Mpesa C2B Payment Register": "public/js/c2b_reversal.js",
    "Integration Request": "public/js/c2b_verification.js",
    "Supplier": "public/js/supplier.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "frappe_mpsa_payments/public/icons.svg"

# Prepare ERPNext test data the version-appropriate way (v15 setup wizard / v16 bootstrap)
before_tests = "frappe_mpsa_payments.money_in.testing.before_tests"

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

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "frappe_mpsa_payments.utils.jinja_methods",
# 	"filters": "frappe_mpsa_payments.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "frappe_mpsa_payments.install.before_install"
after_install = "frappe_mpsa_payments.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "frappe_mpsa_payments.uninstall.before_uninstall"
# after_uninstall = "frappe_mpsa_payments.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "frappe_mpsa_payments.utils.before_app_install"
# after_app_install = "frappe_mpsa_payments.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "frappe_mpsa_payments.utils.before_app_uninstall"
# after_app_uninstall = "frappe_mpsa_payments.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "frappe_mpsa_payments.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

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
        "after_insert": "frappe_mpsa_payments.money_in.events.register_after_insert",
    },
    "Bank Transaction": {
        "before_insert": "frappe_mpsa_payments.money_in.bank_transaction.guard_duplicate",
        "on_submit": "frappe_mpsa_payments.money_in.bank_transaction.sync_voucher_clearance",
        "on_update_after_submit": "frappe_mpsa_payments.money_in.bank_transaction.sync_voucher_clearance",
    },
    "Payment Entry": {
        "before_insert": "frappe_mpsa_payments.money_in.payment_entry.before_insert",
        "before_validate": "frappe_mpsa_payments.money_in.payment_entry.before_validate",
    },
    "Journal Entry": {
        "before_insert": "frappe_mpsa_payments.money_in.payment_entry.journal_before_insert",
    },
    "Payment Request": {
        "before_validate": "frappe_mpsa_payments.money_in.payment_request.before_validate",
        "before_insert": "frappe_mpsa_payments.money_in.payment_request.before_insert",
    },
}

# Scheduled Tasks
# ---------------

scheduler_events = {
    "cron": {
        "*/15 * * * *": [
            "frappe_mpsa_payments.services.b2c_scheduler_service.update_b2c_disbursement_statuses",
            "frappe_mpsa_payments.services.b2c_postprocess_service.retry_unbooked_payouts",
            "frappe_mpsa_payments.services.b2c_scheduler_service.check_unsettled_payouts",
        ],
        "*/2 * * * *": ["frappe_mpsa_payments.money_in.events.every_two_minutes"],
        "*/5 * * * *": ["frappe_mpsa_payments.money_in.events.every_five_minutes"],
        "* * * * *": [
            "frappe_mpsa_payments.gateway.jobs.every_minute",
            "frappe_mpsa_payments.sandbox_lab.runner.check_waiting",
        ],
    },
    "hourly": [
        "frappe_mpsa_payments.services.b2c_pull_transaction_scheduler_service.run_hourly_pull_transactions"
    ],
}

# Testing
# -------

# before_tests = "frappe_mpsa_payments.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "frappe_mpsa_payments.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "frappe_mpsa_payments.task.get_dashboard_data"
# }
override_doctype_dashboards = {
    "Supplier": "frappe_mpsa_payments.money_out.supplier_dashboard.get_data"
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["frappe_mpsa_payments.utils.before_request"]
# after_request = ["frappe_mpsa_payments.utils.after_request"]

# Job Events
# ----------
# before_job = ["frappe_mpsa_payments.utils.before_job"]
# after_job = ["frappe_mpsa_payments.utils.after_job"]

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
# 	"frappe_mpsa_payments.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }


after_migrate = "frappe_mpsa_payments.frappe_mpsa_payments.migrate.after_migrate"
