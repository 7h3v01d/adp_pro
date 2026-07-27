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
    QListWidget, QLabel, QListWidgetItem, QSpinBox, QMenu, QComboBox, QMessageBox,
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
        # The set of download_ids that currently hold a concurrency slot.
        # Using a set (not a bare counter) makes acquire/release idempotent, so
        # a double completion callback, or removing a restored-paused download
        # that never held a slot, can't corrupt the count. active_downloads is
        # derived from this, keeping the concurrency invariant robust.
        self._slot_holders: set[str] = set()

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
            if not self._confirm_overwrite_if_needed(data):
                return
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
    def _confirm_overwrite_if_needed(self, data) -> bool:
        """If the chosen save_path already holds a non-resumable file, ask the
        user before truncating it. Sets data['allow_overwrite'] on a Yes.
        Returns False if the user cancelled (caller should abort the add)."""
        save_path = data.get("save_path")
        if save_path and os.path.exists(save_path) and not os.path.exists(f"{save_path}.progress"):
            choice = QMessageBox.question(
                self, "File already exists",
                f"A file already exists at:\n{save_path}\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return False
            data["allow_overwrite"] = True
        return True

    def add_download(self, url, save_path, checksum=None, num_threads=4, start_immediately=True,
                      headers=None, category=None, speed_limit_bps=0, scheduled_time=None,
                      verify_tls=None, download_id=None, allow_overwrite=False):
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
                                   speed_limit_bps=speed_limit_bps, verify_tls=verify_tls,
                                   allow_overwrite=allow_overwrite)
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
        # Persist the session now that the job exists, so a crash before the
        # next periodic/close save still leaves a record that owns the
        # .progress sidecar on restart (rather than orphaning it).
        self.save_downloads()
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
        # The job's effective state changed (scheduled -> queued/active);
        # persist so a crash reflects that it's no longer merely scheduled.
        self.save_downloads()

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
            if not self._confirm_overwrite_if_needed(data):
                return
            self.add_download(**data)

    @property
    def active_downloads(self) -> int:
        """Number of downloads currently holding a concurrency slot."""
        return len(self._slot_holders)

    def _acquire_slot(self, download_id: str) -> None:
        self._slot_holders.add(download_id)  # idempotent

    def _release_slot(self, download_id: str) -> None:
        self._slot_holders.discard(download_id)  # idempotent; no-op if not held

    def process_queue(self):
        max_active = self.concurrency_spinbox.value()
        while len(self._slot_holders) < max_active and self.download_queue:
            manager = self.download_queue.popleft()
            if manager.status == Status.PENDING:
                self._acquire_slot(manager.download_id)
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
        # Persist the COMPLETED state now, so a crash before the next save
        # doesn't restore this as still-active.
        self.save_downloads()

    def on_download_error(self, download_id, error_message):
        manager = self.downloads.get(download_id)
        if not manager:
            return

        self.status_update_requested.emit(f"Error: {manager.filename} - {error_message}", 8000)
        widget = self.find_widget(download_id)
        if widget:
            widget.set_final_status("Error", error_message)
        self.finish_download_slot(download_id)
        # Persist the ERROR state and its recovery-policy fields (notably
        # restart_required, set before this signal fired) so a crash doesn't
        # lose them and let a later retry re-verify a corrupt file.
        self.save_downloads()

    def finish_download_slot(self, download_id):
        if download_id in self.downloads:
            # Release this download's slot (idempotent -- a double callback or a
            # download that never held a slot is harmless) and pull the queue.
            self._release_slot(download_id)
            self.process_queue()

    # -- selection helpers ---------------------------------------------------
    def get_selected_download_id(self):
        selected_items = self.download_list.selectedItems()
        return selected_items[0].data(Qt.ItemDataRole.UserRole) if selected_items else None

    def _find_path_conflict(self, save_path, exclude_download_id=None):
        """Returns an existing non-terminal manager already targeting save_path,
        if any -- the shared destination-reservation check used by BOTH adding
        a new download and retrying/resurrecting a terminal one. A destination
        is reserved the moment a job is accepted (PENDING, QUEUED/scheduled,
        PAUSED, STARTING, DOWNLOADING); terminal jobs (COMPLETED/ERROR/STOPPED)
        release their reservation. exclude_download_id skips the job being
        transitioned itself (so retry doesn't conflict with its own record)."""
        target = os.path.normcase(os.path.abspath(save_path))
        for manager in self.downloads.values():
            if exclude_download_id is not None and manager.download_id == exclude_download_id:
                continue
            if not manager.status.is_terminal:
                if os.path.normcase(os.path.abspath(manager.save_path)) == target:
                    return manager
        return None

    # Back-compat alias for the add-download path.
    _find_active_manager_for_path = _find_path_conflict

    def find_widget(self, download_id):
        for i in range(self.download_list.count()):
            item = self.download_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == download_id:
                return self.download_list.itemWidget(item)
        return None

    # -- per-download controls -----------------------------------------------
    def pause_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id:
            self.pause_download(download_id)

    def pause_download(self, download_id) -> bool:
        """Pause a download. Panel-level authority, called by GUI and API.
        Only an active (downloading/starting/verifying) download can be paused;
        returns False for anything else so callers (the API) can reject the
        transition rather than silently no-op."""
        if download_id not in self.downloads:
            return False
        manager = self.downloads[download_id]
        if manager.status not in (Status.DOWNLOADING, Status.STARTING):
            return False
        manager.pause()
        self.save_downloads()  # persist the PAUSED intent immediately
        return True

    def resume_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id:
            self.resume_download(download_id)

    def resume_download(self, download_id) -> bool:
        """Resume a download under the panel's concurrency policy. The single
        authority for resume, called by both GUI and API so REST/MCP can't
        bypass the restored-pause queue logic. Only a PAUSED download can be
        resumed -- a terminal (COMPLETED/STOPPED/ERROR) download must go
        through retry (which re-checks path reservation), not resume, or an
        API caller could tunnel a finished job back into active state and
        bypass the destination-conflict check."""
        if download_id not in self.downloads:
            return False
        manager = self.downloads[download_id]
        if manager.status != Status.PAUSED:
            return False
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
        self.save_downloads()  # persist the resumed intent
        return True

    def stop_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id:
            self.stop_download(download_id)

    def stop_download(self, download_id) -> bool:
        """Stop a download and release its slot. Panel-level authority."""
        if download_id not in self.downloads:
            return False
        manager = self.downloads[download_id]
        if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING,
                              Status.VERIFYING]:
            manager.stop()
            self.finish_download_slot(download_id)
            widget = self.find_widget(download_id)
            if widget:
                widget.set_final_status("Stopped")
            self.save_downloads()  # persist the STOPPED intent
            return True
        return False

    def retry_selected_download(self):
        download_id = self.get_selected_download_id()
        if download_id:
            self.retry_download(download_id)

    def retry_download(self, download_id) -> bool:
        """Retry a terminal download under the panel's concurrency + path-
        reservation policy. The single authority for retrying, called by both
        the GUI and the REST/MCP controller so the API can't bypass the rules.
        Returns True if the retry was accepted."""
        if download_id not in self.downloads:
            return False
        manager = self.downloads[download_id]
        if manager.status not in (Status.ERROR, Status.STOPPED, Status.COMPLETED):
            return False
        # A terminal job released its path reservation, so another download may
        # have since claimed the same destination. Re-check before resurrecting
        # this one into PENDING -- otherwise retry re-creates the very
        # two-managers-one-file collision the reservation exists to prevent.
        conflict = self._find_path_conflict(manager.save_path, exclude_download_id=download_id)
        if conflict is not None:
            self.status_update_requested.emit(
                f"Can't retry: '{os.path.basename(manager.save_path)}' is now targeted by "
                "another active download. Remove or redirect that one first.", 8000)
            return False
        # Reset to PENDING and let process_queue be the single owner of
        # starting downloads and incrementing active_downloads.
        manager.prepare_retry()
        if manager not in self.download_queue:
            self.download_queue.append(manager)
        self.process_queue()
        self.save_downloads()  # persist the retry (PENDING + recovery fields)
        return True

    def remove_selected_download(self):
        selected_items = self.download_list.selectedItems()
        if not selected_items:
            return

        item = selected_items[0]
        download_id = item.data(Qt.ItemDataRole.UserRole)

        self.scheduler.unschedule(download_id)
        if download_id in self.downloads:
            manager = self.downloads[download_id]
            if manager.status in [Status.DOWNLOADING, Status.PAUSED, Status.STARTING,
                                   Status.VERIFYING]:
                manager.stop()
            # Release any slot this download holds, regardless of status --
            # VERIFYING owns a slot too, and inferring ownership from the enum
            # is exactly what leaked it. The slot set is the source of truth.
            self.finish_download_slot(download_id)
            if manager in self.download_queue:
                self.download_queue.remove(manager)
            del self.downloads[download_id]

        self.download_list.takeItem(self.download_list.row(item))
        # Persist the removal immediately. Otherwise a crash before the next
        # save would leave the old record on disk and the job would come back
        # on restart -- the classic "I swear I deleted that" bug.
        self.save_downloads()

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
            # The .progress sidecar is the single source of truth for how many
            # bytes are actually on disk for a resumable job. Persisting a
            # second, save-time snapshot of downloaded_size in the session row
            # invites the two to disagree after a mid-download crash. So only
            # record a byte figure for states that have NO sidecar to recompute
            # from (COMPLETED/ERROR, restored for display); for recoverable
            # jobs store 0 and let load_progress() derive the real figure from
            # the sidecar on resume.
            has_own_byte_truth = store_status in (Status.COMPLETED, Status.ERROR)
            persisted_downloaded = manager.downloaded_size if has_own_byte_truth else 0
            records.append(DownloadRecord(
                download_id=manager.download_id, url=manager.url, save_path=manager.save_path,
                checksum=manager.checksum, num_threads=manager.num_threads, headers=manager.headers,
                category=manager.category, speed_limit_bps=manager.speed_limiter.rate,
                scheduled_time=scheduled.isoformat() if scheduled else None,
                status=store_status.name, downloaded_size=persisted_downloaded,
                total_size=manager.total_size, verify_tls=manager.verify_tls,
                destination_owned_by_adp=manager.destination_owned_by_adp,
                restart_required=manager.restart_required,
                allow_overwrite=manager.allow_overwrite,
            ))
        self.session_store.save(records)

    # Statuses that must be restored exactly as saved, NOT resurrected into
    # the queue. A COMPLETED download must not re-download; a STOPPED one must
    # stay stopped (stop() even deleted its .progress sidecar); an ERROR one
    # stays failed until the user explicitly retries. Only PENDING/QUEUED
    # (incl. formerly-active jobs mapped to PENDING on save) actually re-enter
    # the queue.
    _RESTORE_AS_IS = {Status.COMPLETED, Status.STOPPED, Status.ERROR, Status.PAUSED}

    def load_downloads(self):
        for record in self.session_store.load():
            # Restore with the ORIGINAL id so the .progress sidecar and any
            # external controllers still line up.
            self.add_download(
                url=record.url, save_path=record.save_path, checksum=record.checksum,
                num_threads=record.num_threads, headers=record.headers, category=record.category,
                speed_limit_bps=record.speed_limit_bps, scheduled_time=record.scheduled_time,
                start_immediately=False, download_id=record.download_id,
                verify_tls=record.verify_tls,
            )
            manager = self.downloads.get(record.download_id)
            if manager is None:
                continue
            # Restore the recovery-policy fields so the job's legal operations
            # after restart match what they were before the crash/close:
            # whether ADP owns the file, whether a retry must restart from
            # scratch, and any explicit overwrite authorization.
            manager.destination_owned_by_adp = record.destination_owned_by_adp
            manager.restart_required = record.restart_required
            manager.allow_overwrite = record.allow_overwrite
            try:
                saved_status = Status[record.status]
            except (KeyError, TypeError):
                saved_status = Status.PENDING
            if saved_status in self._RESTORE_AS_IS:
                # Pull it out of the queue (add_download enqueues unscheduled
                # jobs) and pin it to its saved terminal/paused status so
                # process_queue() below won't start it. For terminal states we
                # also restore the persisted byte figures so the row reads
                # correctly without re-fetching anything.
                if manager in self.download_queue:
                    self.download_queue.remove(manager)
                if saved_status in (Status.COMPLETED, Status.ERROR):
                    manager.total_size = record.total_size or 0
                    manager.downloaded_size = record.downloaded_size or 0
                manager.set_status(saved_status)
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


