"""Wiederholende Timer auf Basis von ``NSTimer`` (rumps-frei).

Ersetzt ``rumps.Timer``. Der Timer läuft auf dem Main-Runloop; der Callback wird also
**auf dem Main-Thread** aufgerufen und darf UI direkt anfassen.

Bewusst nur im ``NSDefaultRunLoopMode`` (das macht ``scheduledTimer…`` automatisch) und
**nicht** in ``NSRunLoopCommonModes``: Während der Nutzer das Menü geöffnet hat, läuft der
Runloop im Event-Tracking-Modus. Feuerte der UI-Tick dort weiter, würde ein Menü-Neuaufbau
dem Nutzer das offene Menü unter den Fingern wegziehen. Dieses Verhalten entspricht zudem
exakt dem bisherigen ``rumps.Timer``.

**Verhaltensunterschied zu rumps, der hier bewusst ausgeglichen wird:** ``rumps.Timer``
setzt das Feuerdatum auf *jetzt* und feuert damit sofort nach dem Start ein erstes Mal;
``NSTimer.scheduledTimer…`` dagegen erst nach Ablauf des ersten Intervalls. Der Scheduler
in ``app.py`` verlässt sich auf das sofortige Feuern (der 300-s-Tick baut u. a. das Menü
neu auf — ohne ihn bliebe es fünf Minuten lang stehen). Daher gibt es
``fire_immediately``; die Start-Gnadenfrist (``_in_startup_grace``) fängt wie gehabt ab,
dass dieser erste Tick schon synchronisiert.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .ui_appkit import run_on_main_sync

LOGGER = logging.getLogger(__name__)

# Erlaubte Abweichung der Feuerzeit (Anteil des Intervalls). Gibt macOS Spielraum, Timer
# zu bündeln → weniger Aufwachvorgänge, besseres Energieverhalten. Für den 300-s-Scheduler
# und den UI-Refresh völlig unkritisch.
_TOLERANCE_RATIO = 0.1

try:  # pragma: no cover - umgebungsabhängig
    from Foundation import NSObject

    _APPKIT = True
except Exception:  # pragma: no cover
    NSObject = object  # type: ignore[assignment,misc]
    _APPKIT = False


class _TimerTarget(NSObject):  # type: ignore[misc]
    """ObjC-Ziel für ``NSTimer`` (der Timer hält es stark, solange er lebt)."""

    def fire_(self, _timer) -> None:
        callback = getattr(self, "_callback", None)
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - ein Tick darf die App nie killen
            LOGGER.exception("Timer-Callback fehlgeschlagen")


class RepeatingTimer:
    """Wiederholender Main-Thread-Timer mit ``start``/``stop`` und änderbarem Intervall."""

    def __init__(self, interval: float, callback: Callable[[], None],
                 fire_immediately: bool = False) -> None:
        """:param fire_immediately: erstes Feuern sofort statt erst nach ``interval``
            (entspricht dem Verhalten von ``rumps.Timer``)."""
        self._interval = max(0.1, float(interval))
        self._target = _TimerTarget.alloc().init()
        self._target._callback = callback
        self._fire_immediately = fire_immediately
        self._timer = None

    @property
    def interval(self) -> float:
        return self._interval

    @interval.setter
    def interval(self, seconds: float) -> None:
        """Setzt das Intervall; ein laufender Timer wird mit dem neuen Wert neu geplant."""
        new = max(0.1, float(seconds))
        if new == self._interval:
            return
        self._interval = new
        if self.is_running:
            self.stop()
            self.start()

    @property
    def is_running(self) -> bool:
        return self._timer is not None

    def start(self) -> None:
        """Plant den Timer auf dem Main-Runloop ein (idempotent)."""

        def _apply() -> None:
            if self._timer is not None:
                return
            from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop, NSTimer

            if self._fire_immediately:
                # Feuerdatum "jetzt" → erster Tick, sobald der Runloop dreht (wie rumps).
                timer = NSTimer.alloc().initWithFireDate_interval_target_selector_userInfo_repeats_(
                    NSDate.date(), self._interval, self._target, "fire:", None, True)
                NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSDefaultRunLoopMode)
            else:
                timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    self._interval, self._target, "fire:", None, True)
            timer.setTolerance_(self._interval * _TOLERANCE_RATIO)
            self._timer = timer

        run_on_main_sync(_apply)

    def stop(self) -> None:
        """Invalidiert den Timer (idempotent). Danach ist ``start`` wieder möglich."""

        def _apply() -> None:
            if self._timer is None:
                return
            self._timer.invalidate()
            self._timer = None

        run_on_main_sync(_apply)
