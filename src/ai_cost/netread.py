"""Reading a remote answer whole: the one body reader and the one tuple of transport failures every network reader uses.

A body shorter than the length its response declared is a body cut off, never a whole answer: ``read_body`` raises
``http.client.IncompleteRead`` for it, as ``HTTPResponse.read()`` does without a limit and ``read(n)`` does not.
"""

from __future__ import annotations

import http.client
import urllib.error
from typing import Any

# What a request and the read of its answer can raise, the caller's own parsing aside:
#   urllib.error.URLError — no answer (DNS, refused, a timeout wrapped by urllib, a scheme refused by the caller) and
#                           its subclass urllib.error.HTTPError, an answer with an error status: a caller that names
#                           the status catches HTTPError before this tuple;
#   http.client.HTTPException — an answer that is not HTTP or ended early (IncompleteRead, BadStatusLine,
#                           RemoteDisconnected is also an OSError);
#   OSError — the socket itself (a reset, a timeout during the read).
TRANSPORT_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)

# What reading an answer as UTF-8 text can raise: a transport failure, or a body that is not UTF-8.
ANSWER_ERRORS = (*TRANSPORT_ERRORS, UnicodeDecodeError)


class PageTooLarge(ValueError):
    """A body longer than the caller's limit is not a whole answer.

    A ``ValueError``, so a caller that already holds ``ValueError`` for an unreadable page holds this too.
    """


def read_body(response: Any, limit: int | None = None) -> bytes:
    """The body of a GET or POST answer (a HEAD answer carries none; no caller sends one), whole or not at all.

    Fewer bytes than the response declared raise ``IncompleteRead``; more than ``limit`` bytes raise ``PageTooLarge``
    (``limit + 1`` bytes are read to tell). A response that declared no length (chunked) is checked by ``http.client``
    itself, which raises ``IncompleteRead`` when its chunks end early.
    """
    declared = getattr(response, "length", None)  # http.client's count of the bytes the response still owes
    declared = declared if isinstance(declared, int) and not isinstance(declared, bool) else None
    if limit is not None and declared is not None and declared > limit:
        raise PageTooLarge(f"the answer declares {declared} bytes, more than {limit}")
    data = bytes(response.read() if limit is None else response.read(limit + 1))
    if limit is not None and len(data) > limit:
        raise PageTooLarge(f"the answer is longer than {limit} bytes")
    if declared is not None and len(data) < declared:
        raise http.client.IncompleteRead(data, declared - len(data))
    return data
