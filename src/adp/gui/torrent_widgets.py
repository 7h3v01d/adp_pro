# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import Qt

from adp.torrent.models import TorrentState
from adp.utils.format import format_size, format_speed, format_eta
from adp.gui.theme import STATUS

STATE_LABELS = {
    TorrentState.QUEUED: "Queued",
    TorrentState.CHECKING: "Checking",
    TorrentState.DOWNLOADING_METADATA: "Fetching metadata",
    TorrentState.DOWNLOADING: "Downloading",
    TorrentState.FINISHED: "Finished",
    TorrentState.SEEDING: "Seeding",
    TorrentState.PAUSED: "Paused",
    TorrentState.STOPPED: "Stopped",
    TorrentState.ERROR: "Error",
}

STATE_COLORS = {
    TorrentState.SEEDING: STATUS["success"],
    TorrentState.FINISHED: STATUS["success"],
    TorrentState.ERROR: STATUS["error"],
    TorrentState.STOPPED: STATUS["warning"],
    TorrentState.PAUSED: STATUS["idle"],
}


class TorrentItemWidget(QWidget):
    """Displays one torrent's name, category, progress, and swarm stats."""

    def __init__(self, torrent_id, name, category="Torrents"):
        super().__init__()
        self.torrent_id = torrent_id
        self.category = category
        # Exponential moving average of the download rate, so the ETA is
        # computed from a smoothed rate rather than the instantaneous one.
        # A raw remaining/rate ETA swings wildly and *climbs* as the rate
        # decays (e.g. a post-recheck torrent whose peer connection is
        # dying), which reads to the user as "ETA going up, never finishing".
        self._smoothed_rate = 0.0
        # Count consecutive polls with (near-)zero raw rate. A stall is best
        # detected by "no bytes moving for a while", independent of file size
        # -- an absolute rate floor is wrong for huge torrents, where the EMA
        # can sit above the floor for many polls while the ETA still climbs.
        self._zero_rate_polls = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        self.name_label = QLabel(f"<b>{name}</b>")
        self.category_badge = QLabel(category)
        self.category_badge.setObjectName("categoryBadge")
        self.category_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.name_label)
        header.addStretch()
        header.addWidget(self.category_badge)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.info_label = QLabel("Queued")
        self.swarm_label = QLabel("")

        layout.addLayout(header)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.info_label)
        layout.addWidget(self.swarm_label)

    def set_name(self, name: str):
        self.name_label.setText(f"<b>{name}</b>")

    def update_status(self, status: dict):
        state = status["state"]
        progress = status.get("progress", 0.0)
        self.progress_bar.setValue(int(progress * 100))

        downloaded = status.get("total_wanted_done", 0)
        total = status.get("total_wanted", 0)
        down_rate = status.get("download_rate", 0)
        up_rate = status.get("upload_rate", 0)

        state_text = STATE_LABELS.get(state, state.name.title())
        if state == TorrentState.DOWNLOADING:
            # Smooth the rate (EMA) so a jittery rate doesn't make the ETA
            # jump around. Track consecutive near-zero polls separately for
            # stall detection (size-independent).
            alpha = 0.3
            self._smoothed_rate = alpha * down_rate + (1 - alpha) * self._smoothed_rate
            if down_rate < 1024:  # < 1 KiB/s this poll
                self._zero_rate_polls += 1
            else:
                self._zero_rate_polls = 0
            remaining = total - downloaded
            # Stalled: several consecutive polls with no meaningful transfer.
            # Show that plainly instead of an ETA that climbs without bound
            # (which is size-independent, unlike an absolute-rate floor that
            # a huge torrent's EMA can hover above while the ETA still grows).
            if self._zero_rate_polls >= 3 and remaining > 0:
                eta_text = "stalled"
            elif self._smoothed_rate < 1:
                eta_text = "--"
            else:
                eta_text = format_eta(self._smoothed_rate, remaining)
            self.info_label.setText(
                f"{state_text} | {format_size(downloaded)} / {format_size(total)} | "
                f"\u2193 {format_speed(down_rate)} \u2191 {format_speed(up_rate)} | ETA: {eta_text}"
            )
        elif state == TorrentState.SEEDING:
            self._smoothed_rate = 0.0
            self._zero_rate_polls = 0
            self.info_label.setText(
                f"{state_text} | {format_size(total)} | \u2191 {format_speed(up_rate)} | "
                f"Ratio: {status.get('ratio', 0):.2f}"
            )
        else:
            # Checking / paused / queued: no meaningful ETA. Reset the smoother
            # and stall counter so a later download starts fresh rather than
            # inheriting stale state from before a recheck.
            self._smoothed_rate = 0.0
            self._zero_rate_polls = 0
            if state == TorrentState.CHECKING:
                # During a recheck libtorrent reports progress as 0 until the
                # check completes; say "Checking..." rather than showing a
                # 0-byte figure that looks like data was lost.
                self.info_label.setText(f"{state_text}\u2026 verifying data on disk")
            else:
                self.info_label.setText(
                    f"{state_text} | {format_size(downloaded)} / {format_size(total)}")

        self.swarm_label.setText(f"Peers: {status.get('num_peers', 0)} | Seeds: {status.get('num_seeds', 0)}")

        color = STATE_COLORS.get(state)
        style = f"QProgressBar::chunk {{ background-color: {color}; }}" if color else ""
        self.progress_bar.setStyleSheet(style)

    def set_category(self, category: str):
        self.category = category
        self.category_badge.setText(category)
