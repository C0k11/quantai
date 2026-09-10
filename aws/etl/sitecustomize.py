"""Make multiprocessing locks work on AWS Lambda.

Lambda gives the runtime no POSIX shared memory, so `_multiprocessing.SemLock`
raises `PermissionError: [Errno 13]`. dbt hits this the moment it registers an
adapter (`ConnectionManager.__init__` -> `mp_context.RLock()`), which means
`dbt build` dies on Lambda even though the identical image runs fine under
Docker locally, where /dev/shm exists.

dbt schedules models on threads inside one process, not across processes, so the
lock only needs to be thread-safe. Falling back to `threading` primitives is
therefore semantically correct for this workload.

This file is only baked into the Lambda image, and it patches only when SemLock
is genuinely unavailable, so a local run keeps the real multiprocessing
primitives and behaves exactly as before.
"""
import multiprocessing
import multiprocessing.context
import threading


def _semlock_available() -> bool:
    try:
        multiprocessing.get_context().RLock()
    except (PermissionError, OSError, ImportError):
        return False
    return True


if not _semlock_available():  # pragma: no cover - only true inside Lambda
    _ctx = multiprocessing.context.BaseContext
    _ctx.RLock = lambda self: threading.RLock()
    _ctx.Lock = lambda self: threading.Lock()
    _ctx.Event = lambda self: threading.Event()
    _ctx.Condition = lambda self, lock=None: threading.Condition(lock)
    _ctx.Semaphore = lambda self, value=1: threading.Semaphore(value)
    _ctx.BoundedSemaphore = lambda self, value=1: threading.BoundedSemaphore(value)
