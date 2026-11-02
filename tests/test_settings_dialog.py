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
    assert "free" in dialog.download_free_space_label.text().lower()


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


def test_stop_on_complete_defaults_on(qtbot):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    assert dialog.torrent_stop_on_complete_checkbox.isChecked()
    assert dialog.get_settings()["torrent_stop_on_complete"] is True


def test_stop_on_complete_round_trip_off(qtbot):
    dialog = SettingsDialog(current_settings={"torrent_stop_on_complete": False})
    qtbot.addWidget(dialog)
    assert not dialog.torrent_stop_on_complete_checkbox.isChecked()
    dialog.torrent_stop_on_complete_checkbox.setChecked(True)
    assert dialog.get_settings()["torrent_stop_on_complete"] is True


def test_torrent_record_keep_seeding_round_trip():
    from adp.torrent.models import TorrentRecord
    rec = TorrentRecord(torrent_id="a" * 40, name="x", save_path="/tmp", keep_seeding=True)
    assert TorrentRecord.from_dict(rec.to_dict()).keep_seeding is True
    # Sessions saved before this field existed load with the default.
    legacy = rec.to_dict()
    legacy.pop("keep_seeding")
    assert TorrentRecord.from_dict(legacy).keep_seeding is False


def test_settings_are_grouped_into_tabs(qtbot):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert titles == ["General", "Downloads", "Torrents"]
    page = {t: dialog.tabs.widget(i) for i, t in enumerate(titles)}
    assert page["General"].isAncestorOf(dialog.theme_input)
    assert page["General"].isAncestorOf(dialog.minimize_to_tray_checkbox)
    assert page["Downloads"].isAncestorOf(dialog.download_dir_input)
    assert page["Downloads"].isAncestorOf(dialog.verify_tls_checkbox)
    assert page["Downloads"].isAncestorOf(dialog.download_free_space_label)
    for w in (dialog.torrent_dir_input, dialog.torrent_stop_on_complete_checkbox,
              dialog.torrent_seed_ratio_input, dialog.torrent_listen_port_input,
              dialog.torrent_dht_checkbox, dialog.torrent_free_space_label):
        assert page["Torrents"].isAncestorOf(w)


def test_each_folder_gets_its_own_free_space_readout(qtbot, tmp_path):
    dialog = SettingsDialog(current_settings={"torrent_download_dir": str(tmp_path)})
    qtbot.addWidget(dialog)
    assert "free" in dialog.torrent_free_space_label.text().lower()
    assert "app-data" in dialog.download_free_space_label.text().lower()


def test_max_active_downloads_setting(qtbot):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    assert dialog.torrent_max_active_input.value() == 3
    assert dialog.get_settings()["torrent_max_active_downloads"] == 3
    dialog.torrent_max_active_input.setValue(0)
    assert dialog.torrent_max_active_input.text() == "Unlimited"
    assert dialog.get_settings()["torrent_max_active_downloads"] == 0
    torrents_tab = dialog.tabs.widget(2)
    assert torrents_tab.isAncestorOf(dialog.torrent_max_active_input)


def test_move_completed_settings_round_trip(qtbot, tmp_path):
    dialog = SettingsDialog(current_settings={})
    qtbot.addWidget(dialog)
    assert not dialog.torrent_move_checkbox.isChecked()
    assert not dialog.torrent_move_dir_input.isEnabled()  # greyed out while off
    dialog.torrent_move_checkbox.setChecked(True)
    assert dialog.torrent_move_dir_input.isEnabled()
    dialog.torrent_move_dir_input.setText(str(tmp_path))
    dialog.torrent_move_by_category_checkbox.setChecked(True)
    out = dialog.get_settings()
    assert out["torrent_move_completed"] is True
    assert out["torrent_move_completed_dir"] == str(tmp_path)
    assert out["torrent_move_completed_by_category"] is True
    assert "free" in dialog.torrent_move_free_space_label.text().lower()
    assert dialog.tabs.widget(2).isAncestorOf(dialog.torrent_move_dir_input)


def test_move_completed_dir_resolution():
    from adp.gui.main_window.window import MainWindow
    f = MainWindow._move_completed_dir
    assert f({}) is None
    assert f({"torrent_move_completed": True, "torrent_move_completed_dir": "  "}) is None
    assert f({"torrent_move_completed": False, "torrent_move_completed_dir": "E:/Lib"}) is None
    assert f({"torrent_move_completed": True, "torrent_move_completed_dir": "E:/Lib"}) == "E:/Lib"
