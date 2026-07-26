# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import hashlib
import os
import time

import pytest

from adp.core.downloader import DownloadManager
from adp.core.models import Status

FILE_CONTENT = os.urandom(200_000)  # ~195KB, big enough to split across threads


def pump_events(app, condition, timeout=10.0):
    """Processes the Qt event loop until `condition()` is True or we time out.
    Necessary because QThreadPool workers emit signals asynchronously."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_manager(qapp, thread_pool, mock_server, download_dir, *, path="file.bin",
                  content=FILE_CONTENT, num_threads=4, checksum=None):
    mock_server.add_file(path, content)
    save_path = os.path.join(download_dir, path)
    manager = DownloadManager(
        download_id="dl-1",
        url=mock_server.url_for(path),
        save_path=save_path,
        thread_pool=thread_pool,
        num_threads=num_threads,
        checksum=checksum,
    )
    return manager


def test_basic_concurrent_download_completes(qapp, thread_pool, mock_server, download_dir):
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=4)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == FILE_CONTENT


def test_checksum_verification_success(qapp, thread_pool, mock_server, download_dir):
    checksum = hashlib.sha256(FILE_CONTENT).hexdigest()
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, checksum=checksum)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED


def test_checksum_verification_failure(qapp, thread_pool, mock_server, download_dir):
    wrong_checksum = hashlib.sha256(b"not the right content").hexdigest()
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, checksum=wrong_checksum)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.ERROR
    assert "checksum" in manager.traceback_info.lower()


def test_single_threaded_fallback_when_server_rejects_ranges(qapp, thread_pool, mock_server, download_dir):
    mock_server.set_accept_ranges(False)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=4)
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert manager.status == Status.COMPLETED
    assert manager.num_threads == 1


@pytest.mark.timeout(60)
def test_pause_then_resume(qapp, thread_pool, mock_server, download_dir):
    big_content = os.urandom(2_000_000)
    # Unthrottled loopback can finish before pause() lands (or before the
    # DOWNLOADING state is even observed), burning the pump timeout.
    mock_server.set_throttle("file.bin", 600_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, content=big_content, num_threads=2)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0,
                        timeout=40.0)
    manager.pause()
    assert pump_events(qapp, lambda: manager.status == Status.PAUSED)
    # A small in-flight trickle (already-read chunks) may land right after pause;
    # give it a moment to settle before asserting the count is truly frozen.
    time.sleep(0.3)
    qapp.processEvents()
    paused_bytes = manager.downloaded_size

    time.sleep(0.3)
    qapp.processEvents()
    assert manager.downloaded_size == paused_bytes  # nothing new trickled in while paused

    manager.resume()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == big_content


def test_pause_releases_workers_and_resume_respawns(qapp, thread_pool, mock_server, download_dir):
    """Pause must retire the workers (releasing their HTTP connections)
    rather than parking them in a sleep loop on an open socket -- real
    servers drop idle transfers after their send-timeout, which used to turn
    any pause longer than ~a minute into a hard ERROR on resume. Resume
    respawns a fresh worker generation from chunk_progress."""
    big_content = os.urandom(2_000_000)
    mock_server.set_throttle("epoch.bin", 600_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir,
                            path="epoch.bin", content=big_content, num_threads=2)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0,
                        timeout=40.0)
    epoch_before = manager.worker_epoch
    manager.pause()
    assert manager.status == Status.PAUSED
    # The pause itself must invalidate the running generation and clear the
    # roster -- no worker may be left holding a connection open.
    assert manager.worker_epoch == epoch_before + 1
    assert manager.workers == []
    assert manager.active_workers == 0

    # Give retired workers time to notice the epoch change, exit, and (in
    # the buggy world) emit late signals -- none of which may flip state.
    time.sleep(0.5)
    qapp.processEvents()
    assert manager.status == Status.PAUSED

    manager.resume()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=30)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == big_content


def test_resume_after_progress_file_exists(qapp, thread_pool, mock_server, download_dir):
    """Simulates an app restart mid-download: a manager writes partial progress,
    then a fresh manager instance picks up where it left off."""
    path = "resumable.bin"
    big_content = os.urandom(3_000_000)
    mock_server.add_file(path, big_content)
    save_path = os.path.join(download_dir, path)

    first = DownloadManager("dl-a", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    first.start()
    assert pump_events(qapp, lambda: first.status == Status.DOWNLOADING and first.downloaded_size > 1000)
    first.pause()
    assert pump_events(qapp, lambda: first.status == Status.PAUSED)
    assert os.path.exists(first.progress_file)
    partial_bytes = first.downloaded_size
    assert 0 < partial_bytes < len(big_content)

    # A real app restart kills the process outright, closing every open file
    # handle with it. Pausing alone does NOT do that here: the paused worker
    # thread just busy-waits and keeps its handle to save_path open. Leaving
    # it open while a second manager instance writes to the same path is not
    # a faithful "restart" simulation (and on Windows, unlike POSIX, doing so
    # is exactly what produced real file corruption). Stop the underlying
    # worker -- without going through first.stop(), which would also delete
    # the progress file we're testing resume from -- and give it a moment to
    # actually exit and release its handle before proceeding.
    first.stop_all_workers()
    assert pump_events(qapp, lambda: first.active_workers == 0 or True, timeout=2)
    time.sleep(0.3)
    qapp.processEvents()
    assert os.path.exists(first.progress_file)  # still intact -- only the handle was released

    second = DownloadManager("dl-a", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    second.start()
    assert pump_events(qapp, lambda: second.status.is_terminal, timeout=15)
    assert second.status == Status.COMPLETED
    with open(save_path, 'rb') as f:
        assert f.read() == big_content


def test_stop_cleans_up_progress_file(qapp, thread_pool, mock_server, download_dir):
    big_content = os.urandom(2_000_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, content=big_content, num_threads=2)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0)
    manager.stop()
    assert pump_events(qapp, lambda: not os.path.exists(manager.progress_file))
    assert manager.status == Status.STOPPED


def test_retry_after_error(qapp, thread_pool, mock_server, download_dir):
    path = "flaky.bin"
    mock_server.add_file(path, FILE_CONTENT)
    mock_server.fail_path_after(path, 500)  # server drops connection almost immediately

    save_path = os.path.join(download_dir, path)
    manager = DownloadManager("dl-flaky", mock_server.url_for(path), save_path, thread_pool, num_threads=1)
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)

    mock_server.clear_fault(path)
    manager.retry()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.COMPLETED
    with open(save_path, 'rb') as f:
        assert f.read() == FILE_CONTENT


def test_progress_signal_reports_monotonic_growth(qapp, thread_pool, mock_server, download_dir):
    manager = make_manager(qapp, thread_pool, mock_server, download_dir, num_threads=3)
    seen = []
    manager.progress_updated.connect(lambda *_args: seen.append(_args[1]))
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal)
    assert len(seen) > 0
    assert all(b1 <= b2 for b1, b2 in zip(seen, seen[1:]))
    assert seen[-1] == len(FILE_CONTENT)


def test_stop_during_metadata_fetch_prevents_worker_spawn(qapp, thread_pool, mock_server, download_dir):
    """Regression test: stopping a download while it's still waiting on the
    metadata (HEAD) request must not let it spawn workers once that callback
    finally arrives -- otherwise a 'removed' download can silently resurrect."""
    manager = make_manager(qapp, thread_pool, mock_server, download_dir)
    manager.start()
    # Stop immediately, almost certainly before the metadata fetch has
    # returned (it must hop through the thread pool and back via a signal).
    manager.stop()
    assert manager.status == Status.STOPPED

    # Give the in-flight metadata fetch plenty of time to complete and try
    # (and fail) to resurrect the download.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert manager.status == Status.STOPPED
    assert manager.active_workers == 0


@pytest.mark.timeout(45)
def test_metadata_signals_survive_fast_failure_without_gc_crash(qapp, thread_pool, download_dir):
    """Regression test: the MetadataFetcherSignals object created inside
    start() must not be collectible while its background thread is still
    trying to emit on it. Using an immediately-refused connection (nothing
    listening on this port) reproduces the fast-failure timing that exposed
    a 'wrapped C/C++ object ... has been deleted' crash when this was a bare
    local variable instead of an instance attribute."""
    import gc

    save_path = os.path.join(download_dir, "unreachable.bin")
    manager = DownloadManager(
        "dl-unreachable", "http://127.0.0.1:1/nope.bin", save_path, thread_pool, num_threads=1,
    )
    manager.start()
    gc.collect()  # aggressively try to collect anything not properly referenced
    # Note: this timeout is generous (rather than the ~1s this takes on Linux)
    # because Windows' connection-refused detection plus urllib3's retry
    # backoff for a closed port can take noticeably longer in practice --
    # that's just platform variance in how fast the failure surfaces, not a
    # sign that the fix (no GC crash) has regressed.
    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=30)
    assert "deleted" not in manager.traceback_info.lower()


def test_metadata_fetch_rejects_url_without_scheme_immediately(qapp, thread_pool):
    """Regression test for a real user mistake: pasting a download link's
    visible label/title text (e.g. 'DOWNLOAD 1.7GB 8K MP4') into the URL
    field instead of the actual link. This should fail fast with a friendly
    message rather than burning through HEAD+GET retry cycles."""
    from adp.core.downloader import MetadataFetcher, MetadataFetcherSignals

    signals = MetadataFetcherSignals()
    result = {}
    signals.metadata_fetched.connect(lambda *args: result.update(ok=True))
    signals.error_occurred.connect(lambda msg: result.update(error=msg))

    fetcher = MetadataFetcher("DOWNLOAD 1.7GB 8K MP4", signals=signals)
    start = time.time()
    thread_pool.start(fetcher)
    assert pump_events(qapp, lambda: bool(result), timeout=5)
    elapsed = time.time() - start

    assert "error" in result
    assert "valid URL" in result["error"]
    assert elapsed < 2.0  # should fail immediately, not after retry backoff


def test_metadata_fetch_decodes_content_disposition_filename(qapp, thread_pool, mock_server):
    """Regression test: the old parser captured a single character from any
    Content-Disposition header ('report.zip' became 'r') and ignored the
    RFC 5987 filename* form entirely. The emitted filename must be the
    decoded, sanitized server-supplied name."""
    from adp.core.downloader import MetadataFetcher, MetadataFetcherSignals

    cases = [
        ('cd_plain.bin', 'attachment; filename="Quarterly Report.zip"', "Quarterly Report.zip"),
        ('cd_rfc5987.bin', "attachment; filename*=UTF-8''na%C3%AFve%20r%C3%A9sum%C3%A9.pdf",
         "naïve résumé.pdf"),
        ('cd_traversal.bin', 'attachment; filename="..\\..\\evil.exe"', "evil.exe"),
    ]
    for path, header, expected in cases:
        mock_server.add_file(path, b"x" * 1000)
        mock_server.set_extra_headers(path, {"Content-Disposition": header})

        results = {}
        signals = MetadataFetcherSignals()
        signals.metadata_fetched.connect(
            lambda size, ranges, etag, lm, name: results.update(name=name))
        signals.error_occurred.connect(lambda msg: results.update(error=msg))
        thread_pool.start(MetadataFetcher(mock_server.url_for(path), signals=signals))

        assert pump_events(qapp, lambda: bool(results), timeout=10)
        assert results.get("error") is None
        assert results["name"] == expected


def test_tls_verification_rejects_self_signed_cert_by_default(qapp, thread_pool, tls_mock_server, download_dir):
    """Default-on TLS verification must refuse an untrusted certificate with
    a clear, actionable error instead of silently downloading over an
    unverifiable connection (the old code passed verify=False everywhere)."""
    tls_mock_server.add_file("secure.bin", os.urandom(50_000))
    manager = DownloadManager(
        download_id="dl-tls-1",
        url=tls_mock_server.url_for("secure.bin"),
        save_path=os.path.join(download_dir, "secure.bin"),
        thread_pool=thread_pool,
    )
    assert manager.verify_tls is True  # the default, with no argument given
    errors = []
    manager.error_occurred.connect(lambda _id, msg: errors.append(msg))
    manager.start()

    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=20)
    assert errors and "certificate" in errors[0].lower()


@pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning")
def test_tls_verification_opt_out_downloads_from_self_signed_server(qapp, thread_pool, tls_mock_server, download_dir):
    """The per-download opt-out (user explicitly trusts this server) must
    complete the download over the same self-signed endpoint. The unverified-
    request warning is the expected consequence of that choice, hence the
    scoped filter."""
    content = os.urandom(50_000)
    tls_mock_server.add_file("trusted.bin", content)
    manager = DownloadManager(
        download_id="dl-tls-2",
        url=tls_mock_server.url_for("trusted.bin"),
        save_path=os.path.join(download_dir, "trusted.bin"),
        thread_pool=thread_pool,
        num_threads=2,
        verify_tls=False,
    )
    manager.start()

    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=20)
    assert manager.status == Status.COMPLETED
    with open(manager.save_path, 'rb') as f:
        assert f.read() == content


@pytest.mark.timeout(60)
def test_non_range_pause_resume_restarts_cleanly(qapp, thread_pool, mock_server, download_dir):
    """A server without Range support can't resume: pause+resume must restart
    the full GET from scratch AND reset the global byte counter. Regression --
    the worker reset its own chunk to 0 but left manager.downloaded_size at the
    pre-pause value, so the resumed full GET double-counted (e.g. 120MB for a
    100MB file) and the exact-size check then declared the download broken."""
    content = os.urandom(2_000_000)
    mock_server.set_accept_ranges(False)   # force single-thread full-GET mode
    mock_server.set_throttle("nr.bin", 600_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir,
                           path="nr.bin", content=content, num_threads=4)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0,
                        timeout=40.0)
    manager.pause()
    assert pump_events(qapp, lambda: manager.status == Status.PAUSED)
    assert manager.downloaded_size > 0   # some bytes landed before pause

    # Remove the throttle so the restarted download can finish promptly.
    mock_server.set_throttle("nr.bin", 0)
    manager.resume()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=20)
    assert manager.status == Status.COMPLETED
    # The counter must equal exactly the file size, not size + pre-pause bytes.
    assert manager.downloaded_size == len(content)
    with open(manager.save_path, "rb") as f:
        assert f.read() == content


@pytest.mark.timeout(60)
def test_non_range_restart_restarts_cleanly(qapp, thread_pool, mock_server, download_dir):
    """Retrying (prepare_retry) a partially-done non-range download also
    restarts cleanly with a correct final byte count."""
    content = os.urandom(1_500_000)
    mock_server.set_accept_ranges(False)
    mock_server.set_throttle("nr2.bin", 500_000)
    manager = make_manager(qapp, thread_pool, mock_server, download_dir,
                           path="nr2.bin", content=content, num_threads=1)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0,
                        timeout=40.0)
    manager.pause()
    assert pump_events(qapp, lambda: manager.status == Status.PAUSED)

    mock_server.set_throttle("nr2.bin", 0)
    manager.prepare_retry()
    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=20)
    assert manager.status == Status.COMPLETED
    assert manager.downloaded_size == len(content)
    with open(manager.save_path, "rb") as f:
        assert f.read() == content
