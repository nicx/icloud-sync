"""Globale App-Settings, als JSON in App Support persistiert.

Bewusst getrennt von der User-Liste (``users.py``): hier stehen nur prozessweite
Einstellungen wie das Sync-Intervall.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .paths import settings_file

# Default-Sync-Intervall. Nutzer-Entscheidung: konfigurierbar, nicht fix täglich.
DEFAULT_SYNC_INTERVAL_HOURS = 4


@dataclass
class Settings:
    """Prozessweite Einstellungen.

    :param sync_interval_hours: Mindestabstand zwischen zwei Sync-Läufen je User.
    :param sync_times: feste Sync-Uhrzeiten (lokale Wandzeit, ``"HH:MM"``). Leer ⇒ es gilt
        ``sync_interval_hours``; gesetzt ⇒ feste Uhrzeiten **statt** Intervall.
    :param autostart: Beim Login automatisch starten (Verdrahtung folgt späterer Durchgang).
    :param notifications: macOS-Notifications aktiviert.
    :param auto_sync_paused: Auto-Sync pausiert (Scheduler stößt keine Läufe an; Icon umrandet).
    :param startup_delay_seconds: Gnadenfrist nach App-Start, bevor der erste Auto-Sync läuft
        (gibt dem Netzwerk/DNS nach einem Reboot Zeit; manueller „Sync jetzt" ignoriert sie).
    :param error_email_enabled: bei Fehler/Re-Auth eine E-Mail verschicken (über lokales Relay).
    :param error_email_to: Empfänger der Fehler-Mails (leer = aus).
    :param error_email_from: Absender; leer ⇒ es wird ``error_email_to`` genutzt.
    :param smtp_host/smtp_port: lokales Mail-Relay (Default: MailRelay-Projekt, 127.0.0.1:2525).
    """

    sync_interval_hours: int = DEFAULT_SYNC_INTERVAL_HOURS
    sync_times: list[str] = field(default_factory=list)  # leer ⇒ Intervall; sonst feste Uhrzeiten (lokal)
    autostart: bool = False
    notifications: bool = True
    auto_sync_paused: bool = False  # True ⇒ Scheduler stößt keine Läufe an (Icon umrandet)
    startup_delay_seconds: int = 90  # Gnadenfrist nach App-Start, bevor automatisch gesynct wird
    error_email_enabled: bool = False
    error_email_to: str = ""
    error_email_from: str = ""
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 2525


def load_settings() -> Settings:
    """Lädt die Settings; bei fehlender/kaputter Datei werden Defaults zurückgegeben.

    Unbekannte Felder in der JSON werden ignoriert, damit alte Dateien nach einem
    Schema-Zuwachs nicht brechen.
    """
    path = settings_file()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Settings()
    known = {f for f in Settings.__dataclass_fields__}
    filtered = {k: v for k, v in raw.items() if k in known}
    return Settings(**filtered)


def save_settings(settings: Settings) -> None:
    """Schreibt die Settings atomar-genug (write + replace) als JSON."""
    path = settings_file()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    tmp.replace(path)
