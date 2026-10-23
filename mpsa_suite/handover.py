"""Hand-over from the app's old package name (frappe_mpsa_payments) to mpsa_suite.

Both packages ship the same DocTypes under the same names, so they share the same tables.
Installing mpsa_suite next to frappe_mpsa_payments re-imports those DocTypes into the
"Mpsa Suite" module. Uninstalling frappe_mpsa_payments afterwards deletes everything that is
still in a module the old app owns (Module Def.app_name), DROPs the table of every DocType
in it and deletes every record linked to it (Custom Field, Property Setter, Report,
Workspace, Server Script, Client Script, ...).

rehome() files every record still under the old module under the new one. It runs at the end
of install-app (after_sync, R1), on every migrate (after_migrate) and when the old app is
uninstalled: Frappe calls before_app_uninstall() with the name of the app being removed, after
the old app's own before_uninstall and before it reads the Module Defs, so by the time it looks
there is nothing left to delete or drop. Any other module the old app still owns that has
something in it (Navari's "Stanbic" on older sites) is handed to mpsa_suite instead of being
deleted. See docs/RENAME_SPIKE.md.
"""

import frappe

OLD_APP = "frappe_mpsa_payments"
NEW_APP = "mpsa_suite"
OLD_MODULE = "Frappe Mpsa Payments"
NEW_MODULE = "Mpsa Suite"
# (doctype, field) holding an app name: v16 Workspace / Workspace Sidebar / Desktop Icon `app`,
# and the landing page a user picked on the apps screen (R6)
APP_FIELDS = (
    ("Workspace", "app"),
    ("Workspace Sidebar", "app"),
    ("Desktop Icon", "app"),
    ("User", "default_app"),
)
# the site-wide landing page (System Settings is a Single: its values live in tabSingles)
SINGLE_APP_FIELDS = (("System Settings", "default_app"),)


def _module_link_fields() -> list:
    """(doctype, fieldname) of DocType.module and every Link to Module Def on the site."""
    fields = [("DocType", "module")]
    for row in frappe.get_all(
        "DocField", filters={"fieldtype": "Link", "options": "Module Def"}, fields=["parent", "fieldname"]
    ):
        if (row.parent, row.fieldname) not in fields:
            fields.append((row.parent, row.fieldname))
    return fields


def _usable(doctype: str, fieldname: str) -> bool:
    if not frappe.db.exists("DocType", doctype):
        return False
    meta = frappe.get_meta(doctype)
    return not meta.issingle and frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, fieldname)


def module_contents(module: str) -> dict:
    """{"Doctype.field": count} of everything filed under ``module``."""
    found = {}
    for doctype, fieldname in _module_link_fields():
        if _usable(doctype, fieldname):
            n = frappe.db.count(doctype, {fieldname: module})
            if n:
                found[f"{doctype}.{fieldname}"] = n
    return found


def rehome() -> dict:
    """Move every record filed under the old module (and old app) to the new one. Idempotent."""
    if not frappe.db.exists("Module Def", NEW_MODULE):
        return {}
    moved = {}
    for doctype, fieldname in _module_link_fields():
        if not _usable(doctype, fieldname):
            continue
        n = frappe.db.count(doctype, {fieldname: OLD_MODULE})
        if n:
            frappe.db.sql(
                f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE `{fieldname}` = %s",
                (NEW_MODULE, OLD_MODULE),
            )
            moved[f"{doctype}.{fieldname}"] = n
    for doctype, fieldname in APP_FIELDS:
        if _usable(doctype, fieldname):
            n = frappe.db.count(doctype, {fieldname: OLD_APP})
            if n:
                frappe.db.sql(
                    f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE `{fieldname}` = %s", (NEW_APP, OLD_APP)
                )
                moved[f"{doctype}.{fieldname}"] = n
    for doctype, fieldname in SINGLE_APP_FIELDS:
        filters = (doctype, fieldname, OLD_APP)
        n = frappe.db.sql(
            "SELECT COUNT(*) FROM `tabSingles` WHERE doctype = %s AND field = %s AND value = %s", filters
        )[0][0]
        if n:
            frappe.db.sql(
                "UPDATE `tabSingles` SET value = %s WHERE doctype = %s AND field = %s AND value = %s",
                (NEW_APP, *filters),
            )
            moved[f"{doctype}.{fieldname}"] = n
    if moved:
        frappe.clear_cache()
    return moved


def after_sync() -> None:
    """R1: install-app runs this last, so the check before uninstalling the old app can expect zero."""
    moved = rehome()
    if moved:
        print(f"mpsa_suite hand-over: moved to '{NEW_MODULE}': {moved}")


def before_app_uninstall(app_name: str) -> None:
    """Runs for every app uninstall; acts only when the old package or mpsa_suite is being removed."""
    if app_name == NEW_APP:
        # Second check only (R2): mpsa_suite's own before_uninstall has already refused, and it
        # runs even when mpsa_suite is missing from installed_apps; this hook does not.
        from mpsa_suite.uninstall_guard import refuse_unless_allowed

        refuse_unless_allowed()
        return
    if app_name != OLD_APP:
        return
    moved = rehome()
    kept = []
    for module in frappe.get_all("Module Def", filters={"app_name": OLD_APP}, pluck="name"):
        if module_contents(module):
            frappe.db.set_value("Module Def", module, "app_name", NEW_APP, update_modified=False)
            kept.append(module)
    frappe.db.commit()
    print(f"mpsa_suite hand-over: moved to '{NEW_MODULE}': {moved or 'nothing'}; "
          f"modules kept for {NEW_APP}: {kept or 'none'}")


def after_migrate() -> None:
    """Catch records the old app's after_migrate filed under its module while both are installed."""
    moved = rehome()
    if moved:
        print(f"mpsa_suite hand-over: moved to '{NEW_MODULE}': {moved}")


def old_patch_name(patch_module: str) -> str:
    """mpsa_suite.mpsa_suite.patches.x -> frappe_mpsa_payments.frappe_mpsa_payments.patches.x"""
    return ".".join(OLD_APP if part == NEW_APP else part for part in patch_module.split("."))


def ran_under_old_name(patch_module: str) -> bool:
    """R8: True when this patch already ran on the site under the old package name.

    Install-app records every mpsa_suite patch as done without running it, but if mpsa_suite
    reaches installed_apps another way (a restore, a manual fix) the next migrate would run them
    all again. Each patch returns at once when its twin is in Patch Log as done (skipped = 0 is
    what Frappe itself counts as executed; install-app writes its rows that way too).
    """
    twin = old_patch_name(patch_module)
    if twin == patch_module or not frappe.db.exists("Patch Log", {"patch": twin, "skipped": 0}):
        return False
    print(f"mpsa_suite: {patch_module} already ran as {twin}; nothing to do")
    return True
