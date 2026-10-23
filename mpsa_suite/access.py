"""The site side of the suite's permission model (docs/ROLES_AND_PERMISSIONS.md).

``after_migrate`` (also run by after_install) is idempotent and never takes anything away:

- Role Profiles: each created once and never changed afterwards (an admin may edit them).
- Credential fields: a site's own permission rules (Custom DocPerm) replace the suite's rules
  for that DocType altogether. Where they do on a DocType with permission-level-1 fields
  (Mpesa Settings), the roles that may edit it today get level 1 too, so the move of the
  credentials to level 1 hides them from readers without locking out the site's editors.
- Site rules that differ from the suite's are reported (one Error Log per change), never
  deleted. An M-Pesa admin resets a DocType to the suite's rules with ``reset_permissions``
  (M-Pesa Suite Settings > Permissions > Check Permissions).
"""

import hashlib

import frappe
from frappe import _

from . import permission_matrix as matrix
from .permissions import CONFIG_ROLES, require

CUSTOM = "Custom DocPerm"
RIGHTS = tuple(matrix.RIGHTS.values())
# Ships in this app's folder (from upstream) but belongs to the payments app: never reset here.
NOT_OURS = ("Payment Gateway",)
REPORTED_KEY = "mpsa_suite_permission_overrides"


def after_migrate() -> None:
    ensure_role_profiles()
    ensure_secret_field_access()
    report_overrides()


def suite_doctypes() -> list:
    return [dt for dt in matrix.DOCTYPES if dt not in NOT_OURS and frappe.db.exists("DocType", dt)]


# ---------------------------------------------------------------- Role Profiles
def ensure_role_profiles() -> list:
    """Create the suite's Role Profiles that are missing. Existing ones are left as they are."""
    created = []
    for name, roles in matrix.ROLE_PROFILES.items():
        if frappe.db.exists("Role Profile", name):
            continue
        profile = frappe.new_doc("Role Profile")
        profile.role_profile = name
        for role in roles:
            if frappe.db.exists("Role", role):
                profile.append("roles", {"role": role})
        profile.insert(ignore_permissions=True)  # install / migrate: no session user
        created.append(name)
    return created


# ---------------------------------------------------------------- credential fields
def custom_rows(doctype: str) -> list:
    return frappe.get_all(
        CUSTOM,
        filters={"parent": doctype},
        fields=["name", "role", "permlevel", "if_owner", *RIGHTS],
        order_by="idx asc",
    )


def ensure_secret_field_access() -> list:
    """Give each role that edits a credential DocType under the site's own rules the same right
    at the credentials' level. Returns the (doctype, role, level) rows added."""
    added = []
    for doctype in matrix.SECRET_FIELDS:
        rows = custom_rows(doctype) if frappe.db.exists("DocType", doctype) else []
        if not rows:
            continue  # the suite's rules apply, and they have the level-1 rows
        levels = {int(df.permlevel or 0) for df in frappe.get_meta(doctype).fields if df.permlevel}
        editors = sorted({r.role for r in rows if not int(r.permlevel or 0) and r.write})
        for level in sorted(levels):
            have = {r.role for r in rows if int(r.permlevel or 0) == level}
            for role in editors:
                if role in have:
                    continue
                frappe.get_doc(
                    {
                        "doctype": CUSTOM,
                        "parent": doctype,
                        "parenttype": "DocType",
                        "parentfield": "permissions",
                        "role": role,
                        "permlevel": level,
                        "read": 1,
                        "write": 1,
                    }
                ).insert(ignore_permissions=True)
                added.append((doctype, role, level))
        if added:
            frappe.clear_cache(doctype=doctype)
    return added


# ---------------------------------------------------------------- site overrides
def _rights(row) -> set:
    return {r for r in RIGHTS if row.get(r)}


def overrides() -> list:
    """DocTypes whose permissions on this site are the site's own (Custom DocPerm), with how they
    differ from the suite's: rights the site grants beyond the suite (``extra``) and rights the
    suite would grant that the site does not (``missing``)."""
    out = []
    for doctype in suite_doctypes():
        rows = custom_rows(doctype)
        if not rows:
            continue
        site = {}
        for row in rows:
            site.setdefault((row.role, int(row.permlevel or 0)), set()).update(_rights(row))
        suite = matrix.rows(doctype)
        extra, missing = [], []
        for key in sorted(set(site) | set(suite)):
            more = sorted(site.get(key, set()) - suite.get(key, set()))
            less = sorted(suite.get(key, set()) - site.get(key, set()))
            if more:
                extra.append({"role": key[0], "permlevel": key[1], "rights": more})
            if less:
                missing.append({"role": key[0], "permlevel": key[1], "rights": less})
        out.append({"doctype": doctype, "rows": len(rows), "extra": extra, "missing": missing})
    return out


def _summary(found: list) -> str:
    lines = []
    for item in found:
        lines.append(f"{item['doctype']}: the site's own rules ({item['rows']} rows) replace the suite's.")
        for label, key in (("grants beyond the suite", "extra"), ("does not grant", "missing")):
            for row in item[key]:
                level = f" (level {row['permlevel']})" if row["permlevel"] else ""
                lines.append(f"  {label}: {row['role']}{level}: {', '.join(row['rights'])}")
    return "\n".join(lines)


def report_overrides() -> None:
    """One Error Log when the site's overrides change (not on every migrate)."""
    text = _summary(overrides())
    digest = hashlib.sha1(text.encode()).hexdigest() if text else ""
    if digest == (frappe.db.get_default(REPORTED_KEY) or ""):
        return
    frappe.db.set_default(REPORTED_KEY, digest)
    if text:
        frappe.log_error(
            title="M-Pesa Suite: this site's own permission rules differ from the suite's",
            message=(
                "Frappe uses a site's Custom DocPerm rows for a DocType instead of the rules the app "
                "ships, so the suite's roles and its stricter defaults do not apply to these DocTypes. "
                "Nothing was changed. An M-Pesa admin can compare and reset them in M-Pesa Suite "
                "Settings > Permissions > Check Permissions (see docs/ROLES_AND_PERMISSIONS.md).\n\n" + text
            ),
        )


@frappe.whitelist()
def permission_report() -> dict:
    """What the Check Permissions dialog shows."""
    require(CONFIG_ROLES)
    found = overrides()
    for item in found:
        for row in item["extra"]:
            row["users"] = _enabled_users_with(row["role"])
    return {"overrides": found, "suite_doctypes": suite_doctypes()}


def _enabled_users_with(role: str) -> int:
    return frappe.db.sql(
        """SELECT COUNT(DISTINCT u.name) FROM `tabHas Role` hr JOIN `tabUser` u ON u.name = hr.parent
        WHERE hr.parenttype = 'User' AND hr.role = %s AND u.enabled = 1 AND u.name NOT IN ('Administrator', 'Guest')""",
        role,
    )[0][0]


@frappe.whitelist(methods=["POST"])
def reset_permissions(doctypes) -> dict:
    """Delete this site's own permission rules (Custom DocPerm) of these DocTypes, so the suite's
    apply. Explicit, by an M-Pesa admin, recorded on M-Pesa Suite Settings."""
    require(CONFIG_ROLES)
    doctypes = frappe.parse_json(doctypes) if isinstance(doctypes, str) else doctypes
    allowed = set(suite_doctypes())
    unknown = [dt for dt in doctypes or [] if dt not in allowed]
    if unknown or not doctypes:
        frappe.throw(_("Only M-Pesa Suite DocTypes can be reset here: {0}").format(", ".join(unknown) or "-"))
    for doctype in doctypes:
        count = frappe.db.count(CUSTOM, {"parent": doctype})
        if not count:
            continue
        frappe.db.delete(CUSTOM, {"parent": doctype})
        frappe.clear_cache(doctype=doctype)
        frappe.get_doc("M-Pesa Suite Settings").add_comment(
            "Comment",
            _("Permissions of {0} reset to the M-Pesa Suite's rules by {1} ({2} site rows removed).").format(
                doctype, frappe.session.user, count
            ),
        )
    frappe.db.set_default(REPORTED_KEY, "")
    return permission_report()
