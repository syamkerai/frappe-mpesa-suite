"""frappe.log_error(title, message) takes a single-line first argument as the title, and on v15
the Error Log title is limited to 140 characters: a longer one makes the log itself raise
(CharacterLengthExceededError), so the code that meant to record a problem crashes instead.
Dynamic text (f-strings, str(e), .format) must therefore go in ``message=``, never first."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "frappe_mpsa_payments"
NEWLINE = chr(10)


def _dynamic(node) -> bool:
    """Text whose length is not fixed and that Frappe would keep as the title."""
    if isinstance(node, ast.Constant):
        return False
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)):
        return False  # a fixed, translated sentence
    if isinstance(node, ast.JoinedStr) and any(
        isinstance(part, ast.Constant) and NEWLINE in str(part.value) for part in node.values
    ):
        return False  # has a newline: Frappe swaps it into the message
    text = ast.unparse(node)
    return not (text.endswith("get_traceback()") or text == "frappe.get_traceback(with_context=True)")  # has newlines


def test_dynamic_log_error_text_is_passed_as_message():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log_error"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "frappe"
                and node.args
                and _dynamic(node.args[0])
            ):
                offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}: {ast.unparse(node.args[0])[:80]}")
    assert not offenders, "pass dynamic text as message=, with a short title=:\n" + "\n".join(offenders)
