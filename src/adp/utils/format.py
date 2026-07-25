"""Pure formatting helpers, deliberately kept free of any GUI or Qt imports
so they're trivial to unit test."""

_POWER_LABELS = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}


def format_size(size_bytes: float) -> str:
    """Formats a byte count into a human-readable string, e.g. '12.34 MB'."""
    if size_bytes <= 0:
        return "0 B"
    n = 0
    while size_bytes >= 1024 and n < len(_POWER_LABELS) - 1:
        size_bytes /= 1024
        n += 1
    return f"{size_bytes:.2f} {_POWER_LABELS[n]}"


def format_speed(speed_bytes_per_sec: float) -> str:
    if speed_bytes_per_sec <= 0:
        return "0 B/s"
    return f"{format_size(speed_bytes_per_sec)}/s"


def format_eta(speed_bytes_per_sec: float, remaining_bytes: float) -> str:
    """Formats a countdown estimate; returns '--' when it can't be computed."""
    if speed_bytes_per_sec <= 0 or remaining_bytes <= 0:
        return "--"

    eta_sec = remaining_bytes / speed_bytes_per_sec
    if eta_sec < 60:
        return f"{int(eta_sec)}s"
    elif eta_sec < 3600:
        return f"{int(eta_sec / 60)}m {int(eta_sec % 60)}s"
    else:
        return f"{int(eta_sec / 3600)}h {int((eta_sec % 3600) / 60)}m"


def parse_size_to_bytes(text: str) -> int:
    """Parses user-entered strings like '512 KB', '2MB', '1.5 gb', '0' (unlimited)
    into a byte count. Returns 0 for empty/unlimited input. Raises ValueError
    on unparseable input."""
    text = (text or "").strip()
    if not text or text == "0":
        return 0
    text = text.upper().replace(" ", "")
    multipliers = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
    for suffix, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            number_part = text[: -len(suffix)]
            if not number_part:
                raise ValueError(f"Missing numeric value in '{text}'")
            return int(float(number_part) * mult)
    # No recognized suffix -- assume raw bytes.
    return int(float(text))
