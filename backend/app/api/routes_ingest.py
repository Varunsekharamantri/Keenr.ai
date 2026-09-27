import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from ..db.database import get_db, SessionLocal
from ..models.schema import Company, IngestionJob, IngestionTriggerRequest
from ..ingestion.pipeline import IngestionPipeline
from .. import scheduler as scheduler_module

router = APIRouter(prefix="/ingest", tags=["Ingestion Pipeline"])
pipeline = IngestionPipeline()


def _run_batch_in_background(sector_filter: Optional[str], limit_per_company: int,
                             only_uncovered: bool = False):
    db = SessionLocal()
    try:
        pipeline.run_batch_ingestion(
            db=db,
            limit_per_company=limit_per_company,
            sector_filter=sector_filter,
            only_uncovered=only_uncovered,
        )
    finally:
        db.close()

@router.post("/run")
def trigger_ingestion(
    payload: IngestionTriggerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Trigger live ingestion for a specific company or sector.
    """
    if payload.company_id or payload.ticker or payload.cik:
        query = db.query(Company)
        if payload.company_id:
            query = query.filter(Company.id == payload.company_id)
        elif payload.ticker:
            query = query.filter(Company.ticker == payload.ticker.upper())
        elif payload.cik:
            query = query.filter(Company.cik == payload.cik.zfill(10))

        company = query.first()
        if not company:
            raise HTTPException(status_code=404, detail="Target company not found in database.")

        job = IngestionJob(
            job_type="single_company",
            target_ticker=company.ticker,
            target_cik=company.cik,
            status="running",
            started_at=datetime.datetime.utcnow()
        )
        db.add(job)
        db.commit()

        # Run synchronous single company ingestion for immediate feedback
        try:
            result = pipeline.run_company_ingestion(
                db=db,
                company=company,
                source_type=payload.source_type or "all",
                limit=payload.limit_docs or 5,
                job_id=job.id
            )
            job.status = "completed"
            job.items_ingested = result["docs_ingested"]
            job.events_extracted = result["events_extracted"]
        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            raise
        finally:
            job.finished_at = datetime.datetime.utcnow()
            db.commit()

        return {
            "status": "success",
            "message": f"Ingestion completed for {company.name} ({company.ticker or 'news-only'})",
            "result": result
        }
    else:
        # Run batch pipeline across the company universe (or a sector) in the
        # background — with 600+ companies this can take many minutes.
        only_uncovered = (payload.mode or "").lower() == "uncovered"
        background_tasks.add_task(
            _run_batch_in_background,
            payload.sector,
            payload.limit_docs or 2,
            only_uncovered,
        )
        scope = "companies with no data yet" if only_uncovered else "the company universe"
        return {
            "status": "queued",
            "message": f"Batch ingestion started in the background across {scope}. "
                       "Poll /api/ingest/jobs for progress."
        }

@router.get("/coverage")
def coverage_status(sector: Optional[str] = "BFSI", db: Session = Depends(get_db)):
    """How much of the universe has any data — what the backfill run targets."""
    from sqlalchemy import select, func
    from ..models.schema import Event

    q = db.query(Company)
    if sector and sector.lower() != "all":
        q = q.filter(Company.sector == sector)
    total = q.count()
    covered_ids = db.query(Event.company_id).distinct().subquery()
    uncovered = q.filter(~Company.id.in_(select(covered_ids.c.company_id))).count()
    return {
        "sector": sector,
        "total_companies": total,
        "covered": total - uncovered,
        "uncovered": uncovered,
        "coverage_pct": round(100 * (total - uncovered) / total, 1) if total else 0.0,
    }


@router.get("/jobs")
def list_jobs(limit: int = 10, db: Session = Depends(get_db)):
    jobs = db.query(IngestionJob).order_by(IngestionJob.started_at.desc()).limit(limit).all()
    return [j.to_dict() for j in jobs]

@router.get("/schedule")
def get_schedule_status():
    return scheduler_module.get_scheduler_status()

@router.get("/exa-usage")
def get_exa_usage():
    """Self-imposed monthly Exa request budget — the pipeline hard-stops calling
    Exa once this is used up, so it never spends money past this cap."""
    return pipeline.exa_adapter.get_usage_status()

@router.get("/ir-usage")
def get_ir_usage():
    """Self-imposed monthly Exa request budget for the IR press-release
    adapter — tracked separately from news-Exa's budget (see exa-usage)."""
    return pipeline.ir_adapter.get_usage_status()

@router.get("/career-usage")
def get_career_usage():
    """Self-imposed monthly Exa request budget for the career-page/hiring
    adapter — tracked separately from news-Exa and IR-Exa's budgets."""
    return pipeline.career_adapter.get_usage_status()
