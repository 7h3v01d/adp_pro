# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The Downloads tab: DownloadPanel owns the download queue, the list widget,
add/pause/resume/stop/retry/remove actions, scheduling, clipboard monitoring,
session persistence, and the settings the whole app reads. It's the largest
single piece of GUI, split out from the window chrome for maintainability.

Imported directly by the API controller tests and the dev test rig, so it's
kept importable without constructing a full MainWindow.
"""

import os
import sys
import uuid
import subprocess
import logging
from collections import deque
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QListWidget, QLabel, QListWidgetItem, QSpinBox, QMenu, QComboBox,
)
from PyQt6.QtCore import QThreadPool, pyqtSlot, Qt, pyqtSignal
from PyQt6.QtGui import QAction

from adp.core.downloader import DownloadManager
from adp.core.models import Status, DownloadRecord, category_for_filename
from adp.core.session import SessionStore
from adp.core.app_settings import AppSettingsStore
from adp.core.scheduler import DownloadScheduler
from adp.core.storage import resolve_dir, ConfiguredPathUnavailableError
from adp.utils.url_utils import extract_urls_from_mime_text, looks_like_download_url
from adp.gui.widgets import DownloadItemWidget
from adp.gui.dialogs import AddDownloadDialog

logger = logging.getLogger(__name__)

ALL_CATEGORIES_FILTER = "All Categories"


class DownloadPanel(QWidget):
    """The core download panel: queue management, search/filter, add/pause/
    resume/stop/retry, scheduling, and session persistence."""

    status_update_requested = pyqtSignal(str, int)
    download_completed = pyqtSignal(str, str)  # download_id, filename -- for tray notifications

    def __init__(self, parent=None, state_dir=None, thread_pool=None):
        super().__init__(parent)
        self.thread_pool = thread_pool or QThreadPool()
        if thread_pool is None:
            self.thread_pool.setMaxThreadCount(16)
        self.downloads: dict[str, DownloadManager] = {}
        self.download_queue = deque()
        self.active_downloads = 0

        state_dir = state_dir or os.getcwd()
        self.state_dir = state_dir
        self.session_store = SessionStore(os.path.join(state_dir, 'downloads_session.json'))
        self.settings_store = AppSettingsStore(os.path.join(state_dir, 'settings.json'))
        self.settings = self.settings_store.load()

        self.scheduler = DownloadScheduler()
        self.scheduler.due.connect(self._on_schedule_due)
        self.scheduler.start()

        layout = QVBoxLayout(self)
        controls_layout = QHBoxLayout()

        add_button = QPushButton("Add Download")
        add_button.clicked.connect(self.add_download_from_dialog)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search downloads...")
        self.search_input.textChanged.connect(self.apply_filters)

        self.category_filter = QComboBox()
        self.category_filter.addItem(ALL_CATEGORIES_FILTER)
        self.category_filter.currentIndexChanged.connect(self.apply_filters)

        self.concurrency_spinbox = QSpinBox()
        self.concurrency_spinbox.setRange(1, 10)
        self.concurrency_spinbox.setValue(3)
        self.concurrency_spinbox.setToolTip("Max simultaneous downloads")

        controls_layout.addWidget(add_button)
        controls_layout.addWidget(self.search_input)
        controls_layout.addWidget(self.category_filter)
        controls_layout.addStretch()
        controls_layout.addWidget(QLabel("Concurrent Downloads:"))
        controls_layout.addWidget(self.concurrency_spinbox)
        layout.addLayout(controls_layout)

        self.download_list = QListWidget()
        self.download_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.download_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.download_list)

        self.setAcceptDrops(True)

        self.create_actions()
        self.load_downloads()

        self._clipboard_last_seen = None
        if self.settings.get("clipboard_monitor_enabled"):
            self.enable_clipboard_monitor()

    # -- drag and drop ---------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        urls = []
        if mime.hasUrls():
            urls = [u.toString() for u in mime.urls()]
        elif mime.hasText():
            urls = extract_urls_from_mime_text(mime.text())

        for url in urls:
            self._prompt_add_for_dropped_url(url)
        event.acceptProposedAction()

    def _prompt_add_for_dropped_url(self, url):
        try:
            default_dir = self._download_dir()
        except ConfiguredPathUnavailableError as e:
            self.status_update_requested.emit(
                f"Download folder unavailable: {e}. Fix it in Settings.", 8000)
            return
        dialog = AddDownloadDialog(self, thread_pool=self.thread_pool,
                                    default_speed_limit_bps=self.settings.get("default_speed_limit_bps", 0),
                                    default_verify_tls=self.settings.get("verify_tls", True),
                                    default_dir=default_dir)
        dialog.url_input.setText(url)
        if dialog.exec():
            data = dialog.get_data()
            self.add_download(**data)

    # -- clipboard monitoring --------------------------------------------
    def enable_clipboard_monitor(self):
        clipboard = QApplication.clipboard()
        clipboard.dataChanged.connect(self._on_clipboard_changed)

    def disable_clipboard_monitor(self):
        clipboard = QApplication.clipboard()
        try:
            clipboard.dataChanged.disconnect(self._on_clipboard_changed)
        except TypeError:
            pass  # wasn't connected

    def _on_clipboard_changed(self):
        text = QApplication.clipboard().text()
        if text == self._clipboard_last_seen:
            return
        self._clipboard_last_seen = text
        if looks_like_download_url(text):
            self.status_update_requested.emit(
                f"Downloadable link detected in clipboard: {text}", 6000
            )
            self._prompt_add_for_dropped_url(text)

    # -- actions / context menu ------------------------------------------
    def create_actions(self):
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pause_selected_download)
        self.resume_action = QAction("Resume", self)
        self.resume_action.triggered.connect(self.resume_selected_download)
        self.stop_action = QAction("Stop", self)
        self.stop_action.triggered.connect(self.stop_selected_download)
        self.retry_action = QAction("Retry", self)
        self.retry_action.triggered.connect(self.retry_selected_download)
        self.remove_action = QAction("Remove from List", self)
        self.remove_action.triggered.connect(self.remove_selected_download)
        self.open_action = QAction("Open File", self)
        self.open_action.triggered.connect(self.open_file)
        self.open_location_action = QAction("Open Folder", self)
        self.open_location_action.triggered.connect(self.open_file_location)
        self.unschedule_action = QAction("Start Now", self)
        self.unschedule_action.triggered.connect(self.start_scheduled_now)

    def show_context_menu(self, position):
        item = self.download_list.itemAt(position)
        if not item:
            return

        self.download_list.setCurrentItem(item)
        download_id = item.data(Qt.ItemDataRole.UserRole)
        manager = self.downloads.get(download_id)
        if not manager:
            return

        menu = QMenu(self)
        status = manager.status
        if self.scheduler.is_scheduled(download_id):
            menu.addAction(self.unschedule_action)
        elif status == Status.DOWNLOADING:
            menu.addAction(self.pause_action)
            menu.addAction(self.stop_action)
        elif status == Status.PAUSED:
            menu.addAction(self.resume_action)
            menu.addAction(self.stop_action)
        elif status in [Status.ERROR, Status.STOPPED]:
            menu.addAction(self.retry_action)
        elif status == Status.COMPLETED:
            if os.path.exists(manager.save_path):
                menu.addAction(self.open_action)
                menu.addAction(self.open_location_action)
            else:
                menu.addAction(self.retry_action)

        menu.addSeparator()
        menu.addAction(self.remove_action)
        menu.exec(self.download_list.mapToGlobal(position))

    # -- add / start -------------------------------------------------------
    def add_download(self, url, save_path, checksum=None, num_threads=4, start_immediately=True,
                      headers=None, category=None, speed_limit_bps=0, scheduled_time=None,
                      verify_tls=None, download_id=None):
        if not (url and save_path):
            return None, None

        conflict = self._find_active_manager_for_path(save_path)
        if conflict is not None:
            self.status_update_requested.emit(
                f"'{os.path.basename(save_path)}' is already downloading or paused -- "
                "skipped to avoid two downloads writing to the same file.", 6000
            )
            return None, None

        # Preserve the id across a restart when restoring from session, so
        # external controllers (REST/MCP) and the .progress sidecar keep
        # referring to the same download. New downloads get a fresh uuid.
        download_id = download_id or str(uuid.uuid4())
        category = category or category_for_filename(os.path.basename(save_path))
        item_widget = DownloadItemWidget(download_id, save_path, category=category)
        list_item = QListWidgetItem(self.download_list)
        list_item.setSizeHint(item_widget.sizeHint())
        list_item.setData(Qt.ItemDataRole.UserRole, download_id)

        self.download_list.addItem(list_item)
        self.download_list.setItemWidget(list_item, item_widget)

        if verify_tls is None:
            verify_tls = bool(self.settings.get("verify_tls", True))
        manager = DownloadManager(download_id, url, save_path, self.thread_pool, num_threads,
                                   checksum, headers=headers, category=category,
                                   speed_limit_bps=speed_limit_bps, verify_tls=verify_tls)
        self.downloads[download_id] = manager

        manager.progress_updated.connect(self.update_download_progress)
        manager.download_finished.connect(self.on_download_finished)
        manager.error_occurred.connect(self.on_download_error)

        self._register_category(category)

        if scheduled_time:
            when = datetime.fromisoformat(scheduled_time)
            self.scheduler.schedule(download_id, when)
            item_widget.set_scheduled(when.strftime("%Y-%m-%d %H:%M"))
        else:
            self.download_queue.append(manager)
            if start_immediately:
                self.process_queue()

        self.apply_filters()
        return manager, item_widget

    def _register_category(self, category):
        if self.category_filter.findText(category) < 0:
            self.category_filter.addItem(category)

    def _on_schedule_due(self, download_id):
        manager = self.downloads.get(download_id)
        if not manager:
            return
        widget = self.find_widget(download_id)
        if widget:
            widget.info_label.setText("Status: Pending | Queued from schedule")
        self.download_queue.append(manager)
        self.process_queue()

    def start_scheduled_now(self):
        download_id = self.get_selected_download_id()
        if download_id and self.scheduler.is_scheduled(download_id):
            self.scheduler.unschedule(download_id)
            self._on_schedule_due(download_id)

    def resolve_download_dir(self) -> str:
        """The resolved default download folder, shared by the GUI Add dialog
        and the REST/MCP controller so every surface lands files in the same
        place: the user's configured download_dir when set and usable, else a
        Downloads folder under the state dir. Raises
        ConfiguredPathUnavailableError if the configured location is currently
        unreachable -- the caller surfaces that rather than silently
        redirecting to the system drive."""
        return resolve_dir(self.settings.get("download_dir"),
                            os.path.join(self.state_dir, "downloads"))

    # Back-compat alias for existing internal callers.
    _download_dir = resolve_download_dir

    def add_download_from_dialog(self):
        try:
            default_dir = self._download_dir()
        except ConfiguredPathUnavailableError as e:
            self.status_update_requested.emit(
                f"Download folder unavailable: {e}. Fix it in Settings.", 8000)
            return
        dialog = AddDownloadDialog(self, thread_pool=self.thread_pool,
                                    default_speed_limit_bps=self.settings.get("default_speed_limit_bps", 0),
                                    default_verify_tls=self.settings.get("verify_tls", True),
                                    default_dir=default_dir)
        if dialog.exec():
            data = dialog.get_data()
            self.add_download(**data)

    def process_queue(self):
        max_active = self.concurrency_spinbox.value()
        while self.active_downloads < max_active and self.download_queue:
            manager = self.download_queue.popleft()
            if manager.status == Status.PENDING:
                self.active_downloads += 1
                manager.start()

    # -- filtering ----------------------------------------------------------
    def apply_filters(self):
        query = self.search_input.text().strip().lower()
        category = self.category_filter.currentText()

        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            download_id = item.data(Qt.ItemDataRole.UserRole)
            manager = self.downloads.get(download_id)
            if not manager:
                continue
            matches_query = query in manager.filename.lower() if query else True
            matches_category = category == ALL_CATEGORIES_FILTER or manager.category == category
            item.setHidden(not (matches_query and matches_category))

    # -- progress / completion handlers -----------------------------------
    @pyqtSlot(str, 'qint64', 'qint64', float, str)
    def update_download_progress(self, download_id, downloaded, total, speed, status):
        widget = self.find_widget(download_id)
        if widget:
            widget.update_progress(downloaded, total, speed, status)

    def on_download_finished(self, download_id, filename):
        manager = self.downloads.get(download_id)
        if not manager:
            return

        self.status_update_requested.emit(f"Completed: {filename}", 5000)
        widget = self.find_widget(download_id)
        if widget:
            widget.set_final_status("Completed")
        self.download_completed.emit(download_id, filename)
        self.finish_download_slot(download_id)

    def on_download_error(self, download_id, error_message):
        manager = self.downloads.get(download_id)
        if not manager:
            return

        self.status_update_requested.emit(f"Error: {manager.filename} - {error_message}", 8000)
        widget = self.find_widget(download_id)
        if widget:
            widget.set_final_status("Error", error_message)
        self.finish_download_slot(download_id)

    def finish_download_slot(self, download_id):
        if download_id in self.downloads:
            self.active_downloads = max(0, self.active_downloads - 1)
            self.process_queue()

    # -- selection helpers ---------------------------------------------------
    def get_selected_download_id(self):
        selected_items = self.download_list.selectedItems()
        return selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None

    def _find_active_manager_for_path(self, save_path):
        """Returns an existing non-terminal manager already targeting save_path,
        if any -- used to prevent two managers from ever writing to the same
        file. A destination is reserved the moment a job is accepted (PENDING,
        QUEUED/scheduled, PAUSED, STARTING, DOWNLOADING), not only once its
        first worker starts: otherwise two PENDING or scheduled downloads with
        the same path both get accepted and collide when they start."""
        target = os.path.normcase(os.path.abspath(save_path))
        for manager in self.downloads.values():
            if not manager.status.is_terminal:
                if os.path.normcase(os.path.abspath(manager.save_path)) == target:
                    return manager
        return None

    def find_widget(self, download_id):
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == download_id:
                return self.download_list.itemWidget(item)
        return None

    # -- per-download controls -----------------------------------------------
    def pause_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            self.downloads[download_id].pause()

    def resume_selected_download(self):
        download_id = self.get_selected_download_id()
        if not (download_id and download_id in self.downloads):
            return
        manager = self.downloads[download_id]
        if not manager.metadata_initialized:
            # Restored-from-session pause: this job never occupied a
            # concurrency slot. Resuming it is really "start it", so route it
            # through the queue/accounting like any fresh start, honouring the
            # max-active limit. Its resume() will take the start() path.
            manager.set_status(Status.PENDING)
            if manager not in self.download_queue:
                self.download_queue.append(manager)
            self.process_queue()
        else:
            # Runtime pause -> resume: the slot was counted at start and held
            # across the pause, so just resume in place.
            manager.resume()

    def stop_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id and download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING]:
                manager.stop()
                self.finish_download_slot(download_id)
                widget = self.find_widget(download_id)
                if widget:
                    widget.set_final_status("Stopped")

    def retry_selected_download(self):
        download_id = self.get_selected_download_id()
        if not (download_id and download_id in self.downloads):
            return
        manager = self.downloads[download_id]
        if manager.status not in (Status.ERROR, Status.STOPPED, Status.COMPLETED):
            return
        # Reset to PENDING and let process_queue be the single owner of
        # starting downloads and incrementing active_downloads. Previously
        # this both queued the manager AND called manager.retry() directly:
        # process_queue would drop the non-PENDING manager without starting
        # or counting it, and the direct start ran it *outside* the
        # concurrency limit -- and a COMPLETED retry did nothing at all.
        manager.prepare_retry()
        if manager not in self.download_queue:
            self.download_queue.append(manager)
        self.process_queue()

    def remove_selected_download(self):
        selected_items = self.download_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        download_id = item.data(Qt.ItemDataRole.UserRole)

        self.scheduler.unschedule(download_id)
        if download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING]:
                manager.stop()
                self.finish_download_slot(download_id)
            if manager in self.download_queue:
                self.download_queue.remove(manager)
            del self.downloads[download_id]

        self.download_list.takeItem(self.download_list.row(item))

    def open_file(self):
        download_id = self.get_selected_download_id()
        manager = self.downloads.get(download_id)
        if manager and os.path.exists(manager.save_path):
            try:
                if sys.platform == "win32":
                    os.startfile(manager.save_path)
                else:
                    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", manager.save_path])
            except OSError as e:
                self.status_update_requested.emit(f"Could not open file: {e}", 5000)

    def open_file_location(self):
        download_id = self.get_selected_download_id()
        manager = self.downloads.get(download_id)
        if manager and os.path.exists(manager.save_path):
            try:
                if sys.platform == "win32":
                    subprocess.run(['explorer', '/select,', os.path.normpath(manager.save_path)])
                elif sys.platform == "darwin":
                    subprocess.run(['open', '-R', manager.save_path])
                else:
                    subprocess.run(['xdg-open', os.path.dirname(manager.save_path)])
            except OSError as e:
                self.status_update_requested.emit(f"Could not open folder: {e}", 5000)

    # -- persistence -----------------------------------------------------------
    # States that can't meaningfully resume are stored as-is; the two active
    # states are stored as recoverable equivalents so a restart genuinely
    # picks them back up (the .progress sidecar on disk lets them continue
    # from where they left off rather than restarting).
    _RESTORE_STATUS_MAP = {
        Status.DOWNLOADING: Status.PENDING,
        Status.STARTING: Status.PENDING,
        Status.PAUSED: Status.PAUSED,
    }

    def save_downloads(self):
        records = []
        all_downloads = list(self.downloads.values()) + list(self.download_queue)
        for manager in {m.download_id: m for m in all_downloads}.values():
            # Persist everything except downloads the user has removed. Active
            # (DOWNLOADING/STARTING) jobs are recorded as recoverable so they
            # actually come back on restart -- the whole point of the feature.
            store_status = self._RESTORE_STATUS_MAP.get(manager.status, manager.status)
            scheduled = self.scheduler.scheduled_time(manager.download_id)
            records.append(DownloadRecord(
                download_id=manager.download_id, url=manager.url, save_path=manager.save_path,
                checksum=manager.checksum, num_threads=manager.num_threads, headers=manager.headers,
                category=manager.category, speed_limit_bps=manager.speed_limiter.rate,
                scheduled_time=scheduled.isoformat() if scheduled else None,
                status=store_status.name, downloaded_size=manager.downloaded_size,
                total_size=manager.total_size,
            ))
        self.session_store.save(records)

    def load_downloads(self):
        for record in self.session_store.load():
            # Restore with the ORIGINAL id so the .progress sidecar and any
            # external controllers still line up. A job saved as PAUSED is
            # restored paused; a recoverable (formerly active) job is restored
            # pending and resumes automatically via process_queue().
            restore_paused = (record.status == Status.PAUSED.name)
            self.add_download(
                url=record.url, save_path=record.save_path, checksum=record.checksum,
                num_threads=record.num_threads, headers=record.headers, category=record.category,
                speed_limit_bps=record.speed_limit_bps, scheduled_time=record.scheduled_time,
                start_immediately=False, download_id=record.download_id,
            )
            if restore_paused:
                manager = self.downloads.get(record.download_id)
                if manager is not None:
                    # Keep it paused rather than auto-queuing it: the user
                    # paused it, that intent should survive the restart.
                    if manager in self.download_queue:
                        self.download_queue.remove(manager)
                    manager.set_status(Status.PAUSED)
        self.process_queue()

    def apply_settings(self, new_settings: dict):
        clipboard_was_on = self.settings.get("clipboard_monitor_enabled")
        self.settings = new_settings
        self.settings_store.save(new_settings)
        if new_settings.get("clipboard_monitor_enabled") and not clipboard_was_on:
            self.enable_clipboard_monitor()
        elif not new_settings.get("clipboard_monitor_enabled") and clipboard_was_on:
            self.disable_clipboard_monitor()

    def closeEvent(self, event):
        self.save_downloads()
        super().closeEvent(event)


