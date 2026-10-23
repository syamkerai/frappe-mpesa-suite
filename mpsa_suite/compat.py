"""Small shims so the app runs unchanged on Frappe/ERPNext v15 and v16.

Rules we follow everywhere (see docs/v15_v16_compat.md in the project root):
- always pass an explicit ``order_by`` (v16 default sort changed modified -> creation)
- read Single values through ``get_setting`` (v16 returns typed values, v15 strings)
- never ``frappe.db.commit()`` inside doc_events (forbidden in v16)
"""

import frappe
from frappe.utils import cint, cstr, flt

FRAPPE_MAJOR = cint(str(getattr(frappe, "__version__", "15")).split(".")[0])
IS_V16_PLUS = FRAPPE_MAJOR >= 16


def get_cache():
    """``frappe.cache()`` on v15, ``frappe.cache`` object on v16."""
    cache = frappe.cache
    if hasattr(cache, "get_value"):
        return cache
    return cache()


def in_test() -> bool:
    return bool(getattr(frappe, "in_test", False) or getattr(frappe.flags, "in_test", False))


def get_setting(doctype: str, field: str, kind: str = "str"):
    """Typed read of a Single DocType value, identical on v15 and v16."""
    value = frappe.db.get_single_value(doctype, field)
    if kind == "int":
        return cint(value)
    if kind == "float":
        return flt(value)
    if kind == "bool":
        return bool(cint(value))
    return cstr(value)


def __getattr__(name):
    """Lazy ``IntegrationTestCase`` so production imports never load the test framework."""
    if name == "IntegrationTestCase":
        try:  # v16 (and late v15) expose the new test classes
            from frappe.tests import IntegrationTestCase
        except ImportError:  # v15 fallback
            from frappe.tests.utils import FrappeTestCase as IntegrationTestCase
        return IntegrationTestCase
    raise AttributeError(name)
