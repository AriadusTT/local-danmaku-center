from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import case, func, or_, select

from ..db import SessionFactory
from ..emby import EmbyError
from ..models import Library, MediaItem, SeriesMeta
from ..runtime import load_config, make_probe_client

router = APIRouter(prefix="/api/media", tags=["media"])


def _serialize(row: MediaItem, library_name: str | None) -> dict:
    return {
        "emby_id": row.emby_id,
        "library_id": row.library_id,
        "library_name": library_name,
        "item_type": row.item_type,
        "name": row.name,
        "series_name": row.series_name,
        "series_id": row.series_id,
        "season": row.season,
        "episode": row.episode,
        "year": row.year,
        "path": row.path,
        "scan_state": row.scan_state,
        "match_status": row.match_status,
        "episode_id": row.episode_id,
        "danmaku_status": row.danmaku_status,
        "danmaku_count": row.danmaku_count,
        "last_seen_at": row.last_seen_at,
        "date_last_saved": row.date_last_saved,
    }


def _group_key(
    library_id: str | None,
    series_id: str | None,
    tmdb_id: str | None,
    emby_id: str,
) -> str:
    """归并键：有 TMDB 按 TMDB 合并，没有就按 Emby 的 Series 各自成卡。"""
    if tmdb_id:
        return f"t:{library_id}:{tmdb_id}"
    if series_id:
        return f"s:{series_id}"
    return f"e:{emby_id}"


def _dedupe_episodes(items: list[dict]) -> list[dict]:
    """同一集被两个 Series 记录各存一次时只留一条。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for it in items:
        if it["season"] is not None and it["episode"] is not None:
            marker = (it["season"], it["episode"])
            if marker in seen:
                continue
            seen.add(marker)
        out.append(it)
    return out


def _filters(
    library_id: str | None,
    q: str | None,
    include_removed: bool,
) -> list:
    conditions = []
    if library_id:
        conditions.append(MediaItem.library_id == library_id)
    if not include_removed:
        conditions.append(MediaItem.scan_state != "removed")
    if q:
        like = f"%{q}%"
        conditions.append(
            or_(
                MediaItem.name.like(like),
                MediaItem.series_name.like(like),
                MediaItem.path.like(like),
            )
        )
    return conditions


@router.get("")
async def list_media(
    library_id: str | None = None,
    scan_state: str | None = None,
    match_status: str | None = None,
    danmaku_status: str | None = None,
    q: str | None = None,
    include_removed: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict:
    conditions = _filters(library_id, q, include_removed)
    if scan_state:
        conditions.append(MediaItem.scan_state == scan_state)
    if match_status:
        conditions.append(MediaItem.match_status == match_status)
    if danmaku_status:
        conditions.append(MediaItem.danmaku_status == danmaku_status)

    async with SessionFactory() as session:
        count_stmt = select(func.count()).select_from(MediaItem)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = (await session.execute(count_stmt)).scalar_one()

        stmt = (
            select(MediaItem, Library.name)
            .join(Library, Library.id == MediaItem.library_id, isouter=True)
            .order_by(
                MediaItem.date_last_saved.desc(),
                MediaItem.series_name,
                MediaItem.season,
                MediaItem.episode,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if conditions:
            stmt = stmt.where(*conditions)
        rows = (await session.execute(stmt)).all()

    return {
        "items": [_serialize(item, lib_name) for item, lib_name in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/groups")
async def list_groups(
    library_id: str | None = None,
    q: str | None = None,
    include_removed: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=100),
) -> dict:
    """按「一部剧 / 一部电影」分组，供卡片墙使用。

    剧集归并规则：
      - 有 TMDB id 的，按 **Tmdb + 所属库** 合并 —— Emby 里因文件夹改名留下的
        重复 Series 记录，它们的 Tmdb 是同一个，这样才会并成一张卡；
      - 没有 TMDB id 的，按 Emby 的 Series 条目各自成卡，**不做名字猜测**，
        宁可多一张卡，也不把两部同名作品揉在一起。
    电影每条自成一组。
    """
    conditions = _filters(library_id, q, include_removed)

    async with SessionFactory() as session:
        series_rows = (
            await session.execute(
                select(
                    MediaItem.library_id,
                    MediaItem.series_id,
                    MediaItem.series_name,
                    func.min(MediaItem.emby_id),
                    func.count(),
                    func.max(MediaItem.year),
                    func.sum(case((MediaItem.match_status == "matched", 1), else_=0)),
                    func.sum(case((MediaItem.danmaku_status == "cached", 1), else_=0)),
                    func.max(MediaItem.date_last_saved),
                    func.max(MediaItem.last_seen_at),
                )
                .where(MediaItem.item_type == "Episode", *conditions)
                .group_by(MediaItem.library_id, MediaItem.series_id, MediaItem.series_name)
            )
        ).all()

        movie_rows = (
            await session.execute(
                select(
                    MediaItem.emby_id,
                    MediaItem.name,
                    MediaItem.year,
                    MediaItem.library_id,
                    MediaItem.match_status,
                    MediaItem.danmaku_status,
                    MediaItem.scan_state,
                    MediaItem.date_last_saved,
                    MediaItem.last_seen_at,
                ).where(MediaItem.item_type == "Movie", *conditions)
            )
        ).all()

        all_sids = {row[1] for row in series_rows if row[1]}
        tmdb_by_sid: dict[str, str] = {}
        if all_sids:
            for sid, tmdb in (
                await session.execute(
                    select(SeriesMeta.id, SeriesMeta.tmdb_id).where(
                        SeriesMeta.id.in_(all_sids)
                    )
                )
            ).all():
                if tmdb:
                    tmdb_by_sid[sid] = str(tmdb)

        merged: dict[str, dict] = {}
        for (
            lib_id,
            sid,
            sname,
            first_id,
            total,
            year,
            matched,
            cached,
            date_last_saved,
            last_seen_at,
        ) in series_rows:
            tmdb = tmdb_by_sid.get(sid) if sid else None
            key = _group_key(lib_id, sid, tmdb, first_id)
            g = merged.get(key)
            latest = date_last_saved or last_seen_at
            if g is None:
                merged[key] = {
                    "key": key,
                    "kind": "series",
                    "title": (sname or "").strip() or "未命名剧集",
                    "year": year,
                    "library_id": lib_id,
                    "tmdb_id": tmdb,
                    "cover_id": sid,
                    "total": int(total or 0),
                    "matched": int(matched or 0),
                    "cached": int(cached or 0),
                    "updated_at": latest,
                    "_sids": {sid} if sid else set(),
                    "_anon": set() if sid else {first_id},
                }
            else:
                g["total"] += int(total or 0)
                g["matched"] += int(matched or 0)
                g["cached"] += int(cached or 0)
                g["year"] = g["year"] or year
                if latest and (not g["updated_at"] or latest > g["updated_at"]):
                    g["updated_at"] = latest
                if sid:
                    g["_sids"].add(sid)
                else:
                    g["_anon"].add(first_id)

        groups: list[dict] = list(merged.values())
        for (
            emby_id,
            name,
            year,
            lib_id,
            m_status,
            d_status,
            scan_state,
            date_last_saved,
            last_seen_at,
        ) in movie_rows:
            groups.append(
                {
                    "key": f"m:{emby_id}",
                    "kind": "movie",
                    "title": (name or "").strip() or "未命名影片",
                    "year": year,
                    "library_id": lib_id,
                    "tmdb_id": None,
                    "cover_id": emby_id,
                    "total": 1,
                    "matched": 1 if m_status == "matched" else 0,
                    "cached": 1 if d_status == "cached" else 0,
                    "scan_state": scan_state,
                    "updated_at": date_last_saved or last_seen_at,
                    "_sids": set(),
                    "_anon": set(),
                }
            )

        # 最近更新优先；同时间再按标题稳定排序。
        groups.sort(key=lambda g: (g["title"].casefold(), g["year"] or 0))
        groups.sort(key=lambda g: g.get("updated_at") or "", reverse=True)
        total_groups = len(groups)
        start = (page - 1) * page_size
        page_groups = groups[start : start + page_size]

        page_sids: set[str] = set()
        page_anon: set[str] = set()
        mids: list[str] = []
        for g in page_groups:
            if g["kind"] == "series":
                page_sids |= g["_sids"]
                page_anon |= g["_anon"]
            else:
                mids.append(g["cover_id"])

        clauses = []
        if page_sids:
            clauses.append(MediaItem.series_id.in_(page_sids))
        if page_anon:
            clauses.append(MediaItem.emby_id.in_(page_anon))
        if mids:
            clauses.append(MediaItem.emby_id.in_(mids))

        lib_ids = {g["library_id"] for g in page_groups if g["library_id"]}
        lib_names: dict[str, str] = {}
        if lib_ids:
            for lib in (
                await session.execute(select(Library).where(Library.id.in_(lib_ids)))
            ).scalars():
                lib_names[lib.id] = lib.name

        items_by_sid: dict[str, list[dict]] = {}
        items_by_eid: dict[str, dict] = {}
        if clauses:
            rows = (
                await session.execute(
                    select(
                        MediaItem.emby_id,
                        MediaItem.item_type,
                        MediaItem.library_id,
                        MediaItem.series_id,
                        MediaItem.series_name,
                        MediaItem.name,
                        MediaItem.season,
                        MediaItem.episode,
                        MediaItem.match_status,
                        MediaItem.danmaku_status,
                        MediaItem.danmaku_count,
                        MediaItem.scan_state,
                    )
                    .where(or_(*clauses))
                    .order_by(MediaItem.season, MediaItem.episode)
                )
            ).all()
            for (
                emby_id,
                item_type,
                lib_id,
                sid,
                sname,
                name,
                season,
                episode,
                m_status,
                d_status,
                d_count,
                scan_state,
            ) in rows:
                payload = {
                    "emby_id": emby_id,
                    "series_id": sid,
                    "season": season,
                    "episode": episode,
                    "name": name,
                    "match_status": m_status,
                    "danmaku_status": d_status,
                    "danmaku_count": d_count,
                    "scan_state": scan_state,
                }
                if item_type == "Movie":
                    items_by_eid[emby_id] = payload
                elif sid:
                    items_by_sid.setdefault(sid, []).append(payload)
                else:
                    items_by_eid[emby_id] = payload

        for g in page_groups:
            if g["kind"] == "series":
                items: list[dict] = []
                for sid in g["_sids"]:
                    items.extend(items_by_sid.get(sid, []))
                for eid in g["_anon"]:
                    if eid in items_by_eid:
                        items.append(items_by_eid[eid])
                items.sort(key=lambda it: (it["season"] or 0, it["episode"] or 0))
                items = _dedupe_episodes(items)
                g["items"] = items
                g["total"] = len(items)
                # 封面取「集数最多的那个 Emby Series」，避免拿到只有一集的空壳
                counts: dict[str, int] = {}
                for it in items:
                    if it.get("series_id"):
                        counts[it["series_id"]] = counts.get(it["series_id"], 0) + 1
                if counts:
                    g["cover_id"] = max(counts, key=counts.get)
                elif items:
                    g["cover_id"] = items[0]["emby_id"]
            else:
                payload = items_by_eid.get(g["cover_id"])
                g["items"] = [payload] if payload else []
            g["library_name"] = lib_names.get(g["library_id"] or "")
            g.pop("_sids", None)
            g.pop("_anon", None)

    return {
        "groups": page_groups,
        "total": total_groups,
        "page": page,
        "page_size": page_size,
        "pages": (total_groups + page_size - 1) // page_size,
    }


@router.get("/cover/{item_id}")
async def cover(
    item_id: str,
    w: int = Query(240, ge=60, le=800),
    fallback: str | None = None,
) -> Response:
    """转发 Emby 封面。走服务端是为了不把 API Key 暴露进 img 标签。

    剧集常缺海报，这时用 `fallback`（通常是第一集的 Id）退而取单集剧照。
    """
    cfg = await load_config()
    if not cfg.is_configured:
        raise HTTPException(status_code=404, detail="尚未配置 Emby")

    async with await make_probe_client(cfg) as client:
        try:
            image = await client.get_primary_image(item_id, w)
            if image is None and fallback and fallback != item_id:
                image = await client.get_primary_image(fallback, w)
        except EmbyError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if image is None:
        raise HTTPException(status_code=404, detail="没有封面")
    data, content_type = image
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/stats")
async def stats() -> dict:
    async with SessionFactory() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(MediaItem).where(
                    MediaItem.scan_state != "removed"
                )
            )
        ).scalar_one()
        removed = (
            await session.execute(
                select(func.count()).select_from(MediaItem).where(
                    MediaItem.scan_state == "removed"
                )
            )
        ).scalar_one()
        by_scan = (
            await session.execute(
                select(MediaItem.scan_state, func.count()).group_by(MediaItem.scan_state)
            )
        ).all()
        by_match = (
            await session.execute(
                select(MediaItem.match_status, func.count()).group_by(
                    MediaItem.match_status
                )
            )
        ).all()
        by_library = (
            await session.execute(
                select(Library.id, Library.name, func.count(MediaItem.emby_id))
                .join(MediaItem, MediaItem.library_id == Library.id, isouter=True)
                .where(MediaItem.scan_state != "removed")
                .group_by(Library.id, Library.name)
            )
        ).all()

    return {
        "total": total,
        "removed": removed,
        "by_scan_state": {k: v for k, v in by_scan},
        "by_match_status": {k: v for k, v in by_match},
        "by_library": [
            {"library_id": lib_id, "name": name, "count": count}
            for lib_id, name, count in by_library
        ],
    }
