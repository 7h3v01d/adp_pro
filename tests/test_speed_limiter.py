import time

from adp.core.speed_limiter import SpeedLimiter


def test_unlimited_by_default_does_not_block():
    limiter = SpeedLimiter(0)
    assert limiter.unlimited
    start = time.monotonic()
    limiter.acquire(10_000_000)
    assert time.monotonic() - start < 0.05


def test_limit_throttles_large_transfer():
    limiter = SpeedLimiter(bytes_per_second=50_000)  # 50 KB/s
    start = time.monotonic()
    # Burst allowance means the first chunk should be near-instant...
    limiter.acquire(12_500)
    # ...but consuming well beyond the burst capacity forces real waiting.
    limiter.acquire(50_000)
    elapsed = time.monotonic() - start
    assert elapsed > 0.5  # roughly (50000 - remaining_tokens) / 50000 sec


def test_set_limit_updates_rate_and_resets_bucket():
    limiter = SpeedLimiter(1000)
    assert limiter.rate == 1000
    limiter.set_limit(0)
    assert limiter.unlimited
    limiter.set_limit(2000)
    assert limiter.rate == 2000
    assert not limiter.unlimited


def test_acquire_zero_or_negative_is_noop():
    limiter = SpeedLimiter(100)
    start = time.monotonic()
    limiter.acquire(0)
    limiter.acquire(-5)
    assert time.monotonic() - start < 0.05
