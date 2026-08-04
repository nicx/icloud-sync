"""iCloud Contacts – dateibasierter Sync-Spiegel über **CardDAV**.

Sichert jeden Kontakt als ``<dest_base_path>/Contacts/<name>_<kurz-id>.vcf`` — die
**Original-vCard von Apple**, unverändert übernommen (inkl. Foto und ``X-APPLE-*``-
Erweiterungen). Damit ist die Datei selbst die verlustfreie Quelle; ein zusätzliches
Roh-JSON gibt es nicht mehr.

**Warum CardDAV und nicht die Web-API** (Wechsel am 2026-08-04): Die früher genutzte
``/co/``-Web-API (``api.contacts.all``) lieferte für einen Account ab dem 2026-08-03
14:47:50 dauerhaft einen eingefrorenen Stand — über 16 h und ~16 Läufe hinweg meldete
sie „0 geändert", während icloud.com die Änderungen längst zeigte. Alle Varianten des
Dienstes (``/co/startup``, ``/co/contacts`` mit und ohne Tokens) gaben denselben alten
Stand zurück; Re-Login und ein pyicloud-Update (2.6.5 ist an dieser Stelle identisch)
halfen nicht. CardDAV — dasselbe Protokoll, das die Kontakte-App nutzt — hatte alle
Änderungen korrekt. Ein dokumentiertes Standardprotokoll ist hier also nicht nur
zuverlässiger, sondern auch weniger driftanfällig als die inoffizielle Web-API
(vgl. Fallstrick #7).

**Credentials:** CardDAV verlangt ein **app-spezifisches Passwort** (dasselbe wie IMAP,
Keychain-Service ``icloud-sync-mail``); das reguläre Apple-ID-Passwort wird abgelehnt.
Contacts läuft dadurch — wie Mail — **unabhängig von der Web-Session**: Drive/Photos
können Re-Auth brauchen, die Kontakte werden trotzdem gesichert.

**Kein Manifest** — das Dateisystem ist der Zustand. Spiegel: lokal Überzähliges wird
entfernt, aber **nur** nach vollständigem, fehlerfreiem, nicht-leerem Listing.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

from . import util

LOGGER = logging.getLogger(__name__)
_PROGRESS_EVERY = 100

_ROOT = "https://contacts.icloud.com"
_TIMEOUT = 120          # Sekunden; der REPORT über alle Kontakte darf dauern
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}

# Netz-Wiederholungen wie beim übrigen Sync (Apple nicht hämmern).
_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY = 3.0


class ContactsAuthError(Exception):
    """CardDAV-Login abgelehnt — app-spezifisches Passwort fehlt/abgelaufen."""


@dataclass
class ContactStats:
    downloaded: int = 0   # neu geschriebene Kontakte
    updated: int = 0      # geänderte Kontakte
    skipped: int = 0      # unverändert
    deleted: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (f"Contacts: {self.downloaded} neu, {self.updated} geändert, "
                f"{self.skipped} unverändert, {self.deleted} entfernt, {self.errors} Fehler")


def _emit(stats: ContactStats, progress_cb) -> None:
    if progress_cb is not None:
        progress_cb({"downloaded": stats.downloaded, "skipped": stats.skipped,
                     "deleted": stats.deleted, "errors": stats.errors})


# --- CardDAV-Zugriff --------------------------------------------------------

_PROP_PRINCIPAL = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                   '<d:prop><d:current-user-principal/></d:prop></d:propfind>')
_PROP_HOME = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
              'xmlns:c="urn:ietf:params:xml:ns:carddav">'
              '<d:prop><c:addressbook-home-set/></d:prop></d:propfind>')
_PROP_TYPE = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
              '<d:prop><d:resourcetype/></d:prop></d:propfind>')
_REPORT_ALL = ('<?xml version="1.0"?><c:addressbook-query xmlns:d="DAV:" '
               'xmlns:c="urn:ietf:params:xml:ns:carddav">'
               '<d:prop><d:getetag/><c:address-data/></d:prop><c:filter/></c:addressbook-query>')


def _request(method: str, url: str, auth, body: str, depth: str, session_obj) -> ET.Element:
    """Ein CardDAV-Request mit Retry; liefert den geparsten XML-Baum."""
    def do():
        r = session_obj.request(method, url, data=body.encode("utf-8"),
                                headers={"Depth": depth,
                                         "Content-Type": "application/xml; charset=utf-8"},
                                auth=auth, timeout=_TIMEOUT)
        if r.status_code in (401, 403):
            raise ContactsAuthError(f"{r.status_code} für {url}")
        r.raise_for_status()
        return r.content

    raw = util.with_retries(do, attempts=_RETRY_ATTEMPTS, base_delay=_RETRY_BASE_DELAY,
                            label=f"CardDAV {method}")
    return ET.fromstring(raw)


def _abs(url: str, base: str) -> str:
    """Relative href (``/123/carddavhome/``) auf den zuständigen Host beziehen."""
    if url.startswith("http"):
        return url
    m = re.match(r"(https://[^/]+)", base)
    return (m.group(1) if m else _ROOT) + url


def _discover(apple_id: str, password: str, session_obj) -> Optional[str]:
    """Findet die Adressbuch-Sammlung des Accounts (``.../carddavhome/card/``)."""
    auth = HTTPBasicAuth(apple_id, password)

    root = _request("PROPFIND", _ROOT + "/", auth, _PROP_PRINCIPAL, "0", session_obj)
    el = root.find(".//d:current-user-principal/d:href", _NS)
    if el is None or not (el.text or "").strip():
        LOGGER.error("[%s] CardDAV: kein Principal in der Antwort", apple_id)
        return None
    principal = _abs(el.text.strip(), _ROOT)

    root = _request("PROPFIND", principal, auth, _PROP_HOME, "0", session_obj)
    el = root.find(".//c:addressbook-home-set/d:href", _NS)
    if el is None or not (el.text or "").strip():
        LOGGER.error("[%s] CardDAV: kein addressbook-home-set", apple_id)
        return None
    home = _abs(el.text.strip(), principal)

    # Sammlungen unter dem Home: die Adressbuch-Sammlung nehmen (nicht das Home selbst).
    root = _request("PROPFIND", home, auth, _PROP_TYPE, "1", session_obj)
    for resp in root.findall("d:response", _NS):
        href_el = resp.find("d:href", _NS)
        if href_el is None or not (href_el.text or "").strip():
            continue
        href = href_el.text.strip()
        is_addressbook = resp.find(".//d:resourcetype/c:addressbook", _NS) is not None
        if is_addressbook or (href.rstrip("/").endswith("card") and href.rstrip("/") != home.rstrip("/")):
            return _abs(href, home)
    LOGGER.error("[%s] CardDAV: keine Adressbuch-Sammlung unter %s", apple_id, home)
    return None


def _fetch_vcards(collection: str, apple_id: str, password: str, session_obj) -> list[tuple[str, bytes]]:
    """Holt alle vCards der Sammlung in EINEM Request. Liefert [(href, vcard-bytes)]."""
    auth = HTTPBasicAuth(apple_id, password)
    root = _request("REPORT", collection, auth, _REPORT_ALL, "1", session_obj)
    out: list[tuple[str, bytes]] = []
    for resp in root.findall("d:response", _NS):
        href_el = resp.find("d:href", _NS)
        data_el = resp.find(".//c:address-data", _NS)
        if href_el is None or data_el is None or not (data_el.text or "").strip():
            continue
        # ElementTree hat die XML-Entities bereits aufgelöst -> echter vCard-Text.
        out.append((href_el.text or "", data_el.text.encode("utf-8")))
    return out


# --- vCard-Auswertung (nur fürs Benennen; der Inhalt bleibt unangetastet) ----

def _unfold(text: str) -> str:
    """vCard-Faltung auflösen (Folgezeilen beginnen mit Space/Tab)."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _field(vcard: str, name: str) -> str:
    m = re.search(r"^%s(?:;[^:\r\n]*)?:(.*)$" % name, _unfold(vcard), re.M | re.I)
    return m.group(1).strip() if m else ""


def _display_name(vcard: str) -> str:
    """Anzeigename für den Dateinamen: FN, sonst aus N, sonst ORG, sonst 'Kontakt'."""
    fn = _field(vcard, "FN")
    if not fn:
        n = _field(vcard, "N")
        if n:
            teile = [p.replace("\\,", ",").strip() for p in n.split(";")]
            fn = " ".join(p for p in (teile[1:2] + teile[0:1]) if p)  # Vorname Nachname
    if not fn:
        fn = _field(vcard, "ORG").split(";")[0].strip()
    fn = fn.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    return util.safe_component(fn or "Kontakt")


def _short_id(vcard: str, href: str) -> str:
    """Stabile Kurz-ID: bevorzugt die vCard-UID, sonst der Dateiname aus dem href."""
    uid = _field(vcard, "UID") or href.rstrip("/").rsplit("/", 1)[-1]
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:10]


# --- Sync -------------------------------------------------------------------

def sync_contacts(apple_id: str, app_password: str, dest_base_path: str,
                  progress_cb=None) -> ContactStats:
    """Spiegelt die iCloud-Kontakte per CardDAV nach ``dest_base_path/Contacts``.

    Schreibt je Kontakt Apples Original-vCard als ``.vcf``. Fehler ⇒ **kein** Löschen;
    leere Kontaktliste ⇒ ebenfalls kein Löschen (Schutz vor Massenlöschen).
    """
    stats = ContactStats()
    dest = Path(dest_base_path) / "Contacts"
    expected: set = set()
    _emit(stats, progress_cb)

    with requests.Session() as http:
        try:
            collection = _discover(apple_id, app_password, http)
            if not collection:
                stats.errors += 1
                return stats  # Fehler -> niemals löschen
            cards = _fetch_vcards(collection, apple_id, app_password, http)
        except ContactsAuthError:
            raise  # der Engine meldet das als „App-Passwort prüfen"
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Kontakte nicht lesbar für %s: %s", apple_id, exc)
            stats.errors += 1
            return stats

    if not cards:
        LOGGER.warning("[%s] Kontakte-Liste leer -> kein Löschen (Sicherheit).", apple_id)
        _emit(stats, progress_cb)
        LOGGER.info("[%s] %s", apple_id, stats.summary())
        return stats

    for i, (href, raw) in enumerate(cards, 1):
        try:
            _sync_one(href, raw, dest, stats, expected)
        except Exception as exc:  # noqa: BLE001 - ein Kontakt darf den Lauf nicht kippen
            LOGGER.warning("Kontakt nicht sicherbar (%s): %s", href, exc)
            stats.errors += 1
        if i % _PROGRESS_EVERY == 0:
            _emit(stats, progress_cb)

    # Spiegel: nur bei vollständigem, fehlerfreiem Listing + nicht-leerem expected.
    if expected and stats.errors == 0:
        stats.deleted = util.prune_extra(dest, expected)
    _emit(stats, progress_cb)
    LOGGER.info("[%s] %s", apple_id, stats.summary())
    return stats


def _sync_one(href: str, raw: bytes, dest: Path, stats: ContactStats, expected: set) -> None:
    text = raw.decode("utf-8", "replace")
    path = dest / f"{_display_name(text)}_{_short_id(text, href)}.vcf"
    expected.add(path)

    existed_before = path.exists()
    if _write_if_changed(path, raw):
        if existed_before:
            stats.updated += 1
        else:
            stats.downloaded += 1
    else:
        stats.skipped += 1


def _write_if_changed(path: Path, data: bytes) -> bool:
    """Schreibt ``data`` nur, wenn die Datei fehlt oder sich der Inhalt unterscheidet."""
    try:
        if path.exists() and path.read_bytes() == data:
            return False
    except OSError:
        pass
    util.write_bytes(path, data)
    return True
