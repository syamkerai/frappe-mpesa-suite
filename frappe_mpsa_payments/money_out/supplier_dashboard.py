def get_data(data=None):
    """Add the supplier's M-Pesa accounts and B2B payments to the Supplier form's Connections."""
    data = data or {}
    data.setdefault("fieldname", "supplier")
    data.setdefault("transactions", []).append(
        {"label": "M-Pesa", "items": ["Supplier M-Pesa Account", "Mpesa B2B Payment Request"]}
    )
    return data
