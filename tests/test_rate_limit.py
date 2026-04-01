from rate_limit import SimpleRateLimiter


def test_rate_limiter_allows_within_limit():
    r = SimpleRateLimiter()
    assert r.hit("k", limit=3, window_seconds=60) is True
    assert r.hit("k", limit=3, window_seconds=60) is True
    assert r.hit("k", limit=3, window_seconds=60) is True
    assert r.hit("k", limit=3, window_seconds=60) is False


def test_rate_limiter_keys_independent():
    r = SimpleRateLimiter()
    assert r.hit("a", limit=1, window_seconds=60) is True
    assert r.hit("a", limit=1, window_seconds=60) is False
    assert r.hit("b", limit=1, window_seconds=60) is True
