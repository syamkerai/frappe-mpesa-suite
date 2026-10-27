"""A FakeDB (fake_frappe) that also understands what the balance, charge and status code asks:
filters as a dict or as [doctype, field, op, value] lists, the comparison operators, ``order_by``,
``limit``, ``fields`` and ``pluck``. The LIKE operator matches every row, as in fake_frappe."""

import datetime

import fake_frappe


def _cond(value, op, arg) -> bool:
    if op in ("=", "=="):
        return value == arg
    if op == "!=":
        return value != arg
    if op == "in":
        return value in arg
    if op == "not in":
        return value not in arg
    if op == "is":
        return (value not in (None, "")) if arg == "set" else (value in (None, ""))
    if op == "like":
        return True
    if value in (None, ""):
        return False
    if op == "between":
        return arg[0] <= value <= arg[1]
    return {"<": lambda: value < arg, ">": lambda: value > arg, "<=": lambda: value <= arg,
            ">=": lambda: value >= arg}[op]()


def _terms(filters):
    if not filters:
        return []
    if isinstance(filters, dict):
        out = []
        for key, cond in filters.items():
            if isinstance(cond, (list, tuple)):
                out.append((key, cond[0], cond[1]))
            else:
                out.append((key, "=", cond))
        return out
    return [(f[1], f[2], f[3]) if len(f) == 4 else (f[0], f[1], f[2]) for f in filters]


def _sort_key(value):
    if value is None:
        return (0, "")
    if isinstance(value, (datetime.datetime, datetime.date)):
        return (1, value.isoformat())
    if isinstance(value, (int, float)):
        return (1, f"{value:020.6f}")
    return (1, str(value))


class RichDB(fake_frappe.FakeDB):
    def __init__(self, frappe):
        super().__init__(frappe)
        self.db_type = "fake"

    def _rows(self, doctype, filters):
        rows = self.table(doctype)
        if isinstance(filters, str):
            return [rows[filters]] if filters in rows else []
        terms = _terms(filters)
        return [r for r in rows.values() if all(_cond(r.get(k), op, arg) for k, op, arg in terms)]

    @staticmethod
    def _order(rows, order_by):
        for term in reversed([t.split() for t in (order_by or "").split(",") if t.strip()]):
            field, desc = term[0], len(term) > 1 and term[1].lower() == "desc"
            rows = sorted(rows, key=lambda r: _sort_key(r.get(field)), reverse=desc)
        return rows

    def get_value(self, doctype, filters, fieldname="name", as_dict=False, for_update=False, order_by=None, **kw):
        rows = self._order(self._rows(doctype, filters), order_by)
        if not rows:
            return None
        row = rows[0]
        if for_update:
            self.locks.append((doctype, row["name"]))
        if isinstance(fieldname, (list, tuple)):
            values = {f: row.get(f) for f in fieldname}
            return fake_frappe._dict(values) if as_dict else tuple(values.values())
        return row.get(fieldname)

    def get_all(self, doctype, filters=None, pluck=None, fields=None, order_by=None, limit=None, or_filters=None,
                **kw):
        rows = self._order(self._rows(doctype, filters), order_by)
        if or_filters:
            terms = _terms(or_filters)
            rows = [r for r in rows if any(_cond(r.get(k), op, arg) for k, op, arg in terms)]
        if limit:
            rows = rows[:limit]
        return [r.get(pluck) for r in rows] if pluck else [fake_frappe._dict(r) for r in rows]

    def exists(self, doctype, filters=None):
        return bool(self._rows(doctype, filters))


def install(frappe):
    """Swap ``frappe.db`` for a RichDB, keeping the tables, and point frappe.get_all at it."""
    db = RichDB(frappe)
    db.tables = frappe.db.tables
    frappe.db = db
    frappe.get_all = lambda doctype, *a, **kw: db.get_all(doctype, *a, **kw)
    frappe.get_list = frappe.get_all
    return db
