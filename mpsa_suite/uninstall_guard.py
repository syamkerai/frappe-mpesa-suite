"""Refuse every uninstall of mpsa_suite (docs/RENAME_SPIKE.md, R2).

After the rename hand-over mpsa_suite owns the "Mpsa Suite" module, so uninstalling it deletes
far more than the M-Pesa rows: Frappe DROPs the table of every DocType in the module (the shared
Mpesa Settings and Payment Gateway included), deletes the Singles M-Pesa Suite Settings and
Mpesa Public Key Certificate with their secrets, the Custom Fields and Property Setters filed
under the module, and every site-made Server Script, Client Script, Report and custom DocType
the hand-over moved in. A row count would miss most of that, so the guard refuses whatever the
counts, unless site_config has ``mpsa_allow_uninstall: 1``. This is not a rollback route.

Frappe runs an app's own ``before_uninstall`` from ``get_hooks(app_name=...)``, so it runs even
when mpsa_suite is missing from installed_apps (a half-failed install, a restore that dropped it)
and Frappe Cloud's ``--force`` skips the "not installed" check. It runs for a ``--dry-run`` too,
and Frappe gives it no way to tell the two apart. The old app's own guard (R3) uses a different
flag, so unlocking one never unlocks the other.
"""

import frappe

APP = "mpsa_suite"
FLAG = "mpsa_allow_uninstall"


def allowed() -> bool:
    """Only an explicit 1 / true in site_config unlocks the uninstall."""
    return str(frappe.conf.get(FLAG)).strip().lower() in ("1", "true")


def refuse_unless_allowed() -> None:
    if allowed():
        return
    frappe.throw(
        f"Uninstalling {APP} would drop every M-Pesa table (Mpesa Settings and Payment Gateway "
        "included), delete M-Pesa Suite Settings and its secrets, the M-Pesa custom fields and "
        "every report and script filed under the Mpsa Suite module. It is not a rollback. Set "
        f"{FLAG} = 1 in site_config only if you really mean to delete all M-Pesa data.",
        title="Uninstall refused",
    )


def before_uninstall():
    """mpsa_suite's own before_uninstall: refuses every uninstall unless the flag is set."""
    refuse_unless_allowed()
