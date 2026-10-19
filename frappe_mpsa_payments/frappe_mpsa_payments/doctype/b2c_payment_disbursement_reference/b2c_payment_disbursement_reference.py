# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class B2CPaymentDisbursementReference(Document):
    def validate(self) -> None:
        """Validation Hook"""
        parent_doc = getattr(self, "parent_doc", None)
        payment_type = parent_doc.payment_type

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

        if self.outstanding_amount and (
            self.allocated_amount > self.outstanding_amount
            or not self.outstanding_amount
        ):
            frappe.throw(
                "Allocated Amount cannot be greater than Outstanding Amount",
                frappe.ValidationError,
                title="Validation Error",
            )

        if payment_type != "Stanbic PesaLink" and self.partyb:
            mobile_no = sanitise_phone_number(self.partyb)

            if not is_valid_receiver_contact(mobile_no):
                frappe.throw(
                    f"Row #{self.idx}: Incorrect Receiver's Mobile Number: {self.partyb}",
                    frappe.ValidationError,
                    title="Incorrect Contact",
                )

            self.partyb = mobile_no

        if payment_type == "Stanbic PesaLink":
            if not self.bank_code:
                frappe.throw(
                    f"Row #{self.idx}: Bank Code is Required",
                    frappe.ValidationError,
                    title="Validation Error",
                )


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
