"""M-PESA Org Portal statements: one file per account, read and sorted into kinds of movement.

The portal exports each account of a shortcode on its own ("ORG_<shortcode>_<Account>_All_
<stamp>.xls": Utility Account, MMF Account for Organization, Merchant Account...). Above the
table a header block names the shortcode, the account, the period and the opening and closing
balances; the table has one row per movement, newest first, with the balance after it. A
payout and its Safaricom charge are two rows sharing one receipt; a transfer between two
accounts of the shortcode is a row on each account's statement under one receipt.

The portal's combined export ("Statements for <shortcode> from ... to ....xlsx") has no account
column, so a row cannot be put on its account: it is refused.

Pure Python (xlrd / openpyxl / csv), so it is tested without a bench.
"""

import csv
import datetime
import io
import re
from dataclasses import dataclass, field

SUB_ACCOUNTS = ("Utility", "Working", "Merchant", "Charges Paid", "Organization Settlement", "Float")

XLS_MAGIC = b"\xd0\xcf\x11\xe0"
XLSX_MAGIC = b"PK"
SEPARATE_EXPORTS = (
    "Export each account separately from the M-PESA Org Portal (Utility Account, MMF/Working Account, "
    "Merchant Account...) and import those files: this one has no account column, so its rows cannot be put "
    "on their account."
)


class StatementError(ValueError):
    pass


# ---------------------------------------------------------------- names
_ACCOUNT_NAMES = {
    "utility": "Utility",
    "mmf": "Working",
    "working": "Working",
    "merchant": "Merchant",
    "chargespaid": "Charges Paid",
    "organizationsettlement": "Organization Settlement",
    "organisationsettlement": "Organization Settlement",
    "float": "Float",
}


def _squash(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def sub_account_key(name) -> str | None:
    """Portal / Daraja account name -> sub-account key: "MMF Account for Organization" -> "Working"."""
    text = _squash(name)
    for suffix in ("accountfororganization", "accountfororganisation", "controlaccount", "account"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return _ACCOUNT_NAMES.get(text)


# ---------------------------------------------------------------- rows
@dataclass
class Row:
    receipt: str
    completed_at: datetime.datetime | None
    initiated_at: datetime.datetime | None
    details: str
    status: str
    paid_in: float
    withdrawn: float  # always positive: the portal prints it negative
    balance: float | None
    reason_type: str = ""
    other_party: str = ""
    linked_id: str = ""
    account_no: str = ""
    currency: str = ""

    @property
    def amount(self) -> float:
        """Signed: money in positive, money out negative."""
        return round(self.paid_in - self.withdrawn, 2)

    @property
    def completed(self) -> bool:
        return self.status.strip().lower() == "completed"


@dataclass
class Statement:
    shortcode: str
    holder: str
    account_name: str
    sub_account: str
    period_from: datetime.datetime | None
    period_to: datetime.datetime | None
    opening_balance: float | None
    closing_balance: float | None
    total_paid_in: float | None
    total_withdrawn: float | None
    rows: list = field(default_factory=list)


@dataclass
class Kind:
    kind: str
    api: str | None = None
    from_sub: str | None = None
    to_sub: str | None = None


# ---------------------------------------------------------------- reading
def read(content: bytes, filename: str = "") -> Statement:
    """Parse one per-account portal export (.xls, .xlsx or .csv of the same layout)."""
    if not content:
        raise StatementError("The statement file is empty")
    name = (filename or "").lower()
    if content[:4] == XLS_MAGIC or (content[:2] != XLSX_MAGIC and name.endswith(".xls")):
        grid = _grid_from_xls(content)
    elif content[:2] == XLSX_MAGIC or name.endswith(".xlsx"):
        grid = _grid_from_xlsx(content)
    else:
        grid = _grid_from_csv(content)
    return _parse(grid)


def _grid_from_xls(content: bytes) -> list:
    import xlrd

    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    grid = []
    for r in range(sheet.nrows):
        row = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
            else:
                row.append(cell.value)
        grid.append(row)
    return grid


def _grid_from_xlsx(content: bytes) -> list:
    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        return [["" if v is None else v for v in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()


def _grid_from_csv(content: bytes) -> list:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    return [row for row in csv.reader(io.StringIO(text))]


COLUMNS = {
    "receiptno": "receipt",
    "completiontime": "completed_at",
    "initiationtime": "initiated_at",
    "details": "details",
    "transactionstatus": "status",
    "paidin": "paid_in",
    "withdrawn": "withdrawn",
    "balance": "balance",
    "reasontype": "reason_type",
    "otherpartyinfo": "other_party",
    "linkedtransactionid": "linked_id",
    "acno": "account_no",
    "currency": "currency",
}


def _column_key(header) -> str:
    text = _squash(header)
    for suffix in ("kshs", "ksh", "kes"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _parse(grid: list) -> Statement:
    header_at = next(
        (i for i, row in enumerate(grid) if row and _squash(_first(row)).startswith("receiptno")), None
    )
    if header_at is None:
        raise StatementError("No 'Receipt No.' header row: this is not an M-PESA Org Portal statement")
    columns = {}
    for c, header in enumerate(grid[header_at]):
        key = COLUMNS.get(_column_key(header))
        if key and key not in columns:
            columns[key] = c

    labels = _labels(grid[:header_at])
    if "account" not in labels:
        raise StatementError(SEPARATE_EXPORTS)
    account_name = _text(labels["account"])
    sub_account = sub_account_key(account_name)
    if not sub_account:
        raise StatementError(f"Unknown M-PESA account {account_name!r} in the statement header")
    missing = {"receipt", "completed_at", "details", "status", "paid_in", "withdrawn"} - set(columns)
    if missing:
        raise StatementError(f"The statement has no column for {', '.join(sorted(missing))}")

    rows = []
    for raw in grid[header_at + 1:]:
        get = lambda key: raw[columns[key]] if key in columns and columns[key] < len(raw) else ""  # noqa: E731
        receipt = _text(get("receipt"))
        if not receipt:
            continue
        rows.append(
            Row(
                receipt=receipt,
                completed_at=_when(get("completed_at")),
                initiated_at=_when(get("initiated_at")),
                details=_text(get("details")),
                status=_text(get("status")),
                paid_in=abs(_number(get("paid_in")) or 0.0),
                withdrawn=abs(_number(get("withdrawn")) or 0.0),
                balance=_number(get("balance")),
                reason_type=_text(get("reason_type")),
                other_party=_text(get("other_party")),
                linked_id=_text(get("linked_id")),
                account_no=_text(get("account_no")),
                currency=_text(get("currency")),
            )
        )

    withdrawn = _number(labels.get("total withdrawn"))
    return Statement(
        shortcode=_code(labels.get("short code")),
        holder=_text(labels.get("account holder")),
        account_name=account_name,
        sub_account=sub_account,
        period_from=_when(labels.get("time period from")),
        period_to=_when(labels.get("time period to")),
        opening_balance=_number(labels.get("opening balance")),
        closing_balance=_number(labels.get("closing balance")),
        total_paid_in=_number(labels.get("total paid in")),
        total_withdrawn=abs(withdrawn) if withdrawn is not None else None,
        rows=rows,
    )


def _labels(rows: list) -> dict:
    """The header block: "Label:" cells followed by their value, several pairs per row.
    "Time Period:" reads From <date> To <date>."""
    labels = {}
    for row in rows:
        cells = [c for c in row if _text(c) != ""]
        i = 0
        while i < len(cells):
            label = _text(cells[i])
            if not label.endswith(":"):
                i += 1
                continue
            key = label[:-1].strip().lower()
            if key == "time period":
                rest = [_text(c) for c in cells[i + 1:]]
                for word, target in (("from", "time period from"), ("to", "time period to")):
                    at = next((j for j, v in enumerate(rest) if v.lower() == word), None)
                    if at is not None and at + 1 < len(rest):
                        labels[target] = rest[at + 1]
                break
            following = cells[i + 1] if i + 1 < len(cells) else ""
            if _text(following).endswith(":"):
                labels[key] = ""  # empty value (e.g. Available Balance)
                i += 1
                continue
            labels[key] = following
            i += 2
    return labels


# ---------------------------------------------------------------- values
def _first(row) -> str:
    return next((_text(c) for c in row if _text(c) != ""), "")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _code(value) -> str:
    return re.sub(r"\D", "", _text(value))


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).replace(",", "").replace("KES", "").replace("KSh", "").strip()
    if text in ("", "-"):
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
                "%d-%m-%Y", "%d/%m/%Y")


def _when(value) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    text = _text(value)
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- kinds
_ACCOUNT = r"(utility|mmf|working|merchant|organi[sz]ation\s+settlement|charges\s+paid|float)"
_TRANSFER = re.compile(
    rf"\b{_ACCOUNT}\s+account(?:\s+for\s+organi[sz]ation)?\s+to\s+(?:the\s+)?{_ACCOUNT}\s+account", re.I
)
B2B_REASONS = ("business pay bill", "business buy goods")
B2C_REASONS = ("salary payment", "business payment", "promotion payment")


def transfer_sides(text) -> tuple | None:
    """("Utility", "Working") for "Funds Transfer from Utility Account to Working Account"."""
    match = _TRANSFER.search(str(text or ""))
    if not match:
        return None
    return sub_account_key(match.group(1)), sub_account_key(match.group(2))


def _api(*texts) -> str | None:
    text = " ".join(t.lower() for t in texts if t)
    if any(r in text for r in B2B_REASONS):
        return "B2B"
    if any(r in text for r in B2C_REASONS) or text.startswith("b2c"):
        return "B2C"
    return None


def classify(row: Row, shortcode: str, sub_account: str) -> Kind:
    """What one row of ``sub_account``'s statement is (see the module docstring)."""
    if not row.completed:
        return Kind("skipped")
    details, reason = row.details.strip(), row.reason_type.strip()
    money_out = row.amount < 0

    if details.lower().endswith("charge"):
        return Kind("charge", api=_api(reason) or _api(details))

    sides = transfer_sides(details) or transfer_sides(reason)
    own = bool(shortcode) and _code(row.other_party.split("-")[0]) == _code(shortcode)
    if sides or own:
        from_sub, to_sub = sides or (None, None)
        if money_out:
            from_sub = from_sub or sub_account
        else:
            to_sub = to_sub or sub_account
        return Kind("transfer", from_sub=from_sub, to_sub=to_sub)

    if "reversal" in f"{details} {reason}".lower():
        return Kind("reversal", api=_api(reason))
    api = _api(reason)
    if money_out and api:
        return Kind("payout", api=api)
    if not money_out and row.amount > 0:
        if reason.lower() == "organization deposit of funds" or details.lower().startswith("deposit into"):
            return Kind("funding")
        return Kind("receipt")
    if money_out:
        return Kind("withdrawal")
    return Kind("other")
