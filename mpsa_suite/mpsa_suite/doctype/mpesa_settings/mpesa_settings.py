# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt


import re
import time
from json import dumps, loads
from typing import Any
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document

from ....compat import in_test
from ....daraja import apis
from ....daraja.guard import is_zero_code
from ....permissions import ADMIN_ROLES, CONFIG_ROLES, FINANCE_ROLES, require, require_for_direct_call
from ....daraja.callbacks import KIND_PULL, KIND_RESULT, safaricom_callback, with_token
from frappe.utils import (
    fmt_money,
    get_request_site_address,
    get_url,
    now_datetime,
)

from mpsa_suite.utils.encoding_initiator_password import (
    generate_security_credential,
)

from ....utils.doctype_names import (
    MPESA_EXPRESS_REQUEST_DOCTYPE,
    PUBLIC_CERTIFICATES_DOCTYPE,
)
from ....utils.utils import (
    convert_amount_to_kes,
    create_payment_gateway_account,
    erpnext_app_import_guard,
    validate_phone_number,
)
from ...api.m_pesa_api import get_account_balance, sanitize_mobile_number
from .mpesa_connector import MpesaConnector


class MpesaSettings(Document):
    supported_currencies = ["KES"]

    def validate_transaction_currency(self, currency: str) -> None:
        """
        Validates that the transaction currency is supported for Mpesa.

        Allows the transaction if:
        - The currency is KES, OR
        - The company's default currency is KES
        """
        if currency in self.supported_currencies:
            return

        # If company is provided, check if its default currency is KES
        if self.company:
            default_currency = frappe.db.get_value(
                "Company", self.company, "default_currency"
            )
            if default_currency in self.supported_currencies:
                return

        # If neither the currency nor company default is supported
        frappe.throw(
            _(
                "Please select another payment method. Mpesa does not support transactions in currency '{0}'."
            ).format(currency)
        )

    def get_payment_url(self, **kwargs):
        phone = kwargs.get("phone") or kwargs.get("phone_number") or ""
        base_amount = kwargs.get("amount", 0.0)
        currency = kwargs.get("currency", "KES")
        setting_name = self.name
        actual_amount = base_amount

        if currency != "KES" and base_amount:
            try:
                actual_amount = convert_amount_to_kes(
                    amount=float(base_amount), currency=currency, settings=setting_name
                )
            except Exception:
                actual_amount = base_amount

        express_request = frappe.get_doc(
            {
                "doctype": MPESA_EXPRESS_REQUEST_DOCTYPE,
                "payment_gateway": kwargs.get("payment_gateway"),
                "reference_doctype": kwargs.get("reference_doctype"),
                "reference_name": kwargs.get("reference_docname"),
                "transaction_title": kwargs.get("title"),
                "transaction_description": kwargs.get("description"),
                "base_amount": base_amount,
                "currency": currency,
                "amount": actual_amount,
                "status": "In Progress",
                "redirect_to": kwargs.get("redirect_to"),
                "title": kwargs.get("title"),
                "description": kwargs.get("description"),
            }
        )

        if phone:
            express_request.phone_number = sanitize_mobile_number(phone)

        express_request.insert(ignore_permissions=True)

        return get_url(f"{express_request.route}")

    def on_update(self) -> None:
        """On Update Hook"""
        from ....daraja.client import invalidate_token
        from ....utils.utils import create_payment_gateway

        # Credentials or environment may have changed: drop the cached OAuth token.
        invalidate_token(self)

        create_payment_gateway(
            "Mpesa-" + self.payment_gateway_name,
            settings="Mpesa Settings",
            controller=self.payment_gateway_name,
        )
        if "erpnext" in frappe.get_installed_apps():
            create_payment_gateway_account(
                gateway="Mpesa-" + self.payment_gateway_name,
                payment_channel="Phone",
                company=self.company,
            )

            create_mode_of_payment(
                "Mpesa-" + self.payment_gateway_name,
                payment_type="Phone",
                company=self.company,
            )

    def validate(self) -> None:
        self.refresh_security_credential()

        if self.exchange_rates:
            for rate in self.exchange_rates:
                if rate.currency == "KES":
                    frappe.throw(_("Conversion rate for KES cannot be set."))

        self.validate_api_switches()
        self.warn_shared_shortcode()
        if self.meta.has_field("sub_accounts"):
            from ....wallet.accounts import validate_settings

            validate_settings(self)

    def validate_api_switches(self) -> None:
        """At least one API; money-out APIs need an API operator (Initiator)."""
        if self.is_new() and self.api_type:
            # Created by code that still sets the old single-purpose field.
            legacy = apis.switches_from_legacy(self.api_type)
            for api in (apis.C2B, apis.STK, apis.B2C):
                self.set(api, legacy[api])
        if not apis.enabled_apis(self):
            frappe.throw(_("Tick at least one API under APIs this record uses."))
        missing = [apis.LABELS[api] for api in apis.PAYING if apis.enabled(self, api)]
        if missing and not (self.initiator_name and (self.initiator_password or self.security_credential)):
            frappe.msgprint(
                _("{0} need an Initiator Name and Initiator Password (an API operator with a role for each).").format(
                    ", ".join(missing)
                ),
                title=_("Initiator missing"),
                indicator="orange",
            )

    def warn_shared_shortcode(self) -> None:
        """One shortcode can do Paybill, STK, B2C and B2B (Safaricom Business One Account); keep it on
        one record. A second record for the same number is allowed but money received is booked with
        only one of them (see money_in.common.settings_for_shortcode), so say which."""
        from mpsa_suite.money_in.common import records_sharing_shortcode, settings_for_shortcode

        others = records_sharing_shortcode(self.name, [self.business_shortcode, self.till_number])
        if not others:
            return
        receiving = settings_for_shortcode(self.business_shortcode or self.till_number)
        frappe.msgprint(
            _(
                "Shortcode {0} is also used by {1}. One shortcode can collect (Paybill / STK) and pay out "
                "(B2C / B2B) on one Mpesa Settings record. If you keep several, money received on it is "
                "booked with {2}."
            ).format(self.business_shortcode or self.till_number, ", ".join(others), receiving or self.name),
            title=_("Shortcode used by another record"),
            indicator="orange",
        )

    def refresh_security_credential(self, force: bool = False) -> None:
        """SecurityCredential = RSA-PKCS1v15(initiator password) with the cert for this environment.

        Regenerated whenever the initiator password is (re)typed, the environment flips between
        sandbox and production, or it is missing. Upstream only generated it once (and for B2C
        encrypted the STK passkey with OAEP), so a changed password or environment kept sending a
        stale credential and Safaricom answered 2001 "invalid initiator information".
        """
        typed = self.initiator_password and set(str(self.initiator_password)) != {"*"}
        changed_env = not self.is_new() and self.has_value_changed("sandbox")
        if not (force or typed or changed_env or not self.security_credential):
            return

        password = (
            self.initiator_password
            if typed
            else self.get_password("initiator_password", raise_exception=False)
        )
        if not password:
            return

        certs = frappe.get_single("Mpesa Public Key Certificate")
        cert_url = certs.sandbox_certificate if self.sandbox else certs.production_certificate
        self.security_credential = generate_security_credential(password, cert_url, self.sandbox)

    @frappe.whitelist()
    def regenerate_security_credential(self):
        require(CONFIG_ROLES)
        self.refresh_security_credential(force=True)
        self.save()
        return _("Security credential regenerated.")

    def request_for_payment(self, **kwargs) -> None:
        from frappe.utils import flt

        from ....daraja.guard import STK_MAX_AMOUNT, stk_amount

        args = frappe._dict(kwargs)
        if args.get("reference_doctype") == "Payment Request":
            # One prompt per Payment Request, never split: Safaricom refuses a second prompt to
            # the same phone while the first is pending, so every part after the first went
            # uncollected and the request stayed Partially Paid with nobody told.
            cap = min(flt(self.transaction_limit) or STK_MAX_AMOUNT, STK_MAX_AMOUNT)
            currency = args.get("currency") or "KES"
            amount = flt(args.request_amount)
            kes = amount if currency == "KES" else flt(convert_amount_to_kes(currency, amount) or amount)
            if kes > cap:
                frappe.throw(
                    _(
                        "Payment Request {0} is for KES {1}, above the M-Pesa Transaction Limit of KES {2} on {3}. "
                        "A Payment Request is prompted once: raise the Transaction Limit (Safaricom allows at most "
                        "KES 250,000 per prompt), or ask for the amount in several Payment Requests, one after the "
                        "other, each within the limit."
                    ).format(args.get("reference_docname"), f"{kes:,.2f}", f"{cap:,.2f}", self.name),
                    title=_("M-Pesa Transaction Limit"),
                )
            request_amounts = [amount]
        else:
            request_amounts = self.split_request_amount_according_to_transaction_limit(args)
        phone_number = args.get("phone_number")

        if not validate_phone_number(phone_number):
            sender = args.get("sender", "")
            if (
                isinstance(sender, str)
                and sender
                and (sender.startswith(("0", "254", "+", "7", "1")))
            ):
                phone_number = sender
        if not phone_number:
            frappe.throw(_("A valid phone number is required for Mpesa payment."))
        else:
            phone_number = sanitize_mobile_number(phone_number)

        def refuse_unsendable(amounts):
            # Every part (in KES, as it will be asked) is checked before the first prompt goes
            # out: one refused at its own submit, after an earlier part's push, rolled back the
            # records of a prompt that was already on the customer's phone.
            for value in amounts:
                try:
                    stk_amount(value)
                except ValueError as e:
                    frappe.throw(_("Cannot send an STK Push for {0}: {1}. No prompt was sent.").format(value, e))

        if in_test():
            refuse_unsendable(request_amounts)
            for amount in request_amounts:
                args.request_amount = amount
                from .test_mpesa_settings import get_payment_request_response_payload

                # Log the request as the live STK flow does, so the callback can find it.
                self.handle_api_response(
                    "CheckoutRequestID", args, frappe._dict(get_payment_request_response_payload(amount))
                )
            return

        parts = []
        for amount in request_amounts:
            stk_request = frappe.new_doc(MPESA_EXPRESS_REQUEST_DOCTYPE)
            stk_request.update(
                {
                    "base_amount": amount,
                    "currency": args.get("currency", "KES"),
                    "phone_number": phone_number,
                    "timestamp": frappe.utils.now(),
                    "settings": args.payment_gateway[6:],
                    "payment_gateway": args.get("payment_gateway"),
                    "reference_doctype": args.get("reference_doctype"),
                    "reference_name": args.get("reference_docname"),
                }
            )
            stk_request.flags.ignore_permissions = True
            stk_request.insert(ignore_permissions=True)  # a draft: nothing is sent yet
            parts.append(stk_request)
        refuse_unsendable([part.amount for part in parts])
        for stk_request in parts:
            stk_request.submit()  # sends its prompt

    def split_request_amount_according_to_transaction_limit(
        self, args: frappe._dict
    ) -> list:
        """Parts of at most the transaction limit (blank: Safaricom's 250,000), none under 1."""
        from frappe.utils import flt

        from ....daraja.guard import STK_MAX_AMOUNT, STK_MIN_AMOUNT

        request_amount = flt(args.request_amount)
        limit = min(flt(self.transaction_limit) or STK_MAX_AMOUNT, STK_MAX_AMOUNT)
        if request_amount <= limit:
            return [request_amount]
        requests_to_be_made = frappe.utils.ceil(request_amount / limit)  # 480/150 = ceil(3.2) = 4
        request_amounts = [limit] * (requests_to_be_made - 1)
        # for the 4th request, 480 - (150 * 3) = 30
        request_amounts.append(flt(request_amount - limit * (requests_to_be_made - 1), 2))
        if request_amounts[-1] < STK_MIN_AMOUNT:
            # e.g. 150,000.30 by 150,000: a 0.30 part cannot be prompted (STK asks whole shillings,
            # at least 1), so it goes with the part before it, asked as 150,000
            remainder = request_amounts.pop()
            request_amounts[-1] = flt(request_amounts[-1] + remainder, 2)
        return request_amounts

    @frappe.whitelist()
    def get_account_balance_info(self) -> None:
        # A form method runs for anyone who can read the record (M-Pesa Viewer too); asking
        # Safaricom is a finance action, as get_account_balance is when called directly.
        require(FINANCE_ROLES)
        if not (self.initiator_name and self.security_credential):
            frappe.throw(_("Set the Initiator Name and Initiator Password first."))
        if in_test():
            from .test_mpesa_settings import get_test_account_balance_response

            # Log the request as the live call does, so the balance callback can find it.
            request = {"reference_doctype": self.doctype, "reference_docname": self.name}
            self.handle_api_response("ConversationID", request, frappe._dict(get_test_account_balance_response()))
        else:
            get_account_balance(self.name)

    def handle_api_response(
        self, global_id: str, request_dict: frappe._dict, response: frappe._dict
    ) -> None:
        """Response received from API calls returns a global identifier for each transaction, this code is returned during the callback."""
        # check error response
        if "requestId" in response:
            req_name = response["requestId"]
            error = response
        else:
            # global checkout id used as request name
            req_name = response[global_id]
            error = None

        if not frappe.db.exists("Integration Request", req_name):
            create_request_log(request_dict, "Host", "Mpesa", req_name, error)

        if error:
            frappe.throw(_(response["errorMessage"]), title=_("Transaction Error"))


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def process_balance_info(**kwargs) -> None:
    """Process and store account balance information received via callback from the account balance API call."""
    account_balance_response = frappe._dict(kwargs["Result"])

    conversation_id = getattr(account_balance_response, "ConversationID", "")
    if not isinstance(conversation_id, str):
        frappe.throw(_("Invalid Conversation ID"))

    request = frappe.get_doc("Integration Request", conversation_id)

    if request.status == "Completed":
        return

    transaction_data = frappe._dict(loads(request.data))

    if str(account_balance_response.get("ResultCode")) == "0":
        try:
            result_params = account_balance_response["ResultParameters"][
                "ResultParameter"
            ]
            if isinstance(result_params, dict):  # Daraja sends a bare dict for one param
                result_params = [result_params]

            balance_info = fetch_param_value(result_params, "AccountBalance", "Key")
            balance_info = format_string_to_json(balance_info)

            ref_doc = frappe.get_doc(
                transaction_data.reference_doctype, transaction_data.reference_docname
            )
            ref_doc.db_set("account_balance", balance_info)

            request.handle_success(account_balance_response)
            frappe.publish_realtime(
                "refresh_mpesa_dashboard",
                doctype="Mpesa Settings",
                docname=transaction_data.reference_docname,
                user=transaction_data.owner,
            )
        except Exception:
            request.handle_failure(account_balance_response)
            frappe.log_error(
                title="Mpesa Account Balance Processing Error",
                message=account_balance_response,
            )
    else:
        request.handle_failure(account_balance_response)


def format_string_to_json(balance_info: str) -> str:
    """
    Format string to json.

    e.g: '''Working Account|KES|481000.00|481000.00|0.00|0.00'''
    => {'Working Account': {'current_balance': '481000.00',
            'available_balance': '481000.00',
            'reserved_balance': '0.00',
            'uncleared_balance': '0.00'}}
    """
    balance_dict = frappe._dict()
    for account_info in balance_info.split("&"):
        account_info = account_info.split("|")
        balance_dict[account_info[0]] = dict(
            current_balance=fmt_money(account_info[2], currency="KES"),
            available_balance=fmt_money(account_info[3], currency="KES"),
            reserved_balance=fmt_money(account_info[4], currency="KES"),
            uncleared_balance=fmt_money(account_info[5], currency="KES"),
        )
    return dumps(balance_dict)


def fetch_param_value(response: dict, key: str, key_field: str) -> str | None:
    """Fetch the specified key from list of dictionary. Key is identified via the key field."""
    for param in response:
        if param[key_field] == key:
            return param["Value"]


def create_mode_of_payment(
    gateway: str, payment_type: str = "General", company: str = None
) -> Document:
    with erpnext_app_import_guard():
        from erpnext import get_default_company

    filters = {"payment_gateway": gateway}
    if company and frappe.get_meta("Payment Gateway Account").has_field("company"):
        filters["company"] = company  # ERPNext v16: one gateway account per company
    payment_gateway_account = frappe.db.get_value("Payment Gateway Account", filters, ["payment_account"])

    mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
    if not mode_of_payment and payment_gateway_account:
        mode_of_payment = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": gateway,
                "enabled": 1,
                "type": payment_type,
                "accounts": [
                    {
                        "doctype": "Mode of Payment Account",
                        "company": company or get_default_company(),
                        "default_account": payment_gateway_account,
                    }
                ],
            }
        )
        mode_of_payment.insert(ignore_permissions=True)

        return mode_of_payment

    if not mode_of_payment:
        return None  # no Payment Gateway Account yet (no Company set): created on a later save

    mode_of_payment = frappe.get_doc("Mode of Payment", mode_of_payment)
    company = company or get_default_company()
    if company and payment_gateway_account and not any(row.company == company for row in mode_of_payment.accounts):
        # Company set or changed after the Mode of Payment was made: give it this company's account.
        account_company = frappe.db.get_value("Account", payment_gateway_account, "company")
        if account_company == company:
            mode_of_payment.append("accounts", {"company": company, "default_account": payment_gateway_account})
            mode_of_payment.save(ignore_permissions=True)
    return mode_of_payment


@frappe.whitelist()
def trigger_transaction_status(mpesa_settings, transaction_id, remarks="OK"):
    require_for_direct_call("trigger_transaction_status", FINANCE_ROLES)
    # checked here, not in the browser: only M-Pesa admins can read the credential fields
    initiator = frappe.db.get_value("Mpesa Settings", mpesa_settings, ["initiator_name", "security_credential"])
    if not initiator or not all(initiator):
        return {
            "status": "error",
            "message": _("Set the Initiator Name and Initiator Password on {0} first.").format(mpesa_settings),
        }
    try:
        site_address = get_request_site_address(True)
        parsed_url = urlparse(site_address)
        site_url = f"{parsed_url.scheme}://{parsed_url.hostname}"

        queue_timeout_url = with_token(
            site_url
            + "/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.handle_queue_timeout"
        )
        result_url = with_token(
            site_url
            + "/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.handle_transaction_status_result"
        )

        integration_request = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "is_remote_request": 1,
                "integration_request_service": "Mpesa Transaction Status",
                "reference_doctype": "Mpesa C2B Payment Register",
                "status": "Queued",
                "data": dumps(
                    {
                        "mpesa_settings": mpesa_settings,
                        "transaction_id": transaction_id,
                        "remarks": remarks,
                        "queue_timeout_url": queue_timeout_url,
                        "result_url": result_url,
                    }
                ),
                "method": "POST",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.enqueue(
            "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.process_transaction_status",
            queue="short",
            timeout=300,
            job_id=f"mpesa_status_{integration_request.name}",
            integration_request_name=integration_request.name,
            deduplicate=True,
        )

        frappe.publish_realtime(
            event="mpesa_transaction_status",
            message={
                "status": "queued",
                "message": _("Transaction status check queued for processing"),
            },
            user=frappe.session.user,
        )
        return {"status": "queued", "message": "Transaction status check queued"}

    except Exception as e:
        frappe.log_error(title="Mpesa Transaction Status Queue Error", message=str(e))
        return {"status": "error", "message": str(e)}


def process_transaction_status(integration_request_name):
    """Process the Mpesa transaction status check in the background"""
    try:
        integration_request = frappe.get_doc(
            "Integration Request", integration_request_name
        )
        data = loads(integration_request.data)

        mpesa_settings = data["mpesa_settings"]
        transaction_id = data["transaction_id"]
        remarks = data["remarks"]
        queue_timeout_url = data["queue_timeout_url"]
        result_url = data["result_url"]

        settings = frappe.get_doc("Mpesa Settings", mpesa_settings)

        # Initialize Mpesa Connector
        connector = MpesaConnector(
            env="production" if not settings.sandbox else "sandbox",
            app_key=settings.consumer_key,
            app_secret=settings.get_password("consumer_secret"),
        )

        response = connector.transaction_status(
            initiator=settings.initiator_name,
            security_credential=settings.security_credential,
            transaction_id=transaction_id,
            party_a=apis.initiator_party_a(settings),
            identifier_type=4,  # Organization Short Code
            remarks=remarks,
            occasion="Ok",
            queue_timeout_url=queue_timeout_url,
            result_url=result_url,
        )

        if is_zero_code(response.get("ResponseCode")):
            integration_request.status = "Completed"
            integration_request.output = dumps(response)
            integration_request.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.publish_realtime(
                event="mpesa_transaction_status",
                message={
                    "status": "success",
                    "message": f"Transaction Status: {response.get('ResponseDescription')}",
                },
                user=frappe.session.user,
            )
        else:
            error_msg = f"{response.get('errorCode', 'Unknown')}: {response.get('errorMessage', 'Unknown error')}"
            integration_request.status = "Failed"
            integration_request.output = error_msg
            integration_request.save(ignore_permissions=True)
            frappe.db.commit()

            frappe.publish_realtime(
                event="mpesa_transaction_status",
                message={"status": "error", "message": error_msg},
                user=frappe.session.user,
            )

    except Exception as e:
        integration_request.status = "Failed"
        integration_request.output = str(e)
        integration_request.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.log_error(title="Mpesa Transaction Status Process Error", message=str(e))
        frappe.publish_realtime(
            event="mpesa_transaction_status",
            message={"status": "error", "message": f"Error checking status: {str(e)}"},
            user=frappe.session.user,
        )


@frappe.whitelist()
def get_doctype_fields(doctype):
    # the reconciliation-order picker on the Mpesa Settings form
    frappe.has_permission("Mpesa Settings", "write", throw=True)
    if not doctype:
        return []

    try:
        meta = frappe.get_meta(doctype)
        fields = [
            f.fieldname
            for f in meta.fields
            if f.fieldtype
            not in (
                "Table",
                "Section Break",
                "Column Break",
                "Tab Break",
                "HTML",
                "Button",
                "Table MultiSelect",
                "Attach",
                "Attach Image",
            )
        ]
        return fields
    except Exception as e:
        frappe.log_error(title="Mpesa Settings: field list failed", message=f"Error fetching fields for {doctype}: {e}\n\n{frappe.get_traceback()}")
        return []


# Safaricom throttles bursts; pace the batch rather than fire 69 requests flat out.
BULK_REGISTRATION_DELAY_SECONDS = 1

# Daraja bans "mpesa" in callback URLs, so new registrations use the m_pesa_api endpoint;
# pull_transaction_callback below stays for shortcodes registered with the old URL.
PULL_REGISTRATION_CALLBACK = (
    "mpsa_suite.mpsa_suite.api.m_pesa_api.pull_registration_result"
)


def _record_registration_outcome(settings_name: str, status: str, message: str) -> None:
    """Persist pull registration state so enrollment is visible in the list view."""
    try:
        frappe.db.set_value(
            "Mpesa Settings",
            settings_name,
            {
                "pull_registration_status": status,
                "pull_registration_message": (message or "")[:500],
                "pull_registered_on": now_datetime(),
            },
            update_modified=False,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Mpesa Pull Registration: could not record outcome for {settings_name}",
        )


@frappe.whitelist()
def register_pull_transaction(
    mpesa_settings: str, callback_url: str | None = None
) -> dict:
    """Register a shortcode with Safaricom for Pull Transactions.

    `callback_url` exists so bulk registration can capture the URL in the web
    request context and pass it down. Built from the request Host header,
    `build_callback_url` silently falls back to conf.host_name (or
    http://<site>) when there is no request - and since Safaricom rejects
    re-registration with "Shortcode already Registered!", a wrong URL baked in
    from a background job cannot be corrected afterwards.
    """
    require_for_direct_call("register_pull_transaction", ADMIN_ROLES)
    import requests as _requests

    from mpsa_suite.mpsa_suite.api.m_pesa_api import get_token
    from mpsa_suite.utils.utils import build_callback_url

    settings = frappe.get_doc("Mpesa Settings", mpesa_settings)

    if not settings.pull_transaction_nominated_number:
        frappe.throw(_("Pull Transaction Nominated Number is required."))

    if not re.match(r"^254\d{9}$", settings.pull_transaction_nominated_number):
        frappe.throw(
            _(
                "Pull Transaction Nominated Number must be in the format 254XXXXXXXXX (254 followed by 9 digits)."
            )
        )

    base_url = (
        "https://sandbox.safaricom.co.ke"
        if settings.sandbox
        else "https://api.safaricom.co.ke"
    )

    try:
        token = get_token(
            app_key=settings.consumer_key,
            app_secret=settings.get_password("consumer_secret"),
            base_url=base_url,
        )
        shortcode = (
            settings.till_number if settings.sandbox else settings.business_shortcode
        )
        callback_url = callback_url or build_callback_url(PULL_REGISTRATION_CALLBACK, add_token=False)

        payload = {
            "ShortCode": shortcode,
            "RequestType": "Pull",
            "NominatedNumber": settings.pull_transaction_nominated_number,
            "CallBackURL": callback_url,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        resp = _requests.post(
            f"{base_url}/pulltransactions/v1/register",
            headers=headers,
            json=payload,
            timeout=(10, 30),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        frappe.log_error(
            title="Mpesa Pull Transaction Registration Error", message=str(e)
        )
        _record_registration_outcome(mpesa_settings, "Failed", str(e))
        return {"status": "error", "message": str(e)}

    # The spec shows "ResponseStatus"/"ResponseDescription"; live responses have used
    # the spaced keys. Accept both.
    response_status = str(
        data.get("ResponseStatus") or data.get("Response Status") or data.get("ResponseCode") or ""
    )
    response_description = data.get("ResponseDescription") or data.get("Response Description")

    if response_status == "1000":
        description = response_description or "Registered successfully"
        _record_registration_outcome(mpesa_settings, "Registered", description)
        return {"status": "success", "message": description}

    description = response_description or "Registration failed"

    # Safaricom rejects a second registration for a shortcode already enrolled.
    # That is not a failure to act on - it means the shortcode is already set up.
    if "already registered" in str(description).lower():
        _record_registration_outcome(mpesa_settings, "Already Registered", description)
        return {"status": "already_registered", "message": description}

    frappe.log_error(
        title="Mpesa Pull Transaction Registration Error", message=str(data)
    )
    _record_registration_outcome(mpesa_settings, "Failed", description)
    return {"status": "error", "message": description}


@frappe.whitelist()
def bulk_register_pull_transactions(
    settings_names: str | list | None = None, dry_run: int | bool = 0
) -> dict:
    """Queue pull registration for many shortcodes at once.

    Registration is effectively one-way (Safaricom rejects re-registration), so
    the CallBackURL is resolved here, inside the web request, and passed to the
    worker rather than rebuilt there from an unreliable fallback.
    """
    from mpsa_suite.utils.utils import build_callback_url

    require(("System Manager",))

    if isinstance(settings_names, str):
        settings_names = frappe.parse_json(settings_names)

    if not settings_names:
        settings_names = [
            row.name
            for row in frappe.get_all(
                "Mpesa Settings",
                filters={"pull_transaction_nominated_number": ["!=", ""]},
                fields=["name"],
            )
        ]

    if not settings_names:
        return {"status": "error", "message": _("No Mpesa Settings to register.")}

    try:
        callback_url = build_callback_url(PULL_REGISTRATION_CALLBACK, add_token=False)
    except RuntimeError:
        # build_callback_url reads the request Host header and raises
        # "object is not bound" with no request. Better to stop here than to
        # register 60+ shortcodes against a guessed hostname we cannot correct.
        frappe.throw(
            _(
                "Bulk pull registration must be run from the web interface so the "
                "callback URL can be read from the request. Registration cannot be "
                "undone, so it will not run with a guessed hostname."
            )
        )

    if frappe.utils.cint(dry_run):
        return {
            "status": "dry_run",
            "message": _("Would register {0} shortcode(s) with callback {1}").format(
                len(settings_names), callback_url
            ),
            "count": len(settings_names),
            "callback_url": callback_url,
            "settings": settings_names,
        }

    frappe.enqueue(
        "mpsa_suite.mpsa_suite.doctype.mpesa_settings"
        ".mpesa_settings.execute_bulk_pull_registration",
        queue="long",
        timeout=3600,
        settings_names=settings_names,
        callback_url=callback_url,
    )

    return {
        "status": "success",
        "message": _("Queued registration for {0} shortcode(s).").format(
            len(settings_names)
        ),
        "count": len(settings_names),
        "callback_url": callback_url,
    }


def execute_bulk_pull_registration(settings_names: list, callback_url: str) -> None:
    """Register each shortcode in turn, isolating failures so one cannot abort the batch."""
    registered, already, failed = [], [], []

    for name in settings_names:
        try:
            result = register_pull_transaction(name, callback_url=callback_url)
            status = result.get("status")
            if status == "success":
                registered.append(name)
            elif status == "already_registered":
                already.append(name)
            else:
                failed.append(f"{name}: {result.get('message')}")
        except Exception as e:
            # Includes the frappe.throw for a missing nominated number.
            failed.append(f"{name}: {e}")
            _record_registration_outcome(name, "Failed", str(e))

        frappe.db.commit()
        time.sleep(BULK_REGISTRATION_DELAY_SECONDS)

    summary = (
        f"Registered: {len(registered)}\n"
        f"Already registered: {len(already)}\n"
        f"Failed: {len(failed)}\n\n"
        f"Callback URL used: {callback_url}\n\n"
        f"Newly registered: {registered}\n\n"
        f"Failures:\n" + "\n".join(failed)
    )
    frappe.log_error(title="Mpesa Bulk Pull Registration Complete", message=summary)

    frappe.publish_realtime(
        event="mpesa_bulk_pull_registration_complete",
        message={
            "status": "success" if not failed else "warning",
            "title": "Bulk Pull Registration Complete",
            "message": (
                f"{len(registered)} registered, {len(already)} already registered, "
                f"{len(failed)} failed."
            ),
        },
        user=frappe.session.user,
    )


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_PULL)
def pull_transaction_callback(**kwargs) -> dict:
    frappe.log_error(
        title="Mpesa Pull Transaction Callback",
        message=frappe.as_json(kwargs),
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
