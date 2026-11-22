# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Resume integrity: ADP must never splice bytes from two different versions
of a file into one output.

Reported in external review: progress was resumed when the server's file had
been replaced by a different one of the same size -- the ETag check was
skipped when the server stopped sending one, and Last-Modified was saved but
never compared. A same-size check proves nothing. These tests pin down:

* the validator rules used when resuming saved progress (unit level), and
* end-to-end behaviour against a real local HTTP server whose file is swapped
  mid-download, including servers that ignore If-Range.
"""
import os
import time

import pytest

from adp.core.downloader import DownloadManager
from adp.core.downloader.http import (
    RemoteFileChangedError, if_range_value, validators_match,
)
from adp.core.models import Status

LM_OLD = "Mon, 02 Nov 2026 10:00:00 GMT"
LM_NEW = "Tue, 03 Nov 2026 07:00:00 GMT"


def pump_events(app, condition, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


# -- the resume rules ------------------------------------------------------------
@pytest.fixture
def manager(thread_pool, tmp_path):
    return DownloadManager("dl-rules", "http://example.invalid/f.bin",
                           str(tmp_path / "f.bin"), thread_pool)


def _check(manager, saved, server, checksum=None):
    manager.server_etag, manager.server_last_modified = server
    manager.checksum = checksum
    return manager._saved_progress_matches_server({"etag": saved[0], "last_modified": saved[1]})


@pytest.mark.parametrize("saved,server,expected", [
    (('"a"', None), ('"a"', None), True),          # same ETag
    (('"a"', None), ('"b"', None), False),         # same size + different ETag
    (('"a"', LM_OLD), (None, LM_OLD), False),      # ETag disappeared
    ((None, LM_OLD), ('"a"', LM_OLD), False),      # ETag appeared
    ((None, LM_OLD), (None, LM_OLD), True),        # no ETag, same Last-Modified
    ((None, LM_OLD), (None, LM_NEW), False),       # no ETag + different Last-Modified
    ((None, LM_OLD), (None, None), False),         # Last-Modified disappeared
    ((None, None), (None, None), False),           # no validators at all -> restart
])
def test_resume_validator_rules(manager, saved, server, expected):
    assert _check(manager, saved, server) is expected


def test_no_validators_but_checksum_allows_resume(manager):
    """A supplied checksum verifies the finished file, so resuming is safe."""
    assert _check(manager, (None, None), (None, None), checksum="ab" * 32) is True


def test_if_range_value_never_uses_weak_etag():
    assert if_range_value('"strong"', LM_OLD) == '"strong"'
    assert if_range_value('W/"weak"', LM_OLD) == LM_OLD   # weak ETags never match If-Range
    assert if_range_value('W/"weak"', None) is None
    assert if_range_value(None, None) is None


def test_validators_match():
    assert validators_match('"a"', None, {"ETag": '"a"'})
    assert validators_match('"a"', None, {"ETag": 'W/"a"'})   # proxy weakened it
    assert not validators_match('"a"', None, {"ETag": '"b"'})
    assert validators_match(None, LM_OLD, {"Last-Modified": LM_OLD})
    assert not validators_match(None, LM_OLD, {"Last-Modified": LM_NEW})
    assert validators_match(None, None, {"ETag": '"x"'})       # nothing to compare


# -- end to end ---------------------------------------------------------------------
PATH = "release.iso"
SIZE = 1_500_000


def _start_partial(qapp, thread_pool, mock_server, download_dir, *, num_threads=2):
    """Starts a throttled download and pauses it partway."""
    old, new = os.urandom(SIZE), os.urandom(SIZE)  # same size, different bytes
    mock_server.add_file(PATH, old)
    mock_server.set_throttle(PATH, 400_000)
    save_path = os.path.join(download_dir, PATH)
    m = DownloadManager("dl-swap", mock_server.url_for(PATH), save_path, thread_pool,
                        num_threads=num_threads)
    m.start()
    assert pump_events(qapp, lambda: m.status == Status.DOWNLOADING and m.downloaded_size > 50_000)
    m.pause()
    assert pump_events(qapp, lambda: m.status == Status.PAUSED)
    time.sleep(0.2)
    qapp.processEvents()
    return m, old, new, save_path


def _expect_changed_error(qapp, m, save_path, old, new):
    assert pump_events(qapp, lambda: m.status.is_terminal, timeout=20)
    assert m.status == Status.ERROR
    assert m.traceback_info == RemoteFileChangedError.USER_MESSAGE
    with open(save_path, "rb") as f:
        data = f.read()
    assert data != new  # and certainly never reported complete


def test_file_replaced_while_paused_is_detected_via_if_range(qapp, thread_pool, mock_server, download_dir):
    m, old, new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir)
    mock_server.add_file(PATH, new)  # same size, new content -> new ETag
    m.resume()
    _expect_changed_error(qapp, m, save_path, old, new)


def test_file_replaced_detected_even_if_server_ignores_if_range(qapp, thread_pool, mock_server, download_dir):
    mock_server.set_ignore_if_range(PATH)
    m, old, new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir)
    mock_server.add_file(PATH, new)
    m.resume()
    _expect_changed_error(qapp, m, save_path, old, new)


def test_file_replaced_with_only_last_modified_is_detected(qapp, thread_pool, mock_server, download_dir):
    """Server sends no ETag at all, only Last-Modified."""
    mock_server.set_validators(PATH, etag=None, last_modified=LM_OLD)
    m, old, new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir)
    mock_server.add_file(PATH, new)
    mock_server.set_validators(PATH, etag=None, last_modified=LM_NEW)
    m.resume()
    _expect_changed_error(qapp, m, save_path, old, new)


def test_retry_after_change_downloads_new_version_cleanly(qapp, thread_pool, mock_server, download_dir):
    m, old, new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir)
    mock_server.add_file(PATH, new)
    m.resume()
    assert pump_events(qapp, lambda: m.status == Status.ERROR, timeout=20)
    mock_server.set_throttle(PATH, 0)
    m.retry()
    assert pump_events(qapp, lambda: m.status.is_terminal, timeout=20)
    assert m.status == Status.COMPLETED
    with open(save_path, "rb") as f:
        assert f.read() == new  # the whole new file -- no old prefix


@pytest.mark.parametrize("validators", [
    {"etag": None, "last_modified": None},   # the review's "no validators" case
    {"etag": None, "last_modified": LM_OLD},  # no ETag, Last-Modified changes
])
def test_restart_with_replaced_same_size_file_never_splices(qapp, thread_pool, mock_server,
                                                            download_dir, validators):
    """The exact scenario from the review: 40% downloaded, app restarts, the
    server has meanwhile replaced the file with a same-size one."""
    mock_server.set_validators(PATH, **validators)
    first, old, new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir,
                                                num_threads=1)
    first.stop_all_workers()  # simulate the process exiting (keeps the .progress file)
    time.sleep(0.3)
    qapp.processEvents()
    assert os.path.exists(first.progress_file)

    mock_server.add_file(PATH, new)
    if validators["last_modified"]:
        mock_server.set_validators(PATH, etag=None, last_modified=LM_NEW)
    mock_server.set_throttle(PATH, 0)
    # allow_overwrite mirrors a restored ADP-owned download (the panel sets it).
    second = DownloadManager("dl-swap", mock_server.url_for(PATH), save_path, thread_pool,
                             num_threads=1, allow_overwrite=True)
    second.start()
    assert pump_events(qapp, lambda: second.status.is_terminal, timeout=20)
    assert second.status == Status.COMPLETED
    with open(save_path, "rb") as f:
        assert f.read() == new


def test_unchanged_file_still_resumes_across_restart(qapp, thread_pool, mock_server, download_dir):
    """The fix must not cost normal resumes: same file + matching validators
    picks up where it left off instead of re-downloading."""
    mock_server.set_validators(PATH, etag='"v1"', last_modified=LM_OLD)
    first, old, _new, save_path = _start_partial(qapp, thread_pool, mock_server, download_dir,
                                                 num_threads=1)
    partial = first.downloaded_size
    first.stop_all_workers()
    time.sleep(0.3)
    qapp.processEvents()
    mock_server.set_throttle(PATH, 0)
    second = DownloadManager("dl-swap", mock_server.url_for(PATH), save_path, thread_pool, num_threads=1)
    second.start()
    assert pump_events(qapp, lambda: second.status.is_terminal, timeout=20)
    assert second.status == Status.COMPLETED
    assert second.downloaded_at_start >= partial  # resumed, not restarted
    with open(save_path, "rb") as f:
        assert f.read() == old
