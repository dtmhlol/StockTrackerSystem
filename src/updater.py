"""
Manual update check and one-click install, using GitHub Releases.

The check asks GitHub for the latest published release. Installing downloads that release's
Windows installer and runs it silently, so the destination PC needs no Python.

Because this downloads and RUNS a program, nothing is installed unless the installer's
signature verifies against the public key built into this app (app_paths.UPDATE_PUBLIC_KEY).
Only whoever holds the matching private key can publish an update the app will accept, so a
tampered download or a hijacked GitHub account cannot push code onto client PCs.
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import app_paths

INSTALLER_PATTERN = re.compile(r"^StockTracker-Setup-.+\.exe$")
_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")


class UpdateError(Exception):
    """A problem with a user-readable message."""


class SignatureError(UpdateError):
    """The download failed its security check and must NOT be installed."""


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    installer_name: str
    installer_url: str
    installer_size: int
    signature_url: str


# ----------------------------------------------------------------- versions

def parse_version(text):
    """'v1.2.3' -> (1, 2, 3). Raises ValueError for anything else."""
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(text).strip())
    if not match:
        raise ValueError(f"Not a version number: {text!r}")
    return tuple(int(part) for part in match.groups())


def is_newer(candidate, current):
    return parse_version(candidate) > parse_version(current)


# ------------------------------------------------------------ asking GitHub

def _request(url, timeout):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"StockTracker/{app_paths.APP_VERSION}",
    })
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_latest(api_url=None, timeout=12):
    """The latest published (non-draft, non-prerelease) release. Raises UpdateError."""
    url = api_url or app_paths.UPDATE_API_URL
    try:
        with _request(url, timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("No releases have been published yet.")
        if e.code in (403, 429):
            raise UpdateError("GitHub is limiting requests right now. Try again in a few minutes.")
        raise UpdateError(f"GitHub returned an error (HTTP {e.code}).")
    except urllib.error.URLError:
        raise UpdateError("Couldn't reach GitHub. Check the internet connection and try again.")
    except (ValueError, TimeoutError, OSError):
        raise UpdateError("GitHub sent a response that couldn't be read. Try again later.")

    try:
        tag = data["tag_name"]
        version = ".".join(str(n) for n in parse_version(tag))
    except (KeyError, TypeError, ValueError):
        raise UpdateError("The latest release has no readable version number.")

    assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
    installer = next((a for name, a in assets.items() if name and INSTALLER_PATTERN.match(name)), None)
    signature = assets.get(installer["name"] + ".sig") if installer else None

    return Release(
        version=version,
        tag=tag,
        notes=(data.get("body") or "").strip(),
        page_url=data.get("html_url") or f"https://github.com/{app_paths.GITHUB_REPO}/releases",
        installer_name=installer["name"] if installer else "",
        installer_url=installer.get("browser_download_url", "") if installer else "",
        installer_size=int(installer.get("size") or 0) if installer else 0,
        signature_url=signature.get("browser_download_url", "") if signature else "",
    )


def install_blocker(release):
    """Why this release can't be installed automatically, or None if it can."""
    if not (app_paths.is_frozen() and sys.platform == "win32"):
        return "Automatic install is only available in the installed app. From source, update with git pull."
    if not app_paths.UPDATE_PUBLIC_KEY:
        return "This build has no update signing key, so it can't verify downloads."
    if not release.installer_url:
        return "This release has no Windows installer attached."
    if not release.signature_url:
        return "This release isn't signed, so it can't be installed automatically."
    return None


# ------------------------------------------------------------- downloading

def _check_source(url):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
        raise UpdateError("The update isn't hosted on GitHub over HTTPS, so it was not downloaded.")


def download(url, destination, expected_size=0, progress=None, cancelled=None, timeout=30):
    """Streams `url` to `destination`. progress(done_bytes, total_bytes); cancelled() -> bool."""
    _check_source(url)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    partial = destination + ".part"
    try:
        with _request(url, timeout) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            done = 0
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Update cancelled.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if expected_size and os.path.getsize(partial) != expected_size:
            raise UpdateError("The download was incomplete. Try again.")
        os.replace(partial, destination)
    except urllib.error.URLError:
        raise UpdateError("The download failed. Check the internet connection and try again.")
    except OSError as e:
        raise UpdateError(f"Couldn't save the download: {e}")
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    return destination


def fetch_signature(release, timeout=15):
    """The release's .sig file contents (a base64 string)."""
    _check_source(release.signature_url)
    try:
        with _request(release.signature_url, timeout) as response:
            return response.read(4096).decode("ascii", "ignore").strip()
    except (urllib.error.URLError, OSError):
        raise UpdateError("Couldn't download the update's signature.")


# ------------------------------------------------------------ verification

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signed_message(version, digest):
    """The exact bytes that are signed: bound to the version so an old signed installer
    can't be passed off as a newer release."""
    return f"StockTracker|{version}|{digest}".encode("ascii")


def verify(path, version, signature_b64, public_key_b64=None):
    """Raises SignatureError unless `signature_b64` is a valid signature, by the app's
    public key, of this exact file and version."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    public_key_b64 = public_key_b64 if public_key_b64 is not None else app_paths.UPDATE_PUBLIC_KEY
    if not public_key_b64:
        raise UpdateError("This build has no update signing key, so it can't verify downloads.")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        raise SignatureError("The update's signature is malformed.")
    try:
        key.verify(signature, signed_message(version, sha256_file(path)))
    except InvalidSignature:
        raise SignatureError("The downloaded update failed its security check.")


# ------------------------------------------------------------ installing

def backup_database(db_path, keep=5, reason="before-update"):
    """Copies the database into its `backups` folder (SQLite's own backup API, safe while open),
    named with the time and `reason`. Keeps the newest `keep` backups for that reason.
    Returns the backup path."""
    folder = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"store_inventory-{datetime.now():%Y%m%d-%H%M%S}-{reason}.db")
    source, destination = sqlite3.connect(db_path), sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    old = sorted(f for f in os.listdir(folder) if f.endswith(f"-{reason}.db"))
    for name in old[:-keep]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    return target


def download_folder():
    return os.path.join(tempfile.gettempdir(), "StockTracker-update")


def launch_installer(path, relaunch=True):
    """Starts the installer silently and detached, so it keeps running after this app exits.
    /RELAUNCH=1 makes the installer reopen the app once it has finished."""
    arguments = [path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"]
    if relaunch:
        arguments.append("/RELAUNCH=1")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(arguments, creationflags=flags, close_fds=True)
