# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Shared HTTP concerns for the download engine: browser-like headers, the
range-integrity exception, and human-friendly error messaging.

Kept free of Qt so it can be imported and tested in isolation.
"""

import requests

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,'
              'image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
    # Identity, deliberately: this is a resumable *byte-range* engine, and an
    # HTTP byte range refers to bytes of the *selected representation*. If the
    # server compresses the response, our filesystem offsets (raw bytes) and
    # the range offsets (compressed bytes) stop corresponding, and requests'
    # transparent decoding can splice mismatched data. Correctness beats the
    # few saved bytes on compressible files. Applies to metadata probes and
    # ranged GETs alike.
    'Accept-Encoding': 'identity',
    'Connection': 'keep-alive',
}

CHUNK_READ_SIZE = 8192


class RangeNotHonoredError(requests.RequestException):
    """Raised when a server answers a Range request with something other than a
    valid 206 partial response. A RequestException subclass so it flows through
    the worker's existing error handling and fails the download loudly rather
    than corrupting the output file."""


def describe_http_error(exc: "requests.RequestException") -> str:
    """Turn a requests exception into a short, plain-language explanation a
    non-developer can act on. The raw '403 Client Error: Forbidden for url:
    ...' string is accurate but unhelpful; these messages say what it likely
    means and what to try. Falls back to the raw string for anything we don't
    have specific advice for."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 401:
        return ("The server requires a login or authorization for this file (HTTP 401). "
                "If it's behind an account, you may need a direct/authorized link.")
    if status == 403:
        return ("The server refused the request (HTTP 403). Some sites block download "
                "managers or links opened without a browser session. The direct download "
                "may still work -- you can set a save location and try starting it anyway.")
    if status == 404:
        return ("The file wasn't found at that URL (HTTP 404). The link may be expired, "
                "mistyped, or the file has been removed.")
    if status == 429:
        return ("The server is rate-limiting requests (HTTP 429). Wait a little while and "
                "try again.")
    if status is not None and 500 <= status < 600:
        return (f"The server had an internal error (HTTP {status}). This is on the server's "
                "side; trying again later may work.")
    if isinstance(exc, requests.exceptions.SSLError):
        return ("The server's TLS certificate could not be verified. If you trust this "
                "server, re-add the download with certificate verification turned off.")
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "The server didn't respond in time (connection timed out)."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ("Couldn't connect to the server. Check the URL and your internet "
                "connection.")
    return str(exc)
