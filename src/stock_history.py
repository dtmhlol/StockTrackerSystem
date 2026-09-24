"""
Stock history: an append-only record of every change, for auditing and later analysis.

One row per event, written in the SAME database transaction as the change itself, so the
history can never disagree with the stock. Rows are never updated or deleted by the app
(not even by "Reset App State"). Timestamps are local time with the UTC offset
(2026-09-25T14:03:11+10:00), which is unambiguous and sorts correctly.
"""
import csv
import json
import sqlite3
from datetime import datetime

# code -> label shown in the History window and used for filtering
EVENT_LABELS = {
    "SCAN_ADD": "Scan: stock added",
    "SCAN_REMOVE": "Scan: stock removed",
    "SCAN_HELD": "Scan: held for a decision",
    "EDIT": "Manual edit",
    "REMOVE": "Manual removal",
    "IMPORT": "Catalog import",
    "IMPORT_UNDO": "Import undone",
    "MERGE": "Unnamed stock merged",
    "BASELINE": "Starting stock",
    "RESET": "App reset",
    "SYSTEM": "Settings / system",
}

# Column order of the CSV export (and of the table)
COLUMNS = (
    ("id", "Event ID"),
    ("occurred_at", "Time"),
    ("event", "Event"),
    ("source", "Source"),
    ("sys_key", "Product Key"),
    ("product", "Product"),
    ("alias", "Alias / Barcode"),
    ("item_code", "Item Code"),
    ("raw_code", "Code Scanned"),
    ("expiry", "Expiry"),
    ("quantity_delta", "Change"),
    ("quantity_before", "Qty Before"),
    ("quantity_after", "Qty After"),
    ("scanned_at", "Scanned At (phone clock)"),
    ("note", "Note"),
    ("details", "Details (JSON)"),
)

_FIELDS = [key for key, _heading in COLUMNS]


def now_stamp():
    """Local time with UTC offset, to the second."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(conn):
    """Creates the history table if needed. Returns True if it was just created."""
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_history'"
    ).fetchone()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stock_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            event TEXT NOT NULL,
            source TEXT NOT NULL,
            sys_key INTEGER,
            product TEXT NOT NULL DEFAULT '',
            alias TEXT NOT NULL DEFAULT '',
            item_code TEXT NOT NULL DEFAULT '',
            raw_code TEXT NOT NULL DEFAULT '',
            expiry TEXT NOT NULL DEFAULT '',
            quantity_delta INTEGER,
            quantity_before INTEGER,
            quantity_after INTEGER,
            scanned_at TEXT,
            note TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT ''
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_time ON stock_history(occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_product ON stock_history(sys_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_event ON stock_history(event)")
    return not existed


def product_snapshot(conn, sys_key):
    """(name, alias, item code) as they are right now. Stored with each event, because
    products can be renamed later and the history must keep what it looked like then."""
    row = conn.execute(
        "SELECT description, alias, item_code FROM products WHERE sys_key = ?", (sys_key,)
    ).fetchone()
    return row if row else ("", "", "")


def record(conn, event, source, *, sys_key=None, product="", alias="", item_code="", raw_code="",
           expiry="", delta=None, before=None, after=None, scanned_at=None, note="", details=None):
    """Appends one event on `conn` (the caller commits together with the change itself)."""
    if event not in EVENT_LABELS:
        raise ValueError(f"Unknown history event: {event}")
    if sys_key is not None and not (product or alias or item_code):
        product, alias, item_code = product_snapshot(conn, sys_key)
    conn.execute(
        "INSERT INTO stock_history (occurred_at, event, source, sys_key, product, alias, item_code, "
        "raw_code, expiry, quantity_delta, quantity_before, quantity_after, scanned_at, note, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_stamp(), event, source, sys_key, product or "", alias or "", item_code or "", raw_code or "",
         expiry or "", delta, before, after, scanned_at, note or "",
         json.dumps(details, ensure_ascii=False, sort_keys=True) if details else ""),
    )


# ------------------------------------------------------------------- reading

def _where(date_from, date_to, events, search):
    clauses, params = [], []
    if date_from:
        clauses.append("substr(occurred_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("substr(occurred_at, 1, 10) <= ?")
        params.append(date_to)
    if events is not None:
        if not events:
            return "WHERE 0", []
        clauses.append(f"event IN ({','.join('?' * len(events))})")
        params += sorted(events)
    if search and search.strip():
        like = f"%{search.strip()}%"
        clauses.append("(product LIKE ? OR alias LIKE ? OR item_code LIKE ? OR raw_code LIKE ? OR note LIKE ?)")
        params += [like] * 5
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def count(db_path, date_from=None, date_to=None, events=None, search=""):
    where, params = _where(date_from, date_to, events, search)
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM stock_history {where}", params).fetchone()[0]
    finally:
        conn.close()


def query(db_path, date_from=None, date_to=None, events=None, search="", limit=None, newest_first=True):
    """Matching events as dicts. date_from/date_to are 'YYYY-MM-DD' (inclusive, local date);
    events is a set of event codes (None = all); search matches product, codes and notes."""
    where, params = _where(date_from, date_to, events, search)
    order = "DESC" if newest_first else "ASC"
    sql = f"SELECT {', '.join(_FIELDS)} FROM stock_history {where} ORDER BY id {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = sqlite3.connect(db_path)
    try:
        return [dict(zip(_FIELDS, row)) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _excel_text(value):
    value = str(value)
    return f'="{value}"' if value.isascii() and value.isdigit() else value


def write_csv(path, rows, excel_friendly=False):
    """Writes events (oldest first is best for analysis) as UTF-8 CSV with a BOM.
    Plain values by default, which is what analysis tools want; excel_friendly keeps
    long barcodes and leading zeros intact when the file is opened in Excel."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([heading for _key, heading in COLUMNS])
        for row in rows:
            values = []
            for key in _FIELDS:
                value = row[key]
                # The event column keeps the stable code (SCAN_ADD, ...) so analysis scripts don't
                # break if a display label is ever reworded.
                if excel_friendly and key in ("alias", "item_code", "raw_code"):
                    value = _excel_text(value)
                values.append("" if value is None else value)
            writer.writerow(values)
