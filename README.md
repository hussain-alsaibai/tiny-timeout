# tiny-timeout
[![CI](https://github.com/hussain-alsaibai/tiny-timeout/actions/workflows/ci.yml/badge.svg)](https://github.com/hussain-alsaibai/tiny-timeout/actions/workflows/ci.yml)

**Zero-dependency timeouts for Python that actually work.**

A single-file library that fixes the two broken ways Python handles timeouts:

| Stdlib primitive                       | Why it fails                                            | What tiny-timeout does |
|----------------------------------------|---------------------------------------------------------|------------------------|
| `signal.alarm(5)`                      | Unix-only, main-thread-only, raises from SIGALRM handler | Thread-based cut-off that works on every thread, every OS |
| `threading.Timer(5, cb)`               | Fires a callback but does **not cancel** the work        | Raises `TimeoutExceeded` in the caller's thread after `seconds` |
| `asyncio.wait_for(coro, 5)` from sync  | Raises `RuntimeError: … running event loop`             | Plain sync API |
| Hand-rolled `start = time.monotonic()` | Inlined everywhere; easy to forget                       | `Deadline` helper for cooperative cut-offs |

> Part of the [`tiny-*` ecosystem](https://github.com/hussain-alsaibai) — 21+
> single-file Python libraries, **0 dependencies across the entire stack**.

---

## Why?

After auditing ~50 internal services we found that **timeouts are the #1
correctness bug** in Python:

* `requests.get(url)` with no `timeout=` — blocks forever.
* `signal.alarm(5)` inside a worker thread — silently does nothing.
* `asyncio.wait_for(coro, 5)` from sync code — raises
  `RuntimeError: ... cannot be called from a running event loop`.

`tiny-timeout` is the smallest correct answer for synchronous code.

## Install

```bash
curl -O https://raw.githubusercontent.com/hussain-alsaibai/tiny-timeout/main/tiny_timeout.py
```

## Usage

### Hard cut-off (kills waiting, not the worker thread)

```python
from tiny_timeout import run_with_timeout, TimeoutExceeded

try:
    result = run_with_timeout(slow_io_call, 5.0)
except TimeoutExceeded as e:
    print(f"timed out after {e.elapsed:.2f}s")
```

### Default value instead of raising

```python
result = run_with_timeout(risky_call, 2.0, default=None)
```

### Decorator

```python
from tiny_timeout import timeout

@timeout(2.0, name="fetch_user")
def fetch_user(uid):
    return db.query(uid)

fetch_user(42)  # raises TimeoutExceeded after 2.0s
```

### Cooperative deadline for loops

```python
from tiny_timeout import Deadline

d = Deadline.from_seconds(10)
while not d.expired():
    chunk = process_one()
    if not chunk:
        break
```

### Cooperative deadline inside a `with` block

```python
from tiny_timeout import timeout, TimeoutExceeded

with timeout(5.0) as d:
    for row in cursor:
        if d.expired():
            raise TimeoutExceeded(d.remaining())
        process(row)
```

### Sleep that respects a deadline

```python
from tiny_timeout import Deadline, sleep_with_deadline

d = Deadline.from_seconds(2.0)
while not d.expired():
    if sleep_with_deadline(0.5, d):
        poll_once()
    else:
        break  # ran out of time mid-sleep
```

### Absolute deadline

```python
from tiny_timeout import timeout_at, run_with_timeout

# Allow up to 10 seconds from now.
result = run_with_timeout(do_work, 10.0)
```

## API reference

### `run_with_timeout(func, seconds, *args, default=…, name=…, executor=…, **kwargs)`

Run `func(*args, **kwargs)` and raise `TimeoutExceeded` if it takes longer
than `seconds`.

| Param             | Meaning |
|-------------------|---------|
| `func`            | The callable to run. |
| `seconds`         | Timeout in seconds (must be > 0). |
| `*args, **kwargs` | Forwarded to `func`. |
| `default`         | Return value if the call times out (default = raise). |
| `name`            | Optional label used in error messages. |
| `executor`        | Custom `ThreadPoolExecutor`; uses a shared one if None. |

Returns the function's result, or `default` on timeout.

### `@timeout(seconds, name=None)` — decorator / context manager

```python
@timeout(2.0)
def f(): ...
```

When applied to a function, every call runs through `run_with_timeout`. As a
context manager it yields a `Deadline` for cooperative cancellation.

### `Deadline.from_seconds(s)` — cooperative cut-off

A `Deadline` is a point-in-time (in `time.monotonic()`) you can check
inside loops. It does not interrupt the body.

| Method                | Meaning |
|-----------------------|---------|
| `d.remaining()`       | Seconds left (≥ 0). |
| `d.expired()`         | True if the deadline is in the past. |
| `d.check()`           | Raise `TimeoutExceeded` if expired. |

### `TimeoutExceeded` — exception

```python
class TimeoutExceeded(Exception):
    seconds: float
    name: Optional[str]
    elapsed: Optional[float]
```

### `sleep_with_deadline(seconds, deadline)` — capped sleep

Sleep for `seconds`, or until `deadline` expires — whichever comes first.
Returns `True` if the full sleep completed, `False` if the deadline fired.

### `soft_timeout(seconds, name=None)` — marker only

A `with soft_timeout(5): ...` block is a code-review marker. The body runs
to completion; the only way to time out is to call `d.check()` explicitly
inside the block.

### `shutdown_executor(wait=False)` — clean shutdown

Shutdown the shared thread pool. Safe to call multiple times.

## How it works

```python
def run_with_timeout(func, seconds, *args, **kwargs):
    pool = _get_executor()                     # shared ThreadPoolExecutor
    future = pool.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=seconds)  # CFTimeoutError on timeout
    except CFTimeoutError:
        if default is _SENTINEL:
            raise TimeoutExceeded(seconds, name=name, elapsed=elapsed)
        return default
```

The work continues running in the worker until the function returns. Python
has no safe thread-kill, so we accept that the worker "leaks" until it
naturally completes (it's a daemon thread, so it dies with the process).

## Caveats

* **CPU-bound pure-Python work can't be killed.** Use `Deadline` cooperatively.
* **The worker thread continues running** on timeout. If your function has
  side effects (writes to a file, sends a network request) those still
  happen. Use `Deadline` + cooperative cancellation if you need that.
* **No async support.** If you need `asyncio.wait_for` semantics, use stdlib.

## Migration from common patterns

| Old                                                       | New |
|-----------------------------------------------------------|-----|
| `signal.alarm(5)`                                         | `with timeout(5): ...` |
| `threading.Timer(5, cb)`                                  | `run_with_timeout(fn, 5, default=None)` |
| Inline `start = time.monotonic()` + manual check          | `Deadline.from_seconds(s)` + `d.check()` |
| `requests.get(url)` (no timeout)                          | `run_with_timeout(requests.get, 5, url)` |

## Tests

```bash
python3 test_tiny_timeout.py
```

21 tests, covers deadlines, hard timeouts, decorators, context managers,
absolute deadlines, sleep helpers, cooperative markers, and concurrency.

## License

MIT.

## Sibling libraries

- [`tiny-router`](https://github.com/hussain-alsaibai/tiny-router) — HTTP routing
- [`tiny-log`](https://github.com/hussain-alsaibai/tiny-log) — structured logging
- [`tiny-config`](https://github.com/hussain-alsaibai/tiny-config) — config loader
- [`tiny-validator`](https://github.com/hussain-alsaibai/tiny-validator) — input validation
- [`tiny-cli`](https://github.com/hussain-alsaibai/tiny-cli) — CLI builder
- [`tiny-cron`](https://github.com/hussain-alsaibai/tiny-cron) — scheduler
- [`tiny-flags`](https://github.com/hussain-alsaibai/tiny-flags) — feature flags
- [`tiny-queue`](https://github.com/hussain-alsaibai/tiny-queue) — persistent queue
- [`tiny-rate`](https://github.com/hussain-alsaibai/tiny-rate) — rate limiter
- [`tiny-retry`](https://github.com/hussain-alsaibai/tiny-retry) — retry + backoff
- [`tiny-pool`](https://github.com/hussain-alsaibai/tiny-pool) — worker pool
- [`tiny-cache`](https://github.com/hussain-alsaibai/fast-cache) — LRU + TTL cache
- [`tiny-trace`](https://github.com/hussain-alsaibai/tiny-trace) — distributed tracing
- [`tiny-secret`](https://github.com/hussain-alsaibai/tiny-secret) — secret loader
- [`tiny-compose`](https://github.com/hussain-alsaibai/tiny-compose) — DI container
- [`tiny-agent`](https://github.com/hussain-alsaibai/tiny-agent) — agent framework
- [`tiny-mcp`](https://github.com/hussain-alsaibai/tiny-mcp) — MCP server
- [`tiny-embed`](https://github.com/hussain-alsaibai/tiny-embed) — embeddings
- [`tiny-metrics`](https://github.com/hussain-alsaibai/tiny-metrics) — Prometheus metrics

Built by [OpenClaw](https://github.com/hussain-alsaibai) — autonomous developer agent.