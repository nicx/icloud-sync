"""Mock-basierte Tests für die Sync-Schicht (kein Netzwerk, kein Apple-Account).

Ausführen::

    .venv/bin/python tests/test_sync.py

``HOME`` wird im Skript auf ein Temp-Verzeichnis gesetzt, damit nichts Echtes berührt wird.
Reiner Datei-Sync (kein sqlite): das Dateisystem ist der Zustand.
"""

from __future__ import annotations

import imaplib
import os
import tempfile
from datetime import datetime, timezone

os.environ["HOME"] = tempfile.mkdtemp(prefix="iclbk_test_home_")
import sys
sys.path.insert(0, os.getcwd())

from pathlib import Path  # noqa: E402
from src.sync import drive, photos, mail, contacts, engine, util  # noqa: E402
from src.config.users import User, UserStatus  # noqa: E402

# Tests laufen ohne Netz: Erreichbarkeitsprüfung global auf "online" setzen, damit run_user
# die (gemockten) Syncs ausführt statt offline zu überspringen. Der Offline-Fall wird in
# test_engine_offline_is_transient gezielt umgeschaltet.
engine.is_online = lambda *a, **k: True


# --- Fakes: Drive/Photos ----------------------------------------------------

class FakeResponse:
    def __init__(self, content: bytes):
        self._content = content

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def raise_for_status(self):
        pass

    def close(self):
        pass


class FakeDriveNode:
    def __init__(self, name, node_type, *, size=None, content=b"", children=None,
                 date_modified=None, raise_children=False):
        self.name = name
        self.type = node_type
        self.size = size
        self._content = content
        self.data = {"etag": "e"}
        self._children = children or []
        self._raise_children = raise_children
        self.date_modified = date_modified or datetime(2024, 1, 1, tzinfo=timezone.utc)

    def get_children(self):
        if self._raise_children:
            raise RuntimeError("boom: folder unreadable")
        return self._children

    def open(self, stream=True):
        return FakeResponse(self._content)


class FakeDriveService:
    def __init__(self, root_children):
        self._root_children = root_children

    def get_children(self):
        return self._root_children


class FakePhotoAsset:
    def __init__(self, asset_id, filename, *, content=b"PHOTO", is_live=False,
                 video_content=b"VIDEO", created=None):
        self.id = asset_id
        self.filename = filename
        self.is_live_photo = is_live
        self.created = created or datetime(2023, 7, 15, tzinfo=timezone.utc)
        self._urls = {"original": f"https://x/{asset_id}/orig"}
        self._content = {f"https://x/{asset_id}/orig": content}
        self.versions = {"original": {"filename": filename, "url": self._urls["original"]}}
        if is_live:
            vurl = f"https://x/{asset_id}/vid"
            self._urls["original_video"] = vurl
            self._content[vurl] = video_content
            vname = filename.rsplit(".", 1)[0] + ".MOV"
            self.versions["original_video"] = {"filename": vname, "url": vurl}

    def download_url(self, version="original"):
        return self._urls.get(version)

    def download(self, version="original"):
        return self._content.get(self._urls.get(version, ""))


class FakeSession:
    def __init__(self, url_to_content):
        self._map = url_to_content

    def get(self, url, stream=True):
        return FakeResponse(self._map[url])


class _IterFail:
    """Iterierbar, die nach `n` Elementen wirft (für Photos-Guard-Test)."""
    def __init__(self, items, fail_after):
        self._items, self._fail = items, fail_after

    def __iter__(self):
        for i, it in enumerate(self._items):
            if i >= self._fail:
                raise RuntimeError("iteration boom")
            yield it


class FakePhotoLibraryZone:
    """Eine CloudKit-Bibliothek mit Scope + All-Album (für api.photos.libraries)."""
    def __init__(self, assets, scope):
        self.all = assets
        self.scope = scope


class FakePhotosLib:
    def __init__(self, assets, shared_assets=None, libraries_error=False):
        self.all = assets
        self._shared_assets = shared_assets   # None = keine geteilte Mediathek vorhanden
        self._libraries_error = libraries_error

    @property
    def libraries(self):
        if self._libraries_error:
            raise RuntimeError("libraries boom")
        libs = {
            "root": FakePhotoLibraryZone(self.all, "private"),
            "shared": FakePhotoLibraryZone([], "shared-stream"),  # Legacy-Shared-Streams: ignorieren
        }
        if self._shared_assets is not None:
            libs["shared:SharedSync-x"] = FakePhotoLibraryZone(self._shared_assets, "shared-library")
        return libs


class FakeContactsLib:
    def __init__(self, data):
        self._data = data   # list[dict] oder None

    @property
    def all(self):
        return self._data


class FakeApi:
    def __init__(self, *, drive_service=None, photos_assets=None, url_map=None,
                 shared_assets=None, libraries_error=False, contacts_data=None):
        self.drive = drive_service
        self.photos = FakePhotosLib(photos_assets if photos_assets is not None else [],
                                    shared_assets=shared_assets, libraries_error=libraries_error)
        self.contacts = FakeContactsLib(contacts_data)
        self.session = FakeSession(url_map or {})


# --- Fakes: IMAP ------------------------------------------------------------

class FakeIMAP:
    """Minimaler IMAP-Server-Mock. mailboxes: name -> {"uidv": int, "msgs": {uid:int -> bytes}}."""
    good_password = "app-pw"
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.mailboxes = FakeIMAP._next_mailboxes
        self.search_fail = FakeIMAP._next_search_fail
        self._current = None
        self.selected_readonly = []
        self.fetch_specs = []
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        if pw != FakeIMAP.good_password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return ("OK", [b"LOGIN ok"])

    def list(self):
        lines = [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.mailboxes]
        return ("OK", lines)

    def select(self, mailbox, readonly=False):
        self._current = mailbox.strip('"')
        self.selected_readonly.append((self._current, readonly))
        return ("OK", [b"1"])

    def status(self, mailbox, what):
        name = mailbox.strip('"')
        uidv = self.mailboxes[name]["uidv"]
        return ("OK", [f"{name} (UIDVALIDITY {uidv})".encode()])

    def uid(self, command, *args):
        msgs = self.mailboxes[self._current]["msgs"]
        if command.upper() == "SEARCH":
            if self._current in self.search_fail:
                raise imaplib.IMAP4.error("SEARCH boom")
            # key=str: erlaubt gemischte int-/str-UIDs (für den Traversal-Test).
            uids = " ".join(str(u) for u in sorted(msgs, key=str))
            return ("OK", [uids.encode()])
        if command.upper() == "FETCH":
            uid = args[0].decode() if isinstance(args[0], (bytes, bytearray)) else str(args[0])
            self.fetch_specs.append(args[1])
            # int- ODER str-gekeyte msgs-Dicts tolerieren.
            raw = msgs.get(uid)
            if raw is None and uid.isdigit():
                raw = msgs.get(int(uid))
            if raw is None:
                raise imaplib.IMAP4.error(f"FETCH unbekannte UID {uid}")
            # INTERNALDATE bewusst NACH dem Body-Literal (eigenes Listenelement) — so liefert
            # ein realer Server, wenn BODY[] vor INTERNALDATE kommt. Prüft, dass der Code ALLE
            # Antwortteile nach INTERNALDATE absucht (nicht nur data[0][0]).
            dates = self.mailboxes[self._current].get("dates", {})
            idate = dates.get(uid) or (dates.get(int(uid)) if uid.isdigit() else None) \
                or "01-Jan-2020 00:00:00 +0000"
            body_hdr = f'{uid} (BODY[] {{{len(raw)}}}'.encode()
            trailer = f' INTERNALDATE "{idate}")'.encode()
            return ("OK", [(body_hdr, raw), trailer])
        raise imaplib.IMAP4.error(f"unknown {command}")

    def logout(self):
        return ("BYE", [b"bye"])


def use_imap(mailboxes, search_fail=()):
    FakeIMAP._next_mailboxes = mailboxes
    FakeIMAP._next_search_fail = set(search_fail)
    FakeIMAP.instances = []
    mail.imaplib.IMAP4_SSL = FakeIMAP


# --- Helpers ----------------------------------------------------------------

PASS = []
def check(cond, msg):
    assert cond, "FAIL: " + msg
    PASS.append(msg)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def listdir(*parts):
    p = os.path.join(*parts)
    return sorted(os.listdir(p)) if os.path.isdir(p) else []


# --- Drive ------------------------------------------------------------------

def test_drive():
    dest = tempfile.mkdtemp(prefix="drivedest_")
    f1 = FakeDriveNode("hello.txt", "file", size=5, content=b"hello")
    f0 = FakeDriveNode("empty.bin", "file", size=0, content=b"")
    top = FakeDriveNode("top.txt", "file", size=3, content=b"abc")
    api = FakeApi(drive_service=FakeDriveService([FakeDriveNode("Sub", "folder", children=[f1]), f0, top]))

    s = drive.sync_drive(api, dest, "d@example.com")
    check(s.downloaded == 3, f"drive: 3 geladen (war {s.downloaded})")
    check(read(os.path.join(dest, "Drive", "Sub", "hello.txt")) == b"hello", "drive nested content")
    check(os.path.exists(os.path.join(dest, "Drive", "empty.bin")), "drive 0-byte angelegt")

    # 2. Lauf: unverändert -> skip (dateibasiert via Größe/mtime)
    s2 = drive.sync_drive(api, dest, "d@example.com")
    check(s2.downloaded == 0 and s2.skipped == 3, f"drive 2. Lauf skip (dl={s2.downloaded}, skip={s2.skipped})")

    # Spiegel: top.txt serverseitig entfernt -> lokal gelöscht
    api2 = FakeApi(drive_service=FakeDriveService([FakeDriveNode("Sub", "folder", children=[f1]), f0]))
    s3 = drive.sync_drive(api2, dest, "d@example.com")
    check(not os.path.exists(os.path.join(dest, "Drive", "top.txt")), "drive Spiegel: entfernte Datei weg")
    check(s3.deleted == 1, f"drive: 1 gelöscht (war {s3.deleted})")

    # Guard: Ordner-Listing-Fehler -> NICHTS löschen
    extra = os.path.join(dest, "Drive", "keepme.txt")
    with open(extra, "wb") as fh:
        fh.write(b"x")
    api3 = FakeApi(drive_service=FakeDriveService([FakeDriveNode("Bad", "folder", raise_children=True)]))
    s4 = drive.sync_drive(api3, dest, "d@example.com")
    check(s4.deleted == 0 and os.path.exists(extra), "drive Guard: Listing-Fehler -> kein Löschen")


def test_drive_excludes():
    """Ausgeschlossene Top-Level-Ordner werden nicht geladen und lokal geprunt; Rest bleibt."""
    dest = tempfile.mkdtemp(prefix="driveexcl_")
    keep = FakeDriveNode("Eigen", "folder", children=[FakeDriveNode("k.txt", "file", size=1, content=b"k")])
    shared = FakeDriveNode("Geteilt", "folder", children=[FakeDriveNode("s.txt", "file", size=1, content=b"s")])
    api = FakeApi(drive_service=FakeDriveService([keep, shared]))

    # 1. Lauf ohne Ausschluss -> beide da
    drive.sync_drive(api, dest, "d@example.com")
    check(os.path.exists(os.path.join(dest, "Drive", "Eigen", "k.txt")), "drive: Eigen geladen")
    check(os.path.exists(os.path.join(dest, "Drive", "Geteilt", "s.txt")), "drive: Geteilt geladen")

    # 2. Lauf mit Ausschluss "Geteilt" -> nicht geladen UND lokal geprunt; Eigen bleibt
    s = drive.sync_drive(api, dest, "d@example.com", excludes=["Geteilt"])
    check(os.path.exists(os.path.join(dest, "Drive", "Eigen", "k.txt")), "drive Ausschluss: Eigen bleibt")
    check(not os.path.exists(os.path.join(dest, "Drive", "Geteilt")), "drive Ausschluss: Geteilt lokal entfernt")
    check(s.deleted == 1, f"drive Ausschluss: 1 geprunt (war {s.deleted})")


def test_drive_excludes_nested():
    """Ausschluss greift auch auf verschachtelte Pfade (a/b)."""
    dest = tempfile.mkdtemp(prefix="driveexcln_")
    sub = FakeDriveNode("b", "folder", children=[FakeDriveNode("x.txt", "file", size=1, content=b"x")])
    other = FakeDriveNode("c", "folder", children=[FakeDriveNode("y.txt", "file", size=1, content=b"y")])
    top = FakeDriveNode("a", "folder", children=[sub, other])
    api = FakeApi(drive_service=FakeDriveService([top]))
    drive.sync_drive(api, dest, "d@example.com", excludes=["a/b"])
    check(not os.path.exists(os.path.join(dest, "Drive", "a", "b")), "drive nested-Ausschluss: a/b weg")
    check(os.path.exists(os.path.join(dest, "Drive", "a", "c", "y.txt")), "drive nested-Ausschluss: a/c bleibt")


# --- Photos -----------------------------------------------------------------

def test_photos():
    dest = tempfile.mkdtemp(prefix="photodest_")
    a = FakePhotoAsset("AAA", "IMG_1.JPG", content=b"img1")
    b = FakePhotoAsset("BBB", "IMG_1.JPG", content=b"img2")  # gleicher Name, anderes Asset
    live = FakePhotoAsset("CCC", "IMG_2.HEIC", content=b"heic", is_live=True, video_content=b"mov")
    url_map = {}
    for asset in (a, b, live):
        url_map.update(asset._content)
    api = FakeApi(photos_assets=[a, b, live], url_map=url_map)

    s = photos.sync_photos(api, dest, "p@example.com")
    check(s.downloaded == 3 and s.components == 4, f"photos: 3 Assets/4 Dateien (dl={s.downloaded}, c={s.components})")
    pdir = os.path.join(dest, "Photos", "2023", "07")
    files = listdir(pdir)
    check(len([f for f in files if f.endswith(".HEIC")]) == 1
          and len([f for f in files if f.endswith(".MOV")]) == 1, f"live -> HEIC+MOV ({files})")
    check(len([f for f in files if f.endswith(".JPG")]) == 2, f"kollision: 2 JPG ({files})")

    # 2. Lauf -> skip (Existenz)
    s2 = photos.sync_photos(api, dest, "p@example.com")
    check(s2.downloaded == 0 and s2.skipped == 3, f"photos 2. Lauf skip (dl={s2.downloaded}, skip={s2.skipped})")

    # Spiegel: Asset BBB entfernt -> dessen Datei weg
    api2 = FakeApi(photos_assets=[a, live], url_map=url_map)
    s3 = photos.sync_photos(api2, dest, "p@example.com")
    check(s3.deleted == 1, f"photos Spiegel: 1 gelöscht (war {s3.deleted})")
    check(len(listdir(pdir)) == 3, "photos: nach Löschen noch 3 Dateien (a + live HEIC+MOV)")

    # Guard: leere Liste -> NICHTS löschen
    api3 = FakeApi(photos_assets=[], url_map=url_map)
    s4 = photos.sync_photos(api3, dest, "p@example.com")
    check(s4.deleted == 0 and len(listdir(pdir)) == 3, "photos Guard: leere Liste -> kein Löschen")

    # Guard: Iterationsfehler -> NICHTS löschen
    api5 = FakeApi(url_map=url_map)
    api5.photos = FakePhotosLib(_IterFail([a, live], fail_after=1))
    s5 = photos.sync_photos(api5, dest, "p@example.com")
    check(s5.deleted == 0, "photos Guard: Iterationsfehler -> kein Löschen")


# --- Photos: geteilte Mediathek --------------------------------------------

def test_photos_shared_library():
    """include_shared trennt private (Photos/) und geteilte (SharedPhotos/) Mediathek sauber."""
    dest = tempfile.mkdtemp(prefix="sharedphotos_")
    p = FakePhotoAsset("PRIV1", "PRIV.JPG", content=b"priv")
    s1 = FakePhotoAsset("SH1", "A.JPG", content=b"sh1")
    s2 = FakePhotoAsset("SH2", "B.JPG", content=b"sh2")
    url_map = {}
    for asset in (p, s1, s2):
        url_map.update(asset._content)
    priv_dir = os.path.join(dest, "Photos", "2023", "07")
    shared_dir = os.path.join(dest, "SharedPhotos", "2023", "07")

    # include_shared=False -> nur Photos/, kein SharedPhotos/
    api = FakeApi(photos_assets=[p], url_map=url_map, shared_assets=[s1, s2])
    photos.sync_photos(api, dest, "x@example.com", include_shared=False)
    check(len([f for f in listdir(priv_dir) if f.endswith(".JPG")]) == 1, "shared aus: privat geladen")
    check(not os.path.isdir(os.path.join(dest, "SharedPhotos")), "shared aus: kein SharedPhotos/")

    # include_shared=True -> beide Ablagen gefüllt
    api2 = FakeApi(photos_assets=[p], url_map=url_map, shared_assets=[s1, s2])
    photos.sync_photos(api2, dest, "x@example.com", include_shared=True)
    check(len([f for f in listdir(priv_dir) if f.endswith(".JPG")]) == 1, "shared an: privat unverändert")
    check(len([f for f in listdir(shared_dir) if f.endswith(".JPG")]) == 2, "shared an: 2 geteilte Dateien")

    # Getrennter Prune: ein geteiltes Asset entfernt -> nur SharedPhotos betroffen, Photos bleibt
    api3 = FakeApi(photos_assets=[p], url_map=url_map, shared_assets=[s1])
    s3 = photos.sync_photos(api3, dest, "x@example.com", include_shared=True)
    check(s3.deleted == 1, f"shared Prune: 1 gelöscht (war {s3.deleted})")
    check(len([f for f in listdir(shared_dir) if f.endswith(".JPG")]) == 1, "shared Prune: noch 1 geteilt")
    check(len([f for f in listdir(priv_dir) if f.endswith(".JPG")]) == 1, "shared Prune: privat unberührt")


def test_photos_shared_resilience():
    """Fehler/Leere bei der geteilten Mediathek dürfen weder den privaten Sync kippen noch löschen."""
    dest = tempfile.mkdtemp(prefix="sharedres_")
    p = FakePhotoAsset("PR1", "P.JPG", content=b"p")
    url_map = dict(p._content)
    priv_dir = os.path.join(dest, "Photos", "2023", "07")

    # libraries-Zugriff wirft -> privat trotzdem ok
    api = FakeApi(photos_assets=[p], url_map=url_map, libraries_error=True)
    photos.sync_photos(api, dest, "x@example.com", include_shared=True)
    check(len(listdir(priv_dir)) == 1, "shared-Fehler: privat dennoch geladen")
    check(not os.path.isdir(os.path.join(dest, "SharedPhotos")), "shared-Fehler: kein SharedPhotos/")

    # leere geteilte Mediathek -> kein Prune (stray-Datei in SharedPhotos bleibt)
    shared_dir = os.path.join(dest, "SharedPhotos")
    os.makedirs(shared_dir, exist_ok=True)
    stray = os.path.join(shared_dir, "stray.bin")
    with open(stray, "wb") as fh:
        fh.write(b"x")
    api2 = FakeApi(photos_assets=[p], url_map=url_map, shared_assets=[])
    s = photos.sync_photos(api2, dest, "x@example.com", include_shared=True)
    check(s.deleted == 0 and os.path.exists(stray), "shared leer -> kein Löschen (Guard)")


# --- Contacts ---------------------------------------------------------------

def test_contacts():
    dest = tempfile.mkdtemp(prefix="contacts_")
    c1 = {"contactId": "C1", "firstName": "Max", "lastName": "Mustermann",
          "nickName": "Maxi",
          "phones": [{"label": "MOBILE", "field": "+49 170 1"}],
          "emailAddresses": [{"label": "WORK", "field": "max@example.com"}]}
    c2 = {"contactId": "C2", "firstName": "Erika", "companyName": "ACME"}
    api = FakeApi(contacts_data=[c1, c2])

    s = contacts.sync_contacts(api, dest, "c@example.com")
    cdir = os.path.join(dest, "Contacts")
    files = listdir(cdir)
    check(len([f for f in files if f.endswith(".vcf")]) == 2, f"contacts: 2 vCards ({files})")
    check(len([f for f in files if f.endswith(".json")]) == 2, f"contacts: 2 JSON ({files})")
    check(s.downloaded == 2, f"contacts: 2 neu (war {s.downloaded})")
    vcf = [f for f in files if f.startswith("Max Mustermann") and f.endswith(".vcf")]
    check(vcf and b"TEL;TYPE=MOBILE:+49 170 1" in read(os.path.join(cdir, vcf[0])), "contacts: vCard-Telefon")
    # Apple liefert den Spitznamen als "nickName" (camelCase) – nicht "nickname"
    check(vcf and b"NICKNAME:Maxi" in read(os.path.join(cdir, vcf[0])), "contacts: vCard-Spitzname")

    # 2. Lauf unverändert -> skip
    s2 = contacts.sync_contacts(api, dest, "c@example.com")
    check(s2.downloaded == 0 and s2.updated == 0 and s2.skipped == 2,
          f"contacts 2. Lauf skip (dl={s2.downloaded}, upd={s2.updated}, skip={s2.skipped})")

    # Spiegel: C2 entfernt -> dessen Dateien weg
    api2 = FakeApi(contacts_data=[c1])
    s3 = contacts.sync_contacts(api2, dest, "c@example.com")
    check(s3.deleted == 2, f"contacts Spiegel: 2 Dateien gelöscht (war {s3.deleted})")
    check(len(listdir(cdir)) == 2, "contacts: nach Löschen noch 2 Dateien (C1 vcf+json)")

    # Guard: None -> kein Löschen
    s4 = contacts.sync_contacts(FakeApi(contacts_data=None), dest, "c@example.com")
    check(s4.deleted == 0 and s4.errors == 1 and len(listdir(cdir)) == 2, "contacts Guard: None -> kein Löschen")

    # Guard: leere Liste -> kein Löschen
    s5 = contacts.sync_contacts(FakeApi(contacts_data=[]), dest, "c@example.com")
    check(s5.deleted == 0 and len(listdir(cdir)) == 2, "contacts Guard: leere Liste -> kein Löschen")

    # Punkt im Namen ("Dr.") darf Namensrest + Kollisions-Hash nicht abschneiden:
    # with_suffix() haette "Arzt Dr. Mueller_<hash>.json" zu "Arzt Dr.json" gemacht.
    dest2 = tempfile.mkdtemp(prefix="contacts_dot_")
    dot1 = {"contactId": "D1", "firstName": "Arzt Dr.", "lastName": "Mueller"}
    dot2 = {"contactId": "D2", "firstName": "Arzt Dr.", "lastName": "Schmidt"}
    contacts.sync_contacts(FakeApi(contacts_data=[dot1, dot2]), dest2, "c@example.com")
    dfiles = sorted(listdir(os.path.join(dest2, "Contacts")))
    check(len(dfiles) == 4, f"contacts Punkt-Name: 4 Dateien statt Kollision ({dfiles})")
    check(all(f.startswith("Arzt Dr. Mueller_") or f.startswith("Arzt Dr. Schmidt_") for f in dfiles),
          f"contacts Punkt-Name: voller Name + Hash erhalten ({dfiles})")

    # Apple wirft bei Contacts sporadisch 420 "Invalid sync token" -> retrybar
    class Boom(Exception):
        def __init__(self):
            super().__init__("Client Error (420) (420): Invalid sync token")
            self.code = 420

    class FlakyContacts:
        def __init__(self):
            self.calls = 0

        @property
        def all(self):
            self.calls += 1
            if self.calls == 1:
                raise Boom()
            return [c1]

    class FlakyApi:
        def __init__(self):
            self.contacts = FlakyContacts()

    dest3 = tempfile.mkdtemp(prefix="contacts_420_")
    fapi = FlakyApi()
    s6 = contacts.sync_contacts(fapi, dest3, "c@example.com")
    check(s6.errors == 0 and s6.downloaded == 1,
          f"contacts 420: Retry rettet den Lauf (err={s6.errors}, dl={s6.downloaded})")
    check(fapi.contacts.calls == 2, f"contacts 420: genau 1 Wiederholung (calls={fapi.contacts.calls})")


# --- Mail -------------------------------------------------------------------

def test_mail():
    dest = tempfile.mkdtemp(prefix="maildest_")
    boxes = {
        "INBOX": {"uidv": 10, "msgs": {1: b"From: a\r\n\r\nHallo", 2: b"From: b\r\n\r\nWelt"}},
        "Archive": {"uidv": 5, "msgs": {7: b"archived"}},
        "Trash": {"uidv": 3, "msgs": {}},
    }
    use_imap(boxes)
    s = mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(s.downloaded == 3, f"mail: 3 geladen (war {s.downloaded})")
    check(read(os.path.join(dest, "Mail", "INBOX", "1.eml")).endswith(b"Hallo"), "mail INBOX/1.eml Inhalt")
    check(os.path.exists(os.path.join(dest, "Mail", "Archive", "7.eml")), "mail Archive/7.eml")
    # readonly + PEEK
    inst = FakeIMAP.instances[0]
    check(all(ro for _n, ro in inst.selected_readonly), "mail: select readonly=True")
    check(all("PEEK" in spec for spec in inst.fetch_specs), "mail: BODY.PEEK[] genutzt (ungelesen)")

    # 2. Lauf -> skip
    use_imap(boxes)
    s2 = mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(s2.downloaded == 0 and s2.skipped == 3, f"mail 2. Lauf skip (dl={s2.downloaded}, skip={s2.skipped})")

    # Move/Delete: INBOX-Mail 2 -> Trash (neue UID); Spiegel zieht nach
    boxes2 = {
        "INBOX": {"uidv": 10, "msgs": {1: b"From: a\r\n\r\nHallo"}},
        "Archive": {"uidv": 5, "msgs": {7: b"archived"}},
        "Trash": {"uidv": 3, "msgs": {9: b"From: b\r\n\r\nWelt"}},
    }
    use_imap(boxes2)
    s3 = mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(not os.path.exists(os.path.join(dest, "Mail", "INBOX", "2.eml")), "mail Move: INBOX/2.eml weg")
    check(os.path.exists(os.path.join(dest, "Mail", "Trash", "9.eml")), "mail Move: Trash/9.eml da")
    check(s3.deleted == 1 and s3.downloaded == 1, f"mail Move: 1 weg/1 neu (del={s3.deleted}, dl={s3.downloaded})")

    # UIDVALIDITY-Wechsel -> Ordner-Resync (stale Datei verschwindet)
    stale = os.path.join(dest, "Mail", "Archive", "999.eml")
    with open(stale, "wb") as fh:
        fh.write(b"stale")
    boxes3 = dict(boxes2)
    boxes3["Archive"] = {"uidv": 6, "msgs": {7: b"archived"}}  # uidv 5 -> 6
    use_imap(boxes3)
    mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(not os.path.exists(stale), "mail UIDVALIDITY-Wechsel: stale Datei weg")
    check(read(os.path.join(dest, "Mail", "Archive", ".uidvalidity")) == b"6", "mail .uidvalidity aktualisiert")

    # Auth-Fehler -> MailAuthError
    use_imap(boxes2)
    try:
        mail.sync_mail("u@icloud.com", "wrong", dest)
        check(False, "mail: falsches PW hätte MailAuthError werfen müssen")
    except mail.MailAuthError:
        check(True, "mail: falsches PW -> MailAuthError")

    # Guard: SEARCH-Fehler in einem Ordner -> NICHTS löschen
    guard_extra = os.path.join(dest, "Mail", "INBOX", "1.eml")  # existiert
    boxes4 = {
        "INBOX": {"uidv": 10, "msgs": {}},   # leer -> würde 1.eml löschen, wenn nicht geguardet
        "Archive": {"uidv": 6, "msgs": {7: b"archived"}},
    }
    use_imap(boxes4, search_fail={"Archive"})
    s5 = mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(s5.deleted == 0 and os.path.exists(guard_extra), "mail Guard: SEARCH-Fehler -> kein Löschen")


# --- Engine -----------------------------------------------------------------

def test_engine_all_services():
    dest = tempfile.mkdtemp(prefix="enginedest_")
    a = FakePhotoAsset("E1", "P.JPG", content=b"x")
    f1 = FakeDriveNode("d.txt", "file", size=3, content=b"abc")
    api = FakeApi(drive_service=FakeDriveService([f1]), photos_assets=[a], url_map=dict(a._content))

    from src.auth import session as sess, keychain
    keychain.get_password = lambda aid: "secret"
    keychain.get_mail_password = lambda aid: "app-pw"
    sess.login = lambda aid, pw: sess.LoginResult(api=api)
    use_imap({"INBOX": {"uidv": 1, "msgs": {1: b"hi"}}})

    user = User(apple_id="e@example.com", sync_drive=True, sync_photos=True,
                sync_mail=True, dest_base_path=dest)
    events = []
    status = engine.run_user(user, progress_cb=lambda aid, ph, c: events.append(ph))
    check(status == UserStatus.OK, f"engine: OK (war {status})")
    check({"drive", "photos", "mail"} <= set(events), f"engine: alle Phasen gemeldet ({set(events)})")
    check(os.path.exists(os.path.join(dest, "Drive", "d.txt")), "engine: Drive-Datei")
    check(os.path.exists(os.path.join(dest, "Mail", "INBOX", "1.eml")), "engine: Mail-Datei")


def test_engine_mail_independent_of_web():
    """Mail läuft, auch wenn die Web-Session 2FA braucht."""
    dest = tempfile.mkdtemp(prefix="engineindep_")
    from src.auth import session as sess, keychain
    keychain.get_password = lambda aid: "secret"
    keychain.get_mail_password = lambda aid: "app-pw"
    sess.login = lambda aid, pw: sess.LoginResult(needs_2fa=True)  # Web braucht Re-Auth
    use_imap({"INBOX": {"uidv": 1, "msgs": {1: b"hi"}}})

    user = User(apple_id="x@example.com", sync_drive=True, sync_mail=True, dest_base_path=dest)
    status = engine.run_user(user)
    check(os.path.exists(os.path.join(dest, "Mail", "INBOX", "1.eml")),
          "engine: Mail trotz Web-Re-Auth gesichert")
    check(status == UserStatus.NEEDS_REAUTH, f"engine: Status NEEDS_REAUTH bei Web-2FA (war {status})")


def test_engine_mount_missing():
    bad = User(apple_id="m@example.com", dest_base_path="/nope/missing", sync_mail=True)
    check(engine.run_user(bad) == UserStatus.ERROR, "engine: fehlender Mount -> ERROR")


def test_engine_records_last_error():
    """Fehlschlag schreibt einen Klartext-Grund in user.last_error (für Menü/Notification)."""
    from src.config.users import UsersStore

    store = UsersStore()
    u = User(apple_id="err@example.com", dest_base_path="/nope/missing", sync_mail=True)
    store.add(u)
    status = engine.run_user(u, store)
    got = store.get("err@example.com")
    check(status == UserStatus.ERROR, f"engine: ERROR bei fehlendem Mount (war {status})")
    check(got.last_error and "gemountet" in got.last_error,
          f"engine: last_error nennt den Grund (war {got.last_error!r})")


def test_engine_clears_last_error_on_success():
    """Ein erfolgreicher Lauf löscht einen zuvor gesetzten Fehlergrund."""
    from src.auth import session as sess, keychain
    from src.config.users import UsersStore

    dest = tempfile.mkdtemp(prefix="engineok_")
    a = FakePhotoAsset("Z1", "P.JPG", content=b"x")
    f1 = FakeDriveNode("d.txt", "file", size=3, content=b"abc")
    api = FakeApi(drive_service=FakeDriveService([f1]), photos_assets=[a], url_map=dict(a._content))
    keychain.get_password = lambda aid: "secret"
    keychain.get_mail_password = lambda aid: "app-pw"
    sess.login = lambda aid, pw: sess.LoginResult(api=api)
    use_imap({"INBOX": {"uidv": 1, "msgs": {1: b"hi"}}})

    store = UsersStore()
    u = User(apple_id="ok2@example.com", sync_drive=True, sync_photos=True,
             sync_mail=True, dest_base_path=dest)
    store.add(u)
    store.set_status(u.apple_id, UserStatus.ERROR, last_error="alter Fehler")
    status = engine.run_user(u, store)
    check(status == UserStatus.OK, f"engine: OK bei Erfolg (war {status})")
    check(store.get(u.apple_id).last_error is None, "engine: last_error bei Erfolg gelöscht")


def test_user_last_error_roundtrip():
    """last_error überlebt to_dict/from_dict; alte JSON ohne Feld -> None."""
    u = User(apple_id="r@example.com", last_error="kaputt")
    d = u.to_dict()
    check(d.get("last_error") == "kaputt", "user: last_error in to_dict")
    check(User.from_dict(d).last_error == "kaputt", "user: last_error aus from_dict")
    check(User.from_dict({"apple_id": "old@example.com"}).last_error is None,
          "user: fehlendes last_error -> None")
    # sync_shared_photos: Roundtrip + Default + alte JSON
    check(User.from_dict(User(apple_id="s@x", sync_shared_photos=True).to_dict()).sync_shared_photos is True,
          "user: sync_shared_photos Roundtrip")
    check(User.from_dict({"apple_id": "old@example.com"}).sync_shared_photos is False,
          "user: sync_shared_photos Default False (alte JSON)")
    # drive_excludes: Roundtrip + Default + alte JSON
    check(User.from_dict(User(apple_id="d@x", drive_excludes=["Geteilt"]).to_dict()).drive_excludes == ["Geteilt"],
          "user: drive_excludes Roundtrip")
    check(User.from_dict({"apple_id": "old@example.com"}).drive_excludes == [],
          "user: drive_excludes Default [] (alte JSON)")
    check(User.from_dict(User(apple_id="c@x", sync_contacts=True).to_dict()).sync_contacts is True,
          "user: sync_contacts Roundtrip")
    check(User.from_dict({"apple_id": "old@example.com"}).sync_contacts is False,
          "user: sync_contacts Default False (alte JSON)")


def test_settings_auto_sync_paused_roundtrip():
    """auto_sync_paused/startup_delay überleben save/load; sinnvolle Defaults."""
    from src.config.settings import Settings, load_settings, save_settings

    save_settings(Settings(auto_sync_paused=True, startup_delay_seconds=5))
    loaded = load_settings()
    check(loaded.auto_sync_paused is True, "settings: auto_sync_paused=True persistiert")
    check(loaded.startup_delay_seconds == 5, "settings: startup_delay_seconds persistiert")
    check(Settings().auto_sync_paused is False, "settings: auto_sync_paused-Default False")
    check(Settings().startup_delay_seconds == 90, "settings: startup_delay-Default 90")
    save_settings(Settings())  # zurücksetzen


def test_user_services_summary():
    """Dienste-Kurzliste (Menü + Accounts-Tabelle) bildet aktive Flags korrekt ab."""
    from src.app import user_services_summary
    from src.config.users import User, UserStatus

    u = User(apple_id="x@icloud.com", sync_drive=True, sync_photos=True, sync_shared_photos=True,
             sync_contacts=False, sync_mail=True, dest_base_path="/tmp", status=UserStatus.IDLE)
    check(user_services_summary(u) == "Drive, Photos, +Geteilt, Mail", "ui: services summary aktive Dienste")
    off = User(apple_id="y@icloud.com", sync_drive=False, sync_photos=False, sync_shared_photos=False,
               sync_contacts=False, sync_mail=False, dest_base_path="/tmp", status=UserStatus.IDLE)
    check(user_services_summary(off) == "—", "ui: services summary keine Dienste -> —")


def test_settings_sync_times_roundtrip():
    """sync_times überleben save/load; Default ist leere Liste."""
    from src.config.settings import Settings, load_settings, save_settings

    save_settings(Settings(sync_times=["07:30", "19:30"]))
    loaded = load_settings()
    check(loaded.sync_times == ["07:30", "19:30"], "settings: sync_times persistiert")
    check(Settings().sync_times == [], "settings: sync_times-Default leer")
    save_settings(Settings())  # zurücksetzen


def test_parse_schedule():
    """parse_schedule normalisiert, dedupliziert, sortiert; wirft bei Unsinn."""
    from src.schedule import parse_schedule

    check(parse_schedule("7:30, 19:30") == ["07:30", "19:30"], "schedule: parse normalisiert HH:MM")
    check(parse_schedule("19:30, 07:30, 7:30") == ["07:30", "19:30"], "schedule: parse dedupliziert+sortiert")
    check(parse_schedule("  ") == [], "schedule: parse leer -> []")
    for bad in ("25:00", "abc", "7:60"):
        try:
            parse_schedule(bad)
            check(False, f"schedule: parse('{bad}') hätte ValueError werfen müssen")
        except ValueError:
            check(True, f"schedule: parse('{bad}') -> ValueError")


def test_due_by_schedule():
    """due_by_schedule: pro Slot genau einmal, inkl. Tageswechsel-Catch-up."""
    from datetime import datetime, timezone
    from src.schedule import due_by_schedule

    tz = timezone.utc  # aware-Zeit reicht; Logik ist tz-relativ
    def local(h, m):
        return datetime(2026, 6, 19, h, m, tzinfo=tz)
    def iso(day, h, m):
        return datetime(2026, 6, day, h, m, tzinfo=tz).isoformat()

    check(due_by_schedule(["07:30"], None, local(8, 0)) is True, "schedule: nie gelaufen -> fällig")
    check(due_by_schedule([], iso(18, 1, 0), local(8, 0)) is False, "schedule: keine Zeiten -> nie fällig")
    # Slot 07:30 heute bereits erreicht; last_run davor (gestern) -> fällig, danach (07:31) -> nicht.
    check(due_by_schedule(["07:30"], iso(18, 19, 0), local(8, 0)) is True, "schedule: Slot überschritten -> fällig")
    check(due_by_schedule(["07:30"], iso(19, 7, 31), local(8, 0)) is False, "schedule: nach Lauf im Slot -> nicht erneut")
    # Vor dem heutigen Slot: gestriger 07:30 ist schon abgedeckt -> nicht fällig.
    check(due_by_schedule(["07:30"], iso(18, 7, 35), local(7, 29)) is False, "schedule: vor Slot -> nicht fällig")
    # Zwei Slots: 19:30 fällig, obwohl 07:30 heute schon lief.
    check(due_by_schedule(["07:30", "19:30"], iso(19, 7, 35), local(20, 0)) is True, "schedule: zweiter Slot fällig")


def test_engine_offline_is_transient():
    """Offline (iCloud nicht erreichbar) ist KEIN Fehler: kein error-Status, keine Mail,
    last_run unverändert -> Retry beim nächsten Tick (nicht erst nach sync_interval_hours)."""
    from src import notify
    from src.config.settings import Settings, save_settings
    from src.config.users import UsersStore

    dest = tempfile.mkdtemp(prefix="offline_")
    save_settings(Settings(error_email_enabled=True, error_email_to="ops@example.com"))
    sent: list = []
    orig_mail, orig_online = notify.send_mail, engine.is_online
    notify.send_mail = lambda *a, **k: (sent.append(a) or True)
    engine.is_online = lambda *a, **k: False
    try:
        store = UsersStore()
        u = User(apple_id="off@example.com", sync_drive=True, sync_photos=True, sync_mail=True,
                 dest_base_path=dest, status=UserStatus.OK, last_run="2026-01-01T00:00:00+00:00")
        store.add(u)
        status = engine.run_user(u, store)
        got = store.get("off@example.com")
        check(status != UserStatus.ERROR, f"offline: kein ERROR-Status (war {status})")
        check(got.last_run == "2026-01-01T00:00:00+00:00", "offline: last_run unverändert (Retry bald, nicht 4h)")
        check(sent == [], "offline: keine Fehler-E-Mail")
    finally:
        notify.send_mail, engine.is_online = orig_mail, orig_online
        save_settings(Settings())


def test_engine_emails_on_new_problem():
    """Fehler-Mail nur bei aktiviertem Setting UND neuem/geändertem Problem (kein Spam)."""
    from src import notify
    from src.config.settings import Settings, save_settings
    from src.config.users import UsersStore

    save_settings(Settings(error_email_enabled=True, error_email_to="ops@example.com"))
    sent: list = []
    orig = notify.send_mail
    notify.send_mail = lambda *a, **k: (sent.append(a) or True)
    try:
        store = UsersStore()
        u = User(apple_id="mailerr@example.com", dest_base_path="/nope/missing", sync_mail=True)
        store.add(u)
        engine.run_user(u, store)   # neuer Fehler -> 1 Mail
        check(len(sent) == 1, f"mail-alert: 1 Mail bei neuem Fehler (war {len(sent)})")
        engine.run_user(u, store)   # identischer Fehler -> keine weitere
        check(len(sent) == 1, f"mail-alert: keine weitere bei gleichem Fehler (war {len(sent)})")

        # deaktiviert -> keine Mail, auch bei frischem Fehler
        save_settings(Settings(error_email_enabled=False, error_email_to="ops@example.com"))
        u2 = User(apple_id="mailerr2@example.com", dest_base_path="/nope/missing")
        store.add(u2)
        engine.run_user(u2, store)
        check(len(sent) == 1, "mail-alert: deaktiviert -> keine Mail")
    finally:
        notify.send_mail = orig
        save_settings(Settings())  # Settings zurücksetzen (andere Tests unbeeinflusst)


# --- Security: UID-Sanitisierung (Path-Traversal-Schutz) --------------------

def test_mail_uid_traversal_blocked():
    """Eine bösartige, nicht-numerische UID darf keine Datei außerhalb von Mail/ schreiben."""
    dest = tempfile.mkdtemp(prefix="mailuid_")
    boxes = {
        # Gültige UID "1" + Angriffs-UID mit Pfad-Traversal (würde nach dest/evil.eml schreiben).
        "INBOX": {"uidv": 1, "msgs": {"1": b"From: a\r\n\r\nok", "../../evil": b"pwn"}},
    }
    use_imap(boxes)
    s = mail.sync_mail("u@icloud.com", "app-pw", dest)
    check(os.path.exists(os.path.join(dest, "Mail", "INBOX", "1.eml")), "mail uid: gültige UID geladen")
    check(s.downloaded == 1, f"mail uid: nur die gültige UID geladen (dl={s.downloaded})")
    # Der Traversal-Pfad Mail/INBOX/../../evil.eml == dest/evil.eml darf NICHT existieren.
    check(not os.path.exists(os.path.join(dest, "evil.eml")), "mail uid: Traversal-Datei NICHT geschrieben")
    eml = [f for f in listdir(dest, "Mail", "INBOX") if f.endswith(".eml")]
    check(eml == ["1.eml"], f"mail uid: nur 1.eml im Ordner ({eml})")


# --- Security: Session-Verzeichnis-Rechte (Token-Schutz) --------------------

def test_session_dir_perms():
    """session_dir ist 0700 und reduziert enthaltene Dateien auf 0600 (Tokens umgehen 2FA)."""
    import stat
    from src.config import paths

    d = paths.session_dir("perm@example.com")
    mode = stat.S_IMODE(os.stat(d).st_mode)
    check(mode == 0o700, f"session_dir 0700 (war {oct(mode)})")

    # pyicloud legt Dateien teils 0644 an -> nächster Aufruf muss sie auf 0600 ziehen.
    f = d / "x.session"
    f.write_bytes(b"token")
    os.chmod(f, 0o644)
    paths.session_dir("perm@example.com")
    fmode = stat.S_IMODE(os.stat(f).st_mode)
    check(fmode == 0o600, f"session-Datei 0600 (war {oct(fmode)})")


def test_mail_sets_mtime_from_internaldate():
    """Die .eml-mtime trägt das IMAP-INTERNALDATE (Empfangszeit), nicht die Download-Zeit."""
    import calendar
    import time

    dest = tempfile.mkdtemp(prefix="mailmtime_")
    boxes = {
        "INBOX": {"uidv": 1,
                  "msgs": {1: b"From: a\r\n\r\nfrueh", 2: b"From: b\r\n\r\nspaet"},
                  "dates": {1: "07-Jun-2026 09:15:00 +0000",
                            2: "09-Jun-2026 18:30:00 +0000"}},
    }
    use_imap(boxes)
    mail.sync_mail("u@icloud.com", "app-pw", dest)

    def expect(idate):
        return calendar.timegm(time.strptime(idate, "%d-%b-%Y %H:%M:%S +0000"))

    import sys

    p1 = os.path.join(dest, "Mail", "INBOX", "1.eml")
    p2 = os.path.join(dest, "Mail", "INBOX", "2.eml")
    e1, e2 = expect("07-Jun-2026 09:15:00 +0000"), expect("09-Jun-2026 18:30:00 +0000")
    m1, m2 = os.path.getmtime(p1), os.path.getmtime(p2)
    check(abs(m1 - e1) < 2, f"mail mtime #1 = INTERNALDATE (war {m1})")
    check(abs(m2 - e2) < 2, f"mail mtime #2 = INTERNALDATE (war {m2})")
    check(m2 > m1, "mail mtime: spätere Mail hat jüngere mtime")
    # Erstellungsdatum (birthtime) ebenfalls gesetzt (nur macOS).
    st1 = os.stat(p1)
    if sys.platform == "darwin" and hasattr(st1, "st_birthtime"):
        check(abs(st1.st_birthtime - e1) < 2, f"mail birthtime = INTERNALDATE (war {st1.st_birthtime})")
    inst = FakeIMAP.instances[0]
    check(all("PEEK" in spec for spec in inst.fetch_specs), "mail mtime: BODY.PEEK[] erhalten (ungelesen)")


def test_config_backup_restore():
    """backup_config_to/restore_config_from kopiert settings+users (ohne Passwörter) hin und zurück."""
    from src.config import backup
    from src.config.settings import Settings, load_settings, save_settings
    from src.config.users import User, UsersStore

    save_settings(Settings(sync_interval_hours=7, error_email_to="ops@example.com"))
    store = UsersStore()
    store.add(User(apple_id="cfg@example.com", dest_base_path="/tmp/x"))

    target = os.path.join(tempfile.mkdtemp(prefix="cfgbackup_"), "icloud-sync-config")
    n = backup.backup_config_to(target)
    check(n == 2, f"config: 2 Dateien gesichert (war {n})")
    check(os.path.exists(os.path.join(target, "settings.json"))
          and os.path.exists(os.path.join(target, "users.json")), "config: beide Dateien da")

    # Aktuelle Config "verlieren", dann zurücksichern.
    save_settings(Settings())  # Intervall zurück auf Default
    restored = backup.restore_config_from(target)
    check(restored == 2, f"config: 2 Dateien wiederhergestellt (war {restored})")
    check(load_settings().sync_interval_hours == 7, "config: settings nach Restore wiederhergestellt")
    check(UsersStore.loaded().get("cfg@example.com") is not None,
          "config: user nach Restore wiederhergestellt")
    save_settings(Settings())  # zurücksetzen


def test_contacts_retry_fenster():
    """Das 420-Retry-Fenster muss den im Feld beobachteten Ausfall (~15 s reichten nicht)
    ueberbrücken: 5 Versuche ab 3 s = 3+6+12+24 = 45 s Wartezeit."""
    class Boom(Exception):
        def __init__(self):
            super().__init__("Client Error (420) (420): Invalid sync token")
            self.code = 420

    wartezeiten = []
    versuche = {"n": 0}

    def immer_420():
        versuche["n"] += 1
        raise Boom()

    try:
        util.with_retries(immer_420, attempts=contacts._RETRY_ATTEMPTS,
                          base_delay=contacts._RETRY_BASE_DELAY,
                          sleep=wartezeiten.append, label="test")
    except Boom:
        pass

    check(versuche["n"] == 5, f"retry-fenster: 5 Versuche (waren {versuche['n']})")
    check(wartezeiten == [3.0, 6.0, 12.0, 24.0], f"retry-fenster: Backoff-Folge ({wartezeiten})")
    check(sum(wartezeiten) == 45.0, f"retry-fenster: ~45s ueberbrueckt (waren {sum(wartezeiten)}s)")

    # Der reale Fall: 4 Fehlschlaege, der 5. Versuch klappt -> Lauf gerettet.
    zaehler = {"n": 0}

    def klappt_beim_fuenften():
        zaehler["n"] += 1
        if zaehler["n"] < 5:
            raise Boom()
        return "ok"

    res = util.with_retries(klappt_beim_fuenften, attempts=contacts._RETRY_ATTEMPTS,
                            base_delay=contacts._RETRY_BASE_DELAY,
                            sleep=lambda _: None, label="test")
    check(res == "ok", "retry-fenster: 5. Versuch rettet den Lauf")


def test_prune_unicode_normalisierung():
    """Dateien mit ä/ö/ü/é dürfen nicht weggeprunt werden, nur weil das Dateisystem
    den Namen in NFD zurückgibt, während ``expected`` ihn in NFC enthält (SMB-Ziel)."""
    import unicodedata
    root = Path(tempfile.mkdtemp(prefix="prune_uni_"))

    # So verhält sich das SMB-Ziel: geschrieben wird NFC, auf der Platte liegt NFD.
    nfc = unicodedata.normalize("NFC", "Müller_abc123.json")
    nfd = unicodedata.normalize("NFD", nfc)
    check(nfc != nfd, "prune-unicode: Testdaten unterscheiden sich in NFC/NFD")
    (root / nfd).write_bytes(b"x")

    # expected enthält den NFC-Pfad — dieselbe Datei, andere Normalform.
    deleted = util.prune_extra(root, {root / nfc})
    check(deleted == 0, f"prune-unicode: NFD-Datei bleibt trotz NFC-expected (gelöscht={deleted})")
    check((root / nfd).exists() or (root / nfc).exists(), "prune-unicode: Datei noch da")

    # Gegenprobe: wirklich Überzähliges wird weiterhin gelöscht.
    (root / "wirklich_ueberzaehlig.json").write_bytes(b"y")
    deleted2 = util.prune_extra(root, {root / nfc})
    check(deleted2 == 1, f"prune-unicode: echter Ueberhang wird geloescht (war {deleted2})")


if __name__ == "__main__":
    test_contacts_retry_fenster()
    test_prune_unicode_normalisierung()
    test_drive()
    test_drive_excludes()
    test_drive_excludes_nested()
    test_photos()
    test_photos_shared_library()
    test_photos_shared_resilience()
    test_contacts()
    test_mail()
    test_mail_uid_traversal_blocked()
    test_mail_sets_mtime_from_internaldate()
    test_session_dir_perms()
    test_engine_all_services()
    test_engine_mail_independent_of_web()
    test_engine_mount_missing()
    test_engine_records_last_error()
    test_engine_clears_last_error_on_success()
    test_user_last_error_roundtrip()
    test_settings_auto_sync_paused_roundtrip()
    test_user_services_summary()
    test_settings_sync_times_roundtrip()
    test_parse_schedule()
    test_due_by_schedule()
    test_engine_offline_is_transient()
    test_engine_emails_on_new_problem()
    test_config_backup_restore()
    for m in PASS:
        print("  ok:", m)
    print(f"\nALL {len(PASS)} SYNC TESTS PASSED")
