import os

from adp.core.stats_store import StatsStore, DEFAULT_STATS


def test_load_missing_file_returns_defaults_with_first_used_at_set(tmp_path):
    store = StatsStore(str(tmp_path))
    stats = store.load()
    assert stats["lifetime_downloaded_bytes"] == 0
    assert stats["lifetime_uploaded_bytes"] == 0
    assert stats["lifetime_completed_downloads"] == 0
    assert stats["lifetime_completed_torrents"] == 0
    assert stats["first_used_at"] is not None


def test_save_and_load_round_trip(tmp_path):
    store = StatsStore(str(tmp_path))
    stats = store.load()
    stats["lifetime_downloaded_bytes"] = 12345
    stats["lifetime_completed_downloads"] = 3
    store.save(stats)

    reloaded = StatsStore(str(tmp_path)).load()
    assert reloaded["lifetime_downloaded_bytes"] == 12345
    assert reloaded["lifetime_completed_downloads"] == 3
    # first_used_at should be preserved across reloads, not reset
    assert reloaded["first_used_at"] == stats["first_used_at"]


def test_save_is_atomic_tmp_file_cleaned_up(tmp_path):
    store = StatsStore(str(tmp_path))
    store.save(dict(DEFAULT_STATS))
    assert os.path.exists(store.stats_file)
    assert not os.path.exists(store.stats_file + ".tmp")


def test_load_corrupt_file_falls_back_to_defaults(tmp_path):
    stats_file = tmp_path / "stats.json"
    stats_file.write_text("{not valid json")
    store = StatsStore(str(tmp_path))
    stats = store.load()
    assert stats["lifetime_downloaded_bytes"] == 0
