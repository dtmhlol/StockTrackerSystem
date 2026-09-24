"""
Expiry report: expiry status rules, filtering, grouping, sorting, and CSV / PDF output.

No UI code lives here, so the same functions drive the dashboard colours and the export.
"""
import csv
import os
import re
import sqlite3
from datetime import datetime
from xml.sax.saxutils import escape

from product_catalog import PLACEHOLDER_LABEL

STATUS_ORDER = ("expired", "expiring_soon", "good", "invalid")
STATUS_LABELS = {
    "expired": "Expired",
    "expiring_soon": "Expiring soon",
    "good": "Good",
    "invalid": "Invalid date",
}

# key, heading. Also the choices offered for sorting.
COLUMNS = (
    ("id", "Sys ID"),
    ("product", "Product"),
    ("alias", "Alias / Barcode"),
    ("item_code", "Item Code"),
    ("expiry", "Expiry"),
    ("quantity", "Quantity"),
    ("status", "Status"),
)
COLUMN_HEADINGS = dict(COLUMNS)


def status_for(expiry, warning_days, now=None):
    """The one rule for a stock row's status: 'expired', 'expiring_soon', 'good' or 'invalid'.

    `expiry` is YYYY-MM. Days are counted from the FIRST day of that month (the same rule the
    dashboard has always used), so stock stamped with the current month already counts as
    expired. To count to the END of the month instead, change the line marked below.
    """
    now = now or datetime.now()
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m")   # <- month start; use month end here to change the rule
    except (TypeError, ValueError):
        return "invalid"
    days_until_expiry = (expiry_date - now).days
    if days_until_expiry < 0:
        return "expired"
    if days_until_expiry < warning_days:
        return "expiring_soon"
    return "good"


def format_month(month):
    """'2026-09' -> 'September 2026' (blank -> 'No valid date')."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except (TypeError, ValueError):
        return "No valid date"


def load_rows(db_path, warning_days, now=None):
    """Every stock row with its product details and status."""
    conn = sqlite3.connect(db_path)
    try:
        raw = conn.execute(
            "SELECT i.id, p.description, p.alias, p.item_code, i.barcode, i.expiry_date, i.quantity "
            "FROM inventory i LEFT JOIN products p ON p.sys_key = i.sys_key"
        ).fetchall()
    finally:
        conn.close()

    rows = []
    for row_id, description, alias, item_code, barcode, expiry, quantity in raw:
        status = status_for(expiry, warning_days, now)
        rows.append({
            "id": row_id,
            "product": description or PLACEHOLDER_LABEL,
            "alias": alias or barcode or "",
            "item_code": item_code or "",
            "expiry": expiry or "",
            "quantity": quantity,
            "status": status,
            "month": expiry if status != "invalid" else "",
        })
    return rows


def months_available(rows):
    """Distinct YYYY-MM values present in the stock, oldest first."""
    return sorted({r["month"] for r in rows if r["month"]})


# ------------------------------------------------------------ sorting / grouping

def _natural(text):
    """Sort key that orders 'item 2' before 'item 10' and ignores case."""
    parts = re.split(r"(\d+)", str(text))
    return [int(p) if i % 2 else p.casefold() for i, p in enumerate(parts)]


def _sort_value(row, key):
    if key in ("id", "quantity"):
        return row[key]
    if key == "status":
        return STATUS_ORDER.index(row["status"])
    if key == "expiry":
        return (row["expiry"] == "", row["expiry"])      # blanks last
    return _natural(row[key])


def _sorted(rows, sort):
    """Stable multi-key sort. `sort` is [(column key, descending), ...], most important first."""
    ordered = sorted(rows, key=lambda r: r["id"])         # deterministic tie-break
    for key, descending in reversed(sort):
        ordered.sort(key=lambda r, k=key: _sort_value(r, k), reverse=descending)
    return ordered


def build_report(rows, statuses=None, month_from=None, month_to=None, group_by="none", sort=()):
    """Filters, sorts and groups the rows.

    statuses    set of status codes to keep (None = all)
    month_from  'YYYY-MM' or None; month_to likewise (inclusive). Rows without a valid date
                are dropped when either bound is set.
    group_by    'none' | 'status' | 'month'
    sort        [(column key, descending), ...]

    Returns {"groups": [{"label", "rows", "quantity"}], "total_items", "total_quantity",
             "status_totals": {code: (items, quantity)}}.
    """
    keep = [
        r for r in rows
        if (statuses is None or r["status"] in statuses)
        and (not month_from or (r["month"] and r["month"] >= month_from))
        and (not month_to or (r["month"] and r["month"] <= month_to))
    ]
    keep = _sorted(keep, sort)

    if group_by == "status":
        keyed = {code: [r for r in keep if r["status"] == code] for code in STATUS_ORDER}
        groups = [(STATUS_LABELS[code], members) for code, members in keyed.items() if members]
    elif group_by == "month":
        months = sorted({r["month"] for r in keep if r["month"]})
        groups = [(format_month(m), [r for r in keep if r["month"] == m]) for m in months]
        undated = [r for r in keep if not r["month"]]
        if undated:
            groups.append(("No valid date", undated))
    else:
        groups = [(None, keep)] if keep else []

    status_totals = {}
    for code in STATUS_ORDER:
        members = [r for r in keep if r["status"] == code]
        status_totals[code] = (len(members), sum(r["quantity"] for r in members))

    return {
        "groups": [{"label": label, "rows": members, "quantity": sum(r["quantity"] for r in members)}
                   for label, members in groups],
        "total_items": len(keep),
        "total_quantity": sum(r["quantity"] for r in keep),
        "status_totals": status_totals,
    }


def describe(statuses, month_from, month_to, group_by, sort, warning_days):
    """Plain-English lines describing what a report contains (used in the PDF header)."""
    chosen = [STATUS_LABELS[c] for c in STATUS_ORDER if statuses is None or c in statuses]
    if month_from or month_to:
        if month_from and month_to and month_from == month_to:
            months = format_month(month_from)
        else:
            months = f"{format_month(month_from) if month_from else 'earliest'} to {format_month(month_to) if month_to else 'latest'}"
    else:
        months = "all months"
    order = ", then ".join(f"{COLUMN_HEADINGS[k]} ({'descending' if d else 'ascending'})" for k, d in sort) or "default order"
    grouping = {"none": "not grouped", "status": "grouped by status", "month": "grouped by month"}[group_by]
    return [
        f"Showing: {', '.join(chosen)} | {months}",
        f"Sorted by {order} | {grouping}",
        f"Expiring soon means within {warning_days} days.",
    ]


# ------------------------------------------------------------------- CSV

def _excel_text(value):
    """Wraps all-digit codes as ="0123" so Excel keeps leading zeros and long barcodes intact."""
    value = str(value)
    return f'="{value}"' if value.isascii() and value.isdigit() else value


def write_csv(path, report, group_by="none", excel_friendly=True):
    """Writes the report as UTF-8 CSV (with a BOM so Excel reads accents correctly)."""
    grouped = group_by != "none"
    headings = [h for _k, h in COLUMNS]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow((["Group"] if grouped else []) + headings)
        for group in report["groups"]:
            for r in group["rows"]:
                alias, code = r["alias"], r["item_code"]
                if excel_friendly:
                    alias, code = _excel_text(alias), _excel_text(code)
                writer.writerow(
                    ([group["label"]] if grouped else [])
                    + [r["id"], r["product"], alias, code, r["expiry"], r["quantity"], STATUS_LABELS[r["status"]]]
                )


# ------------------------------------------------------------------- PDF

def _register_fonts():
    """(regular, bold) font names. Uses Segoe UI or Arial from Windows for full accent support,
    falling back to the built-in Helvetica (Latin-1 only)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for regular, bold, name in (("segoeui.ttf", "segoeuib.ttf", "SegoeUI"), ("arial.ttf", "arialbd.ttf", "ArialWin")):
        regular_path, bold_path = os.path.join(fonts_dir, regular), os.path.join(fonts_dir, bold)
        if os.path.exists(regular_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont(name, regular_path))
                pdfmetrics.registerFont(TTFont(name + "-Bold", bold_path))
                return name, name + "-Bold", True
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold", False


def write_pdf(path, report, meta):
    """Writes the report as a landscape A4 PDF.

    meta: {"title", "generated" (str), "lines" (list of str), "footer" (str)}
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    regular, bold, unicode_ok = _register_fonts()

    def clean(text):
        text = str(text)
        return text if unicode_ok else text.encode("latin-1", "replace").decode("latin-1")

    def para(text, style):
        return Paragraph(escape(clean(text)), style)

    ink, muted = colors.HexColor("#111827"), colors.HexColor("#6b7280")
    cell = ParagraphStyle("cell", fontName=regular, fontSize=8.5, leading=10.5, textColor=ink)
    cell_bold = ParagraphStyle("cell_bold", parent=cell, fontName=bold)
    cell_center = ParagraphStyle("cell_center", parent=cell, alignment=1)
    cell_right = ParagraphStyle("cell_right", parent=cell, alignment=2)
    head = ParagraphStyle("head", parent=cell_bold, textColor=colors.white)
    head_center = ParagraphStyle("head_center", parent=head, alignment=1)
    head_right = ParagraphStyle("head_right", parent=head, alignment=2)
    title_style = ParagraphStyle("title", fontName=bold, fontSize=20, leading=24, textColor=ink)
    small = ParagraphStyle("small", fontName=regular, fontSize=8.5, leading=11.5, textColor=muted)
    group_style = ParagraphStyle("group", fontName=bold, fontSize=12, leading=15, textColor=ink,
                                 spaceBefore=10, spaceAfter=5)

    # Column widths in mm (sum fits landscape A4 with 14 mm margins) and per-column styles
    widths = [14, 109, 42, 32, 22, 17, 32]   # = 268 mm, the full width between the margins
    body_styles = [cell_center, cell, cell, cell, cell_center, cell_right, cell_center]
    head_styles = [head_center, head, head, head, head_center, head_right, head_center]
    row_fill = {
        "expired": colors.HexColor("#fee2e2"),
        "expiring_soon": colors.HexColor("#fef3c7"),
        "invalid": colors.HexColor("#e5e7eb"),
    }

    def make_table(rows):
        data = [[para(h, head_styles[i]) for i, (_k, h) in enumerate(COLUMNS)]]
        for r in rows:
            values = [r["id"], r["product"], r["alias"], r["item_code"], r["expiry"], r["quantity"], STATUS_LABELS[r["status"]]]
            data.append([para(v, body_styles[i]) for i, v in enumerate(values)])

        table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1, hAlign="LEFT")
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        for i, r in enumerate(rows, start=1):
            if r["status"] in row_fill:
                style.append(("BACKGROUND", (0, i), (-1, i), row_fill[r["status"]]))
        table.setStyle(TableStyle(style))
        return table

    story = [para(meta["title"], title_style), para("Generated " + meta["generated"], small), Spacer(1, 4)]
    story += [para(line, small) for line in meta["lines"]]
    story.append(Spacer(1, 10))

    # Summary boxes for what this report contains
    cells = []
    for code in STATUS_ORDER[:3]:
        items, units = report["status_totals"][code]
        cells.append([para(STATUS_LABELS[code], cell_bold), para(f"{items:,} items", cell), para(f"{units:,} units", small)])
    cells.append([para("Total", cell_bold), para(f"{report['total_items']:,} items", cell), para(f"{report['total_quantity']:,} units", small)])
    summary = Table([cells], colWidths=[67 * mm] * 4, hAlign="LEFT")
    summary.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#d1d5db")),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#d1d5db")),
        ("BACKGROUND", (0, 0), (0, 0), row_fill["expired"]),
        ("BACKGROUND", (1, 0), (1, 0), row_fill["expiring_soon"]),
        ("BACKGROUND", (3, 0), (3, 0), colors.HexColor("#f3f4f6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [summary, Spacer(1, 8)]

    for group in report["groups"]:
        if group["label"] is not None:
            count = len(group["rows"])
            # Start a new page only if a heading plus a few rows won't fit. (keepWithNext would
            # glue the heading to the WHOLE table and push big groups onto fresh pages.)
            story.append(CondPageBreak(45 * mm))
            story.append(para(f"{group['label']}  -  {count:,} item{'s' if count != 1 else ''}, {group['quantity']:,} units", group_style))
        story.append(make_table(group["rows"]))
        story.append(Spacer(1, 6))

    footer_text = clean(meta["footer"])

    class NumberedCanvas(canvas.Canvas):
        """Adds 'Page x of y' by drawing the footer once the total page count is known."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self.setFont(regular, 8)
                self.setFillColor(muted)
                width, _height = landscape(A4)
                self.drawString(14 * mm, 9 * mm, footer_text)
                self.drawRightString(width - 14 * mm, 9 * mm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    document = SimpleDocTemplate(
        path, pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm, topMargin=13 * mm, bottomMargin=16 * mm,
        title=clean(meta["title"]), author="Stock Tracker",
    )
    document.build(story, canvasmaker=NumberedCanvas)
