# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Remove the suite's ``search_index`` Property Setters from text fields.

Bank Transaction's Reference Number is Small Text. Frappe refuses ``search_index`` on text
fields, so with the setter in place every Custom Field or Customize Form save on Bank
Transaction failed ("Fieldtype Small Text for Reference Number cannot be indexed"). The
setter was never needed there: MariaDB built a prefix index for the column, and schema sync
never adds or drops indexes on text columns, so the index (and the duplicate check's
index-backed locking read) stays.
"""

import frappe

from mpsa_suite.handover import ran_under_old_name

# what install.ensure_lookup_indexes gave a search_index Property Setter
LOOKUP_FIELDS = (
    ("Bank Transaction", "reference_number"),
    ("Bank Transaction", "transaction_id"),
    ("Payment Entry", "reference_no"),
    ("Integration Request", "request_id"),
)
# fieldtypes Frappe will not index (core DocType check_unique_and_text, v15 and v16)
UNINDEXABLE = ("Text", "Long Text", "Small Text", "Code", "Text Editor")


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    for doctype, fieldname in LOOKUP_FIELDS:
        if not frappe.db.exists("DocType", doctype):
            continue
        df = frappe.get_meta(doctype).get_field(fieldname)
        if not df or df.fieldtype not in UNINDEXABLE:
            continue
        filters = {"doc_type": doctype, "field_name": fieldname, "property": "search_index"}
        if frappe.db.exists("Property Setter", filters):
            frappe.db.delete("Property Setter", filters)
            frappe.clear_cache(doctype=doctype)
