# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ....money_out import known_names


class MPesaKnownName(Document):
    """The name M-Pesa gave a destination when it paid it (money_out.known_names). Written by the
    system only: no role creates or changes a record, and a save that is not the system's is refused."""

    def validate(self):
        known_names.check_system_write(self)
