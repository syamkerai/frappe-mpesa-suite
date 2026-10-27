def get_data():
    """Mpesa Settings Connections: what each shortcode sent, received and reconciled."""
    return {
        "fieldname": "mpesa_settings",
        "non_standard_fieldnames": {"Mpesa Express Request": "settings"},
        "transactions": [
            {
                "label": "Money Out",
                "items": [
                    "B2C Disbursement Request",
                    "Mpesa B2B Payment Request",
                    "Mpesa Reversal Request",
                    "Mpesa B2C Status Query",
                ],
            },
            {"label": "Money In", "items": ["Mpesa Express Request", "Mpesa C2B Payment Register URL", "Mpesa API Client"]},
            {"label": "Statements", "items": ["Mpesa Balance Snapshot", "Mpesa Statement Import", "Mpesa Statement Row"]},
        ],
    }
