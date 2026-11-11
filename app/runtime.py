"""运行时配置：数据库里的值优先，.env 只作为默认值。

Emby 地址与密钥由用户在设置页填写后落到 app_setting 表，重启不丢，
也避免让用户去改文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from sqlalchemy import delete, select

from .config import settings
from .db import SessionFactory
from .emby import EmbyClient
from .models import AppSetting

KEY_URL = "emby_base_url"
KEY_API = "emby_api_key"
KEY_PAGE_SIZE = "page_size"
KEY_SCHEDULER = "scheduler_enabled"
KEY_CRON_INC = "scan_cron_incremental"
KEY_CRON_FULL = "scan_cron_full"

ALL_KEYS = (
    KEY_URL,
    KEY_API,
    KEY_PAGE_SIZE,
    KEY_SCHEDULER,
    KEY_CRON_INC,
    KEY_CRON_FULL,
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    emby_base_url: str
    emby_api_key: str
    page_size: int
    scheduler_enabled: bool
    scan_cron_incremental: str
    scan_cron_full: str
    #: 每个字段的来源：db / env
    sources: dict[str, str]

    @property
    def is_configured(self) -> bool:
        return bool(self.emby_base_url and self.emby_api_key)

    @property
    def masked_key(self) -> str:
        key = self.emby_api_key
        if not key:
            return ""
        if len(key) <= 10:
            return "*" * len(key)
        return f"{key[:4]}{'*' * 8}{key[-4:]}"

    def as_public_dict(self) -> dict:
        return {
            "emby_base_url": self.emby_base_url,
            "emby_api_key_masked": self.masked_key,
            "emby_api_key_set": bool(self.emby_api_key),
            "page_size": self.page_size,
            "scheduler_enabled": self.scheduler_enabled,
            "scan_cron_incremental": self.scan_cron_incremental,
            "scan_cron_full": self.scan_cron_full,
            "configured": self.is_configured,
            "sources": self.sources,
        }


def _env_defaults() -> dict[str, str]:
    return {
        KEY_URL: settings.emby_base_url,
        KEY_API: settings.emby_api_key,
        KEY_PAGE_SIZE: str(settings.page_size),
        KEY_SCHEDULER: "true" if settings.scheduler_enabled else "false",
        KEY_CRON_INC: settings.scan_cron_incremental,
        KEY_CRON_FULL: settings.scan_cron_full,
    }


def normalize_base_url(value: str) -> str:
    """把用户输入整理成可用的 base url。

    允许四种写法：
      - 192.168.0.112:8096     （IP + 端口）
      - emby.local:8096        （主机名，局域网/内网 DNS 都行）
      - https://emby.example.com（域名 + https，反代常用）
      - http://nas:8096/web/   （从浏览器地址栏直接粘来的）
    """
    url = (value or "").strip()
    if not url:
        return ""
    # 去掉从地址栏粘过来的锚点，如 /web/index.html#!/home
    url = url.split("#", 1)[0].strip()
    if url.startswith("//"):
        url = "http:" + url
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    url = url.rstrip("/")
    # Emby 的 Web 界面路径不是 API 根，顺手削掉，避免 404
    for suffix in ("/web/index.html", "/web"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
            break
    url = url.rstrip("/")
    # 统一小写 scheme，其余部分保持原样（路径可能区分大小写）
    if url.lower().startswith("https://"):
        url = "https://" + url[8:]
    elif url.lower().startswith("http://"):
        url = "http://" + url[7:]
    return url


def validate_base_url(value: str) -> str:
    """规范化 + 校验；不合法时抛 ValueError，消息可以直接给用户看。"""
    url = normalize_base_url(value)
    if not url:
        raise ValueError("请填写 Emby 地址")
    if " " in url:
        raise ValueError("地址里不能有空格")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "地址格式不正确，示例：192.168.0.112:8096 或 https://emby.example.com"
        )
    return url


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


async def load_config() -> RuntimeConfig:
    defaults = _env_defaults()
    async with SessionFactory() as session:
        rows = (await session.execute(select(AppSetting))).scalars().all()
    db = {row.key: row.value for row in rows}

    def pick(key: str) -> tuple[str, str]:
        value = db.get(key)
        if value not in (None, ""):
            return value, "db"
        return defaults.get(key, ""), "env"

    raw: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in ALL_KEYS:
        value, source = pick(key)
        raw[key] = value
        sources[key] = source

    try:
        page_size = max(20, min(1000, int(raw[KEY_PAGE_SIZE] or 500)))
    except ValueError:
        page_size = 500

    return RuntimeConfig(
        emby_base_url=normalize_base_url(raw[KEY_URL]),
        emby_api_key=(raw[KEY_API] or "").strip(),
        page_size=page_size,
        scheduler_enabled=_as_bool(raw[KEY_SCHEDULER], True),
        scan_cron_incremental=raw[KEY_CRON_INC] or "0 3 * * *",
        scan_cron_full=raw[KEY_CRON_FULL] or "0 3 * * 0",
        sources=sources,
    )


async def save_config(updates: dict[str, str | None]) -> RuntimeConfig:
    """只写传入的键；值为 None 表示删除该键（回落到 .env）。"""
    async with SessionFactory() as session:
        for key, value in updates.items():
            if key not in ALL_KEYS:
                continue
            row = await session.get(AppSetting, key)
            if value is None:
                if row is not None:
                    await session.delete(row)
                continue
            if row is None:
                session.add(AppSetting(key=key, value=str(value)))
            else:
                row.value = str(value)
        await session.commit()
    return await load_config()


async def clear_config() -> None:
    async with SessionFactory() as session:
        await session.execute(delete(AppSetting))
        await session.commit()


async def make_client(cfg: RuntimeConfig | None = None) -> EmbyClient:
    config = cfg or await load_config()
    return EmbyClient(
        config.emby_base_url, config.emby_api_key, settings.request_timeout
    )


async def make_probe_client(cfg: RuntimeConfig | None = None) -> EmbyClient:
    """健康检查/连接测试用：短超时、不重试。"""
    config = cfg or await load_config()
    return EmbyClient(
        config.emby_base_url, config.emby_api_key, timeout=6.0, retries=0
    )


def with_updates(cfg: RuntimeConfig, **kwargs: object) -> RuntimeConfig:
    return replace(cfg, **kwargs)
