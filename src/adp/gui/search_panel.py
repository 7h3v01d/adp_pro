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
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_SIZE, self.COL_SEEDERS, self.COL_LEECHERS, self.COL_SOURCES, self.COL_ADD):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(lambda item: self.add_result_to_torrents(item.row()))
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
        self.table.setRowCount(0)
        self.table.setRowCount(len(results))
        for row, result in enumerate(results):
            title_item = QTableWidgetItem(result.title)
            title_item.setToolTip(result.title)
            self.table.setItem(row, self.COL_TITLE, title_item)

            size_text = format_size(result.size_bytes) if result.size_bytes else "--"
            for col, text in (
                (self.COL_SIZE, size_text),
                (self.COL_SEEDERS, str(result.seeders)),
                (self.COL_LEECHERS, str(result.leechers)),
                (self.COL_SOURCES, ", ".join(sorted({s.provider for s in result.sources}))),
            ):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

            add_button = QPushButton("Add", self.table)
            add_button.setObjectName("secondary")
            if self.torrent_panel is None:
                add_button.setEnabled(False)
                add_button.setToolTip("Torrent support isn't available (libtorrent missing).")
            else:
                add_button.setToolTip("Add to the Torrents tab and start downloading")
                add_button.clicked.connect(lambda _checked=False, r=row: self.add_result_to_torrents(r))
            self.table.setCellWidget(row, self.COL_ADD, add_button)

    # -- acting on results ------------------------------------------------
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
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        add_action = menu.addAction("Add to Torrents")
        add_action.setEnabled(self.torrent_panel is not None)
        copy_action = menu.addAction("Copy Magnet Link")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is add_action:
            self.add_result_to_torrents(row)
        elif chosen is copy_action:
            self.copy_magnet(row)
