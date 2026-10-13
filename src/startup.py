"""
Launch-on-startup for the installed app: a per-user entry under HKCU\\...\\Run (no admin needed).

Only the packaged .exe ever registers itself, so running from source can't leave a stray entry
pointing at python.exe. The entry passes --background so the app starts hidden in the tray.
"""
import sys

import app_paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "StockTracker"
BACKGROUND_FLAG = "--background"


def supported():
    return sys.platform == "win32" and app_paths.is_frozen()


def _command():
    return f'"{sys.executable}" {BACKGROUND_FLAG}'


def is_enabled():
    if not supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, VALUE_NAME)[0])
    except OSError:
        return False


def set_enabled(enabled):
    """Adds or removes the startup entry. Returns True on success."""
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
