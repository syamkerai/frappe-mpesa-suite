# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

ON_ITS_OWN = "Change payment schedule lines on their schedule."


class MPesaPaymentScheduleLine(Document):
    """One planned payment. Its checks run in money_out.schedule.validate_schedule when the
    schedule is saved. Frappe runs a row's own validate and on_trash only when the row is saved
    or deleted by itself (the REST API, frappe.client.save), which would skip those checks, so
    that is refused. Saving or deleting the schedule never calls them."""

    def validate(self):
        frappe.throw(ON_ITS_OWN)

    def on_trash(self):
        frappe.throw(ON_ITS_OWN)
