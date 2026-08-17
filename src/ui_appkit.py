"""Geteilte AppKit/pyobjc-UI-Helfer (rumps-frei).

Bewusst **ohne** rumps-Import: diese Helfer bedienen das neue Einstellungs-Fenster
(`prefs_window.py`) und später (Phase 3) ein natives ``NSStatusItem`` — Ziel ist genau **eine**
UI-Architektur in pyobjc. Alles hier ist macOS-only und muss auf dem **Main-Thread** laufen
(AppKit-Regel); für die Marshalling-Rückkehr aus Hintergrund-Threads gibt es :func:`run_on_main`.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

# NSAlert-Button-Rückgabewerte (Foundation-Konstanten, hier fix statt Import).
_NS_ALERT_FIRST_BUTTON = 1000  # NSAlertFirstButtonReturn


def run_on_main(fn: Callable, *args) -> None:
    """Führt ``fn(*args)`` **asynchron** auf dem Main-Thread aus (feuern und vergessen).

    Für UI-Updates aus Hintergrund-Threads, bei denen niemand auf ein Ergebnis wartet.
    Wer eine Rückgabe oder eine deterministische Reihenfolge braucht, nimmt
    :func:`run_on_main_sync`.
    """
    from PyObjCTools import AppHelper

    AppHelper.callAfter(fn, *args)


def is_main_thread() -> bool:
    """True, wenn der aufrufende Thread der AppKit-Main-Thread ist."""
    from Foundation import NSThread

    return bool(NSThread.isMainThread())


def run_on_main_sync(fn: Callable):
    """Führt ``fn()`` **synchron** auf dem Main-Thread aus; reicht Rückgabe/Fehler durch.

    Auf dem Main-Thread direkt (kein Dispatch, keine Deadlock-Gefahr); sonst über die
    Main-Operation-Queue und per :class:`threading.Event` auf das Ergebnis warten.
    Genutzt von :mod:`src.statusitem` und :mod:`src.timers`, die AppKit-Objekte anfassen
    und dabei eine definierte Reihenfolge brauchen (z. B. Menü ersetzen).
    """
    import threading

    from Foundation import NSOperationQueue, NSThread

    if NSThread.isMainThread():
        return fn()

    box: dict = {}
    done = threading.Event()

    def _wrapper() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - auf den Aufrufer-Thread weiterreichen
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapper)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def activate_app() -> None:
    """Holt die Menüleisten-App (LSUIElement) in den Vordergrund, sonst öffnen Fenster dahinter."""
    from AppKit import NSApp

    NSApp.activateIgnoringOtherApps_(True)


def alert(title: str, message: str = "", style: str = "info") -> None:
    """Zeigt einen modalen NSAlert (nur OK)."""
    from AppKit import NSAlert, NSAlertStyleCritical, NSAlertStyleWarning

    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    if message:
        a.setInformativeText_(message)
    if style == "warning":
        a.setAlertStyle_(NSAlertStyleWarning)
    elif style == "critical":
        a.setAlertStyle_(NSAlertStyleCritical)
    a.addButtonWithTitle_("OK")
    activate_app()
    a.runModal()


def confirm(title: str, message: str = "", ok: str = "OK", cancel: str = "Abbrechen") -> bool:
    """Modaler Ja/Nein-Dialog. True, wenn der erste (OK-)Button geklickt wurde."""
    from AppKit import NSAlert

    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    if message:
        a.setInformativeText_(message)
    a.addButtonWithTitle_(ok)
    a.addButtonWithTitle_(cancel)
    activate_app()
    return a.runModal() == _NS_ALERT_FIRST_BUTTON


def ask_text(title: str, message: str = "", default: str = "", secure: bool = False) -> Optional[str]:
    """Modale Texteingabe via NSAlert + Eingabefeld. None bei Abbruch, sonst getrimmter Text."""
    from AppKit import NSAlert, NSSecureTextField, NSTextField
    from Foundation import NSMakeRect

    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    if message:
        a.setInformativeText_(message)
    a.addButtonWithTitle_("OK")
    a.addButtonWithTitle_("Abbrechen")
    cls = NSSecureTextField if secure else NSTextField
    field = cls.alloc().initWithFrame_(NSMakeRect(0, 0, 280, 24))
    field.setStringValue_(default or "")
    a.setAccessoryView_(field)
    activate_app()
    # Eingabefeld direkt fokussieren, damit man sofort tippen kann.
    a.window().setInitialFirstResponder_(field)
    if a.runModal() != _NS_ALERT_FIRST_BUTTON:
        return None
    return field.stringValue().strip()


def choose_directory(message: str, default_path: Optional[str] = None) -> Optional[str]:
    """Nativer Finder-Ordnerdialog (NSOpenPanel). Muss auf dem Main-Thread laufen.

    Gibt den gewählten Pfad zurück oder None bei Abbruch.
    """
    from AppKit import NSOpenPanel
    from Foundation import NSURL

    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setCanCreateDirectories_(True)
    panel.setPrompt_("Auswählen")
    panel.setMessage_(message)
    if default_path:
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(default_path))
    activate_app()
    if panel.runModal() != 1:  # 1 == NSModalResponseOK
        return None
    urls = panel.URLs()
    return urls[0].path() if urls else None
