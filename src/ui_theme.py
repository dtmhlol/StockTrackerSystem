"""
Light / dark theming for the Tkinter desktop app.

Windows are built once using the light palette's hex values. ThemeManager
remembers each widget's original ("canonical") colours the first time it sees
it and re-derives its colours for the active mode from those, so toggling back
and forth is exact and windows built later just call `style()` on themselves.
"""
import ctypes
import sys
import tkinter as tk
from tkinter import ttk

UI_FONT = "Segoe UI"
ACCENT = "#3b82f6"

LIGHT = {
    "bg": "#f3f4f6", "surface": "#ffffff", "subtle": "#f9fafb", "input": "#ffffff",
    "border": "#e5e7eb", "text": "#111827", "muted": "#6b7280",
    "thumb": "#cbd5e1", "thumb_active": "#94a3b8",
    "disabled_fg": "#9ca3af",
    "tags": {
        "expired": ("#fee2e2", "#991b1b"),
        "expiring_soon": ("#fef3c7", "#92400e"),
        "good": ("#ffffff", "#111827"),
    },
    # Canonical (light) colour -> colour for this mode. Empty = unchanged.
    "bg_map": {}, "fg_map": {}, "button_neutral": None,
}

DARK = {
    "bg": "#0f1420", "surface": "#1a2130", "subtle": "#232c3d", "input": "#111827",
    "border": "#2e394d", "text": "#f1f5f9", "muted": "#94a3b8",
    "thumb": "#3b475c", "thumb_active": "#56657d",
    "disabled_fg": "#64748b",
    "tags": {
        "expired": ("#4c1d1d", "#fecaca"),
        "expiring_soon": ("#4a3a12", "#fde68a"),
        "good": ("#1a2130", "#f1f5f9"),
    },
    "bg_map": {
        "white": "#1a2130", "#ffffff": "#1a2130", "#f3f4f6": "#0f1420", "#f9fafb": "#232c3d",
        "#eff6ff": "#1b2a48", "#e5e7eb": "#2e394d", "#dcfce7": "#143d28", "#fef3c7": "#3f3212",
        "#6b7280": "#475569", "#9ca3af": "#475569",
        "systembuttonface": "#1a2130", "systemwindow": "#111827",
    },
    "fg_map": {
        "#111827": "#f1f5f9", "#1f2937": "#e2e8f0", "#374151": "#cbd5e1", "#4b5563": "#94a3b8",
        "#6b7280": "#94a3b8", "#1d4ed8": "#93c5fd", "#2563eb": "#60a5fa", "#166534": "#86efac",
        "#991b1b": "#fca5a5", "#92400e": "#fcd34d",
        "systemwindowtext": "#f1f5f9", "systembuttontext": "#f1f5f9",
    },
    "button_neutral": "#2e394d",
}

_OPTIONS = {
    "Tk": ("bg",), "Toplevel": ("bg",), "Frame": ("bg",), "Canvas": ("bg",),
    "Label": ("bg", "fg"), "Button": ("bg", "fg"),
    "Entry": ("bg", "fg", "insertbackground"), "Text": ("bg", "fg", "insertbackground"),
    "Menu": ("bg", "fg"),
    "Checkbutton": ("bg", "fg"), "Radiobutton": ("bg", "fg"),
}
_FG_LIKE = ("fg", "insertbackground")


def system_prefers_dark():
    """Whether Windows apps are set to dark mode (False anywhere else / on error)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def make_button(parent, text, command, kind="primary", **kwargs):
    """A flat, padded button. kind: primary | success | danger | neutral | muted."""
    colors = {
        "primary": ("#3b82f6", "white"),
        "success": ("#10b981", "white"),
        "danger": ("#ef4444", "white"),
        "muted": ("#6b7280", "white"),
        "neutral": ("#e5e7eb", "#111827"),
    }
    bg, fg = colors[kind]
    options = dict(
        text=text, command=command, bg=bg, fg=fg, font=(UI_FONT, 10, "bold"),
        relief=tk.FLAT, bd=0, padx=16, pady=7, cursor="hand2",
    )
    options.update(kwargs)
    return tk.Button(parent, **options)


class ThemedScrolledText(tk.Text):
    """A Text with a ttk scrollbar (so it themes like everything else) that
    packs/grids as a single widget, like tkinter.scrolledtext.ScrolledText."""

    def __init__(self, parent, **kwargs):
        self.frame = tk.Frame(parent)
        super().__init__(self.frame, **kwargs)
        self.vbar = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, command=self.yview)
        self.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side=tk.RIGHT, fill=tk.Y)
        super().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        text_methods = vars(tk.Text).keys()
        geometry_methods = vars(tk.Pack).keys() | vars(tk.Grid).keys() | vars(tk.Place).keys()
        for name in geometry_methods.difference(text_methods):
            if name[0] != "_" and name not in ("config", "configure"):
                setattr(self, name, getattr(self.frame, name))


def _shade(widget, color, mode):
    """Slightly lighter (dark mode) or darker (light mode) version of `color`, for hover."""
    try:
        r, g, b = (v // 256 for v in widget.winfo_rgb(color))
    except tk.TclError:
        return color
    if mode == "dark":
        r, g, b = (int(c + (255 - c) * 0.14) for c in (r, g, b))
    else:
        r, g, b = (int(c * 0.9) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


class ThemeManager:
    def __init__(self, root, mode="light"):
        self.root = root
        self.mode = mode if mode in ("light", "dark") else "light"
        self._tag_trees = []
        self.apply()

    @property
    def palette(self):
        return DARK if self.mode == "dark" else LIGHT

    def toggle(self):
        self.mode = "light" if self.mode == "dark" else "dark"
        self.apply()
        return self.mode

    # -- whole-app -----------------------------------------------------

    def apply(self):
        self._configure_ttk()
        self._walk(self.root)
        self._tag_trees = [t for t in self._tag_trees if self._alive(t)]
        for tree in self._tag_trees:
            self._configure_tags(tree)

    def style(self, widget):
        """Themes a freshly built window or subtree."""
        self._walk(widget)

    def register_row_tags(self, tree):
        """Keeps the expired / expiring / good row colours of `tree` in sync with the theme."""
        self._tag_trees.append(tree)
        self._configure_tags(tree)

    def set_colors(self, widget, **colors):
        """Changes a widget's colours at runtime (e.g. bg="#f59e0b") in a theme-aware way."""
        self._capture(widget)
        widget._canon.update(colors)
        self._apply_widget(widget)

    # -- ttk -----------------------------------------------------------

    @staticmethod
    def _alive(widget):
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _configure_ttk(self):
        p = self.palette
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(UI_FONT, 10))
        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                        foreground=p["text"], rowheight=34, borderwidth=0, font=(UI_FONT, 10),
                        bordercolor=p["surface"], lightcolor=p["surface"], darkcolor=p["surface"])
        style.configure("Treeview.Heading", background=p["subtle"], foreground=p["muted"],
                        font=(UI_FONT, 9, "bold"), relief="flat", borderwidth=0, padding=(10, 9))
        style.map("Treeview.Heading", background=[("active", p["border"])])
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        style.configure("TScrollbar", background=p["thumb"], troughcolor=p["bg"], bordercolor=p["bg"],
                        lightcolor=p["thumb"], darkcolor=p["thumb"], arrowcolor=p["muted"],
                        relief="flat", borderwidth=0, arrowsize=14)
        style.map("TScrollbar", background=[("active", p["thumb_active"])])
        style.configure("TCombobox", fieldbackground=p["input"], background=p["subtle"], foreground=p["text"],
                        arrowcolor=p["text"], bordercolor=p["border"], lightcolor=p["border"],
                        darkcolor=p["border"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", p["input"])],
                  foreground=[("readonly", p["text"])],
                  selectbackground=[("readonly", p["input"])], selectforeground=[("readonly", p["text"])])
        self.root.option_add("*TCombobox*Listbox.background", p["input"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")

    def _configure_tags(self, tree):
        for tag, (bg, fg) in self.palette["tags"].items():
            tree.tag_configure(tag, background=bg, foreground=fg)

    # -- per-widget ----------------------------------------------------

    def _walk(self, widget):
        if getattr(widget, "_keep_subtree", False):
            return  # e.g. QR codes, which must stay dark-on-white to scan
        self._apply_widget(widget)
        for child in widget.winfo_children():
            self._walk(child)

    def _capture(self, widget):
        """Remembers a widget's original colours the first time it is seen."""
        if hasattr(widget, "_canon"):
            return
        cls = widget.winfo_class()
        canon = {}
        for option in _OPTIONS.get(cls, ()):
            try:
                canon[option] = str(widget.cget(option))
            except tk.TclError:
                pass
        widget._canon = canon
        widget._solid_border = False

        if cls in ("Frame", "Label", "Entry", "Text"):
            try:
                if str(widget.cget("relief")) == "solid":
                    widget._solid_border = True
                    widget.configure(relief="flat", bd=0, highlightthickness=1)
            except tk.TclError:
                pass
        if cls == "Button":
            widget.bind("<Enter>", lambda _e, w=widget: self._hover(w, True), add="+")
            widget.bind("<Leave>", lambda _e, w=widget: self._hover(w, False), add="+")

    def _mapped(self, widget, option, value):
        if self.mode == "light" or not value:
            return value
        p = self.palette
        key = value.casefold()
        cls = widget.winfo_class()
        if option in _FG_LIKE:
            return p["fg_map"].get(key, value)
        if cls == "Button" and key == "#f3f4f6":
            return p["button_neutral"]
        if cls in ("Entry", "Text") and key in ("white", "#ffffff", "systemwindow"):
            return p["input"]
        return p["bg_map"].get(key, value)

    def _apply_widget(self, widget):
        cls = widget.winfo_class()
        if cls not in _OPTIONS:
            return
        self._capture(widget)
        p = self.palette

        settings = {opt: self._mapped(widget, opt, val) for opt, val in widget._canon.items()}
        if cls == "Button":
            settings["activebackground"] = _shade(widget, settings.get("bg", p["surface"]), self.mode)
            settings["activeforeground"] = settings.get("fg", p["text"])
            settings["disabledforeground"] = p["disabled_fg"]
            settings["cursor"] = "hand2"
        if cls in ("Checkbutton", "Radiobutton"):
            settings["activebackground"] = settings.get("bg", p["surface"])
            settings["activeforeground"] = settings.get("fg", p["text"])
            settings["selectcolor"] = p["input"]          # the tick box's own background
            settings["disabledforeground"] = p["disabled_fg"]
            settings["cursor"] = "hand2"
        if cls == "Menu":
            settings["activebackground"] = ACCENT
            settings["activeforeground"] = "white"
            settings["disabledforeground"] = p["disabled_fg"]
        if widget._solid_border:
            settings["highlightbackground"] = p["border"]
            settings["highlightcolor"] = ACCENT if cls in ("Entry", "Text") else p["border"]
        try:
            widget.configure(**settings)
        except tk.TclError:
            pass

        if cls in ("Tk", "Toplevel"):
            self._dark_titlebar(widget)

    def _hover(self, widget, entering):
        if str(widget.cget("state")) == "disabled":
            return
        base = self._mapped(widget, "bg", widget._canon.get("bg", ""))
        widget.configure(bg=_shade(widget, base, self.mode) if entering else base)

    def _dark_titlebar(self, window):
        """Best-effort dark title bar on Windows 10/11."""
        if sys.platform != "win32":
            return
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1 if self.mode == "dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass
