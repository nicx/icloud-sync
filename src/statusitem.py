"""Menüleisten-Item auf Basis von ``NSStatusItem``/``NSMenu`` (rumps-frei).

Ersetzt ``rumps.App``/``rumps.MenuItem`` — Phase 3 der UI-Konvergenz auf eine einzige
pyobjc-UI (siehe CLAUDE.md). Aufteilung wie bei :mod:`src.prefs_window`:

- Die **Menü-Spezifikation** (:class:`MenuEntry`, :data:`SEPARATOR`) ist reine Daten-Logik
  ohne AppKit-Import und damit unit-testbar.
- Die AppKit-Schicht (:class:`StatusItem`) übersetzt sie in ein ``NSMenu``.

Zwei Fallstricke, die das Design bestimmen:

- **``NSMenuItem.target`` ist eine schwache Referenz.** Ein pro Eintrag erzeugtes
  Ziel-Objekt würde deallokiert und der Klick liefe ins Leere. Daher **ein** langlebiges
  :class:`_MenuTarget` je :class:`StatusItem`; die Zuordnung Eintrag→Callback läuft über
  das ``tag``-Feld.
- **Der Live-Fortschritt darf das Menü nicht neu bauen.** Während eines Syncs aktualisiert
  die App im Sekundentakt Spinner und Zähler. Ein kompletter Neuaufbau wäre verschwenderisch
  und würde ein geöffnetes Menü stören — deshalb tragen Einträge einen stabilen ``key``,
  über den :meth:`StatusItem.set_item_title` den Titel **an Ort und Stelle** ändert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .ui_appkit import run_on_main_sync

LOGGER = logging.getLogger(__name__)

# Kantenlänge des Menüleisten-Icons in Punkten. Die gerenderten PNGs sind quadratisch
# (siehe :mod:`src.menubar_icon`); ohne explizites setSize_ würde NSImage die Pixelmaße
# als Punkte interpretieren und das Icon die Menüleiste sprengen.
ICON_POINTS = 20.0

_STATE_ON = 1
_STATE_OFF = 0


class _Separator:
    """Sentinel für eine Trennlinie im Menü."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - nur Debug-Ausgabe
        return "SEPARATOR"


SEPARATOR = _Separator()


@dataclass(frozen=True)
class MenuEntry:
    """Ein Menüeintrag als reine Daten (ohne AppKit).

    :param title: Beschriftung.
    :param callback: parameterloses Callable; ``None`` = nicht klickbar (Info-Zeile).
    :param checked: Häkchen an/aus.
    :param children: Untermenü-Einträge (dann wird ``callback`` ignoriert).
    :param enabled: erzwingt den Aktiv-Zustand; ``None`` = automatisch (aktiv, wenn ein
        Callback oder ein Untermenü vorhanden ist).
    :param key: stabiler Schlüssel für spätere In-place-Titeländerungen
        (:meth:`StatusItem.set_item_title`), z. B. die Apple-ID eines Accounts.
    """

    title: str
    callback: Optional[Callable[[], None]] = None
    checked: bool = False
    children: Optional[Sequence] = field(default=None)
    enabled: Optional[bool] = None
    key: Optional[str] = None

    def is_enabled(self) -> bool:
        """Effektiver Aktiv-Zustand (explizit gesetzt oder automatisch abgeleitet)."""
        if self.enabled is not None:
            return self.enabled
        return self.callback is not None or bool(self.children)


# -- AppKit-Schicht ----------------------------------------------------------
# NSObject lazy/guarded importieren: hält das Modul (und die Datenklassen oben) auch
# ohne AppKit importierbar; die ObjC-Klasse wird nur einmal registriert.
try:  # pragma: no cover - umgebungsabhängig
    from Foundation import NSObject

    _APPKIT = True
except Exception:  # pragma: no cover
    NSObject = object  # type: ignore[assignment,misc]
    _APPKIT = False


class _MenuTarget(NSObject):  # type: ignore[misc]
    """Einziges Target aller Menüeinträge; verteilt Klicks anhand des ``tag``."""

    def menuAction_(self, sender) -> None:
        callback = self._callbacks.get(int(sender.tag()))  # type: ignore[attr-defined]
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - ein Menü-Klick darf die App nie killen
            LOGGER.exception("Menü-Callback fehlgeschlagen")


class StatusItem:
    """Wrapper um ein ``NSStatusItem`` mit Icon, Titel und datengetriebenem Menü."""

    def __init__(self) -> None:
        import AppKit

        self._appkit = AppKit
        self._item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        self._target = _MenuTarget.alloc().init()
        self._target._callbacks = {}
        self._items_by_key: dict = {}
        self._images: dict = {}
        self._current_icon: Optional[str] = None
        self._current_title: Optional[str] = None

    # -- Darstellung -----------------------------------------------------

    def set_icon(self, path: Optional[str]) -> None:
        """Setzt das Template-Icon; No-op, wenn der Pfad unverändert ist (kein Flackern)."""
        if not path or path == self._current_icon:
            return

        def _apply() -> None:
            image = self._load_image(path)
            if image is None:
                return
            self._item.button().setImage_(image)
            self._current_icon = path

        run_on_main_sync(_apply)

    def set_title(self, text: str) -> None:
        """Setzt den Text neben dem Icon (Badge/Spinner); No-op bei Gleichstand."""
        if text == self._current_title:
            return

        def _apply() -> None:
            self._item.button().setTitle_(text)
            self._current_title = text

        run_on_main_sync(_apply)

    def _load_image(self, path: str):
        """Lädt (und cached) ein PNG als Template-NSImage in Menüleistengröße."""
        cached = self._images.get(path)
        if cached is not None:
            return cached
        from Foundation import NSMakeSize

        image = self._appkit.NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            LOGGER.warning("Menüleisten-Icon nicht ladbar: %s", path)
            return None
        image.setTemplate_(True)  # System tönt hell/dunkel
        image.setSize_(NSMakeSize(ICON_POINTS, ICON_POINTS))
        self._images[path] = image
        return image

    # -- Menü ------------------------------------------------------------

    def set_menu(self, entries: Sequence) -> None:
        """Ersetzt das Menü vollständig durch die übergebene Spezifikation."""

        def _apply() -> None:
            self._target._callbacks = {}
            self._items_by_key = {}
            menu = self._build_menu(entries, [0])
            self._item.setMenu_(menu)

        run_on_main_sync(_apply)

    def set_item_title(self, key: str, title: str) -> bool:
        """Ändert den Titel eines Eintrags **ohne** Menü-Neuaufbau (Live-Fortschritt).

        :returns: ``True``, wenn ein Eintrag mit diesem ``key`` existierte.
        """
        item = self._items_by_key.get(key)
        if item is None:
            return False

        run_on_main_sync(lambda: item.setTitle_(title))
        return True

    def _build_menu(self, entries: Sequence, counter: list):
        """Baut rekursiv ein ``NSMenu`` aus der Spezifikation.

        :param counter: einelementige Liste als Zähler für eindeutige ``tag``-Werte
            über alle Menü-Ebenen hinweg.
        """
        AppKit = self._appkit
        menu = AppKit.NSMenu.alloc().init()
        # Ohne dies überschreibt AppKit den Aktiv-Zustand anhand der Responder-Chain und
        # würde unsere bewusst deaktivierten Info-Zeilen wieder aktivieren.
        menu.setAutoenablesItems_(False)

        for entry in entries:
            if entry is SEPARATOR:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue

            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                entry.title, None, "")
            if entry.children:
                item.setSubmenu_(self._build_menu(entry.children, counter))
            elif entry.callback is not None:
                tag = counter[0]
                counter[0] += 1
                self._target._callbacks[tag] = entry.callback
                item.setTag_(tag)
                item.setTarget_(self._target)
                item.setAction_("menuAction:")
            item.setEnabled_(entry.is_enabled())
            item.setState_(_STATE_ON if entry.checked else _STATE_OFF)
            if entry.key:
                self._items_by_key[entry.key] = item
            menu.addItem_(item)

        return menu
