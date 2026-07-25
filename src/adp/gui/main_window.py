# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import sys
import os
import subprocess
import uuid
import logging
from collections import deque
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QListWidget, QLabel, QListWidgetItem,
    QSpinBox, QMenu, QStatusBar, QComboBox, QToolBar, QMessageBox, QTabWidget
)
from PyQt6.QtCore import QThreadPool, pyqtSlot, Qt, pyqtSignal
from PyQt6.QtGui import QAction

from adp.core.downloader import DownloadManager
from adp.core.logging_setup import get_current_log_path
from adp.core.models import Status, DownloadRecord, DEFAULT_CATEGORY, category_for_filename
from adp.core.session import SessionStore
from adp.core.app_settings import AppSettingsStore
from adp.core.scheduler import DownloadScheduler
from adp.utils.url_utils import extract_urls_from_mime_text, looks_like_download_url
from adp.gui.widgets import DownloadItemWidget
from adp.gui.dialogs import AddDownloadDialog, SettingsDialog, ApiInfoDialog

try:
    from adp.gui.torrent_panel import TorrentPanel
    TORRENT_SUPPORT_AVAILABLE = True
    _TORRENT_IMPORT_ERROR = None
except ImportError as e:
    TorrentPanel = None
    TORRENT_SUPPORT_AVAILABLE = False
    _TORRENT_IMPORT_ERROR = e

from adp.gui.stats_panel import StatsPanel
from adp.gui.search_panel import SearchPanel
from adp.search.service import SearchService
from adp.core.storage import resolve_dir, ConfiguredPathUnavailableError

from adp.api.auth import ApiKeyStore
from adp.api.bridge import GuiBridge
from adp.api.controller import AppController
from adp.api.rest_server import build_app, start_api_server_with_fallback
from adp.gui.theme import stylesheet_for
from adp.gui.tray import DownloaderTrayIcon

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

    def _download_dir(self) -> str:
        """The resolved default download folder for the Add dialog: the user's
        configured download_dir when set and usable, else a Downloads folder
        under the state dir. Raises ConfiguredPathUnavailableError if the user
        configured a location that's currently unreachable -- the caller shows
        that to the user rather than silently redirecting to the system drive."""
        return resolve_dir(self.settings.get("download_dir"),
                            os.path.join(self.state_dir, "downloads"))

    def add_download_from_dialog(self):
        try:
            default_dir = self._download_dir()
        except ConfiguredPathUnavailableError as e:
            self.status_update_requested.emit(
                f"Download folder unavailable: {e}. Fix it in Settings.", 8000)
            return
        dialog = AddDownloadDialog(self, thread_pool=self.thread_pool,
                                    default_speed_limit_bps=self.settings.get("default_speed_limit_bps", 0),
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
        """Returns an existing manager already writing to (or paused on)
        save_path, if any -- used to prevent two managers from ever holding
        open file handles to the same file at once."""
        target = os.path.normcase(os.path.abspath(save_path))
        for manager in self.downloads.values():
            if manager.status.is_active or manager.status == Status.PAUSED:
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
        if download_id and download_id in self.downloads:
            self.downloads[download_id].resume()

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


class MainWindow(QMainWindow):
    """Standalone window hosting the downloader and torrent panels as tabs,
    plus Pro chrome: toolbar, tray icon, and theme switching."""

    def __init__(self, state_dir=None):
        super().__init__()
        self.setWindowTitle("Accelerated Downloader Pro")
        self.resize(900, 640)

        self.download_panel = DownloadPanel(self, state_dir=state_dir)
        self.setStatusBar(QStatusBar(self))

        settings = self.download_panel.settings
        self.torrent_panel = None
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.download_panel, "Downloads")

        if TORRENT_SUPPORT_AVAILABLE:
            default_torrent_dir = os.path.join(state_dir, "torrent_downloads")
            try:
                torrent_dir = resolve_dir(
                    settings.get("torrent_download_dir"), default_torrent_dir)
            except ConfiguredPathUnavailableError as e:
                # At construction there's no status bar yet and we must not
                # crash startup. Fall back to the default location but log
                # loudly; the user will also see the failure the next time
                # they open Settings or add a torrent.
                logger.warning("Configured torrent folder unavailable at startup, "
                               "using default: %s", e)
                os.makedirs(default_torrent_dir, exist_ok=True)
                torrent_dir = default_torrent_dir
            self.torrent_panel = TorrentPanel(
                self, state_dir=state_dir,
                listen_port=settings.get("torrent_listen_port", 6881),
                enable_dht=settings.get("torrent_enable_dht", True),
                default_seed_ratio_limit=settings.get("torrent_default_seed_ratio_limit", 0.0),
                default_save_path=torrent_dir,
            )
            self.tabs.addTab(self.torrent_panel, "Torrents")
            self.torrent_panel.status_update_requested.connect(self.statusBar().showMessage)
            self.torrent_panel.torrent_completed.connect(self._notify_torrent_completion)
        else:
            placeholder = QLabel(
                "Torrent support isn't available: the 'libtorrent' package couldn't be "
                "imported.\n\nInstall it with:\n\n    pip install libtorrent\n\nand restart "
                "the app to enable the Torrents tab."
            )
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setWordWrap(True)
            self.tabs.addTab(placeholder, "Torrents (unavailable)")
            logger.warning(f"Torrent support disabled: {_TORRENT_IMPORT_ERROR}")

        # Search tab: settings are read via a getter so Settings-dialog
        # changes apply on the next search, and the same SearchService
        # instance backs the REST /search route and MCP search tools.
        self.search_service = SearchService(lambda: self.download_panel.settings)
        self.search_panel = SearchPanel(
            self.search_service, torrent_panel=self.torrent_panel, parent=self,
        )
        self.tabs.addTab(self.search_panel, "Search")
        self.search_panel.status_update_requested.connect(self.statusBar().showMessage)

        self.stats_panel = StatsPanel(
            self, download_panel=self.download_panel, torrent_panel=self.torrent_panel, state_dir=state_dir,
        )
        self.tabs.addTab(self.stats_panel, "Stats")

        self.setCentralWidget(self.tabs)

        self.download_panel.status_update_requested.connect(self.statusBar().showMessage)
        self.download_panel.download_completed.connect(self._notify_download_completion)

        self._start_api_server(state_dir, settings)

        self._build_toolbar()
        self.apply_theme(self.download_panel.settings.get("theme", "dark"))

        self.tray_icon = DownloaderTrayIcon(self)
        self.tray_icon.show()
        self._force_quit = False

    def _start_api_server(self, state_dir, settings):
        self.gui_bridge = GuiBridge(parent=self)
        self.api_key_store = ApiKeyStore(state_dir)
        self.app_controller = AppController(
            self.gui_bridge, self.download_panel, self.torrent_panel, self.stats_panel,
            search_service=self.search_service,
        )
        app = build_app(self.app_controller, self.api_key_store)
        try:
            self.api_server = start_api_server_with_fallback(
                app, host="127.0.0.1", preferred_port=settings.get("api_port", 8765),
            )
            logger.info(f"API server listening on {self.api_server.base_url} (REST) "
                        f"and {self.api_server.base_url}/mcp (MCP)")
        except RuntimeError as e:
            self.api_server = None
            logger.error(f"Could not start the API server: {e}")

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.open_settings)
        toolbar.addAction(settings_action)

        logs_action = QAction("View Logs", self)
        logs_action.setToolTip("Open the folder containing the diagnostic log file")
        logs_action.triggered.connect(self.open_log_folder)
        toolbar.addAction(logs_action)

        api_action = QAction("API Access", self)
        api_action.setToolTip("Connection info for controlling this app via REST or MCP")
        api_action.triggered.connect(self.open_api_info)
        toolbar.addAction(api_action)

    def open_api_info(self):
        if self.api_server is None:
            QMessageBox.warning(self, "API Access", "The API server could not be started. Check the logs.")
            return
        dialog = ApiInfoDialog(self, base_url=self.api_server.base_url, api_key=self.api_key_store.key)
        if dialog.exec() and dialog.regenerate_requested:
            new_key = self.api_key_store.regenerate()
            QMessageBox.information(
                self, "API Access",
                "A new API key was generated. Anything previously configured with the old "
                "key (scripts, AI tool configs) will need updating."
            )

    def open_log_folder(self):
        log_path = get_current_log_path()
        if not log_path or not os.path.exists(log_path):
            QMessageBox.information(self, "View Logs", "No log file has been created yet.")
            return
        try:
            if sys.platform == "win32":
                subprocess.run(['explorer', '/select,', os.path.normpath(log_path)])
            elif sys.platform == "darwin":
                subprocess.run(['open', '-R', log_path])
            else:
                subprocess.run(['xdg-open', os.path.dirname(log_path)])
        except OSError as e:
            QMessageBox.warning(self, "View Logs", f"Could not open the log folder:\n{e}")

    def open_settings(self):
        dialog = SettingsDialog(self, current_settings=self.download_panel.settings)
        if dialog.exec():
            new_settings = dialog.get_settings()
            self.download_panel.apply_settings(new_settings)
            self.apply_theme(new_settings.get("theme", "dark"))
            # Apply a changed torrent folder without a restart: future adds
            # use it, existing torrents keep the path they started with. The
            # download folder is read fresh from settings each time the Add
            # dialog opens, so it needs no explicit push here.
            if self.torrent_panel is not None:
                try:
                    new_torrent_dir = resolve_dir(
                        new_settings.get("torrent_download_dir"),
                        os.path.join(self.download_panel.state_dir, "torrent_downloads"))
                    self.torrent_panel.set_default_save_path(new_torrent_dir)
                except ConfiguredPathUnavailableError as e:
                    self.statusBar().showMessage(
                        f"Torrent folder unavailable: {e}. Existing folder kept.", 8000)

    def apply_theme(self, theme_name: str):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(stylesheet_for(theme_name))

    def _notify_download_completion(self, download_id, filename):
        if self.download_panel.settings.get("notifications_enabled", True):
            self.tray_icon.notify("Download complete", filename)

    def _notify_torrent_completion(self, torrent_id, name):
        if self.download_panel.settings.get("notifications_enabled", True):
            self.tray_icon.notify("Torrent finished", name)

    def quit_application(self):
        self._force_quit = True
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self.download_panel.settings.get("minimize_to_tray", True) and not self._force_quit:
            event.ignore()
            self.hide()
            self.tray_icon.notify("Still running", "Accelerated Downloader Pro is minimized to the tray.")
            return
        if self.api_server is not None:
            self.api_server.stop()
        self.gui_bridge.stop()
        self.download_panel.closeEvent(event)
        if self.torrent_panel is not None:
            self.torrent_panel.closeEvent(event)
        self.stats_panel.closeEvent(event)
        super().closeEvent(event)


def create_app(argv=None):
    app = QApplication(argv or sys.argv)
    app.setQuitOnLastWindowClosed(False)
    return app


if __name__ == "__main__":
    app = create_app()
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
