# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

from ....daraja.guard import parse_allowlist
from ....permissions import CONFIG_ROLES, require

# Switches that guard who sends payouts and where: a change to any is written on the settings and
# sent to the Notify Role, naming who changed what (turning off maker-checker, allowing one-off
# B2B, shortening the hold on new payee accounts, another Integration User, Administrator sends).
WATCHED = (
    "schedule_maker_checker",
    "refuse_administrator_payouts",
    "allow_one_off_b2b",
    "supplier_account_hold_hours",
    "employee_account_hold_hours",
    "integration_user",
)


class MPesaSuiteSettings(Document):
    def validate(self):
        if not self.get_password("callback_token", raise_exception=False):
            self.callback_token = frappe.generate_hash(length=40)

        if self.enforce_ip_allowlist and not parse_allowlist(self.allowed_callback_ips):
            frappe.throw(_("Add at least one valid IP or CIDR before enforcing the callback allow-list."))

        from ....money_out.statement import parse_bands

        try:
            parse_bands(self.get("b2c_charge_bands"))
        except ValueError as e:
            frappe.throw(_("B2C Charge Bands: {0}").format(e))

    def on_update(self):
        self.alert_safety_changes()

    def alert_safety_changes(self):
        """A comment on the settings and an alert to the Notify Role for each WATCHED change."""
        before = self.get_doc_before_save()
        if not before:
            return  # first save (install)
        changes = []
        for field in WATCHED:
            df = self.meta.get_field(field)
            if not df or _shown(df, before.get(field)) == _shown(df, self.get(field)):
                continue
            changes.append(_("{0} from {1} to {2}").format(_(df.label), _shown(df, before.get(field)),
                                                         _shown(df, self.get(field))))
        if not changes:
            return
        from ....money_out.party_accounts import alert

        message = _("{0} changed M-Pesa Suite Settings: {1}.").format(frappe.session.user, "; ".join(changes))
        self.add_comment("Comment", message)
        alert(message, self)

    @frappe.whitelist()
    def regenerate_callback_token(self):
        require(CONFIG_ROLES)
        self.callback_token = frappe.generate_hash(length=40)
        self.save()
        return _(
            "New callback token saved. STK/B2C callbacks use it immediately; "
            "re-register every C2B URL before enforcing the C2B token."
        )

    @frappe.whitelist()
    def reset_rejection_counter(self):
        require(CONFIG_ROLES)
        self.rejected_callbacks = 0
        self.last_rejected_callback_on = None
        self.last_rejected_callback_reason = None
        self.save()


def _shown(df, value) -> str:
    """A setting's value as the alert says it."""
    if df.fieldtype == "Check":
        return _("on") if cint(value) else _("off")
    if df.fieldtype == "Int":
        return str(cint(value))
    return cstr(value).strip() or _("not set")
