from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from .config import settings
from .db import SessionFactory, dispose_db, init_db
from .emby.client import is_media_library
from .jobs.scanner import ScanBusy, scan_manager
from .library_sync import refresh_libraries
from .models import Library, MediaItem, ScanRun
from .runtime import load_config, make_client


def _bar(percent: float, width: int = 28) -> str:
    filled = int(width * max(0.0, min(100.0, percent)) / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {percent:5.1f}%"


async def cmd_health(_args: argparse.Namespace) -> int:
    await init_db()
    cfg = await load_config()
    if not cfg.is_configured:
        print("尚未配置 Emby。启动网页后在「设置」里填写地址与 API Key，")
        print("或直接调用 PUT /api/settings 写入。")
        return 2
    async with await make_client(cfg) as client:
        info = await client.ping()
    print(f"Emby: {cfg.emby_base_url}（密钥 {cfg.masked_key}）")
    print(f"  服务器: {info.get('ServerName')}  版本: {info.get('Version')}")
    print(f"  系统: {info.get('OperatingSystem')}  版本号ID: {info.get('Id')}")
    return 0


async def cmd_libraries(args: argparse.Namespace) -> int:
    await init_db()
    if args.refresh:
        result = await refresh_libraries()
        print(f"同步完成：新增 {result['added']} / 改名 {result['renamed']} / 消失 {result['removed']}")

    async with SessionFactory() as session:
        rows = (
            await session.execute(
                select(Library).order_by(Library.present.desc(), Library.name)
            )
        ).scalars().all()

    if not rows:
        print("本地库表为空，试试：python -m app.cli libraries --refresh")
        return 0

    print(f"{'ItemId':<8} {'名称':<16} {'类型':<10} {'可扫':<5} {'启用':<5} {'条目':>7}  上次扫描")
    for row in rows:
        scannable = "是" if is_media_library(row.collection_type) else "否"
        flags = []
        if not row.present:
            flags.append("已移除")
        print(
            f"{row.id:<8} {row.name[:14]:<16} {(row.collection_type or '-'):<10} "
            f"{scannable:<5} {'是' if row.enabled else '否':<5} {row.item_count:>7}  "
            f"{row.last_scan_at or '-'} {' '.join(flags)}"
        )
    return 0


async def cmd_scan(args: argparse.Namespace) -> int:
    await init_db()

    ids: list[str] | None = None
    if args.all:
        async with SessionFactory() as session:
            rows = (await session.execute(select(Library).where(Library.present == 1))).scalars().all()
        ids = [
            row.id
            for row in rows
            if is_media_library(row.collection_type)
        ]
    elif args.library:
        ids = args.library

    try:
        run_id = await scan_manager.start(args.mode, ids)
    except ScanBusy as exc:
        print(f"跳过：{exc}")
        return 1
    except ValueError as exc:
        print(f"参数错误：{exc}")
        return 1

    print(f"扫描已启动 run_id={run_id} mode={args.mode}")
    last_state = "running"
    while True:
        await asyncio.sleep(0.4)
        async with SessionFactory() as session:
            run = await session.get(ScanRun, run_id)
        if run is None:
            print("扫描记录丢失")
            return 1
        percent = (run.processed / run.total * 100) if run.total else 0.0
        sys.stdout.write(
            f"\r{_bar(percent)}  {run.processed}/{run.total}  "
            f"新增 {run.added} 更新 {run.updated} 移除 {run.removed}"
        )
        sys.stdout.flush()
        last_state = run.state
        if run.state != "running":
            break

    print()
    async with SessionFactory() as session:
        run = await session.get(ScanRun, run_id)
    print(
        f"状态：{last_state}  总计 {run.total}  新增 {run.added}  更新 {run.updated}  "
        f"未变 {run.unchanged}  移除 {run.removed}"
    )
    if run.error:
        print(f"错误：{run.error}")
    return 0 if last_state == "done" else 1


async def cmd_stats(_args: argparse.Namespace) -> int:
    await init_db()
    async with SessionFactory() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(MediaItem).where(MediaItem.scan_state != "removed")
            )
        ).scalar_one()
        removed = (
            await session.execute(
                select(func.count()).select_from(MediaItem).where(MediaItem.scan_state == "removed")
            )
        ).scalar_one()
        by_lib = (
            await session.execute(
                select(Library.name, func.count(MediaItem.emby_id))
                .join(MediaItem, MediaItem.library_id == Library.id, isouter=True)
                .where(MediaItem.scan_state != "removed")
                .group_by(Library.name)
            )
        ).all()
    print(f"条目总数（不含已移除）：{total}   已移除：{removed}")
    for name, count in by_lib:
        print(f"  {name:<16} {count:>6}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="Emby 弹幕缓存服务 · 命令行"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_health = sub.add_parser("health", help="测试 Emby 连接")
    p_health.set_defaults(func=cmd_health)

    p_libs = sub.add_parser("libraries", help="查看/同步媒体库")
    p_libs.add_argument("--refresh", action="store_true", help="先从 Emby 同步库列表")
    p_libs.set_defaults(func=cmd_libraries)

    p_scan = sub.add_parser("scan", help="执行扫描")
    p_scan.add_argument("--mode", choices=["full", "incremental"], default="full")
    p_scan.add_argument("--library", action="append", help="只扫指定库（可重复）")
    p_scan.add_argument("--all", action="store_true", help="扫全部可扫库（忽略启用开关）")
    p_scan.set_defaults(func=cmd_scan)

    p_stats = sub.add_parser("stats", help="查看入库统计")
    p_stats.set_defaults(func=cmd_stats)
    return parser


async def _main() -> int:
    args = build_parser().parse_args()
    try:
        return await args.func(args)
    finally:
        await dispose_db()


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
