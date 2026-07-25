"""Centralized logging configuration for Accelerated Downloader Pro.

Goal: when a download fails, there should be a plain-text log file a user
can open (or attach to a bug report) that shows exactly what happened --
which download, which byte range, what the server responded, and the full
exception -- without needing to reproduce the problem with a debugger
attached.

configure_logging() should be called exactly once, as early as possible
(before any DownloadManager is created), typically from main.py.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_FILENAME = "adp.log"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 3

FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(threadName)-15s | %(name)s | %(message)s"
CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(message)s"

_configured = False


def configure_logging(log_dir: str, file_level=logging.DEBUG, console_level=logging.INFO) -> str:
    """Sets up the root logger with a rotating file handler (verbose, for
    diagnosing issues after the fact) and a console handler (concise, for
    watching things live when run from a terminal). Returns the full path
    to the active log file.

    Safe to call more than once -- subsequent calls are no-ops so tests and
    the dev test-rig tool can share this without duplicating handlers.
    """
    global _configured
    if _configured:
        return get_current_log_path()

    log_path = os.path.join(log_dir, LOG_FILENAME)
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    root.addHandler(file_handler)

    # A windowed (non-console) build has sys.stdout/stderr set to None --
    # guard against that rather than letting logging crash on setup.
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(console_level)
        console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        root.addHandler(console_handler)

    _install_excepthook()

    # When the disk holding the log file fills up (a state a download
    # manager can genuinely cause), every emit raises OSError(ENOSPC) and,
    # with raiseExceptions on, logging dumps a full '--- Logging error ---'
    # call stack to stderr per log call -- burying the actual incident in
    # noise at exactly the moment someone is trying to read the console.
    # Drop failed log writes silently instead; the console handler (a
    # different stream) keeps working either way.
    logging.raiseExceptions = False

    _configured = True
    logging.getLogger(__name__).info("Logging initialized. Log file: %s", log_path)
    return log_path


def _install_excepthook():
    """Routes uncaught exceptions (e.g. a bug in a Qt slot) into the log
    file instead of only flashing past in a console window or getting lost
    entirely in a windowed build."""
    previous_hook = sys.excepthook

    def _log_uncaught(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger("adp.uncaught").critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback)
        )
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = _log_uncaught


def get_current_log_path() -> str | None:
    """Returns the path of the active rotating file log, if configure_logging
    has been called, else None. Lets any part of the app (e.g. a 'View Logs'
    menu action) find the log file without needing it threaded through every
    constructor."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return handler.baseFilename
    return None


def reset_logging_for_tests():
    """Removes all handlers from the root logger and clears the
    already-configured flag. Only intended for use by the test suite, so
    each test that exercises logging starts from a clean slate."""
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _configured = False
