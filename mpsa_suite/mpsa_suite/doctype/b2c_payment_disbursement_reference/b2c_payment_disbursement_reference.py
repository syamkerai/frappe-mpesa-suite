# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from ....daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT


class B2CPaymentDisbursementReference(Document):
    def validate(self) -> None:
        """Validation Hook"""
        if not self.party:
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
            mobile_no = sanitise_phone_number(self.partyb)

            if not is_valid_receiver_contact(mobile_no):
                frappe.throw(
                    f"Row #{self.idx}: Incorrect Receiver's Mobile Number: {self.partyb}",
                    frappe.ValidationError,
                    title="Incorrect Contact",
                )

            self.partyb = mobile_no

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
    """Sanitises a given phone_number string"""
    phone_number = phone_number.replace("+", "").replace(" ", "")

    regex = re.compile(r"^0\d{9}$")
    if not regex.match(phone_number):
        return phone_number

    phone_number = "254" + phone_number[1:]
    return phone_number


def is_valid_receiver_contact(receiver: str) -> bool:
    """True for a Kenyan mobile number: 2547XXXXXXXX, 25410/25411XXXXXXX, or the 07/01, +254 forms."""
    return bool(re.fullmatch(r"254(7\d{8}|1[01]\d{7})", sanitise_phone_number(str(receiver or "").strip())))
