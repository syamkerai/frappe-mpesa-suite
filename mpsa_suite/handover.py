"""Hand-over from the app's old package name (frappe_mpsa_payments) to mpsa_suite.

Both packages ship the same DocTypes under the same names, so they share the same tables.
Installing mpsa_suite next to frappe_mpsa_payments re-imports those DocTypes into the
"Mpsa Suite" module. Uninstalling frappe_mpsa_payments afterwards deletes everything that is
still in a module the old app owns (Module Def.app_name), DROPs the table of every DocType
in it and deletes every record linked to it (Custom Field, Property Setter, Report,
Workspace, Server Script, Client Script, ...).

rehome() files every record still under the old module under the new one. Frappe calls
before_app_uninstall() with the name of the app being removed, after the old app's own
before_uninstall and before it reads the Module Defs, so by the time it looks there is nothing
left to delete or drop. Any other module the old app still owns that has something in it
(Navari's "Stanbic" on older sites) is handed to mpsa_suite instead of being deleted.
"""

import frappe

OLD_APP = "frappe_mpsa_payments"
NEW_APP = "mpsa_suite"
OLD_MODULE = "Frappe Mpsa Payments"
NEW_MODULE = "Mpsa Suite"
APP_FIELD_DOCTYPES = ("Workspace", "Workspace Sidebar", "Desktop Icon")


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
    for doctype in APP_FIELD_DOCTYPES:
        if _usable(doctype, "app"):
            n = frappe.db.count(doctype, {"app": OLD_APP})
            if n:
                frappe.db.sql(f"UPDATE `tab{doctype}` SET `app` = %s WHERE `app` = %s", (NEW_APP, OLD_APP))
                moved[f"{doctype}.app"] = n
    if moved:
        frappe.clear_cache()
    return moved


def before_app_uninstall(app_name: str) -> None:
    """Runs for every app uninstall; acts only when the old package is being removed."""
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
