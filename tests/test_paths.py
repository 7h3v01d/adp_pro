# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
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


class TestSettingsDeepMerge:
    """AppSettingsStore.load must deep-copy defaults and deep-merge stored
    values, so nested config (search_providers) is neither shared with the
    module default nor clobbered by a partial stored file."""

    def test_partial_stored_provider_keeps_default_siblings(self, tmp_path):
        import json
        from adp.core.app_settings import AppSettingsStore, DEFAULT_SETTINGS
        settings_file = tmp_path / "settings.json"
        # Stored file only mentions jackett; torrents_csv default must survive.
        settings_file.write_text(json.dumps({
            "search_providers": {"jackett": {"enabled": True, "api_key": "K"}}
        }))
        loaded = AppSettingsStore(str(settings_file)).load()
        assert loaded["search_providers"]["jackett"]["api_key"] == "K"
        assert "torrents_csv" in loaded["search_providers"]
        # And the module-level default was not mutated.
        assert DEFAULT_SETTINGS["search_providers"]["jackett"].get("api_key", "") == ""

    def test_load_does_not_share_nested_dicts_with_default(self, tmp_path):
        from adp.core.app_settings import AppSettingsStore, DEFAULT_SETTINGS
        loaded = AppSettingsStore(str(tmp_path / "none.json")).load()
        loaded["search_providers"]["jackett"]["api_key"] = "MUTATED"
        assert DEFAULT_SETTINGS["search_providers"]["jackett"].get("api_key", "") == ""
