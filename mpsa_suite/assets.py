"""Desk script URLs that change when the script does.

Frappe Cloud serves /assets with ``Cache-Control: max-age=31536000, immutable``, so a browser
keeps a script it loaded by a plain path (app_include_js) for a year and never sees a deploy's
change to it, a hard refresh included. A fingerprint of the file's content in the URL makes a
changed script a new URL (fetched once) and leaves an unchanged one cached.

The scripts stay plain files (not an esbuild bundle): utils.js declares top-level functions the
form scripts call as globals, which a bundle would make module-private.

No frappe import: hooks.py uses this when Frappe loads the app's hooks.
"""

import hashlib
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent / "public" / "js"
URL_PREFIX = "/assets/mpsa_suite/js/"


def versioned(name: str) -> str:
    """``/assets/mpsa_suite/js/<name>?v=<content hash>``; the plain path if the file is unreadable."""
    try:
        digest = hashlib.sha256((JS_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return URL_PREFIX + name
    return f"{URL_PREFIX}{name}?v={digest}"
