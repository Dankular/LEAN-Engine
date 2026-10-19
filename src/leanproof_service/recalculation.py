"""Bounded, process-local continuations for queries stopped by scan budgets."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any, Callable
from uuid import uuid4


JOB_TTL_SECONDS = 30 * 60
MAX_RETAINED_JOBS = 256
MAX_ACTIVE_RECALCULATIONS = 4
MAX_TIMEOUT_SECONDS = 30.0


class RecalculationCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecalculationJob:
    job_id: str
    work: Callable[[float, float], dict[str, Any]]
    input_text: str
    scan_timeout_seconds: float
    total_timeout_seconds: float
    output: str
    created_at: float
    expires_at: float


_jobs: dict[str, RecalculationJob] = {}
_active_recalculations = 0
_lock = Lock()


def create_recalculation_job(
    work: Callable[[float, float], dict[str, Any]],
    *,
    input_text: str,
    scan_timeout_seconds: float,
    total_timeout_seconds: float,
    output: str,
) -> RecalculationJob | None:
    next_scan_timeout = _increase_timeout(scan_timeout_seconds)
    next_total_timeout = _increase_timeout(total_timeout_seconds)
    if next_scan_timeout is None and next_total_timeout is None:
        return None

    scan_timeout = next_scan_timeout or scan_timeout_seconds
    total_timeout = next_total_timeout or total_timeout_seconds
    if next_scan_timeout is not None:
        total_timeout = max(total_timeout, next_scan_timeout)
    now = monotonic()
    job = RecalculationJob(
        job_id=str(uuid4()),
        work=work,
        input_text=input_text,
        scan_timeout_seconds=scan_timeout,
        total_timeout_seconds=min(MAX_TIMEOUT_SECONDS, total_timeout),
        output=output,
        created_at=now,
        expires_at=now + JOB_TTL_SECONDS,
    )
    with _lock:
        _remove_expired(now)
        if len(_jobs) >= MAX_RETAINED_JOBS:
            oldest = min(_jobs.values(), key=lambda item: item.created_at)
            _jobs.pop(oldest.job_id, None)
        _jobs[job.job_id] = job
    return job


def take_recalculation_job(job_id: str) -> RecalculationJob | None:
    global _active_recalculations
    now = monotonic()
    with _lock:
        _remove_expired(now)
        job = _jobs.get(job_id)
        if job is None:
            return None
        if _active_recalculations >= MAX_ACTIVE_RECALCULATIONS:
            raise RecalculationCapacityError("The recalculation service is busy")
        _jobs.pop(job_id, None)
        _active_recalculations += 1
        return job


def finish_recalculation() -> None:
    global _active_recalculations
    with _lock:
        _active_recalculations = max(0, _active_recalculations - 1)


def _increase_timeout(value: float) -> float | None:
    increased = min(MAX_TIMEOUT_SECONDS, max(value * 2, value + 1))
    return increased if increased > value else None


def _remove_expired(now: float) -> None:
    expired = [job_id for job_id, job in _jobs.items() if job.expires_at <= now]
    for job_id in expired:
        _jobs.pop(job_id, None)
