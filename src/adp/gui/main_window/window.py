# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The application window: MainWindow hosts the Downloads/Torrents/Search/Stats
tabs, the toolbar, tray icon, theme switching, the local REST/MCP API server,
and Settings. create_app() builds the QApplication and the window.

The heavy Downloads-tab logic lives in download_panel.DownloadPanel; this
module is the chrome around it plus the torrent/search/stats/API wiring.
"""

import os
import sys
import subprocess
import logging

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel,
    QStatusBar, QToolBar, QMessageBox, QTabWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction

from adp.core.logging_setup import get_current_log_path
from adp.core.storage import resolve_dir, ConfiguredPathUnavailableError
from adp.gui.dialogs import SettingsDialog, ApiInfoDialog
from adp.gui.stats_panel import StatsPanel
from adp.gui.search_panel import SearchPanel
from adp.search.service import SearchService
from adp.api.auth import ApiKeyStore
from adp.api.bridge import GuiBridge
from adp.api.controller import AppController
from adp.api.rest_server import build_app, start_api_server_with_fallback
from adp.gui.theme import stylesheet_for
from adp.gui.tray import DownloaderTrayIcon
from adp.gui.main_window.download_panel import DownloadPanel

try:
    from adp.gui.torrent_panel import TorrentPanel
    TORRENT_SUPPORT_AVAILABLE = True
    _TORRENT_IMPORT_ERROR = None
except ImportError as e:
    TorrentPanel = None
    TORRENT_SUPPORT_AVAILABLE = False
    _TORRENT_IMPORT_ERROR = e

logger = logging.getLogger(__name__)


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
            torrent_dir = None
            unavailable_path = None
            try:
                torrent_dir = resolve_dir(
                    settings.get("torrent_download_dir"), default_torrent_dir)
            except ConfiguredPathUnavailableError as e:
                # Fail closed: the user configured a torrent folder that's
                # currently unavailable (e.g. unplugged drive). We do NOT
                # silently redirect downloads to the system drive -- that
                # would defeat storage.py's whole purpose. The tab opens in a
                # "storage unavailable" state instead: Add is blocked and the
                # UI names the missing path, so the user's storage decision
                # survives. They recover by reconnecting the drive or picking
                # another folder in Settings.
                logger.warning("Configured torrent folder unavailable at startup: %s", e)
                unavailable_path = e.configured
            self.torrent_panel = TorrentPanel(
                self, state_dir=state_dir,
                listen_port=settings.get("torrent_listen_port", 6881),
                enable_dht=settings.get("torrent_enable_dht", True),
                default_seed_ratio_limit=settings.get("torrent_default_seed_ratio_limit", 0.0),
                default_save_path=torrent_dir,
                unavailable_path=unavailable_path,
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
