# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import logging
import os
import time
import uuid

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QListWidget,
    QListWidgetItem, QLabel, QComboBox, QMenu, QApplication, QMessageBox,
    QAbstractItemView, QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence

from adp.torrent.engine import TorrentEngine
from adp.torrent.models import TorrentRecord, TorrentState, FilePriority
from adp.torrent.session_store import TorrentSessionStore
from adp.gui.torrent_widgets import TorrentItemWidget
from adp.gui.torrent_dialogs import AddTorrentDialog, SelectFilesDialog, TorrentPropertiesDialog

from adp.core.storage import check_space, same_volume
from adp.utils.url_utils import sanitize_filename

logger = logging.getLogger(__name__)

ALL_CATEGORIES_FILTER = "All Categories"
RESUME_DATA_WAIT_SECONDS = 4.0


class TorrentPanel(QWidget):
    status_update_requested = pyqtSignal(str, int)
    torrent_completed = pyqtSignal(str, str)  # torrent_id, name -- for tray notifications

    def __init__(self, parent=None, state_dir=None, listen_port=6881, enable_dht=True,
                 default_seed_ratio_limit=0.0, default_save_path=None,
                 unavailable_path=None, stop_on_complete=True, engine_options=None,
                 max_active_downloads=3, move_completed_dir=None,
                 move_completed_by_category=False):
        super().__init__(parent)
        state_dir = state_dir or os.getcwd()
        self.state_dir = state_dir
        self.default_seed_ratio_limit = default_seed_ratio_limit
        # Global "stop when download completes" policy (Settings). Enforced on
        # libtorrent's torrent_finished_alert -- the real transition into the
        # finished state (after downloading, after a recheck, or when a
        # complete torrent is restored) -- rather than by polling, so it never
        # races a recheck the user just started.
        self.stop_on_complete = stop_on_complete
        # Move-on-complete (Settings). None = off. Only torrents seen
        # actually downloading this session are auto-moved, so enabling the
        # setting (or restarting with it on) never relocates old torrents.
        self.move_completed_dir = move_completed_dir or None
        self.move_completed_by_category = bool(move_completed_by_category)
        self._seen_downloading = set()
        self._moves_pending = {}  # torrent_id -> destination folder
        self.session_store = TorrentSessionStore(state_dir)
        # Fail-closed storage: if the user configured a torrent folder that is
        # currently unavailable (e.g. an unplugged drive), we do NOT silently
        # redirect downloads to the system drive. Instead the tab opens in a
        # "storage unavailable" state -- Add is blocked and the UI names the
        # missing path -- so the user's storage decision is preserved. They fix
        # it in Settings (or reconnect the drive) and it recovers.
        self._unavailable_path = unavailable_path
        # Where torrents land by default: caller-provided (the user's
        # configured torrent_download_dir) when given, else the historical
        # app-data location. Settable at runtime via set_default_save_path
        # so a Settings change applies without a restart.
        if unavailable_path:
            self.default_save_path = None
        else:
            self.default_save_path = default_save_path or os.path.join(state_dir, "torrent_downloads")
            os.makedirs(self.default_save_path, exist_ok=True)

        # engine_options: extra TorrentEngine kwargs (bind_address,
        # enable_lsd/upnp/natpmp, extra_settings) -- used by tests to keep
        # the session fully offline.
        self.engine = TorrentEngine(listen_port=listen_port, enable_dht=enable_dht,
                                    max_active_downloads=max_active_downloads,
                                    **(engine_options or {}))
        self.records = {}  # torrent_id -> TorrentRecord

        layout = QVBoxLayout(self)
        controls_layout = QHBoxLayout()

        add_magnet_button = QPushButton("Add Magnet/Torrent")
        add_magnet_button.clicked.connect(self.add_torrent_from_dialog)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search torrents...")
        self.search_input.textChanged.connect(self.apply_filters)

        self.category_filter = QComboBox()
        self.category_filter.addItem(ALL_CATEGORIES_FILTER)
        self.category_filter.currentIndexChanged.connect(self.apply_filters)

        controls_layout.addWidget(add_magnet_button)
        controls_layout.addWidget(self.search_input)
        controls_layout.addWidget(self.category_filter)
        controls_layout.addStretch()
        layout.addLayout(controls_layout)

        self.torrent_list = QListWidget()
        # Ctrl/Shift-click (and Ctrl+A) multi-select; actions apply to all
        # selected torrents.
        self.torrent_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.torrent_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.torrent_list.customContextMenuRequested.connect(self.show_context_menu)
        self.torrent_list.itemDoubleClicked.connect(lambda _item: self.show_properties_for_selected())
        layout.addWidget(self.torrent_list)

        self.engine.progress_updated.connect(self.on_progress_updated)
        self.engine.metadata_received.connect(self.on_metadata_received)
        self.engine.torrent_finished.connect(self.on_torrent_finished)
        self.engine.torrent_error.connect(self.on_torrent_error)

        self._pending_resume_data = {}
        self.engine.resume_data_saved.connect(self._on_resume_data_saved)
        self.engine.download_started.connect(self._seen_downloading.add)
        self.engine.storage_moved.connect(self._on_storage_moved)
        self.engine.storage_move_failed.connect(self._on_storage_move_failed)

        self.create_actions()
        self.engine.start()
        self.load_torrents()

    # -- actions / context menu ------------------------------------------
    def create_actions(self):
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pause_selected)
        self.resume_action = QAction("Resume", self)
        self.resume_action.triggered.connect(self.resume_selected)
        self.start_now_action = QAction("Start Now (skip queue)", self)
        self.start_now_action.setToolTip("Start downloading immediately, ignoring the active-downloads limit.")
        self.start_now_action.triggered.connect(self.start_now_selected)
        self.start_seeding_action = QAction("Start Seeding", self)
        self.start_seeding_action.triggered.connect(self.start_seeding_selected)
        self.remove_action = QAction("Remove (keep files)", self)
        self.remove_action.setShortcut(QKeySequence.StandardKey.Delete)
        self.remove_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.remove_action.triggered.connect(lambda: self.remove_selected(delete_files=False))
        self.torrent_list.addAction(self.remove_action)  # Delete key works without the menu
        self.remove_delete_action = QAction("Remove and Delete Files...", self)
        self.remove_delete_action.triggered.connect(lambda: self.remove_selected(delete_files=True))
        self.remove_completed_action = QAction("Remove All Completed (keep files)", self)
        self.remove_completed_action.triggered.connect(self.remove_completed)
        self.force_recheck_action = QAction("Force Recheck", self)
        self.force_recheck_action.triggered.connect(self.force_recheck_selected)
        self.select_files_action = QAction("Select Files...", self)
        self.select_files_action.triggered.connect(self.select_files_for_selected)
        self.move_files_action = QAction("Move Files...", self)
        self.move_files_action.triggered.connect(self.move_selected_to_folder)
        self.open_folder_action = QAction("Open Folder", self)
        self.open_folder_action.triggered.connect(self.open_folder_for_selected)
        self.properties_action = QAction("Properties...", self)
        self.properties_action.triggered.connect(self.show_properties_for_selected)

    def show_context_menu(self, position):
        item = self.torrent_list.itemAt(position)
        if not item:
            return
        # Right-clicking inside the current selection keeps it (so bulk
        # actions apply); right-clicking elsewhere selects just that row.
        if not item.isSelected():
            self.torrent_list.setCurrentItem(item)
        torrent_ids = self.get_selected_torrent_ids()
        statuses = self._statuses(torrent_ids)
        if not statuses:
            return

        n_queued = sum(1 for st in statuses.values() if self.engine.is_queued(st))
        # "Pause" covers running AND queued torrents (pausing a queued one
        # takes it out of the queue); "Resume" only user-paused ones.
        n_active = sum(1 for st in statuses.values() if not st.paused) + n_queued
        n_paused_incomplete = sum(1 for st in statuses.values()
                                  if st.paused and not st.is_finished and not self.engine.is_queued(st))
        n_paused_complete = sum(1 for st in statuses.values() if st.paused and st.is_finished)
        all_finished = all(st.is_finished for st in statuses.values())
        multi = len(statuses) > 1
        suffix = f" ({len(statuses)} torrents)" if multi else ""

        menu = QMenu(self)
        if n_active:
            self.pause_action.setText(("Stop Seeding" if all_finished else "Pause")
                                      + (f" ({n_active})" if multi else ""))
            menu.addAction(self.pause_action)
        if n_queued:
            self.start_now_action.setText(
                "Start Now (skip queue)" + (f" ({n_queued})" if multi else ""))
            menu.addAction(self.start_now_action)
        if n_paused_incomplete:
            self.resume_action.setText("Resume" + (f" ({n_paused_incomplete})" if multi else ""))
            menu.addAction(self.resume_action)
        if n_paused_complete:
            self.start_seeding_action.setText(
                "Start Seeding" + (f" ({n_paused_complete})" if multi else ""))
            menu.addAction(self.start_seeding_action)
        if not multi:
            menu.addAction(self.select_files_action)
        self.force_recheck_action.setText("Force Recheck" + suffix)
        menu.addAction(self.force_recheck_action)
        self.move_files_action.setText("Move Files..." + (f" ({len(statuses)} torrents)" if multi else ""))
        menu.addAction(self.move_files_action)
        if not multi:
            menu.addAction(self.open_folder_action)
        menu.addSeparator()
        self.remove_action.setText("Remove (keep files)" + suffix)
        self.remove_delete_action.setText("Remove and Delete Files" + suffix + "...")
        menu.addAction(self.remove_action)
        menu.addAction(self.remove_delete_action)
        if not multi:
            menu.addSeparator()
            menu.addAction(self.properties_action)
        menu.addSeparator()
        self.remove_completed_action.setEnabled(any(
            h.is_valid() and h.status().is_finished for h in self.engine.handles.values()))
        menu.addAction(self.remove_completed_action)
        menu.exec(self.torrent_list.mapToGlobal(position))

    def _statuses(self, torrent_ids):
        out = {}
        for torrent_id in torrent_ids:
            handle = self.engine.handles.get(torrent_id)
            if handle is not None and handle.is_valid():
                out[torrent_id] = handle.status()
        return out

    # -- add ---------------------------------------------------------------
    def add_torrent_from_dialog(self):
        dialog = AddTorrentDialog(
            self, default_save_path=self.default_save_path,
            default_seed_ratio_limit=self.default_seed_ratio_limit,
        )
        if dialog.exec():
            data = dialog.get_data()
            self.add_torrent(**data)

    def add_torrent(self, mode, torrent_file_path=None, magnet_uri=None, save_path=None,
                     category="Torrents", file_priorities=None, download_limit_bps=0,
                     upload_limit_bps=0, seed_ratio_limit=0.0):
        if self._unavailable_path is not None and not save_path:
            self.status_update_requested.emit(
                f"Torrent download folder '{self._unavailable_path}' is unavailable. "
                "Reconnect it or choose another location in Settings.", 8000)
            return None
        save_path = save_path or self.default_save_path
        os.makedirs(save_path, exist_ok=True)

        if mode == "file":
            if not torrent_file_path or not os.path.exists(torrent_file_path):
                self.status_update_requested.emit("Could not add torrent: file not found.", 6000)
                return None
            torrent_id = self.engine.add_torrent_file(torrent_file_path, save_path, file_priorities=file_priorities)
            stored_copy = self.session_store.store_torrent_file_copy(torrent_id, torrent_file_path)
            record = TorrentRecord(
                # The torrent's own name ("ubuntu.iso"), not the .torrent
                # file's ("ubuntu.iso.torrent"); file name only as a fallback.
                torrent_id=torrent_id,
                name=self.engine.known_names.get(torrent_id) or os.path.basename(torrent_file_path),
                save_path=save_path,
                category=category, source_torrent_file=stored_copy,
                file_priorities=file_priorities or {}, upload_limit_bps=upload_limit_bps,
                download_limit_bps=download_limit_bps, seed_ratio_limit=seed_ratio_limit,
            )
            display_name = record.name
        else:
            if not magnet_uri:
                self.status_update_requested.emit("Could not add torrent: no magnet link given.", 6000)
                return None
            torrent_id = self.engine.add_magnet(magnet_uri, save_path)
            record = TorrentRecord(
                torrent_id=torrent_id, name=self.engine.known_names.get(torrent_id, "Fetching metadata..."),
                save_path=save_path, category=category, source_magnet=magnet_uri,
                upload_limit_bps=upload_limit_bps, download_limit_bps=download_limit_bps,
                seed_ratio_limit=seed_ratio_limit,
            )
            display_name = record.name

        if torrent_id in self.records:
            # The engine recognized this info-hash as already in the session
            # (duplicate magnet/.torrent add). Without this guard we'd create
            # a second list row and record both pointing at the same handle.
            self.status_update_requested.emit(
                f"Torrent is already in the list: {self.records[torrent_id].name}", 6000)
            return torrent_id

        if download_limit_bps or upload_limit_bps:
            self.engine.set_speed_limits(torrent_id, download_limit_bps, upload_limit_bps)

        self.records[torrent_id] = record
        self._create_list_item(torrent_id, display_name, category)
        self._register_category(category)
        self.apply_filters()
        return torrent_id

    def _create_list_item(self, torrent_id, name, category):
        widget = TorrentItemWidget(torrent_id, name, category=category)
        item = QListWidgetItem(self.torrent_list)
        item.setSizeHint(widget.sizeHint())
        item.setData(Qt.ItemDataRole.UserRole, torrent_id)
        self.torrent_list.addItem(item)
        self.torrent_list.setItemWidget(item, widget)
        return widget

    def _register_category(self, category):
        if self.category_filter.findText(category) < 0:
            self.category_filter.addItem(category)

    # -- engine signal handlers -------------------------------------------
    def on_progress_updated(self, torrent_id, status):
        widget = self.find_widget(torrent_id)
        if widget:
            widget.update_status(status)
        self._enforce_seed_ratio_limit(torrent_id, status)
        if status.get("state") in (TorrentState.DOWNLOADING, TorrentState.DOWNLOADING_METADATA):
            self._seen_downloading.add(torrent_id)

    def _enforce_seed_ratio_limit(self, torrent_id, status):
        record = self.records.get(torrent_id)
        if not record or record.seed_ratio_limit <= 0:
            return  # 0 == seed indefinitely, the default
        if not status.get("is_seeding"):
            return
        if status.get("ratio", 0) < record.seed_ratio_limit:
            return
        handle = self.engine.handles.get(torrent_id)
        if handle is not None and not handle.status().paused:
            self.engine.pause(torrent_id)
            self.status_update_requested.emit(
                f"Reached seed ratio {record.seed_ratio_limit:.2f} -- stopped seeding: {record.name}", 6000
            )
            logger.info(f"[{torrent_id}] Seed ratio limit {record.seed_ratio_limit:.2f} reached "
                        f"(actual: {status.get('ratio', 0):.2f}); auto-paused.")

    def set_default_save_path(self, path: str):
        """Applies a new default location for future torrent adds. Existing
        torrents keep the save path they were added with. Also clears the
        storage-unavailable state -- setting a usable path is how the user (or
        a reconnected drive via a Settings re-apply) recovers the tab."""
        os.makedirs(path, exist_ok=True)
        self.default_save_path = path
        self._unavailable_path = None

    def on_metadata_received(self, torrent_id, name, total_size, files):
        record = self.records.get(torrent_id)
        if record:
            record.name = name
        widget = self.find_widget(torrent_id)
        if widget:
            widget.set_name(name)
        logger.info(f"[{torrent_id}] Metadata resolved in GUI: {name} ({total_size} bytes)")
        self._enforce_free_space(torrent_id, name, total_size)

    def _enforce_free_space(self, torrent_id, name, total_size):
        """Metadata time is the first moment a magnet's size is known -- and
        the last moment before the engine starts writing. If the torrent
        won't fit (with headroom, so the drive is never filled to the brim),
        pause it now instead of letting it die mid-write with a disk-full
        error after gigabytes of wasted transfer. Pausing rather than
        removing keeps the decision with the user: free some space or pick
        a new location, then Resume -- which deliberately is NOT re-checked,
        so resuming doubles as an explicit override."""
        record = self.records.get(torrent_id)
        target = record.save_path if record else self.default_save_path
        result = check_space(target, int(total_size))
        if result.fits:
            return
        try:
            self.engine.pause(torrent_id)
        except Exception:  # noqa: BLE001 -- guard must never crash the metadata path
            logger.exception(f"[{torrent_id}] Failed to pause after space check")
        logger.warning(f"[{torrent_id}] Paused for insufficient disk space at {target}: "
                       f"{name} {result.message}")
        self.status_update_requested.emit(
            f"Paused '{name}' -- not enough disk space at {target} ({result.message}). "
            f"Free space or change the folder in Settings, then Resume.", 12000)

    def on_torrent_finished(self, torrent_id, name):
        self.status_update_requested.emit(f"Torrent finished: {name}", 5000)
        self.torrent_completed.emit(torrent_id, name)
        self._enforce_stop_on_complete(torrent_id)
        self._maybe_move_completed(torrent_id)

    # -- moving files ----------------------------------------------------------
    def set_move_completed(self, folder, by_category=False):
        """Applies the Settings values live. folder None/'' = off."""
        self.move_completed_dir = folder or None
        self.move_completed_by_category = bool(by_category)

    def _completed_destination(self, record) -> str:
        if not self.move_completed_by_category:
            return self.move_completed_dir
        return os.path.join(self.move_completed_dir,
                            sanitize_filename(record.category, fallback="Torrents"))

    def _maybe_move_completed(self, torrent_id):
        """Auto-move on genuine completion only: the torrent must have been
        seen downloading in this session. A torrent restored already
        complete, or finishing a recheck without downloading, stays put."""
        if not self.move_completed_dir or torrent_id not in self._seen_downloading:
            return
        self._seen_downloading.discard(torrent_id)
        record = self.records.get(torrent_id)
        if record is not None:
            self.move_torrents([torrent_id], self._completed_destination(record))

    def move_selected_to_folder(self):
        torrent_ids = self.get_selected_torrent_ids()
        if not torrent_ids:
            return
        first = self.records.get(torrent_ids[0])
        start = self.move_completed_dir or (first.save_path if first else "") or os.path.expanduser("~")
        destination = QFileDialog.getExistingDirectory(self, "Move Files To", start)
        if destination:
            self.move_torrents(torrent_ids, destination)

    def move_torrents(self, torrent_ids, destination) -> int:
        """Starts moving each torrent's files into `destination`. Checks
        first, so a move that can't work is refused up front with the files
        left in place: the folder must be creatable, and a move to another
        drive must fit (with headroom). libtorrent does the move itself
        (rename on the same drive, copy-then-delete across drives) and
        never overwrites existing files. Returns how many moves started."""
        destination = os.path.abspath(os.path.expanduser(destination))
        started = 0
        for torrent_id in torrent_ids:
            record = self.records.get(torrent_id)
            handle = self.engine.handles.get(torrent_id)
            if record is None or handle is None or not handle.is_valid():
                continue
            if torrent_id in self._moves_pending:
                continue  # already moving
            if os.path.normcase(os.path.abspath(record.save_path)) == os.path.normcase(destination):
                continue  # already there
            try:
                os.makedirs(destination, exist_ok=True)
            except OSError as e:
                self.status_update_requested.emit(
                    f"Couldn't move '{record.name}': destination unavailable ({e}). Files left in place.", 10000)
                logger.warning(f"[{torrent_id}] Move refused, destination unavailable: {destination}: {e}")
                continue
            if same_volume(record.save_path, destination) is not True:
                needed = handle.status().total_wanted_done
                space = check_space(destination, needed)
                if not space.fits:
                    self.status_update_requested.emit(
                        f"Couldn't move '{record.name}' to {destination}: {space.message}. "
                        "Files left in place.", 12000)
                    logger.warning(f"[{torrent_id}] Move refused for space: {space.message}")
                    continue
            self._moves_pending[torrent_id] = destination
            self.engine.move_storage(torrent_id, destination)
            self.status_update_requested.emit(f"Moving '{record.name}' to {destination}...", 6000)
            logger.info(f"[{torrent_id}] Moving storage {record.save_path} -> {destination}")
            started += 1
        return started

    def _on_storage_moved(self, torrent_id, new_path):
        self._moves_pending.pop(torrent_id, None)
        record = self.records.get(torrent_id)
        if record is None:
            return
        record.save_path = new_path
        self.session_store.save_records(list(self.records.values()))
        self.engine.request_save_resume_data(torrent_id)  # resume data carries the path too
        self.status_update_requested.emit(f"Moved '{record.name}' to {new_path}", 6000)

    def _on_storage_move_failed(self, torrent_id, message):
        self._moves_pending.pop(torrent_id, None)
        record = self.records.get(torrent_id)
        name = record.name if record else torrent_id
        self.status_update_requested.emit(
            f"Couldn't move '{name}': {message}. Files left where they were.", 12000)

    def _enforce_stop_on_complete(self, torrent_id) -> bool:
        """Pauses a finished torrent instead of letting it seed, when the
        global setting is on and the user hasn't explicitly chosen to keep
        seeding this one. Returns True if it paused the torrent."""
        if not self.stop_on_complete:
            return False
        record = self.records.get(torrent_id)
        if record is None or record.keep_seeding:
            return False
        handle = self.engine.handles.get(torrent_id)
        if handle is None or not handle.is_valid():
            return False
        status = handle.status()
        if status.paused or not status.is_finished:
            return False
        self.engine.pause(torrent_id)
        # Persist promptly so a crash/kill can't bring it back seeding.
        self.engine.request_save_resume_data(torrent_id)
        self.status_update_requested.emit(f"Download complete -- stopped (not seeding): {record.name}", 6000)
        logger.info(f"[{torrent_id}] Download complete; stopped per 'stop on complete' setting.")
        return True

    def set_stop_on_complete(self, enabled: bool):
        """Applies the Settings toggle live. Turning it on also stops any
        torrent that is already sitting there seeding (unless the user
        deliberately resumed it to seed)."""
        self.stop_on_complete = bool(enabled)
        if not self.stop_on_complete:
            return
        stopped = sum(1 for tid in list(self.records) if self._enforce_stop_on_complete(tid))
        if stopped > 1:
            self.status_update_requested.emit(f"Stopped seeding {stopped} completed torrents.", 6000)

    def set_default_seed_ratio_limit(self, ratio: float):
        """Default ratio pre-filled in the Add Torrent dialog; applies live."""
        self.default_seed_ratio_limit = ratio or 0.0

    def on_torrent_error(self, torrent_id, message):
        self.status_update_requested.emit(f"Torrent error: {message}", 8000)

    def _on_resume_data_saved(self, torrent_id, data):
        self._pending_resume_data[torrent_id] = data

    # -- filtering ----------------------------------------------------------
    def apply_filters(self):
        query = self.search_input.text().strip().lower()
        category = self.category_filter.currentText()
        for i in range(self.torrent_list.count()):
            item = self.torrent_list.item(i)
            torrent_id = item.data(Qt.ItemDataRole.UserRole)
            record = self.records.get(torrent_id)
            if not record:
                continue
            matches_query = query in record.name.lower() if query else True
            matches_category = category == ALL_CATEGORIES_FILTER or record.category == category
            item.setHidden(not (matches_query and matches_category))

    # -- selection helpers ---------------------------------------------------
    def get_selected_torrent_id(self):
        """The single selected torrent (first, if several) -- for actions
        that only make sense on one, like Select Files / Open Folder."""
        ids = self.get_selected_torrent_ids()
        return ids[0] if ids else None

    def get_selected_torrent_ids(self):
        """All selected torrents, in list order, skipping filtered-out rows
        (Ctrl+A / shift-click could otherwise sweep up hidden ones)."""
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in sorted(self.torrent_list.selectedItems(), key=self.torrent_list.row)
            if not item.isHidden()
        ]

    def find_widget(self, torrent_id):
        for i in range(self.torrent_list.count()):
            item = self.torrent_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == torrent_id:
                return self.torrent_list.itemWidget(item)
        return None

    # -- per-torrent controls -----------------------------------------------
    def pause_selected(self):
        for torrent_id in self.get_selected_torrent_ids():
            self.engine.pause(torrent_id)

    def resume_selected(self):
        """Resumes selected torrents that are paused mid-download. Completed
        ones are left alone -- seeding them is the separate, explicit
        Start Seeding action."""
        for torrent_id, status in self._statuses(self.get_selected_torrent_ids()).items():
            if not status.is_finished:
                self.resume_torrent(torrent_id)

    def start_now_selected(self):
        """Force-starts selected queued torrents past the download limit."""
        for torrent_id, status in self._statuses(self.get_selected_torrent_ids()).items():
            if self.engine.is_queued(status):
                self.engine.start_now(torrent_id)

    def set_max_active_downloads(self, n: int):
        """Applies the Settings value live (0 = no limit)."""
        self.engine.set_max_active_downloads(n)

    def start_seeding_selected(self):
        for torrent_id, status in self._statuses(self.get_selected_torrent_ids()).items():
            if status.is_finished:
                self.resume_torrent(torrent_id)

    def resume_torrent(self, torrent_id):
        """Resumes a torrent. Resuming one that has already completed is an
        explicit request to seed it, so it's exempted from 'stop on
        complete' from now on (persisted on the record)."""
        handle = self.engine.handles.get(torrent_id)
        record = self.records.get(torrent_id)
        if handle is not None and record is not None and handle.is_valid() and handle.status().is_finished:
            record.keep_seeding = True
        self.engine.resume(torrent_id)

    def force_recheck_selected(self):
        torrent_ids = self.get_selected_torrent_ids()
        if not torrent_ids:
            return
        issued = [tid for tid in torrent_ids if self.engine.force_recheck(tid)]
        if not issued:
            self.status_update_requested.emit(
                "Couldn't recheck: torrent is no longer available.", 5000)
        elif len(issued) == 1:
            record = self.records.get(issued[0])
            name = record.name if record else issued[0]
            self.status_update_requested.emit(
                f"Rechecking '{name}' -- verifying files on disk...", 6000)
        else:
            self.status_update_requested.emit(
                f"Rechecking {len(issued)} torrents -- verifying files on disk...", 6000)

    def select_files_for_selected(self):
        torrent_id = self.get_selected_torrent_id()
        if not torrent_id:
            return
        entries = self.engine.get_file_list(torrent_id)
        if not entries:
            self.status_update_requested.emit(
                "File list isn't available yet (metadata still resolving?).", 5000
            )
            return
        dialog = SelectFilesDialog(self, entries=entries)
        if dialog.exec():
            priorities_by_value = dialog.get_priorities()
            priorities = {i: FilePriority(v) for i, v in priorities_by_value.items()}
            self.engine.set_file_priorities(torrent_id, priorities)
            record = self.records.get(torrent_id)
            if record:
                record.file_priorities = priorities_by_value

    def show_properties_for_selected(self):
        torrent_id = self.get_selected_torrent_id()
        record = self.records.get(torrent_id)
        if record is None:
            return
        categories = [self.category_filter.itemText(i) for i in range(self.category_filter.count())
                      if self.category_filter.itemText(i) != ALL_CATEGORIES_FILTER]
        dialog = TorrentPropertiesDialog(self, record=record, categories=categories)
        if dialog.exec():
            self.apply_properties(torrent_id, **dialog.get_data())

    def apply_properties(self, torrent_id, category, download_limit_bps, upload_limit_bps,
                         seed_ratio_limit, keep_seeding) -> bool:
        """Applies edited settings to a live torrent and persists them.
        Everything takes effect immediately: limits on the handle, the
        category on the row and filter, and the seeding policy -- turning
        'keep seeding' off stops a completed torrent that's seeding now (when
        'stop on complete' is on); a lowered ratio limit is enforced on the
        next status poll."""
        record = self.records.get(torrent_id)
        if record is None:
            return False
        record.category = category
        record.download_limit_bps = max(0, int(download_limit_bps))
        record.upload_limit_bps = max(0, int(upload_limit_bps))
        record.seed_ratio_limit = max(0.0, float(seed_ratio_limit))
        record.keep_seeding = bool(keep_seeding)

        self.engine.set_speed_limits(torrent_id, record.download_limit_bps, record.upload_limit_bps)
        widget = self.find_widget(torrent_id)
        if widget:
            widget.set_category(category)
        self._register_category(category)
        self.apply_filters()
        if not record.keep_seeding:
            self._enforce_stop_on_complete(torrent_id)

        self.session_store.save_records(list(self.records.values()))
        self.status_update_requested.emit(f"Updated properties: {record.name}", 4000)
        logger.info(f"[{torrent_id}] Properties updated: category={category!r}, "
                    f"down={record.download_limit_bps}, up={record.upload_limit_bps}, "
                    f"ratio={record.seed_ratio_limit}, keep_seeding={record.keep_seeding}")
        return True

    def open_folder_for_selected(self):
        torrent_id = self.get_selected_torrent_id()
        record = self.records.get(torrent_id)
        if record and os.path.exists(record.save_path):
            import subprocess
            import sys
            try:
                if sys.platform == "win32":
                    os.startfile(record.save_path)
                else:
                    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", record.save_path])
            except OSError as e:
                self.status_update_requested.emit(f"Could not open folder: {e}", 5000)

    def _confirm(self, title: str, text: str) -> bool:
        """Yes/No prompt defaulting to No. Kept as one method so tests (and
        any future 'don't ask again' setting) have a single seam."""
        choice = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def _describe(self, torrent_ids, limit=5) -> str:
        names = [self.records[t].name if t in self.records else t for t in torrent_ids]
        shown = "\n".join(f"  \u2022 {n}" for n in names[:limit])
        if len(names) > limit:
            shown += f"\n  \u2026and {len(names) - limit} more"
        return shown

    def remove_selected(self, delete_files=False):
        """Removes the selected torrent(s). Deleting files always asks first
        (it's irreversible); removing several at once also asks. Removing a
        single torrent while keeping its files stays one click."""
        torrent_ids = self.get_selected_torrent_ids()
        if not torrent_ids:
            return 0
        count = len(torrent_ids)
        noun = "this torrent" if count == 1 else f"these {count} torrents"
        if delete_files:
            if not self._confirm(
                    "Delete downloaded files?",
                    f"Remove {noun} AND permanently delete the downloaded files from disk?\n\n"
                    f"{self._describe(torrent_ids)}\n\nThis can't be undone."):
                return 0
        elif count > 1:
            if not self._confirm(
                    "Remove torrents?",
                    f"Remove {noun} from the list? Downloaded files are kept.\n\n"
                    f"{self._describe(torrent_ids)}"):
                return 0
        for torrent_id in torrent_ids:
            self.remove_torrent(torrent_id, delete_files=delete_files)
        if count > 1:
            self.status_update_requested.emit(
                f"Removed {count} torrents" + (" and deleted their files." if delete_files else "."), 5000)
        return count

    def remove_completed(self):
        """Clears every completed torrent from the list (whatever the filter
        shows), keeping the files. Asks first, listing what will go."""
        torrent_ids = [tid for tid, st in self._statuses(list(self.records)).items() if st.is_finished]
        if not torrent_ids:
            self.status_update_requested.emit("No completed torrents to remove.", 4000)
            return 0
        if not self._confirm(
                "Remove completed torrents?",
                f"Remove {len(torrent_ids)} completed torrent(s) from the list? "
                f"Downloaded files are kept.\n\n{self._describe(torrent_ids)}"):
            return 0
        for torrent_id in torrent_ids:
            self.remove_torrent(torrent_id, delete_files=False)
        self.status_update_requested.emit(f"Removed {len(torrent_ids)} completed torrent(s).", 5000)
        return len(torrent_ids)

    def remove_torrent(self, torrent_id, delete_files=False):
        """Removes one torrent: engine handle, record, resume data, list row.
        No prompting -- callers (GUI actions) confirm first; the API calls
        this directly."""
        self.engine.remove(torrent_id, delete_files=delete_files)
        self.records.pop(torrent_id, None)
        self.session_store.delete_resume_data(torrent_id)
        for i in range(self.torrent_list.count()):
            item = self.torrent_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == torrent_id:
                self.torrent_list.takeItem(i)
                break

    # -- persistence -----------------------------------------------------------
    def load_torrents(self):
        for record in self.session_store.load_records():
            resume_data = self.session_store.load_resume_data(record.torrent_id)
            try:
                if resume_data:
                    torrent_id = self.engine.restore_torrent(resume_data, record.source_torrent_file)
                elif record.source_torrent_file and os.path.exists(record.source_torrent_file):
                    file_priorities = {int(k): v for k, v in record.file_priorities.items()}
                    torrent_id = self.engine.add_torrent_file(
                        record.source_torrent_file, record.save_path, file_priorities=file_priorities
                    )
                elif record.source_magnet:
                    torrent_id = self.engine.add_magnet(record.source_magnet, record.save_path)
                else:
                    logger.warning(f"Skipping torrent record with no resume data, file, or magnet: {record.name}")
                    continue
            except Exception as e:
                logger.error(f"Failed to restore torrent '{record.name}': {e}", exc_info=True)
                continue

            record.torrent_id = torrent_id
            self.records[torrent_id] = record
            self._create_list_item(torrent_id, record.name, record.category)
            self._register_category(record.category)
            if record.download_limit_bps or record.upload_limit_bps:
                self.engine.set_speed_limits(torrent_id, record.download_limit_bps, record.upload_limit_bps)
        self.apply_filters()

    def save_session(self, wait_for_resume_data=False):
        """Persists our own metadata immediately, plus (optionally) blocks
        briefly pumping the event loop to collect libtorrent's own async
        resume-data blobs -- used on app shutdown so a restart resumes
        quickly instead of doing a full recheck."""
        torrent_ids = list(self.engine.handles.keys())
        self._pending_resume_data.clear()
        self.engine.request_save_all_resume_data()

        if wait_for_resume_data and torrent_ids:
            app = QApplication.instance()
            deadline = time.time() + RESUME_DATA_WAIT_SECONDS
            while time.time() < deadline and len(self._pending_resume_data) < len(torrent_ids):
                if app is not None:
                    app.processEvents()
                time.sleep(0.02)

        for torrent_id, data in self._pending_resume_data.items():
            self.session_store.save_resume_data(torrent_id, data)

        self.session_store.save_records(list(self.records.values()))

    def closeEvent(self, event):
        self.save_session(wait_for_resume_data=True)
        self.engine.stop()
        super().closeEvent(event)
