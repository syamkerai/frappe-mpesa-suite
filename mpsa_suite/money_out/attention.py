"""One daily "needs attention" alert to the Notify Role (M-Pesa Suite Settings).

Round 4.5's features each have things staff must follow up: bulk payout sheets part paid,
failed or waiting, a petty cash phone whose book balance and last SMS balance differ or whose
money lies unspent, staff advances not settled. Each feature adds a section; once a day the
sections that have something to say go out together, as one alert (party_accounts.alert: the
Notify Role's users, a notification and its email), instead of one alert per feature.

Sections, in hooks.py (read with ``frappe.get_hooks``, every app's in turn):

    mpsa_attention_sections = ["mpsa_suite.money_out.bulk_payout.attention_section"]

Each path names a function taking no argument and returning ``(title, [lines])``: a short title
("Bulk payout sheets") and one line per item ("MBP-26-11-0003: 64 paid, 3 failed, KES 12,400 left").
No lines (or None): the section has nothing to say today. ``(title, [lines], link)``: also where the
section's items are listed (a URL, e.g. the sheets' list): its title links there in the email, and
the alert opens the first such link instead of M-Pesa Suite Settings. ``register(section)`` adds a function
(or a path) in-process. A section that raises is logged and left out: it never stops the others.
"""

import html
import importlib

import frappe
from frappe.utils import cstr

HOOK = "mpsa_attention_sections"
SUITE = "M-Pesa Suite Settings"
SAVEPOINT = "mpesa_attention_section"
# The notification's subject begins so and names each section; its email lists every line
SUBJECT = "M-Pesa needs attention"

# in-process sections (register): functions or dotted paths
_REGISTERED = []


def register(section) -> None:
    """Add a section (a function, or its dotted path) in this process."""
    if section not in _REGISTERED:
        _REGISTERED.append(section)


def sections() -> list:
    """Every section: the apps' hooks, then those registered in-process."""
    get_hooks = getattr(frappe, "get_hooks", None)
    hooked = []
    if get_hooks:
        try:
            found = get_hooks(HOOK)
        except Exception:
            frappe.log_error(title="M-Pesa daily attention: hooks unreadable", message=frappe.get_traceback())
            found = []
        hooked = [cstr(p).strip() for p in (found if isinstance(found, (list, tuple)) else []) if cstr(p).strip()]
    return list(dict.fromkeys([*hooked, *_REGISTERED]))


def _name(section) -> str:
    if isinstance(section, str):
        return section
    return f"{getattr(section, '__module__', '')}.{getattr(section, '__qualname__', repr(section))}".strip(".")


def _resolve(section):
    if callable(section):
        return section
    module, _, attr = cstr(section).rpartition(".")
    return getattr(importlib.import_module(module), attr)


def collect() -> list:
    """[(title, [lines])] of the sections with something to say, in hook order. Each runs under a
    savepoint; one that raises is rolled back to it, logged and left out."""
    out = []
    for section in sections():
        frappe.db.savepoint(SAVEPOINT)
        try:
            found = _resolve(section)()
        except Exception:
            frappe.db.rollback(save_point=SAVEPOINT)
            frappe.log_error(title=f"M-Pesa daily attention: {_name(section)} failed"[:140],
                             message=frappe.get_traceback())
            continue
        if not found:
            continue
        title, lines, *link = found
        lines = [cstr(line).strip() for line in lines or [] if cstr(line).strip()]
        link = cstr(link[0]).strip() if link else ""
        if lines:
            title = cstr(title).strip() or _name(section)
            out.append((title, lines, link) if link else (title, lines))
    return out


def subject(collected) -> str:
    """The notification's subject: each section's title and how many lines it has."""
    return f"{SUBJECT}: " + "; ".join(f"{title} ({len(lines)})" for title, lines, *_link in collected)


def details(collected) -> str:
    """The email: each section's title and its lines, as HTML (the lines are escaped)."""
    parts = []
    for title, lines, *link in collected:
        items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        heading = html.escape(title)
        if link and link[0]:
            heading = f'<a href="{html.escape(link[0], quote=True)}">{heading}</a>'
        parts.append(f"<p><b>{heading}</b></p><ul>{items}</ul>")
    return "".join(parts)


def link_of(collected) -> str | None:
    """The first section's link (the alert opens it), or None."""
    return next((link[0] for _title, _lines, *link in collected if link and link[0]), None)


def daily_attention() -> None:
    """Scheduler (daily): one alert to the Notify Role when any section has lines; nothing
    otherwise."""
    collected = collect()
    if not collected:
        return
    from .party_accounts import alert

    link = link_of(collected)
    extra = {"link": link} if link else {}
    alert(subject(collected), frappe._dict(doctype=SUITE, name=SUITE), details=details(collected), **extra)
