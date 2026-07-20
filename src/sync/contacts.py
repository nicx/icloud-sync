"""iCloud Contacts – dateibasierter Sync-Spiegel (vCard + Roh-JSON).

Sichert jeden Kontakt nach ``<dest_base_path>/Contacts/<name>_<kurz-id>.{vcf,json}``:

- **vCard 3.0** (`.vcf`) — importierbar in Kontakte/andere Apps; bildet die gängigen Felder
  defensiv ab (fehlende werden ausgelassen).
- **Roh-JSON** (`.json`) — das vollständige iCloud-Kontakt-Dict 1:1 (verlustfrei; fängt
  Apple-Extensions/Gruppen/Foto ab, die in der vCard verloren gingen).

**Kein Manifest** — das Dateisystem ist der Zustand. Spiegel: lokal Überzähliges wird entfernt,
aber **nur** nach vollständigem, fehlerfreiem, nicht-leerem Listing (Guard gegen Massenlöschen).

pyicloud-API: ``api.contacts.all`` (Property, triggert Netz) -> ``list[dict]`` | ``None``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import util

LOGGER = logging.getLogger(__name__)
_PROGRESS_EVERY = 100


@dataclass
class ContactStats:
    downloaded: int = 0   # neu geschriebene Kontakte (mind. eine Datei neu)
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


def sync_contacts(api, dest_base_path: str, apple_id: str, progress_cb=None) -> ContactStats:
    """Spiegelt iCloud-Kontakte nach ``dest_base_path/Contacts`` (vCard + JSON, dateibasiert)."""
    stats = ContactStats()
    dest = Path(dest_base_path) / "Contacts"
    expected: set = set()
    _emit(stats, progress_cb)

    try:
        # ``all`` ist eine Property, die pro Zugriff neu lädt (startup -> contacts) — ein
        # Retry holt also einen frischen syncToken (Apple wirft sporadisch 420).
        contacts = util.with_retries(lambda: api.contacts.all, label=f"Contacts {apple_id}")
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Kontakte nicht lesbar für %s: %s", apple_id, exc)
        stats.errors += 1
        return stats  # Fehler -> niemals löschen

    if contacts is None:
        LOGGER.error("Kontakte-Liste leer/None für %s -> kein Löschen.", apple_id)
        stats.errors += 1
        return stats
    if not contacts:
        LOGGER.warning("[%s] Kontakte-Liste leer -> kein Löschen (Sicherheit).", apple_id)
        _emit(stats, progress_cb)
        LOGGER.info("[%s] %s", apple_id, stats.summary())
        return stats

    seen = 0
    for contact in contacts:
        try:
            _sync_one(contact, dest, stats, expected)
        except Exception as exc:  # noqa: BLE001 - einzelner Kontakt darf den Lauf nicht kippen
            LOGGER.warning("Kontakt nicht sicherbar: %s", exc)
            stats.errors += 1
        seen += 1
        if seen % _PROGRESS_EVERY == 0:
            _emit(stats, progress_cb)

    # Spiegel: nur bei vollständigem, fehlerfreiem Listing (Guards oben) + nicht-leerem expected.
    if expected:
        stats.deleted = util.prune_extra(dest, expected)
    _emit(stats, progress_cb)
    LOGGER.info("[%s] %s", apple_id, stats.summary())
    return stats


def _sync_one(contact: dict, dest: Path, stats: ContactStats, expected: set) -> None:
    cid = contact.get("contactId") or hashlib.sha1(
        json.dumps(contact, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    short = hashlib.sha1(str(cid).encode("utf-8")).hexdigest()[:10]
    # Endung anhängen statt with_suffix(): Punkte im Namen ("Dr.", "St.", Initialen) gelten
    # sonst als Suffix und with_suffix() würde Namensrest UND Kollisions-Hash abschneiden
    # ("Arzt Dr. Mueller_1a2b3c4d5e" -> "Arzt Dr.json").
    stem = f"{_display_name(contact)}_{short}"
    json_path = dest / f"{stem}.json"
    vcf_path = dest / f"{stem}.vcf"
    expected.add(json_path)
    expected.add(vcf_path)

    json_bytes = json.dumps(contact, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
    vcf_bytes = _vcard(contact).encode("utf-8")

    existed_before = json_path.exists() or vcf_path.exists()  # VOR dem Schreiben prüfen
    changed = _write_if_changed(json_path, json_bytes) | _write_if_changed(vcf_path, vcf_bytes)
    if not changed:
        stats.skipped += 1
    elif existed_before:
        stats.updated += 1
    else:
        stats.downloaded += 1


def _write_if_changed(path: Path, data: bytes) -> bool:
    """Schreibt ``data`` nur, wenn die Datei fehlt oder sich der Inhalt unterscheidet."""
    try:
        if path.exists() and path.read_bytes() == data:
            return False
    except OSError:
        pass
    util.write_bytes(path, data)
    return True


# --- vCard-Abbildung (defensiv; JSON bleibt die verlustfreie Quelle) --------

def _display_name(contact: dict) -> str:
    parts = [contact.get("firstName"), contact.get("lastName")]
    name = " ".join(p for p in parts if p) or contact.get("companyName") or "Kontakt"
    return util.safe_component(name)


def _esc(value: str) -> str:
    """vCard-Text escapen (RFC 6350-nah: \\ ; , und Zeilenumbrüche)."""
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _vcard(contact: dict) -> str:
    g = contact.get
    lines = ["BEGIN:VCARD", "VERSION:3.0"]

    last, first = g("lastName") or "", g("firstName") or ""
    middle, prefix, suffix = g("middleName") or "", g("prefix") or "", g("suffix") or ""
    lines.append("N:%s;%s;%s;%s;%s" % (_esc(last), _esc(first), _esc(middle), _esc(prefix), _esc(suffix)))
    fn = " ".join(p for p in (first, last) if p) or g("companyName") or "Kontakt"
    lines.append("FN:" + _esc(fn))

    if g("companyName"):
        org = g("companyName")
        if g("department"):
            org = f"{org};{g('department')}"
        lines.append("ORG:" + _esc(org))
    if g("jobTitle"):
        lines.append("TITLE:" + _esc(g("jobTitle")))
    if g("nickName"):
        lines.append("NICKNAME:" + _esc(g("nickName")))

    for ph in g("phones") or []:
        num = ph.get("field")
        if num:
            lines.append("TEL;TYPE=%s:%s" % (_esc(ph.get("label") or "VOICE"), _esc(num)))
    for em in g("emailAddresses") or []:
        addr = em.get("field")
        if addr:
            lines.append("EMAIL;TYPE=%s:%s" % (_esc(em.get("label") or "INTERNET"), _esc(addr)))
    for ad in g("streetAddresses") or []:
        f = ad.get("field") or {}
        lines.append("ADR;TYPE=%s:;;%s;%s;%s;%s;%s" % (
            _esc(ad.get("label") or "HOME"), _esc(f.get("street") or ""), _esc(f.get("city") or ""),
            _esc(f.get("state") or ""), _esc(f.get("postalCode") or ""), _esc(f.get("country") or "")))
    for url in g("urls") or []:
        if url.get("field"):
            lines.append("URL:" + _esc(url.get("field")))
    if g("birthday"):
        lines.append("BDAY:" + _esc(g("birthday")))
    if g("notes"):
        lines.append("NOTE:" + _esc(g("notes")))

    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"
