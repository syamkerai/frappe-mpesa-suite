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


# Columns the suite looks receipts up by on core tables that ship without an index. Unindexed,
# every C2B / STK / sweep lookup scans the table, and the locking duplicate checks lock all of it.
LOOKUP_INDEXES = (
    ("Bank Transaction", "reference_number"),
    ("Bank Transaction", "transaction_id"),
    ("Payment Entry", "reference_no"),
    ("Integration Request", "request_id"),
)
# Fieldtypes Frappe refuses to index (DocType check_unique_and_text, v15 and v16). With a
# search_index setter on one, every Custom Field / Customize Form save on the DocType fails.
UNINDEXABLE = ("Text", "Long Text", "Small Text", "Code", "Text Editor")


def ensure_lookup_indexes() -> None:
    """Index the receipt columns, idempotently (after_install and every migrate).

    A ``search_index`` Property Setter keeps schema sync from dropping the index when the
    owning app's DocType is re-synced; ``add_index`` (named like sync names it) builds it now.
    Text columns (Bank Transaction's Reference Number is Small Text) get no setter, and one
    left by an earlier release is removed on every run: this hook runs after the patches, so
    a one-shot patch alone cannot keep it gone. Schema sync never adds or drops indexes on
    text columns, and MariaDB builds a prefix index for them, so their index stays.
    """
    from frappe.custom.doctype.property_setter.property_setter import make_property_setter

    for doctype, fieldname in LOOKUP_INDEXES:
        if not frappe.db.exists("DocType", doctype):
            continue
        df = frappe.get_meta(doctype).get_field(fieldname)
        if not df:
            continue
        setter = {"doc_type": doctype, "field_name": fieldname, "property": "search_index"}
        try:
            if df.fieldtype in UNINDEXABLE:
                if frappe.db.exists("Property Setter", setter):
                    frappe.db.delete("Property Setter", setter)
                    frappe.clear_cache(doctype=doctype)
            elif not frappe.db.exists("Property Setter", setter):
                make_property_setter(
                    doctype, fieldname, "search_index", 1, "Check", validate_fields_for_doctype=False
                )
            frappe.db.add_index(doctype, [fieldname], index_name=fieldname)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"M-Pesa Suite: could not index {doctype}.{fieldname}")


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


def before_install() -> None:
    """Make sure Frappe's module map lists mpsa_suite before install-app syncs the app.

    Frappe builds the map once per process, in frappe.init, from the site's Redis cache key
    ``app_modules``. `bench get-app` adds mpsa_suite to apps.txt but never clears that key, so on
    a bench whose cache was warm before, the map has no mpsa_suite. Frappe 15's install-app then
    syncs no DocType, still commits the Module Def, installed_apps and the Patch Log rows, and
    after_install fails with "Module Mpsa Suite not found". Frappe 16 rebuilds the map itself
    after add_module_defs (installer.install_app); this does the same on both, before the sync.
    """
    if "mpsa_suite" not in (getattr(frappe.local, "app_modules", None) or {}):
        frappe.cache.delete_value("app_modules")
        frappe.setup_module_map(include_all_apps=True)


def after_install() -> None:
    # install-app does not run after_migrate; without this a fresh site misses the
    # ERPNext custom fields (company, customer, payment_entry, ...) until the next migrate.
    from mpsa_suite.mpsa_suite.migrate import after_migrate

    after_migrate()  # also creates roles and M-Pesa Suite Settings
    approved_phones_only_on_new_sites()


# The app's name before the rename (docs/RENAME_SPIKE.md): a site that has it becomes mpsa_suite by
# install-app next to it, which runs after_install on a site with live payouts.
OLD_APP = "frappe_mpsa_payments"
# Payouts that pay employees by B2C: any row means the site has paid employees before this release.
B2C_HISTORY = ("B2C Disbursement Request", "B2C Payment Disbursement")


def has_b2c_history() -> bool:
    """Whether the site paid by B2C before mpsa_suite was installed: it still has the old app (the
    rename installs mpsa_suite next to it), or any B2C payout row."""
    if OLD_APP in frappe.get_installed_apps():
        return True
    return any(frappe.db.table_exists(dt) and frappe.get_all(dt, limit=1, pluck="name") for dt in B2C_HISTORY)


def approved_phones_only_on_new_sites() -> None:
    """A new site pays employees by B2C only to phones approved on their Employee M-Pesa Accounts
    (plan C.9). A site with B2C history keeps its setting (off until its employees' phones are
    approved): migrate never runs this, and install-app does not switch it on for a site that
    still has the old app (the rename, Trident and Astral) or any B2C payout."""
    if not (frappe.db.exists("DocType", "M-Pesa Suite Settings") and frappe.get_meta(
            "M-Pesa Suite Settings").has_field("b2c_approved_phones_only")):
        return
    if has_b2c_history():
        return
    frappe.db.set_single_value("M-Pesa Suite Settings", "b2c_approved_phones_only", 1)
