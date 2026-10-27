def get_data():
    """Mpesa Payment Intent Connections: the prompts sent for it and its webhook deliveries."""
    return {
        "fieldname": "intent",
        "non_standard_fieldnames": {"Mpesa Express Request": "reference_name"},
        "dynamic_links": {"reference_name": ["Mpesa Payment Intent", "reference_doctype"]},
        "transactions": [{"label": "M-Pesa", "items": ["Mpesa Express Request", "Mpesa Webhook Delivery"]}],
    }
