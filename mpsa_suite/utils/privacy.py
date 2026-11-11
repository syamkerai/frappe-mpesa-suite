"""Phone numbers, account numbers and the names M-Pesa paid, as staff see them (round 4.3).

M-Pesa Suite Settings > Show Numbers and Names in Full (on by default): forms, the Send via M-Pesa
dialog, reports, alerts, comments, logs and the 80 mm prints show 254712345678, 0123456789 and
"254712345678 - JANE DOE" as they are. Off, a phone reads 2547****5678 and an account number
****6789, as before round 4.3. The external Payments API (gateway) always masks: its caller is a
system, not staff. The masking rules themselves stay in utils.phone (pure) and in each caller.
"""

import frappe
from frappe.utils import cint, cstr

from .phone import mask, normalize_msisdn, shown as masked_shown

SUITE = "M-Pesa Suite Settings"
FIELD = "show_full_details"


def full_details() -> bool:
    """Show numbers and names in full: on unless M-Pesa Suite Settings says off (a site not migrated
    yet, or never saved, shows them in full: the setting's default)."""
    try:
        value = frappe.db.get_single_value(SUITE, FIELD)
    except Exception:  # before the migrate that adds the field
        return True
    return value in (None, "") or bool(cint(value))


def phone(value) -> str:
    """A phone as staff see it: 254712345678 in full, else 2547****5678 ('' for nothing)."""
    value = cstr(value).strip()
    if not value:
        return ""
    if full_details():
        return normalize_msisdn(value) or value
    return mask(value)


def shown(value) -> str:
    """A number typed into a field, for a message about it: as typed in full, else masked
    (utils.phone.shown: a phone masked, a mistyped number by its last 3 digits and length)."""
    value = cstr(value).strip()
    if full_details():
        return value or repr(value)
    return masked_shown(value)


def account(number, keep: int = 4, longer_than: int = 6, stars: str = "****") -> str:
    """An account number as staff see it: in full, else its last ``keep`` characters behind
    ``stars`` when it is longer than ``longer_than`` (a short one is shown as it is)."""
    number = cstr(number).strip()
    if full_details() or len(number) <= longer_than:
        return number
    return f"{stars}{number[-keep:]}"
