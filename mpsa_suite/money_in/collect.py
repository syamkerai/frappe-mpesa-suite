"""Ask a customer to pay a submitted Sales Order or Sales Invoice by M-Pesa, from its form.

There is one way in, the one every M-Pesa prompt for a document already takes: an ERPNext Payment
Request (Phone channel, an M-Pesa Payment Gateway Account of the document's company) whose submit
prompts the phone through Mpesa Settings.request_for_payment and an Mpesa Express Request. Nothing
here talks to Safaricom.

The server decides, whatever the browser shows:
- COLLECT_ROLES, read on the order or invoice and create on Payment Request (ERPNext's own rule
  in make_payment_request);
- only a submitted Sales Order or non-POS Sales Invoice in KES that still owes;
- the amount is at most what is owed less what open Payment Requests still ask for (the unpaid
  M-Pesa ones a new prompt supersedes aside, when M-Pesa Suite Settings says so), and at most the
  shortcode's Transaction Limit (Safaricom's 250,000 at most): a Payment Request is prompted once;
- at most PROMPTS_PER_MINUTE prompts a minute per user, and none while the document's last
  prompt may still be on the customer's phone (as the Express Request's own resend rule).
"""

import datetime

import frappe
from frappe import _
from frappe.utils import cstr, flt, now_datetime

from ..compat import get_cache
from ..daraja import apis
from ..daraja.guard import STK_MAX_AMOUNT, STK_MIN_AMOUNT
from ..permissions import COLLECT_ROLES, require
from ..utils.phone import normalize_msisdn, to_local
from .common import EXPRESS, enabled
from .payment_request import is_mpesa, supersede_unpaid

DOCTYPES = ("Sales Order", "Sales Invoice")
PAYMENT_REQUEST = "Payment Request"
GATEWAY_ACCOUNT = "Payment Gateway Account"
SETTINGS = "Mpesa Settings"
PROMPTS_PER_MINUTE = 5
RESEND_AFTER_SECONDS = 90  # as m_pesa_api.STK_RESEND_AFTER_SECONDS
# Payment Requests a new M-Pesa prompt supersedes (money_in.payment_request.supersede_unpaid)
UNPAID = ("Requested", "Initiated")


@frappe.whitelist()
def collect_options(doctype: str, name: str) -> dict:
    """What the Request Payment dialog shows: what is left to ask for, the customer's phone and
    the shortcodes that can prompt for the document's company."""
    require(COLLECT_ROLES)
    _check_doctype(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    doc = frappe.get_doc(doctype, name)
    problem = document_problem(doc)
    held, holding = requested(doc)
    return {
        "problem": problem,
        "owed": flt(max(owed(doc) - held, 0), 2),
        "held_by": holding,
        "currency": doc.currency,
        "customer": doc.customer,
        "customer_name": doc.get("customer_name") or doc.customer,
        "phone": to_local(default_phone(doc)) or "",
        "gateways": [{"name": g.name, "label": g.label, "limit": g.limit} for g in gateway_accounts(doc.company)],
    }


@frappe.whitelist(methods=["POST"])
def request_payment(doctype: str, name: str, phone_number: str, amount, payment_gateway_account: str | None = None) -> dict:
    """Prompt the customer's phone for ``amount`` of the order or invoice: a Payment Request for it,
    submitted, which sends the prompt. Returns the Payment Request and its prompt."""
    require(COLLECT_ROLES)
    _check_doctype(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    frappe.has_permission(PAYMENT_REQUEST, "create", throw=True)
    doc = frappe.get_doc(doctype, name)
    problem = document_problem(doc)
    if problem:
        frappe.throw(problem)
    gateway = _gateway(doc.company, payment_gateway_account)
    phone = normalize_msisdn(phone_number)
    if not phone:
        frappe.throw(_("Enter the customer's Kenyan mobile number, e.g. 0712345678."))
    amount = flt(amount, 2)
    _check_amount(doc, amount, gateway)
    _rate_limit()
    pending = prompt_pending(doc)
    if pending:
        frappe.throw(
            _("A prompt was just sent for {0} ({1}): wait for the customer to answer it before sending another.").format(
                doc.name, pending
            )
        )
    _check_drafts(doc)
    if enabled("supersede_unpaid_prompts"):
        # what make_payment_request counts must not include what this prompt replaces
        supersede_unpaid(frappe._dict(reference_doctype=doc.doctype, reference_name=doc.name, name=None))
    pr = _payment_request(doc, gateway, phone, amount)
    return {
        "payment_request": pr.name,
        "express_request": frappe.db.get_value(
            EXPRESS, {"reference_doctype": PAYMENT_REQUEST, "reference_name": pr.name}, "name", order_by="creation desc"
        ),
        "amount": amount,
        "phone": to_local(phone),
    }


# ---------------------------------------------------------------- the document
def _check_doctype(doctype: str) -> None:
    if doctype not in DOCTYPES:
        frappe.throw(_("M-Pesa payment can be requested from a Sales Order or a Sales Invoice only."))


def document_problem(doc) -> str | None:
    """Why ``doc`` cannot be paid by a prompt from its form, or None."""
    if doc.docstatus != 1:
        return _("Submit {0} first: payment is requested on a submitted document.").format(doc.name)
    if cstr(doc.get("currency")) != "KES":
        return _("M-Pesa collects KES: {0} is in {1}.").format(doc.name, doc.get("currency"))
    if doc.doctype == "Sales Invoice":
        if doc.get("is_return"):
            return _("{0} is a return: nothing is collected on it.").format(doc.name)
        if doc.get("is_pos"):
            return _("{0} is a POS invoice: collect it from its payments (Lipa Na Mpesa).").format(doc.name)
    if doc.doctype == "Sales Order":
        if doc.get("status") in ("Closed", "On Hold", "Completed"):
            return _("{0} is {1}: payment is not requested on it.").format(doc.name, doc.status)
        if flt(doc.get("per_billed")) >= 100:
            return _("{0} is fully invoiced: request payment on its invoice.").format(doc.name)
    if owed(doc) <= 0:
        return _("{0} has nothing left to pay.").format(doc.name)
    return None


def owed(doc) -> float:
    """What the document still owes, as ERPNext's Payment Request reckons it (order total less
    advances, invoice outstanding)."""
    from erpnext.accounts.doctype.payment_request.payment_request import get_amount

    return flt(get_amount(doc), 2)


def requested(doc) -> tuple:
    """(amount, names): what submitted Payment Requests of ``doc`` still ask for, which ERPNext
    counts against a new one, less the unpaid M-Pesa ones a new prompt supersedes."""
    supersede = enabled("supersede_unpaid_prompts")
    has_outstanding = frappe.get_meta(PAYMENT_REQUEST).has_field("outstanding_amount")
    fields = ["name", "status", "grand_total", "payment_gateway", "payment_gateway_account"]
    if has_outstanding:
        fields.append("outstanding_amount")
    held, names = 0.0, []
    for row in frappe.get_all(
        PAYMENT_REQUEST,
        filters={"reference_doctype": doc.doctype, "reference_name": doc.name, "docstatus": 1},
        fields=fields,
        order_by="creation asc",
    ):
        if supersede and row.status in UNPAID and is_mpesa(row):
            continue
        amount = flt(row.outstanding_amount) if has_outstanding else (0 if row.status == "Paid" else flt(row.grand_total))
        if amount > 0:
            held += amount
            names.append(row.name)
    return flt(held, 2), names


def default_phone(doc) -> str | None:
    """The document's contact mobile, else the customer's."""
    return doc.get("contact_mobile") or frappe.db.get_value("Customer", doc.customer, "mobile_no")


def _check_amount(doc, amount: float, gateway) -> None:
    if amount < STK_MIN_AMOUNT:
        frappe.throw(_("Ask for at least KES {0}.").format(STK_MIN_AMOUNT))
    held, holding = requested(doc)
    left = flt(owed(doc) - held, 2)
    if left <= 0:
        frappe.throw(
            _("Payment Requests {0} already ask for everything {1} owes: resend their prompt instead.").format(
                ", ".join(holding), doc.name
            )
        )
    if amount > left + 0.005:
        frappe.throw(_("{0} has KES {1} left to ask for: ask for KES {1} or less.").format(doc.name, f"{left:,.2f}"))
    if amount > gateway.limit:
        frappe.throw(
            _("KES {0} is above the M-Pesa Transaction Limit of KES {1} on {2}: ask for KES {1} or less, and "
              "for the rest in another request.").format(f"{amount:,.2f}", f"{gateway.limit:,.2f}", gateway.settings)
        )


def _check_drafts(doc) -> None:
    """ERPNext's make_payment_request reuses a draft Payment Request of the document: one that is
    not an M-Pesa draft this prompt removes must be dealt with first."""
    supersede = enabled("supersede_unpaid_prompts")
    for row in frappe.get_all(
        PAYMENT_REQUEST,
        filters={"reference_doctype": doc.doctype, "reference_name": doc.name, "docstatus": 0},
        fields=["name", "payment_gateway", "payment_gateway_account"],
        order_by="creation asc",
    ):
        if not (supersede and is_mpesa(row)):
            frappe.throw(
                _("Payment Request {0} is a draft for {1}: submit it, or delete it, first.").format(row.name, doc.name)
            )


# ---------------------------------------------------------------- shortcodes
def gateway_accounts(company: str) -> list:
    """The M-Pesa Payment Gateway Accounts that can prompt for ``company`` (its Mpesa Settings
    sends STK prompts), default first, each with the most one prompt may ask for."""
    fields = ["name", "payment_gateway", "payment_account", "is_default"]
    by_company = frappe.get_meta(GATEWAY_ACCOUNT).has_field("company")  # ERPNext v16
    if by_company:
        fields.append("company")
    settings_fields = ["name", "business_shortcode", "till_number", "transaction_limit", apis.STK]
    if frappe.get_meta(SETTINGS).has_field("company"):
        settings_fields.append("company")
    out = []
    for row in frappe.get_all(GATEWAY_ACCOUNT, fields=fields, order_by="is_default desc, creation asc"):
        gateway = frappe.db.get_value(
            "Payment Gateway", row.payment_gateway, ["gateway_settings", "gateway_controller"], as_dict=True
        )
        if not gateway or gateway.gateway_settings != SETTINGS or not gateway.gateway_controller:
            continue
        settings = frappe.db.get_value(SETTINGS, gateway.gateway_controller, settings_fields, as_dict=True)
        if not settings or not apis.enabled(settings, apis.STK):
            continue
        account_company = row.get("company") if by_company else frappe.db.get_value(
            "Account", row.payment_account, "company")
        if (account_company and account_company != company) or (settings.get("company") or company) != company:
            continue
        code = cstr(settings.business_shortcode or settings.till_number).strip()
        out.append(frappe._dict(
            name=row.name, payment_gateway=row.payment_gateway, settings=settings.name,
            label=f"{settings.name} ({code})" if code else settings.name,
            limit=min(flt(settings.transaction_limit) or STK_MAX_AMOUNT, STK_MAX_AMOUNT),
        ))
    return out


def _gateway(company: str, name: str | None):
    accounts = gateway_accounts(company)
    if not accounts:
        frappe.throw(
            _("No M-Pesa shortcode of {0} sends STK prompts: tick Send STK Prompts on its Mpesa Settings.").format(company)
        )
    if not name:
        return accounts[0]
    for account in accounts:
        if account.name == name:
            return account
    frappe.throw(_("Payment Gateway Account {0} cannot prompt for {1}.").format(name, company))


# ---------------------------------------------------------------- how often
def _rate_limit() -> None:
    """A fixed one-minute window per user in Redis, as the Payments API's (gateway.auth)."""
    cache = get_cache()
    key = cache.make_key(f"mpsa_suite:rl:collect:{frappe.session.user}:{now_datetime().strftime('%Y%m%d%H%M')}")
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 70)
    if count > PROMPTS_PER_MINUTE:
        frappe.throw(_("Too many M-Pesa prompts in a minute: wait a minute, then try again."))


def prompt_pending(doc) -> str | None:
    """The document's prompt sent less than RESEND_AFTER_SECONDS ago and not answered yet, if any
    (through one of its Payment Requests or straight from the invoice)."""
    since = now_datetime() - datetime.timedelta(seconds=RESEND_AFTER_SECONDS)
    requests = frappe.get_all(
        PAYMENT_REQUEST,
        filters={"reference_doctype": doc.doctype, "reference_name": doc.name, "docstatus": 1},
        pluck="name",
        order_by="creation asc",
    )
    for reference_doctype, names in ((PAYMENT_REQUEST, requests), (doc.doctype, [doc.name])):
        if not names:
            continue
        pending = frappe.db.get_value(
            EXPRESS,
            {"reference_doctype": reference_doctype, "reference_name": ["in", names], "status": "In Progress",
             "docstatus": 1, "creation": [">", since]},
            "name",
            order_by="creation desc",
        )
        if pending:
            return pending
    return None


# ---------------------------------------------------------------- the Payment Request
def _payment_request(doc, gateway, phone: str, amount: float):
    """ERPNext's own Payment Request for ``doc``, for ``amount`` to ``phone``; submitting it sends
    the prompt (PaymentRequest.request_phone_payment -> Mpesa Settings.request_for_payment)."""
    from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

    pr = make_payment_request(
        dt=doc.doctype, dn=doc.name, payment_gateway_account=gateway.name, phone_number=phone,
        mute_email=1, submit_doc=0, return_doc=1,
    )
    pr.update({"grand_total": amount, "phone_number": phone, "payment_channel": "Phone"})
    pr.flags.mute_email = True
    # as make_payment_request(submit_doc=1) does: whoever may create the request sends it
    pr.flags.ignore_permissions = True
    if pr.is_new():
        pr.insert()
    else:
        pr.save()
    pr.submit()
    return pr
