from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
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


@dataclass
class AsyncQueryJob:
    job_id: str
    future: Future[dict[str, Any]]
    input_text: str
    pod_id: str
    pod_title: str
    scanner: str
    output: str
    created_at: float
    expires_at: float
    prepare_query_result: Callable[[Any, dict[str, Any]], None] | None = None
    query_result_prepared: bool = False
    query_result_lock: Lock = field(default_factory=Lock, repr=False)


_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="full-results")
_jobs: dict[str, AsyncQueryJob] = {}
_lock = Lock()


def submit_async_query(
    work: Callable[[], dict[str, Any]],
    *,
    input_text: str,
    pod_id: str,
    pod_title: str,
    scanner: str,
    output: str,
    prepare_query_result: Callable[[Any, dict[str, Any]], None] | None = None,
) -> AsyncQueryJob:
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
        job = AsyncQueryJob(
            job_id=job_id,
            future=future,
            input_text=input_text,
            pod_id=pod_id,
            pod_title=pod_title,
            scanner=scanner,
            output=output,
            created_at=now,
            expires_at=now + JOB_TTL_SECONDS,
            prepare_query_result=prepare_query_result,
        )
        _jobs[job_id] = job
        return job


def get_async_query_job(job_id: str) -> AsyncQueryJob | None:
    with _lock:
        _remove_expired_completed(monotonic())
        return _jobs.get(job_id)


def discard_async_query_job(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job.future.done():
            _jobs.pop(job_id, None)


def prepare_async_query_result(
    job: AsyncQueryJob,
    request: Any,
    payload: dict[str, Any],
) -> None:
    """Apply query-level response metadata once before returning the shared result."""
    with job.query_result_lock:
        if job.query_result_prepared:
            return
        if job.prepare_query_result is not None:
            job.prepare_query_result(request, payload)
        job.query_result_prepared = True


def _remove_expired_completed(now: float) -> None:
    expired = [
        job_id
        for job_id, job in _jobs.items()
        if job.expires_at <= now and job.future.done()
    ]
    for job_id in expired:
        _jobs.pop(job_id, None)
