"""HTTP error messaging: describe_http_error() plus the end-to-end metadata
403 path (the link.testfile.org field report).
"""
import time

import pytest
import requests

from adp.core.downloader import (
    DownloadManager, MetadataFetcher, MetadataFetcherSignals, describe_http_error,
)


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(f"{status} error", response=response)


class TestDescribeHttpError:
    def test_403_mentions_download_may_still_work(self):
        msg = describe_http_error(_http_error(403))
        assert "403" in msg
        assert "still work" in msg.lower()

    def test_401_mentions_authorization(self):
        msg = describe_http_error(_http_error(401))
        assert "401" in msg and ("login" in msg.lower() or "authoriz" in msg.lower())

    def test_404_mentions_not_found(self):
        msg = describe_http_error(_http_error(404))
        assert "404" in msg and "found" in msg.lower()

    def test_429_mentions_rate_limit(self):
        msg = describe_http_error(_http_error(429))
        assert "429" in msg and "rate" in msg.lower()

    def test_5xx_blames_server(self):
        msg = describe_http_error(_http_error(503))
        assert "503" in msg and "server" in msg.lower()

    def test_ssl_error_message(self):
        msg = describe_http_error(requests.exceptions.SSLError("bad cert"))
        assert "certificate" in msg.lower()

    def test_connection_error_message(self):
        msg = describe_http_error(requests.exceptions.ConnectionError("refused"))
        assert "connect" in msg.lower()

    def test_unknown_falls_back_to_raw_string(self):
        exc = requests.RequestException("some novel failure")
        assert describe_http_error(exc) == "some novel failure"


def pump_events(app, condition, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


class TestMetadata403Path:
    def test_forbidden_metadata_emits_friendly_message(self, qapp, thread_pool, mock_server):
        """A 403 on both HEAD and GET (the field scenario) must surface the
        plain-language 403 guidance, not a raw 'Forbidden for url' string."""
        mock_server.add_file("blocked.bin", b"x" * 1000)
        mock_server.set_forbidden("blocked.bin")

        signals = MetadataFetcherSignals()
        result = {}
        signals.metadata_fetched.connect(lambda *a: result.update(ok=True))
        signals.error_occurred.connect(lambda msg: result.update(error=msg))

        fetcher = MetadataFetcher(mock_server.url_for("blocked.bin"), signals=signals)
        thread_pool.start(fetcher)
        assert pump_events(qapp, lambda: bool(result), timeout=8)

        assert "ok" not in result
        assert "403" in result["error"]
        assert "still work" in result["error"].lower()
        # The raw requests string must not leak through.
        assert "Forbidden for url" not in result["error"]

    def test_manager_reports_friendly_error_on_403(self, qapp, thread_pool, mock_server, download_dir):
        """The download itself (chunked engine needs the size up front) still
        goes to ERROR on a 403, but with the humanized message rather than a
        'Metadata Error: 403 Client Error...' string."""
        import os
        mock_server.add_file("blocked2.bin", b"y" * 1000)
        mock_server.set_forbidden("blocked2.bin")

        errors = {}
        manager = DownloadManager(
            download_id="dl-403",
            url=mock_server.url_for("blocked2.bin"),
            save_path=os.path.join(download_dir, "blocked2.bin"),
            thread_pool=thread_pool,
            num_threads=4,
        )
        manager.error_occurred.connect(lambda did, msg: errors.update(msg=msg))
        manager.start()
        assert pump_events(qapp, lambda: bool(errors), timeout=8)

        assert "403" in errors["msg"]
        assert "Metadata Error:" not in errors["msg"]  # old raw prefix gone
        assert "Forbidden for url" not in errors["msg"]
