# Copyright (c) 2025, Navari Limited and contributors
# For license information, please see license.txt


import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt
from mpsa_suite.utils.doctype_names import MPESA_SETTINGS_DOCTYPE
from frappe.exceptions import DoesNotExistError
from ....daraja import apis
from ....daraja.guard import stk_amount
from ....permissions import ADMIN_ROLES, COLLECT_ROLES, require, require_for_direct_call
from ....utils.utils import (
    handle_successful_transaction,
    stk_books_part_payments,
    validate_phone_number,
    convert_amount_to_kes,
)
from ...api.m_pesa_api import (
    check_transaction_status,
    initiate_stk_push,
)  # Import the STK push function


class MpesaExpressRequest(Document):

    def set_missing_values(self):
        if not self.request_id:
            self.request_id = f"MPESAEXP{frappe.generate_hash(length=32)}"
            self.route = f"/mpesa/stkpush?id={self.request_id}"
        if self.settings and not self.payment_gateway:
            self.payment_gateway = frappe.db.get_value(
                "Payment Gateway", {"gateway_controller": self.settings}, "name"
            )

        if self.payment_gateway and not self.settings:
            self.settings = frappe.db.get_value(
                "Payment Gateway", {"name": self.payment_gateway}, "gateway_controller"
            )

        if self.currency != "KES":
            settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, self.settings)
            if (
                "erpnext" not in frappe.get_installed_apps()
                and not settings.enable_multi_currency_payments
            ):
                frappe.throw("Mpesa Express Requests must be in KES currency.")

            converted_amount = convert_amount_to_kes(self.currency, self.base_amount)
            if converted_amount is not None:
                self.amount = converted_amount

        else:
            if self.base_amount:
                self.amount = self.base_amount

    def validate(self):
        # Take the Payment Request's amount/currency first, so set_missing_values
        # converts it to KES (it used to overwrite the converted amount afterwards).
        if "erpnext" in frappe.get_installed_apps():
            if self.reference_doctype == "Payment Request":
                self.validate_payment_request_amount()

        self.set_missing_values()
        if self.settings and (self.is_new() or self.docstatus == 0):
            apis.require(self.settings, apis.STK)  # submitted requests still take their callbacks

        if self.phone_number and not validate_phone_number(self.phone_number):
            # '712345678', '0712-345-678', '+254 0712...' as the prompt sends them (2547... / 2541...),
            # or refused with the forms to type; never guessed
            from ...api.m_pesa_api import sanitize_mobile_number

            self.phone_number = sanitize_mobile_number(self.phone_number)

    def validate_payment_request_amount(self):
        payment_request = frappe.get_doc("Payment Request", self.reference_name)
        self.currency = payment_request.currency
        # Keep the amount the caller asked for (a manual or gateway prompt for part of the
        # request; MpesaSettings.request_for_payment prompts a Payment Request once, never in
        # parts): overwriting it with the grand total asked the customer for the whole request
        # again. Default to, and never ask more than, what the request still owes, less what a
        # prompt the customer paid, not booked yet, brings (prompt_left).
        owed, paid = prompt_left(payment_request, exclude=getattr(self, "name", None))
        if paid and owed <= 0.005:
            from ....money_in.payment_request import being_booked

            frappe.throw(being_booked(payment_request.name, paid))
        asked = flt(self.base_amount)
        if not asked or asked > owed:
            self.base_amount = owed  # amount (KES) derived from it
        elif owed - asked > 0.005 and not stk_books_part_payments(payment_request):
            # Booked for its whole outstanding whatever was paid: a part, once paid, would be
            # refused as short and its receipt booked nowhere. Refused before any prompt goes.
            frappe.throw(
                f"Payment Request {payment_request.name} can only be booked when paid in full "
                f"({payment_request.currency} {owed}), so it cannot be prompted for {asked}. Prompt "
                "the whole amount, or raise the M-Pesa Transaction Limit (at most 250,000)."
            )
        # company = payment_request.company
        # company_currency = frappe.db.get_value("Company", company, "default_currency")

        # if currency != "KES":
        #     if payment_request.reference_doctype and payment_request.reference_name:
        #         ref_doc = frappe.get_doc(
        #             payment_request.reference_doctype, payment_request.reference_name
        #         )
        #         conversion_rate = getattr(ref_doc, "conversion_rate", None)

        #         if company_currency != "KES":
        #             frappe.throw(
        #                 "STK Push can only be initiated if document or company currency is KES. "
        #                 f"Current currency: {currency}, Company currency: {company_currency}"
        #             )

        #         if not conversion_rate:
        #             frappe.throw(
        #                 "Conversion rate not available to convert amount to KES."
        #             )

        #         self.amount = float(payment_request.grand_total) * float(
        #             conversion_rate
        #         )
        #     else:
        #         frappe.throw("Missing reference document to determine conversion rate.")

    def before_submit(self):
        if self.settings:
            apis.require(self.settings, apis.STK)
        if not self.amount or self.amount <= 0:
            frappe.throw("Amount must be greater than zero to initiate STK Push.")
        try:
            stk_amount(self.amount)  # initiate_stk_push only logs this, so no prompt went out silently
        except ValueError as e:
            frappe.throw(f"Cannot send an STK Push for {self.amount}: {e}.")
        if not self.phone_number:
            frappe.throw("Phone number is required to initiate STK Push.")

    def on_submit(self):
        self.initiate_request()

    @frappe.whitelist()
    def initiate_request(self):
        # Sends a prompt. As a form method it runs for anyone who can read the request; the
        # document flows (submit, retry, the pay page) have checked write or set ignore_permissions.
        require_for_direct_call("initiate_request", COLLECT_ROLES)
        self.check_permission("write")
        args = {
            "payment_gateway": self.payment_gateway,
            "phone_number": self.phone_number,
            "request_amount": self.amount,
            "doctype": self.doctype,
            "document_name": self.name,
            "reference_name": self.reference_name,
        }

        try:
            initiate_stk_push(**args)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "STK Push on Submit Error")
            frappe.throw(f"Failed to initiate STK Push: {str(e)}")

    def on_update_after_submit(self):
        """Neutralize duplicate C2B when transaction_id is finally set after callback."""
        self.validate_duplicate_c2b_records()

    def validate_duplicate_c2b_records(self):
        """Ensure any duplicate C2B is neutralized in favour of this Express Request.

        Decided on the register as committed, read with a lock: a C2B booking still in flight
        is waited for, where this transaction's snapshot would show the register as a draft
        (its delete then failed or removed a booked register, and the prompt booked the receipt
        a second time). A register that cannot be cancelled is raised, never skipped, so the
        caller rolls back and retries instead of booking over its Payment Entry.
        """
        if not self.transaction_id:
            return

        from ....money_in.common import erpnext_installed

        register = "Mpesa C2B Payment Register"
        erpnext = erpnext_installed()  # the register's payment_entry and invoice fields come with ERPNext
        live = frappe.db.get_value(
            register,
            {"transid": self.transaction_id, "docstatus": ["<", 2]},
            ["name", "docstatus"] + (["payment_entry", "pos_invoice", "sales_invoice"] if erpnext else []),
            as_dict=True,
            for_update=True,
        )
        if not live:
            return
        if erpnext and cint(live.docstatus) == 1 and not live.payment_entry and (live.pos_invoice or live.sales_invoice):
            # A cashier put the receipt on an invoice (POS quick pay): the invoice's payment row
            # holds the money, and cancelling the register would not take it back.
            frappe.throw(
                f"C2B register {live.name} for receipt {self.transaction_id} was put on "
                f"{'POS Invoice ' + live.pos_invoice if live.pos_invoice else 'Sales Invoice ' + live.sales_invoice}; "
                "not booked from the prompt, settle it by hand."
            )
        seen = frappe.db.get_value(register, live.name, "docstatus")  # this transaction's snapshot
        if seen is None or cint(seen) != cint(live.docstatus):
            # booked (or inserted) after the snapshot: the documents loaded below would be stale.
            # The STK booking rolls back and its sweep retries in a fresh transaction.
            frappe.throw(
                f"C2B register {live.name} for receipt {self.transaction_id} changed while this "
                "request was being booked; nothing was booked, try again.",
                frappe.TimestampMismatchError,
            )

        c2b_doc = frappe.get_doc(register, live.name)
        c2b_doc.flags.ignore_permissions = True  # system clean-up, also cancels its PE
        if cint(live.docstatus) == 1:
            c2b_doc.cancel()  # raises if its Payment Entry cannot be cancelled
        else:
            frappe.db.savepoint("before_c2b_neutralize")
            try:
                c2b_doc.delete()
            except frappe.LinkExistsError:
                # Mpesa Match Log rows link it. Left as a draft: its on_submit refuses a
                # receipt an Express Request carries, so it can never be booked.
                frappe.db.rollback(save_point="before_c2b_neutralize")
                return

        frappe.log_error(
            message=f"Neutralised duplicate C2B {c2b_doc.name} in favour of Express Request {self.name}",
            title="Mpesa Express vs C2B Duplicate",
        )

    @frappe.whitelist()
    def reconcile_payment(self):
        # Manual "Reconcile Payment" (any HTTP route: run_doc_method or /api/v2) books
        # money: managers only. Callbacks call book_completed_payment() directly.
        require(ADMIN_ROLES)
        self.check_permission("write")
        self.book_completed_payment()

    def book_completed_payment(self):
        self.reload()
        if self.status != "Completed" or not self.transaction_id:
            frappe.throw(
                "Only a Completed request with an M-Pesa receipt (Transaction ID) can be reconciled."
            )
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, self.settings)

        if not self.is_reconciled:
            from ....money_in.common import receipt_reversal

            # locking read: waits for a reversal being sent right now (see stk_verify.book)
            reversal = receipt_reversal(self.transaction_id, for_update=True)
            if reversal:
                frappe.throw(
                    f"M-Pesa receipt {self.transaction_id} has reversal {reversal.name} ({reversal.status}): "
                    "its money is, or may be, back with the payer, so it is not booked."
                )
            register = frappe.db.get_value(
                "Mpesa C2B Payment Register", {"transid": self.transaction_id},
                ["name", "transamount", "docstatus"], as_dict=True, order_by="docstatus asc",
            )
            # The prompt books its own amount. A receipt it was only given from C2B (no
            # callback: sync_statuses, orphan adoption) can be for another amount, and
            # replacing that register's booking would drop or invent the difference.
            if register and abs(flt(register.transamount) - flt(self.amount)) > 0.005:
                frappe.throw(
                    f"M-Pesa receipt {self.transaction_id} was for KES {flt(register.transamount)} "
                    f"(C2B register {register.name}), but this request is for KES {flt(self.amount)}. "
                    "Not booked from the request and the register is left as it is; reconcile it by hand."
                )
            # A live C2B register for it goes first (stk_verify does this too), or its Payment
            # Entry, which carries the receipt, reads as booked.
            if register and register.docstatus < 2:
                self.validate_duplicate_c2b_records()
            handle_successful_transaction(self, settings)


@frappe.whitelist()
def create_new_request(
    phone_number,
    payment_gateway,
    reference_type,
    reference_id,
    amount,
    currency="KES",
    redirect=None,
    title="",
    description="",
):
    # Was allow_guest: anyone could push STK prompts to any phone. Logged-in users only;
    # anonymous checkout goes through the token-protected pay page / API (Phase 3).
    frappe.has_permission("Mpesa Express Request", "create", throw=True)
    phone_number = clean_digits(phone_number)
    if (
        not phone_number
        or not payment_gateway
        or not reference_type
        or not reference_id
        or not amount
    ):
        frappe.throw("Missing fields")

    doc = frappe.get_doc(
        {
            "doctype": "Mpesa Express Request",
            "phone_number": phone_number,
            "payment_gateway": payment_gateway,
            "reference_doctype": reference_type,
            "reference_name": reference_id,
            "base_amount": float(amount),
            "currency": currency,
            "status": "In Progress",
            "transaction_title": title,
            "transaction_description": description,
            "redirect": redirect,
        }
    )
    doc.insert()
    doc.submit()

    return doc.name


def payment_request_owed(payment_request) -> float:
    """What a Payment Request still owes, in its own currency (grand total when unknown)."""
    owed = flt(payment_request.grand_total)
    if payment_request.docstatus != 1 or not frappe.get_meta("Payment Request").has_field(
        "outstanding_amount"
    ):
        return owed  # a draft has no outstanding yet; older ERPNext does not track it
    outstanding = flt(payment_request.outstanding_amount)
    if outstanding <= 0:
        frappe.throw(f"Payment Request {payment_request.name} is already paid.")
    # ERPNext keeps the outstanding in the party account currency when that differs
    if (payment_request.get("party_account_currency") or payment_request.currency) == payment_request.currency:
        owed = min(owed, outstanding)
    return owed


def prompt_left(payment_request, exclude: str | None = None) -> tuple:
    """(left, paid): what may still be asked of the customer for the Payment Request, in its
    currency (payment_request_owed less the prompts the customer paid that are not booked yet,
    ``exclude`` aside), and those prompts."""
    from ....money_in.payment_request import unbooked_prompts

    owed = payment_request_owed(payment_request)
    paid = unbooked_prompts(payment_request.name, exclude=exclude)
    return flt(owed - sum(flt(p.paid) for p in paid), 2), paid


def clean_digits(v):
    if not v:
        return ""
    return "".join(filter(str.isdigit, str(v)))


@frappe.whitelist()
def get_request_status(name):
    # Was allow_guest with a 30 s server-side polling loop (a free DoS and a phone-number
    # leak by sequential name). Now a permission-checked, non-blocking read; clients poll.
    try:
        doc = frappe.get_doc("Mpesa Express Request", name)
    except DoesNotExistError:
        frappe.throw(f"Mpesa Express Request {name} not found.")
    doc.check_permission("read")

    return {
        "status": doc.status,
        "amount": doc.amount,
        "phone_number": doc.phone_number,
    }


@frappe.whitelist()
def get_stkpush_defaults():
    frappe.has_permission("Mpesa Express Request", "create", throw=True)
    gateways = frappe.get_all(
        "Payment Gateway",
        filters={"gateway_settings": "Mpesa Settings"},
        fields=["name", "gateway_controller"],
        ignore_permissions=True,
    )

    reference_map = {}
    for g in gateways:
        settings_name = g.gateway_controller
        if settings_name:
            settings = frappe.get_doc(
                "Mpesa Settings", settings_name, ignore_permissions=True
            )
            reference_map[g.name] = [
                d.target_doctype for d in settings.reconciliation_order
            ]
        else:
            reference_map[g.name] = []

    return {"gateways": gateways, "reference_map": reference_map}


@frappe.whitelist()
def retry_stkpush(name):
    doc = frappe.get_doc("Mpesa Express Request", name)
    doc.check_permission("write")
    if doc.status == "Completed":
        frappe.throw("Cannot retry a completed STK Push request.")

    doc.status = "In Progress"
    doc.save()
    doc.initiate_request()
