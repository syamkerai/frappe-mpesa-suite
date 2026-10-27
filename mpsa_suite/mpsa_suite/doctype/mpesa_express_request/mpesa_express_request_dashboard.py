def get_data():
    """Mpesa Express Request Connections: how its receipt was matched, the pay link it prompted for
    and its statement line (Integration Request comes from the DocType's own links)."""
    return {
        "fieldname": "express_request",
        "non_standard_fieldnames": {"Bank Transaction": "name"},
        "internal_links": {"Bank Transaction": "bank_transaction"},
        "transactions": [
            {"label": "M-Pesa", "items": ["Mpesa Match Log", "Mpesa Payment Intent"]},
            {"label": "Accounting", "items": ["Bank Transaction"]},
        ],
    }
