"""
Publisher-side tools for releases. Not part of the app; run from the project folder:

  python installer\\release_tools.py init-key           create the signing key (once)
  python installer\\release_tools.py sign <installer>   write <installer>.sig
  python installer\\release_tools.py verify <installer>  check the .sig against the key built into the app
  python installer\\release_tools.py notes <version> [--out FILE]   print (or save as UTF-8) that version's CHANGELOG section

The private key lives OUTSIDE the project (in your user profile), so it can never be committed.
Whoever holds it can publish updates that every installed copy will accept: keep it private
and keep a backup somewhere safe (a password manager). If it is lost, installed copies can't
be updated automatically again until they are reinstalled manually with a new key.
"""
import base64
import os
import re
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import app_paths  # noqa: E402
import updater  # noqa: E402

KEY_PATH = os.path.join(os.path.expanduser("~"), ".stocktracker", "release-signing-key.pem")


def load_private_key():
    if not os.path.exists(KEY_PATH):
        sys.exit(f"No signing key at {KEY_PATH}. Run:  python installer\\release_tools.py init-key")
    with open(KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_key_b64(private_key):
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def version_of(installer_path):
    match = re.search(r"StockTracker-Setup-(\d+\.\d+\.\d+)\.exe$", installer_path)
    if not match:
        sys.exit("The installer file name must look like StockTracker-Setup-1.2.3.exe")
    return match.group(1)


def init_key():
    if os.path.exists(KEY_PATH):
        sys.exit(f"A signing key already exists at {KEY_PATH}. Not overwriting it.")
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    key = Ed25519PrivateKey.generate()
    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    print(f"Signing key created: {KEY_PATH}")
    print("BACK THIS FILE UP somewhere safe and private. Anyone with it can publish updates.")
    print("\nPublic key (goes into UPDATE_PUBLIC_KEY in src/app_paths.py):")
    print(public_key_b64(key))


def sign(installer_path):
    version = version_of(installer_path)
    key = load_private_key()
    if public_key_b64(key) != app_paths.UPDATE_PUBLIC_KEY:
        sys.exit("This signing key does NOT match UPDATE_PUBLIC_KEY in src/app_paths.py. "
                 "Apps built with that key would reject the update.")
    digest = updater.sha256_file(installer_path)
    signature = base64.b64encode(key.sign(updater.signed_message(version, digest))).decode("ascii")
    out = installer_path + ".sig"
    with open(out, "w", encoding="ascii") as f:
        f.write(signature + "\n")
    print(f"Signed {os.path.basename(installer_path)} (sha256 {digest[:16]}...) -> {os.path.basename(out)}")


def verify(installer_path):
    version = version_of(installer_path)
    with open(installer_path + ".sig", encoding="ascii") as f:
        signature = f.read().strip()
    updater.verify(installer_path, version, signature)     # uses the key built into the app
    print("Signature OK: the built-in public key accepts this installer.")


def notes(version, out_path=None):
    """Prints (or writes, as UTF-8) the CHANGELOG section for `version`. Writing to a file
    avoids the Windows console's legacy encoding, which can't print emoji."""
    path = os.path.join(ROOT, "CHANGELOG.md")
    text = open(path, encoding="utf-8").read()
    match = re.search(rf"^## \[?{re.escape(version)}\]?.*?\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        sys.exit(f"CHANGELOG.md has no section for version {version}.")
    text = match.group(1).strip() + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "init-key":
        init_key()
    elif command == "sign" and len(sys.argv) == 3:
        sign(sys.argv[2])
    elif command == "verify" and len(sys.argv) == 3:
        verify(sys.argv[2])
    elif command == "notes" and len(sys.argv) in (3, 5) and (len(sys.argv) == 3 or sys.argv[3] == "--out"):
        notes(sys.argv[2], sys.argv[4] if len(sys.argv) == 5 else None)
    else:
        print(__doc__)
        sys.exit(1)
