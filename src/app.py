"""Menüleisten-App (pyobjc/AppKit): Entrypoint, User-Verwaltung, Re-Auth-Flow, Scheduler.

Diese Schicht hält **keine** Sync-Logik — sie zeigt Status, verwaltet User, stößt Läufe an
und blockiert das UI nicht (Syncs laufen im Hintergrund-Thread).

Die UI besteht ausschließlich aus pyobjc-Bausteinen (:mod:`src.statusitem`,
:mod:`src.timers`, :mod:`src.ui_appkit`, :mod:`src.prefs_window`) — ``rumps`` ist
vollständig abgelöst (Phase 3 der UI-Konvergenz, siehe CLAUDE.md).

Start (Entwicklung, ohne .app-Bundle)::

    .venv/bin/python -m src.app
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import plistlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from . import autostart, menubar_icon, notify, statusitem, timers, ui_appkit
from .auth import keychain, session
from .config.backup import backup_config_to, restore_config_from
from .config.paths import logs_dir
from .config.settings import Settings, load_settings, save_settings
from .config.users import User, UsersStore, UserStatus
from .schedule import due_by_schedule, effective_times
from .statusitem import SEPARATOR, MenuEntry
from .sync import engine

LOGGER = logging.getLogger(__name__)


def user_services_summary(user: User) -> str:
    """Kurzliste der aktiven Dienste eines Users (für Menü und Accounts-Tabelle)."""
    return ", ".join(s for s, on in (("Drive", user.sync_drive), ("Photos", user.sync_photos),
                                     ("+Geteilt", user.sync_shared_photos),
                                     ("Kontakte", user.sync_contacts),
                                     ("Mail", user.sync_mail)) if on) or "—"


class PrefsFacade:
    """Schmale Brücke vom Einstellungs-Fenster zu Engine/Config/Keychain.

    Bewusst **ohne** UI/rumps-Bezug, damit das Fenster (`prefs_window`) beim späteren
    rumps-Ausbau (Phase 3) unverändert weiterläuft. Hält nur eine Referenz auf die laufende
    App für Daten-/Aktions-Delegation und das UI-Refresh.
    """

    def __init__(self, app: "SyncApp") -> None:
        self.app = app

    # Settings
    @property
    def settings(self) -> Settings:
        return self.app.settings

    def save_settings(self) -> None:
        save_settings(self.app.settings)

    # Users
    def list_users(self) -> list:
        return self.app.store.list()

    def get_user(self, apple_id: str):
        return self.app.store.get(apple_id)

    def add_user(self, user: User) -> None:
        self.app.store.add(user)

    def update_user(self, user: User) -> None:
        self.app.store.update(user)

    def remove_user(self, apple_id: str) -> None:
        self.app.store.remove(apple_id)
        keychain.delete_password(apple_id)
        keychain.delete_mail_password(apple_id)

    def set_status(self, apple_id: str, status: UserStatus) -> None:
        self.app.store.set_status(apple_id, status)

    def services_summary(self, user: User) -> str:
        return user_services_summary(user)

    # Credentials / Login
    def get_web_password(self, apple_id: str):
        return keychain.get_password(apple_id)

    def set_web_password(self, apple_id: str, pw: str) -> None:
        keychain.set_password(apple_id, pw)

    def delete_web_password(self, apple_id: str) -> None:
        keychain.delete_password(apple_id)

    def get_mail_password(self, apple_id: str):
        return keychain.get_mail_password(apple_id)

    def set_mail_password(self, apple_id: str, pw: str) -> None:
        keychain.set_mail_password(apple_id, pw)

    def login(self, apple_id: str, pw: str):
        return session.login(apple_id, pw)

    def submit_2fa(self, api, code: str) -> bool:
        return session.submit_2fa_code(api, code)

    def is_online(self) -> bool:
        return engine.is_online()

    def list_drive_folders(self, apple_id: str):
        """Oberste Drive-Ordner für die Ausschluss-Auswahl (oder ``None`` bei Fehler/offline)."""
        pw = keychain.get_password(apple_id)
        if not pw or not engine.is_online():
            return None
        return session.list_drive_top_level(apple_id, pw)

    # Aktionen
    def sync_user(self, apple_id: str) -> None:
        user = self.app.store.get(apple_id)
        if user is not None:
            self.app._spawn(partial(self.app._run_sync_user, user))

    def test_mail(self, host: str, port: int, sender: str, to: str) -> None:
        def _run():
            ok = notify.send_mail(host, int(port), sender, to, "iCloud Sync: Test-E-Mail",
                                  "Test der Fehler-Benachrichtigung über das lokale Mail-Relay.")
            notify.notify("iCloud Sync", "Test-E-Mail eingeliefert." if ok else
                          f"Test-E-Mail fehlgeschlagen ({host}:{port}) – läuft das Relay?")
        self.app._spawn(_run)

    # Autostart
    def autostart_enabled(self) -> bool:
        return autostart.is_enabled()

    def set_autostart(self, enable: bool):
        """True bei Erfolg, oder ein Fehlertext (z. B. im Dev-Modus ohne .app-Bundle)."""
        if enable:
            args = self.app._autostart_program_args()
            if args is None:
                return ("Autostart funktioniert nur im gebauten .app-Bundle, "
                        "nicht im Entwicklungsmodus (python -m src.app).")
            autostart.enable(args)
        else:
            autostart.disable()
        return True

    # Konfiguration sichern/laden
    def export_config(self, directory: str) -> int:
        return backup_config_to(Path(directory) / "icloud-sync-config")

    def import_config(self, directory: str) -> int:
        n = restore_config_from(Path(directory))
        if n:
            self.app.settings = load_settings()
            self.app.store = UsersStore.loaded()
            self.app._reset_stale_running()
        return n

    # UI
    def refresh_ui(self) -> None:
        self.app._rebuild_menu()

# Wie oft der Scheduler prüft, ob ein User „fällig" ist (Sekunden). Der eigentliche
# Sync-Abstand steckt in Settings.sync_interval_hours; dieser Tick ist nur die Polling-Rate,
# die zugleich Missed-Run-Catch-up nach Sleep abdeckt (Vergleich gegen last_run).
TICK_SECONDS = 300

# Menüleisten-Symbole
ICON_OK = "☁︎"
ICON_ATTENTION = "☁︎🔴"

# Symbole je User-Status
STATUS_SYMBOL = {
    UserStatus.IDLE: "•",
    UserStatus.RUNNING: "⟳",
    UserStatus.OK: "✓",
    UserStatus.NEEDS_REAUTH: "🔴",
    UserStatus.ERROR: "⚠️",
}

# Spinner-Frames für die Menüleiste während eines Laufs.
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
# Refresh-Rate der Live-Fortschrittsanzeige (Sekunden).
UI_TICK_SECONDS = 1.0


class SyncApp:
    """Menüleisten-Resident für das iCloud-Multi-User-Backup."""

    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.store: UsersStore = UsersStore.loaded()
        self._reset_stale_running()
        self._sync_lock = threading.Lock()  # verhindert überlappende Sync-Läufe
        self._progress: dict = {}           # apple_id -> {"drive": {...}, "photos": {...}}
        self._live_keys: set = set()        # apple_ids mit Menüeintrag (für Live-Updates)
        self._spin = 0
        self._was_running = False
        self._has_icon = False
        self._started = time.monotonic()  # für die Start-Gnadenfrist (Netz nach Reboot)
        self._prefs = None               # Einstellungs-Fenster-Controller (lazy)
        self._status = statusitem.StatusItem()
        self._setup_menubar_icon()
        self._rebuild_menu()

        # fire_immediately=True bildet das bisherige rumps-Verhalten ab: der erste Tick
        # kommt sofort (baut u. a. das Menü neu auf), die Start-Gnadenfrist verhindert
        # dabei den verfrühten Sync.
        self.timer = timers.RepeatingTimer(TICK_SECONDS, self._tick, fire_immediately=True)
        self.timer.start()
        # Schneller Timer nur für die Live-Fortschrittsanzeige (Spinner + Counts).
        self.ui_timer = timers.RepeatingTimer(UI_TICK_SECONDS, self._ui_tick)
        self.ui_timer.start()
        # Beim Start einmal die Sessions prüfen (im Hintergrund), damit needs_reauth früh sichtbar ist.
        self._spawn(self._refresh_sessions)

    def _reset_stale_running(self) -> None:
        """Persistierten ``running``-Status beim Start auf ``idle`` zurücksetzen.

        Ein frischer Prozess hat ein frisches Lock — es kann beim Start kein Sync aktiv
        sein. Ein in ``users.json`` stehender ``running``-Status stammt also aus einem
        abgebrochenen Lauf (Crash/Quit/Sleep) und würde den User sonst **dauerhaft** vom
        Auto-Sync ausschließen, weil sowohl ``_is_due`` als auch ``_refresh_sessions``
        ``running``-User überspringen.
        """
        for user in self.store.list():
            if user.status == UserStatus.RUNNING:
                LOGGER.info("Setze hängenden 'running'-Status für %s zurück", user.apple_id)
                self.store.set_status(user.apple_id, UserStatus.IDLE)

    # -- Menüaufbau ----------------------------------------------------------

    def _rebuild_menu(self) -> None:
        """Baut das gesamte Menü aus dem aktuellen Store-Zustand neu auf."""
        items: list = []
        self._live_keys = set()
        for user in self.store.list():
            items.append(self._user_menu_entry(user))
            self._live_keys.add(user.apple_id)
        if items:
            items.append(SEPARATOR)
        items.append(MenuEntry("Alle jetzt synchronisieren", self._sync_all))
        pause_label = "Auto-Sync fortsetzen" if self.settings.auto_sync_paused else "Auto-Sync pausieren"
        items.append(MenuEntry(pause_label, self._toggle_auto_sync))
        items.append(MenuEntry("Einstellungen…", self._open_prefs))
        items.append(MenuEntry("Log anzeigen…", self._open_log))
        items.append(SEPARATOR)
        items.append(MenuEntry("Beenden", self._quit))
        self._status.set_menu(items)
        self._update_icon()

    def _user_menu_entry(self, user: User) -> MenuEntry:
        """Schlankes Menü pro Account: nur Schnell-Sync; Konfiguration im Fenster.

        Der ``key`` (Apple-ID) erlaubt dem UI-Tick, den Titel während eines Laufs
        **in place** zu aktualisieren, ohne das Menü neu zu bauen.
        """
        symbol = STATUS_SYMBOL.get(user.status, "•")
        last = f" – {self._fmt_last_run(user.last_run)}" if user.last_run else ""
        excl = f"  ·  Ausschlüsse: {len(user.drive_excludes)}" if user.drive_excludes else ""
        children: list = [
            MenuEntry("Sync jetzt", partial(self._sync_one, user.apple_id)),
            # Info-Zeilen ohne Callback sind automatisch nicht klickbar.
            MenuEntry(f"Dienste: {user_services_summary(user)}  ·  "
                      f"Ziel: {user.dest_base_path or '—'}{excl}"),
        ]
        # Bei Fehler/Re-Auth den letzten Grund als nicht-klickbare Info-Zeile zeigen.
        if user.status in (UserStatus.ERROR, UserStatus.NEEDS_REAUTH) and user.last_error:
            reason = user.last_error if len(user.last_error) <= 80 else user.last_error[:77] + "…"
            children.append(MenuEntry(f"⚠️ Letzter Fehler: {reason}"))
        return MenuEntry(f"{symbol} {user.apple_id}{last}", children=children,
                         key=user.apple_id)

    def _open_prefs(self, _sender=None) -> None:
        """Öffnet das native Einstellungs-Fenster (lazy erzeugt, danach wiederverwendet)."""
        from .prefs_window import PreferencesWindowController

        if self._prefs is None:
            self._prefs = PreferencesWindowController.alloc().initWithFacade_(PrefsFacade(self))
        self._prefs.show()

    def _setup_menubar_icon(self) -> None:
        """Setzt ein echtes Template-Image als Menüleisten-Icon (statt Textglyph)."""
        icons = menubar_icon.ensure_menubar_icons()
        self._icon_active = icons.get("active")
        self._icon_idle = icons.get("idle")
        self._has_icon = bool(self._icon_active and self._icon_idle)
        self._update_icon()

    def _update_icon(self) -> None:
        attention = any(
            u.status in (UserStatus.NEEDS_REAUTH, UserStatus.ERROR) for u in self.store.list()
        )
        if self._has_icon:
            # Gefüllt = Auto-Sync aktiv, umrandet = pausiert; Aufmerksamkeit als Badge daneben.
            # set_icon/set_title sind No-ops bei Gleichstand (kein Flackern).
            self._status.set_icon(self._icon_idle if self.settings.auto_sync_paused
                                  else self._icon_active)
            self._status.set_title(" 🔴" if attention else "")
        else:
            base = ICON_ATTENTION if attention else ICON_OK
            self._status.set_title((base + " ⏸") if self.settings.auto_sync_paused else base)

    @staticmethod
    def _fmt_last_run(iso: Optional[str]) -> str:
        if not iso:
            return "noch nie"
        try:
            dt = datetime.fromisoformat(iso)
            return dt.astimezone().strftime("%d.%m. %H:%M")
        except ValueError:
            return iso

    # -- Log -----------------------------------------------------------------

    def _open_log(self, _sender=None) -> None:
        """Zeigt die Log-Datei im Finder (bzw. öffnet den Logs-Ordner als Fallback)."""
        log_path = logs_dir() / "icloud-sync.log"
        try:
            from AppKit import NSWorkspace

            ws = NSWorkspace.sharedWorkspace()
            if log_path.exists():
                ws.selectFile_inFileViewerRootedAtPath_(str(log_path), "")
            else:
                ws.openFile_(str(logs_dir()))  # Datei noch nicht da -> Ordner öffnen
        except Exception:  # noqa: BLE001
            LOGGER.exception("Log konnte nicht im Finder angezeigt werden")
            ui_appkit.alert("Log", f"Log-Datei:\n{log_path}")

    def _quit(self) -> None:
        import AppKit

        self.timer.stop()
        self.ui_timer.stop()
        AppKit.NSApplication.sharedApplication().terminate_(None)

    # -- Auto-Sync pausieren/fortsetzen --------------------------------------

    def _toggle_auto_sync(self, _sender=None) -> None:
        """Schaltet den Auto-Sync (Scheduler) an/aus; Icon + Menü spiegeln den Zustand."""
        self.settings.auto_sync_paused = not self.settings.auto_sync_paused
        save_settings(self.settings)
        self._update_icon()
        self._rebuild_menu()
        notify.notify("iCloud Sync",
                      "Auto-Sync pausiert (manueller Sync bleibt möglich)."
                      if self.settings.auto_sync_paused else "Auto-Sync fortgesetzt.")

    # -- Autostart -----------------------------------------------------------

    @staticmethod
    def _autostart_program_args() -> Optional[list[str]]:
        """Programmargumente für den LaunchAgent – nur sinnvoll im gebauten Bundle.

        py2app setzt ``sys.frozen``. ACHTUNG: ``sys.executable`` zeigt im Bundle auf
        ``…/Contents/MacOS/python`` (den eingebetteten Interpreter), NICHT auf den
        App-Loader-Stub (``CFBundleExecutable``). Würde der LaunchAgent ``python`` direkt
        starten, käme nur ein nackter Interpreter hoch und die Menüleisten-App erschiene
        nie. Daher den echten Bundle-Executable auflösen (Stub triggert ``__boot__`` →
        ``launcher.py`` → ``main()``, genau wie ein Doppelklick).
        """
        if not getattr(sys, "frozen", False):
            return None
        macos_dir = os.path.dirname(sys.executable)              # …/Contents/MacOS
        bundle = os.path.dirname(os.path.dirname(macos_dir))     # …/iCloud Sync.app
        exe_name = os.path.splitext(os.path.basename(bundle))[0]  # Default: App-Name
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                exe_name = plistlib.load(fh).get("CFBundleExecutable") or exe_name
        except (OSError, plistlib.InvalidFileException):
            pass
        return [os.path.join(macos_dir, exe_name)]

    # -- Sync-Anstoß ---------------------------------------------------------

    def _sync_all(self) -> None:
        self._spawn(self._run_sync_all)

    def _sync_one(self, apple_id: str, _sender=None) -> None:
        user = self.store.get(apple_id)
        if user is not None:
            self._spawn(partial(self._run_sync_user, user))

    def _run_sync_all(self) -> None:
        with self._sync_lock:
            engine.run_all(self.store, self._on_progress)

    def _run_sync_user(self, user: User) -> None:
        with self._sync_lock:
            engine.run_user(user, self.store, self._on_progress)

    # -- Live-Fortschritt ----------------------------------------------------

    def _on_progress(self, apple_id: str, phase: str, counts: dict) -> None:
        """Callback aus dem Sync-Thread: aktuelle Zähler je User/Phase ablegen (nur Daten)."""
        self._progress.setdefault(apple_id, {})[phase] = counts

    def _ui_tick(self) -> None:
        """Schneller UI-Refresh: Spinner + Live-Counts, solange ein User läuft."""
        running = [u for u in self.store.list() if u.status == UserStatus.RUNNING]
        if running:
            self._spin = (self._spin + 1) % len(SPINNER)
            frame = SPINNER[self._spin]
            self._status.set_title(f" {frame}" if self._has_icon else f"{ICON_OK} {frame}")
            for u in running:
                # Titel in place ändern statt Menü neu bauen (sekündlich, ggf. bei
                # geöffnetem Menü) — siehe statusitem.set_item_title.
                self._status.set_item_title(u.apple_id, self._running_label(u.apple_id))
            self._was_running = True
        elif self._was_running:
            # Lauf gerade beendet -> Endzustand sauber rendern.
            self._was_running = False
            self._progress.clear()
            self._rebuild_menu()

    def _running_label(self, apple_id: str) -> str:
        p = self._progress.get(apple_id, {})
        parts = []
        d = p.get("drive")
        if d:
            parts.append(f"Drive {d.get('downloaded', 0)}↓")
        ph = p.get("photos")
        if ph:
            parts.append(f"Photos {ph.get('downloaded', 0)}↓ / {ph.get('seen', 0)} gepr.")
        ct = p.get("contacts")
        if ct:
            parts.append(f"Kontakte {ct.get('downloaded', 0)}↓")
        ml = p.get("mail")
        if ml:
            parts.append(f"Mail {ml.get('downloaded', 0)}↓ ({ml.get('folders', 0)} Ordner)")
        detail = "  ".join(parts) if parts else "startet…"
        return f"⟳ {apple_id} – {detail}"

    # -- Scheduler-Tick ------------------------------------------------------

    def _in_startup_grace(self) -> bool:
        """True während der Gnadenfrist nach App-Start (Netz/DNS nach Reboot noch nicht oben)."""
        return (time.monotonic() - self._started) < self.settings.startup_delay_seconds

    def _tick(self) -> None:
        """Periodischer Check: fällige User syncen (mit Catch-up) und UI auffrischen.

        Der Timer feuert sofort beim Start (``fire_immediately``); in der Start-Gnadenfrist
        daher noch nicht synchronisieren (sonst läuft der erste Versuch nach einem Reboot
        ins tote Netz).
        """
        if self._in_startup_grace():
            self._rebuild_menu()
            return
        due = [u for u in self.store.list() if self._is_due(u)]
        if due and not self._sync_lock.locked():
            self._spawn(partial(self._run_due, due))
        self._rebuild_menu()  # spiegelt Ergebnisse des letzten Zyklus

    def _run_due(self, users: list[User]) -> None:
        with self._sync_lock:
            for user in users:
                engine.run_user(user, self.store, self._on_progress)

    def _is_due(self, user: User) -> bool:
        """True, wenn ein Auto-Sync für den User fällig ist.

        Reihenfolge: **eigener** Uhrzeit-Plan des Users (``user.sync_times``) schlägt den
        globalen Plan (``settings.sync_times``); ohne beides gilt das Stunden-Intervall.
        Alle Varianten decken Missed-Run-Catch-up ab: war der Mac im Sleep, ist last_run
        alt -> sofort fällig.
        Re-Auth-/Fehler-User werden nicht automatisch gesynct (brauchen User-Eingriff).
        Bei pausiertem Auto-Sync ist niemand fällig (manueller „Sync jetzt" bleibt möglich).
        """
        if self.settings.auto_sync_paused:
            return False
        if user.status in (UserStatus.NEEDS_REAUTH, UserStatus.RUNNING):
            return False
        times = effective_times(user.sync_times, self.settings.sync_times)
        if times:
            return due_by_schedule(times, user.last_run, datetime.now().astimezone())
        if not user.last_run:
            return True
        try:
            last = datetime.fromisoformat(user.last_run)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last >= timedelta(hours=self.settings.sync_interval_hours)

    # -- Session-Refresh -----------------------------------------------------

    def _refresh_sessions(self) -> None:
        """Prüft je User die Session-Gültigkeit und aktualisiert den Status (Hintergrund).

        Wartet die Start-Gnadenfrist ab und überspringt, wenn iCloud (noch) nicht erreichbar
        ist — sonst würde direkt nach einem Reboot fälschlich `needs_reauth`/`error` gesetzt.
        """
        delay = self.settings.startup_delay_seconds - (time.monotonic() - self._started)
        if delay > 0:
            time.sleep(delay)
        if not engine.is_online():
            LOGGER.info("Session-Check übersprungen: iCloud nicht erreichbar.")
            return
        for user in self.store.list():
            if user.status == UserStatus.RUNNING:
                continue
            password = keychain.get_password(user.apple_id)
            status = session.check_session(user.apple_id, password)
            self.store.set_status(user.apple_id, status)
            if status == UserStatus.NEEDS_REAUTH:
                notify.notify("iCloud Sync – Re-Auth nötig",
                              f"{user.apple_id}: bitte erneut anmelden.")

    # -- Util ----------------------------------------------------------------

    @staticmethod
    def _spawn(fn) -> None:
        """Startet ``fn`` in einem Daemon-Thread; fängt+loggt unerwartete Fehler.

        Ohne diesen Wrapper würde eine Exception im Hintergrund-Thread ihn lautlos beenden
        (z. B. außerhalb der engine-internen try/except, beim Lock o. Ä.).
        """
        def _runner():
            try:
                fn()
            except Exception:  # noqa: BLE001 - Thread darf nie lautlos sterben
                LOGGER.exception("Hintergrund-Task abgebrochen: %r", getattr(fn, "__name__", fn))

        threading.Thread(target=_runner, daemon=True).start()


def _setup_logging() -> None:
    """Root-Logger auf eine rotierende Datei (logs/icloud-sync.log) + stderr konfigurieren.

    In der ``.app`` (Menüleisten-App ohne Terminal) ist stderr verloren — die Datei ist die
    einzige verlässliche Diagnosequelle. Idempotent. Unbehandelte Exceptions (Main- und
    Hintergrund-Threads) werden zusätzlich geloggt, statt spurlos zu verschwinden.
    """
    root = logging.getLogger()
    if any(getattr(h, "_icloud_sync", False) for h in root.handlers):
        return  # schon konfiguriert
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        fh = logging.handlers.RotatingFileHandler(
            logs_dir() / "icloud-sync.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._icloud_sync = True  # type: ignore[attr-defined]
        root.addHandler(fh)
    except OSError:
        pass  # Datei-Logging best-effort (z. B. Ziel nicht schreibbar)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh._icloud_sync = True  # type: ignore[attr-defined]
    root.addHandler(sh)

    def _log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("uncaught").error("Unbehandelte Exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: logging.getLogger("uncaught").error(
            "Unbehandelte Thread-Exception in %s", a.thread,
            exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def main() -> None:
    """Startet die Menüleisten-App und übergibt an den AppKit-Runloop."""
    import AppKit

    _setup_logging()
    LOGGER.info("iCloud Sync startet (Log: %s)", logs_dir() / "icloud-sync.log")

    ns_app = AppKit.NSApplication.sharedApplication()
    # „Accessory": Menüleisten-Resident ohne Dock-Icon und ohne Hauptmenü. Entspricht
    # LSUIElement aus der Info.plist, gilt aber auch im Dev-Modus ohne Bundle.
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    app = SyncApp()  # Referenz halten: Status-Item und Timer hängen daran
    LOGGER.info("Menüleisten-Item aktiv, Runloop startet.")
    ns_app.run()
    LOGGER.info("Runloop beendet (%r).", app.__class__.__name__)


if __name__ == "__main__":
    main()
