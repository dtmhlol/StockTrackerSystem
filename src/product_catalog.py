"""
Product catalog for Stock Tracker.

Maps whatever identifier a scan carries (an alias/barcode or an internal item
code) to one product, keyed by a stable `sys_key`. A product's identity is the
(item code, alias) pair: re-importing the same pair updates its description,
while a code or alias that shows up under a *different* pair becomes a
separate product, so scanning the shared identifier is ambiguous and is
queued for the user to resolve by name.

Lookups go through an in-memory hash table (identifier -> set of sys_keys)
backed by indexed SQLite tables.
"""
import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime

PLACEHOLDER_LABEL = "(unnamed product)"
MAPPING_FIELDS = ("alias", "item_code", "description")

# Excel renders long numbers such as barcodes as 9.32877E+12 once the cell is
# too narrow, permanently losing digits. Real identifiers never look like this.
_SCI_NOTATION = re.compile(r"^\d(?:\.\d+)?[eE]\+\d{2,}$")
_YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DELIMITERS = (",", "\t", ";", "|")
_SAMPLE_LIMIT = 8


def _now():
    return datetime.now().isoformat(timespec="seconds")


def looks_like_scientific_notation(value):
    return bool(_SCI_NOTATION.match(str(value or "").strip()))


def normalize_identifier(value):
    """Canonical form used for every comparison: trimmed, upper-cased, and for
    all-digit values stripped of leading zeros (so a UPC-A and its EAN-13 form,
    or a barcode whose zeros Excel dropped, are the same identifier). Values
    that are blank or corrupted by scientific notation normalize to ''."""
    text = str(value or "").strip()
    if not text or looks_like_scientific_notation(text):
        return ""
    text = text.upper()
    if text.isascii() and text.isdigit():
        text = text.lstrip("0") or "0"
    return text


# ---------------------------------------------------------------- CSV files

def _read_text(path):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, newline="") as f:
                return encoding, f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode the file.")


def read_table(path):
    """Returns (headers, data_rows, delimiter, encoding). The first row is
    treated as the header row. Delimiter is detected from that row, so comma,
    tab, semicolon and pipe separated exports all work."""
    encoding, text = _read_text(path)
    first_line = text.split("\n", 1)[0]
    delimiter = max(_DELIMITERS, key=first_line.count)
    if first_line.count(delimiter) == 0:
        delimiter = ","

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], [], delimiter, encoding
    return [h.strip() for h in rows[0]], rows[1:], delimiter, encoding


_GUESS_KEYWORDS = {
    "item_code": ("item code", "itemcode", "sku", "product code", "code"),
    "alias": ("alias", "barcode", "ean", "upc", "gtin"),
    "description": ("item description", "description", "product name", "name"),
}


def guess_mapping(headers, saved=None):
    """Suggests which column is which field: first the header names the user
    picked last time (`saved`, field -> header name), then keyword matching.
    Returns field -> column index (or None)."""
    folded = [h.casefold() for h in headers]
    saved = saved or {}
    mapping, used = {}, set()

    for field in MAPPING_FIELDS:
        index = None
        wanted = str(saved.get(field) or "").casefold()
        if wanted in folded and folded.index(wanted) not in used:
            index = folded.index(wanted)

        for exact in (True, False):
            for keyword in _GUESS_KEYWORDS[field]:
                if index is not None:
                    break
                for i, header in enumerate(folded):
                    if i in used:
                        continue
                    if (header == keyword) if exact else (keyword in header):
                        index = i
                        break

        mapping[field] = index
        if index is not None:
            used.add(index)
    return mapping


def validate_mapping(mapping):
    """Returns an error message, or None if the mapping is usable."""
    if mapping.get("alias") is None and mapping.get("item_code") is None:
        return "Choose a column for at least one of Alias or Item Code."
    chosen = [i for i in mapping.values() if i is not None]
    if len(chosen) != len(set(chosen)):
        return "The same column can't be used for two different fields."
    return None


def parse_rows(data_rows, mapping):
    """Turns raw CSV rows into {(code_key, alias_key): record}. Rows repeating
    an already-seen pair are merged (last non-blank description wins).
    Returns (records, stats)."""
    stats = {"rows": 0, "skipped_blank": 0, "sci_notation": 0, "duplicates_in_file": 0}
    records = {}

    def cell(row, index):
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    for row in data_rows:
        stats["rows"] += 1
        code_raw = cell(row, mapping.get("item_code"))
        alias_raw = cell(row, mapping.get("alias"))
        description = cell(row, mapping.get("description"))

        if looks_like_scientific_notation(code_raw):
            stats["sci_notation"] += 1
            code_raw = ""
        if looks_like_scientific_notation(alias_raw):
            stats["sci_notation"] += 1
            alias_raw = ""

        code_key = normalize_identifier(code_raw)
        alias_key = normalize_identifier(alias_raw)
        if not code_key and not alias_key:
            stats["skipped_blank"] += 1
            continue

        pair = (code_key, alias_key)
        previous = records.get(pair)
        if previous is not None:
            stats["duplicates_in_file"] += 1
            description = description or previous["description"]
        records[pair] = {"item_code": code_raw, "alias": alias_raw, "description": description}

    return records, stats


def format_import_summary(result):
    """Human-readable summary of an import (or a dry run of one)."""
    lines = [
        f"Rows read: {result['rows']:,}   (unique products in file: {result['unique_products']:,})",
        f"New products: {result['new']:,}",
        f"Descriptions updated: {result['updated']:,}",
        f"Unnamed products now matched to real ones: {result['upgraded']:,}",
        f"Unchanged: {result['unchanged']:,}",
    ]
    if result["duplicates_in_file"]:
        lines.append(f"Repeated rows merged: {result['duplicates_in_file']:,}")
    if result["skipped_blank"]:
        lines.append(f"Skipped (no alias and no item code): {result['skipped_blank']:,}")
    if result["sci_notation"]:
        lines.append(
            f"Values ignored because Excel turned them into scientific notation "
            f"(e.g. 9.32877E+12): {result['sci_notation']:,}"
        )
    if result["absorbed"] or result["absorbed_left"]:
        lines.append(
            f"Stock recorded under unnamed products merged into named ones: {result['absorbed']:,}"
            + (f"  ({result['absorbed_left']:,} left because they match several products)"
               if result["absorbed_left"] else "")
        )
    lines.append(
        f"Identifiers shared by several products (asked on scan): "
        f"{result['ambiguous_before']:,} -> {result['ambiguous_after']:,}"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ catalog

class ProductCatalog:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lookup = {}          # normalized identifier -> {sys_key, ...}
        self._placeholders = set() # sys_keys of unnamed products
        self.ensure_schema()
        self.reload_index()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    # -- schema & migration ------------------------------------------------

    def ensure_schema(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS products (
                    sys_key INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_code TEXT NOT NULL DEFAULT '',
                    alias TEXT NOT NULL DEFAULT '',
                    code_key TEXT NOT NULL DEFAULT '',
                    alias_key TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    is_placeholder INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (code_key, alias_key)
                )
            ''')
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_code_key ON products(code_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_products_alias_key ON products(alias_key)")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS pending_scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL,
                    action TEXT NOT NULL,
                    expiry TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    scanned_at TEXT,
                    created_at TEXT NOT NULL
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS import_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT,
                    imported_at TEXT NOT NULL,
                    summary TEXT,
                    inventory_revision INTEGER NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0
                )
            ''')

            columns = [row[1] for row in cur.execute("PRAGMA table_info(inventory)")]
            if columns:
                if "sys_key" not in columns:
                    self._backup_before_migration(conn)
                    cur.execute("ALTER TABLE inventory ADD COLUMN sys_key INTEGER")
                self._migrate_inventory_rows(conn)
                cur.execute("DROP INDEX IF EXISTS idx_inventory_barcode_expiry")
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_inventory_sys_key_expiry "
                    "ON inventory(sys_key, expiry_date)"
                )
            conn.commit()
        finally:
            conn.close()

    def _backup_before_migration(self, conn):
        """Keeps a copy of the database from before inventory was re-keyed by
        product, once, and only if there is stock worth protecting."""
        if conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
            return
        backup_path = self.db_path + ".pre-catalog.bak"
        if os.path.exists(backup_path):
            return
        conn.commit()
        target = sqlite3.connect(backup_path)
        try:
            conn.backup(target)
        finally:
            target.close()

    def _migrate_inventory_rows(self, conn):
        """Assigns a product to every legacy inventory row (which only knows
        its raw barcode) and merges rows that now collide on product+expiry."""
        cur = conn.cursor()
        legacy = cur.execute("SELECT id, barcode FROM inventory WHERE sys_key IS NULL").fetchall()
        if not legacy:
            return

        for row_id, barcode in legacy:
            sys_key = self._get_or_create_placeholder(conn, barcode)
            cur.execute("UPDATE inventory SET sys_key = ? WHERE id = ?", (sys_key, row_id))

        duplicates = cur.execute(
            "SELECT sys_key, expiry_date, SUM(quantity), MIN(id) FROM inventory "
            "GROUP BY sys_key, expiry_date HAVING COUNT(*) > 1"
        ).fetchall()
        for sys_key, expiry, total, keep_id in duplicates:
            cur.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (total, keep_id))
            cur.execute(
                "DELETE FROM inventory WHERE sys_key = ? AND expiry_date = ? AND id <> ?",
                (sys_key, expiry, keep_id),
            )

    # -- in-memory hash index ---------------------------------------------

    def reload_index(self):
        lookup, placeholders = {}, set()
        conn = self._connect()
        try:
            for sys_key, code_key, alias_key, is_placeholder in conn.execute(
                "SELECT sys_key, code_key, alias_key, is_placeholder FROM products"
            ):
                for key in {code_key, alias_key}:
                    if key:
                        lookup.setdefault(key, set()).add(sys_key)
                if is_placeholder:
                    placeholders.add(sys_key)
        finally:
            conn.close()
        self._lookup, self._placeholders = lookup, placeholders

    def _index_add(self, sys_key, code_key, alias_key, placeholder):
        for key in {code_key, alias_key}:
            if key:
                self._lookup.setdefault(key, set()).add(sys_key)
        if placeholder:
            self._placeholders.add(sys_key)

    def resolve(self, identifier):
        """O(1) lookup: the sys_keys of every product this alias or item code
        could refer to. Named products win over unnamed ones."""
        keys = self._lookup.get(normalize_identifier(identifier))
        if not keys:
            return []
        real = [k for k in keys if k not in self._placeholders]
        return sorted(real or keys)

    # -- revision counter (guards undo) -----------------------------------

    def get_revision(self, conn=None):
        own = conn is None
        conn = conn or self._connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = 'inventory_revision'").fetchone()
            return int(row[0]) if row and row[0] else 0
        finally:
            if own:
                conn.close()

    def bump_inventory_revision(self, conn=None):
        """Call whenever stock changes, so an import can only be undone while
        the stock it left behind is still untouched."""
        own = conn is None
        conn = conn or self._connect()
        try:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('inventory_revision', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
            )
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()

    # -- scans -------------------------------------------------------------

    def _get_or_create_placeholder(self, conn, raw_identifier):
        key = normalize_identifier(raw_identifier)
        row = conn.execute(
            "SELECT sys_key FROM products WHERE code_key = '' AND alias_key = ?", (key,)
        ).fetchone()
        if row:
            return row[0]

        now = _now()
        cursor = conn.execute(
            "INSERT INTO products (item_code, alias, code_key, alias_key, description, "
            "is_placeholder, created_at, updated_at) VALUES ('', ?, '', ?, '', 1, ?, ?)",
            (str(raw_identifier or "").strip(), key, now, now),
        )
        self._index_add(cursor.lastrowid, "", key, placeholder=True)
        return cursor.lastrowid

    def _apply_delta(self, conn, sys_key, barcode, action, expiry, quantity):
        delta = quantity if action == "ADD" else -quantity
        conn.execute(
            "INSERT INTO inventory (barcode, expiry_date, quantity, last_updated, sys_key) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(sys_key, expiry_date) DO UPDATE SET "
            "quantity = quantity + excluded.quantity, last_updated = excluded.last_updated",
            (barcode, expiry, delta, datetime.now().isoformat(), sys_key),
        )
        self.bump_inventory_revision(conn)

    def apply_scan(self, conn, barcode, action, expiry, quantity, scanned_at=None):
        """Applies one scan on `conn` (the caller commits). Returns 'applied',
        'unnamed' (unknown identifier, recorded under a placeholder product) or
        'pending' (matches several products; queued for the user to resolve).
        If the caller's transaction fails, it should call reload_index()."""
        candidates = self.resolve(barcode)

        if len(candidates) > 1:
            conn.execute(
                "INSERT INTO pending_scans (barcode, action, expiry, quantity, scanned_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (barcode, action, expiry, quantity, scanned_at, _now()),
            )
            return "pending"

        status = "applied"
        if candidates:
            sys_key = candidates[0]
        else:
            sys_key = self._get_or_create_placeholder(conn, barcode)
            status = "unnamed"

        self._apply_delta(conn, sys_key, barcode, action, expiry, quantity)
        return status

    # -- pending scans -----------------------------------------------------

    def pending_count(self):
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM pending_scans").fetchone()[0]
        finally:
            conn.close()

    def list_pending(self):
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, barcode, action, expiry, quantity, scanned_at FROM pending_scans ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        keys = ("id", "barcode", "action", "expiry", "quantity", "scanned_at")
        return [dict(zip(keys, row)) for row in rows]

    def candidates_for(self, barcode):
        return self.get_products(self.resolve(barcode))

    def resolve_pending(self, pending_id, sys_key=None):
        """Applies a queued scan to the chosen product (None = record it under
        an unnamed product). Returns False if the scan no longer exists."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT barcode, action, expiry, quantity FROM pending_scans WHERE id = ?", (pending_id,)
            ).fetchone()
            if not row:
                return False
            barcode, action, expiry, quantity = row

            if sys_key is None:
                sys_key = self._get_or_create_placeholder(conn, barcode)
            elif not conn.execute("SELECT 1 FROM products WHERE sys_key = ?", (sys_key,)).fetchone():
                raise ValueError("That product no longer exists.")

            self._apply_delta(conn, sys_key, barcode, action, expiry, quantity)
            conn.execute("DELETE FROM pending_scans WHERE id = ?", (pending_id,))
            conn.execute("DELETE FROM inventory WHERE quantity <= 0")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            self.reload_index()
            raise
        finally:
            conn.close()

    # -- browsing ----------------------------------------------------------

    @staticmethod
    def _product_dict(row):
        keys = ("sys_key", "item_code", "alias", "description", "is_placeholder")
        product = dict(zip(keys, row))
        product["is_placeholder"] = bool(product["is_placeholder"])
        return product

    def get_products(self, sys_keys):
        sys_keys = list(sys_keys)
        if not sys_keys:
            return []
        conn = self._connect()
        try:
            marks = ",".join("?" * len(sys_keys))
            rows = conn.execute(
                f"SELECT sys_key, item_code, alias, description, is_placeholder "
                f"FROM products WHERE sys_key IN ({marks})", sys_keys
            ).fetchall()
        finally:
            conn.close()
        return [self._product_dict(row) for row in rows]

    def search_products(self, text="", limit=500):
        like = f"%{text.strip()}%"
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT sys_key, item_code, alias, description, is_placeholder FROM products "
                "WHERE description LIKE ? OR item_code LIKE ? OR alias LIKE ? "
                "ORDER BY is_placeholder, description COLLATE NOCASE LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        finally:
            conn.close()
        return [self._product_dict(row) for row in rows]

    def stats(self):
        conn = self._connect()
        try:
            total, unnamed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_placeholder), 0) FROM products"
            ).fetchone()
        finally:
            conn.close()
        return {"total": total, "unnamed": unnamed}

    # -- manual edits ------------------------------------------------------

    def get_stock_row(self, inventory_id):
        """One stock row with its product's details, or None if it's gone."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT i.id, i.sys_key, i.expiry_date, i.quantity, p.item_code, p.alias, "
                "p.description, p.is_placeholder, "
                "(SELECT COUNT(*) FROM inventory WHERE sys_key = i.sys_key) "
                "FROM inventory i JOIN products p ON p.sys_key = i.sys_key WHERE i.id = ?",
                (inventory_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        keys = ("id", "sys_key", "expiry", "quantity", "item_code", "alias",
                "description", "is_placeholder", "stock_rows")
        return dict(zip(keys, row))

    def identifier_sharing(self, sys_key, item_code, alias):
        """Other products that already use this item code or alias (so a scan of it
        would have to ask which product is meant)."""
        keys = sorted({k for k in (normalize_identifier(item_code), normalize_identifier(alias)) if k})
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT sys_key, item_code, alias, description, is_placeholder FROM products "
                f"WHERE sys_key <> ? AND (code_key IN ({marks}) OR alias_key IN ({marks}))",
                [sys_key, *keys, *keys],
            ).fetchall()
        finally:
            conn.close()
        return [self._product_dict(row) for row in rows]

    def edit_stock_row(self, inventory_id, description, item_code, alias, expiry, quantity, dry_run=False):
        """Applies a manual edit. Name, item code and alias belong to the product,
        so they change everywhere; expiry and quantity change only this stock row.
        Raises ValueError with a user-readable message (and changes nothing) if the
        values are invalid or would collide with an existing product or stock row.
        With dry_run=True it runs every check and rolls back, so a caller can validate
        an edit (and ask for confirmation) before really applying it."""
        description = (description or "").strip()
        item_code = (item_code or "").strip()
        alias = (alias or "").strip()
        expiry = (expiry or "").strip()

        if not _YEAR_MONTH.match(expiry):
            raise ValueError("Expiry must look like YYYY-MM, for example 2027-03.")
        try:
            quantity = int(str(quantity).strip())
        except ValueError:
            raise ValueError("Quantity must be a whole number.")
        if quantity < 1:
            raise ValueError("Quantity must be at least 1. Use Remove selected to delete a stock row.")
        for label, value in (("Item code", item_code), ("Alias", alias)):
            if looks_like_scientific_notation(value):
                raise ValueError(
                    f"{label} looks like a number Excel has shortened (scientific notation). "
                    "Enter the full digits."
                )
        code_key, alias_key = normalize_identifier(item_code), normalize_identifier(alias)
        if not code_key and not alias_key:
            raise ValueError("Enter an alias or an item code so the product can be identified.")

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT i.sys_key, i.expiry_date, p.code_key, p.alias_key, p.is_placeholder "
                "FROM inventory i JOIN products p ON p.sys_key = i.sys_key WHERE i.id = ?",
                (inventory_id,),
            ).fetchone()
            if not row:
                raise ValueError("That stock row no longer exists. Refresh and try again.")
            sys_key, old_expiry, old_code_key, old_alias_key, old_placeholder = row

            if (code_key, alias_key) != (old_code_key, old_alias_key):
                clash = conn.execute(
                    "SELECT description FROM products WHERE code_key = ? AND alias_key = ? AND sys_key <> ?",
                    (code_key, alias_key, sys_key),
                ).fetchone()
                if clash:
                    raise ValueError(
                        "Another product already has exactly this item code and alias: "
                        f"{clash[0] or PLACEHOLDER_LABEL}. Change one of them."
                    )

            if expiry != old_expiry:
                clash = conn.execute(
                    "SELECT 1 FROM inventory WHERE sys_key = ? AND expiry_date = ? AND id <> ?",
                    (sys_key, expiry, inventory_id),
                ).fetchone()
                if clash:
                    raise ValueError(
                        f"This product already has a stock row expiring {expiry}. "
                        "Edit that row's quantity instead."
                    )

            # Giving a product a name makes it a real product, so a later import
            # won't fold it into a different one as if it were an unnamed scan.
            is_placeholder = 0 if description else old_placeholder
            conn.execute(
                "UPDATE products SET item_code = ?, alias = ?, code_key = ?, alias_key = ?, "
                "description = ?, is_placeholder = ?, updated_at = ? WHERE sys_key = ?",
                (item_code, alias, code_key, alias_key, description, is_placeholder, _now(), sys_key),
            )
            conn.execute(
                "UPDATE inventory SET expiry_date = ?, quantity = ?, last_updated = ? WHERE id = ?",
                (expiry, quantity, datetime.now().isoformat(), inventory_id),
            )
            if dry_run:
                conn.rollback()
                return
            self.bump_inventory_revision(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.reload_index()

    # -- column mapping memory --------------------------------------------

    def save_mapping(self, headers, mapping):
        named = {
            field: (headers[index] if index is not None and index < len(headers) else None)
            for field, index in mapping.items()
        }
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('catalog_column_mapping', ?)",
                (json.dumps(named),),
            )
            conn.commit()
        finally:
            conn.close()

    def load_saved_mapping(self):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = 'catalog_column_mapping'").fetchone()
        finally:
            conn.close()
        try:
            return json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            return {}

    # -- import / undo -----------------------------------------------------

    def import_records(self, records, parse_stats, filename, dry_run=False):
        """Applies parsed records to the catalog. With dry_run=True the exact
        same code runs and is then rolled back, so the preview can never
        disagree with what the real import does."""
        conn = self._connect()
        try:
            if not dry_run:
                self._snapshot(conn)

            result = self._run_import(conn, records)
            result.update(parse_stats)
            result["unique_products"] = len(records)

            if dry_run:
                conn.rollback()
            else:
                self.bump_inventory_revision(conn)
                summary = {k: v for k, v in result.items() if k != "samples"}
                conn.execute(
                    "INSERT INTO import_batches (filename, imported_at, summary, inventory_revision) "
                    "VALUES (?, ?, ?, ?)",
                    (filename, _now(), json.dumps(summary), self.get_revision(conn)),
                )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if not dry_run:
            self.reload_index()
        return result

    def _snapshot(self, conn):
        """Keeps a copy of products and stock from just before the import,
        for undo. Only the most recent import can be undone."""
        conn.commit()
        for table in ("products", "inventory"):
            conn.execute(f"DROP TABLE IF EXISTS snap_{table}")
            conn.execute(f"CREATE TABLE snap_{table} AS SELECT * FROM {table}")
        conn.commit()

    def _count_ambiguous(self, conn):
        return conn.execute('''
            SELECT COUNT(*) FROM (
                SELECT key FROM (
                    SELECT code_key AS key, sys_key FROM products WHERE is_placeholder = 0 AND code_key <> ''
                    UNION
                    SELECT alias_key AS key, sys_key FROM products WHERE is_placeholder = 0 AND alias_key <> ''
                ) GROUP BY key HAVING COUNT(DISTINCT sys_key) > 1
            )
        ''').fetchone()[0]

    def _merge_inventory(self, conn, from_key, to_key):
        rows = conn.execute(
            "SELECT barcode, expiry_date, quantity, last_updated FROM inventory WHERE sys_key = ?",
            (from_key,),
        ).fetchall()
        for barcode, expiry, quantity, updated in rows:
            conn.execute(
                "INSERT INTO inventory (barcode, expiry_date, quantity, last_updated, sys_key) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(sys_key, expiry_date) DO UPDATE SET "
                "quantity = quantity + excluded.quantity, last_updated = excluded.last_updated",
                (barcode, expiry, quantity, updated, to_key),
            )
        conn.execute("DELETE FROM inventory WHERE sys_key = ?", (from_key,))

    def _run_import(self, conn, records):
        ambiguous_before = self._count_ambiguous(conn)
        existing = {
            (code_key, alias_key): (sys_key, description, is_placeholder)
            for sys_key, code_key, alias_key, description, is_placeholder in conn.execute(
                "SELECT sys_key, code_key, alias_key, description, is_placeholder FROM products"
            )
        }

        now = _now()
        new_rows, description_updates, upgrades = [], [], []
        samples = {"new": [], "updated": []}
        unchanged = 0

        for (code_key, alias_key), record in records.items():
            found = existing.get((code_key, alias_key))
            if found is None:
                new_rows.append((record["item_code"], record["alias"], code_key, alias_key,
                                 record["description"], now, now))
                if len(samples["new"]) < _SAMPLE_LIMIT:
                    samples["new"].append((record["description"], record["item_code"], record["alias"]))
                continue

            sys_key, old_description, is_placeholder = found
            # A blank description in the file never wipes out an existing name.
            new_description = record["description"] or old_description
            if is_placeholder:
                upgrades.append((record["item_code"], record["alias"], new_description, now, sys_key))
            elif new_description != old_description:
                description_updates.append((new_description, now, sys_key))
                if len(samples["updated"]) < _SAMPLE_LIMIT:
                    samples["updated"].append((old_description, new_description))
            else:
                unchanged += 1

        conn.executemany(
            "INSERT INTO products (item_code, alias, code_key, alias_key, description, "
            "is_placeholder, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)", new_rows)
        conn.executemany(
            "UPDATE products SET description = ?, updated_at = ? WHERE sys_key = ?", description_updates)
        conn.executemany(
            "UPDATE products SET item_code = ?, alias = ?, description = ?, is_placeholder = 0, "
            "updated_at = ? WHERE sys_key = ?", upgrades)

        # Stock scanned before its product was known was recorded under an
        # unnamed product; fold it into the real product now that one exists.
        absorbed = absorbed_left = 0
        placeholders = conn.execute(
            "SELECT sys_key, alias_key FROM products WHERE is_placeholder = 1 AND alias_key <> ''"
        ).fetchall()
        for placeholder_key, key in placeholders:
            matches = [row[0] for row in conn.execute(
                "SELECT sys_key FROM products WHERE is_placeholder = 0 AND (code_key = ? OR alias_key = ?)",
                (key, key),
            )]
            if len(matches) == 1:
                self._merge_inventory(conn, placeholder_key, matches[0])
                conn.execute("DELETE FROM products WHERE sys_key = ?", (placeholder_key,))
                absorbed += 1
            elif len(matches) > 1:
                absorbed_left += 1

        return {
            "new": len(new_rows),
            "updated": len(description_updates),
            "upgraded": len(upgrades),
            "unchanged": unchanged,
            "absorbed": absorbed,
            "absorbed_left": absorbed_left,
            "ambiguous_before": ambiguous_before,
            "ambiguous_after": self._count_ambiguous(conn),
            "samples": samples,
        }

    def last_import(self):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, filename, imported_at, inventory_revision, undone "
                "FROM import_batches ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row

    def can_undo_last_import(self):
        """Returns (allowed, message). Undo restores the products and stock
        from before the import, so it is only offered while no scan, manual
        removal or resolved scan has changed stock since."""
        batch = self.last_import()
        if not batch:
            return False, "No import to undo."
        batch_id, filename, imported_at, revision, undone = batch
        if undone:
            return False, f"The last import ({filename}) was already undone."

        conn = self._connect()
        try:
            has_snapshot = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('snap_products', 'snap_inventory')"
            ).fetchone()[0] == 2
            current_revision = self.get_revision(conn)
        finally:
            conn.close()

        if not has_snapshot:
            return False, "The undo data for the last import is no longer available."
        if revision != current_revision:
            return False, (
                f"Last import: {filename}. It can no longer be undone because stock has "
                "changed since (scans applied or items removed)."
            )
        return True, f"Last import: {filename} ({imported_at})"

    def undo_last_import(self):
        allowed, message = self.can_undo_last_import()
        if not allowed:
            raise ValueError(message)
        batch_id = self.last_import()[0]

        conn = self._connect()
        try:
            for table in ("inventory", "products"):
                columns = ", ".join(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
                conn.execute(f"DELETE FROM {table}")
                conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM snap_{table}")
            conn.execute("UPDATE import_batches SET undone = 1 WHERE id = ?", (batch_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.reload_index()
