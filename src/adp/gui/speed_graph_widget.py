# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""A lightweight, dependency-free rolling line graph for download/upload
speed, drawn with QPainter rather than pulling in a charting library --
consistent with the rest of this app's philosophy of avoiding extra native
dependencies where a straightforward custom widget will do.
"""

from __future__ import annotations

from collections import deque

from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QPen, QColor, QFont
from PyQt6.QtCore import Qt, QPointF

from adp.utils.format import format_speed

from adp.gui.theme import STATUS

DOWNLOAD_COLOR = QColor(STATUS["download"])   # teal -- house primary accent
UPLOAD_COLOR = QColor(STATUS["upload"])       # amber -- house secondary accent
GRID_COLOR = QColor(128, 128, 128, 60)
TEXT_COLOR = QColor(128, 128, 128, 200)

MIN_SCALE_BPS = 50 * 1024  # floor the y-axis at 50 KB/s so a near-idle graph isn't all noise


class SpeedGraphWidget(QWidget):
    """Rolling line graph of download/upload throughput over the last
    `window_seconds` (default 5 minutes at 1 sample/sec)."""

    def __init__(self, parent=None, window_seconds: int = 300):
        super().__init__(parent)
        self.window_seconds = window_seconds
        self.samples = deque(maxlen=window_seconds)  # each: (download_bps, upload_bps)
        self.setMinimumHeight(160)

    def add_sample(self, download_bps: float, upload_bps: float):
        self.samples.append((download_bps, upload_bps))
        self.update()

    def clear_samples(self):
        self.samples.clear()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        margin_left, margin_right, margin_top, margin_bottom = 60, 12, 12, 24
        plot_rect = rect.adjusted(margin_left, margin_top, -margin_right, -margin_bottom)

        if plot_rect.width() <= 0 or plot_rect.height() <= 0:
            return

        max_value = MIN_SCALE_BPS
        for down, up in self.samples:
            max_value = max(max_value, down, up)
        # Round the scale up to a "nice" number so gridline labels aren't awkward.
        max_value = self._nice_ceiling(max_value)

        self._draw_grid(painter, plot_rect, max_value)

        if len(self.samples) >= 2:
            self._draw_series(painter, plot_rect, max_value, series_index=0, color=DOWNLOAD_COLOR)
            self._draw_series(painter, plot_rect, max_value, series_index=1, color=UPLOAD_COLOR)

        self._draw_legend(painter, plot_rect)

    def _draw_grid(self, painter, plot_rect, max_value):
        painter.setPen(QPen(GRID_COLOR, 1))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        num_lines = 4
        for i in range(num_lines + 1):
            y = plot_rect.bottom() - (plot_rect.height() * i / num_lines)
            painter.drawLine(QPointF(plot_rect.left(), y), QPointF(plot_rect.right(), y))
            value = max_value * i / num_lines
            painter.setPen(QPen(TEXT_COLOR, 1))
            label = (format_speed(value) if value else "0 B/s").replace(".00 ", " ").replace(".50 ", ".5 ")
            painter.drawText(
                0, int(y) - 8, plot_rect.left() - 6, 16,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            painter.setPen(QPen(GRID_COLOR, 1))

    def _draw_series(self, painter, plot_rect, max_value, series_index, color):
        painter.setPen(QPen(color, 2))
        n = len(self.samples)
        points = []
        for i, sample in enumerate(self.samples):
            value = sample[series_index]
            x = plot_rect.left() + (plot_rect.width() * i / max(1, self.window_seconds - 1))
            y = plot_rect.bottom() - (value / max_value) * plot_rect.height()
            points.append(QPointF(x, y))
        for p1, p2 in zip(points, points[1:]):
            painter.drawLine(p1, p2)

    def _draw_legend(self, painter, plot_rect):
        """Current speeds, anchored to the top-right corner of the plot area
        (measured with real font metrics) so they can never collide with the
        y-axis labels on the left."""
        current_down = self.samples[-1][0] if self.samples else 0
        current_up = self.samples[-1][1] if self.samples else 0

        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()

        down_text = f"\u2193 {format_speed(current_down)}"
        up_text = f"\u2191 {format_speed(current_up)}"
        gap = 14
        baseline_y = plot_rect.top() + fm.ascent() + 2

        x_up = plot_rect.right() - fm.horizontalAdvance(up_text)
        x_down = x_up - gap - fm.horizontalAdvance(down_text)

        painter.setPen(QPen(DOWNLOAD_COLOR, 2))
        painter.drawText(QPointF(x_down, baseline_y), down_text)
        painter.setPen(QPen(UPLOAD_COLOR, 2))
        painter.drawText(QPointF(x_up, baseline_y), up_text)

    @staticmethod
    def _nice_ceiling(value: float) -> float:
        """Rounds up to a scale that is a round number in the BINARY units
        format_speed displays (1 KB = 1024 B). Rounding in decimal (the old
        behaviour) produced a 100,000 B/s idle scale whose gridlines
        rendered as 97.66/73.24/48.83/24.41 KB/s; this yields 50/37.5/25/
        12.5 KB/s instead, and e.g. 1.2 MB/s of traffic scales to 2 MB/s
        with 512 KB/1 MB/1.5 MB gridlines."""
        if value <= 0:
            return MIN_SCALE_BPS
        import math
        unit = 1024.0
        while value / unit >= 1000:  # keep the displayed number small (KB -> MB -> GB)
            unit *= 1024
        scaled = value / unit
        magnitude = 10 ** math.floor(math.log10(scaled)) if scaled >= 1 else 1
        for step in (1, 2, 4, 5, 10):
            candidate = step * magnitude
            if candidate >= scaled:
                return candidate * unit
        return 10 * magnitude * unit
