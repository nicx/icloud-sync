"""rumps-Menüleisten-App: Entrypoint, User-Verwaltung, Re-Auth-Flow, Scheduler.

Diese Schicht hält **keine** Sync-Logik — sie zeigt Status, verwaltet User, stößt Läufe an
und blockiert das UI nicht (Syncs laufen im Hintergrund-Thread).

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

import rumps

from . import autostart, menubar_icon, notify
from .auth import keychain, session
from .config.paths import logs_dir
from .config.settings import Settings, load_settings, save_settings
from .config.users import User, UsersStore, UserStatus
from .schedule import due_by_schedule, parse_schedule
from .sync import engine

LOGGER = logging.getLogger(__name__)

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


class SyncApp(rumps.App):
    """Menüleisten-Resident für das iCloud-Multi-User-Backup."""

    def __init__(self) -> None:
        # quit_button=None: wir fügen "Beenden" selbst hinzu, da _rebuild_menu das Menü
        # komplett neu aufbaut und rumps' Auto-Quit-Button dabei sonst verloren ginge.
        super().__init__("iCloud Sync", title=ICON_OK, quit_button=None)
        self.settings: Settings = load_settings()
        self.store: UsersStore = UsersStore.loaded()
        self._reset_stale_running()
        self._sync_lock = threading.Lock()  # verhindert überlappende Sync-Läufe
        self._progress: dict = {}           # apple_id -> {"drive": {...}, "photos": {...}}
        self._user_items: dict = {}         # apple_id -> rumps.MenuItem (für Live-Updates)
        self._spin = 0
        self._was_running = False
        self._has_icon = False
        self._drive_folders: dict = {}   # apple_id -> [Top-Level-Drive-Ordner] (Laufzeit-Cache)
        self._menu_dirty = False         # vom Hintergrund gesetzt -> _ui_tick baut das Menü neu
        self._started = time.monotonic()  # für die Start-Gnadenfrist (Netz nach Reboot)
        self._setup_menubar_icon()
        self._rebuild_menu()

        self.timer = rumps.Timer(self._tick, TICK_SECONDS)
        self.timer.start()
        # Schneller Timer nur für die Live-Fortschrittsanzeige (Spinner + Counts).
        self.ui_timer = rumps.Timer(self._ui_tick, UI_TICK_SECONDS)
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
        self.menu.clear()
        self._user_items = {}
        items: list = []
        for user in self.store.list():
            items.append(self._user_menu_item(user))
        if items:
            items.append(rumps.separator)
        items.append(rumps.MenuItem("Alle jetzt synchronisieren", callback=self._sync_all))
        pause_label = "Auto-Sync fortsetzen" if self.settings.auto_sync_paused else "Auto-Sync pausieren"
        items.append(rumps.MenuItem(pause_label, callback=self._toggle_auto_sync))
        items.append(rumps.MenuItem("User hinzufügen…", callback=self._add_user))
        items.append(rumps.MenuItem("Log anzeigen…", callback=self._open_log))
        cfg = rumps.MenuItem("Konfiguration …")
        cfg.add(rumps.MenuItem("Exportieren…", callback=self._export_config))
        cfg.add(rumps.MenuItem("Importieren…", callback=self._import_config))
        items.append(cfg)
        items.append(self._error_email_menu())
        items.append(rumps.MenuItem("Einstellungen…", callback=self._open_settings))
        items.append(rumps.MenuItem("Sync-Zeiten…", callback=self._set_sync_times))
        autostart_item = rumps.MenuItem("Beim Login starten", callback=self._toggle_autostart)
        autostart_item.state = 1 if autostart.is_enabled() else 0
        items.append(autostart_item)
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Beenden", callback=self._quit))
        self.menu = items
        self._update_icon()

    def _user_menu_item(self, user: User) -> rumps.MenuItem:
        symbol = STATUS_SYMBOL.get(user.status, "•")
        last = f" – {self._fmt_last_run(user.last_run)}" if user.last_run else ""
        parent = rumps.MenuItem(f"{symbol} {user.apple_id}{last}")
        parent.add(rumps.MenuItem("Sync jetzt", callback=partial(self._sync_one, user.apple_id)))
        parent.add(rumps.MenuItem("Re-Auth…", callback=partial(self._reauth, user.apple_id)))
        parent.add(rumps.MenuItem("Mail-App-Passwort setzen…",
                                  callback=partial(self._set_mail_password, user.apple_id)))
        parent.add(rumps.MenuItem("Zielordner ändern…", callback=partial(self._change_dest, user.apple_id)))
        if user.sync_photos:  # geteilte Mediathek ist ein Add-on zu den persönlichen Photos
            shared = rumps.MenuItem("Geteilte Mediathek sichern",
                                    callback=partial(self._toggle_shared_photos, user.apple_id))
            shared.state = 1 if user.sync_shared_photos else 0
            parent.add(shared)
        if user.sync_drive:
            parent.add(self._drive_excludes_menu(user))
        contacts_item = rumps.MenuItem("Kontakte sichern", callback=partial(self._toggle_contacts, user.apple_id))
        contacts_item.state = 1 if user.sync_contacts else 0
        parent.add(contacts_item)
        services = ", ".join(s for s, on in (("Drive", user.sync_drive), ("Photos", user.sync_photos),
                                             ("+Geteilt", user.sync_shared_photos),
                                             ("Kontakte", user.sync_contacts),
                                             ("Mail", user.sync_mail)) if on) or "—"
        excl = f"  ·  Ausschlüsse: {len(user.drive_excludes)}" if user.drive_excludes else ""
        info = rumps.MenuItem(f"Dienste: {services}  ·  Ziel: {user.dest_base_path or '—'}{excl}")
        info.set_callback(None)  # nur Info, nicht klickbar
        parent.add(info)
        # Bei Fehler/Re-Auth den letzten Grund als nicht-klickbare Info-Zeile zeigen.
        if user.status in (UserStatus.ERROR, UserStatus.NEEDS_REAUTH) and user.last_error:
            reason = user.last_error if len(user.last_error) <= 80 else user.last_error[:77] + "…"
            err = rumps.MenuItem(f"⚠️ Letzter Fehler: {reason}")
            err.set_callback(None)
            parent.add(err)
        parent.add(rumps.separator)
        parent.add(rumps.MenuItem("Entfernen…", callback=partial(self._remove_user, user.apple_id)))
        self._user_items[user.apple_id] = parent
        return parent

    def _setup_menubar_icon(self) -> None:
        """Setzt ein echtes Template-Image als Menüleisten-Icon (statt Textglyph)."""
        icons = menubar_icon.ensure_menubar_icons()
        self._icon_active = icons.get("active")
        self._icon_idle = icons.get("idle")
        self._has_icon = bool(self._icon_active and self._icon_idle)
        self._current_icon: Optional[str] = None
        if self._has_icon:
            self.template = True  # System tönt hell/dunkel und skaliert auf Menüleistenhöhe
        self._update_icon()

    def _update_icon(self) -> None:
        attention = any(
            u.status in (UserStatus.NEEDS_REAUTH, UserStatus.ERROR) for u in self.store.list()
        )
        if self._has_icon:
            # Gefüllt = Auto-Sync aktiv, umrandet = pausiert; Aufmerksamkeit als Badge daneben.
            desired = self._icon_idle if self.settings.auto_sync_paused else self._icon_active
            if desired != self._current_icon:
                self.icon = desired
                self._current_icon = desired
            self.title = " 🔴" if attention else ""
        else:
            base = ICON_ATTENTION if attention else ICON_OK
            self.title = (base + " ⏸") if self.settings.auto_sync_paused else base

    @staticmethod
    def _fmt_last_run(iso: Optional[str]) -> str:
        if not iso:
            return "noch nie"
        try:
            dt = datetime.fromisoformat(iso)
            return dt.astimezone().strftime("%d.%m. %H:%M")
        except ValueError:
            return iso

    # -- Kleine Dialog-Helfer ------------------------------------------------

    def _ask_text(self, message: str, title: str, default: str = "", secure: bool = False) -> Optional[str]:
        win = rumps.Window(
            message=message,
            title=title,
            default_text=default,
            ok="OK",
            cancel="Abbrechen",
            dimensions=(320, 24),
            secure=secure,
        )
        resp = win.run()
        if resp.clicked == 0:
            return None
        return resp.text.strip()

    def _ask_yes_no(self, message: str, title: str) -> bool:
        # rumps.Window: OK -> clicked==1 (Ja), Abbrechen -> 0 (Nein)
        win = rumps.Window(message=message, title=title, ok="Ja", cancel="Nein", dimensions=(1, 1))
        return win.run().clicked == 1

    def _ask_directory(self, message: str, default: Optional[str] = None) -> Optional[str]:
        """Komfortable Ordnerauswahl: nativer Finder-Dialog, mit Fallback auf Texteingabe.

        Gibt den gewählten Pfad zurück oder None bei Abbruch.
        """
        try:
            return self._native_directory_dialog(message, default)
        except Exception:  # noqa: BLE001 - im Zweifel nie blockieren
            LOGGER.exception("NSOpenPanel nicht verfügbar, Fallback auf Texteingabe")
            text = self._ask_text(message, "Ordner wählen", default=default or "")
            return text or None

    @staticmethod
    def _native_directory_dialog(message: str, default_path: Optional[str]) -> Optional[str]:
        """Nativer Finder-Ordnerdialog (NSOpenPanel). Muss auf dem Main-Thread laufen."""
        from AppKit import NSApp, NSOpenPanel
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
        # Menüleisten-App (LSUIElement) in den Vordergrund holen, sonst öffnet der Dialog dahinter.
        NSApp.activateIgnoringOtherApps_(True)
        if panel.runModal() != 1:  # 1 == NSModalResponseOK
            return None
        urls = panel.URLs()
        return urls[0].path() if urls else None

    # -- User hinzufügen -----------------------------------------------------

    def _add_user(self, _sender) -> None:
        apple_id = self._ask_text("Apple-ID (E-Mail):", "User hinzufügen")
        if not apple_id:
            return
        if self.store.get(apple_id) is not None:
            rumps.alert("Bereits vorhanden", f"{apple_id} ist schon konfiguriert.")
            return
        dest = self._ask_directory(f"Ziel-Ordner für das Backup von {apple_id} wählen "
                                   "(z. B. auf dem UNAS-Volume):")
        if not dest:
            return
        sync_drive = self._ask_yes_no("iCloud Drive sichern?", "User hinzufügen")
        sync_photos = self._ask_yes_no("iCloud Photos sichern?", "User hinzufügen")
        sync_contacts = self._ask_yes_no("iCloud Kontakte sichern?", "User hinzufügen")
        sync_mail = self._ask_yes_no("iCloud Mail sichern? (braucht ein app-spezifisches Passwort)",
                                     "User hinzufügen")
        if not (sync_drive or sync_photos or sync_contacts or sync_mail):
            rumps.alert("Nichts ausgewählt", "Es wurde kein Dienst zum Sichern gewählt.")
            return

        user = User(apple_id=apple_id, sync_drive=sync_drive, sync_photos=sync_photos,
                    sync_contacts=sync_contacts, sync_mail=sync_mail, dest_base_path=dest,
                    status=UserStatus.IDLE)
        status = UserStatus.OK

        # Web-Passwort + Login nur, wenn Drive/Photos/Contacts gewünscht.
        if sync_drive or sync_photos or sync_contacts:
            password = self._ask_text("Apple-ID-Passwort (für Drive/Photos/Kontakte; nur im macOS-Keychain):",
                                       "User hinzufügen", secure=True)
            if not password:
                rumps.alert("Kein Passwort", "Ohne Apple-ID-Passwort kein Drive/Photos/Kontakte-Sync.")
                return
            keychain.set_password(apple_id, password)
            result = session.login(apple_id, password)
            if result.error:
                keychain.delete_password(apple_id)
                rumps.alert("Login fehlgeschlagen", result.error)
                return
            if result.needs_2fa and not self._complete_2fa(result.api, apple_id):
                status = UserStatus.NEEDS_REAUTH

        # Mail: app-spezifisches Passwort.
        if sync_mail:
            if not self._prompt_mail_password(apple_id):
                user.sync_mail = False
                rumps.alert("Mail übersprungen",
                            "Ohne app-spezifisches Passwort wird Mail nicht gesichert. "
                            "Du kannst es spaeter ueber 'Mail-App-Passwort setzen...' nachholen.")

        user.status = status
        self.store.add(user)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"User {apple_id} hinzugefügt ({user.status.value}).")

    def _prompt_mail_password(self, apple_id: str) -> bool:
        """Fragt das app-spezifische Mail-Passwort ab und legt es im Keychain ab. True bei Erfolg."""
        app_pw = self._ask_text(
            "App-spezifisches Passwort für iCloud Mail.\n"
            "Auf appleid.apple.com → Anmeldung & Sicherheit → App-spezifische Passwörter erzeugen.",
            "iCloud Mail – App-Passwort", secure=True)
        if not app_pw:
            return False
        keychain.set_mail_password(apple_id, app_pw)
        return True

    def _complete_2fa(self, api, apple_id: str) -> bool:
        """Fragt den 2FA-Code ab und vertraut der Session. True bei Erfolg."""
        if api is None:
            rumps.alert("2FA nötig",
                        "Eine 2FA-Bestätigung ist erforderlich. Bitte erneut über Re-Auth versuchen.")
            return False
        code = self._ask_text("6-stelliger Code von einem vertrauenswürdigen Apple-Gerät:",
                              "Zwei-Faktor-Authentifizierung")
        if not code:
            return False
        if session.submit_2fa_code(api, code):
            return True
        rumps.alert("Code abgelehnt", "Der 2FA-Code wurde nicht akzeptiert.")
        return False

    # -- Re-Auth -------------------------------------------------------------

    def _reauth(self, apple_id: str, _sender=None) -> None:
        password = keychain.get_password(apple_id)
        if not password:
            rumps.alert("Kein Passwort", f"Für {apple_id} ist kein Passwort im Keychain hinterlegt.")
            return
        result = session.login(apple_id, password)
        if result.error:
            rumps.alert("Fehler", result.error)
            self.store.set_status(apple_id, UserStatus.ERROR)
            self._rebuild_menu()
            return
        if result.needs_2fa:
            ok = self._complete_2fa(result.api, apple_id)
            self.store.set_status(apple_id, UserStatus.OK if ok else UserStatus.NEEDS_REAUTH)
        else:
            self.store.set_status(apple_id, UserStatus.OK)
        self._rebuild_menu()

    def _set_mail_password(self, apple_id: str, _sender=None) -> None:
        """Mail-App-Passwort setzen/aktualisieren und Mail für den User aktivieren."""
        user = self.store.get(apple_id)
        if user is None:
            return
        if not self._prompt_mail_password(apple_id):
            return
        if not user.sync_mail:
            user.sync_mail = True
            self.store.update(user)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Mail-App-Passwort für {apple_id} gespeichert.")

    def _change_dest(self, apple_id: str, _sender=None) -> None:
        user = self.store.get(apple_id)
        if user is None:
            return
        new_dest = self._ask_directory(f"Neuen Ziel-Ordner für {apple_id} wählen:",
                                       default=user.dest_base_path or None)
        if not new_dest:
            return
        user.dest_base_path = new_dest
        self.store.update(user)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Zielordner für {apple_id} geändert.")

    def _toggle_shared_photos(self, apple_id: str, _sender=None) -> None:
        """Schaltet die Sicherung der geteilten Mediathek (-> SharedPhotos/) für den User um."""
        user = self.store.get(apple_id)
        if user is None:
            return
        user.sync_shared_photos = not user.sync_shared_photos
        self.store.update(user)
        self._rebuild_menu()
        notify.notify("iCloud Sync",
                      f"Geteilte Mediathek für {apple_id}: "
                      f"{'wird gesichert (SharedPhotos/)' if user.sync_shared_photos else 'aus'}.")

    def _toggle_contacts(self, apple_id: str, _sender=None) -> None:
        """Schaltet die Kontakte-Sicherung (-> Contacts/) für den User um."""
        user = self.store.get(apple_id)
        if user is None:
            return
        user.sync_contacts = not user.sync_contacts
        self.store.update(user)
        self._rebuild_menu()
        notify.notify("iCloud Sync",
                      f"Kontakte für {apple_id}: {'werden gesichert' if user.sync_contacts else 'aus'}.")

    # -- Drive-Ausschlüsse ---------------------------------------------------

    def _drive_excludes_menu(self, user: User) -> rumps.MenuItem:
        """Untermenü: oberste Drive-Ordner live laden und per Häkchen aus-/abwählen."""
        parent = rumps.MenuItem("Drive-Ausschlüsse")
        parent.add(rumps.MenuItem("Ordnerliste aktualisieren",
                                  callback=partial(self._load_drive_folders, user.apple_id)))
        parent.add(rumps.separator)
        excluded = set(user.drive_excludes)
        names = sorted(set(self._drive_folders.get(user.apple_id, [])) | excluded)
        if not names:
            hint = rumps.MenuItem("(noch nicht geladen — ‚Ordnerliste aktualisieren')")
            hint.set_callback(None)
            parent.add(hint)
        else:
            for name in names:
                it = rumps.MenuItem(name, callback=partial(self._toggle_drive_exclude, user.apple_id, name))
                it.state = 1 if name in excluded else 0
                parent.add(it)
        return parent

    def _load_drive_folders(self, apple_id: str, _sender=None) -> None:
        notify.notify("iCloud Sync", f"Lade Drive-Ordner für {apple_id} …")
        self._spawn(partial(self._fetch_drive_folders, apple_id))

    def _fetch_drive_folders(self, apple_id: str) -> None:
        """Hintergrund: oberste Drive-Ordner abrufen, cachen, Menü-Neuaufbau anstoßen (Main-Thread)."""
        pw = keychain.get_password(apple_id)
        if not pw:
            notify.notify("iCloud Sync", f"{apple_id}: kein Apple-ID-Passwort im Keychain.")
            return
        if not engine.is_online():
            notify.notify("iCloud Sync", "iCloud nicht erreichbar — später erneut versuchen.")
            return
        names = session.list_drive_top_level(apple_id, pw)
        if names is None:
            notify.notify("iCloud Sync", f"{apple_id}: Drive-Ordner nicht abrufbar (ggf. Re-Auth).")
            return
        self._drive_folders[apple_id] = names
        self._menu_dirty = True  # _ui_tick (Main-Thread) baut das Menü neu
        notify.notify("iCloud Sync", f"{len(names)} Drive-Ordner geladen — jetzt auswählbar.")

    def _toggle_drive_exclude(self, apple_id: str, name: str, _sender=None) -> None:
        user = self.store.get(apple_id)
        if user is None:
            return
        ex = list(user.drive_excludes)
        if name in ex:
            ex.remove(name)
        else:
            ex.append(name)
        user.drive_excludes = ex
        self.store.update(user)
        self._rebuild_menu()
        state = "ausgeschlossen" if name in ex else "wieder dabei"
        notify.notify("iCloud Sync", f"Drive-Ordner ‚{name}': {state} ({apple_id}).")

    def _remove_user(self, apple_id: str, _sender=None) -> None:
        if not self._ask_yes_no(f"{apple_id} entfernen? (Backup-Dateien bleiben erhalten)", "Entfernen"):
            return
        self.store.remove(apple_id)
        keychain.delete_password(apple_id)
        keychain.delete_mail_password(apple_id)
        self._rebuild_menu()

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
            rumps.alert("Log", f"Log-Datei:\n{log_path}")

    # -- Konfiguration sichern/laden -----------------------------------------

    def _export_config(self, _sender=None) -> None:
        """Exportiert settings.json + users.json (ohne Passwörter) in einen gewählten Ordner."""
        from .config.backup import backup_config_to

        d = self._ask_directory("Ordner für den Konfigurations-Export wählen:")
        if not d:
            return
        target = Path(d) / "icloud-sync-config"
        n = backup_config_to(target)
        if n:
            notify.notify("iCloud Sync", f"Konfiguration exportiert ({n} Dateien) → {target}")
        else:
            rumps.alert("Export fehlgeschlagen",
                        "Es konnten keine Konfigurationsdateien geschrieben werden.")

    def _import_config(self, _sender=None) -> None:
        """Importiert settings.json + users.json aus einem Ordner (überschreibt die aktuellen)."""
        from .config.backup import restore_config_from

        if not self._ask_yes_no(
                "Konfiguration importieren? Aktuelle settings.json/users.json werden "
                "überschrieben. Passwörter (Keychain) müssen ggf. neu gesetzt werden.",
                "Konfiguration importieren"):
            return
        d = self._ask_directory("Ordner mit der gesicherten Konfiguration wählen:")
        if not d:
            return
        n = restore_config_from(Path(d))
        if not n:
            rumps.alert("Import", "Im gewählten Ordner wurde keine settings.json/users.json gefunden.")
            return
        # Frisch laden und UI neu aufbauen.
        self.settings = load_settings()
        self.store = UsersStore.loaded()
        self._reset_stale_running()
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Konfiguration importiert ({n} Dateien).")

    # -- Fehler-E-Mail -------------------------------------------------------

    def _error_email_menu(self) -> rumps.MenuItem:
        """Untermenü: Fehler-Benachrichtigung per E-Mail (über lokales Relay)."""
        parent = rumps.MenuItem("Fehler-E-Mail …")
        toggle = rumps.MenuItem("Aktiv", callback=self._toggle_error_email)
        toggle.state = 1 if self.settings.error_email_enabled else 0
        parent.add(toggle)
        parent.add(rumps.MenuItem("Empfänger…", callback=self._set_error_email_to))
        parent.add(rumps.MenuItem("Relay-Host…", callback=self._set_smtp_host))
        parent.add(rumps.MenuItem("Relay-Port…", callback=self._set_smtp_port))
        parent.add(rumps.MenuItem("Test-E-Mail senden", callback=self._send_test_email))
        info = rumps.MenuItem(f"An: {self.settings.error_email_to or '—'}  ·  "
                              f"Relay: {self.settings.smtp_host}:{self.settings.smtp_port}")
        info.set_callback(None)
        parent.add(info)
        return parent

    def _toggle_error_email(self, sender) -> None:
        if not self.settings.error_email_enabled and not self.settings.error_email_to:
            rumps.alert("Empfänger fehlt", "Bitte zuerst einen Empfänger unter 'Empfänger…' setzen.")
            return
        self.settings.error_email_enabled = not self.settings.error_email_enabled
        save_settings(self.settings)
        sender.state = 1 if self.settings.error_email_enabled else 0
        self._rebuild_menu()

    def _set_error_email_to(self, _sender=None) -> None:
        val = self._ask_text("E-Mail-Adresse für Fehlermeldungen (leer = aus):",
                             "Fehler-E-Mail", default=self.settings.error_email_to)
        if val is None:
            return
        self.settings.error_email_to = val
        if not val:
            self.settings.error_email_enabled = False
        elif not self.settings.error_email_enabled:
            self.settings.error_email_enabled = True  # Adresse gesetzt -> direkt aktiv
        save_settings(self.settings)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Fehler-E-Mail: {'an ' + val if val else 'deaktiviert'}.")

    def _set_smtp_host(self, _sender=None) -> None:
        val = self._ask_text("Mail-Relay Host/IP (z. B. 127.0.0.1):",
                             "Fehler-E-Mail – Relay", default=self.settings.smtp_host)
        if not val:
            return
        self.settings.smtp_host = val
        save_settings(self.settings)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Mail-Relay-Host: {val}")

    def _set_smtp_port(self, _sender=None) -> None:
        val = self._ask_text("Mail-Relay Port (z. B. 2525):",
                             "Fehler-E-Mail – Relay", default=str(self.settings.smtp_port))
        if val is None:
            return
        try:
            port = int(val)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            rumps.alert("Ungültig", "Bitte einen Port zwischen 1 und 65535 eingeben.")
            return
        self.settings.smtp_port = port
        save_settings(self.settings)
        self._rebuild_menu()
        notify.notify("iCloud Sync", f"Mail-Relay-Port: {port}")

    def _send_test_email(self, _sender=None) -> None:
        to = self.settings.error_email_to
        if not to:
            rumps.alert("Empfänger fehlt", "Bitte zuerst einen Empfänger unter 'Empfänger…' setzen.")
            return
        sender = self.settings.error_email_from or to

        def _run():
            ok = notify.send_mail(self.settings.smtp_host, int(self.settings.smtp_port), sender, to,
                                  "iCloud Sync: Test-E-Mail",
                                  "Test der Fehler-Benachrichtigung über das lokale Mail-Relay.\n"
                                  "Wenn diese Mail ankommt, funktioniert die Zustellung.")
            notify.notify("iCloud Sync",
                          "Test-E-Mail eingeliefert." if ok else
                          f"Test-E-Mail fehlgeschlagen ({self.settings.smtp_host}:{self.settings.smtp_port}) – läuft das Relay?")

        self._spawn(_run)

    # -- Einstellungen -------------------------------------------------------

    def _open_settings(self, _sender) -> None:
        val = self._ask_text("Sync-Intervall in Stunden:", "Einstellungen",
                             default=str(self.settings.sync_interval_hours))
        if val is None:
            return
        try:
            hours = max(1, int(val))
        except ValueError:
            rumps.alert("Ungültig", "Bitte eine ganze Zahl (Stunden) eingeben.")
            return
        self.settings.sync_interval_hours = hours
        save_settings(self.settings)
        notify.notify("iCloud Sync", f"Sync-Intervall: alle {hours} h.")

    def _set_sync_times(self, _sender) -> None:
        val = self._ask_text(
            "Feste Sync-Uhrzeiten (HH:MM, durch Komma getrennt; leer = Stunden-Intervall):",
            "Sync-Zeiten", default=", ".join(self.settings.sync_times))
        if val is None:
            return
        try:
            times = parse_schedule(val)
        except ValueError:
            rumps.alert("Ungültig", "Bitte Uhrzeiten als HH:MM angeben, z. B. 07:30, 19:30.")
            return
        self.settings.sync_times = times
        save_settings(self.settings)
        self._rebuild_menu()
        if times:
            notify.notify("iCloud Sync", "Sync-Zeiten: " + ", ".join(times))
        else:
            notify.notify("iCloud Sync", f"Feste Zeiten aus – Intervall: alle {self.settings.sync_interval_hours} h.")

    def _quit(self, _sender) -> None:
        rumps.quit_application()

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

    def _toggle_autostart(self, sender) -> None:
        if autostart.is_enabled():
            autostart.disable()
        else:
            args = self._autostart_program_args()
            if args is None:
                rumps.alert(
                    "Autostart nur im .app-Bundle",
                    "Der Login-Autostart funktioniert nur fuer die gebaute App-Bundle-Version. "
                    "Im Entwicklungsmodus (python -m src.app) ist er nicht verfuegbar.",
                )
                return
            autostart.enable(args)
        sender.state = 1 if autostart.is_enabled() else 0

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

    def _sync_all(self, _sender) -> None:
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

    def _ui_tick(self, _timer) -> None:
        """Schneller UI-Refresh: Spinner + Live-Counts, solange ein User läuft."""
        if self._menu_dirty:  # vom Hintergrund (z. B. Drive-Ordner-Fetch) angefordert
            self._menu_dirty = False
            self._rebuild_menu()
        running = [u for u in self.store.list() if u.status == UserStatus.RUNNING]
        if running:
            self._spin = (self._spin + 1) % len(SPINNER)
            frame = SPINNER[self._spin]
            self.title = f" {frame}" if self._has_icon else f"{ICON_OK} {frame}"
            for u in running:
                item = self._user_items.get(u.apple_id)
                if item is not None:
                    item.title = self._running_label(u.apple_id)
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

    def _tick(self, _timer) -> None:
        """Periodischer Check: fällige User syncen (mit Catch-up) und UI auffrischen.

        rumps-Timer feuern sofort beim Start; in der Start-Gnadenfrist daher noch nicht
        synchronisieren (sonst läuft der erste Versuch nach einem Reboot ins tote Netz).
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

        Sind feste Uhrzeiten gesetzt (``settings.sync_times``), gilt der Uhrzeit-Plan
        (lokale Wandzeit); sonst das Stunden-Intervall. Beides deckt Missed-Run-Catch-up
        ab: war der Mac im Sleep, ist last_run alt -> sofort fällig.
        Re-Auth-/Fehler-User werden nicht automatisch gesynct (brauchen User-Eingriff).
        Bei pausiertem Auto-Sync ist niemand fällig (manueller „Sync jetzt" bleibt möglich).
        """
        if self.settings.auto_sync_paused:
            return False
        if user.status in (UserStatus.NEEDS_REAUTH, UserStatus.RUNNING):
            return False
        if self.settings.sync_times:
            return due_by_schedule(self.settings.sync_times, user.last_run,
                                   datetime.now().astimezone())
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
    _setup_logging()
    LOGGER.info("iCloud Sync startet (Log: %s)", logs_dir() / "icloud-sync.log")
    SyncApp().run()


if __name__ == "__main__":
    main()
