from __future__ import annotations

import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from .. import logs
from ..db import SessionFactory
from ..models import Library
from ..runtime import RuntimeConfig, load_config
from .scanner import ScanBusy, scan_manager

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
TZ = "Asia/Shanghai"
JOB_IDS = ("scan_incremental", "scan_full")


async def _scheduled_scan(mode: str) -> None:
    cfg = await load_config()
    if not cfg.is_configured:
        logs.warn("scheduler", "定时扫描跳过：尚未配置 Emby")
        return

    async with SessionFactory() as session:
        enabled = (
            await session.execute(
                select(func.count())
                .select_from(Library)
                .where(Library.enabled == 1, Library.present == 1)
            )
        ).scalar_one()
    if not enabled:
        logs.warn("scheduler", "定时扫描跳过：没有勾选任何媒体库")
        return

    try:
        run_id = await scan_manager.start(mode, None)
        logs.info("scheduler", f"定时扫描已启动：{'全量' if mode == 'full' else '增量'} #{run_id}")
    except ScanBusy:
        logs.warn("scheduler", "定时扫描跳过：已有任务在跑")
    except Exception as exc:  # noqa: BLE001
        logs.error("scheduler", f"定时扫描启动失败：{type(exc).__name__}: {exc}")


def _apply_jobs(scheduler: AsyncIOScheduler, cfg: RuntimeConfig) -> None:
    for job_id in JOB_IDS:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    if not cfg.scheduler_enabled or not cfg.is_configured:
        logs.info("scheduler", "定时扫描已关闭")
        return

    scheduler.add_job(
        _scheduled_scan,
        CronTrigger.from_crontab(cfg.scan_cron_incremental, timezone=TZ),
        args=["incremental"],
        id="scan_incremental",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        _scheduled_scan,
        CronTrigger.from_crontab(cfg.scan_cron_full, timezone=TZ),
        args=["full"],
        id="scan_full",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    logs.info(
        "scheduler",
        f"定时任务已生效：增量 {cfg.scan_cron_incremental} / 全量 {cfg.scan_cron_full}",
    )


async def start_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TZ)
        _scheduler.start()
    _apply_jobs(_scheduler, await load_config())


def reschedule(cfg: RuntimeConfig | None = None) -> None:
    """设置变更后重新排期（同步调用，API 层直接调）。"""
    global _scheduler
    if cfg is None:
        return
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TZ)
        _scheduler.start()
    _apply_jobs(_scheduler, cfg)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
