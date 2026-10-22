#!/usr/bin/env python3
"""Make a renamed copy of the frappe_mpsa_payments app as mpsa_suite ("Mpsa Suite").

    python3 do_rename.py --src <checkout of frappe_mpsa_payments> --dst <new dir> [--force]
                         [--rewrite-docs] [--modified "YYYY-MM-DD HH:MM:SS.ffffff"]

Reads --src only; never writes to it. Writes a renamed copy to --dst:

  * package frappe_mpsa_payments/ -> mpsa_suite/, module folder frappe_mpsa_payments/frappe_mpsa_payments/
    -> mpsa_suite/mpsa_suite/, frappe_mpsa_payments_dashboard/ -> mpsa_suite_dashboard/ (any path part
    containing the old package name is renamed)
  * text replacements in code and config: "Astral M-Pesa Suite" -> "Mpsa Suite",
    "Frappe Mpsa Payments" -> "Mpsa Suite", "frappe_mpsa_payments" -> "mpsa_suite"
    (Markdown only with --rewrite-docs, because docs record historical URLs)
  * hooks.py: app_title and apps-screen title "Mpsa Suite"; override_whitelisted_methods mapping
    every old whitelisted path (guest callbacks and the rest) to the new function;
    before_app_uninstall and an extra after_migrate for the hand-over (mpsa_suite/handover.py)
  * `modified` bumped in every standard non-DocType record JSON (Report, Workspace, Dashboard,
    Dashboard Chart, Number Card, Workspace Sidebar, Desktop Icon, ...) so a normal migrate
    re-imports them into the new module (Frappe skips those unless the file is newer)

Prints a summary and every remaining mention of the old names.

This is the script that produced the rename commit on branch mpsa-suite (run on a clean
checkout of the last old-name release, commit C = 02d94a0). The safeguards R1, R2 and R4 to R9
of docs/RENAME_PLAN.md section 4.2 were added by hand in the next commit; R3 lives only in the
old app. Usage at a later release: see docs/RENAME_SPIKE.md.
"""

import argparse
import ast
import datetime as dt
import json
import os
import re
import shutil
import sys

OLD_APP = "frappe_mpsa_payments"
NEW_APP = "mpsa_suite"
OLD_MODULE = "Frappe Mpsa Payments"
NEW_MODULE = "Mpsa Suite"
OLD_TITLE = "Astral M-Pesa Suite"
NEW_TITLE = "Mpsa Suite"
OLD_SCREEN_TITLE = '"title": "Astral M-Pesa",'
NEW_SCREEN_TITLE = '"title": "Mpsa Suite",'

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
# a git worktree has a .git FILE (pointer to the main repository), never copy it
SKIP_FILES = {".git"}
SKIP_SUFFIXES = (".pyc", ".pyo")
DOC_SUFFIXES = (".md",)
# DocTypes are re-imported by content hash; everything else only when the file is newer.
HASHED_DOCTYPES = {"DocType"}

REPLACEMENTS = [(OLD_TITLE, NEW_TITLE), (OLD_MODULE, NEW_MODULE), (OLD_APP, NEW_APP)]


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def new_relpath(rel: str) -> str:
    return os.path.join(*[p.replace(OLD_APP, NEW_APP) for p in rel.split(os.sep)])


def copy_and_replace(src: str, dst: str, rewrite_docs: bool) -> dict:
    stats = {"files": 0, "text_changed": 0, "replacements": 0, "binary": 0, "docs_left": [], "path_map": {}}
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES) or name in SKIP_FILES:
                continue
            src_path = os.path.join(root, name)
            rel = os.path.relpath(src_path, src)
            out_rel = new_relpath(rel)
            out_path = os.path.join(dst, out_rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            stats["files"] += 1
            stats["path_map"][out_rel] = rel
            with open(src_path, "rb") as f:
                data = f.read()
            if not is_text(data):
                stats["binary"] += 1
                with open(out_path, "wb") as f:
                    f.write(data)
                continue
            text = data.decode("utf-8")
            if name.endswith(DOC_SUFFIXES) and not rewrite_docs:
                if OLD_APP in text or OLD_MODULE in text:
                    stats["docs_left"].append(out_rel)
                with open(out_path, "wb") as f:
                    f.write(data)
                continue
            count = 0
            for old, new in REPLACEMENTS:
                n = text.count(old)
                if n:
                    text = text.replace(old, new)
                    count += n
            if count:
                stats["text_changed"] += 1
                stats["replacements"] += count
            with open(out_path, "wb") as f:
                f.write(text.encode("utf-8"))
    return stats


def bump_modified(dst: str, stamp: dt.datetime) -> list:
    """Make every standard non-DocType record newer than the copy the old app imported."""
    bumped = []
    pkg = os.path.join(dst, NEW_APP)
    for root, dirs, files in os.walk(pkg):
        if os.sep + "custom" in root[len(pkg):] or os.sep + "fixtures" in root[len(pkg):]:
            continue
        for name in files:
            if not name.endswith(".json") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                doc = json.loads(text)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(doc, dict) or not doc.get("doctype") or not doc.get("name"):
                continue
            if doc["doctype"] in HASHED_DOCTYPES or "modified" not in doc:
                continue
            old = doc["modified"]
            try:
                old_dt = dt.datetime.fromisoformat(str(old))
            except ValueError:
                old_dt = stamp
            new_dt = max(stamp, old_dt + dt.timedelta(seconds=1))
            new = new_dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            # replace the top-level occurrence (the least indented one)
            pattern = re.compile(r'^([ \t]*)"modified": "' + re.escape(str(old)) + r'"', re.M)
            hits = list(pattern.finditer(text))
            if not hits:
                continue
            top = min(hits, key=lambda m: len(m.group(1)))
            text = text[: top.start()] + f'{top.group(1)}"modified": "{new}"' + text[top.end():]
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            bumped.append((os.path.relpath(path, dst), doc["doctype"], doc["name"], str(old), new))
    return bumped


def _is_whitelist(dec) -> tuple:
    """(is_whitelist, allow_guest) for a decorator node."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    if name != "whitelist":
        return False, False
    guest = False
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "allow_guest" and isinstance(kw.value, ast.Constant):
                guest = bool(kw.value.value)
    return True, guest


def whitelisted_methods(dst: str, path_map: dict) -> list:
    """[(old dotted path, new dotted path, allow_guest)] for every top-level whitelisted function."""
    out = []
    pkg = os.path.join(dst, NEW_APP)
    for root, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d != "tests"]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_") or name == "conftest.py":
                continue
            path = os.path.join(root, name)
            new_rel = os.path.relpath(path, dst)
            old_rel = path_map.get(new_rel)
            if old_rel is None:  # added by this script (handover.py): no old path to keep
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except SyntaxError as e:
                print(f"!! cannot parse {new_rel}: {e}", file=sys.stderr)
                continue

            def dotted(rel):
                mod = rel[:-3].replace(os.sep, ".")
                return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod

            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                flags = [_is_whitelist(d) for d in node.decorator_list]
                if not any(f[0] for f in flags):
                    continue
                guest = any(f[1] for f in flags)
                out.append((f"{dotted(old_rel)}.{node.name}", f"{dotted(new_rel)}.{node.name}", guest))
    return sorted(out, key=lambda r: (not r[2], r[0]))


HANDOVER_PY = '''"""Hand-over from the app's old package name (frappe_mpsa_payments) to mpsa_suite.

Both packages ship the same DocTypes under the same names, so they share the same tables.
Installing mpsa_suite next to frappe_mpsa_payments re-imports those DocTypes into the
"Mpsa Suite" module. Uninstalling frappe_mpsa_payments afterwards deletes everything that is
still in a module the old app owns (Module Def.app_name), DROPs the table of every DocType
in it and deletes every record linked to it (Custom Field, Property Setter, Report,
Workspace, Server Script, Client Script, ...).

rehome() files every record still under the old module under the new one. Frappe calls
before_app_uninstall() with the name of the app being removed, after the old app's own
before_uninstall and before it reads the Module Defs, so by the time it looks there is nothing
left to delete or drop. Any other module the old app still owns that has something in it
(Navari's "Stanbic" on older sites) is handed to mpsa_suite instead of being deleted.
"""

import frappe

OLD_APP = "frappe_mpsa_payments"
NEW_APP = "mpsa_suite"
OLD_MODULE = "Frappe Mpsa Payments"
NEW_MODULE = "Mpsa Suite"
APP_FIELD_DOCTYPES = ("Workspace", "Workspace Sidebar", "Desktop Icon")


def _module_link_fields() -> list:
    """(doctype, fieldname) of DocType.module and every Link to Module Def on the site."""
    fields = [("DocType", "module")]
    for row in frappe.get_all(
        "DocField", filters={"fieldtype": "Link", "options": "Module Def"}, fields=["parent", "fieldname"]
    ):
        if (row.parent, row.fieldname) not in fields:
            fields.append((row.parent, row.fieldname))
    return fields


def _usable(doctype: str, fieldname: str) -> bool:
    if not frappe.db.exists("DocType", doctype):
        return False
    meta = frappe.get_meta(doctype)
    return not meta.issingle and frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, fieldname)


def module_contents(module: str) -> dict:
    """{"Doctype.field": count} of everything filed under ``module``."""
    found = {}
    for doctype, fieldname in _module_link_fields():
        if _usable(doctype, fieldname):
            n = frappe.db.count(doctype, {fieldname: module})
            if n:
                found[f"{doctype}.{fieldname}"] = n
    return found


def rehome() -> dict:
    """Move every record filed under the old module (and old app) to the new one. Idempotent."""
    if not frappe.db.exists("Module Def", NEW_MODULE):
        return {}
    moved = {}
    for doctype, fieldname in _module_link_fields():
        if not _usable(doctype, fieldname):
            continue
        n = frappe.db.count(doctype, {fieldname: OLD_MODULE})
        if n:
            frappe.db.sql(
                f"UPDATE `tab{doctype}` SET `{fieldname}` = %s WHERE `{fieldname}` = %s",
                (NEW_MODULE, OLD_MODULE),
            )
            moved[f"{doctype}.{fieldname}"] = n
    for doctype in APP_FIELD_DOCTYPES:
        if _usable(doctype, "app"):
            n = frappe.db.count(doctype, {"app": OLD_APP})
            if n:
                frappe.db.sql(f"UPDATE `tab{doctype}` SET `app` = %s WHERE `app` = %s", (NEW_APP, OLD_APP))
                moved[f"{doctype}.app"] = n
    if moved:
        frappe.clear_cache()
    return moved


def before_app_uninstall(app_name: str) -> None:
    """Runs for every app uninstall; acts only when the old package is being removed."""
    if app_name != OLD_APP:
        return
    moved = rehome()
    kept = []
    for module in frappe.get_all("Module Def", filters={"app_name": OLD_APP}, pluck="name"):
        if module_contents(module):
            frappe.db.set_value("Module Def", module, "app_name", NEW_APP, update_modified=False)
            kept.append(module)
    frappe.db.commit()
    print(f"mpsa_suite hand-over: moved to '{NEW_MODULE}': {moved or 'nothing'}; "
          f"modules kept for {NEW_APP}: {kept or 'none'}")


def after_migrate() -> None:
    """Catch records the old app's after_migrate filed under its module while both are installed."""
    moved = rehome()
    if moved:
        print(f"mpsa_suite hand-over: moved to '{NEW_MODULE}': {moved}")
'''


def patch_hooks(dst: str, methods: list) -> None:
    path = os.path.join(dst, NEW_APP, "hooks.py")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'^app_title = ".*"$', f'app_title = "{NEW_TITLE}"', text, count=1, flags=re.M)
    text = text.replace(OLD_SCREEN_TITLE, NEW_SCREEN_TITLE)

    after_migrate = re.search(r'^after_migrate = "([^"]+)"$', text, re.M)
    if after_migrate:
        text = text.replace(
            after_migrate.group(0),
            f'after_migrate = [\n    "{after_migrate.group(1)}",\n    "{NEW_APP}.handover.after_migrate",\n]',
        )
    lines = [
        "",
        "# Hand-over from the old package name (see handover.py)",
        "# ------------------------------------------------------",
        f'before_app_uninstall = "{NEW_APP}.handover.before_app_uninstall"',
        "",
        f"# Old /api/method/{OLD_APP}.* paths keep working: Safaricom holds registered C2B and Pull",
        "# URLs and in-flight callback URLs, integrators and open pay pages call the old paths.",
        "# Guest (callback / pay page / Payments API) entries first.",
        "override_whitelisted_methods = {",
    ]
    for old, new, guest in methods:
        lines.append(f'    "{old}": "{new}",{"  # guest" if guest else ""}')
    lines.append("}")
    text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def leftovers(dst: str) -> list:
    out = []
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, dst)
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if OLD_APP in line or OLD_MODULE in line or "frappe-mpsa-payments" in line:
                            out.append(f"{rel}:{i}: {line.strip()[:140]}")
            except (UnicodeDecodeError, OSError):
                pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--force", action="store_true", help="delete --dst first if it exists")
    ap.add_argument("--rewrite-docs", action="store_true", help="also rewrite Markdown files")
    ap.add_argument("--modified", help="timestamp for bumped JSONs (default: now)")
    args = ap.parse_args()

    src, dst = os.path.abspath(args.src), os.path.abspath(args.dst)
    if not os.path.isfile(os.path.join(src, OLD_APP, "hooks.py")):
        print(f"{src} has no {OLD_APP}/hooks.py", file=sys.stderr)
        return 2
    if os.path.commonpath([src, dst]) == src:
        print("--dst must not be inside --src", file=sys.stderr)
        return 2
    if os.path.exists(dst):
        if not args.force:
            print(f"{dst} exists (use --force)", file=sys.stderr)
            return 2
        shutil.rmtree(dst)

    stamp = dt.datetime.fromisoformat(args.modified) if args.modified else dt.datetime.now()
    stats = copy_and_replace(src, dst, args.rewrite_docs)
    with open(os.path.join(dst, NEW_APP, "handover.py"), "w", encoding="utf-8") as f:
        f.write(HANDOVER_PY)
    bumped = bump_modified(dst, stamp)
    methods = whitelisted_methods(dst, stats["path_map"])
    patch_hooks(dst, methods)

    print(f"copied {stats['files']} files ({stats['binary']} binary) from {src} to {dst}")
    print(f"rewrote {stats['text_changed']} text files, {stats['replacements']} replacements")
    print(f"bumped modified in {len(bumped)} record JSONs:")
    for rel, doctype, name, old, new in bumped:
        print(f"  {doctype} '{name}': {old} -> {new}  ({rel})")
    guest = sum(1 for m in methods if m[2])
    print(f"override_whitelisted_methods: {len(methods)} paths ({guest} guest)")
    print(f"Markdown left unchanged with old names ({len(stats['docs_left'])}): {', '.join(stats['docs_left'])}")
    left = [l for l in leftovers(dst) if not l.startswith(os.path.join(NEW_APP, "handover.py"))
            and not (l.startswith(os.path.join(NEW_APP, "hooks.py")) and f'"{OLD_APP}.' in l)
            and not l.endswith(".md") and ".md:" not in l]
    print(f"old names left outside hooks override map, handover.py and Markdown: {len(left)}")
    for l in left:
        print("  " + l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
