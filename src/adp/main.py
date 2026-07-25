"""Entry point: `python -m adp.main` or the packaged `adp-downloader` script."""
import sys

from adp.core.logging_setup import configure_logging
from adp.core.paths import default_app_data_dir, default_log_dir
from adp.gui.main_window import create_app, MainWindow


def main():
    state_dir = default_app_data_dir()
    log_path = configure_logging(default_log_dir(state_dir))
    print(f"Accelerated Downloader Pro -- logging to: {log_path}")

    app = create_app(sys.argv)
    window = MainWindow(state_dir=state_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
