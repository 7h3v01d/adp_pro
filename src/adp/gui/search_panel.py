# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The Search tab: query the enabled torrent indexers, browse ranked
results, and add a result to the Torrents tab in one click.

Threading: SearchService.search() blocks on network I/O, so it runs on a
QThread subclass; results come back to the panel via signals. A reference
to each in-flight worker is kept in self._worker_refs until its finished()
fires -- without that, Python may garbage-collect the QThread wrapper while
the OS thread is still running ("QThread: Destroyed while thread is still
running" crashes).

Adding to Torrents deliberately does NOT go through AppController: the
controller marshals every call through GuiBridge onto the Qt main thread
and blocks the caller until it runs -- called *from* the main thread that
would deadlock (the drain timer can never fire while its own thread is
blocked waiting on it). This panel already lives on the main thread, so it
calls TorrentPanel.add_torrent() directly, the same path the Torrents tab's
own Add dialog uses.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from adp.search.models import SearchQuery, SearchResult
from adp.search.service import SearchOutcome, SearchService
from adp.utils.format import format_size

logger = logging.getLogger(__name__)

_CATEGORY_CHOICES = ["Any", "software", "video", "audio", "books", "games", "other"]

# Stored on the title item so actions (Add / double-click / context menu)
# resolve the right SearchResult even after the user re-sorts the table --
# indexing by visual row would resolve the wrong result once sorted.
_RESULT_INDEX_ROLE = Qt.ItemDataRole.UserRole


class _SortKeyItem(QTableWidgetItem):
    """A table cell whose *display* is human-readable text ("4.56 GB", "1240")
    but whose *sort* uses a stored numeric key. Without this, Qt sorts these
    columns as strings -- "667 MB" would sort above "4.56 GB", and "1240"
    below "305" -- which is wrong for sizes and counts."""

    def __init__(self, text: str, sort_key: float):
        super().__init__(text)
        self._sort_key = sort_key

    def __lt__(self, other):
        if isinstance(other, _SortKeyItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)


class SearchWorker(QThread):
    """Runs one search off the GUI thread."""
    search_finished = pyqtSignal(object)   # SearchOutcome
    search_failed = pyqtSignal(str)

    def __init__(self, service: SearchService, query: SearchQuery, parent=None):
        super().__init__(parent)
        self._service = service
        self._query = query

    def run(self):
        try:
            self.search_finished.emit(self._service.search(self._query))
        except Exception as e:  # noqa: BLE001 -- surfaced to the status bar, never a crash
            logger.exception("Search worker failed")
            self.search_failed.emit(str(e))


class SearchPanel(QWidget):
    status_update_requested = pyqtSignal(str, int)

    COL_TITLE, COL_SIZE, COL_SEEDERS, COL_LEECHERS, COL_SOURCES, COL_ADD = range(6)

    def __init__(self, search_service: SearchService, torrent_panel=None, parent=None):
        super().__init__(parent)
        self.search_service = search_service
        self.torrent_panel = torrent_panel   # None when libtorrent is unavailable
        self._worker_refs: List[SearchWorker] = []
        self._results: List[SearchResult] = []

        layout = QVBoxLayout(self)

        # -- query row ----------------------------------------------------
        query_row = QHBoxLayout()
        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("Search torrent indexers...")
        self.query_edit.returnPressed.connect(self.start_search)
        query_row.addWidget(self.query_edit, stretch=1)

        self.category_combo = QComboBox(self)
        self.category_combo.addItems(_CATEGORY_CHOICES)
        self.category_combo.setToolTip("Category hint (honored by providers that support it)")
        query_row.addWidget(self.category_combo)

        self.search_button = QPushButton("Search", self)
        self.search_button.clicked.connect(self.start_search)
        query_row.addWidget(self.search_button)
        layout.addLayout(query_row)

        # -- results table ------------------------------------------------
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["Title", "Size", "Seeders", "Leechers", "Sources", ""])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        # Interactive = user can drag any column border to resize. Title
        # stretches to fill leftover space but stays draggable; the rest get
        # sensible starting widths.
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_SIZE, self.COL_SEEDERS, self.COL_LEECHERS, self.COL_SOURCES):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_SIZE, 90)
        self.table.setColumnWidth(self.COL_SEEDERS, 80)
        self.table.setColumnWidth(self.COL_LEECHERS, 80)
        self.table.setColumnWidth(self.COL_SOURCES, 140)
        # The Add-button column is fixed and not draggable/sortable.
        header.setSectionResizeMode(self.COL_ADD, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.COL_ADD, 70)
        # Click a header to sort; click again to reverse. Numeric columns sort
        # numerically (see SortKeyItem below), not as text.
        self.table.setSortingEnabled(True)
        header.setSortIndicatorShown(True)
        header.setSectionsClickable(True)
        # Default sort: best results first (Seeders, descending) -- the ranker
        # already orders results, this just reflects it in the header.
        self.table.sortItems(self.COL_SEEDERS, Qt.SortOrder.DescendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(
            lambda item: self.add_result_to_torrents(self._row_result_index(item.row())))
        layout.addWidget(self.table)

        # -- status line --------------------------------------------------
        self.result_summary = QLabel("", self)
        self.result_summary.setWordWrap(True)
        layout.addWidget(self.result_summary)

        enabled = [p["name"] for p in self.search_service.provider_infos() if p["enabled"]]
        if enabled:
            self.result_summary.setText(f"Providers enabled: {', '.join(enabled)}")
        else:
            self.result_summary.setText(
                "No search providers are enabled -- enable one under "
                "\"search_providers\" in settings.json and restart."
            )

    # -- searching --------------------------------------------------------
    def start_search(self):
        text = self.query_edit.text().strip()
        if not text:
            return
        category = self.category_combo.currentText()
        try:
            query = SearchQuery(text=text, category=None if category == "Any" else category)
        except ValueError as e:
            self.status_update_requested.emit(str(e), 5000)
            return

        self.search_button.setEnabled(False)
        self.result_summary.setText("Searching...")

        worker = SearchWorker(self.search_service, query, parent=self)
        worker.search_finished.connect(self._on_search_finished)
        worker.search_failed.connect(self._on_search_failed)
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._worker_refs.append(worker)
        worker.start()

    def _release_worker(self, worker: SearchWorker):
        if worker in self._worker_refs:
            self._worker_refs.remove(worker)
        worker.deleteLater()

    def _on_search_finished(self, outcome: SearchOutcome):
        self.search_button.setEnabled(True)
        self._results = outcome.results
        self._populate_table(outcome.results)

        if not outcome.providers_queried:
            self.result_summary.setText(
                "No search providers are enabled -- enable one under "
                "\"search_providers\" in settings.json and restart."
            )
            return
        parts = [f"{len(outcome.results)} results from {', '.join(outcome.providers_queried)}"]
        for provider, message in outcome.errors.items():
            parts.append(f"{provider} failed: {message}")
        self.result_summary.setText(" -- ".join(parts))

    def _on_search_failed(self, message: str):
        self.search_button.setEnabled(True)
        self.result_summary.setText(f"Search failed: {message}")

    def _populate_table(self, results: List[SearchResult]):
        # Disable sorting while we fill -- otherwise Qt re-sorts on every
        # setItem and rows shuffle under us mid-population. Re-enable after.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            title_item = QTableWidgetItem(result.title)
            title_item.setToolTip(result.title)
            # Stash the index into self._results so actions resolve the right
            # result regardless of how the view is later sorted.
            title_item.setData(_RESULT_INDEX_ROLE, row)
            self.table.setItem(row, self.COL_TITLE, title_item)

            size_text = format_size(result.size_bytes) if result.size_bytes else "--"
            # (column, display text, numeric sort key)
            numeric_cells = (
                (self.COL_SIZE, size_text, float(result.size_bytes or 0)),
                (self.COL_SEEDERS, str(result.seeders), float(result.seeders)),
                (self.COL_LEECHERS, str(result.leechers), float(result.leechers)),
            )
            for col, text, sort_key in numeric_cells:
                item = _SortKeyItem(text, sort_key)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

            sources_item = QTableWidgetItem(", ".join(sorted({s.provider for s in result.sources})))
            sources_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, self.COL_SOURCES, sources_item)

            add_button = QPushButton("Add", self.table)
            add_button.setObjectName("secondary")
            if self.torrent_panel is None:
                add_button.setEnabled(False)
                add_button.setToolTip("Torrent support isn't available (libtorrent missing).")
            else:
                add_button.setToolTip("Add to the Torrents tab and start downloading")
                # Resolve the result from the button's current row at click
                # time (not a captured row), so sorting can't misroute it.
                add_button.clicked.connect(
                    lambda _checked=False, b=add_button: self.add_result_to_torrents(
                        self._result_index_for_button(b)))
            self.table.setCellWidget(row, self.COL_ADD, add_button)

        self.table.setSortingEnabled(True)

    # -- acting on results ------------------------------------------------
    def _row_result_index(self, visual_row: int) -> int:
        """Translate a *visual* table row (which changes when the user sorts)
        into an index into self._results (which never moves). Reads the index
        stashed on the title item. Returns -1 if the row is invalid."""
        if visual_row < 0:
            return -1
        title_item = self.table.item(visual_row, self.COL_TITLE)
        if title_item is None:
            return -1
        idx = title_item.data(_RESULT_INDEX_ROLE)
        return int(idx) if idx is not None else -1

    def _result_index_for_button(self, button) -> int:
        """Find which result an in-cell Add button belongs to, by locating its
        current visual row (buttons move with their row when sorted)."""
        for visual_row in range(self.table.rowCount()):
            if self.table.cellWidget(visual_row, self.COL_ADD) is button:
                return self._row_result_index(visual_row)
        return -1

    def _result_at(self, row: int) -> Optional[SearchResult]:
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def add_result_to_torrents(self, row: int):
        result = self._result_at(row)
        if result is None:
            return
        if self.torrent_panel is None:
            self.status_update_requested.emit(
                "Torrent support isn't available -- install libtorrent to add torrents.", 6000)
            return
        magnet = result.ensure_magnet()
        if not magnet:
            self.status_update_requested.emit(
                "This result has no magnet link or infohash to add.", 6000)
            return
        torrent_id = self.torrent_panel.add_torrent(mode="magnet", magnet_uri=magnet)
        if torrent_id:
            self.status_update_requested.emit(f"Added to Torrents: {result.title}", 5000)

    def copy_magnet(self, row: int):
        result = self._result_at(row)
        magnet = result.ensure_magnet() if result else None
        if magnet:
            QGuiApplication.clipboard().setText(magnet)
            self.status_update_requested.emit("Magnet link copied to clipboard.", 3000)

    def _show_context_menu(self, pos):
        visual_row = self.table.rowAt(pos.y())
        result_index = self._row_result_index(visual_row)
        if result_index < 0:
            return
        menu = QMenu(self)
        add_action = menu.addAction("Add to Torrents")
        add_action.setEnabled(self.torrent_panel is not None)
        copy_action = menu.addAction("Copy Magnet Link")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is add_action:
            self.add_result_to_torrents(result_index)
        elif chosen is copy_action:
            self.copy_magnet(result_index)
