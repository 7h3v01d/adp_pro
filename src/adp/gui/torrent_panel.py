import logging
import os
import time
import uuid

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QListWidget,
    QListWidgetItem, QLabel, QComboBox, QMenu, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction

from adp.torrent.engine import TorrentEngine
from adp.torrent.models import TorrentRecord, TorrentState, FilePriority
from adp.torrent.session_store import TorrentSessionStore
from adp.gui.torrent_widgets import TorrentItemWidget
from adp.gui.torrent_dialogs import AddTorrentDialog, SelectFilesDialog

from adp.core.storage import check_space

logger = logging.getLogger(__name__)

ALL_CATEGORIES_FILTER = "All Categories"
RESUME_DATA_WAIT_SECONDS = 4.0


class TorrentPanel(QWidget):
    status_update_requested = pyqtSignal(str, int)
    torrent_completed = pyqtSignal(str, str)  # torrent_id, name -- for tray notifications

    def __init__(self, parent=None, state_dir=None, listen_port=6881, enable_dht=True,
                 default_seed_ratio_limit=0.0, default_save_path=None):
        super().__init__(parent)
        state_dir = state_dir or os.getcwd()
        self.state_dir = state_dir
        self.default_seed_ratio_limit = default_seed_ratio_limit
        self.session_store = TorrentSessionStore(state_dir)
        # Where torrents land by default: caller-provided (the user's
        # configured torrent_download_dir) when given, else the historical
        # app-data location. Settable at runtime via set_default_save_path
        # so a Settings change applies without a restart.
        self.default_save_path = default_save_path or os.path.join(state_dir, "torrent_downloads")
        os.makedirs(self.default_save_path, exist_ok=True)

        self.engine = TorrentEngine(listen_port=listen_port, enable_dht=enable_dht)
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
        self.torrent_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.torrent_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.torrent_list)

        self.engine.progress_updated.connect(self.on_progress_updated)
        self.engine.metadata_received.connect(self.on_metadata_received)
        self.engine.torrent_finished.connect(self.on_torrent_finished)
        self.engine.torrent_error.connect(self.on_torrent_error)

        self._pending_resume_data = {}
        self.engine.resume_data_saved.connect(self._on_resume_data_saved)

        self.create_actions()
        self.engine.start()
        self.load_torrents()

    # -- actions / context menu ------------------------------------------
    def create_actions(self):
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self.pause_selected)
        self.resume_action = QAction("Resume", self)
        self.resume_action.triggered.connect(self.resume_selected)
        self.remove_action = QAction("Remove (keep files)", self)
        self.remove_action.triggered.connect(lambda: self.remove_selected(delete_files=False))
        self.remove_delete_action = QAction("Remove and Delete Files", self)
        self.remove_delete_action.triggered.connect(lambda: self.remove_selected(delete_files=True))
        self.force_recheck_action = QAction("Force Recheck", self)
        self.force_recheck_action.triggered.connect(self.force_recheck_selected)
        self.select_files_action = QAction("Select Files...", self)
        self.select_files_action.triggered.connect(self.select_files_for_selected)
        self.open_folder_action = QAction("Open Folder", self)
        self.open_folder_action.triggered.connect(self.open_folder_for_selected)

    def show_context_menu(self, position):
        item = self.torrent_list.itemAt(position)
        if not item:
            return
        self.torrent_list.setCurrentItem(item)
        torrent_id = item.data(Qt.ItemDataRole.UserRole)
        handle = self.engine.handles.get(torrent_id)
        if handle is None:
            return

        menu = QMenu(self)
        status = handle.status()
        if status.paused:
            menu.addAction(self.resume_action)
        elif status.is_finished or status.is_seeding:
            self.pause_action.setText("Stop Seeding")
            menu.addAction(self.pause_action)
        else:
            self.pause_action.setText("Pause")
            menu.addAction(self.pause_action)
        menu.addAction(self.select_files_action)
        menu.addAction(self.force_recheck_action)
        menu.addAction(self.open_folder_action)
        menu.addSeparator()
        menu.addAction(self.remove_action)
        menu.addAction(self.remove_delete_action)
        menu.exec(self.torrent_list.mapToGlobal(position))

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
        save_path = save_path or self.default_save_path
        os.makedirs(save_path, exist_ok=True)

        if mode == "file":
            if not torrent_file_path or not os.path.exists(torrent_file_path):
                self.status_update_requested.emit("Could not add torrent: file not found.", 6000)
                return None
            torrent_id = self.engine.add_torrent_file(torrent_file_path, save_path, file_priorities=file_priorities)
            stored_copy = self.session_store.store_torrent_file_copy(torrent_id, torrent_file_path)
            record = TorrentRecord(
                torrent_id=torrent_id, name=os.path.basename(torrent_file_path), save_path=save_path,
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
        torrents keep the save path they were added with."""
        os.makedirs(path, exist_ok=True)
        self.default_save_path = path

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
        selected = self.torrent_list.selectedItems()
        return selected[0].data(Qt.ItemDataRole.UserRole) if selected else None

    def find_widget(self, torrent_id):
        for i in range(self.torrent_list.count()):
            item = self.torrent_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == torrent_id:
                return self.torrent_list.itemWidget(item)
        return None

    # -- per-torrent controls -----------------------------------------------
    def pause_selected(self):
        torrent_id = self.get_selected_torrent_id()
        if torrent_id:
            self.engine.pause(torrent_id)

    def resume_selected(self):
        torrent_id = self.get_selected_torrent_id()
        if torrent_id:
            self.engine.resume(torrent_id)

    def force_recheck_selected(self):
        torrent_id = self.get_selected_torrent_id()
        if not torrent_id:
            return
        record = self.records.get(torrent_id)
        name = record.name if record else torrent_id
        if self.engine.force_recheck(torrent_id):
            self.status_update_requested.emit(
                f"Rechecking '{name}' -- verifying files on disk...", 6000)
        else:
            self.status_update_requested.emit(
                "Couldn't recheck: torrent is no longer available.", 5000)

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

    def remove_selected(self, delete_files=False):
        torrent_id = self.get_selected_torrent_id()
        if not torrent_id:
            return
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
