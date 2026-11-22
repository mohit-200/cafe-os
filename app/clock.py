"""Café-local clock. In demo mode it starts at a fixed time of day and then runs in real time."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, tz: str, demo_start: str | None = None) -> None:
        self.tz = ZoneInfo(tz)
        self.demo_start = demo_start
        self._offset = timedelta(0)
        self._frozen: datetime | None = None
        self.reset()

    def reset(self) -> None:
        if not self.demo_start:
            self._offset = timedelta(0)
            return
        real = datetime.now(self.tz)
        hour, minute = (int(x) for x in self.demo_start.split(":"))
        self._offset = real.replace(hour=hour, minute=minute, second=0, microsecond=0) - real

    def freeze(self, at: datetime) -> None:
        """Tests only: stop the clock at a fixed instant."""
        self._frozen = at

    def advance(self, minutes: float) -> None:
        if self._frozen is not None:
            self._frozen += timedelta(minutes=minutes)
        else:
            self._offset += timedelta(minutes=minutes)

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(self.tz) + self._offset

    def at_hm(self, hm: str, ref: datetime | None = None) -> datetime:
        """Today's datetime for an 'HH:MM' string, in café time."""
        ref = ref or self.now()
        hour, minute = (int(x) for x in hm.split(":"))
        return ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
