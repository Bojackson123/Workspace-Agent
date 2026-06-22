"""Retry helper for transient Google API failures.

A file created through the Drive API is not always immediately writable through
the Docs / Sheets APIs — the new resource takes a moment to propagate, so the
*first* follow-up write (append text, append rows) can come back ``404`` or
``5xx`` even though the create succeeded. These tools catch ``HttpError`` and
return it as a string, so such a race surfaces to the LLM as a "transient
error" with nothing logged at ERROR level. Wrapping the racy calls in a short
exponential-backoff retry absorbs that propagation window.

Connection-level blips are retried too: Google's frontend occasionally resets a
TLS connection mid-request (``EOF occurred in violation of protocol``, broken
pipe, incomplete read), which surfaces as an ``ssl.SSLError`` / ``OSError`` from
``httplib2`` rather than an ``HttpError``. Those are transient by nature, so a
retry usually succeeds on the next, freshly-opened connection.
"""

import http.client
import logging
import socket
import ssl
import time
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Statuses worth retrying: 404 (created resource not yet visible), 408/429
# (timeout / rate limit), and the 5xx family (transient server errors).
_TRANSIENT_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})

# Connection-level errors worth retrying. ``ssl.SSLError`` covers the
# "EOF occurred in violation of protocol" reset; ``ConnectionError`` covers
# broken pipe / connection reset; the ``http.client`` and ``socket`` entries
# cover half-closed connections and timeouts. ``ssl.SSLError`` is itself an
# ``OSError`` subclass, as are ``ConnectionError`` and ``socket.timeout``.
_TRANSIENT_CONN_ERRORS = (
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
    socket.timeout,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
)


def retry_on_transient(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
) -> T:
    """Call *fn* and retry on transient Google API errors with backoff.

    Retries transient ``HttpError`` statuses (404/408/429/5xx) and
    connection-level blips (TLS resets, broken pipes, timeouts). Re-raises
    immediately on non-transient errors (e.g. 400 bad request, 403 permission
    denied) and after the final attempt. Delays grow as ``base_delay * 2**i``
    (0.5s, 1s, 2s by default → ~3.5s max wait).
    """
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in _TRANSIENT_STATUSES or i == attempts - 1:
                raise
            last_exc = exc
            delay = base_delay * (2 ** i)
            log.warning(
                "Transient Google API error (status=%s); retry %d/%d in %.1fs",
                status, i + 1, attempts - 1, delay,
            )
            time.sleep(delay)
        except _TRANSIENT_CONN_ERRORS as exc:
            if i == attempts - 1:
                raise
            last_exc = exc
            delay = base_delay * (2 ** i)
            log.warning(
                "Transient connection error (%s: %s); retry %d/%d in %.1fs",
                type(exc).__name__, exc, i + 1, attempts - 1, delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # unreachable: loop either returns or raises
    raise last_exc
