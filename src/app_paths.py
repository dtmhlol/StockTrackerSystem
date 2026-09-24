"""
Where the app keeps its files.

Run from source, everything stays in the project folder (database/, config/), as
it always has. Run as the installed .exe, the program folder is read-only and is
wiped on upgrade/uninstall, so data lives in %LOCALAPPDATA%\\StockTracker instead.
"""
import os
import sys

APP_VERSION = "1.0.0"
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
