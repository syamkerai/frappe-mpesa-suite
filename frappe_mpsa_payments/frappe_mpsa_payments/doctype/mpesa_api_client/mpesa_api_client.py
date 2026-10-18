# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from ....daraja.guard import parse_allowlist

ADMIN_ROLES = ("System Manager", "M-Pesa Manager")


class MpesaAPIClient(Document):
    def validate(self):
        if not self.api_key:
            self.api_key = "mpk_" + frappe.generate_hash(length=24)
        if self.webhook_url:
            validate_https(self.webhook_url, _("Webhook URL"))
            if not self.get_password("webhook_secret", raise_exception=False):
                self.webhook_secret = "whsec_" + frappe.generate_hash(length=40)
        if self.allowed_ips and not parse_allowlist(self.allowed_ips):
            frappe.throw(_("Allowed Source IPs has no valid IP or CIDR."))
        api_type = frappe.db.get_value("Mpesa Settings", self.mpesa_settings, "api_type")
        if api_type and api_type != "MPesa Express":
            frappe.throw(_("Prompts must be sent from an MPesa Express record, not {0}.").format(api_type))

    def before_insert(self):
        self._new_secret = "mps_" + frappe.generate_hash(length=40)
        self.api_secret = self._new_secret

    def after_insert(self):
        show_secret_once(self.api_key, self._new_secret, self.get_password("webhook_secret", raise_exception=False))

    @frappe.whitelist()
    def generate_new_secret(self):
        frappe.only_for(ADMIN_ROLES)
        secret = "mps_" + frappe.generate_hash(length=40)
        self.api_secret = secret
        self.save()
        show_secret_once(self.api_key, secret)

    @frappe.whitelist()
    def rotate_webhook_secret(self):
        frappe.only_for(ADMIN_ROLES)
        secret = "whsec_" + frappe.generate_hash(length=40)
        self.webhook_secret = secret
        self.save()
        frappe.msgprint(
            _("New webhook signing secret (copy it now, it will not be shown again):<br><code>{0}</code>").format(secret),
            title=_("Webhook Secret"),
        )

    def allowed_hosts(self) -> set:
        hosts = {h.strip().lower() for h in (self.return_url_hosts or "").splitlines() if h.strip()}
        if self.webhook_url:
            hosts.add((urlparse(self.webhook_url).hostname or "").lower())
        return hosts - {""}


def validate_https(url: str, label: str) -> None:
    parsed = urlparse(url or "")
    local = (parsed.hostname or "") in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and (local or frappe.conf.developer_mode)):
        frappe.throw(_("{0} must start with https://").format(label))


def show_secret_once(api_key: str, secret: str, webhook_secret: str | None = None) -> None:
    lines = [
        _("Copy these now. The API secret is stored encrypted and will not be shown again."),
        f"<br><b>X-Mpesa-Client-Key</b>: <code>{api_key}</code>",
        f"<br><b>X-Mpesa-Client-Secret</b>: <code>{secret}</code>",
    ]
    if webhook_secret:
        lines.append(f"<br><b>Webhook signing secret</b>: <code>{webhook_secret}</code>")
    frappe.msgprint("".join(lines), title=_("API Credentials"), indicator="orange")
