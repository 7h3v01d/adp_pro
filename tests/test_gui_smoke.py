# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os
import time

import pytest
from PyQt6.QtCore import Qt

from adp.core.models import Status
from adp.gui.main_window import DownloadPanel
from adp.gui.dialogs import AddDownloadDialog, SettingsDialog

pytestmark = pytest.mark.gui


def pump(qtbot, condition, timeout=10000):
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture
def panel(qtbot, tmp_path, thread_pool):
    p = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(p)
    yield p
    for manager in list(p.downloads.values()):
        if manager.status.is_active or manager.status == Status.PAUSED:
            manager.stop()


def test_panel_starts_empty(panel):
    assert panel.download_list.count() == 0
    assert panel.category_filter.itemText(0) == "All Categories"


def test_add_download_completes_and_shows_in_list(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("thing.zip", os.urandom(50_000))
    manager, widget = panel.add_download(
        url=mock_server.url_for("thing.zip"),
        save_path=os.path.join(download_dir, "thing.zip"),
        category="Archives",
    )
    assert panel.download_list.count() == 1
    pump(qtbot, lambda: manager.status == Status.COMPLETED, timeout=15000)
    assert widget.category == "Archives"


def test_search_filter_hides_non_matching_items(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("apple.zip", b"x" * 100)
    mock_server.add_file("banana.zip", b"y" * 100)
    m1, _ = panel.add_download(mock_server.url_for("apple.zip"), os.path.join(download_dir, "apple.zip"))
    m2, _ = panel.add_download(mock_server.url_for("banana.zip"), os.path.join(download_dir, "banana.zip"))
    pump(qtbot, lambda: m1.status.is_terminal and m2.status.is_terminal)

    panel.search_input.setText("apple")
    panel.apply_filters()

    visible = [panel.download_list.item(i) for i in range(panel.download_list.count())
               if not panel.download_list.item(i).isHidden()]
    assert len(visible) == 1


def test_category_filter_hides_non_matching_items(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("doc.pdf", b"x" * 100)
    mock_server.add_file("movie.mp4", b"y" * 100)
    m1, _ = panel.add_download(mock_server.url_for("doc.pdf"), os.path.join(download_dir, "doc.pdf"), category="Documents")
    m2, _ = panel.add_download(mock_server.url_for("movie.mp4"), os.path.join(download_dir, "movie.mp4"), category="Video")
    pump(qtbot, lambda: m1.status.is_terminal and m2.status.is_terminal)

    idx = panel.category_filter.findText("Video")
    panel.category_filter.setCurrentIndex(idx)

    visible = [panel.download_list.item(i) for i in range(panel.download_list.count())
               if not panel.download_list.item(i).isHidden()]
    assert len(visible) == 1


def test_pause_stop_via_panel_controls(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("big.bin", os.urandom(2_000_000))
    mock_server.set_throttle("big.bin", 600_000)  # keep it in flight for the mid-download stop
    manager, widget = panel.add_download(
        mock_server.url_for("big.bin"), os.path.join(download_dir, "big.bin"), num_threads=1
    )
    pump(qtbot, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0)

    panel.download_list.setCurrentRow(0)
    panel.stop_selected_download()
    pump(qtbot, lambda: manager.status == Status.STOPPED)
    # Cleanup of the progress file runs on a background CleanupWorker, so give
    # it a moment rather than asserting immediately after the status flips.
    pump(qtbot, lambda: not os.path.exists(manager.progress_file))


def test_speed_limit_is_applied_to_manager(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("throttled.bin", os.urandom(10_000))
    manager, widget = panel.add_download(
        mock_server.url_for("throttled.bin"), os.path.join(download_dir, "throttled.bin"),
        speed_limit_bps=12345,
    )
    assert manager.speed_limiter.rate == 12345


def test_scheduling_defers_start_until_due(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("scheduled.bin", os.urandom(1000))
    from datetime import datetime, timedelta
    future = (datetime.now() + timedelta(hours=1)).isoformat()

    manager, widget = panel.add_download(
        mock_server.url_for("scheduled.bin"), os.path.join(download_dir, "scheduled.bin"),
        scheduled_time=future,
    )
    assert manager.status == Status.PENDING
    assert panel.scheduler.is_scheduled(manager.download_id)

    # Manually trigger "due" as if the clock had advanced, rather than sleeping an hour.
    panel._on_schedule_due(manager.download_id)
    pump(qtbot, lambda: manager.status == Status.COMPLETED, timeout=10000)


def test_session_persistence_round_trip(qtbot, tmp_path, mock_server, download_dir, thread_pool):
    mock_server.add_file("persisted.zip", b"x" * 500)
    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    manager, widget = panel1.add_download(
        mock_server.url_for("persisted.zip"), os.path.join(download_dir, "persisted.zip"),
        category="Archives", start_immediately=False,
    )
    panel1.save_downloads()

    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    assert panel2.download_list.count() == 1
    restored_id = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    assert panel2.downloads[restored_id].category == "Archives"


def test_add_download_rejects_duplicate_active_save_path(qtbot, panel, mock_server, download_dir):
    mock_server.add_file("dup.bin", os.urandom(2_000_000))
    mock_server.set_throttle("dup.bin", 600_000)  # first download must still be active/paused for the conflict check
    save_path = os.path.join(download_dir, "dup.bin")

    first, _ = panel.add_download(mock_server.url_for("dup.bin"), save_path, num_threads=1)
    pump(qtbot, lambda: first.status == Status.DOWNLOADING and first.downloaded_size > 0)
    first.pause()
    pump(qtbot, lambda: first.status == Status.PAUSED)

    second, second_widget = panel.add_download(mock_server.url_for("dup.bin"), save_path, num_threads=1)
    assert second is None
    assert second_widget is None
    assert panel.download_list.count() == 1  # nothing new was added


def test_add_download_dialog_rejects_non_url_text(qtbot):
    """Regression test: pasting a download link's visible label/title text
    (e.g. 'DOWNLOAD 1.7GB 8K MP4') instead of the actual URL should be
    caught in the dialog with an actionable message, not silently accepted
    only to fail later against the network."""
    dialog = AddDownloadDialog()
    qtbot.addWidget(dialog)
    dialog.url_input.setText("DOWNLOAD 1.7GB 8K MP4")
    dialog.path_input.setText("/tmp/whatever.mp4")

    assert "valid URL" in dialog.info_label.text()

    dialog._on_accept()
    assert dialog._error == "invalid_url"


def test_add_download_dialog_accepts_real_url(qtbot):
    dialog = AddDownloadDialog()
    qtbot.addWidget(dialog)
    dialog.url_input.setText("https://example.com/file.zip")
    dialog.path_input.setText("/tmp/file.zip")

    dialog._on_accept()
    assert dialog._error is None


def test_add_download_dialog_parses_speed_limit(qtbot):
    dialog = AddDownloadDialog()
    qtbot.addWidget(dialog)
    dialog.url_input.setText("https://example.com/file.zip")
    dialog.path_input.setText("/tmp/file.zip")
    dialog.speed_limit_input.setText("500 KB")
    data = dialog.get_data()
    assert data["speed_limit_bps"] == 500 * 1024


def test_settings_dialog_round_trips_values(qtbot):
    dialog = SettingsDialog(current_settings={"theme": "dark", "minimize_to_tray": False})
    qtbot.addWidget(dialog)
    assert dialog.theme_input.currentData() == "dark"
    assert dialog.minimize_to_tray_checkbox.isChecked() is False
    settings = dialog.get_settings()
    assert settings["theme"] == "dark"
    assert settings["minimize_to_tray"] is False


def test_retry_completed_download_reruns_via_queue(qtbot, panel, mock_server, download_dir):
    """Retrying a COMPLETED download must actually re-run it (re-fetch from the
    server) and keep active-download accounting correct. The old path no-op'd
    on COMPLETED -- retry() only handled ERROR/STOPPED -- so nothing happened."""
    import time
    from adp.core.models import Status
    mock_server.add_file("retry_me.bin", b"z" * 4000)
    save = os.path.join(download_dir, "retry_me.bin")
    manager, _ = panel.add_download(mock_server.url_for("retry_me.bin"), save)

    deadline = time.time() + 15
    while time.time() < deadline and manager.status != Status.COMPLETED:
        qtbot.wait(20)
    assert manager.status == Status.COMPLETED
    assert panel.active_downloads == 0
    requests_before = mock_server.request_count("retry_me.bin")
    assert requests_before > 0

    # Select it and retry -- must trigger a fresh fetch.
    panel.download_list.setCurrentRow(0)
    panel.retry_selected_download()

    deadline = time.time() + 15
    while time.time() < deadline and (
        manager.status != Status.COMPLETED
        or mock_server.request_count("retry_me.bin") <= requests_before
    ):
        qtbot.wait(20)
    assert manager.status == Status.COMPLETED
    # The re-download actually hit the server again -- proof it re-ran.
    assert mock_server.request_count("retry_me.bin") > requests_before
    # Accounting settled again -- never left over-counted.
    assert panel.active_downloads == 0


def test_retry_respects_concurrency_accounting(qtbot, panel, mock_server, download_dir):
    """After a retry cycle, active_downloads must return to 0 -- proving the
    retried download was counted and decremented through the normal path
    rather than started off-book."""
    import time
    from adp.core.models import Status
    mock_server.add_file("acct.bin", b"q" * 3000)
    save = os.path.join(download_dir, "acct.bin")
    manager, _ = panel.add_download(mock_server.url_for("acct.bin"), save)
    deadline = time.time() + 15
    while time.time() < deadline and manager.status != Status.COMPLETED:
        qtbot.wait(20)

    panel.download_list.setCurrentRow(0)
    panel.retry_selected_download()
    deadline = time.time() + 15
    while time.time() < deadline and manager.status != Status.COMPLETED:
        qtbot.wait(20)
    assert manager.status == Status.COMPLETED
    assert panel.active_downloads == 0


def test_duplicate_pending_save_path_rejected(qtbot, panel, mock_server, download_dir):
    """A PENDING download must reserve its destination: a second download to
    the same path is rejected even though the first hasn't started writing.
    Regression -- reservation used to only cover active/paused managers, so
    two pending jobs could both be accepted and collide on start."""
    mock_server.add_file("pend.bin", os.urandom(1000))
    save_path = os.path.join(download_dir, "pend.bin")
    first, _ = panel.add_download(
        mock_server.url_for("pend.bin"), save_path, num_threads=1, start_immediately=False)
    assert first is not None
    assert first.status == Status.PENDING

    second, second_widget = panel.add_download(
        mock_server.url_for("pend.bin"), save_path, num_threads=1, start_immediately=False)
    assert second is None and second_widget is None
    assert panel.download_list.count() == 1


def test_duplicate_scheduled_save_path_rejected(qtbot, panel, mock_server, download_dir):
    """A scheduled (QUEUED) download reserves its path too."""
    from datetime import datetime, timedelta
    mock_server.add_file("sched.bin", os.urandom(1000))
    save_path = os.path.join(download_dir, "sched.bin")
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    first, _ = panel.add_download(
        mock_server.url_for("sched.bin"), save_path, num_threads=1,
        start_immediately=False, scheduled_time=future)
    assert first is not None

    second, second_widget = panel.add_download(
        mock_server.url_for("sched.bin"), save_path, num_threads=1,
        start_immediately=False, scheduled_time=future)
    assert second is None and second_widget is None
    assert panel.download_list.count() == 1


def test_duplicate_queued_save_path_rejected(qtbot, panel, mock_server, download_dir):
    """With the concurrency limit reached, a second same-path job queued
    behind it is still rejected -- the path is reserved on acceptance."""
    mock_server.add_file("q.bin", os.urandom(1000))
    save_path = os.path.join(download_dir, "q.bin")
    # First job pending (not started); second to same path must be refused
    # regardless of whether the first has acquired a concurrency slot yet.
    first, _ = panel.add_download(
        mock_server.url_for("q.bin"), save_path, num_threads=1, start_immediately=False)
    assert first is not None
    second, second_widget = panel.add_download(
        mock_server.url_for("q.bin"), save_path, num_threads=1, start_immediately=False)
    assert second is None and second_widget is None
    assert panel.download_list.count() == 1


def _poll_until(qtbot, condition, timeout_s=20.0):
    """Direct event-pump poll -- more forgiving than qtbot.waitUntil for the
    multi-stage download/pause/resume flows here."""
    import time as _t
    from PyQt6.QtWidgets import QApplication
    end = _t.time() + timeout_s
    while _t.time() < end:
        QApplication.processEvents()
        if condition():
            return True
        _t.sleep(0.02)
    return False


def test_restored_paused_download_can_resume(qtbot, tmp_path, mock_server, download_dir, thread_pool):
    """The full GUI/session transition the reviewer flagged: download some
    bytes, pause, save session, destroy the panel, build a new one, verify the
    job restores as PAUSED, then resume and verify it COMPLETES with the right
    bytes. Regression: a restored-paused manager had total_size=0 and never
    ran start(), so resuming it spawned workers against a 0-byte file instead
    of fetching metadata and continuing."""
    content = os.urandom(2_000_000)
    mock_server.add_file("resume_me.bin", content)
    mock_server.set_throttle("resume_me.bin", 600_000)
    save_path = os.path.join(download_dir, "resume_me.bin")

    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel1)
    manager, _ = panel1.add_download(
        mock_server.url_for("resume_me.bin"), save_path, num_threads=2)
    assert _poll_until(qtbot, lambda: manager.status == Status.DOWNLOADING and manager.downloaded_size > 0)
    manager.pause()
    assert _poll_until(qtbot, lambda: manager.status == Status.PAUSED)
    panel1.save_downloads()

    # New panel from the same state dir -- simulates an app restart.
    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    restored_id = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    restored = panel2.downloads[restored_id]
    assert restored.status == Status.PAUSED
    assert restored.metadata_initialized is False  # never ran start() yet

    # Remove throttle so resume finishes quickly, then resume via the GUI path.
    mock_server.set_throttle("resume_me.bin", 0)
    panel2.download_list.setCurrentRow(0)
    panel2.resume_selected_download()

    assert _poll_until(qtbot, lambda: restored.status.is_terminal)
    assert restored.status == Status.COMPLETED
    with open(save_path, "rb") as f:
        assert f.read() == content


def test_restored_paused_download_respects_concurrency(qtbot, tmp_path, mock_server,
                                                       download_dir, thread_pool):
    """Resuming a restored-paused download must go through the concurrency
    accounting (it never held a slot), not bypass the limit."""
    mock_server.add_file("rc1.bin", os.urandom(1_500_000))
    mock_server.set_throttle("rc1.bin", 500_000)
    save_path = os.path.join(download_dir, "rc1.bin")

    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel1)
    m, _ = panel1.add_download(mock_server.url_for("rc1.bin"), save_path, num_threads=1)
    assert _poll_until(qtbot, lambda: m.status == Status.DOWNLOADING and m.downloaded_size > 0)
    m.pause()
    assert _poll_until(qtbot, lambda: m.status == Status.PAUSED)
    panel1.save_downloads()

    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    rid = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    restored = panel2.downloads[rid]
    assert panel2.active_downloads == 0

    mock_server.set_throttle("rc1.bin", 0)
    panel2.download_list.setCurrentRow(0)
    panel2.resume_selected_download()
    assert _poll_until(qtbot, lambda: restored.status.is_terminal)
    assert restored.status == Status.COMPLETED
    # Accounting settled back to 0 -- it was counted and decremented, not run off-book.
    assert panel2.active_downloads == 0


def test_completed_session_restores_completed_without_network(qtbot, tmp_path, thread_pool):
    """A COMPLETED download must restore as COMPLETED and NOT re-download.
    Regression: load_downloads resurrected every non-paused record as PENDING,
    so completed downloads re-ran on restart. Uses no mock server -- if the
    restored job tried to download, it would fail/hang against a dead URL."""
    save_path = os.path.join(str(tmp_path), "done.bin")
    with open(save_path, "wb") as f:
        f.write(b"x" * 1000)

    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel1)
    m, _ = panel1.add_download("http://192.0.2.1/done.bin", save_path,
                               num_threads=1, start_immediately=False)
    # Force it terminal as if it had completed.
    m.total_size = 1000
    m.downloaded_size = 1000
    m.set_status(Status.COMPLETED)
    panel1.save_downloads()

    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    rid = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    restored = panel2.downloads[rid]
    # Give any errant queue processing a chance to (wrongly) start it.
    _poll_until(qtbot, lambda: False, timeout_s=1.0)
    assert restored.status == Status.COMPLETED
    assert restored not in panel2.download_queue
    assert panel2.active_downloads == 0


def test_stopped_session_stays_stopped(qtbot, tmp_path, thread_pool):
    """STOPPED means stopped, across restart -- must not restart."""
    save_path = os.path.join(str(tmp_path), "stopped.bin")
    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel1)
    m, _ = panel1.add_download("http://192.0.2.1/stopped.bin", save_path,
                               num_threads=1, start_immediately=False)
    m.set_status(Status.STOPPED)
    panel1.save_downloads()

    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    rid = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    restored = panel2.downloads[rid]
    _poll_until(qtbot, lambda: False, timeout_s=1.0)
    assert restored.status == Status.STOPPED
    assert restored not in panel2.download_queue
    assert panel2.active_downloads == 0


def test_error_session_stays_error(qtbot, tmp_path, thread_pool):
    """ERROR stays ERROR across restart until the user explicitly retries."""
    save_path = os.path.join(str(tmp_path), "err.bin")
    panel1 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel1)
    m, _ = panel1.add_download("http://192.0.2.1/err.bin", save_path,
                               num_threads=1, start_immediately=False)
    m.set_status(Status.ERROR)
    panel1.save_downloads()

    panel2 = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel2)
    rid = panel2.download_list.item(0).data(Qt.ItemDataRole.UserRole)
    restored = panel2.downloads[rid]
    _poll_until(qtbot, lambda: False, timeout_s=1.0)
    assert restored.status == Status.ERROR
    assert restored not in panel2.download_queue
    assert panel2.active_downloads == 0


def test_retry_respects_path_reservation(qtbot, tmp_path, mock_server, download_dir, thread_pool):
    """RC blocker: a terminal download releases its path reservation, so
    another download can claim it. Retrying the terminal one must then be
    REFUSED rather than re-creating a two-managers-one-file collision."""
    mock_server.add_file("shared.bin", b"z" * 1000)
    save_path = os.path.join(download_dir, "shared.bin")

    panel = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel)
    # A: added then forced to a terminal (ERROR) state -> releases reservation.
    a, _ = panel.add_download(mock_server.url_for("shared.bin"), save_path,
                              num_threads=1, start_immediately=False)
    a.set_status(Status.ERROR)
    # B: now claims the same destination (allowed, since A is terminal).
    b, b_widget = panel.add_download(mock_server.url_for("shared.bin"), save_path,
                                     num_threads=1, start_immediately=False)
    assert b is not None  # B accepted because A had released the path

    # Retrying A must be refused -- B now owns the destination.
    accepted = panel.retry_download(a.download_id)
    assert accepted is False
    assert a.status == Status.ERROR  # unchanged; not resurrected


def test_retry_allowed_when_no_path_conflict(qtbot, tmp_path, mock_server, download_dir, thread_pool):
    """The conflict check must not over-block: retrying a terminal download
    with no competing job for its path succeeds."""
    mock_server.add_file("solo.bin", b"q" * 1000)
    save_path = os.path.join(download_dir, "solo.bin")
    panel = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel)
    a, _ = panel.add_download(mock_server.url_for("solo.bin"), save_path,
                              num_threads=1, start_immediately=False)
    a.set_status(Status.ERROR)
    accepted = panel.retry_download(a.download_id)
    assert accepted is True


def test_removing_restored_paused_does_not_free_another_slot(qtbot, tmp_path, mock_server,
                                                             download_dir, thread_pool):
    """Slot-ownership invariant: a restored-paused download holds no slot, so
    removing it must NOT release someone else's. Regression: active_downloads
    was a bare counter, and remove() decremented it unconditionally -- so
    removing a slot-less paused job wrongly freed the running job's slot and
    let the queue over-start."""
    mock_server.add_file("slotA.bin", os.urandom(2_000_000))
    mock_server.set_throttle("slotA.bin", 500_000)
    panel = DownloadPanel(state_dir=str(tmp_path), thread_pool=thread_pool)
    qtbot.addWidget(panel)
    panel.concurrency_spinbox.setValue(1)  # exactly one slot

    # A: running, owns the only slot.
    a, _ = panel.add_download(mock_server.url_for("slotA.bin"),
                              os.path.join(download_dir, "slotA.bin"), num_threads=1)
    assert _poll_until(qtbot, lambda: a.status == Status.DOWNLOADING and a.downloaded_size > 0)
    assert panel.active_downloads == 1

    # B: a restored-paused-style job that never acquired a slot.
    b, _ = panel.add_download(mock_server.url_for("slotA.bin"),
                              os.path.join(download_dir, "slotB.bin"),
                              num_threads=1, start_immediately=False)
    if b in panel.download_queue:
        panel.download_queue.remove(b)
    b.set_status(Status.PAUSED)
    assert panel.active_downloads == 1  # B holds no slot

    # Remove B -- A's slot must be untouched.
    panel.download_list.setCurrentRow(panel.download_list.count() - 1)
    # Ensure we've selected B's row.
    for row in range(panel.download_list.count()):
        if panel.download_list.item(row).data(Qt.ItemDataRole.UserRole) == b.download_id:
            panel.download_list.setCurrentRow(row)
            break
    panel.remove_selected_download()
    assert panel.active_downloads == 1  # A still holds its slot, invariant intact
    assert a.download_id in panel._slot_holders
