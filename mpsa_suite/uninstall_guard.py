"""Refuse an uninstall of this app that would drop the M-Pesa tables (docs/RENAME_PLAN.md, R3).

Uninstalling an app drops the table of every DocType in its modules and deletes their Custom
Fields, Reports, Workspaces and site-made scripts. While this app owns the M-Pesa module, that is
every payout, receipt, prompt and setting. The app is to be replaced by mpsa_suite, which takes
the module over first; only then may this app go.
"""

import frappe

NEW_APP = "mpsa_suite"
NEW_MODULE = "Mpsa Suite"
FLAG = "mpsa_allow_old_uninstall"


def before_uninstall():
    """Frappe runs this app's own before_uninstall first, for a dry run too (it passes no flag)."""
    if frappe.conf.get(FLAG):
        return
    if NEW_APP in frappe.get_installed_apps() and frappe.db.get_value("Module Def", NEW_MODULE, "app_name") == NEW_APP:
        return  # the rename hand-over: mpsa_suite owns the module and moves the rest before deletion
    frappe.throw(
        "Uninstalling mpsa_suite would drop every M-Pesa table and delete its fields, reports "
        f"and scripts. Install {NEW_APP} first (the rename hand-over), or set {FLAG} = 1 in site_config "
        "only if you really mean to delete all M-Pesa data.",
        title="Uninstall refused",
    )
