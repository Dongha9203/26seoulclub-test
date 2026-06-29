"""
인스턴스 cold start 지속시간을 추적합니다.

lifespan이 완료되면 record_cold_start_ms()로 기록하고,
첫 번째 요청이 consume_cold_start_ms()로 값을 가져간 뒤 0으로 리셋합니다.
이후 요청은 0을 받으므로 warm 요청에는 cold start 비용이 중복 기록되지 않습니다.
"""

import threading

_lock = threading.Lock()
_cold_start_ms: int = 0


def record_cold_start_ms(ms: int) -> None:
    global _cold_start_ms
    with _lock:
        _cold_start_ms = ms


def consume_cold_start_ms() -> int:
    global _cold_start_ms
    with _lock:
        val = _cold_start_ms
        _cold_start_ms = 0
        return val
