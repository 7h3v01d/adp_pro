# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The application's main GUI.

Split out of a single main_window.py for maintainability; the public surface
is unchanged. Everything previously importable from `adp.gui.main_window`
(DownloadPanel, MainWindow, create_app) is re-exported here.

Layout:
- download_panel.py  DownloadPanel: the Downloads tab (queue, list, actions,
                     scheduling, clipboard, session persistence, settings)
- window.py          MainWindow + create_app: tabs, toolbar, tray, theme,
                     and the local REST/MCP API server
"""

from adp.gui.main_window.download_panel import DownloadPanel, ALL_CATEGORIES_FILTER
from adp.gui.main_window.window import (
    MainWindow,
    create_app,
    TORRENT_SUPPORT_AVAILABLE,
)

__all__ = [
    "DownloadPanel",
    "ALL_CATEGORIES_FILTER",
    "MainWindow",
    "create_app",
    "TORRENT_SUPPORT_AVAILABLE",
]
