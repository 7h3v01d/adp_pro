import logging
import warnings

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QGroupBox, QFrame,
)
from PyQt6.QtCore import QTimer, Qt

from adp.core.models import Status
from adp.core.stats_aggregator import StatsAggregator
from adp.core.stats_store import StatsStore
from adp.utils.format import format_size, format_speed
from adp.gui.speed_graph_widget import SpeedGraphWidget

logger = logging.getLogger(__name__)

TICK_MS = 1000
SAVE_EVERY_N_TICKS = 10  # persist lifetime stats roughly every 10 seconds, not every tick


def _stat_label() -> QLabel:
    label = QLabel("--")
    font = label.font()
    font.setPointSize(font.pointSize() + 3)
    font.setBold(True)
    label.setFont(font)
    return label


class StatsPanel(QWidget):
    """Dashboard tab: a rolling speed graph, session/lifetime totals, and
    (when torrent support is available) swarm health. Pulls its numbers by
    polling the download and torrent panels on a timer rather than wiring
    into every individual manager's signals -- simpler, and naturally
    resilient to managers being added/removed while the app runs."""

    def __init__(self, parent=None, download_panel=None, torrent_panel=None, state_dir=None):
        super().__init__(parent)
        self.download_panel = download_panel
        self.torrent_panel = torrent_panel

        self.stats_store = StatsStore(state_dir or ".")
        self.aggregator = StatsAggregator(self.stats_store)
        self._tick_count = 0

        layout = QVBoxLayout(self)

        self.graph = SpeedGraphWidget()
        layout.addWidget(self.graph)

        totals_row = QHBoxLayout()
        totals_row.addWidget(self._build_session_box())
        totals_row.addWidget(self._build_lifetime_box())
        layout.addLayout(totals_row)

        if self.torrent_panel is not None:
            layout.addWidget(self._build_swarm_box())
        else:
            notice = QLabel(
                "Torrent support isn't available in this install, so swarm health isn't shown here."
            )
            notice.setWordWrap(True)
            layout.addWidget(notice)

        layout.addStretch()

        if self.download_panel is not None:
            self.download_panel.download_completed.connect(self._on_download_completed)
        if self.torrent_panel is not None:
            self.torrent_panel.torrent_completed.connect(self._on_torrent_completed)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- layout builders -----------------------------------------------------
    def _build_session_box(self) -> QGroupBox:
        box = QGroupBox("This Session")
        grid = QGridLayout(box)
        self.session_downloaded_label = _stat_label()
        self.session_uploaded_label = _stat_label()
        self.session_completed_downloads_label = _stat_label()
        self.session_completed_torrents_label = _stat_label()
        grid.addWidget(QLabel("Downloaded:"), 0, 0)
        grid.addWidget(self.session_downloaded_label, 0, 1)
        grid.addWidget(QLabel("Uploaded:"), 1, 0)
        grid.addWidget(self.session_uploaded_label, 1, 1)
        grid.addWidget(QLabel("Downloads completed:"), 2, 0)
        grid.addWidget(self.session_completed_downloads_label, 2, 1)
        grid.addWidget(QLabel("Torrents completed:"), 3, 0)
        grid.addWidget(self.session_completed_torrents_label, 3, 1)
        return box

    def _build_lifetime_box(self) -> QGroupBox:
        box = QGroupBox("Lifetime")
        grid = QGridLayout(box)
        self.lifetime_downloaded_label = _stat_label()
        self.lifetime_uploaded_label = _stat_label()
        self.lifetime_completed_downloads_label = _stat_label()
        self.lifetime_completed_torrents_label = _stat_label()
        grid.addWidget(QLabel("Downloaded:"), 0, 0)
        grid.addWidget(self.lifetime_downloaded_label, 0, 1)
        grid.addWidget(QLabel("Uploaded:"), 1, 0)
        grid.addWidget(self.lifetime_uploaded_label, 1, 1)
        grid.addWidget(QLabel("Downloads completed:"), 2, 0)
        grid.addWidget(self.lifetime_completed_downloads_label, 2, 1)
        grid.addWidget(QLabel("Torrents completed:"), 3, 0)
        grid.addWidget(self.lifetime_completed_torrents_label, 3, 1)
        return box

    def _build_swarm_box(self) -> QGroupBox:
        box = QGroupBox("Swarm Health")
        grid = QGridLayout(box)
        self.active_torrents_label = _stat_label()
        self.total_peers_label = _stat_label()
        self.total_seeds_label = _stat_label()
        self.dht_nodes_label = _stat_label()
        grid.addWidget(QLabel("Active torrents:"), 0, 0)
        grid.addWidget(self.active_torrents_label, 0, 1)
        grid.addWidget(QLabel("Connected peers:"), 0, 2)
        grid.addWidget(self.total_peers_label, 0, 3)
        grid.addWidget(QLabel("Connected seeds:"), 1, 0)
        grid.addWidget(self.total_seeds_label, 1, 1)
        grid.addWidget(QLabel("DHT nodes:"), 1, 2)
        grid.addWidget(self.dht_nodes_label, 1, 3)
        return box

    # -- polling ----------------------------------------------------------
    def _tick(self):
        # Known limitation: because progress is sampled once per tick (1Hz)
        # rather than driven by push signals, a transfer that starts and
        # finishes within a single tick window has its bytes counted only
        # from whatever partial progress happened to exist at the moment we
        # first observed it (see the baseline-on-first-observation comment
        # in StatsAggregator) -- so very small/fast downloads can be slightly
        # undercounted in the session/lifetime totals. This trades a small
        # amount of accuracy for a much simpler design (no need to hook into
        # every manager's individual signals, or distinguish a brand new
        # transfer from one restored from a previous session's history).
        current_down = 0.0
        current_up = 0.0

        if self.download_panel is not None:
            for manager in self.download_panel.downloads.values():
                self.aggregator.record_download_progress(manager.download_id, manager.downloaded_size)
                if manager.status == Status.DOWNLOADING:
                    current_down += manager.current_speed

        if self.torrent_panel is not None:
            total_peers = 0
            total_seeds = 0
            active_torrents = 0
            for torrent_id, handle in list(self.torrent_panel.engine.handles.items()):
                if not handle.is_valid():
                    continue
                status = handle.status()
                self.aggregator.record_torrent_progress(torrent_id, status.all_time_download, status.all_time_upload)
                if not status.paused:
                    current_down += status.download_rate
                    current_up += status.upload_rate
                    active_torrents += 1
                total_peers += status.num_peers
                total_seeds += status.num_seeds

            self.active_torrents_label.setText(str(active_torrents))
            self.total_peers_label.setText(str(total_peers))
            self.total_seeds_label.setText(str(total_seeds))
            self.dht_nodes_label.setText(str(self._dht_node_count()))

        self.graph.add_sample(current_down, current_up)

        self.session_downloaded_label.setText(format_size(self.aggregator.session_downloaded_bytes))
        self.session_uploaded_label.setText(format_size(self.aggregator.session_uploaded_bytes))
        self.session_completed_downloads_label.setText(str(self.aggregator.session_completed_downloads))
        self.session_completed_torrents_label.setText(str(self.aggregator.session_completed_torrents))

        self.lifetime_downloaded_label.setText(format_size(self.aggregator.lifetime["lifetime_downloaded_bytes"]))
        self.lifetime_uploaded_label.setText(format_size(self.aggregator.lifetime["lifetime_uploaded_bytes"]))
        self.lifetime_completed_downloads_label.setText(str(self.aggregator.lifetime["lifetime_completed_downloads"]))
        self.lifetime_completed_torrents_label.setText(str(self.aggregator.lifetime["lifetime_completed_torrents"]))

        self._tick_count += 1
        if self._tick_count % SAVE_EVERY_N_TICKS == 0:
            self.aggregator.save()

    def _dht_node_count(self) -> int:
        try:
            with warnings.catch_warnings():
                # session.status() is deprecated in favor of post_session_stats()
                # + parsing session_stats_alert's flat counters array. That's a
                # meaningfully more complex API for one number; status() still
                # works correctly in current libtorrent, so we use it and
                # silence the warning rather than add that complexity now.
                warnings.simplefilter("ignore", DeprecationWarning)
                return self.torrent_panel.engine.session.status().dht_nodes
        except Exception:
            return 0

    # -- completion counters -------------------------------------------------
    def _on_download_completed(self, download_id, filename):
        self.aggregator.record_download_completed()

    def _on_torrent_completed(self, torrent_id, name):
        self.aggregator.record_torrent_completed()

    def closeEvent(self, event):
        self._timer.stop()
        self.aggregator.save()
        super().closeEvent(event)
