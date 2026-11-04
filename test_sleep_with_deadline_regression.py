"""Regression tests for sleep_with_deadline (found 2026-11-04)."""
import time
import unittest

from tiny_timeout import Deadline, sleep_with_deadline


class TestSleepWithDeadline(unittest.TestCase):
    def test_short_sleep_completes_when_deadline_allows_it(self):
        deadline = Deadline.from_seconds(5.0)
        self.assertTrue(sleep_with_deadline(0.01, deadline))

    def test_long_sleep_is_cut_short_and_reported_as_incomplete(self):
        # The deadline allows only 0.05s of a 5s sleep, so the sleep did NOT
        # complete and the function must say so.
        deadline = Deadline.from_seconds(0.05)
        self.assertFalse(sleep_with_deadline(5.0, deadline))

    def test_result_does_not_depend_on_the_sleep_oversleeping_the_deadline(self):
        """A sleep that wakes *early* must not be reported as complete.

        Windows has ~15.6ms timer granularity and a loaded runner can wake
        before the requested interval has elapsed. The old implementation
        returned ``not deadline.expired()``: a 5-second sleep cut to 0.05s
        returned True whenever the clock had not yet crossed the deadline,
        which is exactly the case the function exists to report. The GitHub
        Actions Windows job failed on this until it was masked with
        ``continue-on-error``.
        """
        real_sleep = time.sleep
        try:
            time.sleep = lambda t: real_sleep(t * 0.5)  # wake half-way
            deadline = Deadline.from_seconds(0.05)
            self.assertFalse(sleep_with_deadline(5.0, deadline))
        finally:
            time.sleep = real_sleep

    def test_already_expired_deadline_returns_false(self):
        deadline = Deadline.from_seconds(0.0)
        self.assertFalse(sleep_with_deadline(1.0, deadline))


if __name__ == "__main__":
    unittest.main()
