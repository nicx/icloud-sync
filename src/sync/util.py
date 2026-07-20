"""Geteilte Helfer für die Sync-Module: Pfad-Hygiene, Retry/Backoff, Streaming-Download.

Bewusst ohne pyicloud-Import — diese Funktionen arbeiten nur mit bereits übergebenen
Objekten (requests-Response, bytes) und generischen Exceptions, damit der pyicloud-Zugriff
auf ``auth.session`` (und die Aufrufer in ``drive``/``photos``) beschränkt bleibt.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# Standard-Chunkgröße fürs Streaming (1 MiB) — speicherschonend auch bei großen Videos.
CHUNK_SIZE = 1 << 20

# HTTP-Statuscodes, bei denen wir mit Backoff erneut versuchen (Throttling/transient).
# 420 = Apples "Invalid sync token" (Contacts): der gerade von /co/startup ausgestellte
# syncToken wird beim Folge-Request abgelehnt. Serverseitig, transient — ein neuer Versuch
# holt einen frischen Token und läuft durch.
_RETRYABLE_STATUS = {420, 429, 500, 502, 503, 504}


def safe_component(name: str) -> str:
    """Macht einen einzelnen Pfadbestandteil dateisystemtauglich.

    Entfernt Separatoren und problematische Zeichen, ohne den Namen unkenntlich zu machen.
    """
    cleaned = name.replace(os.sep, "_").replace("/", "_").replace("\0", "")
    cleaned = cleaned.strip().strip(".") or "_"
    return cleaned


def _status_of(exc: BaseException) -> Optional[int]:
    """Versucht, aus einer Exception einen HTTP-Statuscode zu lesen (pyicloud/requests)."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        return getattr(resp, "status_code", None)
    return None


def is_retryable(exc: BaseException) -> bool:
    """True, wenn die Exception auf ein transientes/Throttling-Problem hindeutet."""
    status = _status_of(exc)
    if status in _RETRYABLE_STATUS:
        return True
    text = str(exc).lower()
    return any(s in text for s in ("throttl", "rate limit", "timeout", "temporarily"))


def with_retries(
    func: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "operation",
) -> T:
    """Führt ``func`` aus und wiederholt bei retrybaren Fehlern mit exponentiellem Backoff.

    Nicht-retrybare Exceptions werden sofort weitergereicht. ``sleep`` ist injizierbar
    (für Tests). Apple nicht hämmern — daher konservative Defaults.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - bewusst breit, Klassifizierung via is_retryable
            last = exc
            if attempt == attempts or not is_retryable(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1))
            LOGGER.warning("%s fehlgeschlagen (Versuch %d/%d): %s — warte %.1fs",
                           label, attempt, attempts, exc, delay)
            sleep(delay)
    # Unerreichbar, aber für den Typchecker:
    assert last is not None
    raise last


def _atomic_write(dest: Path, write_body: Callable[[object], None]) -> None:
    """Schreibt nach ``dest`` resumebar: erst ``.part``, dann atomarer Rename.

    Ein Abbruch hinterlässt höchstens eine ``.part``-Datei, nie eine halbe Zieldatei —
    der nächste Lauf lädt sauber neu.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with open(part, "wb") as fh:
            write_body(fh)
        os.replace(part, dest)
    except BaseException:
        # Teil-Datei aufräumen, damit kein Müll zurückbleibt.
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def stream_to_file(response, dest: Path, chunk_size: int = CHUNK_SIZE) -> int:
    """Streamt eine requests-Response in eine Datei (resumebar) und liefert die Byte-Anzahl.

    ``response`` muss ``iter_content`` (echte requests-Response) oder zumindest ein
    ``raw.read``-fähiges Objekt bereitstellen.
    """
    written = 0

    def body(fh) -> None:
        nonlocal written
        if hasattr(response, "iter_content"):
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        else:  # Fallback: rohes Read-Objekt (z. B. BytesIO bei 0-Byte-Dateien)
            raw = getattr(response, "raw", response)
            while True:
                chunk = raw.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)

    _atomic_write(dest, body)
    return written


def write_empty_file(dest: Path) -> None:
    """Legt eine leere Datei an (für 0-Byte-iCloud-Dateien, die kein Download erlauben)."""
    _atomic_write(dest, lambda fh: None)


def write_bytes(dest: Path, data: bytes) -> int:
    """Schreibt ``data`` atomar (`.part`+rename) nach ``dest`` und liefert die Byte-Anzahl."""
    _atomic_write(dest, lambda fh: fh.write(data))
    return len(data)


# Toleranz beim mtime-Vergleich (Dateisysteme/SMB runden teils auf ganze Sekunden).
_MTIME_TOLERANCE_S = 2.0


def needs_download(dest: Path, size: Optional[int], date_modified: Optional[datetime]) -> bool:
    """Dateibasierte Entscheidung (rsync-artig), ob ``dest`` (neu) geladen werden muss.

    True, wenn die Zieldatei fehlt, die Größe abweicht oder die Änderungszeit mehr als
    :data:`_MTIME_TOLERANCE_S` von ``date_modified`` abweicht. Ohne Server-Metadaten
    (``size``/``date_modified`` ``None``) entscheidet allein die Existenz.
    """
    try:
        st = dest.stat()
    except OSError:
        return True  # fehlt -> laden
    if size is not None and st.st_size != size:
        return True
    if date_modified is not None:
        try:
            if abs(st.st_mtime - date_modified.timestamp()) > _MTIME_TOLERANCE_S:
                return True
        except (ValueError, OverflowError, OSError):
            return True
    return False


def _norm_path(path: Path) -> str:
    """Pfad als Unicode-normalisierter String — Vergleichsform für Datei-Mengen.

    Namen mit zerlegbaren Zeichen (ä, ö, ü, é …) kommen aus ``os.listdir`` je nach
    Dateisystem in **NFD** zurück, während wir sie in **NFC** erzeugen (so liefert Apple
    sie). Ein reiner String-/Path-Vergleich schlägt dann fehl, obwohl dieselbe Datei
    gemeint ist — beim SMB-Ziel real beobachtet: ``prune_extra`` hat die gerade selbst
    geschriebene Datei wieder gelöscht (jeder Lauf: neu schreiben -> löschen -> ...).
    Der Lookup selbst (``exists``/``stat``) ist normalisierungstolerant, der Vergleich nicht.
    """
    return unicodedata.normalize("NFC", str(path))


def prune_extra(root: Path, expected: set[Path]) -> int:
    """Spiegel-Helfer: löscht unter ``root`` alle Dateien, die NICHT in ``expected`` liegen.

    Entfernt anschließend leer gewordene Verzeichnisse. Liefert die Anzahl gelöschter Dateien.
    ``.part``-Reste werden ebenfalls entfernt.

    **Sicherheit:** Nur mit einem *vollständigen* ``expected``-Set aufrufen (der Aufrufer muss
    sichergestellt haben, dass das Server-Listing fehlerfrei und vollständig war) — sonst würde
    legitim vorhandener Inhalt gelöscht.
    """
    if not root.is_dir():
        return 0
    expected_resolved = {_norm_path(p.resolve()) for p in expected}
    deleted = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fpath = Path(dirpath) / name
            if _norm_path(fpath.resolve()) in expected_resolved:
                continue
            try:
                fpath.unlink()
                deleted += 1
            except OSError as exc:
                LOGGER.warning("Konnte überzählige Datei nicht löschen %s: %s", fpath, exc)
    _remove_empty_dirs(root)
    return deleted


def _remove_empty_dirs(root: Path) -> None:
    """Entfernt leere Unterverzeichnisse unter ``root`` (``root`` selbst bleibt erhalten)."""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        d = Path(dirpath)
        if d == root:
            continue
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
        except OSError:
            pass


def set_mtime(dest: Path, when: Optional[datetime]) -> None:
    """Setzt Änderungs-/Zugriffszeit (mtime/atime) auf ``when`` und – auf macOS – auch die
    Erstellungszeit (birthtime, Finders „Erstellungsdatum").

    ``os.utime`` setzt nur atime/mtime; die birthtime bleibt sonst der Schreibzeitpunkt
    (= Sync-Zeit). Auf macOS wird sie zusätzlich per ``setattrlist`` gesetzt. Alles
    best-effort — Fehler werden ignoriert."""
    if when is None:
        return
    try:
        ts = when.timestamp()
        os.utime(dest, (ts, ts))
    except (OSError, ValueError, OverflowError):
        pass
    _set_btime_darwin(dest, when)


def _set_btime_darwin(dest: Path, when: datetime) -> None:
    """Setzt die Erstellungszeit (ATTR_CMN_CRTIME) einer Datei auf macOS via ``setattrlist``.

    No-op auf anderen Plattformen / bei jedem Fehler (z. B. Netz-FS ohne Unterstützung).
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)

        class _Attrlist(ctypes.Structure):
            _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_ushort),
                        ("commonattr", ctypes.c_uint), ("volattr", ctypes.c_uint),
                        ("dirattr", ctypes.c_uint), ("fileattr", ctypes.c_uint),
                        ("forkattr", ctypes.c_uint)]

        class _Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        libc.setattrlist.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.c_ulong]
        libc.setattrlist.restype = ctypes.c_int

        ATTR_BIT_MAP_COUNT = 5
        ATTR_CMN_CRTIME = 0x00000200
        al = _Attrlist(ATTR_BIT_MAP_COUNT, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
        ts = _Timespec(int(when.timestamp()), 0)
        libc.setattrlist(os.fsencode(str(dest)), ctypes.byref(al),
                         ctypes.byref(ts), ctypes.sizeof(ts), 0)  # rc ignoriert (best-effort)
    except Exception:  # noqa: BLE001 - rein kosmetisch, nie den Sync gefährden
        pass


def iter_dir_components(parts: Iterable[str]) -> str:
    """Verbindet bereits bereinigte Komponenten zu einem relativen Pfad."""
    return os.path.join(*[safe_component(p) for p in parts]) if parts else ""
