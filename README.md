# iCloud Sync

Native macOS-**Menüleisten-App**, die regelmäßig (konfigurierbar: Stunden-Intervall, Default alle 4 h,
oder feste Uhrzeiten) die iCloud-Daten
**mehrerer Apple-Accounts** auf ein Netzlaufwerk (z. B. UNAS Pro) spiegelt:

- **iCloud Drive** (Dokumente)
- **iCloud Photos** (Originale, inkl. Live Photos)
- **iCloud Mail** (alle Ordner, als `.eml`)
- **iCloud Contacts** (vCard `.vcf` + verlustfreies Roh-`.json` je Kontakt)

Drive und Photos laufen über die inoffizielle iCloud-Web-API
([pyicloud](https://github.com/timlaing/pyicloud)); Mail über **IMAP** (Bordmittel `imaplib`).
So lassen sich mehrere Accounts aus einem Prozess sichern — ohne PhotoKit, Full-Disk-Access oder
pro Account einen eigenen macOS-User.

## Sync-Modell (Spiegel, kein additives Backup)

Der lokale Stand ist ein **Spiegel des aktuellen iCloud-Zustands**: Was in iCloud gelöscht oder
verschoben wird, wird beim nächsten Lauf **auch lokal entfernt/verschoben** (kein Duplikat-Wuchs).
**Versionierung/Historie übernehmen UNAS-Snapshots** (copy-on-write) — die zeigen jeden früheren
Stand und können einzelne Dateien/Stände wiederherstellen.

> Das weicht bewusst von der ursprünglichen Bau-Spec (`CLAUDE.md`, „additiv") ab.
>
> **Sicherheit beim Löschen:** Lokal gelöscht wird **nur** innerhalb von `Drive/`, `Photos/`, `Mail/`
> und **nur nach einem vollständigen, fehlerfreien Server-Listing**. Bei Verbindungs-/Listing-Fehler
> oder unplausibel leerem Ergebnis wird **nichts** gelöscht (nur geladen). Es gibt **kein** sqlite —
> das Dateisystem ist der Zustand (reiner Datei-Sync).

## Voraussetzungen

- macOS, dauerhaft laufender Mac (24/7 empfohlen).
- **Python 3.10+** (entwickelt/getestet mit Homebrew `python3.13`; das System-Python 3.9 ist zu alt).
- Kein Account mit *Advanced Data Protection* (sonst ist die Web-API nicht nutzbar).
- Schreibzugriff auf das (gemountete) Ziel-Volume + Keychain. **Kein** Photos-Library- oder
  Full-Disk-Access nötig.

## Setup (Entwicklung / Betrieb ohne .app)

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.app        # startet die Menüleisten-App
```

Beim ersten Start erscheint das Menüleisten-Icon. Alle Einstellungen laufen über das native
**Einstellungs-Fenster** (Menü **„Einstellungen…"**) mit Tabs **Allgemein**, **Sync-Plan**,
**Fehler-E-Mail** und **Accounts** — so sieht man den gesetzten Zustand auf einen Blick statt in
einer Popup-Kette. Im Tab **Accounts** legt **„Hinzufügen"** einen Apple-Account an: Apple-ID,
Ziel-Ordner (per **Finder-Dialog**) und die Auswahl Drive/Photos/Geteilt/Kontakte/Mail. Für
**Drive/Photos/Kontakte** wird das Apple-ID-Passwort abgefragt (nur Keychain) und Apple verlangt eine
**2FA-Bestätigung** — der Code wird in einem Dialog eingegeben; die Trusted-Session wird danach
persistiert (`…/sessions/<apple-id>/`), sodass folgende Starts meist ohne erneute 2FA auskommen. Für
**Mail** wird stattdessen ein **app-spezifisches Passwort** abgefragt (siehe unten). Ziel-Ordner und
Dienste lassen sich später über **„Bearbeiten…"** im selben Tab anpassen.

> Im Menüleisten-Menü selbst bleiben nur die häufigen Aktionen (pro Account „Sync jetzt",
> „Alle jetzt synchronisieren", Auto-Sync pausieren, „Einstellungen…", „Log anzeigen…").

### iCloud Mail (IMAP) einrichten

Apple lässt IMAP-Zugriff nur mit einem **app-spezifischen Passwort** zu (das normale Passwort wird
abgelehnt). Einmal pro Account:

1. [appleid.apple.com](https://appleid.apple.com) → **Anmeldung & Sicherheit** → **App-spezifische Passwörter** → eines erzeugen.
2. In der App im **Einstellungs-Fenster → Accounts → „Mail-Passwort…"** (oder beim Anlegen) das Passwort eingeben.

Mail wird nach `Mail/<Ordner>/<uid>.eml` gespiegelt — **echte Ordnerstruktur** wie in iCloud Mail.
Nachrichten werden mit `BODY.PEEK[]` geladen und bleiben dadurch **ungelesen**. Das **Empfangsdatum**
(IMAP `INTERNALDATE`) wird als **Änderungs- und Erstellungsdatum** der Datei gesetzt (auf macOS via
`setattrlist`), sodass die Finder-Spalten und das Sortieren nach Datum die Empfangszeit zeigen (das
vollständige Datum steckt ohnehin im `Date:`-Header jeder `.eml`). Der Mail-Sync läuft **unabhängig**
von der Drive/Photos-Web-Session (auch wenn die gerade ein Re-Auth braucht).

### Status am Menüleisten-Icon

Das Icon zeigt auf einen Blick, ob der Auto-Sync läuft: **gefüllte Wolke** = Auto-Sync aktiv,
**umrandete Wolke** = pausiert. Über das Menü **„Auto-Sync pausieren/fortsetzen"** lässt er sich
anhalten — geplante Läufe unterbleiben dann, **„Sync jetzt" bleibt aber manuell möglich**. Ein
**rotes Badge** signalisiert zusätzlich `error`/`needs_reauth`, ein Spinner einen laufenden Sync.

### Sync-Zeitplan: Intervall oder feste Uhrzeiten

Im **Einstellungs-Fenster → Sync-Plan** wählt man zwischen **Stunden-Intervall** (Default alle 4 h)
und **festen Uhrzeiten**: Häkchen „Feste Uhrzeiten verwenden" setzen und die Zeiten (lokale Wandzeit,
`HH:MM`, durch Komma getrennt — z. B. `07:30, 19:30`) eintragen; dann gilt **statt** des Intervalls
der Uhrzeit-Plan. Häkchen aus ⇒ zurück zum Intervall. Der Scheduler prüft alle 5 min, feuert also
je Slot **einmal** innerhalb von ≤5 min nach der genannten Zeit. War der Mac zur Zeit im Sleep,
wird der verpasste Slot beim nächsten Aufwachen **einmalig** nachgeholt (Catch-up).

Nach einem **Reboot** wartet die App eine kurze Gnadenfrist (`startup_delay_seconds`, Default 90 s)
und prüft die iCloud-Erreichbarkeit: Ist das Netz/DNS noch nicht oben, wird der Lauf **still
übersprungen** (kein Fehler, keine Fehler-E-Mail) und beim nächsten Tick erneut versucht — kein
Warten bis zum nächsten regulären Intervall.

### Re-Auth

Apple-Sessions laufen periodisch ab (~2 Monate). Erkennt die App das, setzt sie den User-Status auf
`needs_reauth`, zeigt einen **roten Indikator** im Menüleisten-Icon und schickt eine Notification.
Über **Einstellungs-Fenster → Accounts → „Re-Auth…"** wird mit einem neuen 2FA-Code die Session
erneuert. Andere User laufen davon unbeeinflusst weiter.

## Daten & Pfade

| Zweck | Ort |
|-------|-----|
| Globale Settings | `~/Library/Application Support/icloud-sync/settings.json` |
| User-Liste (ohne Passwort) | `~/Library/Application Support/icloud-sync/users.json` |
| Log-Datei (rotierend) | `~/Library/Application Support/icloud-sync/logs/icloud-sync.log` |
| Trusted-Session-Cookies | `~/Library/Application Support/icloud-sync/sessions/<apple-id>/` |
| Apple-ID-Passwort (Drive/Photos) | macOS-Keychain (Service `icloud-sync`) |
| App-spezifisches Passwort (Mail) | macOS-Keychain (Service `icloud-sync-mail`) |

### Logging & Fehlerdiagnose

Alle Läufe werden in eine **rotierende Log-Datei** geschrieben
(`…/logs/icloud-sync.log`, 1 MB × 5) — in der Menüleisten-App die einzige verlässliche
Quelle (stderr ist dort verloren). Menüpunkt **„Log anzeigen…"** öffnet sie im Finder.
Schlägt ein Dienst fehl, steht der **Grund im Klartext** im Account-Menü („⚠️ Letzter Fehler: …"),
in der Accounts-Tabelle des Einstellungs-Fensters und in einer Notification (auch Drive/Photos-Fehler).

**Fehler-E-Mail (optional):** Im **Einstellungs-Fenster → Fehler-E-Mail** lässt sich eine
Benachrichtigung per Mail aktivieren (Empfänger/Absender sowie **Relay-Host/-Port** setzen,
„Test-E-Mail senden" zum Prüfen).
Bei `error`/`needs_reauth` geht dann eine Mail raus — **nur bei neuem/geändertem Problem**
(kein Spam bei wiederholtem gleichem Fehler). Der Versand läuft per einfachem SMTP an ein
**lokales Mail-Relay** (Default `127.0.0.1:2525`, z. B. das Projekt
[MailRelay](https://github.com/nicx/mailrelay)), das Upstream-Auth/TLS/Retry übernimmt — die
App selbst kennt keine Mail-Zugangsdaten. Einstellungen (`error_email_to`, `smtp_host`,
`smtp_port` …) liegen in `settings.json`.

### Konfiguration sichern

`settings.json` + `users.json` (beide **ohne Passwörter** — die liegen im Keychain) werden
bei jedem Lauf automatisch nach `<dest>/_config-backup/` kopiert (von den UNAS-Snapshots
versioniert). Zusätzlich im **Einstellungs-Fenster → Allgemein** → „Exportieren…/Importieren…" für den
Umzug auf einen neuen Mac. Session-Tokens werden bewusst **nicht** gesichert; nach einem
Import sind die Passwörter ggf. neu zu setzen.

### Backup-Ablage auf dem Ziel-Volume

```
<dest_base_path>/
  Drive/<originale Ordnerstruktur>/...          # 1:1-Spiegel des iCloud-Drive-Baums
  Photos/<JJJJ>/<MM>/<kurz-id>_<dateiname>      # persönliche Mediathek; nach Erstelldatum
  SharedPhotos/<JJJJ>/<MM>/<kurz-id>_<dateiname> # geteilte Mediathek (nur wenn aktiviert)
  Mail/<Ordner>/<uid>.eml                       # echte iCloud-Ordnerstruktur, rohe RFC822-Mails
  Contacts/<name>_<id>.vcf | .json              # vCard (importierbar) + verlustfreies Roh-JSON
```

**Kontakte:** Pro Kontakt eine **vCard** (`.vcf`, importierbar) **und** das **Roh-JSON**
(`.json`, verlustfrei). Aktivierbar beim Anlegen oder über **Accounts → „Bearbeiten…"** (Häkchen
„Kontakte"); nutzt die Web-Session (kein Extra-Passwort). Spiegel mit Schutz gegen Massenlöschen (leeres/
fehlerhaftes Ergebnis ⇒ kein Löschen).

**Geteilte Mediathek:** Standardmäßig wird nur die **persönliche** Mediathek gesichert. Über
**Accounts → „Bearbeiten…"** (Häkchen „Geteilte Mediathek") lässt sich zusätzlich die iCloud Shared
Photo Library nach `SharedPhotos/` spiegeln (getrennter Prune). Da Familienmitglieder sich **dieselbe**
geteilte Bibliothek teilen, sollte das nur bei **einem** Account aktiviert werden, sonst wird sie
doppelt gesichert. (Ohne diese Option verschwindet ein von „Persönlich" nach „Gemeinsam"
verschobenes Foto aus dem `Photos/`-Spiegel.)

**Geteilte Drive-Ordner:** Ein Ordner, den du besitzt und mit jemandem teilst, erscheint auch
in **dessen** Account (als „mit mir geteilt") und würde dort doppelt gesichert. Über
**Einstellungs-Fenster → Accounts → „Drive-Ausschlüsse…"** (Account wählen) werden die obersten
Drive-Ordner **live geladen** und per Häkchen vom Sync ausgenommen — typischerweise auf dem
Account des Mitnutzers die geteilten Ordner, sodass nur der **Besitzer** sie sichert. ⚠️ Ausgeschlossene Ordner werden beim nächsten Lauf **lokal aus dem
Spiegel entfernt** (das ist gewollt — so verschwindet die Dublette).

Der Sync ist **inkrementell** (Drive: Vergleich über Größe/Änderungszeit; Photos/Mail: Existenz der
Zieldatei), **resumebar** (Download nach `.part` + atomarer Rename) und ein **Spiegel** (in iCloud
Gelöschtes/Verschobenes wird nachgezogen — Historie via Snapshots). Live Photos werden als Foto **und**
Video gesichert. Bei Throttling greift exponentielles Backoff.

## Tests

```bash
.venv/bin/python tests/test_sync.py    # Mock-basiert, kein Netzwerk/Account nötig
```

## Build zum `.app`-Bundle (py2app)

**Voraussetzung:** das venv muss existieren (einmalig, siehe [Setup](#setup-entwicklung--betrieb-ohne-app)):
`/opt/homebrew/bin/python3.13 -m venv .venv`. `build/build.sh` installiert die Build-Deps
selbst, legt das venv aber **nicht** an und bricht sonst mit Hinweis ab.

Ein Schritt (Build + Ad-hoc-Signierung):

```bash
bash build/build.sh
# Ergebnis: dist/iCloud Sync.app
```

Oder manuell:

```bash
.venv/bin/pip install -r requirements-build.txt
.venv/bin/python build/setup.py py2app --dist-dir dist --bdist-base build/_py2app
codesign --force --deep --sign - "dist/iCloud Sync.app"
```

Das Bundle ist eine reine **Menüleisten-App** (`LSUIElement` → kein Dock-Icon), Bundle-ID
`de.nicx.icloud-sync`. Standardmäßig **ad-hoc signiert** (kein Apple-Developer-Zertifikat).

### Wiederkehrende Schlüsselbund-Abfragen

Der Passwort-Zugriff läuft über das Apple-signierte **`/usr/bin/security`** (siehe
`auth/keychain.py`), nicht in-process. Dadurch hängt die Schlüsselbund-Freigabe an der
**stabilen** Identität von `security` statt an der App — beim **ersten** Lesen je Account fragt
macOS einmal (pro Eintrag `icloud-sync`/`icloud-sync-mail`); dort **„Immer erlauben"** wählen,
dann ist **dauerhaft** Ruhe, auch über alle künftigen Rebuilds/Updates.

> Hintergrund: Bei in-process-Zugriff band macOS „Immer erlauben" an die App-Code-Identität, die
> bei jedem Rebuild wechselt (self-signed, keine Apple-Team-ID) → Abfrage nach jedem Update. Eine
> stabile (self-signed) Signatur via `CODESIGN_IDENTITY` (s. u.) hilft **Gatekeeper**, löst aber
> das Keychain-Problem **nicht** — das tut der Zugriff über `security`.

### Stabile Code-Signatur (Gatekeeper)

Empfohlen, um wiederholte Gatekeeper-/„App geändert"-Hinweise zu vermeiden: mit einer **stabilen
self-signed Identität** signieren statt ad-hoc.

1. Einmalig ein **self-signed Code-Signing-Zertifikat** anlegen (*Schlüsselbundverwaltung →
   Zertifikatsassistent → „Zertifikat erstellen…"*, Name z. B. `iCloud Sync Selfsign`,
   „Selbstsigniertes Stammzertifikat", Zertifikatstyp **„Codesignatur"**).
2. Bauen mit dieser Identität:
   ```bash
   CODESIGN_IDENTITY="iCloud Sync Selfsign" bash build/build.sh
   ```

Ohne `CODESIGN_IDENTITY` wird weiterhin ad-hoc signiert.

> `pyicloud` ist bewusst auf eine feste Version gepinnt; ein Upgrade nur gezielt durchführen
> und danach einen echten Account-Smoke-Test machen (die Tests sind mock-basiert) — Details
> unter „pyicloud aktualisieren" in `CLAUDE.md`.

### Gatekeeper / Quarantäne

Ein ad-hoc/unsigniertes Bundle wird beim ersten Start von Gatekeeper blockiert. Für den
Eigengebrauch:

- **Erststart:** Rechtsklick auf die App → **Öffnen** → im Dialog erneut **Öffnen**. Danach
  startet sie künftig normal per Doppelklick.
- Falls die App aus dem Internet/von einem anderen Mac kam und das Quarantäne-Flag trägt:
  ```bash
  xattr -dr com.apple.quarantine "dist/iCloud Sync.app"
  ```

> Keychain-Abfragen sind davon unabhängig — siehe „Wiederkehrende Schlüsselbund-Abfragen" oben
> (Zugriff über `/usr/bin/security`).

### Autostart beim Login

Im **Einstellungs-Fenster → Allgemein** über „Beim Login starten" umschaltbar. Der Toggle legt einen LaunchAgent unter
`~/Library/LaunchAgents/de.nicx.icloud-sync.plist` an bzw. entfernt ihn. Er funktioniert nur
für das gebaute `.app`-Bundle (nicht im `python -m src.app`-Entwicklungsmodus).

### Voraussetzung Ziel-Volume

Vor jedem Lauf prüft die App, ob `dest_base_path` erreichbar ist. Ist das UNAS-Volume **nicht
gemountet**, bricht der Lauf für den betroffenen User sauber ab (Status `error` + Notification) –
ohne Crash, andere User laufen weiter.

## Noch offen (optional)

- Vollständige Notarisierung/Developer-ID-Signierung (für Verteilung über das eigene Gerät hinaus).

## Lizenz / Maintainer

- **Maintainer:** nicx
- **Lizenz:** MIT (siehe [LICENSE](LICENSE))
