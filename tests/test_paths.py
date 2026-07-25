import os
import sys

from adp.core.paths import default_app_data_dir, default_log_dir, APP_DIR_NAME


def test_default_app_data_dir_creates_and_returns_existing_dir(tmp_path, monkeypatch):
    # Redirect wherever this platform would normally point, so the test
    # never touches the real user's home/AppData directory.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path))

    path = default_app_data_dir()
    assert os.path.isdir(path)
    assert APP_DIR_NAME in path
    assert str(tmp_path) in path


def test_default_log_dir_is_nested_under_app_data_dir(tmp_path):
    app_data = str(tmp_path / "AppData")
    log_dir = default_log_dir(app_data)
    assert os.path.isdir(log_dir)
    assert log_dir == os.path.join(app_data, "logs")
