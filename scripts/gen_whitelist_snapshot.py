"""Write tests/unit/rename_whitelist_at_c.json: every module-level whitelisted function at release C.

    python scripts/gen_whitelist_snapshot.py <repo> 02d94a0 tests/unit/rename_whitelist_at_c.json

Reads the old package straight from git (git ls-tree / git show of commit C, read-only), with its own
AST scan (independent of do_rename.py), so R4's unit test checks the lookup table against C itself.
"""
import ast
import json
import subprocess
import sys

REPO, COMMIT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
OLD = "frappe_mpsa_payments"


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], check=True, capture_output=True).stdout


def whitelist_flags(dec):
    node = dec.func if isinstance(dec, ast.Call) else dec
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    if name != "whitelist":
        return None
    guest = False
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "allow_guest":
                guest = bool(ast.literal_eval(kw.value))
    return guest


files = [f for f in git("ls-tree", "-r", "--name-only", COMMIT, OLD).decode().splitlines() if f.endswith(".py")]
found, in_tests = {}, {}
for path in files:
    tree = ast.parse(git("show", f"{COMMIT}:{path}").decode("utf-8"))
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    is_test = path.rsplit("/", 1)[-1].startswith("test_") or "/tests/" in path
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            flags = [f for f in (whitelist_flags(d) for d in node.decorator_list) if f is not None]
            if flags:
                (in_tests if is_test else found)[f"{module}.{node.name}"] = any(flags)
commit = git("rev-parse", "--short", COMMIT).decode().strip()
data = {
    "release": commit,
    "about": "Every module-level @frappe.whitelist function of frappe_mpsa_payments at the last old-name "
    "release, with its allow_guest flag. Generated from git by an AST scan; read by test_rename_safeguards.py "
    "(RENAME_PLAN.md R4). Do not edit by hand: the old package is frozen at this release.",
    "whitelisted": dict(sorted(found.items())),
}
with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, indent=1)
    f.write("\n")
print(f"{len(found)} functions ({sum(found.values())} guest) at {commit}; in test files (not mapped): {sorted(in_tests)}")
