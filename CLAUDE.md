# iCloud Sync – Projektdokumentation (Ist-Stand)

Native macOS-**Menüleisten-App** (`.app`-Bundle), die die iCloud-Daten **mehrerer
Apple-Accounts** auf ein gemountetes Volume (UNAS Pro) spiegelt — als
**dateibasierter Sync-Spiegel**, kein additives Backup.

> Diese Datei beschreibt den **aktuellen Stand** des Codes (nicht mehr den
> ursprünglichen Bau-Auftrag). Bei Änderungen am Verhalten bitte hier mitziehen.
> Wo Entscheidungen offen sind: nachfragen, nicht raten.

---

## Ziel

Täglicher/periodischer, **inkrementeller, resumebarer** Spiegel der iCloud-Daten
mehrerer Accounts:

- **iCloud Drive** (Dokumente)
- **iCloud Photos** (Originale, inkl. Live Photos)
- **iCloud Mail** (IMAP, rohe `.eml` je Ordner)
- **iCloud Contacts** (vCard `.vcf` + verlustfreies Roh-`.json` je Kontakt)

Ziel-Speicher: **UNAS Pro** (gemountetes Netzlaufwerk, Pfad pro User konfigurierbar).

### Lösch-Semantik: Spiegel, nicht additiv

**Wichtig — bewusste Abkehr vom ursprünglichen Spec:** Das Backup ist ein
**Spiegel**. Eine serverseitig gelöschte/verschobene Datei wird **lokal ebenfalls
entfernt**. Historie/Versionierung übernehmen die **UNAS-Snapshots**, nicht ein
additives Anhäufen im Zielordner.

Das Löschen ist streng **geguarded** (siehe `sync/util.py::prune_extra`): es passiert
**nur** nach einem *vollständigen, fehlerfreien* Server-Listing. Sobald ein Ordner-
Listing / eine Iteration / ein IMAP-SEARCH fehlschlägt, wird in diesem Lauf **nichts**
gelöscht (nur heruntergeladen). Bei Photos zusätzlich: leeres Ergebnis ⇒ kein Löschen.
Damit kann ein API-Aussetzer keinen Massenverlust auslösen.

## Rahmenbedingungen (entschieden)

- Läuft auf einem **24/7 Mac**.
- **Kein** Account hat Advanced Data Protection aktiv → Web-API ist nutzbar.
- Drive/Photos über die **inoffizielle iCloud-Web-API** (`pyicloud`, in **2.6.4**
  verifiziert). Mail über **IMAP** (`imap.mail.me.com`), unabhängig von der Web-Session.
- **Doppelklick-App**, kein Script. Verpackung via py2app zu echtem `.app`.
- **In-App-Scheduler mit Catch-up** (ein Prozess), kein separater LaunchAgent für den
  Sync. Ein LaunchAgent wird nur für den **Login-Autostart** der App selbst genutzt.

## Tech-Stack

- **Python 3.13** (im `.venv`; ≥ 3.12 vorausgesetzt)
- **rumps** – Menüleisten-UI (Status-Bar, Popover, Notifications)
- **pyicloud** (2.6.4) – iCloud Web-API (Drive + Photos)
- **imaplib** (stdlib) – iCloud Mail
- **keyring** – Credentials im macOS-Keychain
- **pyobjc** (AppKit/Foundation) – nativer Ordner-Dialog (NSOpenPanel), Menüleisten-Icon
- **py2app** – Bau des `.app`-Bundles
- macOS-Notifications via rumps, `pync` als Fallback

> **Kein sqlite.** Der ursprüngliche Spec sah ein sqlite-Manifest (`sync/state.py`) vor;
> das ist entfallen. Der Zustand ist allein das Dateisystem des Zielordners.

## Architektur

Ein einziges `.app`-Bundle, das **als Menüleisten-Resident dauerhaft läuft**
(`LSUIElement=True` → kein Dock-Icon, kein Fenster). Login-Autostart optional per
In-App-Toggle (LaunchAgent).

Trennung im Code, NICHT in separate Prozesse:

- **UI-Schicht** (`src/app.py`, rumps): Menüleisten-Icon/-Menü (schlank: pro Account „Sync jetzt",
  „Alle jetzt synchronisieren", Auto-Sync pausieren, „Einstellungen…", „Log anzeigen…"),
  Live-Fortschritt (Spinner + Counts), Scheduler. Hält keine Sync-Logik. Syncs
  laufen in einem Hintergrund-Thread (Daemon), serialisiert über ein `threading.Lock` (keine
  überlappenden Läufe).
- **Einstellungs-Fenster** (`src/prefs_window.py` + `src/ui_appkit.py`, **reines pyobjc/AppKit**):
  ein natives Fenster mit Tabs Allgemein/Sync-Plan/Fehler-E-Mail/Accounts — zeigt alle
  Einstellungen auf einen Blick und ersetzt die frühere Popup-Kette. Bewusst **rumps-frei** und nur
  über die schmale `PrefsFacade` (in `app.py`) an Engine/Config/Keychain gekoppelt. **Ziel: UI-
  Konvergenz** auf eine einzige pyobjc-UI — Phase 3 ersetzt später auch das rumps-Status-Item durch
  ein natives `NSStatusItem` (dann kann das Fenster unverändert weiterlaufen).
- **Scheduler** (in `app.py`): zwei `rumps.Timer`. Ein langsamer Tick (300 s) prüft
  „fällige" User und stößt den Sync an; ein schneller Tick (1 s) aktualisiert nur die
  Live-Anzeige. Fälligkeit per `_is_due`: **entweder** Stunden-Intervall **oder** feste
  Uhrzeiten. Maßgeblich ist `schedule.effective_times(user.sync_times, settings.sync_times)`:
  ein **eigener Account-Plan schlägt den globalen**. Ist danach eine Liste `"HH:MM"` (lokale
  Wandzeit) gesetzt, gilt der **Uhrzeit-Plan** (`schedule.due_by_schedule`: feuert je Slot
  genau einmal, ≤5 min nach der Zeit); sonst das **Intervall**
  (`now - last_run >= sync_interval_hours`, Default 4 h). Die reine Plan-Logik liegt in
  `src/schedule.py` (kein rumps-Import → unit-testbar). Läufe sind **serialisiert**
  (`_sync_lock`) — ein fälliger kleiner Account wartet also, wenn gerade ein großer läuft.
  **Missed-Run-Catch-up** in beiden Modi — war der Mac im Sleep, ist `last_run` alt und der
  User sofort fällig (beim Uhrzeit-Plan wird der verpasste Slot einmalig nachgeholt). `needs_reauth`- und `running`-User werden vom Auto-Sync
  ausgenommen (`_is_due`); `error`-User werden beim nächsten Fälligkeitsfenster
  hingegen wieder mitgenommen (automatischer Retry). **Auto-Sync pausierbar**
  (`settings.auto_sync_paused`, Menü „Auto-Sync pausieren/fortsetzen") — dann ist niemand
  fällig; „Sync jetzt" bleibt manuell möglich. **Start-Gnadenfrist**
  (`settings.startup_delay_seconds`, Default 90 s): rumps-Timer feuern sofort beim Start —
  in der Gnadenfrist wird noch nicht gesynct, damit der erste Lauf nach einem Reboot nicht
  ins noch nicht hochgefahrene Netz/DNS läuft. **Offline-Erkennung** (`engine.is_online`,
  TCP zu `www.icloud.com:443`): ist iCloud nicht erreichbar, wird der Lauf **still
  übersprungen** (kein `error`, keine Fehler-E-Mail) und `last_run` **nicht** gesetzt → der
  nächste Tick versucht es erneut (kein 4-h-Loch). Auch `_refresh_sessions` wartet die
  Gnadenfrist ab und überspringt offline.
- **Menüleisten-Icon als Statusanzeige** (`menubar_icon.py`, zwei Template-Varianten):
  **gefüllt** (`icloud.fill`) = Auto-Sync aktiv, **umrandet** (`icloud`) = pausiert; rotes
  Badge bei `error`/`needs_reauth`, Spinner im Titel während eines Laufs (`_update_icon`).
- **Sync-Engine** (`src/sync/engine.py`): orchestriert pro User Mount-Check →
  Drive/Photos (Web) → Mail (IMAP). Eigenständig ohne UI aufrufbar (Tests/Debug). Ein
  Fehler bei einem User/Dienst stoppt die anderen nicht. **Mail läuft unabhängig von der
  Web-Session** — auch wenn Drive/Photos gerade Re-Auth brauchen, wird Mail gesichert.

## Projektstruktur

```
icloud-sync/
  CLAUDE.md                # diese Datei
  README.md                # Build, Ad-hoc-Signing, Gatekeeper, Mount-Voraussetzung
  launcher.py              # py2app-Entrypoint (ruft src.app.main)
  requirements.txt         # Laufzeit-Abhängigkeiten
  requirements-build.txt   # zusätzlich für den py2app-Build
  src/
    app.py                 # rumps-Entrypoint, Menüleiste, Scheduler, Live-Fortschritt, PrefsFacade
    prefs_window.py        # natives Einstellungs-Fenster (pyobjc/AppKit): Allgemein/Sync-Plan/Fehler-E-Mail/Accounts
    ui_appkit.py           # geteilte AppKit-Helfer (Alert/Eingabe/Ordnerdialog, Main-Thread-Dispatch) – rumps-frei
    schedule.py            # reine Planungslogik für feste Sync-Uhrzeiten (parse_schedule, due_by_schedule)
    notify.py              # macOS-Notifications (rumps / pync-Fallback)
    menubar_icon.py        # Template-Icons (gefüllt=aktiv / umrandet=pausiert) fürs Menüleisten-Icon
    autostart.py           # Login-Autostart via LaunchAgent (In-App-Toggle)
    config/
      users.py             # User-Modell + UsersStore (JSON-Persistenz, kein Passwort)
      settings.py          # globale Settings (Sync-Intervall/-Uhrzeiten, autostart, notifications, Fehler-E-Mail)
      paths.py             # App-Support-Pfade, Pro-User-Cookie-Dir, Legacy-Migration
      backup.py            # Config-Sicherung (settings.json+users.json, ohne Passwörter/Sessions)
    auth/
      session.py           # EINZIGE pyicloud-Stelle: Login, 2FA, Re-Auth, Cookie-Persistenz
      keychain.py          # Credential-Storage via keyring (Web-PW + Mail-App-PW)
    sync/
      engine.py            # Orchestrierung pro User (Drive + Photos + Mail)
      drive.py             # iCloud Drive – Datei-Spiegel
      photos.py            # iCloud Photos – Datei-Spiegel (Originale + Live-Video)
      mail.py              # iCloud Mail – IMAP-Datei-Spiegel (.eml)
      contacts.py          # iCloud Contacts – vCard + Roh-JSON je Kontakt
      util.py              # geteilte Helfer: Pfad-Hygiene, Retry/Backoff, Streaming, prune
  build/
    setup.py               # py2app-Config (LSUIElement, Icon, Bundle-ID de.nicx.icloud-sync)
    icon.icns              # App-Icon (falls vorhanden)
  tests/
    test_sync.py           # mock-basierte Tests (kein Netz/Account); .venv/bin/python tests/test_sync.py
```

## User-Modell (`config/users.py`)

Pro User (`User`-Dataclass, persistiert als `users.json` in App Support):

- `apple_id` (E-Mail) — eindeutiger Schlüssel
- `sync_drive`, `sync_photos` (Default an), `sync_contacts` (Default **aus** — Web-Session,
  kein Extra-Passwort), `sync_mail` (Default **aus** — braucht app-spezifisches Passwort)
- `sync_shared_photos` (Default **aus**): zusätzlich die **geteilte Mediathek** nach
  `SharedPhotos/` sichern (Add-on zu `sync_photos`). Pro geteilter Bibliothek sollte nur **ein**
  Account das aktivieren (Paare teilen sich dieselbe → sonst doppelt). Toggle im User-Untermenü.
- `dest_base_path` — Ziel-Basispfad auf dem (gemounteten) Volume; darunter legt die
  Engine `Drive/`, `Photos/`, `Contacts/`, `Mail/` an
- `sync_times` (Default leer): **eigener** Uhrzeit-Plan nur für diesen Account (`"HH:MM"`,
  lokale Wandzeit). Leer = globaler Plan aus den Settings. Auswahl im Einstellungs-Fenster →
  Accounts → „Sync-Plan…"; die Spalte „Plan" zeigt „global" oder die eigenen Zeiten.
  Hintergrund: Der Aufwand pro Lauf ist **fix** (voller Server-Walk, unabhängig von der
  Änderungsmenge) — gemessen timo ~54 min (Drive 34 / Photos 15 / Mail 5), max ~3 min,
  annabell ~37 s, hanna ~23 s, familie ~4 s. Kleine Accounts dürfen also oft laufen, ohne
  dass der große dabei mitgezogen wird.
- `drive_excludes` (Default leer): Drive-Ordner (rel. Pfade), die **nicht** gesichert werden —
  z. B. mit mir geteilte Ordner auf dem Collaborator-Account. Ausgeschlossenes wird vom
  Spiegel-Prune **lokal entfernt**. Auswahl im Einstellungs-Fenster → Accounts →
  „Drive-Ausschlüsse…" (Live-Ordnerliste + Häkchen).
- `status`: `idle` / `running` / `ok` / `needs_reauth` / `error`
- `last_run`: ISO-8601-Zeitstempel (UTC) des letzten erfolgreichen Laufbeginns
- `last_error`: Klartext-Grund des letzten Fehlers (für Menü/Notification; `None` bei Erfolg)

Passwörter stehen **nie** in `users.json` — nur im Keychain.

## Credentials (`auth/keychain.py`)

Zwei Keychain-Services, Account-Schlüssel ist jeweils die Apple-ID:

- `icloud-sync` — reguläres Apple-ID-Passwort (Web-API: Drive/Photos)
- `icloud-sync-mail` — **app-spezifisches** Passwort (IMAP; reguläres PW wird von Apple
  im IMAP abgelehnt)

Beim Lesen wird transparent auf die Alt-Services (`icloud-backup` / `-mail` vor der
Umbenennung) zurückgegriffen und der Eintrag migriert.

**Zugriff über `/usr/bin/security` (nicht in-process):** `auth/keychain.py` ruft das
Apple-signierte `security`-Tool als Subprozess auf, statt den Keychain in-process (früher
`keyring`) zu lesen. Grund: bei in-process-Zugriff bindet macOS „Immer erlauben" an die
**Code-Identität der App**; die ändert sich bei jedem Rebuild (self-signed, **keine Team-ID**) →
Prompt nach jedem Update. `security` hat eine **stabile** Identität → nach einmaligem „Immer
erlauben" für `security` ist dauerhaft Ruhe. Passwörter werden **base64-kodiert** abgelegt
(`b64:`-Marker), weil `security -w` Nicht-ASCII sonst als Hex ausgibt; Alt-Einträge (roh/Hex)
werden beim ersten Lesen erkannt und auf das neue Format normalisiert.

## Inkrementelle Logik (dateibasiert, kein Manifest)

Gemeinsames Prinzip: **Das Dateisystem ist der Zustand.** „Schon geladen?" =
Zieldatei existiert / stimmt in Größe+mtime. Spiegeln = Überzähliges (geguarded) löschen.

**Drive** (`sync/drive.py`): rekursiver Walk über den Drive-Tree (Tiefenlimit 64,
`trash`/`unknown` übersprungen). Download-Entscheidung via `util.needs_download`
(fehlt / Größe ≠ / mtime weicht > 2 s ab). 0-Byte-Dateien werden als leere Datei
angelegt (iCloud liefert sonst 400). Resumebar via `.part` + atomarem Rename.
**Ausschlüsse** (`user.drive_excludes`, normalisiert via `safe_component`): passende rel. Pfade
werden im `_walk` übersprungen (nicht geladen, **nicht** in `expected` → der Spiegel-Prune
entfernt eine vorhandene lokale Kopie). Damit lassen sich **geteilte Ordner** auf dem
Collaborator-Account entdoppeln (der Besitzer-Account sichert sie). pyicloud liefert kein
verlässliches Eigentümer-Feld → bewusst manuelle Liste statt fragiler Auto-Erkennung
(Fallstrick #7); Auswahl per Live-Ordnerliste (`session.list_drive_top_level`) + Häkchen im UI.

**Photos** (`sync/photos.py`): `api.photos.all` (intern paginiert) iterieren = **private**
Mediathek (CloudKit-Zone `PRIMARY_ZONE`, `scope="private"`). Zielpfad
`Photos/<YYYY>/<MM>/<kurz-id>_<name>`. **Namens-Kollisionen** über eine kurze, stabile
SHA1-Asset-ID als Präfix gelöst. **Live Photos**: Original (`original`) **und** Video
(`original_video`) werden beide gesichert. Originale, nicht optimierte Versionen. Streaming über
die authentifizierte Session. Refaktoriert auf `_sync_into(dest_dir, album_sources, …)` mit
eigenem `expected`-Set + Prune-Scope je Ablage.
**Geteilte Mediathek** (optional, `user.sync_shared_photos`): die iCloud Shared Photo Library
liegt in **separaten** Zonen (`SharedSync-…`, `scope="shared-library"`) und ist **nicht** in
`api.photos.all` — sie kommt über `api.photos.libraries` (`_shared_album_sources`, best-effort
gekapselt) und wird nach `SharedPhotos/` gespiegelt (getrennter Prune; leere/fehlende Shared-Lib
⇒ kein Löschen). **Wichtig** (Move-in-Shared): verschiebt man ein Foto von „Persönlich" nach
„Gemeinsam", verlässt es die private Zone → ohne aktiviertes `sync_shared_photos` würde der
`Photos/`-Spiegel es lokal löschen; mit aktivierter geteilter Sicherung landet es stattdessen in
`SharedPhotos/`.

**Mail** (`sync/mail.py`): alle Ordner via IMAP `LIST` (modified-UTF-7-Namen dekodiert),
je Ordner `Mail/<Ordner>/<uid>.eml` (rohes RFC822 inkl. Anhänge). **Ungelesen-schonend:**
`select(readonly=True)` + `BODY.PEEK[]` (setzt kein `\Seen`, ändert keine Flags).
**UIDVALIDITY** wird je Ordner in `.uidvalidity` gemerkt; bei Wechsel wird der Ordner
lokal zurückgesetzt und neu geladen. Login probiert Apple-ID und Lokalteil. Der FETCH holt
zusätzlich `INTERNALDATE` (Server-Empfangszeit) und setzt sie via `util.set_mtime` als
**Änderungs- und Erstellungsdatum** der `.eml` — die Finder-Spalten zeigen so das Empfangs-,
nicht das Download-Datum (Dateiname/Schema unverändert `<uid>.eml`).

**Contacts** (`sync/contacts.py`): `api.contacts.all` (rohe Kontakt-Dicts) → je Kontakt
`Contacts/<name>_<kurz-id>.vcf` **und** `…_.json`. Die **vCard 3.0** bildet die Standardfelder
defensiv ab (importierbar); das **Roh-JSON** ist die **verlustfreie** Quelle (Apple-Extensions,
Gruppen, Foto). Änderungserkennung über Inhaltsvergleich (`_write_if_changed`); `kurz-id` =
SHA1 der `contactId`. **Guard:** `None`/Fehler ⇒ kein Prune; **leere** Liste ⇒ ebenfalls kein
Prune (Schutz vor Massenlöschen). Web-Session (kein Extra-Passwort).

**Retry/Backoff** (`util.with_retries`): exponentielles Backoff (Default 4 Versuche, ab
2 s) nur bei retrybaren Fehlern (HTTP 429/5xx, „throttl/rate limit/timeout"). Apple nicht
hämmern.

## Re-Auth-Handling (`auth/session.py`)

`session.py` ist die **einzige** Stelle, die `pyicloud` importiert (kapselt API-Drift,
Fallstrick #7). Nach außen nur stabile Typen (`LoginResult`, `UserStatus`).

- Trusted-Session-Cookies pro User in `~/Library/Application Support/icloud-sync/sessions/<id>/`
  (überlebt App-Updates). Dieses Verzeichnis wird auf `0700`, enthaltene Dateien best-effort
  auf `0600` gesetzt (`paths._restrict_session_perms`) — die Tokens umgehen Passwort **und**
  2FA, sind also so schützenswert wie Credentials.
- Abgelaufene Session / `requires_2fa`/`2sa` → `UserStatus.NEEDS_REAUTH`, **rotes Badge**
  am Menüleisten-Icon, macOS-Notification. `check_session` prüft das beim App-Start, ohne
  einen vollen Sync zu starten.
- Re-Auth-Flow im UI: 2FA-Code eingeben → `validate_2fa_code` → `trust_session`.
- Betroffener User wird vom Auto-Sync ausgesetzt; andere User laufen weiter. Mail des
  betroffenen Users läuft trotzdem (eigene Credentials).

## Logging & Fehlerdiagnose

`app._setup_logging()` (in `main()`) konfiguriert den Root-Logger auf eine **rotierende
Datei** `~/Library/Application Support/icloud-sync/logs/icloud-sync.log` (1 MB × 5) **plus**
stderr (für Dev). In der `.app` ist stderr verloren — die Datei ist die einzige
verlässliche Quelle (`paths.logs_dir()` ist damit verdrahtet). Zusätzlich werden
unbehandelte Exceptions via `sys.excepthook`/`threading.excepthook` geloggt, und `_spawn`
kapselt jeden Hintergrund-Task in try/except (kein lautloses Thread-Sterben).

**Lauf-Dauer:** `engine.run_user` loggt beim Start „Sync gestartet" und am Ende „Sync fertig in
Xs (Status …)" je User — damit ist die Laufzeit (v. a. des großen Mail-/Drive-Scans) im Log
ablesbar. Der stille Offline-Skip (Netz noch nicht oben) bleibt bewusst ohne Dauer-Zeile.

**Fehlergrund sichtbar:** `engine.run_user` sammelt je Fehlerpfad einen knappen Klartext
und legt ihn (über `UsersStore.set_status(..., last_error=…)`) in `User.last_error` ab —
bei Erfolg `None` (gelöscht). Das Menü zeigt ihn im User-Untermenü als „⚠️ Letzter Fehler:
…", und Drive/Photos-Fehler (früher still) lösen jetzt ebenfalls eine Notification aus.
Menüpunkt **„Log anzeigen…"** zeigt die Datei im Finder (`NSWorkspace`).

**Fehler-E-Mail (optional):** Bei `error`/`needs_reauth` kann `engine._maybe_send_problem_email`
eine Mail verschicken — **nur bei neuem/geändertem Problem** (kein Spam bei wiederholtem
gleichem Fehler). Versand über `notify.send_mail` per **einfachem SMTP** an ein lokales Relay
(Default `127.0.0.1:2525` → Projekt **MailRelay**, das Upstream-Auth/TLS/Retry übernimmt; kein
Auth/TLS auf dem Loopback-Hop). Konfiguration in `Settings` (`error_email_enabled`,
`error_email_to`, `error_email_from`, `smtp_host`, `smtp_port`) bzw. im Menü unter
**„Fehler-E-Mail …"** (Aktiv-Toggle, Empfänger, Relay-Host, Relay-Port, Test-E-Mail). Adresse
liegt in `settings.json` (App Support), nie im Repo.

## Config-Sicherung (`config/backup.py`)

Gesichert werden nur `settings.json` + `users.json` — **ohne Geheimnisse**: Passwörter
liegen im Keychain (nicht in diesen Dateien), Session-Tokens (`sessions/`) werden bewusst
**nicht** mitgesichert (2FA-umgehend + per Re-Auth regenerierbar). Ein Import verlangt daher
ggf. erneutes Setzen der Passwörter.

- **Automatisch:** `engine.run_user` kopiert die Config nach jedem Mount-Check nach
  `<dest>/_config-backup/` (`backup_config_to`) — die UNAS-Snapshots versionieren sie. Der
  Ordner liegt außerhalb von `Drive/`/`Photos/`/`Mail/` und ist damit vom Prune unberührt.
- **Manuell:** Menü **„Konfiguration …"** → „Exportieren…"/„Importieren…" (NSOpenPanel).
  Import lädt Settings/Users neu und baut das Menü auf.

## Bekannte Fallstricke (im Code berücksichtigt)

1. **Session-Ablauf / 2FA-Re-Auth** – siehe oben. Häufigster Ausfallgrund.
2. **Apple-Throttling** – exponentielles Backoff (`util.with_retries`), Retry-Limit.
3. **Gatekeeper/Quarantäne** – unsigniertes/ad-hoc-signiertes `.app` → README:
   Rechtsklick→Öffnen bzw. `xattr -dr com.apple.quarantine`.
4. **Keychain-Prompts bei Updates** – Ursache: **in-process-Zugriff** band „Immer erlauben" an
   die App-Code-Identität, die bei jedem Rebuild wechselt (self-signed, keine Team-ID). Eine
   stabile (self-signed) Signatur hilft **Gatekeeper**, aber **nicht** dem Keychain über Rebuilds.
   Abhilfe (umgesetzt): Zugriff über das Apple-signierte **`/usr/bin/security`** (stabile
   Identität, Einträge mit `-T /usr/bin/security`) — nach einmaligem „Immer erlauben" für
   `security` ist dauerhaft Ruhe. Siehe „Credentials".
5. **UNAS-Mount fehlt** – `engine.is_mount_available` prüft vor dem Sync; sonst sauberer
   Abbruch + Notification, kein Crash.
5b. **Netz nach Reboot noch nicht oben** – Autostart + sofort feuernder rumps-Timer würden
   ins tote Netz/DNS laufen (`Request failed to iCloud`, `[Errno 8] nodename…`). Abgefangen
   durch Start-Gnadenfrist (`startup_delay_seconds`) + `engine.is_online`-Check: offline ⇒
   stiller Skip ohne `error`/Mail, `last_run` unverändert (Retry nächster Tick).
6. **Freier Speicher** – `engine._check_free_space` warnt (< 2 GiB), bricht aber nicht ab.
7. **pyicloud-API-Drift** – der *Import* ist auf `auth/session.py` beschränkt (Auth/2FA/
   Exceptions). **Aber:** `sync/drive.py` und `sync/photos.py` hängen an pyicloud-Objekt-Shapes
   (`node.get_children/type/size/date_modified/open`, `api.photos.all`,
   `asset.versions/download_url/download`, `api.session.get`) — ein Upgrade kann Drive/Photos
   also brechen, obwohl sie pyicloud nicht importieren. Die mock-basierten Tests fangen das
   **nicht**. Siehe „pyicloud aktualisieren".
8. **Spiegel-Löschen** – `prune_extra` nur bei vollständigem, fehlerfreiem Listing
   (Guards in jedem Sync-Modul). Niemals löschen bei Teil-/Fehlerlauf.
9. **Contacts-API liefert kurzzeitig VERALTETE Daten** (kein Bug bei uns). Am 2026-08-03
   beobachtet: 16 Kontakte um 14:37–14:47 auf einem anderen Gerät geändert; icloud.com zeigte
   sie sofort, die `/co/`-API aber noch ~15 min lang den alten Stand (inkl. alter Anzahl und
   alter `dateModified`). Erst danach kippte sie um. **Konsequenz für die Fehlersuche:** „Der
   Account hat die Änderung nicht" lässt sich mit dieser API **nicht** beweisen — ein
   Re-Login/`refresh_client` hilft nicht, weil `/co/startup` und `/co/contacts` denselben
   veralteten Stand liefern. Maßgeblich ist icloud.com; weicht die API ab, schlicht später
   erneut messen statt Code zu ändern.

## pyicloud aktualisieren

`pyicloud` ist auf `==2.6.4` gepinnt (`requirements.txt`) — von allein passiert **nichts**;
`pip install` und der `.app`-Build ziehen weiterhin exakt diese Version. Ein Upgrade ist daher
eine **bewusste** Aktion und nicht durch die mock-basierten Tests abgesichert. Vorgehen:

1. Pin in `requirements.txt`/`requirements-build.txt` erhöhen, `.venv/bin/pip install -r requirements-build.txt`.
2. `.app` neu bauen (`bash build/build.sh`) und auf **Import-/Bundle-Fehler** achten — ein
   Update kann transitive Deps ändern (vgl. den `charset_normalizer`-mypyc-`.so`-Sonderfall in
   `build/setup.py`; ggf. `packages`-Liste anpassen).
3. **Echter Smoke-Test gegen einen Account** (nicht nur Mocks): Login + 2FA, ein Drive-File, ein
   Photo. Bei Fehlern zuerst `auth/session.py` (Auth/Exceptions), dann `sync/drive.py` /
   `sync/photos.py` (Objekt-Shapes, Fallstrick #7) auf `AttributeError`/geänderte Namen prüfen.

## TCC / Berechtigungen

Durch den Web-API-/IMAP-Weg **kein** Photos-Library- oder Full-Disk-Access nötig. Nur
Schreibzugriff aufs (gemountete) Ziel-Volume und Keychain.

## Verpackung zum `.app` (`build/setup.py`)

py2app, Entrypoint `launcher.py`:

- `LSUIElement = True`; Bundle-ID `de.nicx.icloud-sync`, Name „iCloud Sync", Version 0.1.0
- `iconfile` = `build/icon.icns` (falls vorhanden)
- Sonderfall eingebaut: `charset_normalizer`-mypyc-`.so` wird explizit ins Bundle kopiert
- Login-Autostart als In-App-Toggle (LaunchAgent, nur im gebauten Bundle wirksam)
- `build/build.sh` signiert mit `CODESIGN_IDENTITY` (Default `-` = ad-hoc); stabile
  (self-signed) Identität setzen, um wiederkehrende Keychain-Abfragen zu vermeiden (Fallstrick #4)
- Build-Befehl + Signierung in README dokumentiert

## Tests

`tests/test_sync.py` — eigenständiges, mock-basiertes Skript (kein Netz, kein Account;
`HOME` zeigt auf ein Temp-Verzeichnis). Deckt ab: Drive/Photos/Mail/Contacts inkrementell + Skip
im 2. Lauf, Spiegel-Löschen, alle Lösch-Guards, Photos-Kollision + Live, Mail
readonly/PEEK/UIDVALIDITY/Move/Auth-Fehler, Engine-Resilienz.

```
.venv/bin/python tests/test_sync.py
```

## Status (Definition of Done – Phase 1, erfüllt)

- [x] `.app` baut, doppelklickbar, erscheint in Menüleiste (Template-Icon)
- [x] User anlegen/konfigurieren/löschen, Credentials im Keychain (Web + Mail)
- [x] pyicloud-Login inkl. 2FA-Erstauth pro User
- [x] Re-Auth-Erkennung + Notification + Re-Auth-Flow im UI
- [x] Drive/Photos/Mail/Contacts als Datei-Spiegel auf UNAS Pro (resumebar, geguarded)
- [x] Periodischer Scheduler (Stunden-Intervall Default 4 h **oder** feste Uhrzeiten) mit Missed-Run-Catch-up
- [x] „Sync jetzt"-Button, Live-Status/Fortschritt pro User
- [x] README: Build, Ad-hoc-Signing, Gatekeeper, Mount-Voraussetzung

## Offene Punkte / Nächste Schritte (Stand zum Session-Wechsel)

Noch **nicht gegen einen echten Account** verifiziert (Logik nur mock-getestet, Fallstrick #7
— die pyicloud-Objekt-Shapes können abweichen; im Zweifel ein echtes Beispiel-Response prüfen):

- **Geteilte Mediathek** (`sync_shared_photos` → `SharedPhotos/`, `photos._shared_album_sources`):
  liefert `api.photos.libraries` die `SharedSync-`-Zone? Hat die Shared-`PhotoLibrary`
  `asset_type=PhotoAsset`? Lädt `lib.all` + `api.session.get` die Shared-Assets?
- **Drive-Ordnerliste-Fetch** (`session.list_drive_top_level`): zeigt „Ordnerliste aktualisieren"
  die obersten Ordner korrekt? (für die Drive-Ausschluss-Häkchen).
- **Contacts-vCard** (`sync/contacts._vcard`): Feld-Mapping gegen echte `api.contacts.all`-Dicts
  prüfen; JSON ist verlustfrei, vCard ggf. nachschärfen. Foto/Apple-Extensions bewusst nur im JSON.

Deployment/Betrieb (kein Code):
- **Stabile Signatur**: self-signed Zert anlegen, mit `CODESIGN_IDENTITY=…` bauen → keine
  wiederkehrenden Keychain-Prompts mehr (Fallstrick #4).
- **/Applications + Autostart**: App nach `/Applications` kopieren, von dort starten, „Beim Login
  starten" aus dieser Instanz togglen (LaunchAgent-Pfad zeigt sonst auf `dist/`).
- **Reboot-Verhalten** nach Start-Gnadenfrist/Offline-Erkennung im Feld beobachten.
