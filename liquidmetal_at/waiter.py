"""Poll-until-ready helpers built on tenacity.

Every phase gate polls rather than sleeps. ``wait_until`` retries a predicate
until it returns truthy or a deadline elapses; ``retry_call`` retries a callable
that raises until it stops raising. Both log each attempt so a hung run is legible.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
    wait_random_exponential,
)

log = logging.getLogger("waiter")

T = TypeVar("T")


class WaitTimeout(RuntimeError):
    pass


def wait_until(
    predicate: Callable[[], T | None],
    *,
    timeout: float,
    interval: float = 5.0,
    description: str = "condition",
) -> T:
    """Call ``predicate`` every ``interval`` s until it returns truthy or timeout.

    Returns the truthy value. Raises :class:`WaitTimeout` on deadline.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        attempt += 1
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001 - polling tolerates transient errors
            last_exc = exc
            log.debug("waiting for %s (attempt %d): %s", description, attempt, exc)
        time.sleep(interval)
    raise WaitTimeout(
        f"timed out after {timeout}s waiting for {description}"
        + (f" (last error: {last_exc})" if last_exc else "")
    )


def retry_call(
    fn: Callable[[], T],
    *,
    timeout: float,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    description: str = "operation",
    exponential: bool = True,
) -> T:
    """Retry ``fn`` until it stops raising ``exceptions`` or ``timeout`` elapses."""
    wait = wait_random_exponential(multiplier=1, max=30) if exponential else wait_fixed(5)
    for attempt in Retrying(
        stop=stop_after_delay(timeout),
        wait=wait,
        retry=retry_if_exception_type(exceptions),
        reraise=True,
    ):
        with attempt:
            n = attempt.retry_state.attempt_number
            if n > 1:
                log.info("retrying %s (attempt %d)", description, n)
            return fn()
    raise WaitTimeout(f"timed out after {timeout}s on {description}")  # pragma: no cover
