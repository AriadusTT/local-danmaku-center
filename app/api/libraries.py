from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..db import SessionFactory
from ..emby import EmbyError, is_media_library, library_item_types
from ..library_sync import refresh_libraries
from ..models import Library
from ..schemas import LibraryPatch

router = APIRouter(prefix="/api/libraries", tags=["libraries"])


def _serialize(row: Library) -> dict:
    item_types = library_item_types(row.collection_type)
    return {
        "id": row.id,
        "name": row.name,
        "collection_type": row.collection_type,
        "locations": row.locations,
        "enabled": bool(row.enabled),
        "present": bool(row.present),
        "item_count": row.item_count,
        "last_scan_at": row.last_scan_at,
        "scannable": bool(item_types),
        "item_type": "+".join(item_types),
    }


@router.post("/refresh")
async def refresh() -> dict:
    try:
        return await refresh_libraries()
    except EmbyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("")
async def list_libraries() -> dict:
    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(Library).order_by(Library.present.desc(), Library.name)
            )
        ).scalars().all()
    # 非媒体库（合集/音乐等）一律不对外暴露
    items = [_serialize(row) for row in rows if is_media_library(row.collection_type)]
    return {
        "items": items,
        "enabled_count": sum(1 for i in items if i["enabled"]),
        "scannable_count": sum(1 for i in items if i["scannable"] and i["present"]),
    }


@router.patch("/{library_id}")
async def update_library(library_id: str, payload: LibraryPatch) -> dict:
    async with SessionFactory() as session:
        row = await session.get(Library, library_id)
        if row is None:
            raise HTTPException(status_code=404, detail="媒体库不存在")
        row.enabled = 1 if payload.enabled else 0
        await session.commit()
        return _serialize(row)
