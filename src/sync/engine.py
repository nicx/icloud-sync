"""Sync-Orchestrierung pro User.

Eigenständig (auch ohne UI) aufrufbar – für Tests/Debug und vom Scheduler in ``app.py``.

Ablauf je User: Mount prüfen -> Passwort aus Keychain -> Login (ggf. Re-Auth nötig) ->
Drive- und/oder Photos-Sync gemäß User-Flags -> Status/last_run aktualisieren.
Ein Fehler bei einem User darf die anderen nicht stoppen.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
from datetime import datetime, timezone
from typing import Optional

from .. import notify
from ..auth import keychain, session
from ..config.backup import BACKUP_DIRNAME, backup_config_to
from ..config.paths import logs_dir
from ..config.settings import load_settings
from ..config.users import _UNSET, User, UsersStore, UserStatus
from . import contacts, drive, mail, photos
from .contacts import ContactsAuthError
from .mail import MailAuthError

LOGGER = logging.getLogger(__name__)

# Vor einem großen Lauf warnen, wenn weniger als das frei ist (reine Warnung, kein Abbruch).
_LOW_SPACE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Erreichbarkeitsprüfung (DNS + TCP) für die Offline-Erkennung direkt nach dem Boot.
_REACHABILITY_HOST = "www.icloud.com"
_REACHABILITY_PORT = 443
_REACHABILITY_TIMEOUT = 4.0


def is_mount_available(dest_base_path: str) -> bool:
    """Prüft, ob der Ziel-Basispfad existiert/erreichbar ist (Fallstrick #5: UNAS-Mount fehlt)."""
    return bool(dest_base_path) and os.path.isdir(dest_base_path)


def is_online(host: str = _REACHABILITY_HOST, port: int = _REACHABILITY_PORT,
              timeout: float = _REACHABILITY_TIMEOUT) -> bool:
    """True, wenn iCloud per DNS+TCP erreichbar ist (grobe Online-Prüfung).

    Fängt den häufigsten Reboot-Fall ab: Netz/DNS ist beim Autostart noch nicht oben
    (`Request failed to iCloud`, `[Errno 8] nodename nor servname`). Dann lieber still
    überspringen statt als Fehler zu werten.
    """
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_free_space(dest_base_path: str) -> None:
    """Loggt eine Warnung bei wenig freiem Speicher (Fallstrick #6)."""
    try:
        usage = shutil.disk_usage(dest_base_path)
        if usage.free < _LOW_SPACE_BYTES:
            LOGGER.warning("Wenig freier Speicher auf %s: %.1f GiB",
                           dest_base_path, usage.free / 1024 ** 3)
    except OSError:
        pass


def run_user(user: User, store: Optional[UsersStore] = None, progress_cb=None) -> UserStatus:
    """Führt einen kompletten Sync-Lauf für *einen* User aus.

    Setzt Status auf ``RUNNING`` und am Ende ``OK``/``ERROR``/``NEEDS_REAUTH`` und
    aktualisiert ``last_run`` bei Erfolg.

    :param progress_cb: optionaler Callback ``cb(apple_id, phase, counts)`` für Live-Fortschritt.
    """
    def _phase_cb(phase: str):
        if progress_cb is None:
            return None
        return lambda counts: progress_cb(user.apple_id, phase, counts)
    # Zustand VOR dem Lauf merken (für „nur bei neuem/geändertem Problem"-Mail).
    prev_status, prev_error = user.status, user.last_error

    def _set(status: UserStatus, last_run: Optional[str] = None,
             last_error: object = _UNSET) -> UserStatus:
        if store is not None:
            store.set_status(user.apple_id, status, last_run=last_run, last_error=last_error)
        return status

    def _finalize(status: UserStatus, last_error: Optional[str], last_run: Optional[str]) -> UserStatus:
        _set(status, last_run=last_run, last_error=last_error)
        _maybe_send_problem_email(user, status, last_error, prev_status, prev_error)
        return status

    _set(UserStatus.RUNNING)
    t0 = time.monotonic()
    LOGGER.info("[%s] Sync gestartet", user.apple_id)

    # 1) Ziel-Volume gemountet?
    if not is_mount_available(user.dest_base_path):
        msg = f"Ziel-Volume nicht gemountet: {user.dest_base_path}"
        LOGGER.warning("[%s] %s", user.apple_id, msg)
        notify.notify("iCloud Sync – Ziel fehlt",
                    f"{user.apple_id}: {user.dest_base_path} ist nicht gemountet.")
        return _finalize(UserStatus.ERROR, msg, last_run=None)

    _check_free_space(user.dest_base_path)
    # Config-Kopie aufs Ziel-Volume (ohne Passwörter) — UNAS-Snapshots versionieren sie.
    backup_config_to(os.path.join(user.dest_base_path, BACKUP_DIRNAME))

    # iCloud erreichbar? Direkt nach dem Boot ist das Netz/DNS oft noch nicht oben.
    # Dann ist das KEIN echter Fehler: Lauf still überspringen (kein error-Status, keine
    # Fehler-E-Mail) UND last_run NICHT setzen -> beim nächsten Tick erneut versuchen
    # (nicht erst in `sync_interval_hours`).
    if (user.sync_drive or user.sync_photos or user.sync_contacts or user.sync_mail) and not is_online():
        LOGGER.warning("[%s] iCloud nicht erreichbar -> Lauf übersprungen (Retry beim nächsten Tick).",
                       user.apple_id)
        return _set(UserStatus.IDLE)  # last_run/last_error unverändert, kein _finalize -> keine Mail

    reasons: list[str] = []   # gesammelte Klartext-Fehlergründe (-> last_error)
    web_reauth = False

    # 2) Web-API (Drive/Photos) – nur wenn benötigt. Contacts/Mail laufen davon unabhängig.
    if user.sync_drive or user.sync_photos:
        password = keychain.get_password(user.apple_id)
        if not password:
            msg = "Kein Apple-ID-Passwort im Keychain (Drive/Photos)"
            LOGGER.error("[%s] %s", user.apple_id, msg)
            reasons.append(msg)
        else:
            result = session.login(user.apple_id, password)
            if result.needs_2fa:
                web_reauth = True
                notify.notify("iCloud Sync – Re-Auth nötig",
                              f"{user.apple_id}: bitte erneut anmelden (Drive/Photos).")
            elif result.error or result.api is None:
                msg = result.error or "Login fehlgeschlagen (Drive/Photos)"
                LOGGER.error("[%s] %s", user.apple_id, msg)
                reasons.append(msg)
            else:
                api = result.api
                try:
                    if user.sync_drive:
                        s = drive.sync_drive(api, user.dest_base_path, user.apple_id,
                                             _phase_cb("drive"), excludes=user.drive_excludes)
                        if s.errors > 0:
                            detail = f" (z. B. {s.error_paths[0]})" if s.error_paths else ""
                            msg = f"Drive: {s.errors} Datei-Fehler{detail}"
                            reasons.append(msg)
                            notify.notify("iCloud Sync – Drive-Fehler", f"{user.apple_id}: {msg}")
                    if user.sync_photos:
                        ps = photos.sync_photos(api, user.dest_base_path, user.apple_id,
                                                _phase_cb("photos"), include_shared=user.sync_shared_photos)
                        if ps.errors > 0:
                            msg = f"Photos: {ps.errors} Fehler"
                            reasons.append(msg)
                            notify.notify("iCloud Sync – Photos-Fehler", f"{user.apple_id}: {msg}")
                except Exception as exc:  # noqa: BLE001 - harter, unerwarteter Fehler
                    LOGGER.exception("Drive/Photos-Sync für %s abgebrochen", user.apple_id)
                    reasons.append(f"Drive/Photos-Sync abgebrochen: {exc}")

    # 2b) Contacts (CardDAV) – app-spezifisches Passwort, unabhängig von der Web-Session.
    #     Die frühere Web-API lieferte zeitweise dauerhaft eingefrorene Daten (siehe
    #     sync/contacts.py); CardDAV ist das Protokoll der Kontakte-App.
    if user.sync_contacts:
        app_pw = keychain.get_mail_password(user.apple_id)
        if not app_pw:
            msg = "Kein app-spezifisches Passwort im Keychain (Contacts)"
            LOGGER.error("[%s] %s", user.apple_id, msg)
            notify.notify("iCloud Sync – App-Passwort fehlt",
                          f"{user.apple_id}: app-spezifisches Passwort für Kontakte setzen.")
            reasons.append(msg)
        else:
            try:
                cs = contacts.sync_contacts(user.apple_id, app_pw, user.dest_base_path,
                                            _phase_cb("contacts"))
                if cs.errors > 0:
                    msg = f"Contacts: {cs.errors} Fehler"
                    reasons.append(msg)
                    notify.notify("iCloud Sync – Contacts-Fehler", f"{user.apple_id}: {msg}")
            except ContactsAuthError as exc:
                msg = "Contacts-Login abgelehnt (App-Passwort prüfen/neu erzeugen)"
                LOGGER.error("[%s] %s: %s", user.apple_id, msg, exc)
                notify.notify("iCloud Sync – Contacts-Login", f"{user.apple_id}: {msg}")
                reasons.append(msg)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Contacts-Sync für %s abgebrochen", user.apple_id)
                reasons.append(f"Contacts-Sync abgebrochen: {exc}")

    # 3) Mail (IMAP) – eigene Credentials, unabhängig von der Web-Session.
    if user.sync_mail:
        app_pw = keychain.get_mail_password(user.apple_id)
        if not app_pw:
            msg = "Kein Mail-App-Passwort im Keychain"
            LOGGER.error("[%s] %s", user.apple_id, msg)
            notify.notify("iCloud Sync – Mail-Passwort fehlt",
                          f"{user.apple_id}: app-spezifisches Passwort setzen.")
            reasons.append(msg)
        else:
            try:
                ms = mail.sync_mail(user.apple_id, app_pw, user.dest_base_path, _phase_cb("mail"))
                if ms.errors > 0:
                    reasons.append(f"Mail: {ms.errors} Fehler")
            except MailAuthError as exc:
                msg = "Mail-Login abgelehnt (App-Passwort prüfen/neu erzeugen)"
                LOGGER.error("[%s] %s: %s", user.apple_id, msg, exc)
                notify.notify("iCloud Sync – Mail-Login", f"{user.apple_id}: {msg}")
                reasons.append(msg)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Mail-Sync für %s abgebrochen", user.apple_id)
                reasons.append(f"Mail-Sync abgebrochen: {exc}")

    # Status-Priorität: harte Fehler > Re-Auth > OK. last_run + last_error am Ende setzen.
    if reasons:
        status = UserStatus.ERROR
        last_error: Optional[str] = "; ".join(reasons)
    elif web_reauth:
        status = UserStatus.NEEDS_REAUTH
        last_error = "Re-Auth nötig (2FA für Drive/Photos)"
    else:
        status = UserStatus.OK
        last_error = None  # Erfolg -> alten Grund löschen
    LOGGER.info("[%s] Sync fertig in %.0fs (Status %s)",
                user.apple_id, time.monotonic() - t0, status.value)
    return _finalize(status, last_error, last_run=_now_iso())


def _maybe_send_problem_email(user: User, status: UserStatus, last_error: Optional[str],
                              prev_status: UserStatus, prev_error: Optional[str]) -> None:
    """Schickt bei einem **neuen oder geänderten** Problem (ERROR/NEEDS_REAUTH) eine Mail.

    Nur wenn in den Settings aktiviert und ein Empfänger gesetzt ist. „Neu/geändert" =
    Status hat zuvor nicht schon mit identischem Grund vorgelegen → kein Spam bei mehreren
    fehlgeschlagenen Läufen in Folge. Versand best-effort über das lokale Relay (notify.send_mail).
    """
    if status not in (UserStatus.ERROR, UserStatus.NEEDS_REAUTH):
        return
    if status == prev_status and last_error == prev_error:
        return  # unverändertes Problem -> nicht erneut mailen
    try:
        settings = load_settings()
    except Exception:  # noqa: BLE001
        return
    if not settings.error_email_enabled or not settings.error_email_to:
        return
    sender = settings.error_email_from or settings.error_email_to
    subject = f"iCloud Sync: Problem bei {user.apple_id} ({status.value})"
    body = (
        "iCloud Sync meldet ein Problem.\n\n"
        f"Account: {user.apple_id}\n"
        f"Status:  {status.value}\n"
        f"Grund:   {last_error or '—'}\n"
        f"Zeit:    {_now_iso()}\n"
        f"Ziel:    {user.dest_base_path}\n\n"
        f"Details im Log: {logs_dir() / 'icloud-sync.log'}\n"
    )
    notify.send_mail(settings.smtp_host, int(settings.smtp_port), sender,
                     settings.error_email_to, subject, body)


def run_all(store: UsersStore, progress_cb=None) -> None:
    """Sequenziell alle aktiven User durchlaufen. Ein Fehler stoppt die anderen nicht."""
    for user in store.list():
        try:
            run_user(user, store, progress_cb)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Sync-Lauf für %s abgebrochen", user.apple_id)
            store.set_status(user.apple_id, UserStatus.ERROR,
                             last_error=f"Lauf abgebrochen: {exc}")
