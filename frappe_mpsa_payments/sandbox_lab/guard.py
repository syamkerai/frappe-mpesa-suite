"""The lab may only ever talk to Daraja's sandbox."""

import frappe
from frappe.utils import cint

from ..permissions import ADMIN_ROLES, require

SETTINGS_FIELDS = ("c2b_settings", "stk_settings", "b2c_settings", "b2b_settings")


def lab_enabled() -> bool:
    return bool(cint(frappe.conf.get("mpesa_sandbox_lab")))


def require_lab(run=None) -> None:
    """Throw unless the lab is switched on for this site, the user is an M-Pesa admin and
    every Mpesa Settings record on ``run`` is a sandbox record."""
    if not lab_enabled():
        frappe.throw(
            "The M-Pesa Sandbox Test Lab is off on this site. Switch it on only on a test site: "
            "bench --site <site> set-config mpesa_sandbox_lab 1 (Frappe Cloud: Site Config)."
        )
    require(ADMIN_ROLES)
    if run is not None:
        for field in SETTINGS_FIELDS:
            require_sandbox_settings(run.get(field))


def require_sandbox_settings(settings_name) -> None:
    if not settings_name:
        return
    if not cint(frappe.db.get_value("Mpesa Settings", settings_name, "sandbox")):
        frappe.throw(f"Mpesa Settings {settings_name} is not a Sandbox record. The lab never uses live keys.")
