from PyQt6.QtWidgets import QSystemTrayIcon, QMenu, QStyle
from PyQt6.QtGui import QIcon


class DownloaderTrayIcon(QSystemTrayIcon):
    """Wraps QSystemTrayIcon with the actions a downloader needs: show/hide
    the main window, quit, and a completion toast notification helper."""

    def __init__(self, main_window):
        icon = main_window.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown)
        super().__init__(icon, main_window)
        self.main_window = main_window
        self.setToolTip("Accelerated Downloader Pro")

        menu = QMenu()
        show_action = menu.addAction("Show")
        show_action.triggered.connect(self._show_window)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(main_window.quit_application)
        self.setContextMenu(menu)

        self.activated.connect(self._on_activated)

    def _show_window(self):
        self.main_window.showNormal()
        self.main_window.activateWindow()

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def notify(self, title: str, message: str, duration_ms: int = 5000):
        if self.isSystemTrayAvailable() and self.supportsMessages():
            self.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, duration_ms)
