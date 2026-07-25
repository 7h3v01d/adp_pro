"""Dev-only tool: a browser shell with the real DownloadPanel docked beside
it, so you can click download links on real sites and watch them land in
Accelerated Downloader Pro. Not part of the pytest suite or the shipped app;
run directly with `python -m adp.dev.test_rig`.

Requires PyQt6-WebEngine, which isn't a runtime dependency of the app itself
-- install it separately if you want to use this tool:
    pip install PyQt6-WebEngine
"""
import sys

from PyQt6.QtWidgets import QApplication, QMainWindow, QDockWidget, QFileDialog
from PyQt6.QtCore import Qt, pyqtSlot, QUrl

from adp.gui.main_window import DownloadPanel
from adp.gui.theme import stylesheet_for

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest
except ImportError:  # pragma: no cover -- optional dev dependency
    QWebEngineView = None
    QWebEngineDownloadRequest = None


class TestRig(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Accelerated Downloader Pro -- Browser Test Rig (dev tool)")
        self.resize(1200, 800)

        self.browser = QWebEngineView()
        self.browser.load(QUrl("https://testfiles.hostnetworks.com.au/"))
        self.setCentralWidget(self.browser)

        self.download_panel = DownloadPanel(self)
        dock = QDockWidget("Downloads", self)
        dock.setWidget(self.download_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        self.profile = self.browser.page().profile()
        self.profile.downloadRequested.connect(self.handle_download)

    @pyqtSlot("QWebEngineDownloadRequest")
    def handle_download(self, download):
        download.accept()

        url = download.url().toString()
        suggested_filename = download.suggestedFileName()

        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", suggested_filename)
        if save_path:
            # Default to 1 thread for browser-captured downloads for maximum compatibility
            # (some sites reject concurrent range requests tied to a browser session/cookie).
            self.download_panel.add_download(url=url, save_path=save_path, num_threads=1, start_immediately=True)

    def closeEvent(self, event):
        self.download_panel.closeEvent(event)
        super().closeEvent(event)


def main():
    if QWebEngineView is None:
        print("PyQt6-WebEngine is not installed. Install it with:\n    pip install PyQt6-WebEngine")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyleSheet(stylesheet_for("light"))
    window = TestRig()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
