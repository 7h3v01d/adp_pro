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
