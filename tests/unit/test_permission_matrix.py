"""The permission matrix (mpsa_suite/permission_matrix.py) against the code it describes.

Fails when a DocType, report or whitelisted endpoint changes who may do what without the matrix
and docs/ROLES_AND_PERMISSIONS.md changing with it, and when a whitelisted function has no
server-side permission check.
"""

import ast
import json
from pathlib import Path

import pytest

from mpsa_suite import permission_matrix as M

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
DOC = ROOT / "docs" / "ROLES_AND_PERMISSIONS.md"
BEGIN, END = "<!-- BEGIN GENERATED: permission_matrix -->", "<!-- END GENERATED: permission_matrix -->"


def _doctype_files():
    out = {}
    for path in sorted(APP.glob("**/doctype/*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("doctype") == "DocType":
            out[data["name"]] = data
    return out


DOCTYPES = _doctype_files()
TOP_LEVEL = {name: d for name, d in DOCTYPES.items() if not d.get("istable")}


def _json_rows(data):
    rows = {}
    for p in data.get("permissions", []):
        key = (p["role"], int(p.get("permlevel") or 0))
        assert key not in rows, f"{data['name']}: two permission rows for {key}"
        rows[key] = {r for r in M.RIGHTS.values() if p.get(r)}
    return rows


def _role_sets():
    """permissions.py's role tuples by name, read from the source (it imports frappe)."""
    tree = ast.parse((APP / "permissions.py").read_text(encoding="utf-8"))
    sets = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
            name = node.targets[0].id
            if name.endswith("_ROLES"):
                sets[name] = tuple(ast.literal_eval(node.value))
    return sets


ROLE_SETS = _role_sets()


# ---------------------------------------------------------------- DocTypes
def test_every_doctype_is_in_the_matrix_and_no_stale_entry():
    assert sorted(TOP_LEVEL) == sorted(M.DOCTYPES)


@pytest.mark.parametrize("doctype", sorted(M.DOCTYPES))
def test_doctype_json_permissions_are_the_matrix(doctype):
    assert _json_rows(TOP_LEVEL[doctype]) == M.rows(doctype)


def test_child_tables_carry_no_permissions_of_their_own():
    for name, data in DOCTYPES.items():
        if data.get("istable"):
            assert not data.get("permissions"), name


def test_rights_only_where_the_doctype_has_them():
    for doctype, data in TOP_LEVEL.items():
        for (role, _), rights in M.rows(doctype).items():
            if not data.get("is_submittable"):
                assert not rights & {"submit", "cancel", "amend"}, (doctype, role)


def test_credential_fields_are_on_level_1_and_only_config_roles_hold_it():
    config = set(ROLE_SETS["CONFIG_ROLES"])
    for doctype, fields in M.SECRET_FIELDS.items():
        meta = {f["fieldname"]: f for f in TOP_LEVEL[doctype]["fields"]}
        for name in fields:
            assert int(meta[name].get("permlevel") or 0) == 1, f"{doctype}.{name} is not on level 1"
        for (role, level), rights in M.rows(doctype).items():
            if level:
                assert role in config, f"{role} holds level {level} on {doctype}"
            elif role not in config:
                assert not rights & {"write", "create", "delete", "export", "print", "email", "share"}, (
                    f"{role} may change or export {doctype}"
                )


def test_settings_below_mpesa_manager_are_select_only():
    # nobody below M-Pesa Manager opens the settings records; they pick them in Link fields
    config = set(ROLE_SETS["CONFIG_ROLES"])
    for doctype in M.SECRET_FIELDS:
        for (role, _), rights in M.rows(doctype).items():
            if role not in config:
                assert rights == {"select"}, (doctype, role, rights)


def test_password_fields_are_never_readable_below_config_roles():
    config = set(ROLE_SETS["CONFIG_ROLES"])
    for doctype, data in TOP_LEVEL.items():
        for field in data["fields"]:
            if field["fieldtype"] != "Password":
                continue
            level = int(field.get("permlevel") or 0)
            readers = {role for (role, lvl), rights in M.rows(doctype).items() if lvl == level and "read" in rights}
            assert readers <= config, f"{doctype}.{field['fieldname']} readable by {readers - config}"


def test_config_only_doctypes():
    config = set(ROLE_SETS["CONFIG_ROLES"])
    for doctype in M.CONFIG_ONLY:
        assert {role for role, _ in M.rows(doctype)} <= config, doctype


def test_viewer_only_reads():
    for doctype in M.DOCTYPES:
        rights = M.rows(doctype).get((M.MV, 0), set())
        assert rights <= {"read", "select", "report", "export", "print"}, (doctype, rights)
        if doctype in M.SECRET_FIELDS or doctype in M.CONFIG_ONLY:
            assert rights <= {"select"}, f"M-Pesa Viewer may open {doctype}"


def test_suite_roles_are_the_installed_ones():
    source = (APP / "install.py").read_text(encoding="utf-8")
    for role in M.SUITE_ROLES:
        assert f'"{role}"' in source


def test_role_profiles_use_known_roles():
    known = set(M.ROLES) | {M.EM}
    for profile, roles in M.ROLE_PROFILES.items():
        assert set(roles) <= known, profile
        assert set(roles) & {M.AU, M.SU, M.EM}, f"{profile}: no ERPNext role that reads Company"


# ---------------------------------------------------------------- reports
def test_report_roles_are_the_matrix_and_can_open_the_ref_doctype():
    found = {}
    for path in sorted(APP.glob("mpsa_suite/report/*/*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        found[data["name"]] = data
        assert {r["role"] for r in data.get("roles", [])} == set(M.REPORTS[data["name"]]), data["name"]
        ref = data["ref_doctype"]
        for role in M.REPORTS[data["name"]]:
            assert "report" in M.rows(ref).get((role, 0), set()), f"{role} cannot open {data['name']} ({ref})"
    assert sorted(found) == sorted(M.REPORTS)


# ---------------------------------------------------------------- endpoints
def _module_path(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _whitelisted():
    """{"module.func" | "module.Class.method": (node, module_tree, allow_guest)}."""
    out = {}
    for path in sorted(APP.glob("**/*.py")):
        if path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        if "whitelist" not in source:
            continue
        tree = ast.parse(source)
        module = _module_path(path)

        def visit(body, prefix):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, prefix + node.name + ".")
                elif isinstance(node, ast.FunctionDef):
                    for dec in node.decorator_list:
                        if "whitelist" in ast.unparse(dec):
                            guest = isinstance(dec, ast.Call) and any(
                                k.arg == "allow_guest" and ast.literal_eval(k.value) for k in dec.keywords
                            )
                            out[f"{module}.{prefix}{node.name}"] = (node, tree, guest)

        visit(tree.body, "")
    return out


WHITELISTED = _whitelisted()


def _name(func) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _calls(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _arg_names(call):
    names = set()
    for arg in list(call.args) + [k.value for k in call.keywords]:
        for n in ast.walk(arg):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                names.add(n.value)
    return names


def _evidence(node, token) -> bool:
    calls = _calls(node)
    decorators = [d for d in node.decorator_list if isinstance(d, ast.Call)]
    if token == "callback":
        return any(_name(d.func) == "safaricom_callback" for d in decorators)
    if token in ("api", "page"):
        for d in decorators:
            if _name(d.func) == "endpoint":
                open_page = any(k.arg == "authenticated" and not ast.literal_eval(k.value) for k in d.keywords)
                if token == "api" and not open_page:
                    return True
                if token == "page" and open_page:
                    return any(_name(c.func) == "_intent_by_token" for c in calls)
        return False
    if token == "perm":
        return any(_name(c.func) in ("has_permission", "check_permission") for c in calls)
    if token == "direct":
        return any(_name(c.func) in ("require_for_direct_call", "is_direct_call") for c in calls)
    if token.startswith("roles:"):
        wanted = token.split(":", 1)[1]
        return any(
            _name(c.func) in ("require", "require_for_direct_call") and wanted in _arg_names(c) for c in calls
        )
    if token.startswith("via:"):
        return any(_name(c.func) == token.split(":", 1)[1] for c in calls)
    raise AssertionError(f"unknown evidence {token}")


def test_every_whitelisted_function_is_in_the_matrix_and_no_stale_entry():
    assert sorted(WHITELISTED) == sorted(M.ENDPOINTS)


@pytest.mark.parametrize("path", sorted(M.ENDPOINTS))
def test_endpoint_has_its_server_side_check(path):
    node, _, guest = WHITELISTED[path]
    who, evidence = M.ENDPOINTS[path]
    assert evidence, path
    for token in evidence:
        assert _evidence(node, token), f"{path}: no {token} in its code"
    if guest:
        # guests only through the Safaricom / Payments API / pay page doors, or refused (R4 keeps the flag)
        assert set(evidence) & set(M.GUEST_EVIDENCE) or {"direct", "perm"} <= set(evidence), path
    else:
        assert any(t == "perm" or t.startswith(("roles:", "via:")) for t in evidence), f"{path}: no check"
    for token in evidence:
        if token.startswith("roles:"):
            name = token.split(":", 1)[1]
            assert name in ROLE_SETS or name in M.ROLES or name in ("PAYOUT_OR_ADMIN", "RELEASE_ROLES"), token


@pytest.mark.parametrize("helper", sorted(M.HELPERS))
def test_helpers_check_permissions_themselves(helper):
    module, evidence = M.HELPERS[helper]
    path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == helper)
    for token in evidence:
        assert _evidence(node, token), f"{helper}: no {token}"


def test_guest_endpoints_are_only_the_known_doors():
    guests = sorted(p for p, (_, _, guest) in WHITELISTED.items() if guest)
    for path in guests:
        evidence = set(M.ENDPOINTS[path][1])
        assert evidence & set(M.GUEST_EVIDENCE) or path == "mpsa_suite.utils.utils.convert_amount_to_kes", path


def test_no_frappe_only_for_left():
    # frappe.only_for passes everyone in Frappe 15 bench tests; permissions.require does not
    for path in APP.glob("**/*.py"):
        if path.name.startswith("test_"):
            continue
        for call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            assert ast.unparse(call.func) != "frappe.only_for", path


# ---------------------------------------------------------------- the doc
def test_doc_generated_section_is_current():
    text = DOC.read_text(encoding="utf-8")
    assert BEGIN in text and END in text
    generated = text.split(BEGIN, 1)[1].split(END, 1)[0].strip("\n") + "\n"
    expected = M.render_markdown(ROLE_SETS)
    assert generated == expected, (
        "docs/ROLES_AND_PERMISSIONS.md is out of date: paste "
        "mpsa_suite.permission_matrix.render_markdown(...) between its GENERATED markers "
        "(python -c 'import tests.unit.test_permission_matrix as t; t.write_doc()')"
    )


def write_doc():
    """Refresh the generated part of the doc (a developer helper, not a test)."""
    text = DOC.read_text(encoding="utf-8")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    DOC.write_text(head + BEGIN + "\n" + M.render_markdown(ROLE_SETS) + END + tail, encoding="utf-8", newline="\n")
