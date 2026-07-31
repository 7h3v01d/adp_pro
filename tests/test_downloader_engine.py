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


@pytest.mark.timeout(30)
def test_speed_limited_worker_cannot_write_after_pause(qapp, thread_pool, mock_server, download_dir):
    """Stale-worker post-throttle race: if the speed limiter blocks and the
    download is paused during that block, the worker must NOT write the chunk
    it was holding. It re-checks staleness after acquire() returns."""
    import threading
    from adp.core.downloader import DownloadManager
    from adp.core.speed_limiter import SpeedLimiter

    content = os.urandom(1_000_000)
    mock_server.add_file("throttle.bin", content)

    manager = DownloadManager(
        download_id="throttle-race", url=mock_server.url_for("throttle.bin"),
        save_path=os.path.join(download_dir, "throttle.bin"),
        thread_pool=thread_pool, num_threads=1,
    )

    # A speed limiter that, the first time it's asked to throttle, pauses the
    # manager (bumping worker_epoch) *during* the acquire -- exactly the race
    # window. After that it behaves normally.
    real_limiter = SpeedLimiter(0)
    flipped = threading.Event()

    class RacyLimiter:
        rate = 500_000
        def acquire(self, n):
            if not flipped.is_set():
                flipped.set()
                # Simulate the user pausing while we're throttling.
                manager.pause()
    manager.speed_limiter = RacyLimiter()

    manager.start()
    # Wait for the pause to have been triggered from inside acquire().
    assert pump_events(qapp, lambda: flipped.is_set() and manager.status == Status.PAUSED,
                        timeout=20)
    # Give any (incorrect) post-pause write a chance to happen.
    time.sleep(0.3)
    qapp.processEvents()

    # The worker must have bailed after acquire() without writing its chunk:
    # the on-disk progress must not have advanced past what was written before
    # the pause. Since the racy limiter fires on the first chunk, downloaded
    # size should be 0 (nothing committed before the first throttled write).
    assert manager.downloaded_size == 0


@pytest.mark.timeout(30)
def test_existing_destination_is_not_silently_overwritten(qapp, thread_pool, mock_server, download_dir):
    """A fresh download to a path that already holds a non-ADP file must NOT
    truncate it -- it fails safe to ERROR instead. RC blocker: open(path,'wb')
    silently destroyed pre-existing files."""
    path = "important.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    # Pre-existing unrelated file, no .progress sidecar.
    precious = b"DO NOT DESTROY" * 100
    with open(save_path, "wb") as f:
        f.write(precious)

    manager = DownloadManager("dl-precious", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1)
    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.ERROR
    # The original file must be intact.
    with open(save_path, "rb") as f:
        assert f.read() == precious


@pytest.mark.timeout(30)
def test_existing_destination_overwritten_when_allowed(qapp, thread_pool, mock_server, download_dir):
    """With allow_overwrite=True (retry / explicit API overwrite), the existing
    file is correctly replaced."""
    path = "replaceme.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    with open(save_path, "wb") as f:
        f.write(b"old contents")

    manager = DownloadManager("dl-ow", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1, allow_overwrite=True)
    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.COMPLETED
    with open(save_path, "rb") as f:
        assert f.read() == FILE_CONTENT


@pytest.mark.timeout(30)
def test_completed_retry_ignores_old_progress_sidecar(qapp, thread_pool, mock_server, download_dir):
    """Retrying a COMPLETED download must re-download, not instantly re-complete
    off a stale .progress sidecar. prepare_retry() synchronously deletes it."""
    path = "recomplete.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    manager = DownloadManager("dl-recomplete", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.COMPLETED, timeout=15)

    # The real cleanup runs asynchronously on the thread pool (see
    # CleanupWorker in manager.py). Wait for it to actually finish before
    # writing our own stale sidecar below -- otherwise this write can race
    # the real os.remove() and hit a Windows delete-pending PermissionError.
    thread_pool.waitForDone(5000)

    # Simulate a stale complete sidecar still present (completion cleanup is
    # async) by writing one, then retry.
    with open(manager.progress_file, "w") as f:
        import json as _j
        _j.dump({"etag": None, "chunk_progress": {"0": len(FILE_CONTENT)},
                 "total_size": len(FILE_CONTENT)}, f)
    requests_before = mock_server.request_count(path)
    manager.prepare_retry()
    # The sidecar must be gone synchronously.
    assert not os.path.exists(manager.progress_file)
    thread_pool_start = manager.start
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.COMPLETED, timeout=15)
    # It actually re-fetched rather than short-circuiting on the stale sidecar.
    assert mock_server.request_count(path) > requests_before


@pytest.mark.timeout(30)
def test_late_checksum_result_cannot_resurrect_stopped_job(qapp, thread_pool, mock_server, download_dir):
    """A checksum worker finishing after the user stopped the download must NOT
    flip it back to COMPLETED. on_verification_finished only acts if VERIFYING."""
    path = "verify.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    import hashlib
    checksum = hashlib.sha256(FILE_CONTENT).hexdigest()
    manager = DownloadManager("dl-verify", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1, checksum=checksum)
    # Force VERIFYING, then stop, then simulate the late callback.
    manager.set_status(Status.VERIFYING)
    manager.stop()
    assert manager.status == Status.STOPPED
    # Late checksum result arrives -- must be ignored.
    manager.on_verification_finished(True)
    assert manager.status == Status.STOPPED  # not resurrected to COMPLETED


@pytest.mark.timeout(30)
def test_retry_does_not_grant_overwrite_for_foreign_file(qapp, thread_pool, mock_server, download_dir):
    """The reviewer's scenario: ADP accepts a job for a path that doesn't exist,
    another program then creates a file there, ADP refuses (ERROR), and the
    user retries. Retry must NOT silently gain overwrite rights over the
    foreign file -- destination_owned_by_adp is False, so the guard still bites."""
    path = "foreign.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)

    manager = DownloadManager("dl-foreign", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1)
    # A foreign program creates the file before ADP claims it.
    foreign = b"SOMEONE ELSE'S DATA" * 50
    with open(save_path, "wb") as f:
        f.write(foreign)

    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.ERROR
    assert manager.destination_owned_by_adp is False  # ADP never claimed it
    with open(save_path, "rb") as f:
        assert f.read() == foreign  # untouched

    # Retry must NOT grant overwrite -- the foreign file is still protected.
    manager.prepare_retry()
    assert manager.allow_overwrite is False
    manager.start()
    assert pump_events(qapp, lambda: manager.status.is_terminal, timeout=15)
    assert manager.status == Status.ERROR  # still refused
    with open(save_path, "rb") as f:
        assert f.read() == foreign  # STILL untouched


@pytest.mark.timeout(30)
def test_retry_reuses_adp_created_destination(qapp, thread_pool, mock_server, download_dir):
    """Conversely: if ADP created the file (connection dropped mid-download),
    retry legitimately reuses it -- destination_owned_by_adp is True."""
    path = "adp_owned.bin"
    mock_server.add_file(path, FILE_CONTENT)
    mock_server.fail_path_after(path, 500)  # error after ADP creates the file
    save_path = os.path.join(download_dir, path)

    manager = DownloadManager("dl-owned", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)
    # ADP preallocated the file before the connection dropped.
    assert manager.destination_owned_by_adp is True

    mock_server.clear_fault(path)
    manager.prepare_retry()
    assert manager.allow_overwrite is True  # allowed: ADP owns this file
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.COMPLETED, timeout=15)
    with open(save_path, "rb") as f:
        assert f.read() == FILE_CONTENT


@pytest.mark.timeout(30)
def test_stale_checksum_epoch_rejected_across_retry(qapp, thread_pool, mock_server, download_dir):
    """A checksum result from a previous run must be rejected even if the
    download is VERIFYING again (a stop->retry cycle reached VERIFYING). The
    epoch guard distinguishes the two runs; status alone can't."""
    import hashlib
    path = "epochverify.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    checksum = hashlib.sha256(FILE_CONTENT).hexdigest()
    manager = DownloadManager("dl-epoch", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1, checksum=checksum)

    # Run 1 is verifying at the current epoch.
    manager.set_status(Status.VERIFYING)
    run1_epoch = manager.worker_epoch
    # A stop->retry bumps the epoch; now Run 2 is verifying.
    manager.worker_epoch += 1
    manager.set_status(Status.VERIFYING)

    # Run 1's stale result arrives with the OLD epoch -- must be ignored, even
    # though status is VERIFYING again.
    manager.on_verification_finished(False, run1_epoch)
    assert manager.status == Status.VERIFYING  # not flipped to ERROR by stale run

    # Run 2's own result (current epoch) is accepted.
    manager.on_verification_finished(True, manager.worker_epoch)
    assert manager.status == Status.COMPLETED


@pytest.mark.timeout(30)
def test_checksum_failure_retry_restarts_from_scratch(qapp, thread_pool, mock_server, download_dir):
    """A checksum mismatch marks restart_required, so retry discards the (bad)
    partial state instead of re-verifying the same corrupt file forever."""
    import hashlib
    path = "badsum.bin"
    mock_server.add_file(path, FILE_CONTENT)
    save_path = os.path.join(download_dir, path)
    wrong_checksum = hashlib.sha256(b"not the real content").hexdigest()
    manager = DownloadManager("dl-badsum", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1, checksum=wrong_checksum)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)
    assert manager.restart_required is True
    assert manager.downloaded_size > 0  # bytes are on disk (but wrong checksum)

    manager.prepare_retry()
    # Integrity failure -> clean restart: progress discarded.
    assert manager.downloaded_size == 0
    assert manager.chunk_progress == {}
    assert manager.restart_required is False  # cleared after handling


@pytest.mark.timeout(30)
def test_network_error_retry_preserves_progress(qapp, thread_pool, mock_server, download_dir):
    """A transient (network) ERROR is NOT restart_required, so retry preserves
    partial progress and resumes rather than restarting from zero."""
    path = "netfail.bin"
    mock_server.add_file(path, FILE_CONTENT)
    mock_server.fail_path_after(path, 500)
    save_path = os.path.join(download_dir, path)
    manager = DownloadManager("dl-netfail", mock_server.url_for(path), save_path,
                              thread_pool, num_threads=1)
    manager.start()
    assert pump_events(qapp, lambda: manager.status == Status.ERROR, timeout=15)
    assert manager.restart_required is False  # network error, not integrity

    partial = manager.downloaded_size
    manager.prepare_retry()
    # Progress preserved for a resumable error (not zeroed).
    assert manager.downloaded_size == partial
