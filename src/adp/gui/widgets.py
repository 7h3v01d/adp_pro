# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import Qt

from adp.utils.format import format_size, format_speed, format_eta
from adp.gui.theme import STATUS

STATUS_COLORS = {
    "Completed": STATUS["success"],
    "Error": STATUS["error"],
    "Stopped": STATUS["warning"],
    "Paused": STATUS["idle"],
    "Queued": STATUS["idle"],
}


class DownloadItemWidget(QWidget):
    """Displays one download's filename, category, progress bar, and stats."""

    def __init__(self, download_id, filename, category="General"):
        super().__init__()
        self.download_id = download_id
        self.category = category

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        header = QHBoxLayout()
        self.filename_label = QLabel(f"<b>{os.path.basename(filename)}</b>")
        self.category_badge = QLabel(category)
        self.category_badge.setObjectName("categoryBadge")
        self.category_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.filename_label)
        header.addStretch()
        header.addWidget(self.category_badge)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.info_label = QLabel("Status: Pending | 0 B / 0 B | 0 B/s | ETA: --")

        layout.addLayout(header)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.info_label)

    def set_category(self, category: str):
        self.category = category
        self.category_badge.setText(category)

    def update_progress(self, downloaded, total, speed, status):
        eta_text = format_eta(speed, total - downloaded) if status == "Downloading" else "--"
        self.info_label.setText(
            f"Status: {status} | {format_size(downloaded)} / {format_size(total)} | "
            f"{format_speed(speed)} | ETA: {eta_text}"
        )
        if total > 0:
            progress_percent = int((downloaded / total) * 100)
            self.progress_bar.setValue(progress_percent)
        else:
            self.progress_bar.setValue(0)

    def set_final_status(self, status, message=""):
        self.info_label.setText(f"Status: {status}{f' - {message}' if message else ''}")
        color = STATUS_COLORS.get(status)
        if status == "Completed":
            self.progress_bar.setValue(100)
        style = f"QProgressBar::chunk {{ background-color: {color}; }}" if color else ""
        self.progress_bar.setStyleSheet(style)

    def set_scheduled(self, when_text: str):
        self.info_label.setText(f"Status: Scheduled | Starts at {when_text}")
