from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base  # noqa: F401  (建表时需先导入元数据)

_db_file = Path(settings.db_path).expanduser()
if not _db_file.is_absolute():
    _db_file = (Path.cwd() / _db_file).resolve()
_db_file.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{_db_file.as_posix()}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    """WAL 是 Windows 上避免「读写互锁」的关键；busy_timeout 给足并发余量。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate)


def _migrate(conn) -> None:  # noqa: ANN001
    """SQLite 的轻量迁移：create_all 不会给已有表加列，这里补齐。"""
    existing = {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_item)")
    }
    if not existing:
        return
    if "series_id" not in existing:
        conn.exec_driver_sql("ALTER TABLE media_item ADD COLUMN series_id VARCHAR(64)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_series ON media_item (series_id)"
        )
    if "date_last_saved" not in existing:
        conn.exec_driver_sql(
            "ALTER TABLE media_item ADD COLUMN date_last_saved VARCHAR(32)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_media_updated "
            "ON media_item (date_last_saved)"
        )


async def dispose_db() -> None:
    await engine.dispose()
