"""Cooperative cancellation for long-running simulations.

The HTTP layer installs a :class:`CancelToken` for the request being served
(``with use_token(token): ...``). Long loops in the domain code call
:func:`checkpoint`, which raises :class:`Cancelled` once the token has been
cancelled (client disconnected, explicit ``/api/cancel`` call, or the
per-request time budget ran out). Outside a request there is no token and
:func:`checkpoint` is a cheap no-op, so the CLI and unit tests are unaffected.
"""

from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from typing import Iterator


class Cancelled(Exception):
    """Raised inside domain code when the current request was cancelled."""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """Thread-safe cancellation flag with an optional deadline."""

    def __init__(self, budget_s: float | None = None) -> None:
        self._event = threading.Event()
        self.reason = ""
        self.deadline = (time.monotonic() + budget_s) if budget_s else None

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        if not self._event.is_set() and self.deadline is not None \
                and time.monotonic() > self.deadline:
            self.cancel("timeout")
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled(self.reason)


_current: contextvars.ContextVar[CancelToken | None] = \
    contextvars.ContextVar("cancel_token", default=None)


@contextmanager
def use_token(token: CancelToken) -> Iterator[CancelToken]:
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)


def checkpoint() -> None:
    """Abort the current computation if its request was cancelled."""
    token = _current.get()
    if token is not None:
        token.raise_if_cancelled()
