import pytest

from adp.utils.format import format_size, format_speed, format_eta, parse_size_to_bytes


@pytest.mark.parametrize("size,expected", [
    (0, "0 B"),
    (-5, "0 B"),
    (512, "512.00 B"),
    (1024, "1.00 KB"),
    (1536, "1.50 KB"),
    (1024 ** 2, "1.00 MB"),
    (1024 ** 3, "1.00 GB"),
    (1024 ** 4, "1.00 TB"),
])
def test_format_size(size, expected):
    assert format_size(size) == expected


def test_format_speed_appends_per_second():
    assert format_speed(1024) == "1.00 KB/s"
    assert format_speed(0) == "0 B/s"


@pytest.mark.parametrize("speed,remaining,expected_prefix", [
    (0, 1000, "--"),
    (100, 0, "--"),
    (1000, 5000, "5s"),
    (1000, 90_000, "1m 30s"),
    (1000, 7_200_000, "2h 0m"),
])
def test_format_eta(speed, remaining, expected_prefix):
    assert format_eta(speed, remaining) == expected_prefix


@pytest.mark.parametrize("text,expected", [
    ("", 0),
    ("0", 0),
    ("512", 512),
    ("512B", 512),
    ("1KB", 1024),
    ("2.5 MB", int(2.5 * 1024 ** 2)),
    ("1GB", 1024 ** 3),
    ("1 gb", 1024 ** 3),
])
def test_parse_size_to_bytes(text, expected):
    assert parse_size_to_bytes(text) == expected


def test_parse_size_to_bytes_invalid_raises():
    with pytest.raises(ValueError):
        parse_size_to_bytes("KB")
