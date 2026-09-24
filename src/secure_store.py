"""
Encrypted storage for the Firebase service account key.

The key is encrypted with Windows DPAPI (CryptProtectData) scoped to the current
Windows user and machine, plus an app-specific entropy string. The encrypted
file is useless if copied to another PC or opened under another Windows account.

Honest limits: code running as the same logged-in user (including this app) can
decrypt it, so this protects against copying/browsing the file, not against
malware or someone using the signed-in session. Limit the key's IAM role too.
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes

_MAGIC = b"STDPAPI1"
_ENTROPY = b"StockTracker/firebase-credentials/v1"
_UI_FORBIDDEN = 0x1


class SecureStoreError(Exception):
    """Raised with a user-readable message when the key can't be stored or read."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _require_windows():
    if sys.platform != "win32":
        raise SecureStoreError("Secure credential storage needs Windows (it uses the Windows data protection API).")


def _crypt32():
    crypt32 = ctypes.windll.crypt32
    blob_ptr = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [blob_ptr, wintypes.LPCWSTR, blob_ptr, ctypes.c_void_p,
                                         ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [blob_ptr, ctypes.c_void_p, blob_ptr, ctypes.c_void_p,
                                           ctypes.c_void_p, wintypes.DWORD, blob_ptr]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32


def protect(data):
    """Encrypts bytes for the current Windows user on this machine."""
    _require_windows()
    crypt32 = _crypt32()
    source, keep_source = _blob_from(data)
    entropy, keep_entropy = _blob_from(_ENTROPY)
    result = _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(source), "Stock Tracker credentials",
                                    ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(result)):
        raise SecureStoreError("Windows could not encrypt the credentials.")
    try:
        return ctypes.string_at(ctypes.cast(result.pbData, ctypes.c_void_p), result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def unprotect(blob):
    """Decrypts bytes produced by protect() under the same Windows user and machine."""
    _require_windows()
    crypt32 = _crypt32()
    source, keep_source = _blob_from(blob)
    entropy, keep_entropy = _blob_from(_ENTROPY)
    result = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy),
                                      None, None, _UI_FORBIDDEN, ctypes.byref(result)):
        raise SecureStoreError(
            "The stored Firebase credentials can't be decrypted. They were saved by a different "
            "Windows account or PC, or the file is damaged. Import the key file again."
        )
    try:
        return ctypes.string_at(ctypes.cast(result.pbData, ctypes.c_void_p), result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save_service_account(path, info):
    """Encrypts a service account key (a dict) and writes it to `path`."""
    payload = protect(json.dumps(info).encode("utf-8"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "wb") as f:
        f.write(_MAGIC + payload)
    os.replace(temporary, path)  # never leaves a half-written credentials file


def load_service_account(path):
    """Returns the stored key as a dict, or None if nothing is stored.
    Raises SecureStoreError if it exists but can't be read."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise SecureStoreError(f"Could not read the stored credentials: {e}")
    if not raw.startswith(_MAGIC):
        raise SecureStoreError("The stored credentials file is not in the expected format. Import the key file again.")
    try:
        return json.loads(unprotect(raw[len(_MAGIC):]).decode("utf-8"))
    except ValueError:
        raise SecureStoreError("The stored credentials are damaged. Import the key file again.")


def secure_delete(path):
    """Overwrites a file with zeros, then deletes it. Best effort: SSDs and
    backups can keep old copies, which is why the key should also be rotated."""
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.write(b"\x00" * size)
        f.flush()
        os.fsync(f.fileno())
    os.remove(path)
