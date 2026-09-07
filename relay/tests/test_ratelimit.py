import unittest
from unittest.mock import patch

from app.ratelimit import RateLimiter


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        limiter = RateLimiter(max_events=2, window_seconds=60)
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))

    def test_keys_are_independent(self) -> None:
        limiter = RateLimiter(max_events=1, window_seconds=60)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("a"))

    def test_old_hits_expire_out_of_the_window(self) -> None:
        limiter = RateLimiter(max_events=1, window_seconds=10)
        with patch("app.ratelimit.time.time", return_value=1000.0):
            self.assertTrue(limiter.allow("x"))
            self.assertFalse(limiter.allow("x"))
        with patch("app.ratelimit.time.time", return_value=1011.0):
            self.assertTrue(limiter.allow("x"))


if __name__ == "__main__":
    unittest.main()
