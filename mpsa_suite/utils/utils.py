import re
from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import flt, get_url

from ..accounting.posting import insert_entry
from .doctype_names import ACCESS_TOKENS_DOCTYPE, MPESA_EXPRESS_REQUEST_DOCTYPE


def create_payment_gateway(
    gateway: str, settings: str | None = None, controller: str | None = None
) -> None:
    # NOTE: we don't translate Payment Gateway name because it is an internal doctype
    if not frappe.db.exists("Payment Gateway", gateway):
        payment_gateway = frappe.get_doc(
            {
                "doctype": "Payment Gateway",
                "gateway": gateway,
                "gateway_settings": settings,
                "gateway_controller": controller,
            }
        )
        payment_gateway.insert(ignore_permissions=True)


@contextmanager
def erpnext_app_import_guard() -> Generator:
    marketplace_link = (
        '<a href="https://frappecloud.com/marketplace/apps/erpnext">Marketplace</a>'
    )
    github_link = '<a href="https://github.com/frappe/erpnext">GitHub</a>'
    msg = _("erpnext app is not installed. Please install it from {} or {}").format(
        marketplace_link, github_link
    )
    try:
        yield
    except ImportError:
        frappe.throw(msg, title=_("Missing ERPNext App"))


def save_access_token(
    token: str,
    expiry_time: str | datetime,
    fetch_time: str | datetime,
    associated_setting: str,
    doctype: str = ACCESS_TOKENS_DOCTYPE,
) -> bool:
    doc = frappe.new_doc(doctype)

    doc.associated_settings = associated_setting

    doc.access_token = token
    doc.expiry_time = expiry_time
    doc.token_fetch_time = fetch_time

    try:
        doc.save(ignore_permissions=True)
        doc.submit()

        return True

    except Exception:
        # TODO: Not sure what exception is thrown here. Confirm
        frappe.throw("Error Encountered")
        return False


def get_payment_gateway_controller(payment_gateway):
    """Return payment gateway controller"""
    gateway = frappe.get_doc("Payment Gateway", payment_gateway)
    if gateway.gateway_controller is None:
        try:
            return frappe.get_doc(f"{payment_gateway} Settings")
        except Exception:
            frappe.throw(_("{0} Settings not found").format(payment_gateway))
    else:
        try:
            return frappe.get_doc(gateway.gateway_settings, gateway.gateway_controller)
        except Exception:
            frappe.throw(_("{0} Settings not found").format(payment_gateway))


def create_payment_gateway_account(gateway, payment_channel="Email", company=None):
    from erpnext.setup.setup_wizard.operations.install_fixtures import (
        create_bank_account,
    )

    company = company or frappe.get_cached_value(
        "Global Defaults", "Global Defaults", "default_company"
    )
    if not company:
        return

    # NOTE: we translate Payment Gateway account name because that is going to be used by the end user
    bank_account = frappe.db.get_value(
        "Account",
        {"account_name": _(gateway), "company": company},
        ["name", "account_currency"],
        as_dict=1,
    )

    if not bank_account:
        # check for untranslated one
        bank_account = frappe.db.get_value(
            "Account",
            {"account_name": gateway, "company": company},
            ["name", "account_currency"],
            as_dict=1,
        )

    if not bank_account:
        # try creating one
        bank_account = create_bank_account(
            {"company_name": company, "bank_account": _(gateway)}
        )

    if not bank_account:
        frappe.msgprint(
            _("Payment Gateway Account not created, please create one manually.")
        )
        return

    # if payment gateway account exists, return
    existing = {"payment_gateway": gateway, "currency": bank_account.account_currency}
    if frappe.get_meta("Payment Gateway Account").has_field("company"):
        existing["company"] = company  # ERPNext v16: one gateway account per company
    if frappe.db.exists("Payment Gateway Account", existing):
        return

    try:
        account = frappe.get_doc(
            {
                "doctype": "Payment Gateway Account",
                "is_default": 1,
                "payment_gateway": gateway,
                "payment_account": bank_account.name,
                "currency": bank_account.account_currency,
                "payment_channel": payment_channel,
            }
        )
        if account.meta.has_field("company"):
            account.company = company  # ERPNext v16 names the account after the company's abbr
        account.insert(ignore_permissions=True, ignore_if_duplicate=True)

    except frappe.DuplicateEntryError:
        # already exists, due to a reinstall?
        pass


def build_callback_url(endpoint: str, add_token: bool = True) -> str:
    # get_url(), not get_request_site_address(True): the full-address form reads the current
    # request's URI and raises "object is not bound" in background jobs and scripts.
    base_url = get_url()
    parsed_url = urlparse(base_url)

    if not (
        parsed_url.hostname == "localhost"
        or parsed_url.hostname.replace(".", "").isdigit()
    ):
        base_url = f"{parsed_url.scheme}://{parsed_url.hostname}"

    from ..daraja.callbacks import with_token

    url = f"{base_url}/api/method/{endpoint}"
    return with_token(url) if add_token else url


def log_and_throw_error(err_msg, context=None):
    frappe.log_error(frappe.get_traceback(), err_msg)
    if context:
        frappe.throw(_(f"{err_msg}: {context}"))


def _book_invoice_payment(request_doc, sales_invoice) -> None:
    """Payment Entry for a paid STK prompt against a submitted, non-POS Sales Invoice.

    One entry per receipt (the receipt is its reference); allocation capped at what the
    invoice still owes, the rest stays unallocated on the customer.
    """
    from ..mpsa_suite.api.payment_entry import create_payment_entry
    from ..money_in.common import mode_of_payment_for, named_lock

    receipt = request_doc.transaction_id
    settings_name = request_doc.get("settings") or frappe.db.get_value(
        "Payment Gateway", request_doc.payment_gateway, "gateway_controller"
    )
    with named_lock(f"book:{receipt}"):
        if frappe.db.get_value("Payment Entry", {"reference_no": receipt, "docstatus": ["<", 2]}, "name", for_update=True):
            return
        paid = flt(request_doc.amount)
        owed = flt(frappe.db.get_value("Sales Invoice", sales_invoice.name, "outstanding_amount"))
        refs = None
        if owed > 0:
            refs = [{"reference_doctype": "Sales Invoice", "reference_name": sales_invoice.name,
                     "allocated_amount": min(paid, owed)}]
        paid_on = frappe.utils.getdate(request_doc.get("transaction_date") or frappe.utils.nowdate())
        cost_center = sales_invoice.get("cost_center") or (
            frappe.db.get_value("Mpesa Settings", settings_name, "cost_center")
            if settings_name and frappe.get_meta("Mpesa Settings").has_field("cost_center") else None
        )
        try:
            create_payment_entry(
                sales_invoice.company, sales_invoice.customer, paid,
                sales_invoice.get("party_account_currency") or sales_invoice.currency or "KES",
                mode_of_payment_for(settings_name) or request_doc.payment_gateway, "Customer",
                paid_on, receipt, paid_on, cost_center, 1, references=refs,
            )
        except Exception:
            log_and_throw_error("Payment Creation Error", request_doc.name)


def stk_books_part_payments(payment_request) -> bool:
    """Whether a paid STK prompt for part of this Payment Request is booked for what it collected.

    Yes when the customer's account is in KES, as M-Pesa is, on an ERPNext that tracks a
    request's outstanding (late v15, v16): see _book_payment_request_payment. Otherwise
    ERPNext's create_payment_entry books the request's whole outstanding, whatever was paid.
    """
    currency = payment_request.get("party_account_currency") or payment_request.get("currency") or "KES"
    return currency == "KES" and frappe.get_meta("Payment Request").has_field("outstanding_amount")


def _book_payment_request_payment(request_doc, payment_request) -> None:
    """Payment Entry for what a paid STK prompt collected on a Payment Request (in KES).

    ERPNext's create_payment_entry books the request's whole outstanding, whatever was paid:
    a prompt for one part of a request split by the transaction limit over-books it, and a
    prompt for more than is left books the outstanding only and leaves the rest in no ledger.
    Here the entry is for the amount paid; the request and its document get no more than
    each still owes, the rest stays unallocated on the customer, and ERPNext sets the
    request Partially Paid or Paid. One entry per receipt (the receipt is its reference,
    not the request name, so a second part is not refused as a duplicate), as for invoices.
    """
    from ..money_in.common import named_lock

    receipt = request_doc.transaction_id
    with named_lock(f"book:{receipt}"):
        if frappe.db.get_value("Payment Entry", {"reference_no": receipt, "docstatus": ["<", 2]}, "name", for_update=True):
            return
        paid = flt(request_doc.amount)
        if order_billed_in_full(payment_request):
            _book_on_billed_order(request_doc, payment_request, paid)
            return
        pe = payment_request.create_payment_entry(submit=False)
        if pe.paid_from_account_currency != pe.paid_to_account_currency:
            frappe.throw(
                _("Payment Request {0} is paid into an account in another currency; book M-Pesa receipt {1} manually.").format(
                    payment_request.name, receipt
                )
            )
        if pe.get("deductions") and abs(flt(pe.paid_amount) - paid) > 0.005:
            pe.set("deductions", [])  # an early-payment discount is for paying the outstanding in full
        pe.paid_amount = pe.received_amount = paid
        deducted = sum(flt(d.amount) for d in pe.get("deductions") or [])
        # STK asks whole shillings, so a request owing 1,000.40 is paid 1,000. The cents go to
        # Round Off (the old half-shilling tolerance); left open, the request would stay
        # Partially Paid for less than any prompt can ask (the STK minimum is 1).
        cents = flt(payment_request.outstanding_amount) - paid - deducted
        round_off = _round_off_account(pe.company) if 0 < cents <= 0.5 else None
        _allocate_up_to(pe, paid + deducted + (cents if round_off else 0), payment_request)
        if round_off:
            written_off = flt(sum(flt(r.allocated_amount) for r in pe.get("references")) - paid - deducted, 2)
            if written_off > 0:  # less, or none, when the document itself owes less
                pe.append("deductions", {
                    "account": round_off[0], "cost_center": pe.get("cost_center") or round_off[1],
                    "amount": flt(written_off * flt(pe.get("source_exchange_rate") or 1), 2),  # company currency
                })
        pe.reference_no = receipt
        pe.reference_date = frappe.utils.getdate(request_doc.get("transaction_date") or frappe.utils.nowdate())
        insert_entry(pe, ignore_permissions=True)  # submitted in one save (accounting.posting)


def order_billed_in_full(payment_request) -> bool:
    """A Payment Request on a Sales Order that is now billed in full: ERPNext's get_payment_entry
    refuses it ("Can only make payment against unbilled Sales Order"), so its
    create_payment_entry cannot build the entry, and the order cannot be billed again."""
    if payment_request.get("reference_doctype") != "Sales Order":
        return False
    per_billed = frappe.db.get_value("Sales Order", payment_request.reference_name, "per_billed")
    allowance = flt(frappe.db.get_single_value("Accounts Settings", "over_billing_allowance"))
    return flt(per_billed, 2) >= 100.0 + allowance


def _book_on_billed_order(request_doc, payment_request, paid: float) -> None:
    """Payment Entry for a paid prompt whose Payment Request's order was billed meanwhile (by
    hand, or when another request for it was paid): the receipt goes to the order's open
    invoices, the rest unallocated on the customer, and the request is closed for what it
    collected, as ERPNext would have. Left to ERPNext, the booking was refused and retried
    until it gave up, with the receipt in no ledger."""
    from erpnext.accounts.party import get_party_account
    from erpnext.accounts.utils import get_account_currency

    from ..mpsa_suite.api.payment_entry import get_bank_cash_account
    from ..money_in.common import mode_of_payment_for

    receipt = request_doc.transaction_id
    order = frappe.db.get_value(
        "Sales Order", payment_request.reference_name, ["company", "customer", "cost_center"], as_dict=True
    )
    settings_name = request_doc.get("settings") or frappe.db.get_value(
        "Payment Gateway", request_doc.payment_gateway, "gateway_controller"
    )
    mode_of_payment = payment_request.get("mode_of_payment") or mode_of_payment_for(settings_name)
    party_account = get_party_account("Customer", order.customer, order.company)
    # The request's own account (its gateway account's), as ERPNext's create_payment_entry books it
    bank_account = payment_request.get("payment_account") or (
        (get_bank_cash_account(order.company, mode_of_payment) or {}).get("account") if mode_of_payment else None
    )
    if not bank_account:
        frappe.throw(
            _("Payment Request {0} has no payment account; book M-Pesa receipt {1} manually.").format(
                payment_request.name, receipt
            )
        )
    party_currency, bank_currency = get_account_currency(party_account), get_account_currency(bank_account)
    if party_currency != bank_currency:
        frappe.throw(
            _("Payment Request {0} is paid into an account in another currency; book M-Pesa receipt {1} manually.").format(
                payment_request.name, receipt
            )
        )

    # The cents a whole-shilling prompt cannot ask go to Round Off, as in _book_payment_request_payment,
    # but only those an invoice still owes: none are written off on the request alone.
    cents = flt(flt(payment_request.get("outstanding_amount")) - paid, 2)
    round_off = _round_off_account(order.company) if 0 < cents <= 0.5 else None
    billed_by = frappe.db.get_values(
        "Sales Invoice Item", {"sales_order": payment_request.reference_name, "docstatus": 1}, "parent",
        order_by="creation asc", pluck=True,
    )
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={"name": ["in", list(dict.fromkeys(billed_by)) or [""]], "docstatus": 1,
                 "outstanding_amount": [">", 0], "customer": order.customer, "debit_to": party_account},
        fields=["name", "outstanding_amount"],
        order_by="posting_date asc, creation asc",
    )
    refs, left = [], flt(paid + (cents if round_off else 0), 2)
    for invoice in invoices:
        if left <= 0:
            break
        amount = min(left, flt(invoice.outstanding_amount))
        refs.append({"reference_doctype": "Sales Invoice", "reference_name": invoice.name, "allocated_amount": amount})
        left = flt(left - amount, 2)
    allocated = flt(sum(r["allocated_amount"] for r in refs), 2)
    written_off = max(flt(allocated - paid, 2), 0)  # what the invoices owed beyond what was paid
    invoices_owe = flt(sum(flt(i.outstanding_amount) for i in invoices) - allocated, 2)

    paid_on = frappe.utils.getdate(request_doc.get("transaction_date") or frappe.utils.nowdate())
    cost_center = order.get("cost_center") or (
        frappe.db.get_value("Mpesa Settings", settings_name, "cost_center")
        if settings_name and frappe.get_meta("Mpesa Settings").has_field("cost_center") else None
    )
    pe = frappe.new_doc("Payment Entry")
    pe.update({
        "payment_type": "Receive", "company": order.company, "posting_date": paid_on,
        "mode_of_payment": mode_of_payment, "party_type": "Customer", "party": order.customer,
        "paid_from": party_account, "paid_from_account_currency": party_currency,
        "paid_to": bank_account, "paid_to_account_currency": bank_currency,
        "paid_amount": paid, "received_amount": paid, "reference_no": receipt, "reference_date": paid_on,
    })
    if cost_center:
        pe.cost_center = cost_center
    for ref in refs:
        pe.append("references", ref)
    pe.setup_party_account_field()
    pe.set_missing_values()
    pe.set_exchange_rate()
    if written_off:
        pe.append("deductions", {
            "account": round_off[0], "cost_center": pe.get("cost_center") or round_off[1],
            "amount": flt(written_off * flt(pe.get("source_exchange_rate") or 1), 2),  # company currency
        })
    insert_entry(pe, ignore_permissions=True)

    # The entry has no request row (the order is billed), so ERPNext does not touch the request.
    # Under half a shilling left, with no invoice still owing it, the request is settled.
    owed = flt(flt(payment_request.get("outstanding_amount")) - paid - written_off, 2)
    settled = owed <= 0 or (owed <= 0.5 and invoices_owe <= 0)
    values = {"status": "Paid" if settled else "Partially Paid"}
    if frappe.get_meta("Payment Request").has_field("outstanding_amount"):
        values["outstanding_amount"] = 0 if settled else owed
    frappe.db.set_value("Payment Request", payment_request.name, values)


def _allocate_up_to(pe, available: float, payment_request) -> None:
    """Lower, never raise, the allocations ERPNext made on ``pe``.

    In all at most ``available``; per document at most what it still owes (the outstanding
    ERPNext refreshed on its rows, shared by its payment-term rows); on the request's rows at
    most what the request still owes. A row whose request has nothing left loses the link;
    its document still gets what it owes.
    """
    owed = {}
    request_left = {payment_request.name: max(flt(payment_request.outstanding_amount), 0)}
    for row in pe.get("references"):
        precision = row.precision("allocated_amount")
        key = (row.reference_doctype, row.reference_name)
        owed.setdefault(key, max(flt(row.outstanding_amount), 0))
        amount = min(flt(row.allocated_amount), owed[key], available)
        if row.payment_request and request_left.get(row.payment_request, 0) > 0:
            amount = min(amount, request_left[row.payment_request])
            request_left[row.payment_request] = flt(request_left[row.payment_request] - amount, precision)
        else:
            row.payment_request = None
        row.allocated_amount = max(flt(amount, precision), 0)
        owed[key] = flt(owed[key] - row.allocated_amount, precision)
        available = flt(available - row.allocated_amount, precision)
    pe.set("references", [row for row in pe.get("references") if flt(row.allocated_amount) > 0])


def _round_off_account(company):
    """The company's Round Off account and cost center, or None when it has no such account."""
    account, cost_center, default_cost_center = frappe.get_cached_value(
        "Company", company, ["round_off_account", "round_off_cost_center", "cost_center"]
    ) or (None, None, None)
    return (account, cost_center or default_cost_center) if account else None


def _stk_paid_covers_payment_request(request_doc) -> bool:
    """False when the STK amount paid is short of the Payment Request (KES requests only).

    Only for a request booked for its whole amount (see stk_books_part_payments): marking
    it paid books its full outstanding, so a short payment must not get there. STK sends
    whole shillings, hence the half-shilling tolerance.
    """
    fields = ["grand_total", "currency"]
    if frappe.get_meta("Payment Request").has_field("party_account_currency"):
        # arrived with outstanding_amount (its currency): an older ERPNext has neither column
        fields.append("party_account_currency")
    pr = frappe.db.get_value("Payment Request", request_doc.reference_name, fields, as_dict=True)
    if not pr or stk_books_part_payments(pr) or (pr.currency or "KES") != "KES":
        return True
    return flt(request_doc.amount) + 0.5 >= flt(pr.grand_total)


def handle_successful_transaction(request_doc, settings):
    """Handle actions for a successful transaction"""

    if request_doc.reference_doctype == "Payment Entry" and not request_doc.transaction_id:
        return

    if (
        request_doc.reference_doctype == "Payment Request"
        and "erpnext" in frappe.get_installed_apps()
        and not _stk_paid_covers_payment_request(request_doc)
    ):
        # Raised, not returned: money_in.stk_verify.book has already cancelled any C2B
        # register for this receipt, and only its rollback brings that booking back.
        frappe.throw(
            _("Mpesa Express Request {0} ({1}) paid KES {2}, short of Payment Request {3}. Not marked paid; reconcile manually.").format(
                request_doc.name, request_doc.transaction_id, flt(request_doc.amount), request_doc.reference_name
            ),
            title=_("M-Pesa STK short payment"),
        )

    if request_doc.get("reference_doctype") and request_doc.get("reference_name"):
        from ..money_in.common import BOOKING_RECEIPT_FLAG

        # a payment intent counts only this receipt as the one being booked (see _extra_receipts)
        frappe.flags[BOOKING_RECEIPT_FLAG] = request_doc.transaction_id
        try:
            frappe.get_doc(
                request_doc.get("reference_doctype"), request_doc.get("reference_name")
            ).run_method("on_payment_authorized", "Completed")
        finally:
            frappe.flags[BOOKING_RECEIPT_FLAG] = None
        set_mpesa_request_reconciled(request_doc)
        # No commit here: the caller (money_in.stk_verify job or a manager's Reconcile
        # click) owns the transaction, so a later failure undoes the whole booking.

    if "erpnext" in frappe.get_installed_apps():
        if request_doc.reference_doctype == "Payment Request":
            payment_request = frappe.get_doc(
                "Payment Request", request_doc.reference_name
            )
            if payment_request.reference_doctype == "Sales Invoice":
                invoice = frappe.get_doc(
                    "Sales Invoice", payment_request.reference_name
                )
                if invoice.docstatus == 0:
                    try:
                        invoice.submit()
                    except Exception:
                        log_and_throw_error(
                            "Payment Request Submission Error", request_doc.name
                        )
            books_paid = stk_books_part_payments(payment_request)
            # within half a shilling counts as settled: STK cannot ask for less than 1
            was_open = flt(payment_request.get("outstanding_amount")) > 0.5
            try:
                if books_paid:
                    _book_payment_request_payment(request_doc, payment_request)
                else:
                    # ERPNext's create_payment_entry() inserts, then submits (a workflow's Draft state):
                    # built as a draft, posted in one save (accounting.posting) as its insert and submit did
                    insert_entry(payment_request.create_payment_entry(submit=False), ignore_permissions=True)
            except Exception:
                log_and_throw_error("Payment Entry Creation Error", request_doc.name)

            try:
                if (
                    settings.auto_create_sales_invoice
                    and payment_request.reference_doctype == "Sales Order"
                    # once, when this payment settles the request: billing the order after
                    # one part of a split request refuses the next part's Payment Entry
                    and (
                        not books_paid
                        or (
                            was_open
                            and flt(frappe.db.get_value("Payment Request", payment_request.name, "outstanding_amount")) <= 0.5
                        )
                    )
                    # billed already (the payment went to its invoices): billing it again throws
                    and not order_billed_in_full(payment_request)
                ):
                    from erpnext.selling.doctype.sales_order.sales_order import (
                        make_sales_invoice,
                    )

                    si = make_sales_invoice(
                        payment_request.reference_name, ignore_permissions=True
                    )
                    si.allocate_advances_automatically = True
                    si = si.insert(ignore_permissions=True)
                    si.submit()
            except Exception:
                log_and_throw_error("Sales Invoice Creation Error", request_doc.name)

            if not books_paid:  # otherwise ERPNext set Partially Paid / Paid from the allocation
                frappe.db.set_value(
                    "Payment Request", payment_request.name, "status", "Paid"
                )
            set_mpesa_request_reconciled(request_doc)

        elif request_doc.reference_doctype == "Sales Invoice":
            sales_invoice = frappe.get_doc("Sales Invoice", request_doc.reference_name)

            if sales_invoice.get("is_created_using_pos"):
                # Mark payment as received but don't modify the invoice during checkout
                # The POS form will handle payment rows when it submits
                if sales_invoice.docstatus == 0:
                    # Invoice is still in draft (being edited in POS), just mark the request as reconciled
                    set_mpesa_request_reconciled(request_doc)
                else:
                    # Invoice is already submitted, add payment row
                    found_payment = False
                    for pay in sales_invoice.payments:
                        if pay.mode_of_payment == request_doc.payment_gateway:
                            pay.phone_number = request_doc.phone_number
                            pay.amount = float(request_doc.amount)
                            pay.reference_no = request_doc.transaction_id
                            pay.clearance_date = frappe.utils.nowdate()
                            found_payment = True
                            break

                    if not found_payment:
                        sales_invoice.append(
                            "payments",
                            {
                                "mode_of_payment": request_doc.payment_gateway,
                                "phone_number": request_doc.phone_number,
                                "amount": float(request_doc.amount),
                                "reference_no": request_doc.transaction_id,
                                "clearance_date": frappe.utils.nowdate(),
                            },
                        )

                    sales_invoice.save(ignore_permissions=True)
                    set_mpesa_request_reconciled(request_doc)
            else:
                if sales_invoice.docstatus == 0:
                    try:
                        sales_invoice.submit()
                    except Exception:
                        log_and_throw_error(
                            "Sales Invoice Submission Error", request_doc.name
                        )
                # Upstream appended a payments row and saved: a submitted invoice refuses that
                # (UpdateAfterSubmitError), so every such prompt stayed paid but unbooked.
                _book_invoice_payment(request_doc, sales_invoice)
                set_mpesa_request_reconciled(request_doc)

        elif request_doc.reference_doctype == "Sales Invoice Payment":
            try:
                frappe.db.set_value(
                    "Sales Invoice Payment",
                    request_doc.reference_name,
                    {
                        "reference_no": request_doc.transaction_id,
                    },
                )
                set_mpesa_request_reconciled(request_doc)
            except Exception:
                log_and_throw_error(
                    "Sales Invoice Payment Update Error", request_doc.name
                )
        elif request_doc.reference_doctype == "Payment Entry":
            payment_entry = frappe.get_doc("Payment Entry", request_doc.reference_name)
            if payment_entry.docstatus == 0:
                try:
                    payment_entry.reference_no = request_doc.transaction_id
                    payment_entry.custom_mpesa_receipt_number = (
                        request_doc.transaction_id
                    )
                    payment_entry.reference_date = (
                        frappe.utils.getdate(request_doc.transaction_date)
                        if request_doc.transaction_date
                        else frappe.utils.nowdate()
                    )
                    # one save, submitted (not a save then a submit): accounting.posting
                    payment_entry.flags.ignore_permissions = True
                    payment_entry.submit()
                    set_mpesa_request_reconciled(request_doc)
                except Exception:
                    log_and_throw_error(
                        "Payment Entry Submission Error", request_doc.name
                    )
    event_payload = {
        "status": "Success",
        "request_name": request_doc.name,
        "reference_doctype": request_doc.reference_doctype,
        "reference_name": request_doc.reference_name,
        "transaction_id": request_doc.transaction_id,
        "amount": request_doc.amount,
        "phone_number": request_doc.phone_number,
    }

    # Publish to the request owner first so POS clients subscribed as cashier receive updates.
    frappe.publish_realtime(
        event="mpesa_stk_payment_completed",
        message=event_payload,
        user=request_doc.owner,
    )

    # Keep a global event for integrations that are not user-room specific.
    frappe.publish_realtime(
        event="mpesa_stk_payment_completed",
        message=event_payload,
    )


def set_mpesa_request_reconciled(request_doc):
    request_doc.reload()
    request_doc.is_reconciled = 1
    request_doc.save(ignore_permissions=True)


def update_mpesa_request_status(name, status_data):
    """Update the Mpesa Express Request DocType with callback status"""
    frappe.db.set_value(MPESA_EXPRESS_REQUEST_DOCTYPE, name, status_data)
    frappe.publish_realtime(
        event="refresh_form", doctype=MPESA_EXPRESS_REQUEST_DOCTYPE, docname=name
    )


def validate_phone_number(phone_number):
    if not phone_number or len(phone_number) < 9:
        return False

    number = phone_number.strip().replace(" ", "")
    if not re.match(r"^(?:\+254|254|0)(7\d{8}|1\d{8})$", number):
        return False

    return True


@frappe.whitelist()
def get_mode_of_payment_account(mode_of_payment: str, company: str) -> str:
    # the B2C Payment Disbursement form's helper (HR users who prepare payroll there may not read Mode of Payment)
    frappe.has_permission("B2C Payment Disbursement", "write", throw=True)
    return frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mode_of_payment, "company": company},
        "default_account",
    )


@frappe.whitelist(allow_guest=True)
def convert_amount_to_kes(
    currency: str, amount: float, date: str = None, settings: str = None
) -> float | None:
    """
    Convert the given amount from `currency` to KES.

    Priority:
    1. Check Mpesa Settings → exchange_rates child table
    2. If ERPNext is installed, use get_exchange_rate
    3. Otherwise, return None
    """
    # Stays allow_guest because the old frappe_mpsa_payments path was (R4), but nothing public
    # calls it: over HTTP it is the Express Request form's helper, for its users only.
    from ..permissions import is_direct_call

    if is_direct_call("convert_amount_to_kes"):
        frappe.has_permission("Mpesa Express Request", "read", throw=True)

    try:
        if settings:
            settings = frappe.get_doc("Mpesa Settings", settings)
    except frappe.DoesNotExistError:
        settings = None

    if settings and settings.exchange_rates:
        for row in settings.exchange_rates:
            if row.currency == currency:
                return float(amount) * float(row.rate)

    if "erpnext" in frappe.get_installed_apps():
        from erpnext.setup.utils import get_exchange_rate

        conversion_rate = get_exchange_rate(
            currency,
            "KES",
            date,
            "for_selling",
        )

        if not conversion_rate:
            frappe.throw("Conversion rate not available to convert amount to KES.")

        return float(amount) * float(conversion_rate)

    return None
