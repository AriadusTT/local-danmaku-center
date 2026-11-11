from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..db import SessionFactory
from ..jobs.scanner import ScanBusy, scan_manager
from ..models import Library, ScanRun, ScanRunLibrary
from ..schemas import ScanRequest

router = APIRouter(prefix="/api/scan", tags=["scan"])


async def _serialize_run(run: ScanRun) -> dict:
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(ScanRunLibrary, Library.name)
                .join(Library, Library.id == ScanRunLibrary.library_id, isouter=True)
                .where(ScanRunLibrary.run_id == run.id)
            )
        ).all()

    libraries = [
        {
            "id": link.library_id,
            "name": name or link.library_id,
            "total": link.total,
            "processed": link.processed,
            "added": link.added,
            "updated": link.updated,
            "removed": link.removed,
            "unchanged": link.unchanged,
            "state": link.state,
            "error": link.error,
            "percent": round(link.processed / link.total * 100, 1) if link.total else 0.0,
        }
        for link, name in rows
    ]

    return {
        "run_id": run.id,
        "mode": run.mode,
        "state": run.state,
        "total": run.total,
        "processed": run.processed,
        "percent": round(run.processed / run.total * 100, 1) if run.total else 0.0,
        "added": run.added,
        "updated": run.updated,
        "removed": run.removed,
        "unchanged": run.unchanged,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error": run.error,
        "libraries": libraries,
    }


@router.post("")
async def start_scan(payload: ScanRequest) -> dict:
    try:
        run_id = await scan_manager.start(payload.mode, payload.library_ids)
    except ScanBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"run_id": run_id}


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)
            )
        ).scalars().all()
    return {
        "items": [
            {
                "run_id": r.id,
                "mode": r.mode,
                "state": r.state,
                "total": r.total,
                "processed": r.processed,
                "added": r.added,
                "updated": r.updated,
                "removed": r.removed,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "error": r.error,
            }
            for r in rows
        ]
    }


@router.get("/current")
async def current_run() -> dict:
    return {
        "running": scan_manager.running,
        "run_id": scan_manager.current_run_id,
    }


@router.get("/{run_id}")
async def get_run(run_id: int) -> dict:
    async with SessionFactory() as session:
        run = await session.get(ScanRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="扫描记录不存在")
    return await _serialize_run(run)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: int) -> dict:
    ok = await scan_manager.cancel(run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="该任务不在运行中")
    return {"canceled": True, "run_id": run_id}
