"""py2app-Build-Konfiguration für das Menüleisten-Bundle.

Baut ``iCloud Sync.app`` als Menüleisten-Resident (``LSUIElement``).

Vom Repo-Root ausführen (py2app-Artefakte landen in ``dist/`` bzw. ``build/_py2app/``)::

    .venv/bin/pip install -r requirements-build.txt
    .venv/bin/python build/setup.py py2app \
        --dist-dir dist --bdist-base build/_py2app

Für lokale Iteration (kein eigenständiges Bundle, referenziert die Quelldateien)::

    .venv/bin/python build/setup.py py2app -A \
        --dist-dir dist --bdist-base build/_py2app

Anschließend ad-hoc signieren (siehe README) und ggf. die Quarantäne entfernen.
"""

from __future__ import annotations

import glob
import os
import sys

from setuptools import setup

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

APP = [os.path.join(ROOT, "launcher.py")]

# charset_normalizer ist mypyc-kompiliert und benötigt eine separate Shared-Lib
# (``*__mypyc*.so``) im site-packages-Root, die py2app sonst nicht mitnimmt. Wir kopieren
# sie auf den Python-Pfad des Bundles, sonst schlägt ``import charset_normalizer`` fehl.
_PY_LIBDIR = f"lib/python{sys.version_info.major}.{sys.version_info.minor}"
DATA_FILES = []
try:
    import charset_normalizer  # noqa: F401

    _sp_root = os.path.dirname(os.path.dirname(charset_normalizer.__file__))
    _mypyc = glob.glob(os.path.join(_sp_root, "*__mypyc*.so"))
    if _mypyc:
        DATA_FILES.append((_PY_LIBDIR, _mypyc))
except Exception:  # noqa: BLE001 - ohne charset_normalizer baut es trotzdem
    pass

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "LSUIElement": True,  # reine Menüleisten-App: kein Dock-Icon, kein Fenster
        "CFBundleName": "iCloud Sync",
        "CFBundleDisplayName": "iCloud Sync",
        "CFBundleIdentifier": "de.nicx.icloud-sync",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHumanReadableCopyright": "MIT – nicx",
    },
    # Pakete vollständig einbetten (Quellpaket + Abhängigkeiten mit Binär-/Datenanteilen).
    # Pakete, die als Ganzes eingebettet werden (dynamische Importe / Datendateien).
    # Namespace-Pakete (jaraco.*, keyrings.*) NICHT hier listen — py2app kann sie nicht
    # als Paket erzwingen; sie werden als Abhängigkeiten automatisch eingezogen.
    "packages": [
        "src",
        "rumps",
        "pyicloud",
        "keyring",
        "pync",
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
        "cryptography",
        "cffi",
        "fido2",
        "pydantic",
        "pydantic_core",
        "srp",
    ],
    "includes": ["sqlite3"],
}

_ICON = os.path.join(HERE, "icon.icns")
if os.path.exists(_ICON):
    OPTIONS["iconfile"] = _ICON

if __name__ == "__main__":
    setup(
        app=APP,
        data_files=DATA_FILES,
        options={"py2app": OPTIONS},
        setup_requires=["py2app"],
    )
