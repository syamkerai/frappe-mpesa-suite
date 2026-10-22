# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import frappe

from mpsa_suite.daraja import apis
from mpsa_suite.daraja.callbacks import with_token
from mpsa_suite.daraja.guard import callback_url_problems, is_zero_code
import requests
from frappe.model.document import Document
from frappe.utils import get_url

from mpsa_suite.mpsa_suite.api.m_pesa_api import get_token


# Fields that change what is registered at Safaricom.
REGISTRATION_FIELDS = ("mpesa_settings", "till_number", "business_shortcode")
ALREADY_REGISTERED_CODE = "500.003.1001"


def _is_already_registered(res: dict) -> bool:
    code = str(res.get("errorCode") or res.get("ResponseCode") or "")
    text = str(res.get("errorMessage") or res.get("ResponseDescription") or "").lower()
    return code == ALREADY_REGISTERED_CODE or "already registered" in text


class MpesaC2BPaymentRegisterURL(Document):
    def needs_registration(self) -> bool:
        """Register on first save, after a failure, or when a key field changed - not on every save."""
        if self.is_new() or self.register_status != "Success" or self.flags.register_again:
            return True
        return any(self.has_value_changed(field) for field in REGISTRATION_FIELDS)

    @frappe.whitelist()
    def register_again(self):
        """Send the URLs again, e.g. after host_name or the callback token changed."""
        frappe.only_for(("System Manager", "M-Pesa Manager"))
        self.flags.register_again = True
        self.save()
        return self.register_status

    def validate(self):
        if not self.needs_registration():
            return

        sandbox_url = "https://sandbox.safaricom.co.ke"
        live_url = "https://api.safaricom.co.ke"
        mpesa_settings = frappe.get_doc("Mpesa Settings", self.mpesa_settings)
        apis.require(mpesa_settings, apis.C2B)
        env = "production" if not mpesa_settings.sandbox else "sandbox"
        business_shortcode = (
            mpesa_settings.business_shortcode
            if env == "production"
            else mpesa_settings.till_number
        )
        if env == "sandbox":
            base_url = sandbox_url
        else:
            base_url = live_url

        token = get_token(
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
            base_url=base_url,
        )

        # Public site URL (host_name in site_config, else the request host). Must be https and
        # reachable from the internet, or Safaricom cannot deliver confirmations.
        site_url = get_url()
        # Token is appended so C2B token enforcement can be switched on after re-registering.
        validation_url = with_token(
            # site_url + "/api/method/payments.payment_gateways.doctype.mpesa_c2b_payment_register_url.mpesa_api.validation"
            site_url
            + "/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.validation"
        )
        confirmation_url = with_token(
            # site_url + "/api/method/payments.payment_gateways.doctype.mpesa_c2b_payment_register_url.mpesa_api.confirmation"
            site_url
            + "/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.confirmation"
        )
        register_url = base_url + "/mpesa/c2b/v2/registerurl"

        # Production registration is one-time (Daraja C2B), so never spend it on a URL
        # Safaricom would refuse or could not reach.
        problems = [
            f"{label}: {problem}"
            for label, url in (("Confirmation URL", confirmation_url), ("Validation URL", validation_url))
            for problem in callback_url_problems(url, sandbox=env == "sandbox")
        ]
        if problems:
            self.register_status = "Failed"
            frappe.msgprint("<br>".join(problems), title="C2B URLs not registered", indicator="red")
            return

        payload = {
            "ShortCode": business_shortcode,
            "ResponseType": "Completed",
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        }
        headers = {
            "Authorization": "Bearer {0}".format(token),
            "Content-Type": "application/json",
        }

        try:
            r = requests.post(register_url, headers=headers, json=payload, timeout=(10, 30))
            try:
                res = r.json()
            except ValueError:
                res = {}
            if is_zero_code(res.get("ResponseCode")):  # the sandbox answers "00000000"
                self.register_status = "Success"
            elif _is_already_registered(res):
                # Safaricom keeps the first registration; the shortcode is set up.
                self.register_status = "Success"
                frappe.msgprint(str(res.get("errorMessage") or res.get("ResponseDescription")))
            else:
                r.raise_for_status()  # Raise an HTTPError for bad responses
                self.register_status = "Failed"
                frappe.msgprint(str(res))
        except requests.exceptions.HTTPError as errh:
            # Handle HTTP errors
            self.register_status = "Failed"
            frappe.msgprint(f"Response Content: {errh.response.content}")
        except requests.exceptions.ConnectionError as errc:
            # Handle Connection errors
            self.register_status = "Failed"
            frappe.msgprint(f"Error Connecting: {errc}")
        except requests.exceptions.Timeout as errt:
            # Handle Timeout errors
            self.register_status = "Failed"
            frappe.msgprint(f"Timeout Error: {errt}")
        except requests.exceptions.RequestException as err:
            # Handle other exceptions
            self.register_status = "Failed"
            frappe.msgprint(f"Request Exception: {err}")
