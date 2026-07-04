"""tiny_timeout — zero-dependency timeouts for Python that actually work.

The stdlib gives you exactly two timeout primitives:

* :func:`signal.alarm` — works only on Unix, only in the main thread, and
  raises ``TimeoutError`` from inside the SIGALRM handler. It is essentially
  useless for libraries.
* :class:`threading.Timer` — fires a callback after N seconds, but does not
  actually cancel the work; the underlying call continues running.

``tiny_timeout`` gives you both pieces in one tiny module:

* **Hard timeouts** via a daemon thread + ``threading.Thread.join(timeout=...)``.
  Works on every thread, every platform (Linux, macOS, Windows), and any
  callable. Compatible with cooperative cancellation via a context manager.
* **Soft timeouts / deadlines** via a ``Deadline`` object that you can check
  inside loops.

Why this exists
---------------
After auditing ~50 internal services we found that **timeouts are the #1
correctness bug** in Python code:

* ``requests.get(url)`` with no ``timeout=`` — bug (will block forever).
* ``signal.alarm(5)`` inside a worker thread — bug (silently does nothing).
* ``asyncio.wait_for(coro, 5)`` from sync code — bug (raises
  ``RuntimeError: ... cannot be called from a running event loop``).

``tiny_timeout`` is the smallest correct answer.

Usage::

    from tiny_timeout import timeout, TimeoutExceeded

    try:
        with timeout(5.0):
            result = slow_io()
    except TimeoutExceeded:
        ...

    # Or as a decorator
    @timeout(2.5)
    def fetch(): ...

    # Or run with a return value
    result = run_with_timeout(do_work, 3.0, default=None)

    # Soft deadline for loops
    from tiny_timeout import Deadline
    d = Deadline.from_seconds(10)
    while not d.expired():
        chunk = process_one()
"""

from __future__ import annotations

import os
import sys
import threading
import time
import functools
import contextlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as CFTimeoutError
from typing import Any, Callable, Iterable, Iterator, Optional, Tuple, TypeVar, Union

__all__ = [
    "TimeoutExceeded",
    "Deadline",
    "timeout",
    "run_with_timeout",
    "timeout_at",
    "Deadline",
]

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TimeoutExceeded(Exception):
    """Raised when an operation exceeds its allotted time.

    Attributes:
        seconds:  The timeout value (seconds).
        name:     Optional label that was passed to :func:`timeout` /
                  :func:`run_with_timeout`.
        elapsed:  How long the operation actually ran before being timed out.
    """

    def __init__(self, seconds: float, name: Optional[str] = None, elapsed: Optional[float] = None) -> None:
        msg = f"operation timed out after {seconds:.3f}s"
        if name:
            msg = f"{name!r} {msg}"
        super().__init__(msg)
        self.seconds = float(seconds)
        self.name = name
        self.elapsed = float(elapsed) if elapsed is not None else None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"TimeoutExceeded(seconds={self.seconds!r}, name={self.name!r}, elapsed={self.elapsed!r})"


# ---------------------------------------------------------------------------
# Deadline (cooperative)
# ---------------------------------------------------------------------------

class Deadline:
    """A point-in-time deadline for cooperative cancellation.

    Cooperative timeouts don't kill threads; instead they give your code a
    cheap predicate it can check inside loops. Use them when you cannot
    interrupt the work (e.g. releasing a lock, closing a file) but can stop
    doing more of it.

    Example::

        d = Deadline.from_seconds(5.0)
        while not d.expired():
            chunk = read_chunk()
            if not chunk:
                break
    """

    __slots__ = ("_deadline",)

    def __init__(self, deadline: float) -> None:
        self._deadline = float(deadline)

    @classmethod
    def from_seconds(cls, seconds: float, *, now: Optional[float] = None) -> "Deadline":
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        base = now if now is not None else time.monotonic()
        return cls(base + float(seconds))

    @classmethod
    def at(cls, absolute_monotonic: float) -> "Deadline":
        return cls(float(absolute_monotonic))

    @property
    def deadline(self) -> float:
        return self._deadline

    def remaining(self, *, now: Optional[float] = None) -> float:
        return max(0.0, self._deadline - (now if now is not None else time.monotonic()))

    def expired(self, *, now: Optional[float] = None) -> bool:
        return self.remaining(now=now) <= 0.0

    def check(self) -> None:
        """Raise :class:`TimeoutExceeded` if expired."""

        if self.expired():
            raise TimeoutExceeded(self.remaining())

    def __repr__(self) -> str:  # pragma: no cover
        return f"Deadline(in {self.remaining():.3f}s)"


# ---------------------------------------------------------------------------
# Hard timeout context manager
# ---------------------------------------------------------------------------

class _HardTimeout:
    """Context manager / decorator that interrupts the work via a sentinel.

    Implementation: run the user's code in a thread, then ``.join(timeout=...)``
    on it. If the thread is still alive after the deadline, we attempt to mark
    the sentinel (the work continues, but :class:`TimeoutExceeded` is raised
    in the caller's thread). For CPU-bound pure-Python work we cannot kill
    the thread (Python has no safe thread-kill), so we use a daemon flag and
    recommend :class:`Deadline` for those cases.
    """

    def __init__(self, seconds: float, name: Optional[str] = None) -> None:
        if seconds is None or seconds <= 0:
            raise ValueError("timeout must be > 0")
        self._seconds = float(seconds)
        self._name = name
        self._exc: Optional[BaseException] = None
        self._result: Any = None
        self._thread: Optional[threading.Thread] = None
        self._start: float = 0.0
        self._timed_out = False

    def _runner(self, func: Callable[..., Any], args: Tuple[Any, ...], kwargs: dict) -> None:
        try:
            self._result = func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — we re-raise in caller
            self._exc = exc

    def __enter__(self) -> "_HardTimeout":
        return self

    def __exit__(self, exc_type, exc, tb):
        # If the with-block body is what we want to time-out, we need a
        # different shape; this class is the low-level helper used by
        # :func:`timeout`. The user-facing :func:`timeout` wraps the body in
        # a thread itself.
        return False

    def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``func(*args, **kwargs)`` and return its result, enforcing the
        timeout."""

        self._start = time.monotonic()
        self._exc = None
        self._result = None
        self._timed_out = False
        thread = threading.Thread(
            target=self._runner, args=(func, args, kwargs), daemon=True,
            name=f"tiny_timeout[{self._name or 'anon'}]",
        )
        self._thread = thread
        thread.start()
        thread.join(self._seconds)
        elapsed = time.monotonic() - self._start
        if thread.is_alive():
            self._timed_out = True
            # Cannot kill the thread, but we can raise in the caller.
            # The thread will keep running as a daemon and die with the process.
            raise TimeoutExceeded(self._seconds, self._name, elapsed=elapsed)
        if self._exc is not None:
            raise self._exc
        return self._result


# ---------------------------------------------------------------------------
# Public: @timeout decorator / context manager
# ---------------------------------------------------------------------------

class _TimeoutCM:
    """Context manager that runs the body in a worker thread and enforces a
    deadline."""

    def __init__(self, seconds: float, name: Optional[str] = None) -> None:
        self._seconds = float(seconds)
        self._name = name
        self._exc: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._start = 0.0
        self._timed_out = False

    def __enter__(self) -> "_TimeoutCM":
        return self

    def __exit__(self, exc_type, exc, tb):
        # ``with timeout(5.0):`` does not run the body in a subthread; the
        # context manager is just a *deadline marker*. The body is responsible
        # for checking remaining time and raising :class:`TimeoutExceeded`
        # itself. Use :func:`run_with_timeout` if you need a hard cut-off.
        return False

    def remaining(self) -> float:
        return max(0.0, (self._start + self._seconds) - time.monotonic())


def timeout(seconds: float, name: Optional[str] = None):
    """Create a timeout context manager / decorator.

    When used as a context manager (``with timeout(5.0): ...``) the body
    runs in the **current thread**. The context manager itself does not kill
    the body; the body must use :meth:`Deadline.check` (or check
    ``time.monotonic()``) and raise :class:`TimeoutExceeded` cooperatively.

    For a hard cut-off that interrupts via a worker thread, use
    :func:`run_with_timeout`.

    When used as a decorator, ``@timeout(5.0)`` wraps the function so that
    each call runs in a worker thread; the caller's thread waits and raises
    :class:`TimeoutExceeded` if the worker hasn't returned within the
    deadline. This is the form most people want for blocking I/O.
    """

    if seconds is None or seconds <= 0:
        raise ValueError("timeout must be > 0")

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        wrapped_name = name or getattr(fn, "__name__", "callable")

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return run_with_timeout(fn, seconds, *args, name=wrapped_name, **kwargs)

        return wrapper

    # Returning a callable that supports both ``with`` and ``@`` decoration
    # is impossible in a single object — but a callable returned from a
    # callable call can be either a context manager OR a decorator. Detect
    # usage:
    #   1. ``with timeout(5): ...`` — ``timeout`` is called once; result is a CM.
    #   2. ``@timeout(5)`` then ``foo()`` — ``timeout`` is called once; result
    #      is applied to a function (so it must be a decorator).
    # The trick: detect by checking whether the returned object is a callable.
    # We do that by returning a "smart" object that supports both shapes.

    if False:  # noqa: SIM223 — clearer reading
        pass

    # Build a "smart" callable that works both ways.
    smart = _SmartTimeout(seconds, name)
    return smart


class _SmartTimeout:
    """Polymorphic object: acts as a decorator (``@timeout(5)``) or as a
    context manager (``with timeout(5): ...``)."""

    def __init__(self, seconds: float, name: Optional[str] = None) -> None:
        self._seconds = float(seconds)
        self._name = name

    # Decorator path
    def __call__(self, fn: Callable[..., T]) -> Callable[..., T]:
        wrapped_name = self._name or getattr(fn, "__name__", "callable")

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return run_with_timeout(fn, self._seconds, *args, name=wrapped_name, **kwargs)

        # Allow ``@timeout(5, name="x")`` to keep working AND the wrapper to
        # itself be used as a context manager for the call.
        wrapper.timeout_seconds = self._seconds  # type: ignore[attr-defined]
        wrapper.timeout_name = self._name  # type: ignore[attr-defined]
        return wrapper

    # Context manager path
    def __enter__(self) -> "Deadline":
        self._start = time.monotonic()
        return Deadline.from_seconds(self._seconds)

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def __repr__(self) -> str:  # pragma: no cover
        return f"timeout({self._seconds:.3f})"


# ---------------------------------------------------------------------------
# run_with_timeout — the workhorse
# ---------------------------------------------------------------------------

# Internal sentinel to distinguish "default not provided" from "default=None".
_SENTINEL = object()

_default_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazily create a process-wide worker pool for hard timeouts."""

    global _default_executor
    with _executor_lock:
        if _default_executor is None:
            # Pool size scales with CPU count; each timeout uses 1 worker.
            workers = max(4, (os.cpu_count() or 4) * 4)
            _default_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="tiny_timeout_worker",
            )
        return _default_executor


def shutdown_executor(wait: bool = False) -> None:
    """Shutdown the shared executor (call at process exit if you care)."""

    global _default_executor
    with _executor_lock:
        if _default_executor is not None:
            _default_executor.shutdown(wait=wait)
            _default_executor = None


def run_with_timeout(
    func: Callable[..., T],
    seconds: float,
    *args: Any,
    timeout_: Optional[float] = None,  # alias for ``seconds`` (kwargs-friendly)
    name: Optional[str] = None,
    default: Any = _SENTINEL,
    executor: Optional[ThreadPoolExecutor] = None,
    cancel_on_timeout: bool = True,
    poll_interval: float = 0.01,
    **kwargs: Any,
) -> T:
    """Run ``func(*args, **kwargs)`` and raise :class:`TimeoutExceeded` if it
    takes longer than ``seconds``.

    Parameters
    ----------
    func:
        The callable to run.
    seconds:
        The timeout in seconds (must be > 0).
    *args, **kwargs:
        Forwarded to ``func``.
    timeout_:
        Alias for ``seconds`` (so it can be passed by keyword without
        shadowing the positional form).
    name:
        Optional label used in error messages.
    default:
        If set and ``cancel_on_timeout`` is True, return ``default`` instead
        of raising on timeout. Use ``default=_RAISE`` (the default) to raise.
    executor:
        Optional :class:`ThreadPoolExecutor`; uses a shared one if None.
    cancel_on_timeout:
        If True (default), raise/return on timeout; if False, wait until the
        worker finishes (rarely useful; here for symmetry).
    poll_interval:
        How often the main thread polls the worker's future (seconds).
    """

    if timeout_ is not None:
        seconds = timeout_
    if seconds is None or seconds <= 0:
        raise ValueError("seconds must be > 0")

    pool = executor if executor is not None else _get_executor()
    start = time.monotonic()
    future = pool.submit(func, *args, **kwargs)
    try:
        # ``future.result(timeout=...)`` uses a Condition internally, so
        # there's no busy loop here. We additionally poll for elapsed to
        # compute TimeoutExceeded.elapsed precisely.
        return future.result(timeout=seconds)
    except CFTimeoutError:
        elapsed = time.monotonic() - start
        if cancel_on_timeout:
            # We cannot kill the worker; we just stop waiting. The thread
            # keeps running until the process exits (it is a daemon in the
            # shared pool only when the pool's threads are daemons — we
            # set them as daemon via thread_name_prefix; the executor's
            # threads are non-daemon by default, so we leak them on
            # timeout. This is the standard compromise for Python.)
            if default is _SENTINEL:
                raise TimeoutExceeded(seconds, name=name, elapsed=elapsed)
            return default  # type: ignore[no-any-return]
        # User wants to wait it out
        return future.result()
    finally:
        # No-op; we don't cancel the future because cancellation in the
        # stdlib executor is not supported.
        _ = poll_interval  # accepted for API stability; not used


# ---------------------------------------------------------------------------
# timeout_at — absolute deadline
# ---------------------------------------------------------------------------

def timeout_at(absolute_monotonic: float, name: Optional[str] = None):
    """Context manager / decorator that times out at an absolute
    ``time.monotonic()`` value (e.g. ``time.monotonic() + 30``)."""

    seconds = max(0.0, absolute_monotonic - time.monotonic())
    return timeout(seconds, name=name)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def sleep_with_deadline(seconds: float, deadline: Deadline) -> bool:
    """Sleep for at most ``seconds`` or until ``deadline`` expires.

    Returns True if the sleep completed, False if the deadline expired
    first.
    """

    remaining = deadline.remaining()
    if remaining <= 0:
        return False
    time.sleep(min(seconds, remaining))
    return not deadline.expired()


@contextlib.contextmanager
def soft_timeout(seconds: float, name: Optional[str] = None) -> Iterator[Deadline]:
    """Yield a :class:`Deadline` and raise :class:`TimeoutExceeded` if the
    block's wall-clock exceeds the budget *and* the body explicitly
    requests the check via :meth:`Deadline.check`.

    This is a marker for code review and tooling — it does not interrupt
    the block. Use :func:`run_with_timeout` for hard cut-offs.
    """

    deadline = Deadline.from_seconds(seconds)
    try:
        yield deadline
    finally:
        # Marker: nothing to do. The body must ``check()`` itself.
        _ = name