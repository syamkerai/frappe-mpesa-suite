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
    backfill_single_defaults("M-Pesa Suite Settings")
    settings = frappe.get_single("M-Pesa Suite Settings")
    if not settings.get_password("callback_token", raise_exception=False):
        settings.flags.ignore_permissions = True
        settings.save()  # validate() generates the token and keeps field defaults


def backfill_single_defaults(doctype: str) -> None:
    """Give newly added Single fields their JSON default.

    Frappe only applies defaults when a Single has no stored values at all, so on an upgraded
    site every field added in a later release would read as empty/0 instead of its default.
    """
    stored = set(frappe.db.sql("SELECT field FROM `tabSingles` WHERE doctype = %s", doctype, pluck="field"))
    if not stored:
        return  # never saved: Frappe applies defaults itself
    for df in frappe.get_meta(doctype).fields:
        if df.default not in (None, "") and df.fieldname not in stored and not df.fieldtype.endswith("Break"):
            frappe.db.set_single_value(doctype, df.fieldname, df.default)


def after_install() -> None:
    # install-app does not run after_migrate; without this a fresh site misses the
    # ERPNext custom fields (company, customer, payment_entry, ...) until the next migrate.
    from frappe_mpsa_payments.frappe_mpsa_payments.migrate import after_migrate

    after_migrate()  # also creates roles and M-Pesa Suite Settings
