import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from tkinter import filedialog
import sqlite3
import os
import re
import sys
from datetime import datetime, timedelta, timezone
import webbrowser
import importlib.util
import json
import queue
import random
import string
import threading
from urllib.parse import urlparse

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as admin_firestore

import app_paths
import expiry_report
import secure_store
import stock_history
import updater
from product_catalog import (
    ProductCatalog, DatabaseTooNewError, PLACEHOLDER_LABEL, MAPPING_FIELDS, read_table, guess_mapping,
    validate_mapping, parse_rows, format_import_summary, expiry_from_text,
)
from ui_theme import (
    ThemeManager, ThemedScrolledText, ScrollFrame, UI_FONT, fit_window, make_button, system_prefers_dark,
)

# Dashboard label and row colour for each status code from expiry_report.status_for
TREE_STATUS = {
    "expired": ("EXPIRED", "expired"),
    "expiring_soon": ("Expiring Soon", "expiring_soon"),
    "good": ("Good", "good"),
    "invalid": ("Invalid Date", "good"),
}

# Stock expiring within this many days is highlighted as "expiring soon".
WARNING_DAY_PRESETS = (30, 60, 90)
DEFAULT_WARNING_DAYS = 60

QR_AVAILABLE = importlib.util.find_spec("qrcode") is not None and importlib.util.find_spec("PIL") is not None

# Required keys in the Firebase web app config (Firebase Console > Project
# Settings > General > Your apps > Web app > SDK setup and configuration).
# This config's apiKey is not a secret — access is enforced by Firestore
# Security Rules, not by hiding this object — so it's safe to embed in the
# mobile pairing QR code.
REQUIRED_WEB_CONFIG_KEYS = ["apiKey", "authDomain", "projectId", "appId"]

# Required keys in a Firebase/Google Cloud service account key file, used
# server-side by the desktop app's Admin SDK connection.
REQUIRED_SERVICE_ACCOUNT_KEYS = ["type", "project_id", "private_key", "client_email"]


def validate_firebase_web_config(raw_text):
    """
    Validates that `raw_text` is a JSON object containing the fields the
    mobile app's Firebase Web SDK needs to connect.

    Returns a (is_valid: bool, config: dict|None, message: str) tuple.
    `message` is always a human-readable explanation suitable for showing
    directly to the user, whether validation passed or failed.
    """
    if not raw_text or not raw_text.strip():
        return False, None, "Paste the Firebase web config JSON."

    # The Firebase console shows the config as a JavaScript snippet
    # (`const firebaseConfig = { apiKey: "...", };`), not strict JSON, so
    # normalize that form: keep only the {...} body, quote bare keys, and
    # drop trailing commas.
    start, end = raw_text.find("{"), raw_text.rfind("}")
    normalized = raw_text[start:end + 1] if start != -1 and end > start else raw_text
    normalized = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', normalized)
    normalized = re.sub(r',(\s*})', r'\1', normalized)

    try:
        config = json.loads(normalized)
    except (TypeError, ValueError) as e:
        return False, None, f"That isn't valid JSON: {e}"

    if not isinstance(config, dict):
        return False, None, "The Firebase web config should be a JSON object, e.g. { \"apiKey\": ... }."

    missing = [
        key for key in REQUIRED_WEB_CONFIG_KEYS
        if not config.get(key) or str(config.get(key)).strip() in ("...", "…")
    ]
    if missing:
        return False, None, f"Missing required field(s): {', '.join(missing)}."

    return True, config, "Looks like a valid Firebase web config."


def validate_service_account_info(data):
    """
    Validates the contents of a Firebase/Google Cloud service account key.

    Returns (is_valid, message) where `message` is the project_id on success or
    a human-readable error on failure.
    """
    if not isinstance(data, dict):
        return False, "That file isn't a Firebase service account key."

    missing = [key for key in REQUIRED_SERVICE_ACCOUNT_KEYS if not data.get(key)]
    if data.get("type") != "service_account" or missing:
        return False, (
            "That doesn't look like a Firebase service account key. In the Google Cloud console, "
            "open IAM & Admin > Service accounts, choose the account, then Keys > Add key > JSON."
        )
    return True, data.get("project_id")


def validate_service_account_file(path):
    """Like validate_service_account_info, for a key file on disk."""
    if not os.path.exists(path):
        return False, "No key file was found."
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return False, f"That file is not valid JSON: {e}"
    return validate_service_account_info(data)


def normalize_mobile_url(text):
    """
    Validates the address where the phone page is hosted.

    Returns (is_valid, url_or_message). A missing scheme is assumed to be https,
    and plain http is refused because phone browsers only allow the camera on
    secure pages (localhost excepted, for testing).
    """
    text = (text or "").strip()
    if not text:
        return False, "Enter the address where you hosted the phone page."
    if "://" not in text:
        text = "https://" + text

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or " " in text:
        return False, "That doesn't look like a web address."
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        return False, "The address must start with https:// because phone browsers only allow the camera on secure pages."
    return True, text


class StockTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Tracker - Manager Dashboard")
        self.root.geometry(f"1040x{min(660, max(400, self.root.winfo_screenheight() - 100))}")
        self.root.configure(bg="#f3f4f6") # Light gray background (themed below)
        
        # Project folder when run from source, %LOCALAPPDATA%\\StockTracker when installed
        self.db_path = app_paths.database_path()

        self.connection_status_var = tk.StringVar(value="◌ Checking mobile...")
        self.connection_poll_job = None
        self.db = None
        self.status_pill = None
        self.theme_button = None
        self.legend_soon_label = None
        self.row_menu = None
        self.catalog_refresh = None
        self.history_window = None
        self.add_item_window = None
        self.expiry_warning_days = DEFAULT_WARNING_DAYS

        # init_db() must run first — it creates the settings table that
        # load_or_create_connection_token() reads from and writes to.
        self.init_db()
        saved_theme = self.get_setting("ui_theme")
        self.theme = ThemeManager(self.root, saved_theme or ("dark" if system_prefers_dark() else "light"))
        self.expiry_warning_days = self.load_warning_days()
        self.credentials_info = None
        self.credentials_source = None
        self.credentials_error = None
        self.credentials_listeners = []
        self.reload_credentials()
        # Needs the inventory table from init_db(); re-keys stock by product.
        self.catalog = ProductCatalog(self.db_path)
        self.catalog_window = None
        self.pending_button = None
        self.connection_token = self.load_or_create_connection_token()
        self.setup_completed = False

        if not self.has_firebase_setup():
            self.root.withdraw()
            self.show_initial_setup()
        else:
            self.setup_completed = True
            self.connect_firestore()

        self.setup_ui()

        if self.setup_completed:
            self.refresh_data(run_sync=True, silent_sync=True)
            self.start_connection_polling()
        else:
            self.root.withdraw()

        self.root.after(600, self.offer_credential_migration)

    def init_db(self):
        """Ensures the database and required tables exist before launching the UI."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # Create inventory table if it doesn't exist. 
            # This matches the data your mobile app sends via the vessel.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    barcode TEXT NOT NULL,
                    expiry_date TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create settings table to store the Firebase web config
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            messagebox.showerror("Database Error", f"Could not connect to database: {e}")

    def get_setting(self, key):
        """Reads one value from the settings table (None if missing)."""
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception:
            return None

    def save_setting(self, key, value):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def load_warning_days(self):
        """Saved expiring-soon window in days (falls back to the default)."""
        try:
            days = int(self.get_setting("expiry_warning_days") or DEFAULT_WARNING_DAYS)
            return days if days > 0 else DEFAULT_WARNING_DAYS
        except ValueError:
            return DEFAULT_WARNING_DAYS

    def warning_day_choices(self):
        """Dropdown labels: the presets, plus the current value if it isn't one of them."""
        days = sorted(set(WARNING_DAY_PRESETS) | {self.expiry_warning_days})
        return [f"{d} days" for d in days]

    def set_warning_days(self, days, refresh=True):
        """Saves the expiring-soon window and updates the legend and table colours."""
        previous = self.expiry_warning_days
        self.expiry_warning_days = days
        if days != previous:
            self.catalog.log_system_event(
                f"Expiring-soon window changed from {previous} to {days} days.", {"from": previous, "to": days})
        self.save_setting("expiry_warning_days", str(days))
        if self.legend_soon_label is not None:
            self.legend_soon_label.config(text=f"■  Expiring within {days} days")
        if refresh and getattr(self, "tree", None) is not None:
            self.refresh_data(run_sync=False)

    def get_mobile_app_url(self):
        """The saved phone page address, or the built-in default."""
        return self.get_setting("mobile_app_url") or app_paths.DEFAULT_MOBILE_APP_URL

    def toggle_theme(self):
        """Switches between light and dark mode and remembers the choice."""
        mode = self.theme.toggle()
        self.save_setting("ui_theme", mode)
        self.update_theme_button()

    def update_theme_button(self):
        if self.theme_button is not None:
            self.theme_button.config(text="☀  Light mode" if self.theme.mode == "dark" else "☾  Dark mode")

    def get_service_account_path(self):
        """Legacy plain-text key location. Only read so an existing key can be migrated
        into encrypted storage; new keys are never written here."""
        return os.path.join(app_paths.config_dir(), 'service_account.json')

    def credentials_store_path(self):
        """Where the encrypted key lives (Windows-user-bound, unreadable as text)."""
        return os.path.join(app_paths.config_dir(), 'firebase_credentials.dat')

    def reload_credentials(self):
        """Loads the service account key into memory: encrypted store first, then the
        legacy plain file. The key is never written back out or shown."""
        self.credentials_info, self.credentials_source, self.credentials_error = None, None, None
        try:
            stored = secure_store.load_service_account(self.credentials_store_path())
        except secure_store.SecureStoreError as e:
            stored = None
            self.credentials_error = str(e)

        if stored:
            valid, _ = validate_service_account_info(stored)
            if valid:
                self.credentials_info, self.credentials_source = stored, "secure"
                return

        legacy_valid, _ = validate_service_account_file(self.get_service_account_path())
        if legacy_valid:
            with open(self.get_service_account_path(), "r", encoding="utf-8") as f:
                self.credentials_info, self.credentials_source = json.load(f), "legacy"

    def credentials_summary(self):
        """One-line, non-secret description of the stored credentials for the UI."""
        info = self.credentials_info
        if info is None:
            return self.credentials_error or "No key stored yet. Choose the key file you downloaded from Firebase."
        who = f"project {info.get('project_id')}, account {info.get('client_email')}"
        if self.credentials_source == "legacy":
            return f"Found an UNPROTECTED key file for {who}. You'll be offered to encrypt it."
        return f"Stored encrypted for {who}. The key itself is never shown."

    def notify_credentials_changed(self):
        for listener in list(self.credentials_listeners):
            try:
                listener()
            except tk.TclError:
                self.credentials_listeners.remove(listener)  # its window has been closed

    def has_firebase_setup(self):
        """Checks whether both halves of the Firebase setup are in place: a
        saved web config (embedded in the mobile pairing QR) and a service
        account key (for this app's own Admin SDK access)."""
        return self.credentials_info is not None and bool(self.get_saved_firebase_web_config())

    def connect_firestore(self, info=None, verify=False):
        """Initializes the Firebase Admin SDK connection from the in-memory key (or a
        candidate `info` being tested). With verify=True it also performs a real read,
        which proves the key works and has Firestore access."""
        info = info or self.credentials_info
        if not info:
            self.db = None
            return False, self.credentials_error or "No Firebase credentials are stored yet."

        try:
            try:
                firebase_admin.delete_app(firebase_admin.get_app())
            except ValueError:
                pass  # no default app yet
            firebase_admin.initialize_app(credentials.Certificate(info))
            db = admin_firestore.client()
            if verify:
                db.collection("presence").limit(1).get()
            self.db = db
            return True, "Connected."
        except Exception as e:
            self.db = None
            return False, f"Could not connect to Firebase: {e}"

    def delete_plaintext_key(self, path):
        """Overwrites and deletes a plain key file. Returns an error message or None."""
        try:
            secure_store.secure_delete(path)
            return None
        except OSError as e:
            return f"Couldn't delete {path}: {e}. Delete it yourself."

    def import_credentials_flow(self, parent, on_done=None):
        """The only way to add or replace the Firebase key. Picks a key file, validates it,
        tests it against Firestore, asks for confirmation, then stores it encrypted and
        offers to delete the file. The key itself is never displayed."""
        path = filedialog.askopenfilename(
            parent=parent,
            title="Choose your Firebase service account key",
            filetypes=[("JSON key file", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return False

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Firebase key", f"Could not read that file as a key: {e}", parent=parent)
            return False
        valid, message = validate_service_account_info(data)
        if not valid:
            messagebox.showwarning("Firebase key", message, parent=parent)
            return False

        lines = []
        previous = self.credentials_info
        if previous:
            lines += ["This REPLACES the stored Firebase credentials.",
                      f"   Now: {previous.get('project_id')} / {previous.get('client_email')}",
                      f"   New: {data['project_id']} / {data['client_email']}"]
        else:
            lines += [f"Project: {data['project_id']}", f"Service account: {data['client_email']}"]

        web_config = self.get_saved_firebase_web_config()
        if web_config and web_config.get("projectId") != data["project_id"]:
            lines += ["", f"WARNING: the saved web config is for project '{web_config.get('projectId')}', "
                          "but this key is for a different project. The phone and this PC would use different databases."]
        lines += ["", "The key will be tested, then encrypted for this Windows account. "
                      "The app never shows it again.", "", "Continue?"]
        if not messagebox.askyesno("Store this Firebase key?", "\n".join(lines), parent=parent):
            return False

        connected, error = self.connect_firestore(info=data, verify=True)
        if not connected:
            self.connect_firestore()  # go back to the previous credentials, if any
            messagebox.showerror("Firebase key", f"{error}\n\nNothing was changed.", parent=parent)
            return False

        try:
            secure_store.save_service_account(self.credentials_store_path(), data)
        except secure_store.SecureStoreError as e:
            self.connect_firestore()
            messagebox.showerror("Firebase key", f"{e}\n\nNothing was changed.", parent=parent)
            return False
        self.reload_credentials()
        self.connect_firestore()
        self.notify_credentials_changed()

        self.catalog.log_system_event(
            "Firebase credentials replaced." if previous else "Firebase credentials stored.",
            {"project_id": data["project_id"], "account": data["client_email"]},
        )

        # Plain-text copies are the main leak risk, so offer to remove every one.
        copies = [path]
        legacy = self.get_service_account_path()
        if os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(path):
            copies.append(legacy)
        if messagebox.askyesno(
            "Delete the key file?",
            "The key is now stored encrypted. Delete the plain-text file(s)?\n\n   " + "\n   ".join(copies)
            + "\n\nRecommended: a leftover copy can be opened by anyone using this PC.",
            parent=parent,
        ):
            problems = [m for m in (self.delete_plaintext_key(p) for p in copies if os.path.exists(p)) if m]
            if problems:
                messagebox.showwarning("Delete the key file", "\n".join(problems), parent=parent)

        messagebox.showinfo(
            "Firebase key stored",
            "The key is stored encrypted.\n\nIf an older key was ever left on disk, in Downloads, or "
            "shared, also delete that key in the Firebase console (Project settings > Service accounts).",
            parent=parent,
        )
        if on_done:
            on_done()
        return True

    def offer_credential_migration(self):
        """If the key is still a plain config/service_account.json, offer to encrypt and remove it."""
        if self.credentials_source != "legacy":
            return
        info = self.credentials_info
        legacy = self.get_service_account_path()
        if not messagebox.askyesno(
            "Protect your Firebase key",
            "Your Firebase key is a plain file that anyone using this PC can open:\n\n"
            f"   {legacy}\n\nProject: {info.get('project_id')}\nAccount: {info.get('client_email')}\n\n"
            "Encrypt it for this Windows account and delete the plain file now?",
        ):
            return
        try:
            secure_store.save_service_account(self.credentials_store_path(), info)
        except secure_store.SecureStoreError as e:
            messagebox.showerror("Protect your Firebase key", f"{e}\n\nNothing was changed.")
            return

        problem = self.delete_plaintext_key(legacy)
        self.reload_credentials()
        self.notify_credentials_changed()
        self.catalog.log_system_event(
            "Firebase credentials moved into encrypted storage.",
            {"project_id": info.get("project_id"), "account": info.get("client_email")},
        )
        messagebox.showinfo(
            "Firebase key protected",
            "The key is now stored encrypted." + (f"\n\n{problem}" if problem else "")
            + "\n\nBecause the plain file existed for a while, consider replacing the key in the "
              "Firebase console and importing the new one from Settings.",
        )

    def save_firebase_web_config(self, config):
        """Stores the Firebase web config (a dict) for later use."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('firebase_web_config', ?)", (json.dumps(config),))
        conn.commit()
        conn.close()

    def load_or_create_connection_token(self):
        """Creates a persistent pairing token for the active desktop session."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key='connection_token'")
            result = cursor.fetchone()

            if result and result[0]:
                conn.close()
                return result[0]

            token = "st_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('connection_token', ?)", (token,))
            conn.commit()
            conn.close()
            return token
        except Exception:
            return "st_fallback_token"

    def get_saved_firebase_web_config(self):
        """Returns the saved Firebase web config (a dict) if one exists."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key='firebase_web_config'")
            result = cursor.fetchone()
            conn.close()
            return json.loads(result[0]) if result and result[0] else None
        except Exception:
            return None

    def check_mobile_connection(self):
        """Checks the presence doc the mobile app's heartbeat writes to, to
        see whether the mobile app is currently active."""
        if not self.db:
            return False

        try:
            doc = self.db.collection('presence').document(self.connection_token).get()
            if not doc.exists:
                return False
            last_seen = (doc.to_dict() or {}).get('lastSeen')
            if last_seen is None:
                return False
            elapsed = (datetime.now(timezone.utc) - last_seen).total_seconds()
            return 0 <= elapsed <= 120
        except Exception:
            return False

    def update_connection_status_label(self):
        """Refreshes the live connection label in the dashboard."""
        connected = self.check_mobile_connection()
        if connected:
            self.connection_status_var.set("●  Mobile connected")
            colors = {"bg": "#dcfce7", "fg": "#166534"}
        else:
            self.connection_status_var.set("○  Mobile offline")
            colors = {"bg": "#e5e7eb", "fg": "#4b5563"}
        if self.status_pill is not None:
            self.theme.set_colors(self.status_pill, **colors)

    def sync_from_firestore(self, silent=False):
        """
        Pulls any unsynced scan documents from Firestore, applies them to
        the local `inventory` table, then deletes them remotely. Docs are
        only ever deleted AFTER the local SQLite commit has succeeded, so a
        network failure between read and delete can at worst cause a scan
        to be re-applied once (harmless double count in a rare failure
        case) rather than lost.
        """
        if not self.db:
            return False

        try:
            docs = list(
                self.db.collection('scans')
                .where('token', '==', self.connection_token)
                .stream()
            )
        except Exception as e:
            if not silent:
                messagebox.showerror("Sync Failed", f"Could not fetch new scans: {e}")
            return False

        if not docs:
            return True

        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            for doc in docs:
                row = doc.to_dict() or {}
                barcode = str(row.get("barcode") or "").strip()
                expiry = str(row.get("expiry") or "").strip()
                action = str(row.get("action") or "").strip().upper()

                if not barcode or not expiry or action not in ("ADD", "REMOVE"):
                    continue

                try:
                    quantity = int(float(row.get("quantity") or 0))
                except (TypeError, ValueError):
                    quantity = 0

                if quantity <= 0:
                    continue

                # Resolves the scanned alias/item code to one product. Unknown
                # identifiers become unnamed products; identifiers that match
                # several products are queued in pending_scans for the user.
                self.catalog.apply_scan(conn, barcode, action, expiry, quantity, row.get("timestamp"))

            # Clean up any product/expiry combo that's dropped to zero or below after REMOVE actions.
            cursor.execute("DELETE FROM inventory WHERE quantity <= 0")
            conn.commit()
        except Exception as e:
            if conn is not None:
                conn.rollback()
            self.catalog.reload_index()
            if not silent:
                messagebox.showerror("Sync Failed", f"Could not save new scans locally: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

        # Only clear the docs from Firestore now that they're safely committed locally.
        try:
            batch = self.db.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
        except Exception as e:
            if not silent:
                messagebox.showwarning(
                    "Partial Sync",
                    f"New scans were saved locally, but couldn't clear them from "
                    f"Firestore: {e}. They'll simply be re-applied next sync, "
                    "which is safe unless it keeps failing repeatedly."
                )

        return True

    def start_connection_polling(self):
        """Starts periodic polling of the mobile connection status, and pulls
        in any newly scanned rows from Firestore on the same cadence."""
        self.update_connection_status_label()

        def poll():
            self.update_connection_status_label()
            self.sync_from_firestore(silent=True)
            self.refresh_data(run_sync=False)
            self.connection_poll_job = self.root.after(30000, poll)

        if self.connection_poll_job is None:
            self.connection_poll_job = self.root.after(30000, poll)

    def stop_connection_polling(self):
        """Stops the periodic mobile connection polling."""
        if self.connection_poll_job is not None:
            self.root.after_cancel(self.connection_poll_job)
            self.connection_poll_job = None

    def reset_app_state(self):
        """Resets the app to its first-run state by clearing saved data and setup settings."""
        try:
            self.catalog.clear_stock()   # logged row by row; the history itself is never cleared
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM settings")
            conn.commit()
            conn.close()

            if hasattr(self, "tree") and self.tree is not None:
                for item in self.tree.get_children():
                    self.tree.delete(item)

            self.setup_completed = False
            messagebox.showinfo("Reset Complete", "The app has been reset to its default state. Restarting setup now.")
            self.root.destroy()
            if app_paths.is_frozen():
                os.execl(sys.executable, sys.executable)
            else:
                os.execl(sys.executable, sys.executable, *sys.argv)
        except Exception as e:
            messagebox.showerror("Reset Failed", f"Could not reset the app: {e}")

    def open_developer_tools(self):
        """Shows developer-only maintenance tools for the app."""
        dev_window = tk.Toplevel(self.root)
        dev_window.title("Developer Tools")
        fit_window(dev_window, 420, 260, self.root)
        dev_window.configure(bg="white")
        dev_window.resizable(False, False)
        dev_window.attributes('-topmost', True)

        panel = tk.Frame(dev_window, bg="white", padx=20, pady=20)
        panel.pack(fill=tk.BOTH, expand=True)

        tk.Label(panel, text="Developer Tools", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(panel, text="Use these tools to clear the stored setup and return the app to its default first-run state.", font=(UI_FONT, 9), bg="white", fg="#4b5563", wraplength=360, justify=tk.LEFT).pack(anchor="w", pady=(6, 14))

        def confirm_reset():
            confirm = messagebox.askyesno(
                "Reset App State",
                "This will clear inventory data, the saved Firebase web config, and all local setup. The stock history is kept. Continue?"
            )
            if not confirm:
                return
            dev_window.destroy()
            self.reset_app_state()

        tk.Button(panel, text="Reset App State", command=confirm_reset, bg="#ef4444", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(anchor="w")
        tk.Button(panel, text="Close", command=dev_window.destroy, bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=8).pack(anchor="w", pady=(10, 0))
        self.theme.style(dev_window)

    def open_url(self, url):
        """Opens a help URL in the user's default browser."""
        webbrowser.open_new(url)

    def build_pairing_payload(self, web_config=None):
        """Builds the JSON payload the mobile app scans to pair: the
        Firebase web config plus this desktop session's pairing token."""
        config = dict(web_config or self.get_saved_firebase_web_config() or {})
        config["token"] = self.connection_token
        return json.dumps(config)

    def show_initial_setup(self):
        """Shows a first-run wizard before the dashboard opens."""
        setup_window = tk.Toplevel(self.root)
        setup_window.title("Stock Tracker Setup")
        # Capped to the screen so the window (and its pinned footer buttons) never runs off the edge.
        fit_window(setup_window, 640, 900, self.root)
        setup_window.configure(bg="white")
        setup_window.resizable(False, True)
        setup_window.attributes('-topmost', True)
        setup_window.protocol("WM_DELETE_WINDOW", self.root.destroy)

        # Action buttons live in a footer pinned to the bottom, outside the
        # scrolling area, so they stay visible no matter the window height.
        footer = tk.Frame(setup_window, bg="white", padx=24, pady=12, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)

        scroll_canvas = tk.Canvas(setup_window, bg="white", highlightthickness=0)
        scroll_bar = ttk.Scrollbar(setup_window, orient=tk.VERTICAL, command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=scroll_bar.set)
        scroll_bar.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        container = tk.Frame(scroll_canvas, bg="white", padx=24, pady=24)
        container_window = scroll_canvas.create_window((0, 0), window=container, anchor="nw")
        container.bind("<Configure>", lambda _e: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind("<Configure>", lambda e: scroll_canvas.itemconfigure(container_window, width=e.width))
        setup_window.bind("<MouseWheel>", lambda e: scroll_canvas.yview_scroll(int(-e.delta / 120), "units"))

        tk.Label(container, text="Welcome to Stock Tracker", font=(UI_FONT, 18, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(container, text="Finish this one-time setup before opening the dashboard.", font=(UI_FONT, 10), bg="white", fg="#4b5563").pack(anchor="w", pady=(4, 14))

        intro_frame = tk.Frame(container, bg="#eff6ff", bd=1, relief=tk.SOLID, padx=16, pady=16)
        intro_frame.pack(fill=tk.X)
        tk.Label(intro_frame, text="What you need", font=(UI_FONT, 11, "bold"), bg="#eff6ff", fg="#1d4ed8").pack(anchor="w")
        tk.Label(
            intro_frame,
            text=(
                "1. A Firebase project with Firestore enabled.\n"
                "2. A Firebase service account key file (you choose it below; it is then stored encrypted).\n"
                "3. The Firebase web app config, pasted below."
            ),
            font=(UI_FONT, 9),
            bg="#eff6ff",
            fg="#1f2937",
            justify=tk.LEFT,
            wraplength=480,
        ).pack(anchor="w", pady=(8, 0))

        guide_frame = tk.Frame(container, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=16)
        guide_frame.pack(fill=tk.X, pady=(16, 0))
        tk.Label(guide_frame, text="How to set up Firebase", font=(UI_FONT, 11, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w")

        def add_step(parent, number, title, detail):
            step_row = tk.Frame(parent, bg="#f9fafb")
            step_row.pack(fill=tk.X, anchor="w", pady=(10, 0))

            badge = tk.Label(step_row, text=str(number), width=2, height=1, bg="#3b82f6", fg="white", font=(UI_FONT, 10, "bold"))
            badge.pack(side=tk.LEFT, padx=(0, 10))

            text_block = tk.Frame(step_row, bg="#f9fafb")
            text_block.pack(side=tk.LEFT, fill=tk.X, expand=True)

            tk.Label(text_block, text=title, font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#111827", anchor="w", justify=tk.LEFT).pack(anchor="w")
            tk.Label(text_block, text=detail, font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563", wraplength=430, justify=tk.LEFT).pack(anchor="w", pady=(2, 0))

        add_step(guide_frame, 1, "Create a Firebase project", "console.firebase.google.com > Add project. The free Spark plan is enough for this app.")
        add_step(guide_frame, 2, "Enable Firestore", "Build > Firestore Database > Create database.")
        add_step(guide_frame, 3, "Download a service account key", "Google Cloud console > IAM & Admin > Service accounts > Keys > Add key > JSON. Save it anywhere; you choose it below and the app stores it encrypted and can delete the file.")
        add_step(guide_frame, 4, "Register a web app", "Project Settings > General > Your apps > Add app > Web. Copy the shown firebaseConfig object and paste it below.")

        sa_frame = tk.Frame(container, bg="white")
        sa_frame.pack(fill=tk.X, pady=(18, 0))

        tk.Label(sa_frame, text="Firebase Service Account Key", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")
        sa_status_text = tk.StringVar()
        tk.Label(sa_frame, textvariable=sa_status_text, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))

        def refresh_key_status():
            sa_status_text.set(self.credentials_summary())

        tk.Button(sa_frame, text="Choose key file...", command=lambda: self.import_credentials_flow(setup_window, refresh_key_status), bg="#10b981", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(anchor="w")
        refresh_key_status()
        self.credentials_listeners.append(refresh_key_status)

        form_frame = tk.Frame(container, bg="white")
        form_frame.pack(fill=tk.X, pady=(18, 0))

        tk.Label(form_frame, text="Firebase Web Config (JSON)", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(form_frame, text="Paste the firebaseConfig object from Project Settings > General > Your apps.", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 8))

        config_entry = ThemedScrolledText(form_frame, height=8, wrap=tk.WORD, font=("Consolas", 9), bg="white", fg="#111827", relief=tk.SOLID, bd=1)
        config_entry.pack(fill=tk.X)

        prefs_frame = tk.Frame(container, bg="white")
        prefs_frame.pack(fill=tk.X, pady=(18, 0))
        tk.Label(prefs_frame, text="Expiring-soon warning", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(prefs_frame, text="Stock expiring within this window is highlighted yellow. You can change this later from Settings.", font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
        warning_combo = ttk.Combobox(prefs_frame, state="readonly", width=14, values=self.warning_day_choices())
        warning_combo.set(f"{self.expiry_warning_days} days")
        warning_combo.pack(anchor="w")

        tip_text = tk.StringVar(value="Paste your Firebase web config above.")
        tk.Label(container, textvariable=tip_text, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(10, 0))

        qr_hint_text = (
            "QR support is installed. A preview will be generated from the config below."
            if QR_AVAILABLE
            else "Optional: install QR support with 'pip install qrcode pillow' to generate a scannable QR code here."
        )
        tk.Label(container, text=qr_hint_text, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(6, 0))

        button_row = tk.Frame(footer, bg="white")
        button_row.pack(fill=tk.X)

        def continue_setup():
            if self.credentials_info is None:
                message = self.credentials_error or "Choose your Firebase key file first."
                tip_text.set(message)
                messagebox.showwarning("Firebase Key Missing", message)
                return

            is_valid, config, message = validate_firebase_web_config(config_entry.get("1.0", tk.END))
            tip_text.set(message)
            if not is_valid:
                messagebox.showwarning("Invalid Config", message)
                return

            self.save_firebase_web_config(config)
            self.set_warning_days(int(warning_combo.get().split()[0]), refresh=False)
            connected, connect_message = self.connect_firestore(verify=True)
            if not connected:
                messagebox.showwarning("Connection Failed", connect_message)
                return

            self.setup_completed = True
            setup_window.destroy()
            self.root.deiconify()
            self.refresh_data(run_sync=True, silent_sync=True)
            self.start_connection_polling()

        def open_mobile_instructions():
            messagebox.showinfo(
                "Mobile Setup",
                "After saving setup here, open Connect Mobile from the dashboard and scan the QR code with the mobile app."
            )

        def test_connection():
            if self.credentials_info is None:
                message = self.credentials_error or "Choose your Firebase key file first."
                tip_text.set(message)
                messagebox.showwarning("Firebase Key Missing", message)
                return

            is_valid, config, message = validate_firebase_web_config(config_entry.get("1.0", tk.END))
            tip_text.set(message)
            if not is_valid:
                messagebox.showwarning("Invalid Config", message)
                return

            connected, connect_message = self.connect_firestore(verify=True)
            tip_text.set(connect_message)
            if connected:
                messagebox.showinfo("Connection OK", connect_message)
            else:
                messagebox.showwarning("Connection Failed", connect_message)

        tk.Button(button_row, text="Save and Open Dashboard", command=continue_setup, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.RIGHT)
        tk.Button(button_row, text="Test Connection", command=test_connection, bg="#10b981", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.RIGHT, padx=(0, 10))
        tk.Button(button_row, text="Need Help?", command=open_mobile_instructions, bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.RIGHT, padx=(0, 10))

        qr_frame = tk.Frame(container, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=16)
        qr_frame.pack(fill=tk.X, pady=(16, 0))

        tk.Label(qr_frame, text="Mobile QR Preview", font=(UI_FONT, 11, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w")
        tk.Label(qr_frame, text="Scan the following barcode on the mobile app.", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(4, 8))
        tk.Label(qr_frame, text="This QR code contains the Firebase web config and pairing token you set above.", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(6, 8))

        qr_display = tk.Label(qr_frame, bg="white", bd=1, relief=tk.SOLID)
        qr_display.pack(anchor="w")

        def refresh_qr_preview():
            if not QR_AVAILABLE:
                qr_display.configure(text="Install qrcode and pillow to generate a QR preview.", image="", width=44, height=5, padx=12, pady=12, justify=tk.LEFT)
                return

            is_valid, config, _ = validate_firebase_web_config(config_entry.get("1.0", tk.END))
            if not is_valid:
                qr_display.configure(text="Paste a valid Firebase web config above, then preview again.", image="", width=44, height=5, padx=12, pady=12, justify=tk.LEFT)
                return

            pairing_payload = self.build_pairing_payload(config)

            qrcode = importlib.import_module("qrcode")
            PILImage = importlib.import_module("PIL.Image")
            ImageTk = importlib.import_module("PIL.ImageTk")

            qr_code = qrcode.QRCode(box_size=6, border=2)
            qr_code.add_data(pairing_payload)
            qr_code.make(fit=True)
            qr_image = qr_code.make_image(fill_color="black", back_color="white")
            qr_image = qr_image.convert("RGB").resize((280, 280), PILImage.Resampling.NEAREST)
            tk_image = ImageTk.PhotoImage(qr_image)
            qr_display.configure(image=tk_image, text="")
            qr_display.image = tk_image

        tk.Button(qr_frame, text="Generate QR Preview", command=refresh_qr_preview, bg="#10b981", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(anchor="w", pady=(10, 0))
        refresh_qr_preview()

        links_frame = tk.Frame(container, bg="white")
        links_frame.pack(fill=tk.X, pady=(14, 0))

        tk.Label(links_frame, text="Helpful links", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")

        def add_link(parent, text, url):
            link = tk.Label(parent, text=text, bg="white", fg="#2563eb", cursor="hand2", font=(UI_FONT, 9, "underline"))
            link.pack(anchor="w", pady=(4, 0))
            link.bind("<Button-1>", lambda _event: self.open_url(url))

        add_link(links_frame, "Open Firebase Console", "https://console.firebase.google.com/")
        add_link(links_frame, "Firestore Security Rules guide", "https://firebase.google.com/docs/firestore/security/get-started")
        add_link(links_frame, "Add Firebase to your web app", "https://firebase.google.com/docs/web/setup")

        qr_display._keep_subtree = True  # QR codes must stay dark-on-white to scan
        self.theme.style(setup_window)
        setup_window.lift()
        setup_window.focus_force()

    def setup_ui(self):
        """Builds the dashboard: header, toolbar, inventory table and legend."""
        self.root.minsize(920, min(560, max(400, self.root.winfo_screenheight() - 100)))

        # Header
        header = tk.Frame(self.root, bg="#ffffff", padx=28, pady=18)
        header.pack(fill=tk.X, side=tk.TOP)

        title_block = tk.Frame(header, bg="#ffffff")
        title_block.pack(side=tk.LEFT)
        tk.Label(title_block, text="Stock Tracker", font=(UI_FONT, 20, "bold"), bg="#ffffff", fg="#111827").pack(anchor="w")
        tk.Label(title_block, text="Active inventory overview", font=(UI_FONT, 10), bg="#ffffff", fg="#6b7280").pack(anchor="w")

        self.theme_button = make_button(header, "☾  Dark mode", self.toggle_theme, "neutral", padx=14, pady=6)
        self.theme_button.pack(side=tk.RIGHT)
        self.status_pill = tk.Label(header, textvariable=self.connection_status_var, font=(UI_FONT, 9, "bold"),
                                    bg="#e5e7eb", fg="#4b5563", padx=14, pady=6)
        self.status_pill.pack(side=tk.RIGHT, padx=(0, 12))

        tk.Frame(self.root, bg="#e5e7eb", height=1).pack(fill=tk.X, side=tk.TOP)

        # Toolbar: catalog, report and history windows on the left, app windows on the right
        toolbar = tk.Frame(self.root, bg="#f3f4f6", padx=28)
        toolbar.pack(fill=tk.X, side=tk.TOP, pady=(18, 0))

        make_button(toolbar, "📦  Products", self.open_catalog_window, "primary").pack(side=tk.LEFT, padx=(0, 10))
        self.pending_button = make_button(toolbar, "⚠  Pending scans (0)", self.open_pending_window, "muted")
        self.pending_button.pack(side=tk.LEFT)
        make_button(toolbar, "⬇  Export", self.open_export_dialog, "neutral").pack(side=tk.LEFT, padx=(10, 0))
        make_button(toolbar, "🕘  History", self.open_history_window, "neutral").pack(side=tk.LEFT, padx=(10, 0))

        make_button(toolbar, "🛠  Dev Tools", self.open_developer_tools, "neutral").pack(side=tk.RIGHT, padx=(10, 0))
        make_button(toolbar, "⚙  Settings", self.open_settings_window, "neutral").pack(side=tk.RIGHT, padx=(10, 0))
        make_button(toolbar, "📱  Connect Mobile", self.show_mobile_setup, "neutral").pack(side=tk.RIGHT, padx=(10, 0))

        # Actions on the stock table sit in their own row just above it, so the top toolbar can't overflow
        stock_actions = tk.Frame(self.root, bg="#f3f4f6", padx=28)
        stock_actions.pack(fill=tk.X, side=tk.TOP, pady=(12, 0))
        make_button(stock_actions, "＋  Add Item", self.open_add_item_dialog, "primary").pack(side=tk.LEFT)
        make_button(stock_actions, "🗑  Remove Selected", self.delete_selected, "danger").pack(side=tk.LEFT, padx=(10, 0))
        make_button(stock_actions, "↻  Refresh", self.refresh_data, "success").pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(stock_actions, text="Right-click a row to edit it.", font=(UI_FONT, 9), bg="#f3f4f6", fg="#6b7280").pack(side=tk.RIGHT)
        self.root.bind("<Control-n>", lambda _e: self.open_add_item_dialog())

        # The window may never be narrower than the toolbar needs (button widths depend on the PC's text size)
        self.root.update_idletasks()
        self.root.minsize(max(920, toolbar.winfo_reqwidth() + 8), min(560, max(400, self.root.winfo_screenheight() - 100)))

        # Legend goes in before the table so the table can't squeeze it out
        legend_frame = tk.Frame(self.root, bg="#f3f4f6", padx=28, pady=12)
        legend_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(legend_frame, text="Legend", font=(UI_FONT, 9, "bold"), bg="#f3f4f6", fg="#6b7280").pack(side=tk.LEFT, padx=(0, 14))
        tk.Label(legend_frame, text="■  Expired", fg="#ef4444", font=(UI_FONT, 10, "bold"), bg="#f3f4f6").pack(side=tk.LEFT, padx=(0, 16))
        self.legend_soon_label = tk.Label(legend_frame, text=f"■  Expiring within {self.expiry_warning_days} days",
                                          fg="#eab308", font=(UI_FONT, 10, "bold"), bg="#f3f4f6")
        self.legend_soon_label.pack(side=tk.LEFT)

        # Inventory table, in a bordered card
        card = tk.Frame(self.root, bg="#ffffff", relief=tk.SOLID, bd=1)
        card.pack(fill=tk.BOTH, expand=True, padx=28, pady=(12, 0))

        tree_scroll = ttk.Scrollbar(card)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        columns = ("ID", "Product", "Barcode", "Item Code", "Expiry Date", "Quantity", "Status")
        self.tree = ttk.Treeview(card, columns=columns, show="headings", yscrollcommand=tree_scroll.set)
        self.tree.pack(fill=tk.BOTH, expand=True)
        tree_scroll.config(command=self.tree.yview)

        for column, title, width, anchor in (
            ("ID", "SYS ID", 60, tk.CENTER),
            ("Product", "PRODUCT", 280, tk.W),
            ("Barcode", "ALIAS / BARCODE", 150, tk.W),
            ("Item Code", "ITEM CODE", 110, tk.W),
            ("Expiry Date", "EXPIRY", 100, tk.CENTER),
            ("Quantity", "QTY", 80, tk.CENTER),
            ("Status", "STATUS", 130, tk.CENTER),
        ):
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor)

        # Right-click menu: edit a single row, or remove any number of rows
        self.row_menu = tk.Menu(self.root, tearoff=0, bg="#ffffff", fg="#111827", font=(UI_FONT, 10), relief=tk.FLAT, bd=1)
        self.row_menu.add_command(label="✎  Edit information...", command=self.edit_selected)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="🗑  Remove selected", command=self.delete_selected)
        self.tree.bind("<Button-3>", self.show_row_menu)

        # Row colours for expired / expiring / good stock follow the theme
        self.theme.register_row_tags(self.tree)

        self.theme.style(self.root)
        self.update_theme_button()

    def refresh_data(self, run_sync=True, silent_sync=False):
        """Pulls new scans from the log sheet (unless run_sync is False),
        then fetches data from SQLite and populates the Treeview, calculating
        expiry status."""
        if run_sync:
            self.sync_from_firestore(silent=silent_sync)

        # Clear existing data in the tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT i.id, i.barcode, i.expiry_date, i.quantity, "
                "p.description, p.item_code, p.alias "
                "FROM inventory i LEFT JOIN products p ON p.sys_key = i.sys_key "
                "ORDER BY i.expiry_date ASC"
            )
            rows = cursor.fetchall()
            conn.close()

            current_date = datetime.now()

            for row in rows:
                db_id, raw_barcode, expiry_str, qty, description, item_code, alias = row
                product_name = description or PLACEHOLDER_LABEL
                barcode = alias or raw_barcode
                item_code = item_code or ""

                status, tag = TREE_STATUS[expiry_report.status_for(expiry_str, self.expiry_warning_days, current_date)]

                # Insert row into tree
                self.tree.insert("", tk.END, values=(db_id, product_name, barcode, item_code, expiry_str, qty, status), tags=(tag,))

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load data: {e}")

        self.update_pending_badge()

    def update_pending_badge(self):
        """Shows how many scans are waiting for the user to pick a product."""
        if self.pending_button is None:
            return
        count = self.catalog.pending_count()
        self.pending_button.config(text=f"⚠  Pending scans ({count})")
        self.theme.set_colors(self.pending_button, bg="#f59e0b" if count else "#6b7280")

    def delete_selected(self):
        """Deletes the selected row(s) from the SQLite database."""
        selected_items = self.tree.selection()
        
        if not selected_items:
            messagebox.showwarning("Warning", "Please select an item to remove.")
            return

        # Confirm deletion
        confirm = messagebox.askyesno("Confirm Removal", f"Are you sure you want to permanently remove {len(selected_items)} selected item(s) from inventory?")
        
        if confirm:
            try:
                # The catalog deletes the rows and writes one history event per row, together.
                ids = [int(self.tree.item(item, "values")[0]) for item in selected_items]
                self.catalog.remove_stock_rows(ids)

                # Refresh UI to show updated data
                self.refresh_data()
                messagebox.showinfo("Success", "Items successfully removed.")
                
            except Exception as e:
                messagebox.showerror("Database Error", f"Failed to delete items: {e}")

    def open_catalog_window(self):
        """Lists the product catalog and lets the user import or update it from a CSV file."""
        if self.catalog_window is not None and self.catalog_window.winfo_exists():
            self.catalog_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.catalog_window = win
        win.title("Product Catalog")
        fit_window(win, 860, 640, self.root, min_width=640)
        win.configure(bg="white")

        header = tk.Frame(win, bg="white", padx=20, pady=16)
        header.pack(fill=tk.X)
        tk.Label(header, text="Product Catalog", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            header,
            text=(
                "Maps each alias/barcode and item code to one product, so scanning either finds the same stock. "
                "Importing a file adds and updates products; it never deletes existing ones. "
                "To start over, use Replace or Reset in Settings."
            ),
            font=(UI_FONT, 9), bg="white", fg="#4b5563", wraplength=800, justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))
        stats_var = tk.StringVar()
        tk.Label(header, textvariable=stats_var, font=(UI_FONT, 9, "bold"), bg="white", fg="#1d4ed8").pack(anchor="w", pady=(8, 0))

        actions = tk.Frame(win, bg="white", padx=20)
        actions.pack(fill=tk.X)
        undo_info_var = tk.StringVar()

        search_var = tk.StringVar()
        search_entry = tk.Entry(actions, textvariable=search_var, width=30, relief=tk.SOLID, bd=1, font=(UI_FONT, 10))
        search_entry.pack(side=tk.RIGHT, ipady=4)
        tk.Label(actions, text="Search", font=(UI_FONT, 9, "bold"), bg="white", fg="#6b7280").pack(side=tk.RIGHT, padx=(0, 8))

        # The note under the table is packed first (to the bottom) so the table can't squeeze it out
        undo_note = tk.Label(win, textvariable=undo_info_var, font=(UI_FONT, 9), bg="white", fg="#6b7280", anchor="w", padx=20, wraplength=800, justify=tk.LEFT)
        undo_note.pack(side=tk.BOTTOM, fill=tk.X, pady=(0, 10))

        table_frame = tk.Frame(win, bg="white", padx=20, pady=10)
        table_frame.pack(fill=tk.BOTH, expand=True)
        table_scroll = ttk.Scrollbar(table_frame)
        table_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree = ttk.Treeview(table_frame, columns=("Product", "Item Code", "Alias", "Key"), show="headings", yscrollcommand=table_scroll.set)
        table_scroll.config(command=tree.yview)
        tree.pack(fill=tk.BOTH, expand=True)
        for column, width in (("Product", 380), ("Item Code", 130), ("Alias", 170), ("Key", 60)):
            tree.heading(column, text="Sys Key" if column == "Key" else column, anchor=tk.W)
            tree.column(column, width=width, anchor=tk.W)

        def refresh(*_args):
            stats = self.catalog.stats()
            stats_var.set(f"{stats['total']:,} products ({stats['unnamed']:,} unnamed). Showing up to 500 matches.")
            for item in tree.get_children():
                tree.delete(item)
            for product in self.catalog.search_products(search_var.get()):
                tree.insert("", tk.END, values=(
                    product["description"] or PLACEHOLDER_LABEL, product["item_code"], product["alias"], product["sys_key"],
                ))
            allowed, message = self.catalog.can_undo_last_import()
            undo_button.config(state=tk.NORMAL if allowed else tk.DISABLED)
            undo_info_var.set(message)

        def choose_file():
            path = filedialog.askopenfilename(
                parent=win,
                title="Choose a product list",
                filetypes=[("CSV / text files", "*.csv *.tsv *.txt"), ("All files", "*.*")],
            )
            if path:
                self.open_import_dialog(path, refresh)

        def undo_import():
            if not messagebox.askyesno(
                "Undo last change",
                "Restore the products and stock to how they were before the last catalog change "
                "(an import, a replace or a reset)?",
                parent=win,
            ):
                return
            try:
                self.catalog.undo_last_import()
            except ValueError as e:
                messagebox.showwarning("Undo last change", str(e), parent=win)
                return
            refresh()
            self.refresh_data(run_sync=False)
            messagebox.showinfo("Undo last change", "The last catalog change was undone.", parent=win)

        tk.Button(actions, text="Import CSV...", command=choose_file, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=14, pady=6, cursor="hand2").pack(side=tk.LEFT)
        undo_button = tk.Button(actions, text="Undo last change", command=undo_import, bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=6, cursor="hand2")
        undo_button.pack(side=tk.LEFT, padx=(10, 0))

        search_var.trace_add("write", refresh)
        refresh()
        self.catalog_refresh = refresh
        self.theme.style(win)

    def open_import_dialog(self, path, on_done, replace=False):
        """Column-mapping wizard: pick which column is which field, preview what the
        import would change, then apply it. With replace=True the file becomes the whole
        catalog (the old products are cleared, stock is kept and matched by barcode)."""
        try:
            headers, rows, delimiter, encoding = read_table(path)
        except Exception as e:
            messagebox.showerror("Import", f"Could not read that file: {e}")
            return
        if not headers or not rows:
            messagebox.showwarning("Import", "That file has no data rows.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Replace Product Catalog" if replace else "Import Products")
        fit_window(dlg, 900, 840 if replace else 800, self.root, min_width=560)
        dlg.configure(bg="white")
        dlg.transient(self.root)
        dlg.grab_set()

        # The buttons live in a footer pinned to the bottom edge; only the content above scrolls.
        button_row = tk.Frame(dlg, bg="white", padx=20, pady=12, bd=1, relief=tk.GROOVE)
        button_row.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(dlg, padx=20, pady=16)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        delimiter_names = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}
        tk.Label(body, text=f"{'Replace catalog with' if replace else 'Import'}: {os.path.basename(path)}", font=(UI_FONT, 13, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text=f"{len(rows):,} data rows, {delimiter_names[delimiter]}-separated, {encoding}", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 10))
        if replace:
            tk.Label(body, text="This REPLACES the whole catalog: every existing product is removed and this file becomes the catalog. "
                                "Stock on hand is kept and matched back up to the new products by barcode. Nothing changes until you "
                                "preview and confirm.",
                     font=(UI_FONT, 9, "bold"), bg="white", fg="#b91c1c", wraplength=760, justify=tk.LEFT).pack(anchor="w", pady=(0, 10))
        tk.Label(body, text="Choose which column holds each field. Column names and order can differ from file to file.", font=(UI_FONT, 9), bg="white", fg="#4b5563").pack(anchor="w", pady=(0, 6))

        choices = ["(none)"] + [f"{i + 1}: {h or '(blank)'}" for i, h in enumerate(headers)]
        guess = guess_mapping(headers, self.catalog.load_saved_mapping())
        field_labels = {
            "alias": "Alias (barcode)",
            "item_code": "Item Code (internal SKU)",
            "description": "Item Description (product name)",
        }
        combos = {}
        for field in MAPPING_FIELDS:
            row_frame = tk.Frame(body, bg="white")
            row_frame.pack(fill=tk.X, pady=3)
            tk.Label(row_frame, text=field_labels[field], width=30, anchor="w", font=(UI_FONT, 9, "bold"), bg="white").pack(side=tk.LEFT)
            combo = ttk.Combobox(row_frame, values=choices, state="readonly", width=44)
            combo.set(choices[guess[field] + 1] if guess[field] is not None else choices[0])
            combo.pack(side=tk.LEFT)
            combos[field] = combo

        tk.Label(body, text="First rows of the file", font=(UI_FONT, 9, "bold"), bg="white").pack(anchor="w", pady=(12, 4))
        file_frame = tk.Frame(body, bg="white")
        file_frame.pack(fill=tk.X)
        column_ids = [f"c{i}" for i in range(len(headers))]
        file_tree = ttk.Treeview(file_frame, columns=column_ids, show="headings", height=4)
        file_scroll = ttk.Scrollbar(file_frame, orient=tk.HORIZONTAL, command=file_tree.xview)
        file_tree.configure(xscrollcommand=file_scroll.set)
        for column_id, header in zip(column_ids, headers):
            file_tree.heading(column_id, text=header or "(blank)")
            file_tree.column(column_id, width=120, minwidth=60, stretch=False)
        for row in rows[:4]:
            file_tree.insert("", tk.END, values=(row + [""] * len(headers))[:len(headers)])
        file_tree.pack(fill=tk.X)
        file_scroll.pack(fill=tk.X)

        tk.Label(body, text="What this replacement would change" if replace else "What this import would change",
                 font=(UI_FONT, 9, "bold"), bg="white").pack(anchor="w", pady=(12, 4))
        summary_box = ThemedScrolledText(body, height=12, wrap=tk.WORD, font=("Consolas", 9), bg="#f9fafb", relief=tk.SOLID, bd=1, state="disabled")
        summary_box.pack(fill=tk.BOTH, expand=True)

        state = {"records": None, "stats": None, "result": None}

        def set_summary(text):
            summary_box.configure(state="normal")
            summary_box.delete("1.0", tk.END)
            summary_box.insert("1.0", text)
            summary_box.configure(state="disabled")

        def current_mapping():
            mapping = {}
            for field in MAPPING_FIELDS:
                value = combos[field].get()
                mapping[field] = None if value == "(none)" else choices.index(value) - 1
            return mapping

        def invalidate(_event=None):
            # Any change to the column choice makes the last preview stale.
            state["records"] = state["stats"] = state["result"] = None
            import_button.config(state=tk.DISABLED)
            set_summary(f"Click \"Preview changes\" to see what this {'replacement' if replace else 'import'} would do. "
                        f"Nothing is changed until you click {'Replace catalog' if replace else 'Import'}.")

        def run_preview():
            mapping = current_mapping()
            error = validate_mapping(mapping)
            if error:
                messagebox.showwarning("Column choice", error, parent=dlg)
                return

            preview_button.config(state=tk.DISABLED, text="Working...")
            dlg.update_idletasks()
            try:
                records, parse_stats = parse_rows(rows, mapping)
                result = self.catalog.import_records(records, parse_stats, os.path.basename(path), dry_run=True, replace=replace)
            except Exception as e:
                messagebox.showerror("Replace catalog" if replace else "Import", f"Could not preview this file: {e}", parent=dlg)
                return
            finally:
                preview_button.config(state=tk.NORMAL, text="Preview changes")

            state["records"], state["stats"], state["result"] = records, parse_stats, result
            text = format_import_summary(result)
            if replace:
                reset = result["reset"]
                text = (
                    f"Existing catalog cleared: {reset['catalog_entries']:,} products\n"
                    f"Stock kept: {reset['stock_rows']:,} rows. {reset['stock_rows'] - reset['unmatched_stock_rows']:,} match a product in this file; "
                    f"{reset['unmatched_stock_rows']:,} don't and stay as \"{PLACEHOLDER_LABEL}\" under their barcode.\n\n"
                ) + text
            if result["samples"]["new"]:
                text += "\n\nNew products (first few):\n" + "\n".join(
                    f"  {desc or PLACEHOLDER_LABEL}   [code {code or '-'}, alias {alias or '-'}]"
                    for desc, code, alias in result["samples"]["new"]
                )
            if result["samples"]["updated"]:
                text += "\n\nRenamed (first few):\n" + "\n".join(
                    f"  {old or PLACEHOLDER_LABEL}  ->  {new}" for old, new in result["samples"]["updated"]
                )

            changes = replace or result["new"] or result["updated"] or result["upgraded"] or result["absorbed"]
            action = "Replace catalog" if replace else "Import"
            text += "\n\n" + (f"Nothing has been changed yet. Click {action} to apply." if changes else "This file would not change anything.")
            set_summary(text)
            import_button.config(state=tk.NORMAL if changes else tk.DISABLED)

        def run_import():
            mapping = current_mapping()
            if replace:
                reset = state["result"]["reset"]
                if not messagebox.askyesno(
                    "Replace product catalog",
                    f"This removes all {reset['catalog_entries']:,} existing products and replaces them with the "
                    f"{state['result']['unique_products']:,} in this file.\n\n"
                    f"Stock is kept ({reset['stock_rows']:,} rows); {reset['unmatched_stock_rows']:,} of those don't match the "
                    "new file and stay unnamed. A backup copy of the database is saved first, and you can undo this from the "
                    "Product Catalog window until stock next changes.\n\nReplace the catalog?",
                    icon="warning", parent=dlg,
                ):
                    return
                if self.backup_before_catalog_change(dlg, "Replace product catalog") is None:
                    return
            import_button.config(state=tk.DISABLED, text="Replacing..." if replace else "Importing...")
            dlg.update_idletasks()
            try:
                self.catalog.save_mapping(headers, mapping)
                result = self.catalog.import_records(state["records"], state["stats"], os.path.basename(path), replace=replace)
            except Exception as e:
                messagebox.showerror("Replace catalog" if replace else "Import",
                                     f"The {'replacement' if replace else 'import'} failed and nothing was changed: {e}", parent=dlg)
                import_button.config(state=tk.NORMAL, text="Replace catalog" if replace else "Import")
                return

            dlg.destroy()
            on_done()
            self.refresh_data(run_sync=False)
            messagebox.showinfo(
                "Catalog replaced" if replace else "Import complete",
                (f"The catalog now has the products from {os.path.basename(path)}. "
                 f"{result['reset']['unmatched_stock_rows']:,} stock rows aren't in it and stay unnamed.\n\n" if replace else "")
                + format_import_summary(result)
                + "\n\nYou can undo this from the Product Catalog window, until stock next changes.",
            )

        for combo in combos.values():
            combo.bind("<<ComboboxSelected>>", invalidate)

        import_button = tk.Button(button_row, text="Replace catalog" if replace else "Import", command=run_import, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=16, pady=8, state=tk.DISABLED)
        import_button.pack(side=tk.RIGHT)
        tk.Button(button_row, text="Cancel", command=dlg.destroy, bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=16, pady=8).pack(side=tk.RIGHT, padx=(0, 10))
        preview_button = tk.Button(button_row, text="Preview changes", command=run_preview, bg="#10b981", fg="white", relief=tk.FLAT, padx=16, pady=8)
        preview_button.pack(side=tk.RIGHT, padx=(0, 10))

        invalidate()
        self.theme.style(dlg)

    def open_pending_window(self):
        """Scans whose alias/item code matches several products wait here until the user picks one."""
        win = tk.Toplevel(self.root)
        win.title("Pending Scans")
        fit_window(win, 820, 640, self.root, min_width=600)
        win.configure(bg="white")

        # Buttons are pinned to the bottom edge first, so a short window can't push them off screen
        button_row = tk.Frame(win, bg="white", padx=20, pady=14, bd=1, relief=tk.GROOVE)
        button_row.pack(side=tk.BOTTOM, fill=tk.X)

        header = tk.Frame(win, bg="white", padx=20, pady=16)
        header.pack(fill=tk.X)
        tk.Label(header, text="Pending Scans", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            header,
            text="These scans match more than one product. Select a scan, then pick which product it really is by name.",
            font=(UI_FONT, 9), bg="white", fg="#4b5563", wraplength=760, justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        pending_tree = ttk.Treeview(win, columns=("Scanned", "Action", "Expiry", "Qty", "Time"), show="headings", height=5)
        for column, width in (("Scanned", 200), ("Action", 90), ("Expiry", 110), ("Qty", 70), ("Time", 240)):
            pending_tree.heading(column, text="Scanned code" if column == "Scanned" else column, anchor=tk.W)
            pending_tree.column(column, width=width, anchor=tk.W)
        pending_tree.pack(fill=tk.X, padx=20)

        tk.Label(win, text="Which product is it?", font=(UI_FONT, 10, "bold"), bg="white", padx=20).pack(anchor="w", pady=(14, 4))
        candidate_tree = ttk.Treeview(win, columns=("Product", "Item Code", "Alias"), show="headings", height=5)
        for column, width in (("Product", 420), ("Item Code", 130), ("Alias", 190)):
            candidate_tree.heading(column, text=column, anchor=tk.W)
            candidate_tree.column(column, width=width, anchor=tk.W)
        candidate_tree.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        # Tk turns numeric-looking strings in tree values back into numbers,
        # which would drop leading zeros from barcodes, so keep the originals here.
        barcodes = {}

        def refresh():
            barcodes.clear()
            for tree in (pending_tree, candidate_tree):
                for item in tree.get_children():
                    tree.delete(item)
            for scan in self.catalog.list_pending():
                barcodes[str(scan["id"])] = scan["barcode"]
                pending_tree.insert("", tk.END, iid=str(scan["id"]), values=(
                    scan["barcode"], scan["action"], scan["expiry"], scan["quantity"], scan["scanned_at"] or "",
                ))
            self.update_pending_badge()

        def on_select(_event=None):
            for item in candidate_tree.get_children():
                candidate_tree.delete(item)
            selection = pending_tree.selection()
            if not selection:
                return
            for product in self.catalog.candidates_for(barcodes[selection[0]]):
                candidate_tree.insert("", tk.END, iid=str(product["sys_key"]), values=(
                    product["description"] or PLACEHOLDER_LABEL, product["item_code"], product["alias"],
                ))

        def resolve(use_selected_product):
            selection = pending_tree.selection()
            if not selection:
                messagebox.showwarning("Pending Scans", "Select a scan first.", parent=win)
                return
            sys_key = None
            if use_selected_product:
                chosen = candidate_tree.selection()
                if not chosen:
                    messagebox.showwarning("Pending Scans", "Select the product this scan belongs to.", parent=win)
                    return
                sys_key = int(chosen[0])
            try:
                self.catalog.resolve_pending(int(selection[0]), sys_key)
            except Exception as e:
                messagebox.showerror("Pending Scans", f"Could not apply that scan: {e}", parent=win)
                return
            refresh()
            self.refresh_data(run_sync=False)

        tk.Button(button_row, text="Assign to selected product", command=lambda: resolve(True), bg="#3b82f6", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.LEFT)
        tk.Button(button_row, text="Record as unnamed product", command=lambda: resolve(False), bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.LEFT, padx=(10, 0))

        pending_tree.bind("<<TreeviewSelect>>", on_select)
        refresh()
        self.theme.style(win)

    def open_export_dialog(self):
        """Exports the expiry list as PDF or CSV: filtered by status and month, optionally
        grouped, and sorted by any column."""
        rows = expiry_report.load_rows(self.db_path, self.expiry_warning_days)
        if not rows:
            messagebox.showinfo("Export expiry list", "There is no stock to export yet.")
            return

        win = tk.Toplevel(self.root)
        win.title("Export Expiry List")
        fit_window(win, 660, 700, self.root)
        win.configure(bg="white")
        win.transient(self.root)
        win.grab_set()

        footer = tk.Frame(win, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(win, padx=24, pady=18)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        tk.Label(body, text="Export expiry list", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text="Choose what to include and how to arrange it, then save it as a PDF or CSV file.", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 12))

        def card(title):
            frame = tk.Frame(body, bg="#f9fafb", relief=tk.SOLID, bd=1, padx=16, pady=12)
            frame.pack(fill=tk.X, pady=(0, 12))
            tk.Label(frame, text=title, font=(UI_FONT, 10, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w", pady=(0, 6))
            return frame

        def line(parent, label):
            row = tk.Frame(parent, bg="#f9fafb")
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, width=10, anchor="w", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT)
            return row

        def check(parent, text, variable, command=None, value=None):
            options = dict(text=text, variable=variable, command=command, bg="#f9fafb", fg="#111827",
                           font=(UI_FONT, 10), anchor="w", bd=0, highlightthickness=0)
            if value is None:
                return tk.Checkbutton(parent, **options)
            return tk.Radiobutton(parent, value=value, **options)

        # -- what to include --
        include = card("What to include")

        status_row = line(include, "Status")
        status_vars = {}
        for code in expiry_report.STATUS_ORDER:
            status_vars[code] = tk.BooleanVar(value=True)
            check(status_row, expiry_report.STATUS_LABELS[code], status_vars[code], lambda: update_preview()).pack(side=tk.LEFT, padx=(0, 12))

        month_choices = {"Any month": None}
        for month in expiry_report.months_available(rows):
            month_choices[expiry_report.format_month(month)] = month
        month_row = line(include, "Months")
        tk.Label(month_row, text="From", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563").pack(side=tk.LEFT, padx=(0, 6))
        from_combo = ttk.Combobox(month_row, values=list(month_choices), state="readonly", width=16)
        from_combo.set("Any month")
        from_combo.pack(side=tk.LEFT)
        tk.Label(month_row, text="To", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563").pack(side=tk.LEFT, padx=(14, 6))
        to_combo = ttk.Combobox(month_row, values=list(month_choices), state="readonly", width=16)
        to_combo.set("Any month")
        to_combo.pack(side=tk.LEFT)
        tk.Label(include, text="Pick the same month in both boxes to export a single month.", font=(UI_FONT, 8), bg="#f9fafb", fg="#6b7280").pack(anchor="w", pady=(2, 0))

        preview_var = tk.StringVar()
        preview_label = tk.Label(include, textvariable=preview_var, font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#1d4ed8")
        preview_label.pack(anchor="w", pady=(8, 0))

        # -- how to arrange it --
        arrange = card("How to arrange it")

        group_var = tk.StringVar(value="none")
        group_row = line(arrange, "Group by")
        for text, value in (("No grouping", "none"), ("Status", "status"), ("Month", "month")):
            check(group_row, text, group_var, lambda: update_preview(), value=value).pack(side=tk.LEFT, padx=(0, 14))

        column_keys = {heading: key for key, heading in expiry_report.COLUMNS}
        directions = ["Ascending", "Descending"]

        sort_row = line(arrange, "Sort by")
        sort1 = ttk.Combobox(sort_row, values=list(column_keys), state="readonly", width=18)
        sort1.set(expiry_report.COLUMN_HEADINGS["expiry"])
        sort1.pack(side=tk.LEFT)
        sort1_dir = ttk.Combobox(sort_row, values=directions, state="readonly", width=12)
        sort1_dir.set("Ascending")
        sort1_dir.pack(side=tk.LEFT, padx=(8, 0))

        then_row = line(arrange, "Then by")
        sort2 = ttk.Combobox(then_row, values=["(none)"] + list(column_keys), state="readonly", width=18)
        sort2.set(expiry_report.COLUMN_HEADINGS["product"])
        sort2.pack(side=tk.LEFT)
        sort2_dir = ttk.Combobox(then_row, values=directions, state="readonly", width=12)
        sort2_dir.set("Ascending")
        sort2_dir.pack(side=tk.LEFT, padx=(8, 0))

        # -- file format --
        file_card = card("File format")
        format_var = tk.StringVar(value="pdf")
        excel_var = tk.BooleanVar(value=True)
        format_row = line(file_card, "Save as")
        check(format_row, "PDF (for printing or sharing)", format_var, lambda: on_format(), value="pdf").pack(side=tk.LEFT, padx=(0, 14))
        check(format_row, "CSV (for Excel)", format_var, lambda: on_format(), value="csv").pack(side=tk.LEFT)
        excel_check = check(file_card, "Keep long barcodes and leading zeros intact in Excel", excel_var)
        excel_check.pack(anchor="w", padx=(80, 0))

        def on_format():
            excel_check.config(state=tk.NORMAL if format_var.get() == "csv" else tk.DISABLED)

        def current_choices():
            statuses = {code for code, var in status_vars.items() if var.get()}
            month_from, month_to = month_choices[from_combo.get()], month_choices[to_combo.get()]
            error = None
            if not statuses:
                error = "Choose at least one status."
            elif month_from and month_to and month_from > month_to:
                error = "The 'From' month is after the 'To' month."
            sort = [(column_keys[sort1.get()], sort1_dir.get() == "Descending")]
            if sort2.get() != "(none)":
                sort.append((column_keys[sort2.get()], sort2_dir.get() == "Descending"))
            return {"statuses": statuses, "month_from": month_from, "month_to": month_to,
                    "group_by": group_var.get(), "sort": sort, "error": error}

        def build(choices):
            return expiry_report.build_report(
                rows, choices["statuses"], choices["month_from"], choices["month_to"],
                choices["group_by"], choices["sort"],
            )

        def update_preview(_event=None):
            choices = current_choices()
            if choices["error"]:
                preview_var.set(choices["error"])
                self.theme.set_colors(preview_label, fg="#991b1b")
                return
            report = build(choices)
            if report["total_items"]:
                preview_var.set(f"{report['total_items']:,} items ({report['total_quantity']:,} units) will be exported.")
                self.theme.set_colors(preview_label, fg="#1d4ed8")
            else:
                preview_var.set("Nothing matches these choices.")
                self.theme.set_colors(preview_label, fg="#991b1b")

        for combo in (from_combo, to_combo, sort1, sort1_dir, sort2, sort2_dir):
            combo.bind("<<ComboboxSelected>>", update_preview)

        def export():
            choices = current_choices()
            if choices["error"]:
                messagebox.showwarning("Export expiry list", choices["error"], parent=win)
                return
            report = build(choices)
            if not report["total_items"]:
                messagebox.showwarning("Export expiry list", "Nothing matches these choices, so there is nothing to export.", parent=win)
                return

            as_pdf = format_var.get() == "pdf"
            extension = ".pdf" if as_pdf else ".csv"
            path = filedialog.asksaveasfilename(
                parent=win,
                title="Save expiry list",
                defaultextension=extension,
                initialfile=f"Expiry list {datetime.now().strftime('%Y-%m-%d')}{extension}",
                filetypes=[("PDF document", "*.pdf")] if as_pdf else [("CSV file (opens in Excel)", "*.csv")],
            )
            if not path:
                return

            try:
                if as_pdf:
                    expiry_report.write_pdf(path, report, {
                        "title": "Expiry Report",
                        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "lines": expiry_report.describe(
                            choices["statuses"], choices["month_from"], choices["month_to"],
                            choices["group_by"], choices["sort"], self.expiry_warning_days,
                        ),
                        "footer": "Stock Tracker | Expiry report",
                    })
                else:
                    expiry_report.write_csv(path, report, choices["group_by"], excel_var.get())
            except ImportError:
                messagebox.showerror("Export expiry list", "PDF export needs the 'reportlab' package.\n\nInstall it with:  python -m pip install reportlab", parent=win)
                return
            except PermissionError:
                messagebox.showerror("Export expiry list", f"Couldn't save to:\n{path}\n\nIs the file open in another program? Close it and try again.", parent=win)
                return
            except OSError as e:
                messagebox.showerror("Export expiry list", f"Couldn't save the file: {e}", parent=win)
                return

            win.destroy()
            if messagebox.askyesno("Export complete", f"Saved {report['total_items']:,} items to:\n{path}\n\nOpen it now?"):
                try:
                    os.startfile(path)
                except (AttributeError, OSError) as e:
                    messagebox.showwarning("Export expiry list", f"Couldn't open the file: {e}")

        make_button(footer, "Export", export, "primary").pack(side=tk.RIGHT)
        make_button(footer, "Cancel", win.destroy, "neutral").pack(side=tk.RIGHT, padx=(0, 10))

        on_format()
        update_preview()
        self.theme.style(win)

    def open_history_window(self):
        """Browse and export the stock history: every change ever made, newest first."""
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            return

        win = tk.Toplevel(self.root)
        self.history_window = win
        win.title("Stock History")
        fit_window(win, 1120, 740, self.root, min_width=700, min_height=420)
        win.configure(bg="white")

        footer = tk.Frame(win, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        body = tk.Frame(win, bg="white", padx=24, pady=18)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text="Stock history", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            body,
            text="Every change to stock: scans, edits, removals, imports. Entries can't be edited or deleted, "
                 "so this is a reliable record. Export it as CSV for analysis.",
            font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=1040, justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 12))

        filters = tk.Frame(body, bg="#f9fafb", relief=tk.SOLID, bd=1, padx=16, pady=12)
        filters.pack(fill=tk.X)

        # -- date range --
        date_row = tk.Frame(filters, bg="#f9fafb")
        date_row.pack(fill=tk.X, pady=(0, 6))
        tk.Label(date_row, text="Dates", width=8, anchor="w", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT)
        from_var, to_var = tk.StringVar(), tk.StringVar()
        tk.Label(date_row, text="From", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563").pack(side=tk.LEFT, padx=(0, 6))
        tk.Entry(date_row, textvariable=from_var, width=12, relief=tk.SOLID, bd=1, font=(UI_FONT, 10)).pack(side=tk.LEFT, ipady=3)
        tk.Label(date_row, text="To", font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563").pack(side=tk.LEFT, padx=(12, 6))
        tk.Entry(date_row, textvariable=to_var, width=12, relief=tk.SOLID, bd=1, font=(UI_FONT, 10)).pack(side=tk.LEFT, ipady=3)
        tk.Label(date_row, text="(YYYY-MM-DD, blank = no limit)", font=(UI_FONT, 8), bg="#f9fafb", fg="#6b7280").pack(side=tk.LEFT, padx=(10, 14))

        def set_range(days_back):
            today = datetime.now().date()
            from_var.set("" if days_back is None else (today - timedelta(days=days_back)).isoformat())
            to_var.set("" if days_back is None else today.isoformat())

        for text, days_back in (("Today", 0), ("Last 7 days", 6), ("Last 30 days", 29), ("All time", None)):
            make_button(date_row, text, lambda d=days_back: set_range(d), "neutral", padx=10, pady=2, font=(UI_FONT, 9)).pack(side=tk.LEFT, padx=(0, 6))

        # -- event types --
        event_row = tk.Frame(filters, bg="#f9fafb")
        event_row.pack(fill=tk.X, pady=(0, 6))
        tk.Label(event_row, text="Events", width=8, anchor="nw", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT, anchor="n")
        event_grid = tk.Frame(event_row, bg="#f9fafb")
        event_grid.pack(side=tk.LEFT)
        event_vars = {}
        for index, (code, label) in enumerate(stock_history.EVENT_LABELS.items()):
            event_vars[code] = tk.BooleanVar(value=True)
            tk.Checkbutton(event_grid, text=label, variable=event_vars[code], command=lambda: refresh(), bg="#f9fafb", fg="#111827",
                           font=(UI_FONT, 9), anchor="w", bd=0, highlightthickness=0).grid(row=index // 5, column=index % 5, sticky="w", padx=(0, 18))

        # -- search --
        search_row = tk.Frame(filters, bg="#f9fafb")
        search_row.pack(fill=tk.X)
        tk.Label(search_row, text="Search", width=8, anchor="w", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT)
        search_var = tk.StringVar()
        tk.Entry(search_row, textvariable=search_var, width=40, relief=tk.SOLID, bd=1, font=(UI_FONT, 10)).pack(side=tk.LEFT, ipady=3)
        tk.Label(search_row, text="product name, alias, item code, code scanned or note", font=(UI_FONT, 8), bg="#f9fafb", fg="#6b7280").pack(side=tk.LEFT, padx=(10, 0))

        count_var = tk.StringVar()
        count_label = tk.Label(body, textvariable=count_var, font=(UI_FONT, 9, "bold"), bg="white", fg="#1d4ed8")
        count_label.pack(anchor="w", pady=(10, 6))

        # The details line is packed before the table so the table can't squeeze it out.
        detail_var = tk.StringVar(value="Select an event to see its full details.")
        tk.Label(body, textvariable=detail_var, font=(UI_FONT, 9), bg="white", fg="#4b5563", wraplength=1040,
                 justify=tk.LEFT, anchor="w", height=2).pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

        # -- table --
        table_frame = tk.Frame(body, bg="white", relief=tk.SOLID, bd=1)
        table_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(table_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        columns = ("Time", "Event", "Product", "Alias", "Expiry", "Change", "Before", "After", "Note")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", yscrollcommand=scroll.set, height=10)
        scroll.config(command=tree.yview)
        tree.pack(fill=tk.BOTH, expand=True)
        for column, width, anchor in (("Time", 140, tk.W), ("Event", 150, tk.W), ("Product", 200, tk.W), ("Alias", 120, tk.W),
                                      ("Expiry", 80, tk.CENTER), ("Change", 78, tk.CENTER), ("Before", 78, tk.CENTER),
                                      ("After", 70, tk.CENTER), ("Note", 300, tk.W)):
            tree.heading(column, text=column.upper(), anchor=anchor)
            tree.column(column, width=width, anchor=anchor)


        shown = {}
        pending_refresh = {"job": None}

        def date_error():
            for text in (from_var.get().strip(), to_var.get().strip()):
                if text:
                    try:
                        datetime.strptime(text, "%Y-%m-%d")
                    except ValueError:
                        return "Dates must look like 2026-09-25."
            return None

        def normalized_dates():
            return (from_var.get().strip() or None), (to_var.get().strip() or None)

        def refresh(*_args):
            error = date_error()
            for item in tree.get_children():
                tree.delete(item)
            shown.clear()
            if error:
                count_var.set(error)
                self.theme.set_colors(count_label, fg="#991b1b")
                return
            date_from, date_to = normalized_dates()
            events, search = {c for c, v in event_vars.items() if v.get()}, search_var.get()
            total = stock_history.count(self.db_path, date_from, date_to, events, search)
            rows = stock_history.query(self.db_path, date_from, date_to, events, search, limit=500)
            for row in rows:
                shown[str(row["id"])] = row
                delta = "" if row["quantity_delta"] is None else f"{row['quantity_delta']:+d}"
                tree.insert("", tk.END, iid=str(row["id"]), values=(
                    row["occurred_at"][:19].replace("T", " "), stock_history.EVENT_LABELS.get(row["event"], row["event"]),
                    row["product"], row["alias"], row["expiry"], delta,
                    "" if row["quantity_before"] is None else row["quantity_before"],
                    "" if row["quantity_after"] is None else row["quantity_after"], row["note"],
                ))
            text = f"{total:,} event{'s' if total != 1 else ''} match"
            if total > len(rows):
                text += f" (showing the newest {len(rows):,}; the export includes all {total:,})"
            count_var.set(text + ".")
            self.theme.set_colors(count_label, fg="#1d4ed8")

        def schedule_refresh(*_args):
            if pending_refresh["job"] is not None:
                win.after_cancel(pending_refresh["job"])
            pending_refresh["job"] = win.after(250, refresh)

        def show_detail(_event=None):
            selection = tree.selection()
            if not selection:
                return
            row = shown[selection[0]]
            parts = [f"{stock_history.EVENT_LABELS.get(row['event'], row['event'])} at {row['occurred_at']} (source: {row['source']})"]
            if row["raw_code"]:
                parts.append(f"Code scanned: {row['raw_code']}")
            if row["scanned_at"]:
                parts.append(f"Phone clock: {row['scanned_at']}")
            if row["note"]:
                parts.append(row["note"])
            if row["details"]:
                parts.append(f"Details: {row['details']}")
            detail_var.set("   |   ".join(parts))

        tree.bind("<<TreeviewSelect>>", show_detail)
        for variable in (from_var, to_var, search_var):
            variable.trace_add("write", schedule_refresh)

        # -- export --
        excel_var = tk.BooleanVar(value=False)

        def export():
            error = date_error()
            if error:
                messagebox.showwarning("Export stock history", error, parent=win)
                return
            date_from, date_to = normalized_dates()
            events, search = {c for c, v in event_vars.items() if v.get()}, search_var.get()
            rows = stock_history.query(self.db_path, date_from, date_to, events, search, newest_first=False)
            if not rows:
                messagebox.showwarning("Export stock history", "No events match the current filters.", parent=win)
                return
            path = filedialog.asksaveasfilename(
                parent=win, title="Save stock history", defaultextension=".csv",
                initialfile=f"Stock history {datetime.now():%Y-%m-%d}.csv",
                filetypes=[("CSV file (opens in Excel)", "*.csv")],
            )
            if not path:
                return
            try:
                stock_history.write_csv(path, rows, excel_friendly=excel_var.get())
            except PermissionError:
                messagebox.showerror("Export stock history", f"Couldn't save to:\n{path}\n\nIs the file open in another program? Close it and try again.", parent=win)
                return
            except OSError as e:
                messagebox.showerror("Export stock history", f"Couldn't save the file: {e}", parent=win)
                return
            if messagebox.askyesno("Export complete", f"Saved {len(rows):,} events to:\n{path}\n\nOpen it now?", parent=win):
                try:
                    os.startfile(path)
                except (AttributeError, OSError) as e:
                    messagebox.showwarning("Export stock history", f"Couldn't open the file: {e}", parent=win)

        make_button(footer, "Export CSV...", export, "primary").pack(side=tk.LEFT)
        tk.Checkbutton(footer, text="Excel-friendly barcodes (keeps leading zeros; leave off for data analysis)", variable=excel_var,
                       bg="white", fg="#111827", font=(UI_FONT, 9), bd=0, highlightthickness=0).pack(side=tk.LEFT, padx=(14, 0))
        make_button(footer, "Close", win.destroy, "neutral").pack(side=tk.RIGHT)

        set_range(None)
        refresh()
        self.theme.style(win)

    def check_for_updates(self, parent, status_var, button):
        """Manual update check. Asks GitHub for the latest release in the background, then tells
        the user whether they're current or shows what's new."""
        button.config(state=tk.DISABLED)
        status_var.set("Checking GitHub...")
        outcome = queue.Queue()

        def work():
            try:
                outcome.put(("ok", updater.fetch_latest()))
            except updater.UpdateError as e:
                outcome.put(("error", str(e)))
            except Exception as e:  # never let the worker die silently
                outcome.put(("error", f"Unexpected problem: {e}"))

        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                kind, value = outcome.get_nowait()
            except queue.Empty:
                parent.after(100, poll)
                return
            try:
                button.config(state=tk.NORMAL)
                if kind == "error":
                    status_var.set(value)
                    messagebox.showwarning("Check for updates", value, parent=parent)
                elif not updater.is_newer(value.version, app_paths.APP_VERSION):
                    status_var.set(f"You're up to date (version {app_paths.APP_VERSION}).")
                    messagebox.showinfo("Check for updates", f"You're up to date.\n\nInstalled version: {app_paths.APP_VERSION}\nLatest release: {value.version}", parent=parent)
                else:
                    status_var.set(f"Version {value.version} is available.")
                    self.show_update_dialog(parent, value)
            except tk.TclError:
                pass  # the Settings window was closed while checking

        parent.after(100, poll)

    def show_update_dialog(self, parent, release):
        """Shows what's new in a release and offers a one-click install."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Update Available")
        fit_window(dlg, 640, 600, parent)
        dlg.configure(bg="white")
        dlg.transient(parent)

        footer = tk.Frame(dlg, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        body = tk.Frame(dlg, bg="white", padx=24, pady=20)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(body, text=f"Version {release.version} is available", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text=f"You have version {app_paths.APP_VERSION}.", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 12))
        tk.Label(body, text="What's new", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w", pady=(0, 4))

        notes = ThemedScrolledText(body, height=12, wrap=tk.WORD, font=(UI_FONT, 10), relief=tk.SOLID, bd=1)
        notes.pack(fill=tk.BOTH, expand=True)
        notes.insert("1.0", release.notes or "(This release has no notes.)")
        notes.configure(state="disabled")

        blocker = updater.install_blocker(release)
        if blocker:
            tk.Label(body, text=blocker, font=(UI_FONT, 9), bg="white", fg="#92400e", wraplength=580, justify=tk.LEFT).pack(anchor="w", pady=(10, 0))

        progress_var = tk.StringVar()
        tk.Label(body, textvariable=progress_var, font=(UI_FONT, 9), bg="white", fg="#4b5563").pack(anchor="w", pady=(10, 4))
        progress_bar = ttk.Progressbar(body, mode="determinate", maximum=100)

        state = {"cancel": False, "busy": False}

        def install():
            if not messagebox.askyesno(
                "Install update",
                f"Install version {release.version} now?\n\nStock Tracker will close, update itself and reopen "
                "in about half a minute. Your stock data is kept, and a backup is made first.",
                parent=dlg,
            ):
                return
            state["busy"], state["cancel"] = True, False
            install_button.config(state=tk.DISABLED)
            close_button.config(text="Cancel")
            progress_bar.pack(fill=tk.X)
            messages = queue.Queue()
            target = os.path.join(updater.download_folder(), release.installer_name)

            def work():
                try:
                    messages.put(("status", "Backing up your data..."))
                    updater.backup_database(self.db_path)
                    messages.put(("status", "Downloading the update..."))
                    updater.download(
                        release.installer_url, target, release.installer_size,
                        progress=lambda done, total: messages.put(("progress", (done, total))),
                        cancelled=lambda: state["cancel"],
                    )
                    messages.put(("status", "Checking the update's security signature..."))
                    updater.verify(target, release.version, updater.fetch_signature(release))
                    messages.put(("done", target))
                except updater.SignatureError as e:
                    if os.path.exists(target):
                        os.remove(target)
                    messages.put(("security", str(e)))
                except updater.UpdateError as e:
                    messages.put(("error", str(e)))
                except Exception as e:
                    messages.put(("error", f"Unexpected problem: {e}"))

            threading.Thread(target=work, daemon=True).start()

            def reset_ui():
                state["busy"] = False
                install_button.config(state=tk.NORMAL)
                close_button.config(text="Close")
                progress_bar.pack_forget()

            def poll():
                try:
                    while True:
                        kind, value = messages.get_nowait()
                        if kind == "status":
                            progress_var.set(value)
                        elif kind == "progress":
                            done, total = value
                            if total:
                                progress_bar["value"] = 100 * done / total
                                progress_var.set(f"Downloading the update... {done / 1_048_576:.1f} of {total / 1_048_576:.1f} MB")
                        elif kind == "security":
                            reset_ui()
                            progress_var.set("The update was NOT installed.")
                            messagebox.showerror(
                                "Update blocked",
                                f"{value}\n\nThe file was NOT installed and has been deleted. Do not install this update. "
                                "If this keeps happening, contact whoever supplied Stock Tracker.",
                                parent=dlg,
                            )
                            return
                        elif kind == "error":
                            reset_ui()
                            progress_var.set(value)
                            if "cancelled" not in value:
                                messagebox.showwarning("Update", value, parent=dlg)
                            return
                        elif kind == "done":
                            progress_var.set("Installing... Stock Tracker will reopen by itself.")
                            progress_bar["value"] = 100
                            dlg.update_idletasks()
                            self.catalog.log_system_event(
                                f"Updating Stock Tracker from {app_paths.APP_VERSION} to {release.version}.",
                                {"from": app_paths.APP_VERSION, "to": release.version},
                            )
                            try:
                                updater.launch_installer(value, relaunch=True)
                            except OSError as e:
                                reset_ui()
                                messagebox.showerror("Update", f"Couldn't start the installer: {e}", parent=dlg)
                                return
                            self.stop_connection_polling()
                            self.root.after(400, self.root.destroy)   # the installer takes over from here
                            return
                except queue.Empty:
                    pass
                dlg.after(100, poll)

            dlg.after(100, poll)

        def close_or_cancel():
            if state["busy"]:
                state["cancel"] = True
            else:
                dlg.destroy()

        install_button = make_button(footer, "Install update", install, "primary")
        install_button.pack(side=tk.RIGHT)
        if blocker:
            install_button.config(state=tk.DISABLED)
        close_button = make_button(footer, "Close", close_or_cancel, "neutral")
        close_button.pack(side=tk.RIGHT, padx=(0, 10))
        make_button(footer, "Open release page", lambda: webbrowser.open_new(release.page_url), "neutral").pack(side=tk.LEFT)

        self.theme.style(dlg)

    def after_catalog_change(self):
        """Refreshes everything that shows products or stock after the catalog was changed."""
        self.refresh_data(run_sync=False)
        if self.catalog_window is not None and self.catalog_window.winfo_exists():
            self.catalog_refresh()

    def open_add_item_dialog(self):
        """Adds stock by hand, for example an item found on a shelf that is close to expiring.
        It goes through the same product matching as a phone scan, and is logged in History
        as a manual add."""
        if not self.setup_completed:
            return
        if self.add_item_window is not None and self.add_item_window.winfo_exists():
            self.add_item_window.lift()
            return

        dlg = tk.Toplevel(self.root)
        self.add_item_window = dlg
        dlg.title("Add Item")
        fit_window(dlg, 580, 700, self.root)
        dlg.configure(bg="white")
        dlg.transient(self.root)
        dlg.grab_set()

        footer = tk.Frame(dlg, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(dlg, padx=24, pady=20)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        tk.Label(body, text="Add item", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            body,
            text="Record stock by hand, such as an item you found that is close to expiring. It is added just like "
                 "a scan (an existing row for the same product and expiry goes up) and shows in History as a manual add.",
            font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=520, justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 14))

        def section(title):
            frame = tk.Frame(body, bg="#f9fafb", relief=tk.SOLID, bd=1, padx=16, pady=14)
            frame.pack(fill=tk.X, pady=(0, 14))
            tk.Label(frame, text=title, font=(UI_FONT, 10, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w", pady=(0, 6))
            return frame

        def field(parent, label, variable, width=None):
            row = tk.Frame(parent, bg="#f9fafb")
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, width=15, anchor="w", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT)
            entry = tk.Entry(row, textvariable=variable, relief=tk.SOLID, bd=1, font=(UI_FONT, 10), **({"width": width} if width else {}))
            entry.pack(side=tk.LEFT, ipady=4, **({} if width else {"fill": tk.X, "expand": True}))
            return entry

        def note(parent, variable, color="#4b5563"):
            label = tk.Label(parent, textvariable=variable, font=(UI_FONT, 9), bg="#f9fafb", fg=color,
                             wraplength=490, justify=tk.LEFT, anchor="w")
            label.pack(fill=tk.X, pady=(2, 0))
            return label

        code_var, name_var = tk.StringVar(), tk.StringVar()
        expiry_var, qty_var = tk.StringVar(), tk.StringVar(value="1")
        match_var, expiry_note_var, total_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        status_var = tk.StringVar()
        keep_open = tk.BooleanVar(value=False)

        item_box = section("Item")
        code_entry = field(item_box, "Barcode / code", code_var)
        note(item_box, match_var)
        choice_row = tk.Frame(item_box, bg="#f9fafb")
        choice_box = ttk.Combobox(choice_row, state="readonly", width=58)
        choice_box.pack(side=tk.LEFT, pady=(6, 0))
        name_entry = field(item_box, "Product name", name_var)

        stock_box = section("Stock")
        expiry_entry = field(stock_box, "Expiry (month)", expiry_var, width=14)
        note(stock_box, expiry_note_var)
        field(stock_box, "Quantity", qty_var, width=14)
        note(stock_box, total_var, "#1d4ed8")

        note(body, status_var, "#047857").configure(bg="white", font=(UI_FONT, 9, "bold"))

        state = {"choices": [], "locked": False, "typed_name": ""}

        def chosen_key():
            if len(state["choices"]) > 1 and choice_box.current() >= 0:
                return state["choices"][choice_box.current()]["sys_key"]
            if len(state["choices"]) == 1:
                return state["choices"][0]["sys_key"]
            return None

        def set_name_lock(locked, text=""):
            # While the catalog supplies the name, what the person typed is set aside and put back if they
            # change the code to something the catalog doesn't know.
            if locked and not state["locked"]:
                state["typed_name"] = name_var.get()
            if locked:
                name_var.set(text)
                name_entry.configure(state="disabled")
            elif state["locked"]:
                name_var.set(state["typed_name"])
                name_entry.configure(state="normal")
            state["locked"] = locked

        def describe(product):
            ids = ", ".join(part for part in (
                f"code {product['item_code']}" if product["item_code"] else "",
                f"alias {product['alias']}" if product["alias"] else "") if part)
            return f"{product['description'] or PLACEHOLDER_LABEL}  [{ids or 'no codes'}]"

        def refresh_match(*_args):
            code = code_var.get().strip()
            candidates = self.catalog.candidates_for(code) if code else []
            same = [p["sys_key"] for p in candidates] == [p["sys_key"] for p in state["choices"]]
            if not same:
                candidates.sort(key=lambda p: (p["description"] or "￿").lower())
                state["choices"] = candidates
                choice_box.configure(values=[describe(p) for p in candidates])
                choice_box.set("")
            candidates = state["choices"]

            if len(candidates) > 1:
                choice_row.pack(fill=tk.X, before=name_entry.master)
                match_var.set(f"This code matches {len(candidates)} products. Choose which one it is:")
                product = candidates[choice_box.current()] if choice_box.current() >= 0 else None
                set_name_lock(True, (product["description"] or "") if product else "")
            else:
                choice_row.pack_forget()
                if not code:
                    match_var.set("Type or paste the barcode or item code.")
                    set_name_lock(False)
                elif not candidates:
                    match_var.set("Not in the catalog. It will be added as a new product: enter its name below, or leave "
                                  "the name blank to add it as an unnamed product.")
                    set_name_lock(False)
                elif candidates[0]["is_placeholder"]:
                    match_var.set("Known only as an unnamed product. Enter a name below to fill it in.")
                    set_name_lock(False)
                else:
                    match_var.set("✔ In the catalog. The product's name is used.")
                    set_name_lock(True, candidates[0]["description"])
            refresh_stock()

        def refresh_stock(*_args):
            expiry_text = expiry_var.get().strip()
            expiry = None
            if not expiry_text:
                expiry_note_var.set("Written as 2027-03 or 03/2027.")
            else:
                try:
                    expiry = expiry_from_text(expiry_text)
                    status = TREE_STATUS[expiry_report.status_for(expiry, self.expiry_warning_days)][0]
                    expiry_note_var.set(f"{expiry}: shows as {status}.")
                except ValueError as e:
                    expiry_note_var.set(str(e))

            try:
                quantity = int(qty_var.get().strip())
            except ValueError:
                quantity = None
            key = chosen_key()
            if expiry and quantity and quantity > 0 and key is not None:
                before = self.catalog.stock_quantity(key, expiry)
                total_var.set(f"Stock for this expiry: {before:,} now, {before + quantity:,} after adding." if before
                              else f"Starts a new stock row of {quantity:,}.")
            elif expiry and quantity and quantity > 0 and code_var.get().strip() and not state["choices"]:
                total_var.set(f"Starts a new stock row of {quantity:,}.")
            else:
                total_var.set("")

        def add(_event=None):
            try:
                result = self.catalog.add_stock_item(
                    code_var.get(), qty_var.get(), expiry_var.get(),
                    description="" if state["locked"] else name_var.get(), sys_key=chosen_key(),
                )
            except ValueError as e:
                messagebox.showwarning("Can't add this item", str(e), parent=dlg)
                return

            self.refresh_data(run_sync=False)
            row_id = self.catalog.stock_row_id(result["sys_key"], result["expiry"])
            for item in self.tree.get_children():
                if int(self.tree.item(item, "values")[0]) == row_id:
                    self.tree.selection_set(item)
                    self.tree.see(item)
                    break
            if (result["created_product"] or result["named_product"]) \
                    and self.catalog_window is not None and self.catalog_window.winfo_exists():
                self.catalog_refresh()

            summary = (f"Added {result['added']:,} × {result['product']}, expiring {result['expiry']}"
                       + (f" (now {result['after']:,} for that expiry)." if result["before"] else "."))
            if not keep_open.get():
                dlg.destroy()
                return
            status_var.set("✔ " + summary)
            for variable in (code_var, name_var):
                variable.set("")
            qty_var.set("1")
            set_name_lock(False)
            code_entry.focus_set()

        make_button(footer, "Add to stock", add, "primary").pack(side=tk.RIGHT)
        make_button(footer, "Close", dlg.destroy, "neutral").pack(side=tk.RIGHT, padx=(0, 10))
        tk.Checkbutton(footer, text="Keep this window open to add another", variable=keep_open, bg="white",
                       fg="#111827", font=(UI_FONT, 9), bd=0, highlightthickness=0).pack(side=tk.LEFT)

        code_var.trace_add("write", refresh_match)
        choice_box.bind("<<ComboboxSelected>>", refresh_match)
        expiry_var.trace_add("write", refresh_stock)
        qty_var.trace_add("write", refresh_stock)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.bind("<Return>", add)

        refresh_match()
        self.theme.style(dlg)
        code_entry.focus_set()

    def show_row_menu(self, event):
        """Right-click menu for the stock table."""
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        if row_id not in self.tree.selection():
            self.tree.selection_set(row_id)

        count = len(self.tree.selection())
        # Editing needs exactly one row; removing works on any number.
        self.row_menu.entryconfigure(0, state=tk.NORMAL if count == 1 else tk.DISABLED)
        self.row_menu.entryconfigure(2, label="🗑  Remove selected" if count == 1 else f"🗑  Remove selected ({count})")
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def edit_selected(self):
        selection = self.tree.selection()
        if len(selection) != 1:
            return
        self.open_edit_dialog(int(self.tree.item(selection[0], "values")[0]))

    def open_edit_dialog(self, inventory_id):
        """Edits one stock row. Name, alias and item code belong to the product and
        change everywhere; expiry and quantity change only this row. The changes are
        validated, shown for confirmation, and only then applied."""
        info = self.catalog.get_stock_row(inventory_id)
        if info is None:
            messagebox.showwarning("Edit information", "That item no longer exists. The table will be refreshed.")
            self.refresh_data(run_sync=False)
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Information")
        fit_window(dlg, 560, 640, self.root)
        dlg.configure(bg="white")
        dlg.transient(self.root)
        dlg.grab_set()

        footer = tk.Frame(dlg, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(dlg, padx=24, pady=20)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        tk.Label(body, text="Edit information", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text=info["description"] or PLACEHOLDER_LABEL, font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 14))

        variables = {}

        def section(title, note):
            frame = tk.Frame(body, bg="#f9fafb", relief=tk.SOLID, bd=1, padx=16, pady=14)
            frame.pack(fill=tk.X, pady=(0, 14))
            tk.Label(frame, text=title, font=(UI_FONT, 10, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w")
            tk.Label(frame, text=note, font=(UI_FONT, 9), bg="#f9fafb", fg="#6b7280", wraplength=460, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
            return frame

        def field(parent, key, label, value):
            row = tk.Frame(parent, bg="#f9fafb")
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, width=13, anchor="w", font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#374151").pack(side=tk.LEFT)
            variables[key] = tk.StringVar(value=str(value))
            entry = tk.Entry(row, textvariable=variables[key], relief=tk.SOLID, bd=1, font=(UI_FONT, 10))
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
            return entry

        stock_rows = info["stock_rows"]
        plural = "s" if stock_rows != 1 else ""
        product_box = section("Product", f"Changes apply to this product everywhere ({stock_rows} stock row{plural}).")
        first_entry = field(product_box, "description", "Name", info["description"])
        field(product_box, "alias", "Alias / barcode", info["alias"])
        field(product_box, "item_code", "Item code", info["item_code"])

        row_box = section("This stock row", "Changes apply to this row only. Expiry is written YYYY-MM, for example 2027-03.")
        field(row_box, "expiry", "Expiry", info["expiry"])
        field(row_box, "quantity", "Quantity", info["quantity"])

        labels = (("description", "Name"), ("alias", "Alias"), ("item_code", "Item code"),
                  ("expiry", "Expiry"), ("quantity", "Quantity"))
        original = {"description": info["description"], "alias": info["alias"], "item_code": info["item_code"],
                    "expiry": info["expiry"], "quantity": str(info["quantity"])}

        def save(_event=None):
            new = {key: var.get().strip() for key, var in variables.items()}
            changes = [(key, label, original[key], new[key]) for key, label in labels if new[key] != original[key]]
            if not changes:
                dlg.destroy()
                return

            arguments = (inventory_id, new["description"], new["item_code"], new["alias"], new["expiry"], new["quantity"])
            try:
                self.catalog.edit_stock_row(*arguments, dry_run=True)  # same checks as the real edit, nothing saved
            except ValueError as e:
                messagebox.showwarning("Can't apply this edit", str(e), parent=dlg)
                return

            def describe(keys, heading):
                picked = [c for c in changes if c[0] in keys]
                if not picked:
                    return []
                return [heading] + [
                    f"   {label}: {old or '(blank)'}  ->  {new_value or '(blank)'}"
                    for _key, label, old, new_value in picked
                ]

            lines = describe(("description", "alias", "item_code"), f"Product (everywhere, {stock_rows} stock row{plural}):")
            row_lines = describe(("expiry", "quantity"), "This stock row only:")
            if lines and row_lines:
                lines.append("")
            lines += row_lines

            if any(c[0] in ("alias", "item_code") for c in changes):
                others = self.catalog.identifier_sharing(info["sys_key"], new["item_code"], new["alias"])
                if others:
                    names = ", ".join(p["description"] or PLACEHOLDER_LABEL for p in others[:3])
                    lines += ["", f"Note: this item code/alias is also used by {names}. "
                                  "Scanning it will ask you to choose between the products."]

            if not messagebox.askyesno("Confirm changes", "\n".join(lines) + "\n\nApply these changes?", parent=dlg):
                return
            try:
                self.catalog.edit_stock_row(*arguments)
            except ValueError as e:
                messagebox.showwarning("Can't apply this edit", str(e), parent=dlg)
                return

            dlg.destroy()
            self.refresh_data(run_sync=False)
            for item in self.tree.get_children():
                if int(self.tree.item(item, "values")[0]) == inventory_id:
                    self.tree.selection_set(item)
                    self.tree.see(item)
                    break
            if self.catalog_window is not None and self.catalog_window.winfo_exists():
                self.catalog_refresh()

        make_button(footer, "Save changes", save, "primary").pack(side=tk.RIGHT)
        make_button(footer, "Cancel", dlg.destroy, "neutral").pack(side=tk.RIGHT, padx=(0, 10))
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.bind("<Return>", save)

        self.theme.style(dlg)
        first_entry.focus_set()

    def backup_before_catalog_change(self, parent, what):
        """Saves a copy of the database first. Returns its path, or None (after telling the user,
        and with nothing changed) if the copy couldn't be made."""
        try:
            return updater.backup_database(self.db_path, reason="before-catalog-change")
        except Exception as e:
            messagebox.showerror(
                what, f"A safety backup of the database couldn't be saved, so nothing was changed.\n\n{e}", parent=parent)
            return None

    def reset_catalog_flow(self, parent):
        """Settings > Reset catalog: clears every product but keeps the stock on hand."""
        try:
            preview = self.catalog.reset_catalog(dry_run=True)
        except Exception as e:
            messagebox.showerror("Reset catalog", f"Could not prepare the reset: {e}", parent=parent)
            return
        if not preview["catalog_entries"]:
            messagebox.showinfo("Reset catalog", "The catalog is already empty.", parent=parent)
            return

        if not messagebox.askyesno(
            "Reset product catalog",
            f"This clears the product catalog: {preview['catalog_entries']:,} products with their names, item codes and aliases.\n\n"
            f"Stock is NOT deleted. The {preview['stock_rows']:,} stock rows ({preview['unnamed_kept']:,} products) stay, "
            f"shown as \"{PLACEHOLDER_LABEL}\" under their barcode until you import a catalog again, which matches them back up.\n\n"
            "The stock history is kept. A backup copy of the database is saved first, and you can undo the reset from the "
            "Product Catalog window until stock next changes.\n\nContinue?",
            icon="warning", parent=parent,
        ):
            return
        typed = simpledialog.askstring("Reset product catalog", "Type RESET (in capitals) to confirm.", parent=parent)
        if typed is None or typed.strip() != "RESET":
            if typed is not None:
                messagebox.showinfo("Reset product catalog", "That wasn't RESET, so nothing was changed.", parent=parent)
            return

        backup = self.backup_before_catalog_change(parent, "Reset product catalog")
        if backup is None:
            return
        try:
            self.catalog.reset_catalog()
        except Exception as e:
            messagebox.showerror("Reset product catalog", f"The reset failed and nothing was changed: {e}", parent=parent)
            return
        self.after_catalog_change()
        messagebox.showinfo(
            "Catalog reset",
            f"The catalog was cleared. {preview['stock_rows']:,} stock rows were kept.\n\n"
            "Import a product list (Products > Import CSV) to name them again, or use Undo in the Product Catalog window.\n\n"
            f"Backup saved in: {os.path.dirname(backup)}",
            parent=parent,
        )

    def replace_catalog_flow(self, parent):
        """Settings > Replace from file: the chosen file becomes the whole catalog."""
        path = filedialog.askopenfilename(
            parent=parent,
            title="Choose the new product list",
            filetypes=[("CSV / text files", "*.csv *.tsv *.txt"), ("All files", "*.*")],
        )
        if path:
            self.open_import_dialog(path, self.after_catalog_change, replace=True)

    def open_settings_window(self):
        """Preferences that can be changed after first-run setup."""
        win = tk.Toplevel(self.root)
        win.title("Settings")
        fit_window(win, 480, 780, self.root)
        win.configure(bg="white")
        win.transient(self.root)

        footer = tk.Frame(win, bg="white", padx=24, pady=14, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(win, padx=24, pady=20)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        tk.Label(body, text="Settings", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text="Expiring-soon warning", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w", pady=(16, 0))
        tk.Label(body, text="Stock expiring within this window is highlighted yellow in the table.", font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=400, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
        combo = ttk.Combobox(body, state="readonly", width=14, values=self.warning_day_choices())
        combo.set(f"{self.expiry_warning_days} days")
        combo.pack(anchor="w")

        tk.Label(body, text="Product catalog", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w", pady=(20, 0))
        tk.Label(body, text="Start over with a new product list. Stock on hand is always kept and matched back up by "
                            "barcode. A backup is saved first, and it can be undone until stock next changes.",
                 font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=420, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
        catalog_buttons = tk.Frame(body, bg="white")
        catalog_buttons.pack(anchor="w")
        make_button(catalog_buttons, "Replace from file...", lambda: self.replace_catalog_flow(win), "neutral").pack(side=tk.LEFT)
        make_button(catalog_buttons, "Reset catalog...", lambda: self.reset_catalog_flow(win), "danger").pack(side=tk.LEFT, padx=(10, 0))

        tk.Label(body, text="Firebase credentials", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w", pady=(20, 0))
        key_status = tk.StringVar(value=self.credentials_summary())
        tk.Label(body, textvariable=key_status, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=420, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
        make_button(body, "Replace key file...", lambda: self.import_credentials_flow(win, lambda: key_status.set(self.credentials_summary())), "neutral").pack(anchor="w")

        def save():
            self.set_warning_days(int(combo.get().split()[0]))
            win.destroy()

        tk.Label(body, text="Updates", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w", pady=(20, 0))
        update_status = tk.StringVar(value=f"Installed version: {app_paths.APP_VERSION}")
        tk.Label(body, textvariable=update_status, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=420, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))
        update_button = make_button(body, "Check for updates", lambda: self.check_for_updates(win, update_status, update_button), "neutral")
        update_button.pack(anchor="w")

        make_button(footer, "Save", save, "primary").pack(side=tk.RIGHT)
        make_button(footer, "Cancel", win.destroy, "neutral").pack(side=tk.RIGHT, padx=(0, 10))
        self.theme.style(win)

    def show_mobile_setup(self):
        """Shows the QR codes that connect a phone: one that opens the hosted app, and one that
        pairs it with this PC's Firestore connection."""
        web_config = self.get_saved_firebase_web_config()

        # If no config exists yet, prompt for it
        if not web_config:
            raw_text = simpledialog.askstring("Mobile Setup", "Paste your Firebase web config (JSON):")
            if not raw_text:
                return  # User cancelled

            is_valid, web_config, message = validate_firebase_web_config(raw_text)
            if not is_valid:
                messagebox.showwarning("Invalid Config", message)
                return

            self.save_firebase_web_config(web_config)

        pairing_payload = self.build_pairing_payload(web_config)

        qr_window = tk.Toplevel(self.root)
        qr_window.title("Connect Mobile Device")
        fit_window(qr_window, 720, 780, self.root, min_width=520)
        qr_window.configure(bg="white")
        qr_window.resizable(True, True)

        # Keep window on top
        qr_window.attributes('-topmost', True)

        # The footer is pinned to the bottom edge; on a short screen only the QR area above it scrolls
        footer = tk.Frame(qr_window, bg="white", padx=24, pady=12, bd=1, relief=tk.GROOVE)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        scroller = ScrollFrame(qr_window, padx=24, pady=20)
        scroller.pack(fill=tk.BOTH, expand=True)
        body = scroller.body

        tk.Label(body, text="Connect Mobile", font=(UI_FONT, 16, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            body,
            text="Two quick scans connect a phone: first open the app, then pair it with this PC.",
            font=(UI_FONT, 10), bg="white", fg="#4b5563",
        ).pack(anchor="w", pady=(2, 14))

        # -- where the phone page is hosted (needed for the "open the app" QR) --
        address_frame = tk.Frame(body, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=14)
        address_frame.pack(fill=tk.X)
        tk.Label(address_frame, text="Phone page address", font=(UI_FONT, 10, "bold"), bg="#f9fafb", fg="#111827").pack(anchor="w")
        tk.Label(
            address_frame,
            text="Where you hosted the phone page, for example https://yourname.github.io/StockTrackerSystem/",
            font=(UI_FONT, 9), bg="#f9fafb", fg="#6b7280", wraplength=620, justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 8))

        address_row = tk.Frame(address_frame, bg="#f9fafb")
        address_row.pack(fill=tk.X)
        url_var = tk.StringVar(value=self.get_mobile_app_url())
        url_status = tk.StringVar()

        # -- the two QR cards --
        cards = tk.Frame(body, bg="white")
        cards.pack(fill=tk.BOTH, expand=True, pady=(16, 0))
        cards.columnconfigure(0, weight=1, uniform="card")
        cards.columnconfigure(1, weight=1, uniform="card")

        def make_card(column, number, title, caption):
            card = tk.Frame(cards, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=14)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))

            header = tk.Frame(card, bg="#f9fafb")
            header.pack(anchor="w")
            tk.Label(header, text=str(number), width=2, bg="#3b82f6", fg="white", font=(UI_FONT, 10, "bold")).pack(side=tk.LEFT, padx=(0, 10))
            tk.Label(header, text=title, font=(UI_FONT, 11, "bold"), bg="#f9fafb", fg="#111827").pack(side=tk.LEFT)
            tk.Label(card, text=caption, font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563", wraplength=280, justify=tk.LEFT).pack(anchor="w", pady=(8, 10))

            holder = tk.Frame(card, bg="white", bd=1, relief=tk.SOLID)
            holder.pack()
            holder._keep_subtree = True  # QR codes must stay dark-on-white to scan
            label = tk.Label(holder, bg="white")
            label.pack(padx=8, pady=8)
            return card, label

        app_card, app_qr_label = make_card(
            0, 1, "Open the app",
            "Scan this with your phone's normal camera. It opens the scanner page in your browser.",
        )
        pair_card, pair_qr_label = make_card(
            1, 2, "Pair with this PC",
            "In the app's \"Scan Desktop QR to Connect\" screen, scan this code.",
        )

        def show_note(label, text):
            label.configure(image="", text=text, width=30, height=10, font=(UI_FONT, 9), fg="#4b5563", wraplength=230, justify=tk.CENTER)
            label.image = None

        def render_qr(label, data, size):
            if not QR_AVAILABLE:
                show_note(label, "Install qrcode and pillow to show QR codes.")
                return
            qrcode = importlib.import_module("qrcode")
            PILImage = importlib.import_module("PIL.Image")
            ImageTk = importlib.import_module("PIL.ImageTk")

            qr_code = qrcode.QRCode(box_size=1, border=2)
            qr_code.add_data(data)
            qr_code.make(fit=True)
            # Whole pixels per square (no fractional scaling), so every module is crisp and equal.
            qr_code.box_size = max(2, size // (qr_code.modules_count + 4))
            qr_image = qr_code.make_image(fill_color="black", back_color="white").convert("RGB")
            tk_image = ImageTk.PhotoImage(qr_image)
            label.configure(image=tk_image, text="", width=0, height=0)
            label.image = tk_image

        def render_app_qr():
            saved = self.get_mobile_app_url()
            if saved:
                render_qr(app_qr_label, saved, 240)
            else:
                show_note(app_qr_label, "Enter the phone page address above and click Save to show its QR code.")

        def save_url(_event=None):
            ok, result = normalize_mobile_url(url_var.get())
            if not ok:
                url_status.set(result)
                return
            self.save_setting("mobile_app_url", result)
            url_var.set(result)
            url_status.set("Saved.")
            render_app_qr()

        make_button(address_row, "Save", save_url, "primary", padx=16, pady=5).pack(side=tk.RIGHT, padx=(8, 0))
        url_entry = tk.Entry(address_row, textvariable=url_var, relief=tk.SOLID, bd=1, font=(UI_FONT, 10))
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        url_entry.bind("<Return>", save_url)
        tk.Label(address_frame, textvariable=url_status, font=(UI_FONT, 9), bg="#f9fafb", fg="#991b1b", wraplength=620, justify=tk.LEFT).pack(anchor="w", pady=(6, 0))

        def copy_text(text, done_message):
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            messagebox.showinfo("Copied", done_message, parent=qr_window)

        def copy_address():
            saved = self.get_mobile_app_url()
            if not saved:
                messagebox.showwarning("Phone page address", "Enter and save the address first.", parent=qr_window)
                return
            copy_text(saved, "Address copied to the clipboard.")

        make_button(app_card, "Copy address", copy_address, "neutral", padx=12, pady=5).pack(pady=(12, 0))
        make_button(pair_card, "Copy pairing details", lambda: copy_text(pairing_payload, "Pairing details copied to clipboard."), "neutral", padx=12, pady=5).pack(pady=(12, 0))

        render_app_qr()
        render_qr(pair_qr_label, pairing_payload, 280)

        # Option to reset/change the web config
        def reset_config():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM settings WHERE key='firebase_web_config'")
            conn.commit()
            conn.close()
            qr_window.destroy()
            self.show_mobile_setup()

        make_button(footer, "Change Firebase Web Config", reset_config, "neutral", padx=12, pady=5).pack(side=tk.LEFT)
        tk.Label(
            footer,
            text="The phone remembers its pairing, so you only need this once per phone.",
            font=(UI_FONT, 9), bg="white", fg="#6b7280",
        ).pack(side=tk.LEFT, padx=(14, 0))

        self.theme.style(qr_window)


def log_exception(exc_type, exc, tb):
    """Appends an unexpected error to error.log (a windowed .exe has no console to show it)."""
    import datetime
    import traceback
    try:
        with open(app_paths.log_path(), "a", encoding="utf-8") as log:
            log.write(f"\n[{datetime.datetime.now().isoformat(timespec='seconds')}] v{app_paths.APP_VERSION}\n")
            log.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except OSError:
        pass


def run_selftest(report_path):
    """Checks that a packaged build contains everything the app imports at runtime.
    Used by the build script; writes the result to `report_path`. Returns an exit code."""
    problems = []

    def step(name, check):
        try:
            check()
        except Exception as e:  # any failure means the build is incomplete
            problems.append(f"{name}: {e!r}")

    def gui():
        window = tk.Tk()
        window.destroy()

    def qr_libs():
        import qrcode
        from PIL import Image, ImageTk
        qrcode.QRCode().add_data("x")

    def dpapi():
        assert secure_store.unprotect(secure_store.protect(b"probe")) == b"probe"

    def catalog():
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "t.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, "
                         "expiry_date TEXT NOT NULL, quantity INTEGER NOT NULL, last_updated TIMESTAMP)")
            conn.commit()
            conn.close()
            catalog = ProductCatalog(db)
            assert catalog.stats()["total"] == 0
            catalog.add_stock_item("PROBE", 1, "2030-01", "Probe")
            assert catalog.reset_catalog()["unnamed_kept"] == 1
            assert catalog.stock_quantity(catalog.resolve("PROBE")[0], "2030-01") == 1

    def firebase():
        import grpc  # noqa: F401
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        info = {"type": "service_account", "project_id": "selftest", "client_email": "t@selftest.iam.gserviceaccount.com",
                "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"}
        app = firebase_admin.initialize_app(credentials.Certificate(info), name="selftest")
        try:
            admin_firestore.client(app)
        finally:
            firebase_admin.delete_app(app)

    def pdf():
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "probe.pdf")
            sample = [{"id": 1, "product": "Probe", "alias": "1", "item_code": "", "expiry": "2030-01",
                       "quantity": 1, "status": "good", "month": "2030-01"}]
            expiry_report.write_pdf(target, expiry_report.build_report(sample, group_by="status"),
                                    {"title": "Probe", "generated": "now", "lines": [], "footer": "probe"})
            with open(target, "rb") as f:
                assert f.read(4) == b"%PDF"

    def history_and_updates():
        import base64
        import tempfile
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "t.db")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT NOT NULL, "
                         "expiry_date TEXT NOT NULL, quantity INTEGER NOT NULL, last_updated TIMESTAMP)")
            conn.commit()
            conn.close()
            ProductCatalog(db)
            assert stock_history.count(db) == 0
            # signature check used by the updater, end to end
            key = Ed25519PrivateKey.generate()
            public = base64.b64encode(key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
            installer = os.path.join(folder, "probe.exe")
            with open(installer, "wb") as f:
                f.write(b"probe")
            signature = base64.b64encode(key.sign(
                updater.signed_message("9.9.9", updater.sha256_file(installer)))).decode()
            updater.verify(installer, "9.9.9", signature, public)
            assert updater.is_newer("1.10.0", "1.9.0")
            assert app_paths.UPDATE_PUBLIC_KEY and len(base64.b64decode(app_paths.UPDATE_PUBLIC_KEY)) == 32

    step("tkinter window", gui)
    step("stock history + update signature check", history_and_updates)
    step("PDF export (reportlab)", pdf)
    step("qrcode + Pillow", qr_libs)
    step("Windows key protection", dpapi)
    step("product catalog + sqlite", catalog)
    step("Firebase Admin SDK + Firestore client", firebase)

    with open(report_path, "w", encoding="utf-8") as report:
        report.write("OK\n" if not problems else "FAILED\n" + "\n".join(problems) + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest(sys.argv[sys.argv.index("--selftest") + 1]))

    sys.excepthook = log_exception
    root = tk.Tk()
    try:
        root.iconbitmap(default=app_paths.resource_path("app.ico"))
    except tk.TclError:
        pass  # missing icon file: keep the default one

    def report_callback_exception(exc_type, exc, tb):
        log_exception(exc_type, exc, tb)
        messagebox.showerror(
            "Something went wrong",
            f"{exc}\n\nDetails were saved to:\n{app_paths.log_path()}",
        )

    root.report_callback_exception = report_callback_exception

    try:
        app = StockTrackerApp(root)
        root.mainloop()
    except DatabaseTooNewError as e:
        messagebox.showerror("Update Stock Tracker", str(e))
        root.destroy()
    except Exception:
        log_exception(*sys.exc_info())
        messagebox.showerror("Stock Tracker could not start", f"Details were saved to:\n{app_paths.log_path()}")
        raise
