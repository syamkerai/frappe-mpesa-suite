from .connections import B2B, B2C, B2C_REQUEST, SCHEDULE, add_group

# ERPNext's own dashboards link by "purchase_invoice" / "purchase_order", the names of the B2B
# request's link fields; Frappe does not say which of the two DocTypes it asks for.
DOCTYPES = {"purchase_invoice": "Purchase Invoice", "purchase_order": "Purchase Order"}


def get_data(data=None):
    """Add M-Pesa B2B requests, B2C payouts (their rows) and payment schedule lines to the Purchase
    Invoice and Purchase Order Connections."""
    data = data or {}
    fieldname = data.get("fieldname")
    doctype = DOCTYPES.get(fieldname)
    links = {B2B: fieldname} if doctype else {}
    links.update({B2C_REQUEST: "reference_name", B2C: "reference_name", SCHEDULE: "reference_name"})
    return add_group(doctype, data, links=links, dynamic={"reference_name": "reference_doctype"})
