"""User-Modell und Persistenz.

Ein *User* entspricht einem Apple-Account, der gesichert werden soll. Die Liste wird
als ``users.json`` in App Support gehalten. **Passwörter werden hier nie gespeichert** —
sie liegen ausschließlich im macOS-Keychain (siehe ``auth.keychain``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from .paths import users_file

# Sentinel für "Argument nicht übergeben" (um None als gültigen Wert zuzulassen).
_UNSET = object()


class UserStatus(str, Enum):
    """Status eines Users im Backup-Lebenszyklus.

    ``str``-Enum, damit die Werte direkt JSON-serialisierbar sind.
    """

    IDLE = "idle"            # konfiguriert, aber noch kein Lauf / wartet
    RUNNING = "running"      # Sync läuft gerade
    OK = "ok"                # letzter Lauf erfolgreich, Session gültig
    NEEDS_REAUTH = "needs_reauth"  # 2FA/Session abgelaufen -> User-Eingriff nötig
    ERROR = "error"          # letzter Lauf fehlgeschlagen


@dataclass
class User:
    """Konfiguration und Laufzeit-Status eines Apple-Accounts.

    :param apple_id: Apple-ID (E-Mail). Dient als eindeutiger Schlüssel.
    :param sync_drive: iCloud Drive sichern?
    :param sync_photos: iCloud Photos (persönliche Mediathek) sichern?
    :param sync_shared_photos: zusätzlich die geteilte Mediathek sichern (nur mit sync_photos;
        Default aus — pro geteilter Bibliothek sollte nur EIN Account sichern).
    :param sync_contacts: iCloud Contacts sichern (vCard + Roh-JSON)? Default aus.
    :param sync_mail: iCloud Mail (IMAP) sichern? Default aus (braucht app-spezifisches Passwort).
    :param dest_base_path: Ziel-Basispfad auf dem (gemounteten) Volume.
    :param drive_excludes: Drive-relative Ordner (z. B. geteilte), die NICHT gesichert werden —
        ausgeschlossene Pfade werden vom Spiegel-Prune lokal entfernt.
    :param sync_times: **eigener** Uhrzeit-Plan für diesen Account (``"HH:MM"``, lokale
        Wandzeit). Leer = globaler Plan aus den Settings. Sinn: kleine Accounts (Sekunden
        pro Lauf) dürfen häufig laufen, während ein großer Account (Stunde pro Lauf)
        selten bleibt.
    :param status: aktueller :class:`UserStatus`.
    :param last_run: ISO-8601-Zeitstempel des letzten erfolgreichen Laufbeginns (oder None).
    """

    apple_id: str
    sync_drive: bool = True
    sync_photos: bool = True
    sync_shared_photos: bool = False  # geteilte iCloud-Mediathek (Add-on zu sync_photos)
    sync_contacts: bool = False       # iCloud Contacts (vCard + Roh-JSON)
    sync_mail: bool = False
    dest_base_path: str = ""
    drive_excludes: list = field(default_factory=list)  # Drive-Ordner (rel. Pfade), die NICHT gesichert werden
    sync_times: list = field(default_factory=list)      # eigener "HH:MM"-Plan; leer = globaler Plan
    status: UserStatus = UserStatus.IDLE
    last_run: Optional[str] = None
    last_error: Optional[str] = None  # Klartext-Grund des letzten Fehlers (für Menü/Notification)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "User":
        status = raw.get("status", UserStatus.IDLE.value)
        try:
            status_enum = UserStatus(status)
        except ValueError:
            status_enum = UserStatus.IDLE
        return cls(
            apple_id=raw["apple_id"],
            sync_drive=bool(raw.get("sync_drive", True)),
            sync_photos=bool(raw.get("sync_photos", True)),
            sync_shared_photos=bool(raw.get("sync_shared_photos", False)),
            sync_contacts=bool(raw.get("sync_contacts", False)),
            sync_mail=bool(raw.get("sync_mail", False)),
            dest_base_path=raw.get("dest_base_path", ""),
            drive_excludes=list(raw.get("drive_excludes") or []),
            sync_times=list(raw.get("sync_times") or []),
            status=status_enum,
            last_run=raw.get("last_run"),
            last_error=raw.get("last_error"),
        )


@dataclass
class UsersStore:
    """In-Memory-Liste der User mit JSON-Persistenz.

    Vor dem ersten Zugriff :meth:`load` aufrufen (oder ``UsersStore.loaded()`` nutzen).
    Schreibende Operationen persistieren sofort.
    """

    users: list[User] = field(default_factory=list)

    @classmethod
    def loaded(cls) -> "UsersStore":
        """Erzeugt einen Store und lädt vorhandene ``users.json``."""
        store = cls()
        store.load()
        return store

    def load(self) -> None:
        path = users_file()
        if not path.exists():
            self.users = []
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.users = []
            return
        self.users = [User.from_dict(u) for u in raw.get("users", [])]

    def save(self) -> None:
        path = users_file()
        tmp = path.with_suffix(".json.tmp")
        payload = {"users": [u.to_dict() for u in self.users]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    # --- CRUD ---------------------------------------------------------------

    def list(self) -> list[User]:
        return list(self.users)

    def get(self, apple_id: str) -> Optional[User]:
        for u in self.users:
            if u.apple_id == apple_id:
                return u
        return None

    def add(self, user: User) -> None:
        """Fügt einen User hinzu. Doppelte Apple-ID -> ValueError."""
        if self.get(user.apple_id) is not None:
            raise ValueError(f"User existiert bereits: {user.apple_id}")
        self.users.append(user)
        self.save()

    def update(self, user: User) -> None:
        """Ersetzt einen vorhandenen User (Match per ``apple_id``)."""
        for i, u in enumerate(self.users):
            if u.apple_id == user.apple_id:
                self.users[i] = user
                self.save()
                return
        raise KeyError(f"Unbekannter User: {user.apple_id}")

    def set_status(self, apple_id: str, status: UserStatus, last_run: Optional[str] = None,
                   last_error: object = _UNSET) -> None:
        """Bequemer Helfer: Status (und optional last_run/last_error) eines Users aktualisieren.

        ``last_error`` nutzt einen Sentinel-Default: nur wenn explizit übergeben, wird es
        gesetzt (``None`` löscht den Grund, ein String setzt ihn). So überschreiben Aufrufer,
        die nur den Status ändern, den letzten Fehlergrund nicht versehentlich.
        """
        u = self.get(apple_id)
        if u is None:
            raise KeyError(f"Unbekannter User: {apple_id}")
        u.status = status
        if last_run is not None:
            u.last_run = last_run
        if last_error is not _UNSET:
            u.last_error = last_error  # type: ignore[assignment]
        self.save()

    def remove(self, apple_id: str) -> None:
        before = len(self.users)
        self.users = [u for u in self.users if u.apple_id != apple_id]
        if len(self.users) != before:
            self.save()
