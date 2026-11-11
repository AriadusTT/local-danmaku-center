from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from .. import logs
from ..db import SessionFactory
from ..emby import EmbyClient, is_media_library
from ..jobs.scanner import scan_manager
from ..jobs.scheduler import reschedule
from ..models import Library, MediaItem, ScanRun, ScanRunLibrary
from ..runtime import (
    KEY_API,
    KEY_CRON_FULL,
    KEY_CRON_INC,
    KEY_PAGE_SIZE,
    KEY_SCHEDULER,
    KEY_URL,
    clear_config,
    load_config,
    save_config,
    validate_base_url,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class TestPayload(BaseModel):
    emby_base_url: str
    emby_api_key: str | None = None


class SettingsPayload(BaseModel):
    emby_base_url: str | None = None
    emby_api_key: str | None = None
    page_size: int | None = None
    scheduler_enabled: bool | None = None
    scan_cron_incremental: str | None = None
    scan_cron_full: str | None = None


def _validate_cron(expr: str, label: str) -> None:
    try:
        CronTrigger.from_crontab(expr, timezone="Asia/Shanghai")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"{label} cron 表达式无效：{exc}"
        ) from exc


async def _counts() -> tuple[int, int]:
    """只统计「媒体库」，合集/音乐等不计入。"""
    async with SessionFactory() as session:
        rows = (await session.execute(select(Library))).scalars().all()
    media = [row for row in rows if is_media_library(row.collection_type)]
    return len(media), sum(1 for row in media if row.enabled and row.present)


@router.get("")
async def get_settings() -> dict:
    cfg = await load_config()
    total, enabled = await _counts()
    return {**cfg.as_public_dict(), "library_count": total, "enabled_count": enabled}


@router.get("/status")
async def get_status() -> dict:
    cfg = await load_config()
    total, enabled = await _counts()

    if not cfg.is_configured:
        stage = "unconfigured"
    elif total == 0:
        stage = "no_libraries"
    elif enabled == 0:
        stage = "no_selection"
    else:
        stage = "ready"

    return {
        "stage": stage,
        "needs_onboarding": stage != "ready",
        "configured": cfg.is_configured,
        "library_count": total,
        "enabled_count": enabled,
    }


@router.post("/test")
async def test_connection(payload: TestPayload) -> dict:
    cfg = await load_config()
    try:
        url = validate_base_url(payload.emby_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    key = (payload.emby_api_key or "").strip() or cfg.emby_api_key
    if not key:
        raise HTTPException(status_code=400, detail="请填写 API Key")

    # 测试连接不重试、超时更短：地址填错时要立刻报错，而不是挂 30 秒
    client = EmbyClient(url, key, timeout=6.0, retries=0)
    try:
        info = await client.ping()
        libraries = await client.get_libraries()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"连接失败：{exc}") from exc
    finally:
        await client.close()

    return {
        "ok": True,
        "server_name": info.get("ServerName"),
        "version": info.get("Version"),
        "library_count": len(libraries),
        "media_library_count": sum(1 for lib in libraries if lib.scannable),
        "libraries": [
            {
                "id": lib.id,
                "name": lib.name,
                "collection_type": lib.collection_type,
                "scannable": lib.scannable,
            }
            for lib in libraries
        ],
    }


@router.put("")
async def update_settings(payload: SettingsPayload) -> dict:
    updates: dict[str, str | None] = {}

    if payload.emby_base_url is not None:
        try:
            updates[KEY_URL] = validate_base_url(payload.emby_base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 留空表示「不修改」，避免用户只是改个页码就把密钥清掉
    if payload.emby_api_key is not None and payload.emby_api_key.strip():
        updates[KEY_API] = payload.emby_api_key.strip()

    if payload.page_size is not None:
        if not 20 <= payload.page_size <= 1000:
            raise HTTPException(status_code=400, detail="每页条数需在 20–1000 之间")
        updates[KEY_PAGE_SIZE] = str(payload.page_size)

    if payload.scheduler_enabled is not None:
        updates[KEY_SCHEDULER] = "true" if payload.scheduler_enabled else "false"

    if payload.scan_cron_incremental:
        _validate_cron(payload.scan_cron_incremental, "增量扫描")
        updates[KEY_CRON_INC] = payload.scan_cron_incremental.strip()

    if payload.scan_cron_full:
        _validate_cron(payload.scan_cron_full, "全量扫描")
        updates[KEY_CRON_FULL] = payload.scan_cron_full.strip()

    cfg = await save_config(updates)
    reschedule(cfg)
    if updates:
        logs.info("settings", "设置已更新：" + "、".join(sorted(updates.keys())))
    total, enabled = await _counts()
    return {**cfg.as_public_dict(), "library_count": total, "enabled_count": enabled}


@router.post("/reset")
async def reset_all() -> dict:
    """完全重置：配置 + 全部扫描数据一起清掉，回到没装过的状态。"""
    if scan_manager.running:
        raise HTTPException(status_code=409, detail="扫描进行中，请先取消或等待结束")

    counts = await _purge_scan_data()
    await clear_config()
    cfg = await load_config()
    reschedule(cfg)
    logs.warn("settings", "已执行完全重置（配置与扫描数据全部清空）")
    return {**cfg.as_public_dict(), "reset": True, **counts}


@router.post("/purge")
async def purge_data() -> dict:
    """清空扫描数据（库记录 + 条目 + 扫描历史），配置保留。"""
    if scan_manager.running:
        raise HTTPException(status_code=409, detail="扫描进行中，请先取消或等待结束")

    counts = await _purge_scan_data()
    logs.warn(
        "settings",
        f"已清空扫描数据：{counts['media']} 条条目 / {counts['libraries']} 个库",
    )
    return {"purged": True, **counts}


async def _purge_scan_data() -> dict[str, int]:
    async with SessionFactory() as session:
        media = await session.execute(delete(MediaItem))
        links = await session.execute(delete(ScanRunLibrary))
        runs = await session.execute(delete(ScanRun))
        libs = await session.execute(delete(Library))
        await session.commit()

    return {
        "media": media.rowcount or 0,
        "libraries": libs.rowcount or 0,
        "runs": runs.rowcount or 0,
        "run_links": links.rowcount or 0,
    }
