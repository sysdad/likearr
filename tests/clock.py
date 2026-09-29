"""One fake clock for the whole suite: time only moves when a test moves it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FakeClock:
    """A controllable clock. `now` is seconds as a float (`time()`, `monotonic()`); `value` and
    calling the clock give the same instant as an aware `datetime`. `sleep` and `advance` move it
    forward, so an injected wait never actually waits."""

    def __init__(self, start: float | datetime = 1000.0) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        if isinstance(start, datetime):
            self.value = start
        else:
            self.now = start

    @property
    def value(self) -> datetime:
        return _EPOCH + timedelta(seconds=self.now)

    @value.setter
    def value(self, moment: datetime) -> None:
        self.now = (moment - _EPOCH).total_seconds()

    def __call__(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds
