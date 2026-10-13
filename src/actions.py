"""
Expiry actions: which stock needs attention, how urgent it is, and snoozing.

No UI code lives here. The dashboard's Actions tab, the pop-up and the Windows notification all
use these functions, so they always agree. The status rule itself stays in expiry_report.status_for.

Snoozes live in their own small table (created here, so the database format version is unchanged
and an older version of the app can still open the file). A snooze is tied to the row's expiry
month: if the expiry is edited, the snooze no longer applies and the row shows up again.
"""
import sqlite3
from datetime import datetime, timedelta

import expiry_report
from product_catalog import PLACEHOLDER_LABEL

SNOOZE_DAYS = 3
ACTION_STATUSES = ("expired", "expiring_soon")
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def ensure_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_snoozes ("
        "inventory_id INTEGER PRIMARY KEY, expiry_date TEXT NOT NULL, snoozed_until TEXT NOT NULL)"
    )


def _days_until(expiry, now):
    """Whole days from `now` to the first day of the expiry month (negative once expired).
    Same counting rule as expiry_report.status_for."""
    return (datetime.strptime(expiry, "%Y-%m") - now).days


def load_actions(db_path, warning_days, now=None, include_snoozed=False):
    """Stock rows that are expired or expiring soon, most urgent first.

    Returns (items, snoozed_count). Each item has: id, product, expiry, quantity, status, days.
    Order is by days to expiry ascending, so the longest-expired stock comes first, then stock
    that expired recently, then stock closest to expiring. Rows snoozed right now are left out
    (and counted in snoozed_count) unless include_snoozed is True.
    """
    now = now or datetime.now()
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute("DELETE FROM action_snoozes WHERE snoozed_until <= ?", (now.strftime(_TIME_FORMAT),))
        conn.commit()
        snoozes = {row[0]: row[1] for row in conn.execute("SELECT inventory_id, expiry_date FROM action_snoozes")}
        raw = conn.execute(
            "SELECT i.id, p.description, i.barcode, i.expiry_date, i.quantity "
            "FROM inventory i LEFT JOIN products p ON p.sys_key = i.sys_key"
        ).fetchall()
    finally:
        conn.close()

    items, snoozed = [], 0
    for row_id, description, barcode, expiry, quantity in raw:
        status = expiry_report.status_for(expiry, warning_days, now)
        if status not in ACTION_STATUSES:
            continue
        if snoozes.get(row_id) == expiry and not include_snoozed:
            snoozed += 1
            continue
        items.append({
            "id": row_id,
            "product": description or PLACEHOLDER_LABEL,
            "barcode": barcode or "",
            "expiry": expiry,
            "quantity": quantity,
            "status": status,
            "days": _days_until(expiry, now),
        })
    items.sort(key=lambda item: (item["days"], item["product"].casefold(), item["id"]))
    return items, snoozed


def counts(items):
    expired = sum(1 for item in items if item["status"] == "expired")
    return {"total": len(items), "expired": expired, "expiring_soon": len(items) - expired}


def summary_sentence(items):
    """'3 actions to review: 2 expired, 1 expiring soon' (or a calm 'nothing' line)."""
    c = counts(items)
    if not c["total"]:
        return "Nothing needs attention right now."
    noun = "action" if c["total"] == 1 else "actions"
    parts = []
    if c["expired"]:
        parts.append(f"{c['expired']} expired")
    if c["expiring_soon"]:
        parts.append(f"{c['expiring_soon']} expiring soon")
    return f"{c['total']} {noun} to review: " + ", ".join(parts)


def describe_timing(item):
    """'Expired 42 days ago' / 'Expires this month' / 'Expires in 20 days'."""
    days = item["days"]
    if days < 0:
        return f"Expired {-days} day{'s' if days != -1 else ''} ago"
    if days == 0:
        return "Expires today"
    return f"Expires in {days} day{'s' if days != 1 else ''}"


def snooze(db_path, inventory_ids, days=SNOOZE_DAYS, now=None):
    """Hides these rows from the actions list for `days` days. Returns how many were snoozed."""
    now = now or datetime.now()
    until = (now + timedelta(days=days)).strftime(_TIME_FORMAT)
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        done = 0
        for inventory_id in inventory_ids:
            row = conn.execute("SELECT expiry_date FROM inventory WHERE id = ?", (inventory_id,)).fetchone()
            if not row:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO action_snoozes (inventory_id, expiry_date, snoozed_until) VALUES (?, ?, ?)",
                (inventory_id, row[0], until),
            )
            done += 1
        conn.commit()
        return done
    finally:
        conn.close()


def notification_text(items):
    """(title, message) for the Windows notification."""
    c = counts(items)
    title = "Stock Tracker"
    if not c["total"]:
        return title, summary_sentence(items)
    return title, summary_sentence(items) + ". Click to review."
