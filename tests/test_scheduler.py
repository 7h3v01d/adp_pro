from datetime import datetime, timedelta

from adp.core.scheduler import DownloadScheduler


def test_schedule_and_unschedule(qapp):
    sched = DownloadScheduler(clock=lambda: datetime(2026, 1, 1, 12, 0, 0))
    sched.schedule("dl-1", datetime(2026, 1, 1, 13, 0, 0))
    assert sched.is_scheduled("dl-1")
    sched.unschedule("dl-1")
    assert not sched.is_scheduled("dl-1")


def test_check_due_fires_signal_only_for_elapsed_entries(qapp):
    now = {"t": datetime(2026, 1, 1, 12, 0, 0)}
    sched = DownloadScheduler(clock=lambda: now["t"])

    fired = []
    sched.due.connect(fired.append)

    sched.schedule("past-due", now["t"] - timedelta(minutes=5))
    sched.schedule("future", now["t"] + timedelta(hours=1))

    sched.check_due()

    assert fired == ["past-due"]
    assert sched.is_scheduled("future")
    assert not sched.is_scheduled("past-due")


def test_check_due_advances_with_clock(qapp):
    now = {"t": datetime(2026, 1, 1, 12, 0, 0)}
    sched = DownloadScheduler(clock=lambda: now["t"])
    fired = []
    sched.due.connect(fired.append)

    sched.schedule("dl-x", now["t"] + timedelta(minutes=10))
    sched.check_due()
    assert fired == []

    now["t"] += timedelta(minutes=11)
    sched.check_due()
    assert fired == ["dl-x"]
