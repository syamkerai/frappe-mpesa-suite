def get_data(data=None):
    """Add M-Pesa B2B payments to the Purchase Invoice and Purchase Order Connections.

    ERPNext's own dashboards link by "purchase_invoice" / "purchase_order", the names of the
    request's link fields, so no fieldname is set here.
    """
    data = data or {}
    data.setdefault("transactions", []).append({"label": "M-Pesa", "items": ["Mpesa B2B Payment Request"]})
    return data
