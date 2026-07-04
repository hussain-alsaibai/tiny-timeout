"""Tests for tiny_timeout."""

import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tiny_timeout as tt
from tiny_timeout import (
    TimeoutExceeded,
    Deadline,
    timeout,
    run_with_timeout,
    timeout_at,
    sleep_with_deadline,
    soft_timeout,
    shutdown_executor,
)


class TestDeadline(unittest.TestCase):
    def test_from_seconds(self):
        d = Deadline.from_seconds(0.5)
        self.assertGreater(d.remaining(), 0.0)
        self.assertFalse(d.expired())
        time.sleep(0.55)
        self.assertTrue(d.expired())
        self.assertEqual(d.remaining(), 0.0)

    def test_from_seconds_invalid(self):
        with self.assertRaises(ValueError):
            Deadline.from_seconds(-1.0)

    def test_check_raises_when_expired(self):
        d = Deadline.from_seconds(0.0)
        # Sleep so the deadline becomes "past"
        time.sleep(0.01)
        with self.assertRaises(TimeoutExceeded):
            d.check()

    def test_at(self):
        d = Deadline.at(time.monotonic() + 1.0)
        self.assertGreater(d.remaining(), 0.0)


class TestRunWithTimeout(unittest.TestCase):
    def test_fast_call_returns_result(self):
        result = run_with_timeout(lambda: 42, 1.0)
        self.assertEqual(result, 42)

    def test_slow_call_raises(self):
        def slow():
            time.sleep(1.0)
            return "done"

        start = time.monotonic()
        with self.assertRaises(TimeoutExceeded) as ctx:
            run_with_timeout(slow, 0.1)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)
        self.assertAlmostEqual(ctx.exception.seconds, 0.1, places=2)
        self.assertIsNotNone(ctx.exception.elapsed)

    def test_default_returned_on_timeout(self):
        def slow():
            time.sleep(1.0)
            return "done"

        result = run_with_timeout(slow, 0.1, default="fallback")
        self.assertEqual(result, "fallback")

    def test_exception_propagates(self):
        def bad():
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            run_with_timeout(bad, 1.0)

    def test_positional_and_keyword_args(self):
        def add(a, b, *, c=0):
            return a + b + c

        self.assertEqual(run_with_timeout(add, 1.0, 1, 2, c=3), 6)
        self.assertEqual(run_with_timeout(add, 1.0, 1, 2), 3)

    def test_invalid_seconds(self):
        with self.assertRaises(ValueError):
            run_with_timeout(lambda: 1, 0)
        with self.assertRaises(ValueError):
            run_with_timeout(lambda: 1, -1)


class TestTimeoutDecorator(unittest.TestCase):
    def test_decorator_basic(self):
        @timeout(1.0)
        def fast():
            return 7

        self.assertEqual(fast(), 7)

    def test_decorator_timeout(self):
        @timeout(0.1)
        def slow():
            time.sleep(1.0)

        with self.assertRaises(TimeoutExceeded):
            slow()

    def test_decorator_default(self):
        @timeout(0.1, name="slow_fn")
        def slow():
            time.sleep(1.0)

        # Decorator path does not support default; use run_with_timeout.
        with self.assertRaises(TimeoutExceeded) as ctx:
            slow()
        self.assertEqual(ctx.exception.name, "slow_fn")


class TestTimeoutContextManager(unittest.TestCase):
    def test_context_manager_yields_deadline(self):
        with timeout(0.5) as d:
            self.assertIsInstance(d, Deadline)
            self.assertGreater(d.remaining(), 0.0)

    def test_context_manager_does_not_kill_body(self):
        # Cooperative: body runs to completion even if past the budget.
        # The context manager is a marker only.
        with timeout(0.001):
            time.sleep(0.05)


class TestTimeoutAt(unittest.TestCase):
    def test_absolute_deadline(self):
        with self.assertRaises(TimeoutExceeded):
            run_with_timeout(lambda: time.sleep(1.0), 10.0, timeout_=0.05)


class TestSleepWithDeadline(unittest.TestCase):
    def test_completes_within_remaining(self):
        d = Deadline.from_seconds(1.0)
        ok = sleep_with_deadline(0.05, d)
        self.assertTrue(ok)

    def test_truncated_by_deadline(self):
        d = Deadline.from_seconds(0.05)
        start = time.monotonic()
        ok = sleep_with_deadline(5.0, d)
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.5)


class TestSoftTimeout(unittest.TestCase):
    def test_marker_does_not_interrupt(self):
        with self.assertRaises(TimeoutExceeded):
            with soft_timeout(0.01) as d:
                time.sleep(0.05)
                d.check()  # raises manually inside the block


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_timeouts(self):
        # Multiple hard timeouts in parallel; the shared executor should
        # handle them without crashing.
        def slow():
            time.sleep(0.5)

        results: list = []
        errors: list = []

        def runner():
            try:
                r = run_with_timeout(slow, 0.05, default="timed_out")
            except Exception as exc:
                errors.append(exc)
            else:
                results.append(r)

        threads = [threading.Thread(target=runner) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All should have returned the default sentinel "timed_out".
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r == "timed_out" for r in results))
        self.assertEqual(errors, [])


class TestDefaults(unittest.TestCase):
    def test_executor_shutdown_no_crash(self):
        # Should be safe to call multiple times.
        shutdown_executor(wait=False)
        shutdown_executor(wait=False)
        # Re-acquire by issuing a timeout
        result = run_with_timeout(lambda: 99, 1.0)
        self.assertEqual(result, 99)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutdown_executor(wait=False)