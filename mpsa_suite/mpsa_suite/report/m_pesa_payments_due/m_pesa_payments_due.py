# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

"""What M-Pesa Payment Schedules have due, and what each shortcode can pay (Working for B2B
lines, Utility for B2C phone lines).

Ticked lines are paid with Pay Selected (money_out.schedule.prepare_payment, then
confirm_payment after the 5-second confirm step), or moved to another day with Move Date.
The Problem column says why a line cannot be paid by the person looking at it now.
"""

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, nowdate

from mpsa_suite.money_out import owed
from mpsa_suite.money_out import schedule as s
from mpsa_suite.wallet import balances

DUE, WEEK, IN_PROGRESS, ALL = "Due today and overdue", "Next 7 days", "In progress", "All"
LIMIT = 500


def execute(filters=None):
    filters = frappe._dict(filters or {})
    frappe.has_permission(s.SCHEDULE, "read", throw=True)
    rows = _rows(filters)
    data = [_row(r) for r in rows]
    return columns(), data, None, None, _summary(filters, data)


def columns() -> list:
    return [
        {"fieldname": "planned_date", "label": "Pay On", "fieldtype": "Date", "width": 125},
        {"fieldname": "schedule", "label": "Schedule", "fieldtype": "Link", "options": s.SCHEDULE, "width": 150},
        {"fieldname": "payee", "label": "Payee", "fieldtype": "Data", "width": 180},
        {"fieldname": "route", "label": "Route", "fieldtype": "Data", "width": 100},
        {"fieldname": "destination", "label": "Pays To", "fieldtype": "Data", "width": 140},
        {"fieldname": "reference_doctype", "label": "Document Type", "fieldtype": "Data", "hidden": 1},
        {"fieldname": "reference_name", "label": "Document", "fieldtype": "Dynamic Link",
         "options": "reference_doctype", "width": 150},
        {"fieldname": "left_to_pay", "label": "Document Left to Pay", "fieldtype": "Currency", "options": "currency",
         "width": 120},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "options": "currency", "width": 110},
        # M-Pesa pays KES only; without this the amounts show in the site's default currency
        {"fieldname": "currency", "label": "Currency", "fieldtype": "Link", "options": "Currency", "hidden": 1},
        {"fieldname": "status", "label": "Status", "fieldtype": "Data", "width": 90},
        {"fieldname": "problem", "label": "Problem", "fieldtype": "Data", "width": 320},
        {"fieldname": "prepared_by", "label": "Prepared By", "fieldtype": "Link", "options": "User", "width": 150},
        {"fieldname": "request_doctype", "label": "Payout Type", "fieldtype": "Data", "hidden": 1},
        {"fieldname": "request", "label": "Payout", "fieldtype": "Dynamic Link", "options": "request_doctype",
         "width": 160},
    ]


def _rows(filters) -> list:
    today = getdate(nowdate())
    show = filters.get("show") or DUE
    where, values = ["l.parenttype = %(parenttype)s"], {"parenttype": s.SCHEDULE, "today": today}
    if show == DUE:
        where.append("l.status = 'Planned' AND l.planned_date <= %(today)s")
    elif show == WEEK:
        where.append("l.status = 'Planned' AND l.planned_date <= %(week)s")
        values["week"] = add_days(today, 7)
    elif show == IN_PROGRESS:
        where.append("l.status IN %(in_progress)s")
        values["in_progress"] = s.IN_PROGRESS
    for field, column in (("company", "p.company"), ("route", "l.route"), ("schedule", "l.parent")):
        if filters.get(field):
            where.append(f"{column} = %({field})s")
            values[field] = filters.get(field)
    if filters.get("pay_from"):
        # a phone line is paid from Pay From (B2C), the others from Pay From (B2B)
        where.append("((l.route IN %(b2c_routes)s AND p.b2c_settings = %(pay_from)s)"
                     " OR (l.route NOT IN %(b2c_routes)s AND p.b2b_settings = %(pay_from)s))")
        values.update(pay_from=filters.get("pay_from"), b2c_routes=s.B2C_ROUTES)
    fields = ", ".join(f"l.`{f}`" for f in s.LINE_FIELDS)
    return frappe.db.sql(
        f"""
        SELECT {fields}, p.company, p.b2b_settings, p.b2c_settings, p.title, p.closed
        FROM `tab{s.LINE}` l
        JOIN `tab{s.SCHEDULE}` p ON p.name = l.parent
        WHERE {" AND ".join(where)}
        ORDER BY l.planned_date, l.parent, l.idx
        LIMIT {LIMIT}
        """,
        values,
        as_dict=True,
    )


def _row(line) -> dict:
    header = frappe._dict(name=line.parent, title=line.title, company=line.company, b2b_settings=line.b2b_settings,
                          b2c_settings=line.b2c_settings, closed=line.closed)
    today = getdate(nowdate())
    problem, kind, left = cstr(line.last_problem), "note", None
    if line.status == s.PLANNED and not s._sent(line):
        check = s.line_problem(line, header)
        left = check.left
        if check.problem:
            problem, kind = check.problem, "problem"
        else:
            problem, kind = " ".join(check.warnings), "warning" if check.warnings else "ok"
        if left is None and line.reference_name:
            left = owed.left_to_pay(*s._document(line), exclude_line=line.name).left
    return {
        "line": line.name,
        "row": line.idx,
        "planned_date": line.planned_date,
        "overdue": 1 if line.status == s.PLANNED and getdate(line.planned_date) < today else 0,
        "schedule": line.parent,
        "payee": s._payee(line),
        "party": line.party,
        "route": line.route,
        "destination": line.destination,
        "reference_doctype": line.reference_doctype,
        "reference_name": line.reference_name,
        "left_to_pay": left,
        "amount": flt(line.amount, 2),
        "currency": "KES",
        "status": line.status,
        "problem": problem,
        "problem_kind": kind,
        "payable": 1 if kind in ("ok", "warning") else 0,
        "prepared_by": line.prepared_by,
        "request_doctype": s.B2B if line.request else (s.B2C_REQUEST if line.b2c_request else
                                                       (s.B2C_DOCTYPE if line.b2c_disbursement else None)),
        "request": line.request or line.b2c_request or line.b2c_disbursement,
        "pay_from": s.pay_from(header, line),
    }


def _summary(filters, data) -> list:
    """A card per shortcode's sub-account (approximate balance, with its age), what is due now,
    and the selected total (the report's script keeps it up to date)."""
    today = getdate(nowdate())
    due = [d for d in data if d["status"] == s.PLANNED and getdate(d["planned_date"]) <= today]
    cards = []
    pay_from = {d["pay_from"] for d in data if d["pay_from"]}
    if filters.pay_from:
        pay_from.add(filters.pay_from)
    for settings in sorted(pay_from):
        subs = sorted({s.SUB_ACCOUNT.get(d["route"]) for d in data if d["pay_from"] == settings and d["route"]}
                      - {None}) or [s.SUB_ACCOUNT[s.B2B_PAYBILL]]
        for sub in subs:
            b = balances.available(settings, sub)
            due_here = flt(sum(d["amount"] for d in due if d["pay_from"] == settings
                               and s.SUB_ACCOUNT.get(d["route"]) == sub), 2)
            age = s._age(b.as_of)
            if b.amount is None:
                note = "no balance yet: press Refresh Balances"
            else:
                note = f"approx., {age} old" if age else "approx."
            cards.append({"value": b.after if b.after is not None else 0, "label": f"{sub} {settings} ({note})",
                          "datatype": "Currency", "currency": "KES",
                          "indicator": "Grey" if b.amount is None else ("Red" if flt(b.after) < due_here else "Green")})
    cards.append({"value": flt(sum(d["amount"] for d in due), 2), "label": f"Due Now ({len(due)} lines)",
                  "datatype": "Currency", "currency": "KES", "indicator": "Orange" if due else "Green"})
    payable = [d for d in due if cint(d["payable"])]
    cards.append({"value": flt(sum(d["amount"] for d in payable), 2),
                  "label": f"You Can Pay Now ({len(payable)} lines)", "datatype": "Currency", "currency": "KES",
                  "indicator": "Blue"})
    cards.append({"value": 0, "label": "Selected", "datatype": "Currency", "currency": "KES", "indicator": "Blue"})
    return cards
