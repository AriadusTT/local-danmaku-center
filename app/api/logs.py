from __future__ import annotations

from fastapi import APIRouter, Query

from .. import logs as logbuf

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    after: int = Query(0, ge=0),
    limit: int = Query(300, ge=1, le=800),
) -> dict:
    """增量取日志：前端记住 last_seq，每次只拉新的。"""
    return logbuf.since(after, limit)
