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
