"""Work the web UI asks for. Anything that renders or changes the server runs on the service's worker thread, one job
at a time between library items, so it never races the worker over the shared pipeline context."""

import itertools
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)
KEEP = 200


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobError(Exception):
    """Raised inside a job for a failure to show the user as is, without a stack in the log."""


@dataclass
class Job:
    id: str
    kind: str
    label: str
    run: Callable[[], Any]
    state: JobState = JobState.QUEUED
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "state": self.state.value,
            "created": self.created, "started": self.started, "finished": self.finished,
            "result": self.result, "error": self.error,
        }  # fmt: skip


class Jobs:
    """Keeps the last KEEP jobs. `submit` hands the job to `dispatch`, which must call `execute` on one thread."""

    def __init__(self, dispatch: Callable[[Job], None] | None = None) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.dispatch = dispatch or self.execute

    def submit(self, kind: str, label: str, run: Callable[[], Any]) -> Job:
        with self._lock:
            job = Job(f"{int(time.time())}-{next(self._ids)}", kind, label, run)
            self._jobs[job.id] = job
            while len(self._jobs) > KEEP:
                self._jobs.popitem(last=False)
        self.dispatch(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 50) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs.values()))[:limit]

    def running(self) -> Job | None:
        with self._lock:
            return next((job for job in self._jobs.values() if job.state == JobState.RUNNING), None)

    def execute(self, job: Job) -> None:
        job.state, job.started = JobState.RUNNING, time.time()
        try:
            job.result = job.run()
            job.state = JobState.DONE
        except JobError as exc:
            job.error, job.state = str(exc), JobState.FAILED
        except Exception as exc:
            log.exception("web UI job failed", extra={"job": job.label})
            job.error, job.state = f"{type(exc).__name__}: {exc}", JobState.FAILED
        finally:
            job.finished = time.time()
