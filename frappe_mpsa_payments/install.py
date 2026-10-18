"""Install / migrate setup for M-Pesa Suite: roles and default settings."""

import frappe

SUITE_ROLES = {
    "M-Pesa Manager": "Configures M-Pesa, rotates secrets, releases stuck payouts, approves reversals.",
    "M-Pesa Payer": "Sends B2C/B2B payouts from approved documents.",
    "M-Pesa Collector": "Sends STK payment prompts and attaches received payments to orders.",
    "M-Pesa Viewer": "Read-only access to M-Pesa transactions and reports.",
}


def ensure_roles() -> None:
    for role_name, description in SUITE_ROLES.items():
        if frappe.db.exists("Role", role_name):
            continue
        role = frappe.new_doc("Role")
        role.role_name = role_name
        role.desk_access = 1
        if role.meta.has_field("description"):
            role.description = description
        role.insert(ignore_permissions=True)


def ensure_suite_settings() -> None:
    if not frappe.db.exists("DocType", "M-Pesa Suite Settings"):
        return
    settings = frappe.get_single("M-Pesa Suite Settings")
    if not settings.get_password("callback_token", raise_exception=False):
        settings.flags.ignore_permissions = True
        settings.save()  # validate() generates the token and keeps field defaults


def after_install() -> None:
    # install-app does not run after_migrate; without this a fresh site misses the
    # ERPNext custom fields (company, customer, payment_entry, ...) until the next migrate.
    from frappe_mpsa_payments.frappe_mpsa_payments.migrate import after_migrate

    after_migrate()  # also creates roles and M-Pesa Suite Settings
