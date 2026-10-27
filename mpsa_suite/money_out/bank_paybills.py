"""M-Pesa Bank Paybills: the paybill of each bank, microfinance bank or Sacco, which a payee's
Bank Account (via Bank Paybill) is paid through with its account number as the reference.

The suite ships the top Kenyan institutions in ``data/bank_paybills.json`` (no number is written in
Python). ``sync_shipped`` brings a site's rows in step with it after install and on every migrate
(not a patch: it must run every release): a missing row is inserted, or a row an admin made for the
same paybill is adopted as it is; a field of a shipped row is updated only while it still holds
what the previous release shipped, so an admin's edit is kept (with a Comment saying what the
release ships); a row the file marks ``retired`` is disabled unless an admin changed it. Shipped
rows cannot be deleted (disable them instead), so a release never brings back a row an admin
removed. The plan is ``plan_seed``, a pure function.

A row's paybill is copied onto each payee account when it is entered; approved accounts keep the
number they were approved with. When a row's paybill changes (an admin, or a release), the
approved accounts on it whose number is now different are blocked (``needs_reapproval``) until
they are cancelled and amended, so a manager approves the new number: money never follows an
edit by itself. Changing the paybill back unblocks them. Disabling a row only stops new accounts
from picking it.

Rows are public numbers, readable by the Accounts, HR and Purchase roles that enter payee
accounts; only CONFIG_ROLES change them (the DocPerms, and ``validate_row`` in case a site adds
rights).
"""

import json
import re
from pathlib import Path

import frappe
from frappe.utils import cint, cstr

from ..permissions import CONFIG_ROLES, require
from . import party_accounts as pa

BANK_PAYBILL = pa.BANK_PAYBILL
SHIPPED_FILE = Path(__file__).with_name("data") / "bank_paybills.json"
# What a release ships and may update (never ``bank``, a site's own ERPNext Bank record, nor what
# M-Pesa was seen to show)
SEED_FIELDS = ("institution_name", "institution_type", "paybill", "account_number_label", "account_number_rule",
               "account_number_pattern", "receiver_name", "enabled", "source", "verified_on")
INSERT, ADOPT, UPDATE, KEEP, RETIRE = "insert", "adopt", "update", "keep", "retire"


# ---------------------------------------------------------------- the row itself
def validate_row(doc) -> None:
    """On every save: CONFIG_ROLES only (a release's sync sets flags.mpsa_seed), a paybill that is
    5 to 7 digits, not one of this company's own numbers and not on another row, and a pattern
    that compiles."""
    if not (getattr(doc, "flags", None) or {}).get("mpsa_seed"):
        require(CONFIG_ROLES)
    doc.code = cstr(doc.code).strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,29}", doc.code):
        frappe.throw("Code is letters, digits and dashes only (at most 30), e.g. KCB")
    doc.paybill = cstr(doc.paybill).strip()
    if not doc.paybill.isdigit() or not 5 <= len(doc.paybill) <= 7:
        frappe.throw("Paybill must be 5 to 7 digits")
    pa.check_not_own(doc.paybill)
    other = frappe.db.get_value(BANK_PAYBILL, {"paybill": doc.paybill, "name": ["!=", doc.name or ""]}, "name")
    if other:
        frappe.throw(f"Paybill {doc.paybill} is already M-Pesa Bank Paybill {other}")
    doc.account_number_pattern = cstr(doc.account_number_pattern).strip() or None
    if doc.account_number_pattern:
        try:
            re.compile(doc.account_number_pattern)
        except re.error as e:
            frappe.throw(f"Account Number Pattern is not a valid regular expression: {e}")
    # "522522 - KCB Paybill AC" pasted from a result: the name only
    received = cstr(doc.receiver_name).strip()
    prefix = pa.number_prefix(received)
    if prefix and prefix.isdigit() and prefix != doc.paybill:
        frappe.throw(f"Name M-Pesa Shows starts with {prefix}, not this row's paybill {doc.paybill}")
    doc.receiver_name = pa.without_number(received) or None


def check_delete(doc) -> None:
    """on_trash: a shipped row is disabled, never deleted (a release would not bring it back)."""
    if cint(doc.get("is_shipped")):
        frappe.throw(f"M-Pesa Bank Paybill {doc.name} came with M-Pesa Suite: disable it instead of deleting it.")


def after_change(doc) -> None:
    """on_update: a changed paybill blocks the approved accounts that copied the old number (and a
    change back unblocks them)."""
    before = doc.get_doc_before_save()
    if before and cstr(before.get("paybill")) != cstr(doc.paybill):
        flag_accounts(doc, cstr(before.get("paybill")))


def flag_accounts(row, old_paybill: str = "") -> dict:
    """Block (needs_reapproval) each approved account on ``row`` whose number is no longer the row's
    paybill, unblock those whose number is the row's again. Returns {account DocType: blocked}."""
    blocked = {}
    for p in pa.PROFILES.values():
        accounts = frappe.get_all(p.doctype, filters={"bank_paybill": row.name, "docstatus": 1},
                                  fields=["name", "shortcode", "needs_reapproval", "reapproval_reason"])
        count = 0
        for acc in accounts:
            if cstr(acc.shortcode).strip() != cstr(row.paybill):
                if cint(acc.needs_reapproval) and not _paybill_reason(acc, row):
                    continue  # blocked for another reason (an approval without a second person): kept as it is
                reason = (f"Bank paybill {row.name} changed from {acc.shortcode} to {row.paybill}: cancel and amend "
                          "this account so a manager approves the new number.")
                frappe.db.set_value(p.doctype, acc.name, {"needs_reapproval": 1, "reapproval_reason": reason})
                pa.note(p.doctype, acc.name, reason)
                count += 1
            elif cint(acc.needs_reapproval) and _paybill_reason(acc, row):
                frappe.db.set_value(p.doctype, acc.name, {"needs_reapproval": 0, "reapproval_reason": None})
                pa.note(p.doctype, acc.name, f"Bank paybill {row.name} is {row.paybill} again, the number this "
                                             "account was approved with: it can be paid again.")
        blocked[p.doctype] = count
        if count:
            # employee accounts are salary data: a count, never whose
            pa.alert(f"Bank paybill {row.name} changed from {old_paybill or 'its old number'} to {row.paybill}: "
                     f"{count} approved {p.doctype}{'s are' if count > 1 else ' is'} blocked until cancelled, "
                     "amended and approved again.", row)
    return blocked


def _paybill_reason(acc, row) -> bool:
    """Whether the account is blocked because ``row``'s paybill changed (flag_accounts' own reason, or
    none recorded): the paybill coming back unblocks only those."""
    reason = cstr(acc.get("reapproval_reason")).strip()
    return not reason or reason.startswith(f"Bank paybill {row.name} changed")


# ---------------------------------------------------------------- the shipped list
def load_shipped(path=None) -> list:
    """The rows this release ships: a JSON list of dicts with ``code`` and SEED_FIELDS (and
    ``retired``)."""
    path = Path(path) if path else SHIPPED_FILE
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [r for r in rows if cstr(r.get("code")).strip()] if isinstance(rows, list) else []


def _norm(field, value):
    if field in ("enabled", "is_shipped"):
        return cint(value)
    return cstr(value).strip()


def _shipment(row) -> dict:
    return {"code": cstr(row["code"]).strip().upper(), **{f: _norm(f, row.get(f)) for f in SEED_FIELDS}}


def _shipped_values(existing) -> dict:
    value = existing.get("shipped_values")
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def plan_seed(existing: list, shipped: list) -> list:
    """What a sync does, as actions (pure: unit-tested without a site).

    ``existing``: the site's rows (name, code, SEED_FIELDS, is_shipped, shipped_values).
    ``shipped``: this release's rows. Each action is a dict with ``action`` (insert, adopt, update,
    keep, retire), ``code``, ``name`` (an existing row's), ``values`` (to write) and ``kept``
    (field: (release's value, the row's value) for each admin edit kept)."""
    by_code, by_paybill, by_name = {}, {}, {}
    for row in existing:
        by_name[row.get("name")] = row
        if cint(row.get("is_shipped")):
            code = _shipped_values(row).get("code") or row.get("code")
            by_code.setdefault(cstr(code).upper(), row)
        by_paybill.setdefault(cstr(row.get("paybill")).strip(), row)
    actions, adopted = [], set()
    for item in shipped:
        new = _shipment(item)
        code = new["code"]
        row = by_code.get(code)
        if row is None:
            candidate = by_name.get(code) or by_paybill.get(new["paybill"])
            if (candidate is not None and not cint(candidate.get("is_shipped")) and not item.get("retired")
                    and candidate["name"] not in adopted):
                # made by an admin before the release shipped it: keep their values, mark it shipped
                adopted.add(candidate["name"])
                actions.append({"action": ADOPT, "code": code, "name": candidate["name"],
                                "values": {"is_shipped": 1, "shipped_values": new}, "kept": {}})
                continue
            if candidate is not None or item.get("retired"):
                continue  # the paybill is another shipped row's now, or retired before this site had it
            actions.append({"action": INSERT, "code": code, "name": None,
                            "values": {**new, "is_shipped": 1, "shipped_values": new}, "kept": {}})
            continue
        before = _shipped_values(row)
        values, kept = {}, {}
        for field in SEED_FIELDS:
            current, was, now = _norm(field, row.get(field)), _norm(field, before.get(field)), new[field]
            if now == was or now == current:
                continue
            if current == was:
                values[field] = now
            else:
                kept[field] = (now, current)
        touched = any(_norm(f, row.get(f)) != _norm(f, before.get(f)) for f in SEED_FIELDS)
        if item.get("retired"):
            if not touched and cint(row.get("enabled")):
                actions.append({"action": RETIRE, "code": code, "name": row["name"],
                                "values": {"enabled": 0, "shipped_values": {**new, "enabled": 0}}, "kept": {}})
            continue
        if values or kept or before != new:
            actions.append({"action": UPDATE if values else KEEP, "code": code, "name": row["name"],
                            "values": {**values, "shipped_values": new}, "kept": kept})
    return actions


def sync_shipped(path=None) -> list:
    """After install and every migrate: the shipped rows into the site (``plan_seed``). One row's
    failure is logged and never stops the migrate."""
    if not frappe.db.exists("DocType", BANK_PAYBILL):
        return []
    fields = ["name", "code", *SEED_FIELDS, "is_shipped", "shipped_values"]
    existing = frappe.get_all(BANK_PAYBILL, fields=fields)
    actions = plan_seed(existing, load_shipped(path))
    for action in actions:
        try:
            _apply(action)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"M-Pesa Bank Paybill {action['code']} not synced")
    return actions


def _apply(action) -> None:
    values = dict(action["values"])
    values["shipped_values"] = json.dumps(values["shipped_values"], sort_keys=True)
    if action["action"] == INSERT:
        doc = frappe.get_doc({"doctype": BANK_PAYBILL, **values})
        doc.flags.mpsa_seed = True
        doc.insert(ignore_permissions=True)
        return
    doc = frappe.get_doc(BANK_PAYBILL, action["name"])
    doc.update(values)
    doc.flags.mpsa_seed = True
    doc.save(ignore_permissions=True)  # a paybill change blocks the accounts that copied the old one
    if action["action"] == ADOPT:
        doc.add_comment("Comment", f"M-Pesa Suite ships this paybill as {action['code']}: your values are kept, and a "
                                   "release changes only the fields you have not changed since.")
    for field, (shipped, yours) in action["kept"].items():
        doc.add_comment("Comment", f"This release ships {field} {shipped!r}; your {yours!r} is kept.")
