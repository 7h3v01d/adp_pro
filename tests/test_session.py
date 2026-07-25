import os

from adp.core.models import DownloadRecord, Status
from adp.core.session import SessionStore


def make_record(**overrides):
    defaults = dict(
        download_id="abc-123",
        url="http://example.com/file.zip",
        save_path="/tmp/file.zip",
        checksum=None,
        num_threads=4,
        headers=None,
        category="Archives",
        speed_limit_bps=0,
        scheduled_time=None,
        status=Status.PENDING.name,
        downloaded_size=0,
        total_size=1000,
    )
    defaults.update(overrides)
    return DownloadRecord(**defaults)


def test_save_and_load_round_trip(tmp_path):
    store = SessionStore(str(tmp_path / "session.json"))
    records = [make_record(), make_record(download_id="def-456", category="Video")]

    store.save(records)
    loaded = store.load()

    assert len(loaded) == 2
    assert {r.download_id for r in loaded} == {"abc-123", "def-456"}
    reloaded_video = next(r for r in loaded if r.download_id == "def-456")
    assert reloaded_video.category == "Video"


def test_load_missing_file_returns_empty_list(tmp_path):
    store = SessionStore(str(tmp_path / "nope.json"))
    assert store.load() == []


def test_load_corrupt_file_returns_empty_list(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{not valid json")
    store = SessionStore(str(path))
    assert store.load() == []


def test_load_skips_malformed_entries_but_keeps_good_ones(tmp_path):
    path = tmp_path / "session.json"
    path.write_text('[{"download_id": "ok", "url": "u", "save_path": "s"}, {"category": "Video"}]')
    # the second entry is missing required fields (download_id/url/save_path) -> should be skipped
    store = SessionStore(str(path))
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].download_id == "ok"


def test_save_is_atomic_tmp_file_cleaned_up(tmp_path):
    store = SessionStore(str(tmp_path / "session.json"))
    store.save([make_record()])
    assert os.path.exists(store.session_file)
    assert not os.path.exists(store.session_file + ".tmp")
