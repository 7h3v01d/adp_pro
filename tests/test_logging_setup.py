# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import logging
import logging.handlers
import os

import pytest

from adp.core.logging_setup import configure_logging, get_current_log_path, reset_logging_for_tests


@pytest.fixture(autouse=True)
def _reset_logging():
    """Every test starts and ends with a clean root logger so tests don't
    leak file handles or handler state into each other."""
    reset_logging_for_tests()
    yield
    reset_logging_for_tests()


def test_configure_logging_creates_log_file(tmp_path):
    log_dir = str(tmp_path / "logs")
    log_path = configure_logging(log_dir)

    assert os.path.exists(log_path)
    assert log_path == os.path.join(log_dir, "adp.log")


def test_configure_logging_writes_messages_to_file(tmp_path):
    log_dir = str(tmp_path / "logs")
    log_path = configure_logging(log_dir)

    logging.getLogger("adp.core.downloader").info("test download message %s", "dl-123")

    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "test download message dl-123" in content
    assert "adp.core.downloader" in content


def test_configure_logging_is_idempotent(tmp_path):
    log_dir = str(tmp_path / "logs")
    first_path = configure_logging(log_dir)
    second_path = configure_logging(str(tmp_path / "other_logs"))  # ignored -- already configured

    assert first_path == second_path
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1


def test_get_current_log_path_before_configure_returns_none():
    assert get_current_log_path() is None


def test_get_current_log_path_after_configure(tmp_path):
    log_dir = str(tmp_path / "logs")
    log_path = configure_logging(log_dir)
    assert get_current_log_path() == log_path


def test_debug_level_messages_land_in_file_by_default(tmp_path):
    log_dir = str(tmp_path / "logs")
    log_path = configure_logging(log_dir)
    logging.getLogger("adp.core.downloader").debug("very verbose detail")

    with open(log_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "very verbose detail" in content


# -- secrets never reach the log ---------------------------------------------------
SECRET = "s3cr3t-T0KEN-value"


def _read(log_path):
    for handler in logging.getLogger().handlers:
        handler.flush()
    with open(log_path, "r", encoding="utf-8") as f:
        return f.read()


def test_signed_url_query_values_are_redacted_in_log_file(tmp_path):
    log_path = configure_logging(str(tmp_path / "logs"))
    log = logging.getLogger("adp.core.downloader")
    log.info("Starting download: url=https://cdn.example.com/f.iso?token=%s&X-Amz-Signature=%s", SECRET, SECRET)
    log.warning(f"Metadata fetch failed for https://user:{SECRET}@host.example/file.zip: boom")
    content = _read(log_path)
    assert SECRET not in content
    assert "https://cdn.example.com/f.iso?token=REDACTED&X-Amz-Signature=REDACTED" in content
    assert "https://REDACTED@host.example/file.zip" in content


def test_exception_text_and_traceback_are_redacted(tmp_path):
    """requests puts the full URL in its error messages; those get logged
    with exc_info, so the traceback text must be redacted too."""
    import requests
    log_path = configure_logging(str(tmp_path / "logs"))
    try:
        raise requests.HTTPError(f"404 Client Error: Not Found for url: https://h.example/x?sig={SECRET}")
    except requests.HTTPError as e:
        logging.getLogger("adp.core.downloader.workers").error("Worker failed: %s", e, exc_info=True)
    content = _read(log_path)
    assert SECRET not in content
    assert "for url: https://h.example/x?sig=REDACTED" in content


def test_magnet_tracker_passkeys_are_redacted(tmp_path):
    log_path = configure_logging(str(tmp_path / "logs"))
    logging.getLogger("adp.torrent").info(
        "magnet:?xt=urn:btih:abcdef&dn=ubuntu.iso&tr=https%3A%2F%2Ftracker%2Fannounce%3Fpasskey%3D" + SECRET)
    content = _read(log_path)
    assert SECRET not in content
    assert "magnet:?xt=urn:btih:abcdef&dn=ubuntu.iso&tr=REDACTED" in content


def test_real_failed_download_does_not_log_its_token(tmp_path, qapp, thread_pool, mock_server):
    """End to end: a tokenised URL that 404s goes through the real engine,
    which logs the URL and the requests error in several places."""
    import time
    from adp.core.downloader import DownloadManager
    from adp.core.models import Status
    log_path = configure_logging(str(tmp_path / "logs"))
    url = mock_server.url_for("missing.bin") + f"?token={SECRET}"
    m = DownloadManager("dl-secret", url, str(tmp_path / "missing.bin"), thread_pool)
    m.start()
    deadline = time.time() + 10
    while time.time() < deadline and m.status not in (Status.ERROR, Status.COMPLETED):
        qapp.processEvents()
        time.sleep(0.01)
    assert m.status == Status.ERROR
    content = _read(log_path)
    assert "dl-secret" in content           # the failure was logged...
    assert SECRET not in content            # ...without the token


def test_redaction_keeps_sentence_punctuation():
    from adp.utils.url_utils import redact_urls_in_text
    assert redact_urls_in_text("see https://h/x?a=1, then (https://h/y).") == \
        "see https://h/x?a=REDACTED, then (https://h/y)."
    assert redact_urls_in_text("no urls here") == "no urls here"
