# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from ....daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ....utils.phone import FORMATS, normalize_msisdn, payable_msisdn
from ....utils.privacy import shown


class B2CPaymentDisbursementReference(Document):
    def validate(self) -> None:
        """Validation Hook"""
        from ....money_out import partyless

        # a row of a DocType registered as party-less (a bulk payout sheet's row, a top-up) pays a
        # person no party names: its DocType checks it (money_out.partyless)
        handler = partyless.handler_for(getattr(self, "reference_doctype", None))
        if not self.party and not handler:
            frappe.throw(
                f"Row #{self.idx}: Party is mandatory",
                frappe.ValidationError,
                title="Validation Error",
            )

        if self.allocated_amount is not None and flt(self.allocated_amount) < 10:
            frappe.throw(
                "Allocated Amount cannot be less than Kshs. 10",
                frappe.ValidationError,
                title="Validation Error",
            )

        # The send job applies the same rule; refusing here says so before submit.
        if self.allocated_amount is not None and (problem := b2c_amount_problem(self.allocated_amount)):
            frappe.throw(
                f"Row #{self.idx}: {problem}",
                frappe.ValidationError,
                title="Validation Error",
            )

        if self.outstanding_amount and (
            self.allocated_amount > self.outstanding_amount
            or not self.outstanding_amount
        ):
            frappe.throw(
                "Allocated Amount cannot be greater than Outstanding Amount",
                frappe.ValidationError,
                title="Validation Error",
            )

        if self.partyb:
            mobile_no = sanitise_phone_number(str(self.partyb).strip())

            if not is_valid_receiver_contact(mobile_no):
                who = self.party or getattr(self, "reference_row", None)  # a party-less row: its paying row
                frappe.throw(
                    f"Row #{self.idx} ({who}): the Receiver's Mobile Number {shown(self.partyb)} is not "
                    f"a number M-Pesa B2C can pay. Enter {FORMATS}.",
                    frappe.ValidationError,
                    title="Incorrect Contact",
                )

            self.partyb = mobile_no

        if handler:
            partyless.check_row(self, getattr(self, "parent_doc", None))
            return
        if getattr(self, "reference_row", None):
            frappe.throw(
                f"Row #{self.idx}: only a payout of a document that pays people no party names (a bulk payout "
                f"sheet, a petty cash phone) names a Paying Row; {self.reference_doctype} is paid to its party.",
                frappe.ValidationError,
                title="Validation Error",
            )

        if getattr(self, "mpesa_schedule_line", None):
            # a payment schedule's phone line: the number is its employee's approved phone, the
            # amount and slip the line's (money_out.schedule)
            from ....money_out.schedule import check_b2c_row

            check_b2c_row(self)

        if getattr(self, "claim_payee", None):
            # a casual payee line of an Expense Claim: the line's phone and amount, approved with the
            # claim (money_out.claim_payees)
            from ....money_out import claim_payees

            claim_payees.check_b2c_row(getattr(self, "parent_doc", None), self)
        else:
            from ....money_out import employee_documents

            # a claim or advance that may be paid, of the row's employee; an employee only to an
            # approved phone (M-Pesa Suite Settings)
            employee_documents.check_b2c_row(self)


def b2c_amount_problem(amount) -> str | None:
    """Why Daraja B2C would refuse ``amount`` (whole shillings from 10 to 250,000), or None.

    Same arithmetic as ``validate_b2c_amount_and_party``, so a row that passes is not
    refused when its request is sent.
    """
    value = flt(amount)
    if value != int(value):
        return f"M-Pesa B2C pays whole shillings only; Allocated Amount is {amount}"
    if not B2C_MIN_AMOUNT <= value <= B2C_MAX_AMOUNT:
        return f"M-Pesa B2C pays Kshs. {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} per payout; Allocated Amount is {amount}"
    return None


def sanitise_phone_number(phone_number: str) -> str:
    """A Kenyan mobile number written with spaces, dashes, dots, brackets or a '+', or without its
    leading 0, as 2547... / 2541... (utils.phone); anything else (a letter in it) as typed, less its
    '+' and spaces."""
    phone_number = str(phone_number or "").strip().replace("+", "").replace(" ", "")
    if re.fullmatch(r"[\d\s.()\-]+", phone_number):
        return normalize_msisdn(phone_number) or phone_number
    return phone_number


def is_valid_receiver_contact(receiver: str) -> bool:
    """True for a Kenyan mobile number in a block in use (utils.phone.payable_msisdn: 07XX, 010X /
    011X, 014X, 018X), written 07... / 01..., 2547... / 2541..., +254, or without its leading 0."""
    number = sanitise_phone_number(str(receiver or "").strip())
    return number.isdigit() and payable_msisdn(number) == number
