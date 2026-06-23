"""Credential-Storage im macOS-Keychain über das Apple-Tool ``/usr/bin/security``.

Passwörter werden ausschließlich hier abgelegt — nie in ``users.json`` oder im Klartext.
Service-Name ist konstant, der Account-Schlüssel ist die Apple-ID.

**Warum nicht ``keyring`` (in-process)?** Bei in-process-Zugriff bindet macOS das „Immer
erlauben" an die **Code-Identität der App**. Da die App **self-signed ohne Apple-Team-ID** ist,
ändert sich diese Identität bei **jedem Rebuild** (neuer cdhash) → der Schlüsselbund fragt nach
jedem Update erneut. Indem der Zugriff über das **Apple-signierte ``/usr/bin/security``** läuft
(eigener Prozess, **stabile** Identität) und die Einträge mit ``-T /usr/bin/security`` angelegt
werden, hält „Immer erlauben" **dauerhaft** — unabhängig davon, wie oft die App neu gebaut wird.

Migration: Frühere Einträge (vom alten ``keyring``-Backend bzw. den Alt-Services
``icloud-backup`` / ``icloud-backup-mail``) werden beim ersten Lesen transparent auf den neuen
Service + die ``security``-ACL umgezogen. Beim allerersten Lesen eines Alt-Eintrags kann macOS
**einmal** nachfragen (dann „Immer erlauben" für ``security`` wählen) — danach ist Ruhe.
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
from typing import Optional

LOGGER = logging.getLogger(__name__)

SECURITY = "/usr/bin/security"
_B64_PREFIX = "b64:"   # Marker: so abgelegte Werte sind reines ASCII (kein security-Hex-Problem)
_HEX_RE = re.compile(r"[0-9a-fA-F]+")

# Einheitliche Keychain-Services. Stabil halten — Änderungen "verlieren" gespeicherte Passwörter.
KEYCHAIN_SERVICE = "icloud-sync"            # reguläres Apple-ID-Passwort (Web-API: Drive/Photos)
KEYCHAIN_SERVICE_MAIL = "icloud-sync-mail"  # app-spezifisches Passwort (IMAP/Mail)

_LEGACY_SERVICE = "icloud-backup"
_LEGACY_SERVICE_MAIL = "icloud-backup-mail"


def set_password(apple_id: str, password: str) -> None:
    """Speichert das Apple-ID-Passwort eines Accounts im Keychain."""
    _store(KEYCHAIN_SERVICE, apple_id, password)


def get_password(apple_id: str) -> Optional[str]:
    """Liest das Apple-ID-Passwort eines Accounts aus dem Keychain (oder ``None``)."""
    return _get_with_migration(KEYCHAIN_SERVICE, _LEGACY_SERVICE, apple_id)


def delete_password(apple_id: str) -> None:
    """Entfernt das Apple-ID-Passwort eines Accounts aus dem Keychain (idempotent)."""
    _delete(KEYCHAIN_SERVICE, apple_id)
    _delete(_LEGACY_SERVICE, apple_id)


def set_mail_password(apple_id: str, app_password: str) -> None:
    """Speichert das app-spezifische Passwort (IMAP/Mail) eines Accounts im Keychain."""
    _store(KEYCHAIN_SERVICE_MAIL, apple_id, app_password)


def get_mail_password(apple_id: str) -> Optional[str]:
    """Liest das app-spezifische Mail-Passwort eines Accounts aus dem Keychain (oder ``None``)."""
    return _get_with_migration(KEYCHAIN_SERVICE_MAIL, _LEGACY_SERVICE_MAIL, apple_id)


def delete_mail_password(apple_id: str) -> None:
    """Entfernt das app-spezifische Mail-Passwort eines Accounts (idempotent)."""
    _delete(KEYCHAIN_SERVICE_MAIL, apple_id)
    _delete(_LEGACY_SERVICE_MAIL, apple_id)


# -- intern: Zugriff über /usr/bin/security ---------------------------------

def _store(service: str, account: str, password: str) -> None:
    """Legt/aktualisiert den Eintrag an; ``/usr/bin/security`` ist einziger Trust-Accessor.

    Erst löschen, dann neu anlegen — so wird die ACL frisch auf ``-T /usr/bin/security``
    gesetzt (sonst behielte ein bestehender Eintrag seine alte ACL). Der Wert wird
    base64-kodiert (mit ``b64:``-Marker) abgelegt: so ist er **reines ASCII** und
    ``security -w`` liefert ihn verlustfrei zurück (sonst Hex-Kodierung bei Nicht-ASCII).
    """
    payload = _B64_PREFIX + base64.b64encode(password.encode("utf-8")).decode("ascii")
    _run(["delete-generic-password", "-a", account, "-s", service])
    res = _run(["add-generic-password", "-a", account, "-s", service, "-w", payload,
                "-T", SECURITY, "-U"])
    if res is None or res.returncode != 0:
        LOGGER.warning("Keychain-Eintrag für %s (%s) konnte nicht gespeichert werden.", account, service)


def _read_raw(service: str, account: str) -> Optional[str]:
    res = _run(["find-generic-password", "-a", account, "-s", service, "-w"])
    if res is None or res.returncode != 0:
        return None
    out = res.stdout
    return out[:-1] if out.endswith("\n") else out  # -w hängt ein \n an


def _decode(raw: str) -> Optional[str]:
    """Dekodiert einen gelesenen Rohwert: neues ``b64:``-Format oder Alt-Format (roh/Hex)."""
    if raw.startswith(_B64_PREFIX):
        try:
            return base64.b64decode(raw[len(_B64_PREFIX):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    # Alt-Eintrag (vom früheren keyring-Backend): security -w kann Nicht-ASCII als Hex liefern.
    if raw and len(raw) % 2 == 0 and _HEX_RE.fullmatch(raw):
        try:
            return bytes.fromhex(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass  # doch kein Hex -> als Klartext behandeln
    return raw


def _get_with_migration(service: str, legacy: str, apple_id: str) -> Optional[str]:
    """Liest ``service``; fällt auf ``legacy`` zurück. Alt-Format wird beim Lesen normalisiert.

    Defensiv: schlägt der Zugriff fehl, ``None`` statt Exception — ein Keychain-Problem
    kippt so nicht den ganzen Sync-Lauf, sondern führt nur zu „Passwort fehlt".
    """
    raw = _read_raw(service, apple_id)
    if raw is not None:
        value = _decode(raw)
        if value is not None and not raw.startswith(_B64_PREFIX):
            _store(service, apple_id, value)  # Alt-Format -> auf b64 + security-ACL normalisieren
        return value
    raw_legacy = _read_raw(legacy, apple_id)
    if raw_legacy is None:
        return None
    value = _decode(raw_legacy)
    if value is not None:
        _store(service, apple_id, value)  # auf neuen Service + security-ACL umziehen
        _delete(legacy, apple_id)
    return value


def _delete(service: str, account: str) -> None:
    _run(["delete-generic-password", "-a", account, "-s", service])


def _run(args: list[str]) -> Optional[subprocess.CompletedProcess]:
    """Ruft ``/usr/bin/security`` auf; best-effort (kein Werfen, Fehler werden geloggt)."""
    try:
        return subprocess.run([SECURITY, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        LOGGER.warning("security-Aufruf fehlgeschlagen (%s): %s", args[0] if args else "?", exc)
        return None
