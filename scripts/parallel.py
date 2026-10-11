"""Run jobs on a thread pool, each one after the jobs it depends on.

Threads rather than processes: the heavy work in the pipeline (downloads,
hashing, zlib, file I/O, waiting on child processes) releases the GIL, so
threads run it in parallel without the start-up and data-copying costs of
separate processes.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait


def default_jobs(count: int) -> int:
    """Workers for count jobs: at least 4 (downloads wait on the network), at most count."""
    return max(1, min(count, max(4, os.cpu_count() or 1)))


class Logger:
    """Thread-safe printing with a per-job prefix."""

    def __init__(self, write: Callable[[str], None] = print):
        self._write = write
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        with self._lock:
            self._write(message)

    def prefixed(self, prefix: str) -> Callable[[str], None]:
        return lambda message: self(f"[{prefix}] {message}")


def run_ordered(
    names: Iterable[str],
    depends: Mapping[str, Iterable[str]],
    work: Callable[[str], None],
    jobs: int,
) -> tuple[dict[str, BaseException], list[str]]:
    """Run work(name) for every name, each after all of its dependencies succeeded.

    Dependencies that are not among names are treated as already done. After
    the first failure no new job starts; running jobs finish. Returns the
    failures by name and the names that never started, both in name order.
    """
    pending = list(dict.fromkeys(names))
    waiting = {
        name: {d for d in depends.get(name, ()) if d in pending and d != name}
        for name in pending
    }
    done: set[str] = set()
    failures: dict[str, BaseException] = {}
    running: dict[Future, str] = {}

    jobs = max(1, jobs)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        while True:
            # Submit only while a worker is free, so nothing queues up inside
            # the pool and no job starts after a failure.
            if not failures:
                for name in pending:
                    if len(running) >= jobs:
                        break
                    if name not in running.values() and waiting[name] <= done:
                        running[pool.submit(work, name)] = name
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                name = running.pop(future)
                error = future.exception()
                if error is None:
                    done.add(name)
                else:
                    failures[name] = error
            pending = [
                name for name in pending if name not in done and name not in failures
            ]

    not_started = sorted(name for name in pending if name not in failures)
    if not failures and not_started:
        raise RuntimeError(f"dependency cycle among: {', '.join(not_started)}")
    return dict(sorted(failures.items())), not_started
