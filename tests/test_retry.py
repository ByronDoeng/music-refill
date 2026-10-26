from __future__ import annotations

import requests

from music_refill.services.retry import RetryPolicy, is_retryable


def test_network_errors_are_retryable():
    assert is_retryable(requests.exceptions.Timeout("read timeout"))
    assert is_retryable(ConnectionResetError("reset by peer"))
    assert is_retryable(RuntimeError("接口频繁，请稍后再试"))


def test_business_errors_are_not_retryable():
    assert not is_retryable(ValueError("Cookie 串里没有 MUSIC_U"))
    assert not is_retryable(RuntimeError("info/v2 失败: uploadStatus 6"))


def test_backoff_capped():
    p = RetryPolicy(attempts=5, base_s=5, max_s=40, jitter_s=0)
    assert p.delay(1) == 5
    assert p.delay(2) == 10
    assert p.delay(10) == 40
