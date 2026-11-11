from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Iterable, Iterator, Sequence

from sqlalchemy import select, update

from .. import logs
from ..config import settings
from ..db import SessionFactory
from ..emby import EmbyClient, EmbyItem, library_item_types
from ..models import Library, MediaItem, ScanRun, ScanRunLibrary, SeriesMeta, utcnow
from ..runtime import load_config

#: scope 里的一个媒体库：(库ID, 需要扫描的条目类型)
LibraryScope = tuple[str, tuple[str, ...]]


class ScanBusy(RuntimeError):
    """已有任务在跑。"""


class ScanCanceled(Exception):
    """用户取消。"""


def fingerprint(item: EmbyItem) -> str:
    raw = f"{item.path}|{item.size_bytes}|{item.runtime_ticks}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _chunks(seq: Sequence[str], size: int = 400) -> Iterator[list[str]]:
    buf: list[str] = []
    for value in seq:
        buf.append(value)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


class ScanManager:
    """全局唯一的扫描调度器：同一时刻只允许一个任务，可取消，可重入。"""

    def __init__(self) -> None:
        self._running = False
        self._cancel = asyncio.Event()
        self._run_id: int | None = None
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- 对外

    @property
    def running(self) -> bool:
        return self._running

    @property
    def current_run_id(self) -> int | None:
        return self._run_id

    async def start(
        self, mode: str, library_ids: list[str] | None = None
    ) -> int:
        if mode not in ("full", "incremental"):
            raise ValueError(f"不支持的扫描模式：{mode}")
        if self._running:
            raise ScanBusy("已有扫描任务在进行中")

        cfg = await load_config()
        if not cfg.is_configured:
            raise ValueError("请先在设置页填写 Emby 地址与 API Key")

        scope = await self._resolve_scope(library_ids)
        if not scope:
            raise ValueError("没有可扫描的媒体库（请先同步库列表并勾选）")

        self._running = True
        self._cancel = asyncio.Event()
        run_id = await self._create_run(mode, scope)
        self._run_id = run_id
        logs.info(
            "scan",
            f"#{run_id} 开始{'全量' if mode == 'full' else '增量'}扫描，{len(scope)} 个媒体库",
        )
        self._task = asyncio.create_task(self._execute(run_id, mode, scope))
        return run_id

    async def run_and_wait(
        self, mode: str, library_ids: list[str] | None = None
    ) -> int:
        """给 CLI 用：跑完再返回。"""
        run_id = await self.start(mode, library_ids)
        if self._task is not None:
            await self._task
        return run_id

    async def cancel(self, run_id: int | None = None) -> bool:
        if not self._running:
            return False
        if run_id is not None and self._run_id != run_id:
            return False
        self._cancel.set()
        return True

    # ---------------------------------------------------------------- 内部

    async def _resolve_scope(
        self, library_ids: list[str] | None
    ) -> list[LibraryScope]:
        async with SessionFactory() as session:
            stmt = select(Library).where(Library.present == 1)
            if library_ids:
                stmt = stmt.where(Library.id.in_(library_ids))
            else:
                stmt = stmt.where(Library.enabled == 1)
            rows = (await session.execute(stmt)).scalars().all()
        scope: list[LibraryScope] = []
        for row in rows:
            item_types = library_item_types(row.collection_type)
            if item_types:
                scope.append((row.id, item_types))
        return scope

    async def _create_run(
        self, mode: str, scope: list[LibraryScope]
    ) -> int:
        async with SessionFactory() as session:
            run = ScanRun(mode=mode, scope=json.dumps([s[0] for s in scope]), state="running")
            session.add(run)
            await session.flush()
            for lib_id, _ in scope:
                session.add(
                    ScanRunLibrary(run_id=run.id, library_id=lib_id, state="pending")
                )
            await session.commit()
            return run.id

    async def _execute(
        self, run_id: int, mode: str, scope: list[LibraryScope]
    ) -> None:
        cfg = await load_config()
        if not cfg.is_configured:
            logs.error("scan", f"#{run_id} 中止：尚未配置 Emby 地址与 API Key")
            await self._finish(run_id, "failed", "尚未配置 Emby 地址与 API Key")
            self._running = False
            self._run_id = None
            self._task = None
            return

        client = EmbyClient(
            cfg.emby_base_url, cfg.emby_api_key, settings.request_timeout
        )
        try:
            await self._probe_totals(run_id, scope, client)
            for lib_id, item_types in scope:
                if self._cancel.is_set():
                    raise ScanCanceled
                await self._scan_library(
                    run_id, lib_id, item_types, mode, client, cfg.page_size
                )
            canceled = self._cancel.is_set()
            logs.log("warn" if canceled else "info", "scan", f"#{run_id} {'已取消' if canceled else '扫描完成'}")
            await self._finish(run_id, "canceled" if canceled else "done")
        except ScanCanceled:
            logs.warn("scan", f"#{run_id} 已取消")
            await self._finish(run_id, "canceled")
        except Exception as exc:  # noqa: BLE001
            logs.error("scan", f"#{run_id} 失败：{type(exc).__name__}: {exc}")
            await self._finish(run_id, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            await client.close()
            self._running = False
            self._run_id = None
            self._task = None

    async def _probe_totals(
        self, run_id: int, scope: list[LibraryScope], client: EmbyClient
    ) -> None:
        """先探总量，保证进度条从第一秒就是真的，不会中途倒退。"""
        grand_total = 0
        async with SessionFactory() as session:
            for lib_id, item_types in scope:
                total = 0
                for item_type in item_types:
                    try:
                        total += await client.count_items(lib_id, item_type)
                    except Exception:  # noqa: BLE001  单个库探量失败按 0 处理
                        pass
                grand_total += total
                await session.execute(
                    update(ScanRunLibrary)
                    .where(
                        ScanRunLibrary.run_id == run_id,
                        ScanRunLibrary.library_id == lib_id,
                    )
                    .values(total=total)
                )
            await session.execute(
                update(ScanRun).where(ScanRun.id == run_id).values(total=grand_total)
            )
            await session.commit()

    async def _scan_library(
        self,
        run_id: int,
        lib_id: str,
        item_types: tuple[str, ...],
        mode: str,
        client: EmbyClient,
        page_size: int,
    ) -> None:
        seen: set[str] = set()
        try:
            min_date = None
            if mode == "incremental":
                async with SessionFactory() as session:
                    lib = await session.get(Library, lib_id)
                    min_date = lib.last_scan_at if lib else None

            await self._set_library_state(run_id, lib_id, "running")

            # 混合视频库会有两种条目类型，逐个扫，seen 合并后再做删除判定
            for item_type in item_types:
                async for page in client.iter_items(
                    lib_id,
                    item_type,
                    min_date_last_saved=min_date,
                    page_size=page_size,
                ):
                    if self._cancel.is_set():
                        raise ScanCanceled
                    added, updated, unchanged = await self._upsert_page(
                        lib_id, item_type, page
                    )
                    seen.update(item.id for item in page)
                    await self._bump(
                        run_id, lib_id, len(page), added, updated, unchanged, 0
                    )

            removed = 0
            if mode == "full":
                removed = await self._mark_removed(lib_id, seen)
                if removed:
                    await self._bump(run_id, lib_id, 0, 0, 0, 0, removed)

            # Series 的 ProviderIds 只挂在“剧集”这一层，条目本身拿不到，
            # 所以扫完这个库再同步一次，供按 TMDB 归并使用。
            try:
                await self._sync_series(lib_id, client)
            except Exception:  # noqa: BLE001 拿不到外部 id 不该让整库失败
                pass

            await self._set_library_state(run_id, lib_id, "done")
            await self._log_library_done(run_id, lib_id)
        except ScanCanceled:
            await self._set_library_state(run_id, lib_id, "canceled")
            raise
        except Exception as exc:  # noqa: BLE001  单库失败不影响其他库
            logs.error(
                "scan",
                f"库 {lib_id} 扫描失败：{type(exc).__name__}: {exc}",
            )
            await self._set_library_state(
                run_id, lib_id, "failed", f"{type(exc).__name__}: {exc}"
            )

    async def _log_library_done(self, run_id: int, lib_id: str) -> None:
        """按库落一条汇总日志，比每页都写一条干净。"""
        async with SessionFactory() as session:
            row = (
                await session.execute(
                    select(ScanRunLibrary).where(
                        ScanRunLibrary.run_id == run_id,
                        ScanRunLibrary.library_id == lib_id,
                    )
                )
            ).scalars().first()
            name = (
                await session.execute(select(Library.name).where(Library.id == lib_id))
            ).scalar_one_or_none()
        if row is None:
            return
        bits = [f"{row.processed} 条"]
        if row.added:
            bits.append(f"新增 {row.added}")
        if row.updated:
            bits.append(f"更新 {row.updated}")
        if row.removed:
            bits.append(f"移除 {row.removed}")
        if row.unchanged:
            bits.append(f"未变 {row.unchanged}")
        logs.info("scan", f"库「{name or lib_id}」完成：{' / '.join(bits)}")

    async def _upsert_page(
        self, lib_id: str, item_type: str, items: Iterable[EmbyItem]
    ) -> tuple[int, int, int]:
        items = list(items)
        if not items:
            return 0, 0, 0

        added = updated = unchanged = 0
        now = utcnow()
        async with SessionFactory() as session:
            rows = (
                await session.execute(
                    select(MediaItem).where(
                        MediaItem.emby_id.in_([i.id for i in items])
                    )
                )
            ).scalars().all()
            existing = {row.emby_id: row for row in rows}

            for item in items:
                fp = fingerprint(item)
                row = existing.get(item.id)
                payload = {
                    "library_id": lib_id,
                    "item_type": item.item_type or item_type,
                    "name": item.name,
                    "series_name": item.series_name,
                    "series_id": item.series_id,
                    "season": item.season,
                    "episode": item.episode,
                    "year": item.year,
                    "path": item.path,
                    "size_bytes": item.size_bytes,
                    "runtime_ticks": item.runtime_ticks,
                    "provider_ids": json.dumps(item.provider_ids, ensure_ascii=False),
                    "fingerprint": fp,
                    "last_seen_at": now,
                    "date_last_saved": item.date_last_saved,
                }
                if row is None:
                    session.add(
                        MediaItem(
                            emby_id=item.id,
                            scan_state="new",
                            match_status="pending",
                            danmaku_status="none",
                            first_seen_at=now,
                            **payload,
                        )
                    )
                    added += 1
                elif row.fingerprint != fp:
                    # 文件被替换 → 旧匹配失效
                    for key, value in payload.items():
                        setattr(row, key, value)
                    row.scan_state = "changed"
                    row.match_status = "pending"
                    row.episode_id = None
                    row.match_source = None
                    row.match_score = None
                    updated += 1
                else:
                    for key, value in payload.items():
                        setattr(row, key, value)
                    if row.scan_state == "removed":
                        row.scan_state = "seen"
                    elif row.scan_state in ("new", "changed"):
                        row.scan_state = "seen"
                    unchanged += 1

            await session.commit()
        return added, updated, unchanged

    async def _sync_series(self, lib_id: str, client: EmbyClient) -> None:
        """补齐本库这些 Series 的 ProviderIds（TMDB 等）。

        不按 ParentId 拉整库 Series：Emby 里 Series 的父节点与 VirtualFolders
        的 ItemId 不总是一致，按 ParentId 会漏。改成「库里出现过哪些 series_id，
        就按 id 批量问」。
        """
        async with SessionFactory() as session:
            sids = [
                row[0]
                for row in (
                    await session.execute(
                        select(MediaItem.series_id)
                        .where(
                            MediaItem.library_id == lib_id,
                            MediaItem.series_id.is_not(None),
                        )
                        .distinct()
                    )
                ).all()
                if row[0]
            ]
        if not sids:
            return

        raw = await client.get_series_by_ids(sids)
        now = utcnow()
        async with SessionFactory() as session:
            existing = {
                row.id: row
                for row in (
                    await session.execute(
                        select(SeriesMeta).where(SeriesMeta.id.in_(sids))
                    )
                ).scalars()
            }
            for item in raw:
                sid = str(item.get("Id") or "")
                if not sid:
                    continue
                pid = item.get("ProviderIds") or {}
                values = {
                    "library_id": lib_id,
                    "name": item.get("Name"),
                    "year": item.get("ProductionYear"),
                    "tmdb_id": pid.get("Tmdb"),
                    "tvdb_id": pid.get("Tvdb"),
                    "imdb_id": pid.get("Imdb"),
                    "present": 1,
                    "updated_at": now,
                }
                row = existing.get(sid)
                if row is None:
                    session.add(SeriesMeta(id=sid, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
            await session.commit()

    async def _mark_removed(self, lib_id: str, seen: set[str]) -> int:
        """差集只在「本库作用域」内计算，且仅 full 模式调用。"""
        async with SessionFactory() as session:
            rows = (
                await session.execute(
                    select(MediaItem.emby_id).where(
                        MediaItem.library_id == lib_id,
                        MediaItem.scan_state != "removed",
                    )
                )
            ).scalars().all()
            missing = [emby_id for emby_id in rows if emby_id not in seen]
            for chunk in _chunks(missing):
                await session.execute(
                    update(MediaItem)
                    .where(MediaItem.emby_id.in_(chunk))
                    .values(scan_state="removed")
                )
            await session.commit()
            return len(missing)

    async def _bump(
        self,
        run_id: int,
        lib_id: str,
        processed: int,
        added: int,
        updated: int,
        unchanged: int,
        removed: int,
    ) -> None:
        async with SessionFactory() as session:
            await session.execute(
                update(ScanRunLibrary)
                .where(
                    ScanRunLibrary.run_id == run_id,
                    ScanRunLibrary.library_id == lib_id,
                )
                .values(
                    processed=ScanRunLibrary.processed + processed,
                    added=ScanRunLibrary.added + added,
                    updated=ScanRunLibrary.updated + updated,
                    unchanged=ScanRunLibrary.unchanged + unchanged,
                    removed=ScanRunLibrary.removed + removed,
                )
            )
            await session.execute(
                update(ScanRun)
                .where(ScanRun.id == run_id)
                .values(
                    processed=ScanRun.processed + processed,
                    added=ScanRun.added + added,
                    updated=ScanRun.updated + updated,
                    unchanged=ScanRun.unchanged + unchanged,
                    removed=ScanRun.removed + removed,
                )
            )
            await session.commit()

    async def _set_library_state(
        self,
        run_id: int,
        lib_id: str,
        state: str,
        error: str | None = None,
    ) -> None:
        async with SessionFactory() as session:
            await session.execute(
                update(ScanRunLibrary)
                .where(
                    ScanRunLibrary.run_id == run_id,
                    ScanRunLibrary.library_id == lib_id,
                )
                .values(state=state, error=error)
            )
            if state == "done":
                # 库规模一律取「探量」结果：增量扫描只返回少量条目，
                # 用它回落会把这个库的条目数写成 0。
                link = await session.get(ScanRunLibrary, (run_id, lib_id))
                await session.execute(
                    update(Library)
                    .where(Library.id == lib_id)
                    .values(
                        item_count=link.total if link else 0,
                        last_scan_at=utcnow(),
                    )
                )
            await session.commit()

    async def _finish(
        self, run_id: int, state: str, error: str | None = None
    ) -> None:
        async with SessionFactory() as session:
            await session.execute(
                update(ScanRun)
                .where(ScanRun.id == run_id)
                .values(state=state, error=error, finished_at=utcnow())
            )
            # 收尾：没轮到 / 正在跑的子记录不要停在 pending，否则 UI 看起来像卡住
            if state in ("canceled", "failed"):
                await session.execute(
                    update(ScanRunLibrary)
                    .where(
                        ScanRunLibrary.run_id == run_id,
                        ScanRunLibrary.state.in_(("pending", "running")),
                    )
                    .values(state="canceled" if state == "canceled" else "skipped")
                )
            await session.commit()


scan_manager = ScanManager()
