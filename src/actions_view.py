"""
The list of stock that needs attention, shown both in the dashboard's Actions tab and in the
pop-up. Each row has an Actions menu: dismiss for 3 days, remove, or edit.
"""
import tkinter as tk

import actions
import expiry_report
from ui_theme import UI_FONT, ScrollFrame

STATUS_COLORS = {"expired": "#991b1b", "expiring_soon": "#92400e"}
STATUS_BARS = {"expired": "#ef4444", "expiring_soon": "#eab308"}


class ActionsView(tk.Frame):
    """`app` supplies db_path, expiry_warning_days, theme and the three row handlers
    snooze_action(item), remove_action(item) and edit_action(item)."""

    def __init__(self, parent, app, bg="#ffffff", padx=0):
        super().__init__(parent, bg=bg)
        self.app = app
        self.items = []
        self._bg = bg

        self.summary_var = tk.StringVar()
        tk.Label(self, textvariable=self.summary_var, font=(UI_FONT, 11, "bold"), bg=bg, fg="#111827",
                 anchor="w", padx=padx).pack(fill=tk.X, pady=(12, 2))
        self.note_var = tk.StringVar()
        tk.Label(self, textvariable=self.note_var, font=(UI_FONT, 9), bg=bg, fg="#6b7280",
                 anchor="w", padx=padx).pack(fill=tk.X, pady=(0, 8))

        self.scroller = ScrollFrame(self, bg=bg, padx=padx)
        self.scroller.pack(fill=tk.BOTH, expand=True)
        self.body = self.scroller.body

    def refresh(self):
        """Reloads from the database and redraws the rows. Returns the items shown."""
        try:
            items, snoozed = actions.load_actions(self.app.db_path, self.app.expiry_warning_days)
        except Exception as e:
            self.items = []
            self.summary_var.set("Could not load the list.")
            self.note_var.set(str(e))
            self._draw()
            return self.items

        self.items = items
        self.summary_var.set(actions.summary_sentence(items))
        notes = ["Most urgent first: the longest expired at the top."] if items else []
        if snoozed:
            notes.append(f"{snoozed} dismissed for {actions.SNOOZE_DAYS} days.")
        self.note_var.set("  ".join(notes))
        self._draw()
        return items

    def _draw(self):
        for child in self.body.winfo_children():
            child.destroy()

        for item in self.items:
            row = tk.Frame(self.body, bg=self._bg, highlightthickness=1, highlightbackground="#e5e7eb")
            row.pack(fill=tk.X, pady=(0, 6))
            tk.Frame(row, bg=STATUS_BARS[item["status"]], width=5).pack(side=tk.LEFT, fill=tk.Y)

            menu_button = tk.Button(
                row, text="Actions  ▾", font=(UI_FONT, 9, "bold"), bg="#e5e7eb", fg="#111827",
                relief=tk.FLAT, bd=0, padx=12, pady=6, cursor="hand2",
            )
            menu_button.pack(side=tk.RIGHT, padx=12, pady=10)
            menu_button.configure(command=lambda b=menu_button, i=item: self._open_menu(b, i))

            text = tk.Frame(row, bg=self._bg)
            text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12, pady=8)
            tk.Label(text, text=item["product"], font=(UI_FONT, 10, "bold"), bg=self._bg, fg="#111827",
                     anchor="w").pack(fill=tk.X)
            detail = (f"{expiry_report.format_month(item['expiry'])}  ·  qty {item['quantity']}")
            tk.Label(text, text=detail, font=(UI_FONT, 9), bg=self._bg, fg="#6b7280", anchor="w").pack(fill=tk.X)

            tk.Label(row, text=actions.describe_timing(item), font=(UI_FONT, 9, "bold"), bg=self._bg,
                     fg=STATUS_COLORS[item["status"]]).pack(side=tk.RIGHT, padx=(0, 6))

        self.app.theme.style(self.body)

    def _open_menu(self, button, item):
        menu = tk.Menu(self, tearoff=0, bg="#ffffff", fg="#111827", font=(UI_FONT, 10), relief=tk.FLAT, bd=1)
        menu.add_command(label=f"Dismiss for {actions.SNOOZE_DAYS} days", command=lambda: self.app.snooze_action(item))
        menu.add_command(label="Remove product", command=lambda: self.app.remove_action(item))
        menu.add_command(label="Edit...", command=lambda: self.app.edit_action(item))
        self.app.theme.style(menu)
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()
