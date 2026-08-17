"""macOS-Notifications (Re-Auth, Fehler, Erfolg) + Fehler-Mail.

Notifications laufen über ``UNUserNotificationCenter`` — die aktuelle, von Apple
unterstützte API. Sie ersetzt sowohl ``rumps.notification`` als auch den früheren
``pync``/terminal-notifier-Fallback (beide entfallen mit der pyobjc-Umstellung).

**Voraussetzung:** ein echtes, signiertes ``.app``-Bundle, gestartet über den App-Stub
``iCloud Sync.app/Contents/MacOS/iCloud Sync``. Im Dev-Modus (``python -m src.app``)
lehnt macOS die Zustellung ab — das ist erwartet und wird nur geloggt. Notifications sind
durchgängig **best effort**: ein Fehler hier darf einen Sync nie beeinflussen.
"""

from __future__ import annotations

import logging
import threading
import uuid

from .ui_appkit import run_on_main

LOGGER = logging.getLogger(__name__)

# Die Berechtigung wird genau einmal je Prozess angefragt (macOS zeigt den Dialog ohnehin
# nur beim ersten Mal; wiederholte Anfragen wären reines Rauschen).
_auth_lock = threading.Lock()
_auth_requested = False


def notify(title: str, message: str, subtitle: str | None = None) -> None:
    """Zeigt eine macOS-Notification (asynchron, best effort).

    Der Aufruf kehrt sofort zurück; die Zustellung passiert auf dem Main-Thread — wichtig,
    weil die meisten Aufrufer aus dem Sync-Hintergrund-Thread kommen. Fehler werden
    geloggt, nie geworfen.
    """
    run_on_main(_deliver, title, message, subtitle)


def _deliver(title: str, message: str, subtitle: str | None) -> None:
    """Baut die Notification und übergibt sie dem Notification-Center (Main-Thread)."""
    try:
        import UserNotifications as UN

        center = UN.UNUserNotificationCenter.currentNotificationCenter()
        _ensure_authorization(center, UN)

        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        if subtitle:
            content.setSubtitle_(subtitle)
        content.setBody_(message)

        request = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None)  # trigger=None → sofort zustellen

        def _done(error) -> None:
            if error is not None:
                LOGGER.warning("Notification nicht zugestellt: %s", error)

        center.addNotificationRequest_withCompletionHandler_(request, _done)
    except Exception as exc:  # noqa: BLE001 - Notifications sind best effort
        LOGGER.warning("Notification konnte nicht angezeigt werden: %s", exc)


def _ensure_authorization(center, UN) -> None:
    """Fragt einmalig die Zustellberechtigung an (Dialog zeigt macOS nur beim ersten Mal)."""
    global _auth_requested
    with _auth_lock:
        if _auth_requested:
            return
        _auth_requested = True

    def _granted(granted, error) -> None:
        if error is not None:
            LOGGER.warning("Notification-Berechtigung fehlgeschlagen: %s", error)
        elif not granted:
            LOGGER.info("Notification-Berechtigung vom Nutzer abgelehnt.")

    options = UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound
    center.requestAuthorizationWithOptions_completionHandler_(options, _granted)


def send_mail(host: str, port: int, sender: str, recipient: str, subject: str,
              body: str, timeout: float = 15.0) -> bool:
    """Liefert eine Mail per **einfachem SMTP** an ein lokales Relay ein (kein Auth/TLS).

    Gedacht für das MailRelay-Projekt (Default ``127.0.0.1:2525``), das selbst Upstream-Auth,
    STARTTLS und Retry/Backoff übernimmt. Best-effort: Fehler werden geloggt, nicht geworfen
    (eine nicht zustellbare Benachrichtigung darf den Sync nie beeinflussen).
    """
    try:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - Benachrichtigung ist best-effort
        LOGGER.warning("Fehler-E-Mail an %s über %s:%s fehlgeschlagen: %s",
                       recipient, host, port, exc)
        return False
