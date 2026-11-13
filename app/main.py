from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import settings
from .api import (
    libraries_router,
    logs_router,
    media_router,
    scan_router,
    settings_router,
)
from . import logs
from .db import dispose_db, init_db
from .jobs.scheduler import shutdown_scheduler, start_scheduler
from .runtime import load_config, make_probe_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

# 文件日志：轮转保存，避免长期运行时无限增长；控制台日志仍保留。
_log_path = Path(settings.db_path).expanduser()
if not _log_path.is_absolute():
    _log_path = Path.cwd() / _log_path
LOG_PATH = _log_path.resolve().parent / "app.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
if not any(isinstance(h, RotatingFileHandler) for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(_file_handler)
logs.install_logging_bridge()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    logger.info("数据库就绪")
    logs.info("system", "服务已启动，数据库就绪")
    logs.info("system", f"文件日志：{LOG_PATH}")

    # 启动时只做两件事：建表、排定时任务。
    # 不自动扫描、不自动同步库列表 —— 用户没配好之前不打任何 Emby 请求。
    await start_scheduler()
    cfg = await load_config()
    if cfg.is_configured:
        logger.info("Emby 配置已就绪：%s", cfg.emby_base_url)
        logs.info("system", f"Emby 配置已就绪：{cfg.emby_base_url}")
    else:
        logger.info("尚未配置 Emby，等待用户在设置页填写")

    try:
        yield
    finally:
        shutdown_scheduler()
        await dispose_db()
        logger.info("已退出")


app = FastAPI(
    title="本地弹幕中心",
    description="阶段一：媒体库扫描",
    version=__version__,
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "web" / "static")),
    name="static",
)

app.include_router(settings_router)
app.include_router(libraries_router)
app.include_router(scan_router)
app.include_router(media_router)
app.include_router(logs_router)


@app.middleware("http")
async def log_api_failures(request: Request, call_next):
    """只记录失败/异常请求，避免轮询日志接口本身把日志刷爆。"""
    path = request.url.path
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        if path.startswith("/api/"):
            logger.exception("%s %s 未处理异常", request.method, path)
        raise
    if path.startswith("/api/") and response.status_code >= 400:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.warning(
            "%s %s -> HTTP %s (%.0fms)",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"version": __version__}
    )


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    """首次引导：独立页面，不叠在控制台上。"""
    return templates.TemplateResponse(
        request, "setup.html", {"version": __version__}
    )


@app.get("/api/health")
async def health() -> dict:
    cfg = await load_config()
    if not cfg.is_configured:
        return {
            "configured": False,
            "emby_ok": False,
            "emby": "尚未配置",
            "base_url": cfg.emby_base_url,
        }

    try:
        async with await make_probe_client(cfg) as client:
            info = await client.ping()
    except Exception as exc:  # noqa: BLE001
        return {
            "configured": True,
            "emby_ok": False,
            "emby": str(exc),
            "base_url": cfg.emby_base_url,
        }

    return {
        "configured": True,
        "emby_ok": True,
        "emby": f"{info.get('ServerName')} {info.get('Version')}",
        "base_url": cfg.emby_base_url,
    }
