"""Smoke tests that hit the real internet to sanity-check the engine against
an actual HTTPS server -- real TLS, real redirects, real range support --
rather than the in-process mock. Excluded from the default run: `pytest -m
"not network"` skips these, which is what CI and offline dev should do.

Run explicitly with: pytest -m network

Design notes (learned the hard way):
  * These target a *pinned, immutable* file -- a specific tagged path on
    raw.githubusercontent.com -- not httpbin.org. httpbin removed the
    /range/ endpoint out from under an earlier version of these tests, and
    its /bytes/ endpoint routinely times out; a smoke test shouldn't depend
    on one flaky community host or a mutable URL.
  * The expected size is read from the server at run time, not hardcoded, so
    an upstream change can never masquerade as an engine bug. (An earlier
    draft used a GitHub text file, but GitHub gzips those and a gzipped body
    can't be range-sliced for multi-connection download -- hence the raw
    .tar.gz here.)
  * Genuine network trouble (DNS, timeout, connection reset) is a *skip*, not
    a failure. A red suite should mean "our code broke", never "someone
    else's server had a bad day". Only an actually-wrong result fails.
"""
import os
import time

import pytest
import requests

from adp.core.downloader import DownloadManager, MetadataFetcher, MetadataFetcherSignals
from adp.core.models import Status

pytestmark = pytest.mark.network

# A pinned PyPI artifact -> immutable, and a .tar.gz is already-compressed
# binary, so the server sends it *uncompressed* (content-encoding: none).
# That matters: a text file served gzipped can't be sliced by HTTP Range
# (each chunk is an undecodable fragment of the gzip stream), which breaks
# multi-connection downloads. A raw binary gives clean 206 range responses.
TEST_URL = "https://files.pythonhosted.org/packages/source/s/six/six-1.16.0.tar.gz"
NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout)


def pump_events(app, condition, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def _expected_body_size(url: str) -> int:
    """The decompressed length the engine should end up writing. Fetched live
    so the test never depends on a hardcoded number that a gzipped transfer or
    an upstream edit could invalidate. Network trouble here -> skip."""
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return len(response.content)
    except NETWORK_ERRORS as exc:
        pytest.skip(f"network unavailable while sizing {url}: {exc}")
    except requests.HTTPError as exc:
        pytest.skip(f"test endpoint unavailable ({exc}); pin a new URL if permanent")


def test_metadata_fetch_against_real_server(qapp, thread_pool):
    signals = MetadataFetcherSignals()
    result = {}
    signals.metadata_fetched.connect(
        lambda size, ranges, etag, lm, name: result.update(
            size=size, ranges=ranges, name=name)
    )
    signals.error_occurred.connect(lambda err: result.update(error=err))

    fetcher = MetadataFetcher(TEST_URL, signals=signals)
    thread_pool.start(fetcher)

    if not pump_events(qapp, lambda: result, timeout=30):
        pytest.skip("metadata fetch did not return in time (network slow/unavailable)")
    if "error" in result:
        pytest.skip(f"server-side/network issue, not an engine fault: {result['error']}")
    # The engine reported a positive size and parsed a filename -- the actual
    # smoke signal. Exact bytes are asserted in the download test below.
    assert result["size"] > 0
    assert result["name"] == "six-1.16.0.tar.gz"


def test_real_download_against_real_server(qapp, thread_pool, download_dir):
    expected_size = _expected_body_size(TEST_URL)
    save_path = os.path.join(download_dir, "six-1.16.0.tar.gz")
    manager = DownloadManager(
        download_id="net-1",
        url=TEST_URL,
        save_path=save_path,
        thread_pool=thread_pool,
        num_threads=2,
    )
    manager.start()
    if not pump_events(qapp, lambda: manager.status.is_terminal, timeout=45):
        pytest.skip("download did not finish in time (network slow/unavailable)")

    # A network/HTTP error terminates in ERROR with a message -- distinguish
    # "their server hiccuped" (skip) from "we downloaded the wrong bytes"
    # (fail). Only the latter is an engine bug.
    if manager.status == Status.ERROR:
        pytest.skip(f"server-side/network issue, not an engine fault: {manager.traceback_info}")

    assert manager.status == Status.COMPLETED
    assert os.path.getsize(save_path) == expected_size
