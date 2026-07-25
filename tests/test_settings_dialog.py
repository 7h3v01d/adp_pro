# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""SettingsDialog round-trip for the new storage-location fields."""
import pytest

pytestmark = pytest.mark.gui

from adp.gui.dialogs import SettingsDialog


def test_storage_dirs_round_trip(qtbot, tmp_path):
    dl = str(tmp_path / "downloads")
    tor = str(tmp_path / "torrents")
    dialog = SettingsDialog(current_settings={
        "download_dir": dl, "torrent_download_dir": tor, "theme": "dark",
    })
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["download_dir"] == dl
    assert out["torrent_download_dir"] == tor
    assert out["theme"] == "dark"


def test_blank_dirs_stay_blank(qtbot):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["download_dir"] == ""
    assert out["torrent_download_dir"] == ""


def test_free_space_label_populated_for_real_dir(qtbot, tmp_path):
    dialog = SettingsDialog(current_settings={"download_dir": str(tmp_path)})
    qtbot.addWidget(dialog)
    # Constructor calls _update_free_space_label once.
    assert "free" in dialog.free_space_label.text().lower()


def test_existing_settings_preserved_through_dialog(qtbot):
    """The dialog must not drop unrelated keys it does own -- a regression
    guard so adding storage fields didn't disturb the rest."""
    dialog = SettingsDialog(current_settings={
        "theme": "dark", "verify_tls": False, "torrent_listen_port": 7000,
        "torrent_enable_dht": False,
    })
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    assert out["verify_tls"] is False
    assert out["torrent_listen_port"] == 7000
    assert out["torrent_enable_dht"] is False


def test_unknown_keys_survive_dialog_round_trip(qtbot):
    """Opening Settings and clicking OK must NOT wipe keys the dialog doesn't
    expose as widgets -- notably search_providers (which holds the user's
    Jackett API key) and api_port. Regression for a silent data-loss bug."""
    original = {
        "theme": "dark",
        "api_port": 9999,
        "search_providers": {
            "torrents_csv": {"enabled": True},
            "jackett": {"enabled": True, "api_key": "SECRET-KEY", "base_url": "http://host:9117"},
        },
    }
    dialog = SettingsDialog(current_settings=original)
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    # Dialog-owned key still works.
    assert out["theme"] == "dark"
    # Unknown keys preserved intact.
    assert out["api_port"] == 9999
    assert out["search_providers"]["jackett"]["api_key"] == "SECRET-KEY"
    assert out["search_providers"]["torrents_csv"]["enabled"] is True


def test_get_settings_does_not_mutate_original(qtbot):
    original = {"search_providers": {"jackett": {"api_key": "K"}}}
    dialog = SettingsDialog(current_settings=original)
    qtbot.addWidget(dialog)
    out = dialog.get_settings()
    out["search_providers"]["jackett"]["api_key"] = "TAMPERED"
    # Mutating the returned dict must not reach back into what we passed in.
    assert original["search_providers"]["jackett"]["api_key"] == "K"


def test_metadata_autofill_uses_configured_dir(qtbot, tmp_path):
    """When metadata resolves a filename, it should land in the configured
    download folder, not the process cwd."""
    from adp.gui.dialogs import AddDownloadDialog
    configured = str(tmp_path / "MyDownloads")
    dialog = AddDownloadDialog(default_dir=configured)
    qtbot.addWidget(dialog)
    dialog.on_metadata_fetched(1000, "bytes", "etag", "lm", "cool-file.iso")
    import os
    assert dialog.path_input.text() == os.path.join(configured, "cool-file.iso")
