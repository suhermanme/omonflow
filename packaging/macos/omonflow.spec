# PyInstaller spec for the macOS app bundle (M02 smoke test; M03 adds audio).
# Build:  uv run --group package pyinstaller --noconfirm packaging/macos/omonflow.spec
# Sign:   packaging/macos/sign_adhoc.sh dist/OmonFlow.app
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import omonflow  # noqa: E402

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "src" / "omonflow" / "resources"), "omonflow/resources")],
    hiddenimports=["omonflow._core", "AppKit", "Foundation", "objc", "AVFoundation", "sounddevice",
                   "_sounddevice_data", "numpy", "ApplicationServices", "Quartz", "sqlite3",
                   # M07: refinement (imported lazily) and the macOS Keychain backend for API keys
                   "omonflow.refinement.ai", "omonflow.refinement.clean", "omonflow.refinement.guard",
                   "omonflow.refinement.refiner", "omonflow.refinement.secrets",
                   "keyring", "keyring.backends.macOS"],
    excludes=["tkinter", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OmonFlow", console=False,
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name="OmonFlow")
app = BUNDLE(
    coll,
    name="OmonFlow.app",
    bundle_identifier=omonflow.APP_ID,
    version=omonflow.__version__.split(".dev")[0],
    info_plist={
        "CFBundleName": "OmonFlow",
        "CFBundleDisplayName": "OmonFlow",
        "LSUIElement": True,  # menu-bar app: no Dock icon, no activation on show
        "LSMinimumSystemVersion": "13.0",  # PySide6 6.11 wheels require macOS 13+
        "NSMicrophoneUsageDescription": "OmonFlow records your voice only while you dictate, after you click the microphone.",
        "NSHighResolutionCapable": True,
    },
)
