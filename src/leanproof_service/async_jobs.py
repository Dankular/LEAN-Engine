from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Callable
from uuid import uuid4


JOB_TTL_SECONDS = 30 * 60
MAX_ACTIVE_JOBS = 16
MAX_RETAINED_JOBS = 256
_WORKERS = 4


class AsyncJobCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AsyncPodJob:
    job_id: str
    future: Future[dict[str, Any]]
    pod_id: str
    pod_title: str
    scanner: str
    output: str
    created_at: float
    expires_at: float


_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="full-results")
_jobs: dict[str, AsyncPodJob] = {}
_lock = Lock()


def submit_async_pod(
    work: Callable[[], dict[str, Any]],
    *,
    pod_id: str,
    pod_title: str,
    scanner: str,
    output: str,
) -> AsyncPodJob:
    now = monotonic()
    with _lock:
        _remove_expired_completed(now)
        active = sum(not job.future.done() for job in _jobs.values())
        if active >= MAX_ACTIVE_JOBS:
            raise AsyncJobCapacityError("The asynchronous query queue is full")
        if len(_jobs) >= MAX_RETAINED_JOBS:
            completed = sorted(
                (job for job in _jobs.values() if job.future.done()),
                key=lambda job: job.created_at,
            )
            for job in completed[: len(_jobs) - MAX_RETAINED_JOBS + 1]:
                _jobs.pop(job.job_id, None)
        if len(_jobs) >= MAX_RETAINED_JOBS:
            raise AsyncJobCapacityError("The asynchronous result store is full")

        job_id = str(uuid4())
        future = _executor.submit(work)
        job = AsyncPodJob(
            job_id=job_id,
            future=future,
            pod_id=pod_id,
            pod_title=pod_title,
            scanner=scanner,
            output=output,
            created_at=now,
            expires_at=now + JOB_TTL_SECONDS,
        )
        _jobs[job_id] = job
        return job


def get_async_pod_job(job_id: str) -> AsyncPodJob | None:
    with _lock:
        _remove_expired_completed(monotonic())
        return _jobs.get(job_id)


def discard_async_pod_job(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job.future.done():
            _jobs.pop(job_id, None)


def _remove_expired_completed(now: float) -> None:
    expired = [
        job_id
        for job_id, job in _jobs.items()
        if job.expires_at <= now and job.future.done()
    ]
    for job_id in expired:
        _jobs.pop(job_id, None)
