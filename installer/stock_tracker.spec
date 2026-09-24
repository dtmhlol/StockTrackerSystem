# PyInstaller recipe for Stock Tracker. Build with installer\build.ps1 (or:
#   python -m PyInstaller installer\stock_tracker.spec --noconfirm --clean)
# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

datas = [(os.path.join(SPECPATH, "app.ico"), ".")]
binaries = []
# The app imports these dynamically (importlib.import_module), which PyInstaller
# can't see, so they are listed explicitly.
hiddenimports = ["qrcode", "qrcode.image.pil", "PIL.Image", "PIL.ImageTk", "PIL._tkinter_finder"]

# Firebase / Firestore / gRPC ship data files and lazily loaded submodules.
for package in ("firebase_admin", "google.cloud.firestore", "google.cloud.firestore_v1", "grpc"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [os.path.join(SRC, "pc_backend_automation_service.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "scipy", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir: faster start-up and fewer antivirus false positives than onefile
    name="StockTracker",
    icon=os.path.join(SPECPATH, "app.ico"),
    console=False,              # windowed app; errors go to error.log in the data folder
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="StockTracker", upx=False)
