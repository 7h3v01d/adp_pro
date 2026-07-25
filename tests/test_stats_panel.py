# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os

import pytest

from adp.gui.stats_panel import StatsPanel
from adp.gui.main_window import DownloadPanel
from adp.core.models import Status


@pytest.fixture
def download_panel(qtbot, tmp_path, thread_pool):
    state_dir = tmp_path / "downloads_state"
    state_dir.mkdir()
    p = DownloadPanel(state_dir=str(state_dir), thread_pool=thread_pool)
    qtbot.addWidget(p)
    yield p
    for manager in list(p.downloads.values()):
        if manager.status.is_active or manager.status == Status.PAUSED:
            manager.stop()


def test_stats_panel_without_torrent_support_shows_notice(qtbot, tmp_path, download_panel):
    panel = StatsPanel(download_panel=download_panel, torrent_panel=None, state_dir=str(tmp_path))
    qtbot.addWidget(panel)
    assert not hasattr(panel, "active_torrents_label")
    panel._timer.stop()


def test_stats_panel_tracks_download_bytes_over_ticks(qtbot, tmp_path, mock_server, download_panel, download_dir):
    panel = StatsPanel(download_panel=download_panel, torrent_panel=None, state_dir=str(tmp_path))
    qtbot.addWidget(panel)
    panel._timer.stop()  # drive ticks manually for a deterministic test

    mock_server.add_file("stats_test.bin", os.urandom(200_000))
    manager, widget = download_panel.add_download(
        mock_server.url_for("stats_test.bin"), os.path.join(download_dir, "stats_test.bin")
    )
    panel._tick()  # establish a baseline while the download is still in flight
    qtbot.waitUntil(lambda: manager.status == Status.COMPLETED, timeout=15000)

    panel._tick()

    assert panel.aggregator.session_downloaded_bytes > 0
    assert "B" in panel.session_downloaded_label.text()


def test_stats_panel_records_completion_counts(qtbot, tmp_path, mock_server, download_panel, download_dir):
    panel = StatsPanel(download_panel=download_panel, torrent_panel=None, state_dir=str(tmp_path))
    qtbot.addWidget(panel)
    panel._timer.stop()

    mock_server.add_file("completed.bin", os.urandom(1000))
    manager, widget = download_panel.add_download(
        mock_server.url_for("completed.bin"), os.path.join(download_dir, "completed.bin")
    )
    qtbot.waitUntil(lambda: manager.status == Status.COMPLETED, timeout=15000)

    assert panel.aggregator.session_completed_downloads == 1


def test_speed_graph_accepts_samples_without_crashing(qtbot):
    from adp.gui.speed_graph_widget import SpeedGraphWidget
    graph = SpeedGraphWidget()
    qtbot.addWidget(graph)
    graph.add_sample(1024 * 500, 1024 * 100)
    graph.add_sample(1024 * 600, 1024 * 50)
    graph.resize(400, 200)
    graph.repaint()
    assert len(graph.samples) == 2


def test_stats_persist_across_panel_instances(qtbot, tmp_path, mock_server, download_panel, download_dir):
    panel1 = StatsPanel(download_panel=download_panel, torrent_panel=None, state_dir=str(tmp_path))
    qtbot.addWidget(panel1)
    panel1._timer.stop()

    mock_server.add_file("persist_stats.bin", os.urandom(5000))
    manager, widget = download_panel.add_download(
        mock_server.url_for("persist_stats.bin"), os.path.join(download_dir, "persist_stats.bin")
    )
    panel1._tick()  # establish a baseline while the download is still in flight
    qtbot.waitUntil(lambda: manager.status == Status.COMPLETED, timeout=15000)
    panel1._tick()
    panel1.aggregator.save()

    panel2 = StatsPanel(download_panel=None, torrent_panel=None, state_dir=str(tmp_path))
    qtbot.addWidget(panel2)
    panel2._timer.stop()
    assert panel2.aggregator.lifetime["lifetime_downloaded_bytes"] > 0


# -- speed graph widget ------------------------------------------------------

from adp.gui.speed_graph_widget import SpeedGraphWidget, MIN_SCALE_BPS
from adp.utils.format import format_speed


@pytest.mark.parametrize("value,expected_scale", [
    (MIN_SCALE_BPS, 50 * 1024),      # idle floor stays exactly 50 KB/s
    (30_000, 40 * 1024),             # ~29.3 KB/s -> 40 KB/s
    (100_000, 100 * 1024),           # ~97.7 KB/s -> 100 KB/s
    (1_258_291, 2 * 1024 * 1024),    # ~1.2 MB/s -> 2 MB/s
    (0, MIN_SCALE_BPS),
    (-5, MIN_SCALE_BPS),
])
def test_speed_graph_scale_is_round_in_binary_units(value, expected_scale):
    """Regression test: the y-axis ceiling used to be rounded in decimal,
    which rendered as 97.66/73.24/48.83/24.41 KB/s on an idle graph. The
    scale must be a round number in the binary units format_speed shows."""
    assert SpeedGraphWidget._nice_ceiling(value) == expected_scale


def test_speed_graph_gridline_labels_have_no_awkward_fractions():
    """Every quarter-gridline label at the idle scale must format cleanly."""
    top = SpeedGraphWidget._nice_ceiling(MIN_SCALE_BPS)
    for i in range(1, 5):
        label = format_speed(top * i / 4)
        # 12.50 and 37.50 render (and get trimmed in the widget); what must
        # never appear is a repeating-fraction artifact like 97.66 or 48.83.
        assert not any(frag in label for frag in (".66", ".24", ".83", ".41")), label


@pytest.mark.gui
def test_speed_graph_paints_without_error_at_launch_and_under_load(qtbot):
    """Paint smoke test covering the launch state (few zero samples, the
    screenshot scenario) and a full 5-minute window of live traffic."""
    from PyQt6.QtGui import QImage

    widget = SpeedGraphWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 180)

    for _ in range(10):
        widget.add_sample(0, 0)
    img = QImage(widget.size(), QImage.Format.Format_ARGB32)
    widget.render(img)

    for i in range(300):
        widget.add_sample(800_000 + i * 1000, 120_000)
    img2 = QImage(widget.size(), QImage.Format.Format_ARGB32)
    widget.render(img2)
    assert not img2.isNull()
