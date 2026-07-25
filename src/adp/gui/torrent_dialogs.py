import os

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QFileDialog, QDialogButtonBox, QComboBox, QListWidget,
    QListWidgetItem, QRadioButton, QButtonGroup, QWidget,
)
from PyQt6.QtCore import Qt

from adp.core.models import CATEGORY_RULES, DEFAULT_CATEGORY, category_for_filename
from adp.torrent.engine import TorrentEngine
from adp.torrent.models import FilePriority
from adp.utils.format import format_size, parse_size_to_bytes
from adp.utils.url_utils import is_probably_url

ALL_CATEGORIES = ["Torrents"] + list(CATEGORY_RULES.keys()) + [DEFAULT_CATEGORY]


def _is_magnet_uri(text: str) -> bool:
    return (text or "").strip().lower().startswith("magnet:?")


class FileSelectionWidget(QWidget):
    """A checkable file list, shared by the add-torrent dialog (previewing a
    .torrent before it's added) and the post-add 'Select Files' dialog
    (adjusting an already-added torrent, including one resolved from a
    magnet link)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        self.select_all_button = QPushButton("Select All")
        self.select_all_button.setObjectName("secondary")
        self.select_none_button = QPushButton("Select None")
        self.select_none_button.setObjectName("secondary")
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        self.select_none_button.clicked.connect(lambda: self._set_all(False))
        header.addWidget(QLabel("Files:"))
        header.addStretch()
        header.addWidget(self.select_all_button)
        header.addWidget(self.select_none_button)

        self.list_widget = QListWidget()
        layout.addLayout(header)
        layout.addWidget(self.list_widget)

        self._entries = []

    def set_entries(self, entries):
        self._entries = entries
        self.list_widget.clear()
        for entry in entries:
            item = QListWidgetItem(f"{entry.path}  ({format_size(entry.size)})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked if entry.priority == FilePriority.SKIP else Qt.CheckState.Checked
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.index)
            self.list_widget.addItem(item)

    def _set_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(state)

    def get_priorities(self) -> dict:
        """Returns {file_index: lt_priority_int} for every listed file
        (0 = skip, 4 = normal), suitable for engine.add_torrent_file's
        file_priorities kwarg or engine.set_file_priorities."""
        priorities = {}
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            index = item.data(Qt.ItemDataRole.UserRole)
            checked = item.checkState() == Qt.CheckState.Checked
            priorities[index] = FilePriority.NORMAL.value if checked else FilePriority.SKIP.value
        return priorities

    def largest_selected_filename(self) -> str:
        """Best-effort guess at the 'main' file, for category auto-detection."""
        best = None
        for entry in self._entries:
            if entry.priority == FilePriority.SKIP:
                continue
            if best is None or entry.size > best.size:
                best = entry
        return os.path.basename(best.path) if best else ""


class AddTorrentDialog(QDialog):
    """Add a torrent via magnet link or a .torrent file, with an embedded
    file-selection tree that populates once a .torrent file is parsed."""

    def __init__(self, parent=None, default_save_path=None, default_seed_ratio_limit=0.0):
        super().__init__(parent)
        self.setWindowTitle("Add Torrent")
        self.setMinimumSize(520, 480)

        layout = QVBoxLayout(self)

        mode_row = QHBoxLayout()
        self.magnet_radio = QRadioButton("Magnet Link")
        self.file_radio = QRadioButton(".torrent File")
        self.magnet_radio.setChecked(True)
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.magnet_radio)
        mode_group.addButton(self.file_radio)
        mode_row.addWidget(self.magnet_radio)
        mode_row.addWidget(self.file_radio)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        form = QFormLayout()
        self.magnet_input = QLineEdit()
        self.magnet_input.setPlaceholderText("magnet:?xt=urn:btih:...")
        self.magnet_input.textChanged.connect(self._on_magnet_changed)
        form.addRow("Magnet URI:", self.magnet_input)

        file_row = QHBoxLayout()
        self.file_path_input = QLineEdit()
        self.file_path_input.setReadOnly(True)
        self.file_path_input.setPlaceholderText("Select a .torrent file...")
        browse_button = QPushButton("Browse...")
        browse_button.setObjectName("secondary")
        browse_button.clicked.connect(self.browse_torrent_file)
        file_row.addWidget(self.file_path_input)
        file_row.addWidget(browse_button)
        form.addRow(".torrent File:", file_row)

        save_row = QHBoxLayout()
        self.save_path_input = QLineEdit(default_save_path or os.getcwd())
        save_browse_button = QPushButton("Browse...")
        save_browse_button.setObjectName("secondary")
        save_browse_button.clicked.connect(self.browse_save_path)
        save_row.addWidget(self.save_path_input)
        save_row.addWidget(save_browse_button)
        form.addRow("Save To:", save_row)

        self.category_input = QComboBox()
        self.category_input.addItems(ALL_CATEGORIES)
        form.addRow("Category:", self.category_input)

        self.download_limit_input = QLineEdit()
        self.download_limit_input.setPlaceholderText("e.g. 2 MB (blank = unlimited)")
        self.upload_limit_input = QLineEdit()
        self.upload_limit_input.setPlaceholderText("e.g. 500 KB (blank = unlimited)")
        form.addRow("Download Limit:", self.download_limit_input)
        form.addRow("Upload Limit:", self.upload_limit_input)

        self.seed_ratio_input = QLineEdit()
        self.seed_ratio_input.setPlaceholderText("e.g. 2.0 (blank = seed indefinitely)")
        if default_seed_ratio_limit:
            self.seed_ratio_input.setText(str(default_seed_ratio_limit))
        form.addRow("Seed Ratio Limit:", self.seed_ratio_input)

        layout.addLayout(form)

        self.info_label = QLabel("")
        layout.addWidget(self.info_label)

        self.file_selection = FileSelectionWidget()
        layout.addWidget(self.file_selection)
        self.file_selection.setVisible(False)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._error = None
        self._preview_entries = []

    def _on_magnet_changed(self, text):
        if text:
            self.file_radio.setChecked(False)
            self.magnet_radio.setChecked(True)
            self.file_selection.setVisible(False)
            self.file_path_input.clear()

    def browse_torrent_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select .torrent File", "", "Torrent Files (*.torrent)")
        if not path:
            return
        self.file_radio.setChecked(True)
        self.magnet_input.clear()
        self.file_path_input.setText(path)
        try:
            self._preview_entries = TorrentEngine.preview_torrent_file(path)
            self.file_selection.set_entries(self._preview_entries)
            self.file_selection.setVisible(True)
            total = sum(e.size for e in self._preview_entries)
            self.info_label.setText(f"{len(self._preview_entries)} file(s), {format_size(total)} total")
            guessed = category_for_filename(self.file_selection.largest_selected_filename())
            idx = self.category_input.findText(guessed)
            if idx >= 0:
                self.category_input.setCurrentIndex(idx)
        except Exception as e:
            self.info_label.setText(f"Could not read .torrent file: {e}")
            self.file_selection.setVisible(False)

    def browse_save_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Save Location", self.save_path_input.text())
        if path:
            self.save_path_input.setText(path)

    def _on_accept(self):
        self._error = None
        if self.file_radio.isChecked():
            if not self.file_path_input.text() or not os.path.exists(self.file_path_input.text()):
                self.info_label.setText("Please choose a valid .torrent file.")
                self._error = "invalid_torrent_file"
                return
        else:
            if not _is_magnet_uri(self.magnet_input.text()):
                self.info_label.setText("That doesn't look like a valid magnet link (should start with 'magnet:?').")
                self._error = "invalid_magnet"
                return

        for field, label in [(self.download_limit_input, "download limit"), (self.upload_limit_input, "upload limit")]:
            try:
                parse_size_to_bytes(field.text())
            except ValueError:
                self.info_label.setText(f"Could not parse the {label} (e.g. '500 KB', '2 MB').")
                self._error = "invalid_speed_limit"
                return

        if self.seed_ratio_input.text().strip():
            try:
                float(self.seed_ratio_input.text().strip())
            except ValueError:
                self.info_label.setText("Seed ratio limit should be a number, e.g. '2.0'.")
                self._error = "invalid_ratio"
                return

        self.accept()

    def get_data(self) -> dict:
        download_limit = 0
        upload_limit = 0
        try:
            download_limit = parse_size_to_bytes(self.download_limit_input.text())
            upload_limit = parse_size_to_bytes(self.upload_limit_input.text())
        except ValueError:
            pass

        seed_ratio_limit = 0.0
        if self.seed_ratio_input.text().strip():
            try:
                seed_ratio_limit = float(self.seed_ratio_input.text().strip())
            except ValueError:
                pass

        is_file_mode = self.file_radio.isChecked()
        return {
            "mode": "file" if is_file_mode else "magnet",
            "torrent_file_path": self.file_path_input.text() if is_file_mode else None,
            "magnet_uri": self.magnet_input.text().strip() if not is_file_mode else None,
            "save_path": self.save_path_input.text(),
            "category": self.category_input.currentText(),
            "file_priorities": self.file_selection.get_priorities() if is_file_mode else {},
            "download_limit_bps": download_limit,
            "upload_limit_bps": upload_limit,
            "seed_ratio_limit": seed_ratio_limit,
        }


class SelectFilesDialog(QDialog):
    """Post-add file selection/adjustment for an already-running torrent."""

    def __init__(self, parent=None, entries=None):
        super().__init__(parent)
        self.setWindowTitle("Select Files")
        self.setMinimumSize(480, 400)

        layout = QVBoxLayout(self)
        self.file_selection = FileSelectionWidget()
        self.file_selection.set_entries(entries or [])
        layout.addWidget(self.file_selection)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_priorities(self) -> dict:
        return self.file_selection.get_priorities()
