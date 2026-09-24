"""
Where the app keeps its files.

Run from source, everything stays in the project folder (database/, config/), as
it always has. Run as the installed .exe, the program folder is read-only and is
wiped on upgrade/uninstall, so data lives in %LOCALAPPDATA%\\StockTracker instead.
"""
import os
import sys

APP_VERSION = "1.1.0"

# Updates come from this repository's GitHub Releases.
GITHUB_REPO = "dtmhlol/StockTrackerSystem"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Public half of the release signing key (base64). Installers are only accepted if signed by
# the matching private key. Created with:  python installer\release_tools.py init-key
UPDATE_PUBLIC_KEY = "CaxvWbJkpJFw/Z5DKh2LIkpMq8ykrA5dbHwj4pPvCX8="

# Where the phone page is hosted. Connect Mobile shows a QR code for this address unless
# a different one has been saved there (e.g. a client hosting their own copy).
DEFAULT_MOBILE_APP_URL = "https://dtmhlol.github.io/StockTrackerSystem/"
DATA_FOLDER = "StockTracker"


def is_frozen():
    """True when running as the packaged .exe."""
    return bool(getattr(sys, "frozen", False))


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, DATA_FOLDER)
    else:
        path = _project_root()
    os.makedirs(path, exist_ok=True)
    return path


def _subdir(name):
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def database_path():
    return os.path.join(_subdir("database"), "store_inventory.db")


def config_dir():
    return _subdir("config")


def log_path():
    return os.path.join(data_dir(), "error.log")


def resource_path(name):
    """A read-only file shipped with the app (e.g. the icon)."""
    if is_frozen():
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), name)
    return os.path.join(_project_root(), "installer", name)
