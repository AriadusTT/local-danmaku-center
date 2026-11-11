from __future__ import annotations

import json

from sqlalchemy import func, select

from . import logs
from .db import SessionFactory
from .emby.client import is_media_library
from .models import Library, MediaItem
from .runtime import make_client


async def refresh_libraries() -> dict[str, int]:
    """从 Emby 同步媒体库。

    - 只收「电影 / 剧集 / 混合视频」三类，合集、音乐、家庭视频等直接忽略；
    - 新库默认**不勾选**，由用户在设置页主动选择；
    - 只做发现/改名/标记消失，不会改动用户已经选过的 enabled；
    - 顺手探一次每个库的条目总数，让「约 N 条」在选择库时就是真实数字。
    """
    async with await make_client() as client:
        remote = await client.get_libraries()

    counts: dict[str, int] = {}
    async with await make_client() as client:
        remote = await client.get_libraries()
        # 探量放在同一个 client 生命周期内：轻请求，单库失败不阻断同步
        for lib in remote:
            if not lib.id or not is_media_library(lib.collection_type):
                continue
            total = 0
            for item_type in lib.item_types:
                try:
                    total += await client.count_items(lib.id, item_type)
                except Exception:  # noqa: BLE001
                    pass
            counts[lib.id] = total

    added = renamed = removed = skipped = 0
    seen: set[str] = set()

    async with SessionFactory() as session:
        rows = (await session.execute(select(Library))).scalars().all()
        existing = {row.id: row for row in rows}

        for lib in remote:
            if not lib.id:
                continue
            if not is_media_library(lib.collection_type):
                skipped += 1
                continue

            total = counts.get(lib.id, 0)
            seen.add(lib.id)
            locations = json.dumps(lib.locations, ensure_ascii=False)
            row = existing.get(lib.id)
            if row is None:
                session.add(
                    Library(
                        id=lib.id,
                        name=lib.name,
                        collection_type=lib.collection_type,
                        locations=locations,
                        enabled=0,  # 默认不勾选，交给用户决定
                        present=1,
                        item_count=total,
                    )
                )
                added += 1
            else:
                if row.name != lib.name:
                    renamed += 1
                row.name = lib.name
                row.collection_type = lib.collection_type
                row.locations = locations
                row.present = 1
                row.item_count = total

        for lib_id, row in existing.items():
            if lib_id in seen:
                continue
            if not is_media_library(row.collection_type):
                # 历史遗留的非媒体库（如合集）：没有关联条目就清掉，别留在列表里
                has_items = await session.scalar(
                    select(func.count())
                    .select_from(MediaItem)
                    .where(MediaItem.library_id == lib_id)
                )
                if not has_items:
                    await session.delete(row)
                    skipped += 1
                else:
                    row.present = 0
                continue
            if row.present:
                row.present = 0
                removed += 1

        await session.commit()

    bits = [f"共 {len(seen)} 个可扫描库"]
    if added:
        bits.append(f"新增 {added}")
    if renamed:
        bits.append(f"改名 {renamed}")
    if removed:
        bits.append(f"消失 {removed}")
    if skipped:
        bits.append(f"忽略 {skipped}")
    logs.info("emby", "同步库列表：" + " / ".join(bits))

    return {
        "added": added,
        "renamed": renamed,
        "removed": removed,
        "skipped": skipped,
        "total": len(seen),
    }
