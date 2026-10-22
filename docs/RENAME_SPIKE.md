# Rename to Mpsa Suite: how the branch was made, and the evidence so far

The app `frappe_mpsa_payments` ("Astral M-Pesa Suite", module "Frappe Mpsa Payments") becomes
`mpsa_suite` ("Mpsa Suite", module "Mpsa Suite") on branch `mpsa-suite`. DocType names, tables,
roles, secrets, Payment Gateway names and the `/pay/<token>` page do not change. The full plan,
with the hand-over steps for Trident and Astral, is `docs/RENAME_PLAN.md`, written alongside the
old-name branch (it is not part of this branch). This note records what was built here and what
has been shown to work.

## How the branch was made

1. **Release C.** The last old-name release is commit `02d94a0` on `astral-suite`. It carries the
   old app's own uninstall guard (R3): it refuses to uninstall `frappe_mpsa_payments` unless
   `mpsa_suite` is installed and owns the module "Mpsa Suite", or site_config has
   `mpsa_allow_old_uninstall: 1`. Nothing may land on the old-name branch after C.
2. **Rename commit.** `scripts/do_rename.py` run on a clean checkout of C:

   ```
   python3 scripts/do_rename.py --src <checkout of 02d94a0> --dst <new dir> \
       --modified "2026-10-22 20:35:00.000000"
   ```

   It copied 347 files, rewrote 165 of them (739 replacements), bumped `modified` in 14 record
   JSONs so a plain migrate re-imports them into the new module, wrote `mpsa_suite/handover.py`
   and built the lookup table: 70 old paths, 29 of them guest. The output was committed as is.
   Running the same command on C reproduces that commit. Run it under Linux or WSL: on Windows,
   deep paths exceed the 260-character limit.
3. **Safeguards commit.** R1, R2 and R4 to R9 below, added by hand.

## The safeguards

| # | What | Where |
|---|---|---|
| R1 | `after_sync` runs `rehome()` at the end of install-app, so nothing is left in the old module once mpsa_suite is installed (the check before uninstalling the old app can expect zero). | `mpsa_suite/hooks.py` `after_sync`; `handover.after_sync` |
| R2 | mpsa_suite's own `before_uninstall` refuses every uninstall of mpsa_suite, whatever the row counts, unless site_config has `mpsa_allow_uninstall: 1`. It runs even when mpsa_suite is missing from installed_apps. `handover.before_app_uninstall("mpsa_suite")` repeats the check. | `mpsa_suite/uninstall_guard.py`; `handover.before_app_uninstall` |
| R3 | Old app only (release C), see above. Not shipped under the new name. | `frappe_mpsa_payments/uninstall_guard.py` at `02d94a0` |
| R4 | Unit test: each of the 70 whitelisted functions of C has an entry, each target exists, is whitelisted and keeps C's guest setting. A bench test imports every target as Frappe's handler does. | `tests/unit/test_rename_safeguards.py`, `tests/unit/rename_whitelist_at_c.json` (made by `scripts/gen_whitelist_snapshot.py` from git); `mpsa_suite/test_rename_lookup.py` |
| R5 | The 6 Payment Entry STK fields are made on every migrate and at install, filed under `MODULE`; the patch now calls the same function. | `mpsa_suite/mpsa_suite/migrate.py` `create_payment_entry_mpesa_fields` |
| R6 | `rehome()` also moves `User.default_app` and System Settings `default_app` from the old app to mpsa_suite. | `handover.APP_FIELDS`, `SINGLE_APP_FIELDS` |
| R7 | The rename script and this note are kept in the repository. | `scripts/do_rename.py`, `docs/RENAME_SPIKE.md` |
| R8 | Each of the 8 registered patches returns at once when its twin `frappe_mpsa_payments.frappe_mpsa_payments.patches.*` is in Patch Log with `skipped = 0` (what Frappe itself counts as done). No patch was added. | `handover.ran_under_old_name`; first lines of each `execute()` in `mpsa_suite/mpsa_suite/patches/` |
| R9 | CI runs on pushes to `mpsa-suite`; the linters too. | `.github/workflows/ci.yml`, `.github/workflows/linter.yml` |

The lookup table (`override_whitelisted_methods` in `hooks.py`) stays for good: Safaricom holds
the registered C2B and Pull URLs under the old paths until they are re-registered.

## Evidence so far

**Frappe v16 spike** (throwaway site `rename16.localhost`, since dropped; outbound network
blocked; renamed copy made from an earlier working tree, f427f2b plus work not yet committed,
without R1 to R9). The site had the old
app with M-Pesa records, secrets, and, to mimic Astral, a site-made report, Server Script,
Client Script, Bank custom field holding a value and custom DocType with one row.

- Install mpsa_suite next to the old app (`install-app --force`, as Frappe Cloud runs it): 26
  shipped DocTypes and 44 of 45 custom fields moved to "Mpsa Suite"; both apps' 9 scheduled jobs
  existed side by side; 8 `mpsa_suite.*` Patch Log rows written, no patch ran.
- Uninstall the old app (`--yes --no-backup`): the hand-over hook moved the last 5 records
  (custom DocType, custom field, Server Script, report, Client Script); Frappe then removed only
  the empty Module Def "Frappe Mpsa Payments".
- After migrate: 27 DocTypes in "Mpsa Suite", 0 in the old module; 9 jobs, all `mpsa_suite.*`.
- Unchanged at every step: 11 rows in the M-Pesa tables, 56 + 9 Singles values, 5 secret rows,
  168 custom fields, the Bank field's value and the custom DocType's row.
- Old paths after the old app was gone: C2B validation, confirmation (through `/api/method/`
  and `/api/v2/method/`) and the STK callback returned 200, and their queued jobs ran under the
  new names and created the registers. An old path not in the table returned 417
  "App frappe_mpsa_payments is not installed".
- Without the hand-over hook the same uninstall would have dropped the custom DocType's table
  and deleted the report, both scripts and the Bank field.
- The bench files (apps.txt, apps.json, common_site_config.json) were restored to their
  original checksums afterwards.

**Not shown yet:** the same run on Frappe v15 (both live sites), R1 to R9 on a real site, the
Frappe Cloud dashboard flow, a site that came from Navari, HTTP posts to the other 14 Safaricom
paths, and live traffic during the switch. `docs/RENAME_PLAN.md` section 7 lists the rehearsals.
