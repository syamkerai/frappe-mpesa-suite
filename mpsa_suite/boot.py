"""What the desk may offer the session user: ``frappe.boot.mpsa`` (hooks.py boot_session).

The flags only decide which M-Pesa buttons a form or list shows; every endpoint and document
event checks again on the server. Their names are what the suite's form scripts read:
- ``can_prepare``: PREPARE_ROLES, prepare a payout from a document;
- ``can_pay``: PAYOUT_ROLES, send it;
- ``can_approve``: ADMIN_ROLES, approve payee accounts;
- ``can_collect``: COLLECT_ROLES, send STK prompts;
- ``can_configure``: CONFIG_ROLES, M-Pesa settings;
- ``can_see_salaries``: money_out.salary_access.can_see_salaries;
- ``maker_checker``: M-Pesa Suite Settings' maker-checker is on (who prepared or last changed a
  payout does not send it), for the texts that say who sends.
The shared Administrator login gets no ``can_pay`` or ``can_approve`` while M-Pesa Suite Settings
refuses it (permissions.administrator_refused), and System Manager none of ``can_pay``,
``can_approve``, ``can_see_salaries`` or ``can_prepare`` (decision D5): only named payers send.
"""

import frappe

from .permissions import ADMIN_ROLES, COLLECT_ROLES, CONFIG_ROLES, PAYOUT_ROLES, PREPARE_ROLES, administrator_refused

FLAGS = ("can_prepare", "can_pay", "can_approve", "can_collect", "can_configure", "can_see_salaries",
         "maker_checker")


def boot_session(bootinfo) -> None:
    bootinfo.mpsa = flags()


def flags(user: str | None = None) -> dict:
    user = user or frappe.session.user
    if not user or user == "Guest":
        return dict.fromkeys(FLAGS, False)
    from .money_out.salary_access import can_see_salaries
    from .money_out.schedule import maker_checker_on

    roles = set(frappe.get_roles(user))
    named = not administrator_refused(user)
    return {
        "can_prepare": not roles.isdisjoint(PREPARE_ROLES),
        "can_pay": named and not roles.isdisjoint(PAYOUT_ROLES),
        "can_approve": named and not roles.isdisjoint(ADMIN_ROLES),
        "can_collect": not roles.isdisjoint(COLLECT_ROLES),
        "can_configure": not roles.isdisjoint(CONFIG_ROLES),
        "can_see_salaries": bool(can_see_salaries(user)),
        "maker_checker": bool(maker_checker_on()),
    }
