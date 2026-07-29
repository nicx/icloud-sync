"""Natives Einstellungs-Fenster (pyobjc/AppKit) — alle Einstellungen auf einen Blick.

Ersetzt die rumps-Popup-Kette durch EIN Fenster mit Tabs (Allgemein, Sync-Plan, Fehler-E-Mail,
Accounts). Bewusst **rumps-frei** und nur über eine schmale Fassade an Engine/Config gekoppelt —
so läuft es beim späteren rumps-Ausbau (Phase 3: natives ``NSStatusItem``) unverändert weiter.
Schritt 1 der UI-Konvergenz auf eine einzige pyobjc-UI.

Layout bewusst mit **expliziten Frames** (kein NSGridView/Auto-Layout): das hält editierbare
Textfelder verlässlich klick-/editierbar (AppKit + Frame ist hier robuster als Auto-Layout).
"""

from __future__ import annotations

import logging
from typing import Optional

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject

from . import ui_appkit
from .config.users import User, UserStatus
from .schedule import parse_schedule

LOGGER = logging.getLogger(__name__)

_STYLE = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
          | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)


def _label(text: str, x: float, y: float, w: float = 130, h: float = 18) -> NSTextField:
    f = NSTextField.labelWithString_(text)
    f.setFrame_(NSMakeRect(x, y, w, h))
    return f


def _field(x: float, y: float, w: float, value: str = "") -> NSTextField:
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 24))
    f.setStringValue_(value or "")
    return f


def _checkbox(title: str, x: float, y: float, w: float = 440, target=None, action=None) -> NSButton:
    b = NSButton.checkboxWithTitle_target_action_(title, target, action)
    b.setFrame_(NSMakeRect(x, y, w, 20))
    return b


def _button(title: str, x: float, y: float, w: float, target, action) -> NSButton:
    b = NSButton.buttonWithTitle_target_action_(title, target, action)
    b.setFrame_(NSMakeRect(x, y, w, 28))
    return b


class PreferencesWindowController(NSObject):
    """Hält das Einstellungs-Fenster und dient zugleich als TableView-DataSource."""

    # --- Konstruktion -------------------------------------------------------
    def initWithFacade_(self, facade):  # noqa: N802 (objc-Namensschema)
        self = objc.super(PreferencesWindowController, self).init()
        if self is None:
            return None
        self.facade = facade
        self.window = None
        self._users: list = []
        self._c: dict = {}      # editierbare Controls (Lesen/Schreiben beim Sichern)
        self._table = None
        return self

    # --- Öffnen -------------------------------------------------------------
    def show(self) -> None:
        if self.window is None:
            self._build()
        self._load_into_controls()
        self._reload_accounts()
        ui_appkit.activate_app()
        self.window.makeKeyAndOrderFront_(None)
        self.window.center()

    # --- Fensteraufbau ------------------------------------------------------
    def _build(self) -> None:
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 560, 470), _STYLE, NSBackingStoreBuffered, False)
        win.setTitle_("iCloud Sync – Einstellungen")
        win.setReleasedWhenClosed_(False)
        content = win.contentView()

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(12, 56, 536, 402))
        tabs.setAutoresizingMask_(2 | 16)
        tabs.addTabViewItem_(self._tab("Allgemein", self._build_general()))
        tabs.addTabViewItem_(self._tab("Sync-Plan", self._build_schedule()))
        tabs.addTabViewItem_(self._tab("Fehler-E-Mail", self._build_email()))
        tabs.addTabViewItem_(self._tab("Accounts", self._build_accounts()))
        content.addSubview_(tabs)

        save = _button("Sichern", 452, 14, 96, self, b"saveGlobals:")
        save.setAutoresizingMask_(1)
        save.setKeyEquivalent_("\r")
        content.addSubview_(save)
        self.window = win

    @staticmethod
    def _tab(label: str, view: NSView) -> NSTabViewItem:
        item = NSTabViewItem.alloc().initWithIdentifier_(label)
        item.setLabel_(label)
        item.setView_(view)
        return item

    @staticmethod
    def _content_view() -> NSView:
        return NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 512, 360))

    def _build_general(self) -> NSView:
        v = self._content_view()
        self._c["autostart"] = _checkbox("Beim Login starten (nur im .app-Bundle)", 24, 300)
        self._c["notifications"] = _checkbox("macOS-Benachrichtigungen", 24, 270)
        v.addSubview_(self._c["autostart"])
        v.addSubview_(self._c["notifications"])
        v.addSubview_(_label("Konfiguration:", 24, 226, 120))
        v.addSubview_(_button("Exportieren…", 150, 222, 130, self, b"exportConfig:"))
        v.addSubview_(_button("Importieren…", 290, 222, 130, self, b"importConfig:"))
        return v

    def _build_schedule(self) -> NSView:
        v = self._content_view()
        self._c["use_times"] = _checkbox(
            "Feste Uhrzeiten verwenden (statt Stunden-Intervall)", 24, 300, 460,
            self, b"scheduleModeChanged:")
        v.addSubview_(self._c["use_times"])
        v.addSubview_(_label("Intervall:", 24, 262, 80))
        self._c["interval"] = _field(112, 260, 70)
        v.addSubview_(self._c["interval"])
        v.addSubview_(_label("Stunden", 190, 262, 100))
        v.addSubview_(_label("Uhrzeiten:", 24, 224, 80))
        self._c["times"] = _field(112, 222, 330)
        v.addSubview_(self._c["times"])
        v.addSubview_(_label("Format: HH:MM, durch Komma getrennt (z. B. 07:30, 19:30)", 112, 202, 380, 16))
        return v

    def _build_email(self) -> NSView:
        v = self._content_view()
        self._c["email_enabled"] = _checkbox("Fehler-E-Mail aktiv", 24, 300, 400)
        v.addSubview_(self._c["email_enabled"])
        rows = [("Empfänger:", "email_to", 320), ("Absender (optional):", "email_from", 320),
                ("Relay-Host:", "smtp_host", 200), ("Relay-Port:", "smtp_port", 80)]
        y = 264
        for lbl, key, w in rows:
            v.addSubview_(_label(lbl, 24, y + 2, 140))
            self._c[key] = _field(172, y, w)
            v.addSubview_(self._c[key])
            y -= 34
        v.addSubview_(_button("Test-E-Mail senden", 172, y - 4, 180, self, b"sendTestEmail:"))
        return v

    def _build_accounts(self) -> NSView:
        v = self._content_view()
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, 80, 480, 264))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutoresizingMask_(2 | 16)
        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 478, 262))
        table.setUsesAlternatingRowBackgroundColors_(True)
        for ident, title, width in (("account", "Account", 165), ("status", "Status", 70),
                                    ("services", "Dienste", 150), ("plan", "Plan", 130),
                                    ("dest", "Ziel", 200), ("error", "Letzter Fehler", 200)):
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            table.addTableColumn_(col)
        table.setDataSource_(self)
        table.setDelegate_(self)
        scroll.setDocumentView_(table)
        v.addSubview_(scroll)
        self._table = table

        # Zwei Button-Reihen (sonst zu breit fürs Fenster).
        row1 = [("Hinzufügen", b"addAccount:"), ("Bearbeiten…", b"editAccount:"),
                ("Entfernen…", b"removeAccount:"), ("Sync jetzt", b"syncAccount:")]
        row2 = [("Re-Auth…", b"reauthAccount:"), ("Mail-Passwort…", b"mailPwAccount:"),
                ("Drive-Ausschlüsse…", b"driveExcludesAccount:"), ("Sync-Plan…", b"syncTimesAccount:")]
        for row, y in ((row1, 44), (row2, 10)):
            x = 16
            for title, action in row:
                v.addSubview_(_button(title, x, y, 116, self, action))
                x += 118
        return v

    # --- Laden/Speichern globaler Felder -----------------------------------
    def _load_into_controls(self) -> None:
        s = self.facade.settings
        self._c["autostart"].setState_(1 if self.facade.autostart_enabled() else 0)
        self._c["notifications"].setState_(1 if s.notifications else 0)
        self._c["use_times"].setState_(1 if s.sync_times else 0)
        self._c["interval"].setStringValue_(str(s.sync_interval_hours))
        self._c["times"].setStringValue_(", ".join(s.sync_times))
        self._c["email_enabled"].setState_(1 if s.error_email_enabled else 0)
        self._c["email_to"].setStringValue_(s.error_email_to or "")
        self._c["email_from"].setStringValue_(s.error_email_from or "")
        self._c["smtp_host"].setStringValue_(s.smtp_host or "")
        self._c["smtp_port"].setStringValue_(str(s.smtp_port))
        self._update_schedule_enabled()

    def _update_schedule_enabled(self) -> None:
        use_times = self._c["use_times"].state() == 1
        self._c["interval"].setEnabled_(not use_times)
        self._c["times"].setEnabled_(use_times)

    @objc.IBAction
    def scheduleModeChanged_(self, _sender) -> None:  # noqa: N802
        self._update_schedule_enabled()

    @objc.IBAction
    def saveGlobals_(self, _sender) -> None:  # noqa: N802
        s = self.facade.settings
        if self._c["use_times"].state() == 1:
            try:
                times = parse_schedule(self._c["times"].stringValue())
            except ValueError:
                ui_appkit.alert("Ungültig", "Uhrzeiten als HH:MM angeben, z. B. 07:30, 19:30.", "warning")
                return
            if not times:
                ui_appkit.alert("Ungültig", "Mindestens eine Uhrzeit angeben (oder Häkchen entfernen).", "warning")
                return
            s.sync_times = times
        else:
            try:
                s.sync_interval_hours = max(1, int(self._c["interval"].stringValue()))
            except ValueError:
                ui_appkit.alert("Ungültig", "Intervall als ganze Zahl (Stunden) angeben.", "warning")
                return
            s.sync_times = []
        try:
            port = int(self._c["smtp_port"].stringValue())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            ui_appkit.alert("Ungültig", "Relay-Port zwischen 1 und 65535 angeben.", "warning")
            return
        s.notifications = self._c["notifications"].state() == 1
        s.error_email_to = self._c["email_to"].stringValue().strip()
        s.error_email_from = self._c["email_from"].stringValue().strip()
        s.smtp_host = self._c["smtp_host"].stringValue().strip() or "127.0.0.1"
        s.smtp_port = port
        s.error_email_enabled = (self._c["email_enabled"].state() == 1) and bool(s.error_email_to)
        self.facade.save_settings()
        want_autostart = self._c["autostart"].state() == 1
        if want_autostart != self.facade.autostart_enabled():
            err = self.facade.set_autostart(want_autostart)
            if isinstance(err, str):
                ui_appkit.alert("Autostart", err, "warning")
                self._c["autostart"].setState_(1 if self.facade.autostart_enabled() else 0)
        self.facade.refresh_ui()
        ui_appkit.alert("Gespeichert", "Die Einstellungen wurden übernommen.")

    @objc.IBAction
    def sendTestEmail_(self, _sender) -> None:  # noqa: N802
        to = self._c["email_to"].stringValue().strip()
        if not to:
            ui_appkit.alert("Empfänger fehlt", "Bitte zuerst einen Empfänger eintragen.", "warning")
            return
        host = self._c["smtp_host"].stringValue().strip() or "127.0.0.1"
        try:
            port = int(self._c["smtp_port"].stringValue())
        except ValueError:
            ui_appkit.alert("Ungültig", "Relay-Port ist keine Zahl.", "warning")
            return
        sender = self._c["email_from"].stringValue().strip() or to
        self.facade.test_mail(host, port, sender, to)
        ui_appkit.alert("Test-E-Mail", "Versand angestoßen — Zustellung im Log/Postfach prüfen.")

    @objc.IBAction
    def exportConfig_(self, _sender) -> None:  # noqa: N802
        d = ui_appkit.choose_directory("Ordner für den Konfigurations-Export wählen:")
        if not d:
            return
        n = self.facade.export_config(d)
        ui_appkit.alert("Export", f"{n} Datei(en) exportiert." if n else "Export fehlgeschlagen.",
                        "info" if n else "warning")

    @objc.IBAction
    def importConfig_(self, _sender) -> None:  # noqa: N802
        if not ui_appkit.confirm("Konfiguration importieren?",
                                 "Aktuelle settings.json/users.json werden überschrieben. "
                                 "Passwörter (Keychain) ggf. neu setzen."):
            return
        d = ui_appkit.choose_directory("Ordner mit der gesicherten Konfiguration wählen:")
        if not d:
            return
        n = self.facade.import_config(d)
        if not n:
            ui_appkit.alert("Import", "Keine settings.json/users.json gefunden.", "warning")
            return
        self._load_into_controls()
        self._reload_accounts()
        ui_appkit.alert("Import", f"{n} Datei(en) importiert.")

    # --- TableView DataSource ----------------------------------------------
    def _reload_accounts(self) -> None:
        self._users = list(self.facade.list_users())
        if self._table is not None:
            self._table.reloadData()

    def numberOfRowsInTableView_(self, _table) -> int:  # noqa: N802
        return len(self._users)

    def tableView_objectValueForTableColumn_row_(self, _table, col, row):  # noqa: N802
        if row >= len(self._users):
            return ""
        u = self._users[row]
        ident = col.identifier()
        if ident == "account":
            return u.apple_id
        if ident == "status":
            return u.status.value if isinstance(u.status, UserStatus) else str(u.status)
        if ident == "services":
            return self.facade.services_summary(u)
        if ident == "plan":
            return ", ".join(u.sync_times) if u.sync_times else "global"
        if ident == "dest":
            return u.dest_base_path or "—"
        if ident == "error":
            return u.last_error or ""
        return ""

    def _selected_user(self) -> Optional[User]:
        idx = self._table.selectedRow()
        if idx < 0 or idx >= len(self._users):
            return None
        return self._users[idx]

    # --- Account-Aktionen ---------------------------------------------------
    @objc.IBAction
    def addAccount_(self, _sender) -> None:  # noqa: N802
        self._edit_account(None)

    @objc.IBAction
    def editAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is None:
            ui_appkit.alert("Kein Account gewählt", "Bitte zuerst einen Account in der Liste wählen.", "warning")
            return
        self._edit_account(u)

    @objc.IBAction
    def removeAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is None:
            return
        if not ui_appkit.confirm(f"{u.apple_id} entfernen?", "Backup-Dateien bleiben erhalten."):
            return
        self.facade.remove_user(u.apple_id)
        self._reload_accounts()
        self.facade.refresh_ui()

    @objc.IBAction
    def syncAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is not None:
            self.facade.sync_user(u.apple_id)
            ui_appkit.alert("Sync", f"Sync für {u.apple_id} gestartet.")

    @objc.IBAction
    def reauthAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is None:
            return
        pw = self.facade.get_web_password(u.apple_id)
        if not pw:
            ui_appkit.alert("Kein Passwort", "Für diesen Account ist kein Apple-ID-Passwort hinterlegt.", "warning")
            return
        if not self.facade.is_online():
            ui_appkit.alert("Offline", "iCloud ist gerade nicht erreichbar.", "warning")
            return
        result = self.facade.login(u.apple_id, pw)
        if result.error:
            self.facade.set_status(u.apple_id, UserStatus.ERROR)
            ui_appkit.alert("Fehler", result.error, "warning")
        elif result.needs_2fa:
            ok = self._do_2fa(result.api, u.apple_id)
            self.facade.set_status(u.apple_id, UserStatus.OK if ok else UserStatus.NEEDS_REAUTH)
        else:
            self.facade.set_status(u.apple_id, UserStatus.OK)
        self._reload_accounts()
        self.facade.refresh_ui()

    @objc.IBAction
    def mailPwAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is None:
            return
        if self._prompt_mail_pw(u.apple_id) and not u.sync_mail:
            u.sync_mail = True
            self.facade.update_user(u)
        self._reload_accounts()
        self.facade.refresh_ui()

    @objc.IBAction
    def driveExcludesAccount_(self, _sender) -> None:  # noqa: N802
        u = self._selected_user()
        if u is None:
            ui_appkit.alert("Kein Account gewählt", "Bitte zuerst einen Account in der Liste wählen.", "warning")
            return
        if not u.sync_drive:
            ui_appkit.alert("Drive nicht aktiv", "Drive-Ausschlüsse gibt es nur, wenn Drive gesichert wird.", "warning")
            return
        if not self.facade.is_online():
            ui_appkit.alert("Offline", "iCloud ist gerade nicht erreichbar.", "warning")
            return
        names = self.facade.list_drive_folders(u.apple_id)
        if names is None:
            ui_appkit.alert("Ordner nicht abrufbar",
                            "Oberste Drive-Ordner konnten nicht geladen werden (Passwort fehlt, "
                            "offline oder Re-Auth nötig).", "warning")
            return
        self._choose_excludes(u, names)

    @objc.IBAction
    def syncTimesAccount_(self, _sender) -> None:  # noqa: N802
        """Eigener Uhrzeit-Plan für EINEN Account (leer = globaler Plan)."""
        u = self._selected_user()
        if u is None:
            ui_appkit.alert("Kein Account gewählt", "Bitte zuerst einen Account in der Liste wählen.", "warning")
            return
        global_txt = ", ".join(self.facade.settings.sync_times) or "Intervall alle %d h" % (
            self.facade.settings.sync_interval_hours)
        text = ui_appkit.ask_text(
            f"Sync-Plan – {u.apple_id}",
            "Eigene Uhrzeiten für diesen Account, z. B. „07:00, 12:00, 19:00“.\n"
            f"Leer lassen = globaler Plan ({global_txt}).",
            default=", ".join(u.sync_times))
        if text is None:
            return
        try:
            times = parse_schedule(text)
        except ValueError:
            ui_appkit.alert("Ungültige Uhrzeit",
                            "Bitte Zeiten als HH:MM angeben, durch Komma getrennt.", "warning")
            return
        u.sync_times = times
        self.facade.update_user(u)
        self._reload_accounts()
        self.facade.refresh_ui()

    def _choose_excludes(self, user: User, names: list) -> None:
        """Modaler Dialog: oberste Drive-Ordner per Häkchen aus-/abwählen."""
        excluded = set(user.drive_excludes)
        all_names = sorted(set(names) | excluded)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 420), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        win.setTitle_(f"Drive-Ausschlüsse – {user.apple_id}")
        cv = win.contentView()
        cv.addSubview_(_label("Angehakte Ordner werden NICHT gesichert (und lokal entfernt):",
                              16, 386, 388, 16))

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, 60, 388, 318))
        scroll.setHasVerticalScroller_(True)
        row_h = 24
        doc_h = max(318, len(all_names) * row_h + 8)
        doc = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 368, doc_h))
        checks = []
        for i, name in enumerate(all_names):
            cb = _checkbox(name, 8, doc_h - (i + 1) * row_h, 352)
            cb.setState_(1 if name in excluded else 0)
            doc.addSubview_(cb)
            checks.append((name, cb))
        scroll.setDocumentView_(doc)
        cv.addSubview_(scroll)

        result = {"ok": False}

        def do_ok(_s=None):
            from AppKit import NSApp
            result["ok"] = True
            NSApp.stopModalWithCode_(1)
            win.orderOut_(None)

        def do_cancel(_s=None):
            from AppKit import NSApp
            NSApp.stopModalWithCode_(0)
            win.orderOut_(None)

        ok_btn = _make_button("Speichern", 310, 16, 92, do_ok)
        ok_btn.setKeyEquivalent_("\r")
        cancel_btn = _make_button("Abbrechen", 210, 16, 92, do_cancel)
        cv.addSubview_(ok_btn)
        cv.addSubview_(cancel_btn)
        self._editor_keep = (win, checks, ok_btn, cancel_btn)

        ui_appkit.activate_app()
        from AppKit import NSApp
        NSApp.runModalForWindow_(win)
        if not result["ok"]:
            return
        user.drive_excludes = [name for name, cb in checks if cb.state() == 1]
        self.facade.update_user(user)
        self._reload_accounts()
        self.facade.refresh_ui()

    def _prompt_mail_pw(self, apple_id: str) -> bool:
        pw = ui_appkit.ask_text(
            "iCloud Mail – App-Passwort",
            "App-spezifisches Passwort (appleid.apple.com → Anmeldung & Sicherheit → "
            "App-spezifische Passwörter).", secure=True)
        if not pw:
            return False
        self.facade.set_mail_password(apple_id, pw)
        return True

    def _do_2fa(self, api, apple_id: str) -> bool:
        if api is None:
            ui_appkit.alert("2FA nötig", "Bitte erneut über Re-Auth versuchen.", "warning")
            return False
        code = ui_appkit.ask_text("Zwei-Faktor-Authentifizierung",
                                  "6-stelliger Code von einem vertrauenswürdigen Apple-Gerät:")
        if not code:
            return False
        if self.facade.submit_2fa(api, code):
            return True
        ui_appkit.alert("Code abgelehnt", "Der 2FA-Code wurde nicht akzeptiert.", "warning")
        return False

    # --- Account-Editor (modal) --------------------------------------------
    def _edit_account(self, existing: Optional[User]) -> None:
        editing = existing is not None
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 460, 340), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        win.setTitle_("Account bearbeiten" if editing else "Account hinzufügen")
        cv = win.contentView()

        cv.addSubview_(_label("Apple-ID:", 20, 296, 90))
        apple = _field(115, 294, 320, existing.apple_id if editing else "")
        apple.setEnabled_(not editing)
        cv.addSubview_(apple)

        cv.addSubview_(_label("Dienste:", 20, 262, 90))
        cbs = {}
        specs = [("drive", "iCloud Drive", existing.sync_drive if editing else True),
                 ("photos", "iCloud Photos", existing.sync_photos if editing else True),
                 ("shared", "Geteilte Mediathek", existing.sync_shared_photos if editing else False),
                 ("contacts", "Kontakte", existing.sync_contacts if editing else False),
                 ("mail", "Mail (app-spez. Passwort)", existing.sync_mail if editing else False)]
        y = 262
        for key, title, on in specs:
            cb = _checkbox(title, 115, y, 300)
            cb.setState_(1 if on else 0)
            cv.addSubview_(cb)
            cbs[key] = cb
            y -= 26

        cv.addSubview_(_label("Ziel-Ordner:", 20, y - 2, 90))
        dest_field = _field(115, y - 4, 250, existing.dest_base_path if editing else "")
        dest_field.setEditable_(False)
        cv.addSubview_(dest_field)
        box = {"dest": existing.dest_base_path if editing else ""}

        def choose_dest(_s=None):
            d = ui_appkit.choose_directory("Ziel-Ordner wählen:", box["dest"] or None)
            if d:
                box["dest"] = d
                dest_field.setStringValue_(d)

        choose_btn = _make_button("Wählen…", 370, y - 5, 76, choose_dest)
        cv.addSubview_(choose_btn)

        result = {"ok": False}

        def do_ok(_s=None):
            from AppKit import NSApp
            result["ok"] = True
            NSApp.stopModalWithCode_(1)
            win.orderOut_(None)

        def do_cancel(_s=None):
            from AppKit import NSApp
            NSApp.stopModalWithCode_(0)
            win.orderOut_(None)

        ok_btn = _make_button("Speichern", 350, 16, 92, do_ok)
        ok_btn.setKeyEquivalent_("\r")
        cancel_btn = _make_button("Abbrechen", 250, 16, 92, do_cancel)
        cv.addSubview_(ok_btn)
        cv.addSubview_(cancel_btn)
        # Am Leben halten, solange der Modal-Loop läuft.
        self._editor_keep = (ok_btn, cancel_btn, choose_btn, win, cbs, apple, dest_field)

        ui_appkit.activate_app()
        from AppKit import NSApp
        NSApp.runModalForWindow_(win)
        if not result["ok"]:
            return

        apple_id = apple.stringValue().strip()
        if not apple_id:
            ui_appkit.alert("Fehlt", "Bitte eine Apple-ID angeben.", "warning")
            return
        if not editing and self.facade.get_user(apple_id) is not None:
            ui_appkit.alert("Bereits vorhanden", f"{apple_id} ist schon konfiguriert.", "warning")
            return
        if not box["dest"]:
            ui_appkit.alert("Ziel fehlt", "Bitte einen Ziel-Ordner wählen.", "warning")
            return

        sd, sp = cbs["drive"].state() == 1, cbs["photos"].state() == 1
        ss, sc, sm = cbs["shared"].state() == 1, cbs["contacts"].state() == 1, cbs["mail"].state() == 1
        if not (sd or sp or sc or sm):
            ui_appkit.alert("Nichts gewählt", "Mindestens einen Dienst auswählen.", "warning")
            return

        user = existing or User(apple_id=apple_id, status=UserStatus.IDLE)
        user.sync_drive, user.sync_photos = sd, sp
        user.sync_shared_photos = ss and sp
        user.sync_contacts, user.sync_mail = sc, sm
        user.dest_base_path = box["dest"]

        # Web-Login nur bei neuem Account oder wenn noch kein Passwort hinterlegt ist.
        if (sd or sp or sc) and not self.facade.get_web_password(apple_id):
            pw = ui_appkit.ask_text("Apple-ID-Passwort",
                                    "Nur im macOS-Keychain (für Drive/Photos/Kontakte).", secure=True)
            if not pw:
                ui_appkit.alert("Kein Passwort", "Ohne Apple-ID-Passwort kein Drive/Photos/Kontakte.", "warning")
                return
            self.facade.set_web_password(apple_id, pw)
            res = self.facade.login(apple_id, pw)
            if res.error:
                self.facade.delete_web_password(apple_id)
                ui_appkit.alert("Login fehlgeschlagen", res.error, "warning")
                return
            if res.needs_2fa and not self._do_2fa(res.api, apple_id):
                user.status = UserStatus.NEEDS_REAUTH

        if sm and not self.facade.get_mail_password(apple_id):
            if not self._prompt_mail_pw(apple_id):
                user.sync_mail = False
                ui_appkit.alert("Mail übersprungen",
                                "Ohne app-spezifisches Passwort wird Mail nicht gesichert.", "warning")

        if editing:
            self.facade.update_user(user)
        else:
            self.facade.add_user(user)
        self._reload_accounts()
        self.facade.refresh_ui()


def _make_button(title: str, x: float, y: float, w: float, callback):
    """NSButton mit Python-Callback (über einen kleinen Trampolin-Target)."""
    target = _ActionTarget.alloc().initWithCallback_(callback)
    btn = NSButton.buttonWithTitle_target_action_(title, target, b"invoke:")
    btn.setFrame_(NSMakeRect(x, y, w, 28))
    objc.setAssociatedObject(btn, b"_target", target, 1)  # Target festhalten (GC)
    return btn


class _ActionTarget(NSObject):
    """Brücke von einem NSButton-Action-Selector auf ein Python-Callable."""

    def initWithCallback_(self, callback):  # noqa: N802
        self = objc.super(_ActionTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.IBAction
    def invoke_(self, sender):  # noqa: N802
        self._callback(sender)
