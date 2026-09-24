import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from tkinter import filedialog
import sqlite3
import os
import re
import sys
from datetime import datetime, timezone
import webbrowser
import importlib.util
import json
import random
import string

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as admin_firestore

from product_catalog import (
    ProductCatalog, PLACEHOLDER_LABEL, MAPPING_FIELDS, read_table, guess_mapping,
    validate_mapping, parse_rows, format_import_summary,
)
from ui_theme import ThemeManager, ThemedScrolledText, UI_FONT, make_button, system_prefers_dark

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


def validate_service_account_file(path):
    """
    Validates that a Firebase/Google Cloud service account key exists at
    `path` and looks usable.

    Returns a (is_valid: bool, message: str) tuple, where `message` is the
    resolved project_id on success or a human-readable error on failure.
    """
    if not os.path.exists(path):
        return False, (
            "config/service_account.json was not found. In the Firebase "
            "Console, go to Project Settings > Service Accounts > Generate "
            "new private key, and save the downloaded file at that path."
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"config/service_account.json is not valid JSON: {e}"

    missing = [key for key in REQUIRED_SERVICE_ACCOUNT_KEYS if not data.get(key)]
    if data.get("type") != "service_account" or missing:
        return False, (
            "config/service_account.json doesn't look like a Firebase service "
            "account key. Re-download it from Firebase Console > Project "
            "Settings > Service Accounts > Generate new private key."
        )

    return True, data.get("project_id")

class StockTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Stock Tracker - Manager Dashboard")
        self.root.geometry("1040x660")
        self.root.configure(bg="#f3f4f6") # Light gray background (themed below)
        
        # Define database path (going up one level from src/ to root, then into database/)
        # For simplicity when running as an exe, we check the current directory or a database folder
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.db_path = os.path.join(base_dir, 'database', 'store_inventory.db')
        
        # Fallback to local directory if the structure isn't perfect (e.g., when run as .exe)
        if not os.path.exists(os.path.join(base_dir, 'database')):
            self.db_path = 'store_inventory.db'

        self.connection_status_var = tk.StringVar(value="◌ Checking mobile...")
        self.connection_poll_job = None
        self.db = None
        self.status_pill = None
        self.theme_button = None

        # init_db() must run first — it creates the settings table that
        # load_or_create_connection_token() reads from and writes to.
        self.init_db()
        saved_theme = self.get_setting("ui_theme")
        self.theme = ThemeManager(self.root, saved_theme or ("dark" if system_prefers_dark() else "light"))
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

    def toggle_theme(self):
        """Switches between light and dark mode and remembers the choice."""
        mode = self.theme.toggle()
        self.save_setting("ui_theme", mode)
        self.update_theme_button()

    def update_theme_button(self):
        if self.theme_button is not None:
            self.theme_button.config(text="☀  Light mode" if self.theme.mode == "dark" else "☾  Dark mode")

    def get_service_account_path(self):
        """Fixed on-disk location of the Firebase Admin SDK service account key."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, 'config', 'service_account.json')

    def has_firebase_setup(self):
        """Checks whether both halves of the Firebase setup are in place: a
        saved web config (embedded in the mobile pairing QR) and a valid
        service account key on disk (for this app's own Admin SDK access)."""
        is_valid, _ = validate_service_account_file(self.get_service_account_path())
        return is_valid and bool(self.get_saved_firebase_web_config())

    def connect_firestore(self):
        """Initializes the Firebase Admin SDK connection from the service
        account key on disk. Safe to call more than once — firebase_admin
        only allows one default app per process."""
        is_valid, message = validate_service_account_file(self.get_service_account_path())
        if not is_valid:
            self.db = None
            return False, message

        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(self.get_service_account_path())
                firebase_admin.initialize_app(cred)
            self.db = admin_firestore.client()
            return True, "Connected."
        except Exception as e:
            self.db = None
            return False, f"Could not initialize the Firebase Admin SDK: {e}"

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
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM inventory")
            cursor.execute("DELETE FROM settings")
            conn.commit()
            conn.close()

            if hasattr(self, "tree") and self.tree is not None:
                for item in self.tree.get_children():
                    self.tree.delete(item)

            self.setup_completed = False
            messagebox.showinfo("Reset Complete", "The app has been reset to its default state. Restarting setup now.")
            self.root.destroy()
            os.execl(sys.executable, sys.executable, *sys.argv)
        except Exception as e:
            messagebox.showerror("Reset Failed", f"Could not reset the app: {e}")

    def open_developer_tools(self):
        """Shows developer-only maintenance tools for the app."""
        dev_window = tk.Toplevel(self.root)
        dev_window.title("Developer Tools")
        dev_window.geometry("420x240")
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
                "This will clear inventory data, the saved Firebase web config, and all local setup. Continue?"
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
        # Cap the height to the screen so the window never runs off the bottom.
        window_height = min(900, setup_window.winfo_screenheight() - 100)
        setup_window.geometry(f"640x{window_height}")
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
                "2. A service account key saved as config/service_account.json.\n"
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
        add_step(guide_frame, 3, "Download a service account key", "Project Settings > Service Accounts > Generate new private key. Save the downloaded file as config/service_account.json next to this app.")
        add_step(guide_frame, 4, "Register a web app", "Project Settings > General > Your apps > Add app > Web. Copy the shown firebaseConfig object and paste it below.")

        sa_frame = tk.Frame(container, bg="white")
        sa_frame.pack(fill=tk.X, pady=(18, 0))

        tk.Label(sa_frame, text="Service Account Key", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")
        sa_status_text = tk.StringVar(value="Not checked yet.")
        tk.Label(sa_frame, textvariable=sa_status_text, font=(UI_FONT, 9), bg="white", fg="#6b7280", wraplength=480, justify=tk.LEFT).pack(anchor="w", pady=(2, 8))

        def check_service_account():
            is_valid, message = validate_service_account_file(self.get_service_account_path())
            sa_status_text.set(f"Found. Project ID: {message}" if is_valid else message)

        tk.Button(sa_frame, text="Check service_account.json", command=check_service_account, bg="#10b981", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(anchor="w")
        check_service_account()

        form_frame = tk.Frame(container, bg="white")
        form_frame.pack(fill=tk.X, pady=(18, 0))

        tk.Label(form_frame, text="Firebase Web Config (JSON)", font=(UI_FONT, 10, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(form_frame, text="Paste the firebaseConfig object from Project Settings > General > Your apps.", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 8))

        config_entry = ThemedScrolledText(form_frame, height=8, wrap=tk.WORD, font=("Consolas", 9), bg="white", fg="#111827", relief=tk.SOLID, bd=1)
        config_entry.pack(fill=tk.X)

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
            sa_valid, sa_message = validate_service_account_file(self.get_service_account_path())
            if not sa_valid:
                tip_text.set(sa_message)
                messagebox.showwarning("Service Account Missing", sa_message)
                return

            is_valid, config, message = validate_firebase_web_config(config_entry.get("1.0", tk.END))
            tip_text.set(message)
            if not is_valid:
                messagebox.showwarning("Invalid Config", message)
                return

            self.save_firebase_web_config(config)
            connected, connect_message = self.connect_firestore()
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
            sa_valid, sa_message = validate_service_account_file(self.get_service_account_path())
            if not sa_valid:
                tip_text.set(sa_message)
                messagebox.showwarning("Service Account Missing", sa_message)
                return

            is_valid, config, message = validate_firebase_web_config(config_entry.get("1.0", tk.END))
            tip_text.set(message)
            if not is_valid:
                messagebox.showwarning("Invalid Config", message)
                return

            connected, connect_message = self.connect_firestore()
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
        self.root.minsize(920, 560)

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

        # Toolbar: catalog actions on the left, inventory actions on the right
        toolbar = tk.Frame(self.root, bg="#f3f4f6", padx=28)
        toolbar.pack(fill=tk.X, side=tk.TOP, pady=(18, 0))

        make_button(toolbar, "📦  Products", self.open_catalog_window, "primary").pack(side=tk.LEFT, padx=(0, 10))
        self.pending_button = make_button(toolbar, "⚠  Pending scans (0)", self.open_pending_window, "muted")
        self.pending_button.pack(side=tk.LEFT)

        make_button(toolbar, "🛠  Dev Tools", self.open_developer_tools, "neutral").pack(side=tk.RIGHT, padx=(10, 0))
        make_button(toolbar, "📱  Connect Mobile", self.show_mobile_setup, "neutral").pack(side=tk.RIGHT, padx=(10, 0))
        make_button(toolbar, "🗑  Remove Selected", self.delete_selected, "danger").pack(side=tk.RIGHT, padx=(10, 0))
        make_button(toolbar, "↻  Refresh", self.refresh_data, "success").pack(side=tk.RIGHT)

        # Legend goes in before the table so the table can't squeeze it out
        legend_frame = tk.Frame(self.root, bg="#f3f4f6", padx=28, pady=12)
        legend_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(legend_frame, text="Legend", font=(UI_FONT, 9, "bold"), bg="#f3f4f6", fg="#6b7280").pack(side=tk.LEFT, padx=(0, 14))
        tk.Label(legend_frame, text="■  Expired", fg="#ef4444", font=(UI_FONT, 10, "bold"), bg="#f3f4f6").pack(side=tk.LEFT, padx=(0, 16))
        tk.Label(legend_frame, text="■  Expiring within 60 days", fg="#eab308", font=(UI_FONT, 10, "bold"), bg="#f3f4f6").pack(side=tk.LEFT)

        # Inventory table, in a bordered card
        card = tk.Frame(self.root, bg="#ffffff", relief=tk.SOLID, bd=1)
        card.pack(fill=tk.BOTH, expand=True, padx=28, pady=(18, 0))

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

                # Default status
                status = "Good"
                tag = "good"
                
                try:
                    # Parse YYYY-MM (e.g. 2026-12) format from the mobile app
                    expiry_date = datetime.strptime(expiry_str, "%Y-%m")
                    
                    # Calculate days difference (approximating to the end of the month)
                    days_until_expiry = (expiry_date - current_date).days
                    
                    if days_until_expiry < 0:
                        status = "EXPIRED"
                        tag = "expired"
                    elif days_until_expiry < 60:  # 60 days warning window
                        status = "Expiring Soon"
                        tag = "expiring_soon"
                except ValueError:
                    status = "Invalid Date"
                    
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
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                for item in selected_items:
                    # Get the ID (first column) of the selected row
                    item_values = self.tree.item(item, 'values')
                    db_id = item_values[0]
                    
                    # Delete from database
                    cursor.execute("DELETE FROM inventory WHERE id=?", (db_id,))

                self.catalog.bump_inventory_revision(conn)
                conn.commit()
                conn.close()
                
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
        win.geometry("860x640")
        win.configure(bg="white")

        header = tk.Frame(win, bg="white", padx=20, pady=16)
        header.pack(fill=tk.X)
        tk.Label(header, text="Product Catalog", font=(UI_FONT, 14, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(
            header,
            text=(
                "Maps each alias/barcode and item code to one product, so scanning either finds the same stock. "
                "Importing a file adds and updates products; it never deletes existing ones."
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
                "Undo import",
                "Restore the products and stock to how they were before the last import?",
                parent=win,
            ):
                return
            try:
                self.catalog.undo_last_import()
            except ValueError as e:
                messagebox.showwarning("Undo import", str(e), parent=win)
                return
            refresh()
            self.refresh_data(run_sync=False)
            messagebox.showinfo("Undo import", "The last import was undone.", parent=win)

        tk.Button(actions, text="Import CSV...", command=choose_file, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=14, pady=6, cursor="hand2").pack(side=tk.LEFT)
        undo_button = tk.Button(actions, text="Undo last import", command=undo_import, bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=6, cursor="hand2")
        undo_button.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(win, textvariable=undo_info_var, font=(UI_FONT, 9), bg="white", fg="#6b7280", anchor="w", padx=20, wraplength=800, justify=tk.LEFT).pack(fill=tk.X, pady=(6, 0))

        search_var.trace_add("write", refresh)
        refresh()
        self.theme.style(win)

    def open_import_dialog(self, path, on_done):
        """Column-mapping wizard: pick which column is which field, preview what the
        import would change, then apply it."""
        try:
            headers, rows, delimiter, encoding = read_table(path)
        except Exception as e:
            messagebox.showerror("Import", f"Could not read that file: {e}")
            return
        if not headers or not rows:
            messagebox.showwarning("Import", "That file has no data rows.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Import Products")
        dlg.geometry("900x800")
        dlg.configure(bg="white")
        dlg.transient(self.root)
        dlg.grab_set()

        body = tk.Frame(dlg, bg="white", padx=20, pady=16)
        body.pack(fill=tk.BOTH, expand=True)

        delimiter_names = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}
        tk.Label(body, text=f"Import: {os.path.basename(path)}", font=(UI_FONT, 13, "bold"), bg="white", fg="#111827").pack(anchor="w")
        tk.Label(body, text=f"{len(rows):,} data rows, {delimiter_names[delimiter]}-separated, {encoding}", font=(UI_FONT, 9), bg="white", fg="#6b7280").pack(anchor="w", pady=(2, 10))
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

        tk.Label(body, text="What this import would change", font=(UI_FONT, 9, "bold"), bg="white").pack(anchor="w", pady=(12, 4))
        summary_box = ThemedScrolledText(body, height=14, wrap=tk.WORD, font=("Consolas", 9), bg="#f9fafb", relief=tk.SOLID, bd=1, state="disabled")
        summary_box.pack(fill=tk.BOTH, expand=True)

        state = {"records": None, "stats": None}

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
            state["records"] = state["stats"] = None
            import_button.config(state=tk.DISABLED)
            set_summary("Click \"Preview changes\" to see what this import would do. Nothing is changed until you click Import.")

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
                result = self.catalog.import_records(records, parse_stats, os.path.basename(path), dry_run=True)
            except Exception as e:
                messagebox.showerror("Import", f"Could not preview this file: {e}", parent=dlg)
                return
            finally:
                preview_button.config(state=tk.NORMAL, text="Preview changes")

            state["records"], state["stats"] = records, parse_stats
            text = format_import_summary(result)
            if result["samples"]["new"]:
                text += "\n\nNew products (first few):\n" + "\n".join(
                    f"  {desc or PLACEHOLDER_LABEL}   [code {code or '-'}, alias {alias or '-'}]"
                    for desc, code, alias in result["samples"]["new"]
                )
            if result["samples"]["updated"]:
                text += "\n\nRenamed (first few):\n" + "\n".join(
                    f"  {old or PLACEHOLDER_LABEL}  ->  {new}" for old, new in result["samples"]["updated"]
                )

            changes = result["new"] or result["updated"] or result["upgraded"] or result["absorbed"]
            text += "\n\n" + ("Nothing has been changed yet. Click Import to apply." if changes else "This file would not change anything.")
            set_summary(text)
            import_button.config(state=tk.NORMAL if changes else tk.DISABLED)

        def run_import():
            mapping = current_mapping()
            import_button.config(state=tk.DISABLED, text="Importing...")
            dlg.update_idletasks()
            try:
                self.catalog.save_mapping(headers, mapping)
                result = self.catalog.import_records(state["records"], state["stats"], os.path.basename(path))
            except Exception as e:
                messagebox.showerror("Import", f"The import failed and nothing was changed: {e}", parent=dlg)
                import_button.config(state=tk.NORMAL, text="Import")
                return

            dlg.destroy()
            on_done()
            self.refresh_data(run_sync=False)
            messagebox.showinfo(
                "Import complete",
                format_import_summary(result)
                + "\n\nYou can undo this from the Product Catalog window, until stock next changes.",
            )

        for combo in combos.values():
            combo.bind("<<ComboboxSelected>>", invalidate)

        button_row = tk.Frame(body, bg="white")
        button_row.pack(fill=tk.X, pady=(12, 0))
        import_button = tk.Button(button_row, text="Import", command=run_import, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=16, pady=8, state=tk.DISABLED)
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
        win.geometry("820x640")
        win.configure(bg="white")

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
        candidate_tree.pack(fill=tk.X, padx=20)

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

        button_row = tk.Frame(win, bg="white", padx=20, pady=14)
        button_row.pack(fill=tk.X)
        tk.Button(button_row, text="Assign to selected product", command=lambda: resolve(True), bg="#3b82f6", fg="white", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.LEFT)
        tk.Button(button_row, text="Record as unnamed product", command=lambda: resolve(False), bg="#f3f4f6", fg="#111827", relief=tk.FLAT, padx=14, pady=8).pack(side=tk.LEFT, padx=(10, 0))

        pending_tree.bind("<<TreeviewSelect>>", on_select)
        refresh()
        self.theme.style(win)

    def show_mobile_setup(self):
        """Displays a QR code for the mobile app to scan, establishing the Firestore connection."""
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
        qr_window.geometry("560x820")
        qr_window.configure(bg="white")
        qr_window.resizable(True, True)

        # Keep window on top
        qr_window.attributes('-topmost', True)

        tk.Label(
            qr_window,
            text="Connect Mobile",
            font=(UI_FONT, 14, "bold"),
            bg="white",
            fg="#111827",
        ).pack(pady=(18, 4))
        tk.Label(
            qr_window,
            text="Open the mobile web app, then scan this desktop QR to finish connecting.",
            font=(UI_FONT, 10),
            bg="white",
            fg="#4b5563",
            wraplength=500,
            justify=tk.LEFT,
        ).pack(pady=(0, 10), padx=20, anchor="w")

        instructions_frame = tk.Frame(qr_window, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=16)
        instructions_frame.pack(padx=20, fill=tk.X)

        tk.Label(instructions_frame, text="Mobile web app steps", font=(UI_FONT, 11, "bold"), bg="#f9fafb", fg="#111827", justify=tk.LEFT).pack(anchor="w")

        def add_mobile_step(parent, number, title, detail):
            step_row = tk.Frame(parent, bg="#f9fafb")
            step_row.pack(fill=tk.X, anchor="w", pady=(10, 0))

            badge = tk.Label(step_row, text=str(number), width=2, height=1, bg="#3b82f6", fg="white", font=(UI_FONT, 10, "bold"))
            badge.pack(side=tk.LEFT, padx=(0, 10))

            text_block = tk.Frame(step_row, bg="#f9fafb")
            text_block.pack(side=tk.LEFT, fill=tk.X, expand=True)

            tk.Label(text_block, text=title, font=(UI_FONT, 9, "bold"), bg="#f9fafb", fg="#111827", anchor="w", justify=tk.LEFT).pack(anchor="w")
            tk.Label(text_block, text=detail, font=(UI_FONT, 9), bg="#f9fafb", fg="#4b5563", wraplength=460, justify=tk.LEFT).pack(anchor="w", pady=(2, 0))

        add_mobile_step(instructions_frame, 1, "Open the mobile scanner app", "Open Stock Tracker in your phone or tablet browser.")
        add_mobile_step(instructions_frame, 2, "Scan the desktop QR", "In setup mode, scan the code below to connect the mobile app to this Firebase project.")
        add_mobile_step(instructions_frame, 3, "Start scanning barcodes", "Once connected, the mobile screen should change from setup mode to barcode entry mode.")

        fallback_frame = tk.Frame(qr_window, bg="#f9fafb", bd=1, relief=tk.SOLID, padx=16, pady=16)
        fallback_frame.pack(pady=16, padx=20, fill=tk.X)

        tk.Label(fallback_frame, text="Mobile pairing details", font=(UI_FONT, 11, "bold"), bg="#f9fafb", fg="#111827", wraplength=360, justify=tk.LEFT).pack(anchor="w")
        tk.Label(fallback_frame, text="Scan the QR code below in the mobile app so it can connect to this desktop session.", font=(UI_FONT, 10), bg="#f9fafb", fg="#4b5563", wraplength=460, justify=tk.LEFT).pack(anchor="w", pady=(8, 12))

        qr_container = tk.Frame(fallback_frame, bg="white", bd=1, relief=tk.SOLID)
        qr_container.pack(fill=tk.X, pady=(12, 0))

        tk.Label(
            qr_container,
            text="Scan this QR code in the mobile app",
            font=(UI_FONT, 10, "bold"),
            bg="white",
            fg="#111827",
        ).pack(anchor="w", padx=16, pady=(14, 8))

        qr_inner = tk.Frame(qr_container, bg="white")
        qr_inner.pack(fill=tk.X, padx=16, pady=(0, 14))

        qr_display = tk.Label(qr_inner, bg="white")
        qr_display.pack(anchor="center")

        if QR_AVAILABLE:
            qrcode = importlib.import_module("qrcode")
            PILImage = importlib.import_module("PIL.Image")
            ImageTk = importlib.import_module("PIL.ImageTk")

            qr_code = qrcode.QRCode(box_size=6, border=2)
            qr_code.add_data(pairing_payload)
            qr_code.make(fit=True)
            qr_image = qr_code.make_image(fill_color="black", back_color="white")
            qr_image = qr_image.convert("RGB").resize((260, 260), PILImage.Resampling.NEAREST)
            tk_image = ImageTk.PhotoImage(qr_image)
            qr_display.configure(image=tk_image)
            qr_display.image = tk_image
        else:
            qr_display.configure(
                text="Install qrcode and pillow to generate the QR preview.",
                font=(UI_FONT, 9),
                fg="#4b5563",
                justify=tk.LEFT,
                wraplength=340,
                padx=8,
                pady=8,
            )

        def copy_payload():
            self.root.clipboard_clear()
            self.root.clipboard_append(pairing_payload)
            self.root.update()
            messagebox.showinfo("Copied", "Pairing details copied to clipboard.")

        tk.Button(fallback_frame, text="Copy Pairing Details", command=copy_payload, bg="#3b82f6", fg="white", relief=tk.FLAT, padx=10, pady=5).pack(anchor="e", pady=(12, 0))

        # Option to reset/change the web config
        def reset_config():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM settings WHERE key='firebase_web_config'")
            conn.commit()
            conn.close()
            qr_window.destroy()
            self.show_mobile_setup()

        tk.Button(qr_window, text="Change Firebase Web Config", command=reset_config, bg="#f3f4f6", relief=tk.FLAT, padx=10, pady=5).pack(pady=5)

        qr_container._keep_subtree = True  # QR codes must stay dark-on-white to scan
        self.theme.style(qr_window)


if __name__ == "__main__":
    # Create the main window and start the application loop
    root = tk.Tk()
    app = StockTrackerApp(root)
    
    # Optional: Set window icon (if you have an .ico file, uncomment line below)
    # root.iconbitmap('icon.ico')
    
    root.mainloop()