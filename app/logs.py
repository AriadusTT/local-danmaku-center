"""运行时事件缓冲（给控制台右上角的日志栏用）。

只放内存、不落库：
  - 日志是运维信息，写进 data/app.db 会把业务库搞脏，而且会无限膨胀；
  - 扫描失败的原因在 scan_run.error 里已经有一份，这里只是给人看的实时流。

单进程 asyncio 下不需要锁：写入路径里没有 await，递增 seq 是原子的。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

MAX_ENTRIES = 800

#: 允许的级别，前端按此着色
LEVELS = ("info", "warn", "error")


@dataclass(slots=True)
class Entry:
    seq: int
    ts: float
    level: str
    source: str
    message: str

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "source": self.source,
            "message": self.message,
        }


_buffer: deque[Entry] = deque(maxlen=MAX_ENTRIES)
_seq = 0


def log(level: str, source: str, message: str) -> None:
    """记一条事件。level 不在 LEVELS 里按 info 处理，不抛异常。"""
    global _seq
    _seq += 1
    _buffer.append(
        Entry(
            seq=_seq,
            ts=time.time(),
            level=level if level in LEVELS else "info",
            source=source,
            message=(message or "").strip(),
        )
    )


def info(source: str, message: str) -> None:
    log("info", source, message)


def warn(source: str, message: str) -> None:
    log("warn", source, message)


def error(source: str, message: str) -> None:
    log("error", source, message)


def since(after: int = 0, limit: int = 300) -> dict:
    """取 seq > after 的增量。limit 从旧到新截断，避免一次推 800 条。"""
    if after >= _seq:
        return {"items": [], "last_seq": _seq}
    items = [e.as_dict() for e in _buffer if e.seq > after]
    if len(items) > limit:
        items = items[-limit:]
    return {"items": items, "last_seq": _seq}


def clear() -> None:
    _buffer.clear()


class _RuntimeBridgeHandler(logging.Handler):
    """把 Python 标准 logging 的 WARNING+ 同步到控制台日志缓冲。

    HTTP 请求错误、未捕获异常和第三方库告警都会同时出现在右侧运行日志里，
    但不会把普通的访问日志刷进界面。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("app.logs"):
                return
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warn"
            else:
                return
            source = record.name.rsplit(".", 1)[-1] or "app"
            log(level, source, record.getMessage())
        except Exception:  # noqa: BLE001 日志桥本身不能影响业务
            pass


_bridge_installed = False


def install_logging_bridge() -> None:
    """幂等安装标准日志桥，供 API 错误和后台异常统一进入日志栏。"""
    global _bridge_installed
    if _bridge_installed:
        return
    handler = _RuntimeBridgeHandler(level=logging.WARNING)
    for name in ("app", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(h, _RuntimeBridgeHandler) for h in logger.handlers):
            logger.addHandler(handler)
    _bridge_installed = True
