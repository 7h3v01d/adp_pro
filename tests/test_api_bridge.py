import threading
import time

import pytest

from adp.api.bridge import GuiBridge


@pytest.fixture
def bridge(qtbot):
    b = GuiBridge(poll_interval_ms=5)
    yield b
    b.stop()


def test_call_executes_on_the_qt_main_thread(qtbot, bridge):
    main_thread_id = threading.get_ident()
    seen_thread_id = {}

    def record_thread():
        seen_thread_id["id"] = threading.get_ident()
        return "ok"

    result_box = {}

    def call_from_background():
        result_box["value"] = bridge.call(record_thread, timeout=5)

    worker = threading.Thread(target=call_from_background)
    worker.start()
    qtbot.waitUntil(lambda: "value" in result_box, timeout=5000)
    worker.join(timeout=2)

    assert result_box["value"] == "ok"
    assert seen_thread_id["id"] == main_thread_id


def test_call_returns_the_functions_result(qtbot, bridge):
    result_box = {}

    def worker_func():
        result_box["value"] = bridge.call(lambda: 2 + 2, timeout=5)

    t = threading.Thread(target=worker_func)
    t.start()
    qtbot.waitUntil(lambda: "value" in result_box, timeout=5000)
    t.join(timeout=2)
    assert result_box["value"] == 4


def test_call_reraises_exceptions_on_the_calling_thread(qtbot, bridge):
    error_box = {}

    def raises():
        raise ValueError("boom")

    def worker_func():
        try:
            bridge.call(raises, timeout=5)
        except ValueError as e:
            error_box["error"] = e

    t = threading.Thread(target=worker_func)
    t.start()
    qtbot.waitUntil(lambda: "error" in error_box, timeout=5000)
    t.join(timeout=2)
    assert str(error_box["error"]) == "boom"


def test_call_passes_args_and_kwargs(qtbot, bridge):
    result_box = {}

    def add(a, b, c=0):
        return a + b + c

    def worker_func():
        result_box["value"] = bridge.call(add, 1, 2, c=3, timeout=5)

    t = threading.Thread(target=worker_func)
    t.start()
    qtbot.waitUntil(lambda: "value" in result_box, timeout=5000)
    t.join(timeout=2)
    assert result_box["value"] == 6


def test_call_times_out_if_gui_never_responds(qtbot):
    bridge = GuiBridge(poll_interval_ms=5)
    bridge.stop()  # stop the timer so nothing ever drains the queue

    error_box = {}

    def worker_func():
        try:
            bridge.call(lambda: "unreachable", timeout=0.3)
        except TimeoutError as e:
            error_box["error"] = e

    t = threading.Thread(target=worker_func)
    t.start()
    t.join(timeout=2)
    assert "error" in error_box


def test_multiple_concurrent_calls_all_complete_correctly(qtbot, bridge):
    """Several background threads hammering the bridge at once shouldn't
    cross-contaminate each other's results."""
    results = {}

    def make_worker(i):
        def worker_func():
            results[i] = bridge.call(lambda x=i: x * 10, timeout=5)
        return worker_func

    threads = [threading.Thread(target=make_worker(i)) for i in range(20)]
    for t in threads:
        t.start()
    qtbot.waitUntil(lambda: len(results) == 20, timeout=5000)
    for t in threads:
        t.join(timeout=2)

    assert results == {i: i * 10 for i in range(20)}
