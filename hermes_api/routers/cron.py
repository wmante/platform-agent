"""
Cron router.

Endpoints
---------
GET    /cron         list jobs
POST   /cron         create job
PUT    /cron/{id}    update job
DELETE /cron/{id}    delete job
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from hermes_api.deps import require_auth
from hermes_api.schemas import CronCreate, CronResponse, CronUpdate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cron"])


def _job_to_response(job: dict) -> CronResponse:
    sched = job.get("schedule", {})
    schedule_display = job.get("schedule_display") or (
        sched.get("display") if isinstance(sched, dict) else str(sched)
    )
    return CronResponse(
        job_id=job["id"],
        name=job.get("name", ""),
        schedule=schedule_display,
        enabled=job.get("enabled", True),
        next_run_at=job.get("next_run_at"),
        last_run_at=job.get("last_run_at"),
        last_status=job.get("last_status"),
    )


@router.get("/cron", response_model=list[CronResponse])
async def list_cron_jobs(
    include_disabled: bool = False,
    _=Depends(require_auth),
):
    from cron.jobs import list_jobs
    jobs = list_jobs(include_disabled=include_disabled)
    return [_job_to_response(j) for j in jobs]


@router.post("/cron", response_model=CronResponse, status_code=201)
async def create_cron_job(body: CronCreate, _=Depends(require_auth)):
    from cron.jobs import create_job
    try:
        job = create_job(
            prompt=body.prompt,
            schedule=body.schedule,
            name=body.name,
            deliver=body.deliver,
            repeat=body.repeat,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _job_to_response(job)


@router.put("/cron/{job_id}", response_model=CronResponse)
async def update_cron_job(job_id: str, body: CronUpdate, _=Depends(require_auth)):
    from cron.jobs import update_job

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No updates provided")

    try:
        job = update_job(job_id, updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if job is None:
        raise HTTPException(status_code=404, detail=f"Cron job '{job_id}' not found")
    return _job_to_response(job)


@router.delete("/cron/{job_id}", status_code=204)
async def delete_cron_job(job_id: str, _=Depends(require_auth)):
    from cron.jobs import remove_job
    removed = remove_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Cron job '{job_id}' not found")
