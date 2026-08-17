"""Tests für die UI-Datenschicht (Menü-Spezifikation, Main-Thread-Marshalling).

Ausführen::

    .venv/bin/python tests/test_ui.py

Geprüft wird die **reine Logik** von :mod:`src.statusitem` — sie entscheidet, welche
Einträge klickbar sind und welche per ``key`` live aktualisierbar bleiben. Die AppKit-
Schicht (NSStatusItem/NSMenu/NSTimer) braucht eine laufende GUI und wird per Smoke-Test
abgedeckt, nicht hier.

Stil wie ``test_sync.py``: eigenständiges Skript, kein Netz, ``HOME`` im Temp.
"""

from __future__ import annotations

import os
import tempfile

os.environ["HOME"] = tempfile.mkdtemp(prefix="iclbk_test_ui_home_")
import sys  # noqa: E402

sys.path.insert(0, os.getcwd())

from src.statusitem import SEPARATOR, MenuEntry  # noqa: E402
from src.ui_appkit import is_main_thread, run_on_main_sync  # noqa: E402

PASS = []


def check(cond, msg):
    assert cond, "FAIL: " + msg
    PASS.append(msg)


# -- Menü-Spezifikation ------------------------------------------------------

def test_info_line_not_clickable():
    # Die Dienste-/Fehler-Zeilen im Account-Untermenü sind reine Info.
    check(not MenuEntry("Dienste: Drive, Photos").is_enabled(),
          "Eintrag ohne Callback ist deaktiviert")
    check(not MenuEntry("⚠️ Letzter Fehler: kaputt").is_enabled(),
          "Fehler-Infozeile ist deaktiviert")


def test_action_enabled():
    check(MenuEntry("Sync jetzt", lambda: None).is_enabled(),
          "Eintrag mit Callback ist aktiv")


def test_submenu_enabled_without_callback():
    parent = MenuEntry("user@example.com", children=[MenuEntry("Sync jetzt", lambda: None)])
    check(parent.is_enabled(), "Account-Eintrag mit Untermenü ist aktiv")
    check(not MenuEntry("leer", children=[]).is_enabled(),
          "leeres Untermenü ist deaktiviert")


def test_explicit_enabled_wins():
    check(not MenuEntry("x", lambda: None, enabled=False).is_enabled(),
          "explizites enabled=False schlägt die Automatik")
    check(MenuEntry("x", enabled=True).is_enabled(),
          "explizites enabled=True schlägt die Automatik")


def test_key_for_live_updates():
    # Der key ist die Apple-ID: darüber ersetzt der UI-Tick den Titel in place,
    # ohne das Menü neu zu bauen (Spinner + Live-Counts).
    entry = MenuEntry("• a@b.de", children=[], key="a@b.de")
    check(entry.key == "a@b.de", "key wird durchgereicht")
    check(MenuEntry("ohne").key is None, "key ist optional (Default None)")


def test_separator_is_distinct():
    check(SEPARATOR is not MenuEntry("-"), "SEPARATOR ist kein MenuEntry")
    check(not isinstance(SEPARATOR, MenuEntry), "SEPARATOR ist ein eigener Sentinel")


def test_defaults():
    e = MenuEntry("x")
    check(e.callback is None and e.children is None and e.enabled is None and not e.checked,
          "MenuEntry-Defaults sind leer/aus")


# -- Main-Thread-Marshalling -------------------------------------------------

def test_run_on_main_sync_direct_path():
    # Der Test läuft selbst auf dem Main-Thread -> Direktpfad ohne Dispatch.
    check(is_main_thread(), "Test läuft auf dem Main-Thread")
    check(run_on_main_sync(lambda: 42) == 42, "run_on_main_sync reicht die Rückgabe durch")
    check(run_on_main_sync(lambda: None) is None, "run_on_main_sync kann None liefern")

    calls = []
    run_on_main_sync(lambda: calls.append(1))
    check(calls == [1], "run_on_main_sync ruft genau einmal auf")


def test_run_on_main_sync_propagates_exception():
    def boom():
        raise ValueError("kaputt")

    try:
        run_on_main_sync(boom)
    except ValueError:
        PASS.append("run_on_main_sync reicht Exceptions an den Aufrufer durch")
    else:
        raise AssertionError("FAIL: Exception wurde verschluckt")


if __name__ == "__main__":
    test_info_line_not_clickable()
    test_action_enabled()
    test_submenu_enabled_without_callback()
    test_explicit_enabled_wins()
    test_key_for_live_updates()
    test_separator_is_distinct()
    test_defaults()
    test_run_on_main_sync_direct_path()
    test_run_on_main_sync_propagates_exception()
    for m in PASS:
        print("  ok:", m)
    print(f"\nALL {len(PASS)} UI TESTS PASSED")
