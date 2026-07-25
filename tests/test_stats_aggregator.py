from adp.core.stats_aggregator import StatsAggregator
from adp.core.stats_store import StatsStore


def make_aggregator(tmp_path):
    return StatsAggregator(StatsStore(str(tmp_path)))


def test_first_observation_of_an_id_establishes_a_baseline_not_a_delta(tmp_path):
    """The very first time an id is seen, its current value must NOT be
    counted as a fresh delta -- otherwise a download/torrent restored from
    a previous session would have its entire historical byte count
    double-counted into lifetime stats the moment polling resumes after a
    restart."""
    agg = make_aggregator(tmp_path)
    agg.record_download_progress("dl-1", 1_000_000)  # a big number, as if resumed mid-download
    assert agg.session_downloaded_bytes == 0
    assert agg.lifetime["lifetime_downloaded_bytes"] == 0


def test_download_progress_accumulates_deltas_after_baseline(tmp_path):
    agg = make_aggregator(tmp_path)
    agg.record_download_progress("dl-1", 1000)   # baseline, delta 0
    agg.record_download_progress("dl-1", 2500)   # delta 1500
    agg.record_download_progress("dl-1", 2500)   # no change -- shouldn't double count
    assert agg.session_downloaded_bytes == 1500
    assert agg.lifetime["lifetime_downloaded_bytes"] == 1500


def test_torrent_progress_tracks_download_and_upload_separately(tmp_path):
    agg = make_aggregator(tmp_path)
    agg.record_torrent_progress("t-1", all_time_download=5000, all_time_upload=1000)  # baseline
    agg.record_torrent_progress("t-1", all_time_download=8000, all_time_upload=4000)  # +3000 each
    assert agg.session_downloaded_bytes == 3000
    assert agg.session_uploaded_bytes == 3000


def test_multiple_items_accumulate_independently(tmp_path):
    agg = make_aggregator(tmp_path)
    agg.record_download_progress("dl-1", 1000)
    agg.record_download_progress("dl-1", 1500)   # +500
    agg.record_download_progress("dl-2", 2000)
    agg.record_download_progress("dl-2", 2500)   # +500
    agg.record_torrent_progress("t-1", 3000, 500)
    agg.record_torrent_progress("t-1", 3400, 900)  # +400 down, +400 up
    assert agg.session_downloaded_bytes == 500 + 500 + 400
    assert agg.session_uploaded_bytes == 400


def test_never_goes_negative_on_a_counter_reset(tmp_path):
    """If a manager's byte counter somehow resets lower (e.g. re-added under
    the same id after being cleared), deltas must never go negative and
    silently corrupt the running total."""
    agg = make_aggregator(tmp_path)
    agg.record_download_progress("dl-1", 5000)   # baseline
    agg.record_download_progress("dl-1", 6000)   # +1000, total=1000
    agg.record_download_progress("dl-1", 100)    # would be -5900 -- must clamp to 0
    assert agg.session_downloaded_bytes == 1000


def test_completion_counters(tmp_path):
    agg = make_aggregator(tmp_path)
    agg.record_download_completed()
    agg.record_download_completed()
    agg.record_torrent_completed()
    assert agg.session_completed_downloads == 2
    assert agg.session_completed_torrents == 1
    assert agg.lifetime["lifetime_completed_downloads"] == 2
    assert agg.lifetime["lifetime_completed_torrents"] == 1


def test_lifetime_persists_across_aggregator_instances(tmp_path):
    store = StatsStore(str(tmp_path))
    agg1 = StatsAggregator(store)
    agg1.record_download_progress("dl-1", 9999)    # baseline
    agg1.record_download_progress("dl-1", 19999)   # +10000
    agg1.record_download_completed()
    agg1.save()

    agg2 = StatsAggregator(StatsStore(str(tmp_path)))
    assert agg2.lifetime["lifetime_downloaded_bytes"] == 10000
    assert agg2.lifetime["lifetime_completed_downloads"] == 1
    # A fresh aggregator's session counters always start at zero, though.
    assert agg2.session_downloaded_bytes == 0


def test_forget_resets_baseline_rather_than_causing_a_negative_delta(tmp_path):
    agg = make_aggregator(tmp_path)
    agg.record_download_progress("dl-1", 1000)   # baseline
    agg.record_download_progress("dl-1", 1500)   # +500, total=500
    agg.forget("dl-1")
    # After forgetting, the next observation is a fresh baseline again (delta
    # 0), not a huge negative delta computed against the stale pre-forget value.
    agg.record_download_progress("dl-1", 1500)
    assert agg.session_downloaded_bytes == 500  # unchanged by the re-baseline
