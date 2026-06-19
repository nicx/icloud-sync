"""Feste Sync-Uhrzeiten (cron-artig) — reine, UI-freie Planungslogik.

Bewusst **ohne** rumps/AppKit-Import, damit die Logik direkt (auch im Test) importierbar
ist. Zwei Funktionen:

- :func:`parse_schedule` — wandelt eine Benutzereingabe ("07:30, 19:30") in eine
  normalisierte, deduplizierte, sortierte Liste von ``"HH:MM"``-Strings (oder ``ValueError``).
- :func:`due_by_schedule` — entscheidet, ob nach festem Uhrzeit-Plan ein Lauf fällig ist.

Die Uhrzeiten sind **lokale Wandzeit**. Fällig ist ein User, wenn seit dem letzten Lauf
(`last_run`) ein geplanter Zeitpunkt überschritten wurde — pro Slot genau einmal, verpasste
Slots (Sleep) coaleszieren zu einem Lauf (analog zur Intervall-Catch-up-Semantik).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_schedule(text: str) -> list[str]:
    """Parst eine Eingabe wie ``"7:30, 19:30"`` zu ``["07:30", "19:30"]``.

    Trennt an Komma und/oder Whitespace, parst jeden Eintrag als ``HH:MM`` (24 h),
    normalisiert auf zweistellig, dedupliziert und sortiert. Leere Eingabe ⇒ ``[]``.
    Ungültiger Eintrag ⇒ :class:`ValueError` (für die UI-Validierung).
    """
    tokens = [t for t in text.replace(",", " ").split() if t]
    times: set[str] = set()
    for tok in tokens:
        parsed = datetime.strptime(tok, "%H:%M")  # ValueError bei Unsinn / Stunde>23
        times.add(f"{parsed.hour:02d}:{parsed.minute:02d}")
    return sorted(times)


def due_by_schedule(times: list[str], last_run_iso: Optional[str], now: datetime) -> bool:
    """True, wenn nach festem Uhrzeit-Plan ein Lauf fällig ist.

    :param times: normalisierte ``"HH:MM"``-Liste (lokale Wandzeit); leer ⇒ nie fällig.
    :param last_run_iso: ISO-8601-Zeitstempel des letzten Laufs (UTC; fehlende tz = UTC)
        oder ``None`` (noch nie gelaufen ⇒ fällig).
    :param now: **aware lokale** Jetzt-Zeit (z. B. ``datetime.now().astimezone()``).

    ``recent`` = spätester geplanter Zeitpunkt ``<= now`` (sonst der späteste Slot von
    gestern). Fällig, wenn ``last_run`` davor liegt → pro Slot genau einmal.
    """
    if not times:
        return False
    if not last_run_iso:
        return True

    # Geplante Zeitpunkte: heute und (für den Tageswechsel) gestern.
    candidates: list[datetime] = []
    for t in times:
        hh, mm = (int(x) for x in t.split(":"))
        today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        candidates.append(today)
        candidates.append(today - timedelta(days=1))
    past = [c for c in candidates if c <= now]
    if not past:
        return False  # noch kein Slot erreicht (kann nur bei leerer Liste passieren)
    recent = max(past)

    try:
        last = datetime.fromisoformat(last_run_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last < recent
