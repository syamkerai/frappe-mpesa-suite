from .connections import B2B, B2C, B2C_REQUEST, SCHEDULE, SUPPLIER_ACCOUNT, add_group


def get_data(data=None):
    """Add the supplier's M-Pesa accounts, its B2B and B2C payouts and its payment schedule lines to
    the Supplier form's Connections (ERPNext's dynamic link ``party -> party_type`` narrows the B2C
    ones to suppliers)."""
    return add_group(
        "Supplier", data,
        links={SUPPLIER_ACCOUNT: "supplier", B2B: "supplier", B2C_REQUEST: "party", B2C: "party", SCHEDULE: "party"},
        dynamic={"party": "party_type"}, fieldname="supplier",
    )
