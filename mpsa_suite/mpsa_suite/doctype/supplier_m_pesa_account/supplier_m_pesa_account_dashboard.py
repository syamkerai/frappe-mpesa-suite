def get_data():
    """Supplier M-Pesa Account Connections: the B2B payments and payment schedule lines it is paid by."""
    return {
        "fieldname": "supplier_mpesa_account",
        "non_standard_fieldnames": {"M-Pesa Payment Schedule": "payee_account"},
        "dynamic_links": {"payee_account": ["Supplier M-Pesa Account", "payee_account_doctype"]},
        "transactions": [{"label": "M-Pesa", "items": ["Mpesa B2B Payment Request", "M-Pesa Payment Schedule"]}],
    }
