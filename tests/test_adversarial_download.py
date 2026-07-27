# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Adversarial tests: the engine must reject hostile/broken server behavior
and untrusted API input rather than corrupting output or escaping sandboxes.

These are the tests that would have caught the P0s in review:
  * Range-ignoring / lying servers -> download fails, not corrupts.
  * Path traversal via torrent filename -> confined to the temp dir.
  * Restart persistence -> active/paused downloads actually survive.
"""
import base64
import os
import time

import pytest

from adp.core.downloader import DownloadManager
from adp.core.models import Status
from evil_server import EvilServer, EVIL_BODY


def pump(app, condition, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def _run(qapp, thread_pool, download_dir, url, threads=4):
    manager = DownloadManager(
        download_id="evil", url=url, save_path=os.path.join(download_dir, "out.bin"),
        thread_pool=thread_pool, num_threads=threads,
    )
    manager.start()
    assert pump(qapp, lambda: manager.status.is_terminal, timeout=25), "did not terminate"
    return manager


# --- Range corruption defenses (P0 #2/#3) ---------------------------------

class TestRangeDefenses:
    def test_server_ignoring_range_is_rejected(self, qapp, thread_pool, download_dir):
        """200-with-whole-file in response to a Range request must fail the
        download, never be written at a chunk offset."""
        with EvilServer("ignore_range") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_wrong_content_range_is_rejected(self, qapp, thread_pool, download_dir):
        """A 206 whose Content-Range start != requested start must be rejected."""
        with EvilServer("wrong_content_range") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_missing_content_range_is_rejected(self, qapp, thread_pool, download_dir):
        """A 206 with no Content-Range header at all must be rejected."""
        with EvilServer("no_content_range") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_wrong_content_range_end_is_rejected(self, qapp, thread_pool, download_dir):
        """A 206 with the right start but a different end than requested is a
        contract violation -- the server handed us a slice we didn't ask for."""
        with EvilServer("wrong_content_range_end") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_wrong_content_range_total_is_rejected(self, qapp, thread_pool, download_dir):
        """A 206 whose reported total differs from the download's known size
        means the resource changed under us; reject rather than splice."""
        with EvilServer("wrong_content_range_total") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_unknown_total_is_rejected(self, qapp, thread_pool, download_dir):
        """A 206 with an unknown total (bytes start-end/*) on a sized resumable
        download must be rejected -- we can't confirm the resource is unchanged."""
        with EvilServer("unknown_total") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        assert manager.status == Status.ERROR

    def test_oversending_server_does_not_overflow_file(self, qapp, thread_pool, download_dir):
        """A server that streams more than the requested range must not push
        the output past the expected size. Either it's capped to the correct
        size and completes, or it's rejected -- never a too-large file."""
        with EvilServer("oversend") as server:
            manager = _run(qapp, thread_pool, download_dir, server.url)
        out = os.path.join(download_dir, "out.bin")
        if manager.status == Status.COMPLETED:
            # If it completed, the file must be exactly right despite overflow.
            assert os.path.getsize(out) == len(EVIL_BODY)
            with open(out, "rb") as f:
                assert f.read() == EVIL_BODY
        else:
            # Otherwise it must have failed cleanly, not written a bloated file.
            assert manager.status == Status.ERROR
            if os.path.exists(out):
                assert os.path.getsize(out) <= len(EVIL_BODY)


# --- Path traversal defense (P0 #1) ---------------------------------------

class TestTorrentUploadTraversal:
    def _controller(self, tmp_path):
        # Minimal controller with a fake torrent panel that records the path
        # libtorrent would have been handed, without needing libtorrent.
        from adp.api.controller import AppController

        class FakeEngine:
            def __init__(self):
                self.added_paths = []

        class FakePanel:
            def __init__(self, base):
                self.default_save_path = base
                self.engine = FakeEngine()
                self.records = {}

            def add_torrent(self, mode, torrent_file_path=None, **kw):
                # Record where the file actually landed on disk.
                self.engine.added_paths.append(torrent_file_path)
                return "tid-1"

        panel = FakePanel(str(tmp_path))

        class DirectBridge:
            def call(self, fn, *a, **k):
                return fn(*a, **k)

        controller = AppController(
            bridge=DirectBridge(), download_panel=None,
            torrent_panel=panel, stats_panel=None,
        )
        controller._settle_after_add = lambda *_a, **_k: None
        controller._serialize_torrent = lambda tid: {"id": tid}
        return controller, panel

    @pytest.mark.parametrize("evil_name", [
        "../../../../etc/passwd.torrent",
        "..\\..\\..\\Windows\\evil.torrent",
        "/etc/cron.d/evil.torrent",
        "C:\\Windows\\Temp\\evil.torrent",
    ])
    def test_malicious_filename_cannot_escape_tempdir(self, tmp_path, evil_name):
        controller, panel = self._controller(tmp_path)
        payload = base64.b64encode(b"d4:testi1ee").decode()  # arbitrary bytes
        controller.add_torrent(torrent_file_base64=payload, torrent_file_name=evil_name)

        assert len(panel.engine.added_paths) == 1
        written = panel.engine.added_paths[0]
        # The file must live under a temp dir and be named upload.torrent --
        # never at the attacker-chosen location.
        assert os.path.basename(written) == "upload.torrent"
        assert "etc" not in written.lower().split(os.sep)[:-1] or "tmp" in written.lower()
        # Most importantly, none of the traversal targets exist afterwards.
        for target in ("/etc/passwd.torrent", "/etc/cron.d/evil.torrent"):
            assert not os.path.exists(target)

    def test_oversized_torrent_rejected(self, tmp_path):
        from adp.api.controller import ApiError, MAX_TORRENT_UPLOAD_BYTES
        controller, _ = self._controller(tmp_path)
        huge = base64.b64encode(b"x" * (MAX_TORRENT_UPLOAD_BYTES + 1)).decode()
        with pytest.raises(ApiError):
            controller.add_torrent(torrent_file_base64=huge)

    def test_both_magnet_and_file_rejected(self, tmp_path):
        from adp.api.controller import ApiError
        controller, _ = self._controller(tmp_path)
        with pytest.raises(ApiError):
            controller.add_torrent(
                magnet_uri="magnet:?xt=urn:btih:" + "a" * 40,
                torrent_file_base64=base64.b64encode(b"d4:testi1ee").decode(),
            )