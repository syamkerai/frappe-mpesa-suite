"""A tiny in-memory stand-in for the parts of ``frappe`` that money_out uses.

Lets the Reversal / B2B / Transaction Status state machines run under plain pytest
(no bench). Only the behaviour those modules rely on is modelled.
"""

import datetime
import itertools
import json
import sys
import types
from decimal import Decimal


class ValidationError(Exception):
    pass


class PermissionError_(Exception):
    pass


class DoesNotExistError(Exception):
    pass


class QueryDeadlockError(Exception):
    pass


class QueryTimeoutError(Exception):
    pass


class _dict(dict):
    def __getattr__(self, key):
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


def _match(row, filters):
    for key, cond in (filters or {}).items():
        value = row.get(key)
        if isinstance(cond, (list, tuple)):
            op, arg = cond[0], cond[1]
            if op == "!=" and value == arg:
                return False
            if op == "in" and value not in arg:
                return False
            if op == "<" and not (value is not None and value < arg):
                return False
            if op == ">" and not (value is not None and value > arg):
                return False
            if op == "is" and arg == "set" and value in (None, ""):
                return False
            if op == "is" and arg == "not set" and value not in (None, ""):
                return False
        elif value != cond:
            return False
    return True


class FakeDB:
    def __init__(self, frappe):
        self.frappe = frappe
        self.tables = {}
        self.locks = []

    def table(self, doctype):
        return self.tables.setdefault(doctype, {})

    # columns a test declares present, {(doctype, column)}; others count as missing
    columns = set()

    def has_column(self, doctype, column):
        return (doctype, column) in self.columns

    def _rows(self, doctype, filters):
        rows = self.table(doctype)
        if isinstance(filters, str):
            return [rows[filters]] if filters in rows else []
        return [r for r in rows.values() if _match(r, filters)]

    def get_value(self, doctype, filters, fieldname="name", as_dict=False, for_update=False, order_by=None, **kw):
        rows = self._rows(doctype, filters)
        if not rows:
            return None
        row = rows[0]
        if for_update:
            self.locks.append((doctype, row["name"]))
        if isinstance(fieldname, (list, tuple)):
            values = {f: row.get(f) for f in fieldname}
            return _dict(values) if as_dict else tuple(values.values())
        return row.get(fieldname)

    def set_value(self, doctype, name, field, value=None, **kw):
        values = field if isinstance(field, dict) else {field: value}
        self.table(doctype)[name].update(values)

    def exists(self, doctype, filters):
        return bool(self._rows(doctype, filters))

    def get_all(self, doctype, filters=None, pluck=None, fields=None, order_by=None, **kw):
        rows = self._rows(doctype, filters or {})
        return [r.get(pluck) for r in rows] if pluck else [_dict(r) for r in rows]

    def get_single_value(self, doctype, field, cache=False):
        return self.table(doctype).get(doctype, {}).get(field)

    def savepoint(self, name):
        pass

    def rollback(self, save_point=None):
        pass


class FakeDoc:
    def __init__(self, frappe, doctype, row):
        object.__setattr__(self, "_frappe", frappe)
        object.__setattr__(self, "doctype", doctype)
        object.__setattr__(self, "_row", row)

    def __getattr__(self, key):
        return self._row.get(key)

    def __setattr__(self, key, value):
        self._row[key] = value

    def get(self, key, default=None):
        return self._row.get(key, default)

    def db_set(self, field, value=None, notify=False, **kw):
        values = field if isinstance(field, dict) else {field: value}
        self._row.update(values)
        self._frappe.db.table(self.doctype)[self._row["name"]].update(values)

    def add_comment(self, kind, text):
        self._frappe.comments.append((self.doctype, self._row["name"], text))

    def check_permission(self, ptype="read"):
        return True

    def insert(self, ignore_permissions=False):
        if not self._row.get("name"):
            self._row["name"] = f"{self.doctype[:4].upper()}-{next(self._frappe._seq)}"
        self._row.setdefault("creation", self._frappe.utils.now_datetime())
        self._frappe.db.table(self.doctype)[self._row["name"]] = self._row
        return self


def build():
    frappe = types.ModuleType("frappe")
    frappe.ValidationError = ValidationError
    frappe.PermissionError = PermissionError_
    frappe.DoesNotExistError = DoesNotExistError
    frappe.QueryDeadlockError = QueryDeadlockError
    frappe.QueryTimeoutError = QueryTimeoutError
    frappe._dict = _dict
    frappe._seq = itertools.count(1)
    frappe.db = FakeDB(frappe)
    frappe.comments, frappe.errors, frappe.enqueued, frappe.request_logs = [], [], [], []
    frappe.session = _dict(user="payer@example.com")
    frappe.user_roles = {"M-Pesa Payer"}
    frappe.local = types.SimpleNamespace(request=None, form_dict=_dict(), session=frappe.session)
    frappe.__version__ = "15.0.0"
    frappe.flags = _dict()
    frappe.conf = _dict()

    def throw(msg, exc=ValidationError, *a, **kw):
        raise exc(msg) if isinstance(exc, type) else ValidationError(msg)

    def only_for(roles, message=False):
        if not set(roles) & frappe.user_roles:
            raise PermissionError_(f"needs one of {roles}")

    def get_doc(doctype, name=None, **kw):
        if isinstance(doctype, dict):
            row = dict(doctype)
            return FakeDoc(frappe, row.pop("doctype"), row)
        row = frappe.db.table(doctype).get(name)
        if row is None:
            raise DoesNotExistError(f"{doctype} {name}")
        return FakeDoc(frappe, doctype, row)

    def get_cached_doc(doctype, *a, **kw):
        raise DoesNotExistError(doctype)

    def whitelist(*a, **kw):
        if a and callable(a[0]):
            return a[0]
        return lambda fn: fn

    def set_user(user):
        frappe.session.user = user

    frappe.throw = throw
    frappe.only_for = only_for
    frappe.get_doc = get_doc
    frappe.get_cached_doc = get_cached_doc
    frappe.whitelist = whitelist
    frappe.set_user = set_user
    frappe.log_error = lambda *a, **kw: frappe.errors.append((a, kw))
    frappe.get_traceback = lambda *a, **kw: "traceback"
    frappe.as_json = lambda obj, *a, **kw: json.dumps(obj, default=str)
    frappe.parse_json = json.loads
    frappe.enqueue = lambda method, **kw: frappe.enqueued.append((method, kw))
    frappe.publish_realtime = lambda *a, **kw: None
    frappe.get_installed_apps = lambda: ["frappe", "erpnext"]
    frappe.messages = []
    frappe.msgprint = lambda msg, *a, **kw: frappe.messages.append(msg)
    frappe.get_all = lambda doctype, *a, **kw: frappe.db.get_all(doctype, *a, **kw)
    frappe.get_list = frappe.get_all

    utils = types.ModuleType("frappe.utils")

    def flt(value, precision=None):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def cint(value):
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    utils.flt, utils.cint = flt, cint
    utils.cstr = lambda v: "" if v is None else str(v)
    utils.getdate = lambda v=None: (v.date() if isinstance(v, datetime.datetime) else v) or datetime.date.today()
    utils.nowdate = lambda: datetime.date.today().isoformat()
    utils.now_datetime = lambda: datetime.datetime(2026, 10, 18, 12, 0, 0)
    utils.add_to_date = lambda dt, minutes=0, hours=0, **kw: dt + datetime.timedelta(minutes=minutes, hours=hours)
    utils.add_days = lambda date, days: (datetime.date.fromisoformat(str(date)[:10]) + datetime.timedelta(days=days))
    utils.get_url = lambda *a, **kw: "https://erp.example.com"
    utils.get_datetime = lambda v: v
    frappe.utils = utils

    integrations = types.ModuleType("frappe.integrations")
    integrations_utils = types.ModuleType("frappe.integrations.utils")

    def create_request_log(**kw):
        name = f"IR-{next(frappe._seq)}"
        frappe.request_logs.append(dict(kw, name=name))
        frappe.db.table("Integration Request")[name] = dict(kw, name=name)
        return _dict(name=name)

    integrations_utils.create_request_log = create_request_log
    integrations.utils = integrations_utils

    model = types.ModuleType("frappe.model")
    document = types.ModuleType("frappe.model.document")
    document.Document = type("Document", (), {})
    model.document = document

    frappe._ = lambda s: s
    return {
        "frappe": frappe,
        "frappe.utils": utils,
        "frappe.integrations": integrations,
        "frappe.integrations.utils": integrations_utils,
        "frappe.model": model,
        "frappe.model.document": document,
    }


def install():
    modules = build()
    saved = {k: sys.modules.get(k) for k in modules}
    sys.modules.update(modules)
    return modules["frappe"], saved


def uninstall(saved):
    for key, module in saved.items():
        if module is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = module
    for key in [k for k in sys.modules if k.startswith("mpsa_suite.") and "daraja.guard" not in k
                and "daraja.money_out" not in k and "utils.phone" not in k and "gateway.signing" not in k]:
        sys.modules.pop(key, None)


__all__ = ["install", "uninstall", "Decimal"]
