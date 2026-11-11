from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> str:
    """统一以 UTC ISO8601（带 Z）存库，展示层再转本地时区。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    """运行时配置（用户在设置页填写的值），优先于 .env。"""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Library(Base):
    __tablename__ = "library"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    collection_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locations: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    present: Mapped[int] = mapped_column(Integer, default=1)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    last_scan_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MediaItem(Base):
    __tablename__ = "media_item"

    emby_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_type: Mapped[str] = mapped_column(String(32), default="Episode")
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    series_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Emby 的 SeriesId：拿剧集海报、把同一部剧的单集归到一个卡片里都靠它
    series_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scan_state: Mapped[str] = mapped_column(String(16), default="seen")
    match_status: Mapped[str] = mapped_column(String(16), default="pending")
    episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    danmaku_status: Mapped[str] = mapped_column(String(16), default="none")
    danmaku_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    first_seen_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Emby 的内容更新时间；用于卡片墙「最近更新」排序
    date_last_saved: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_media_status", "match_status", "danmaku_status"),
        Index("idx_media_fp", "fingerprint"),
        Index("idx_media_lib", "library_id"),
        Index("idx_media_series", "series_id"),
        Index("idx_media_updated", "date_last_saved"),
    )


class SeriesMeta(Base):
    """Emby 的 Series 条目（一部剧的“壳”），用来拿 TMDB 等外部标识。

    同一部剧在 Emby 里可能因为文件夹改名留下两条 Series 记录，
    它们的 Tmdb 相同 —— 分组时靠这个把它们并成一张卡。
    """

    __tablename__ = "series_meta"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tmdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tvdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    imdb_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    present: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_series_tmdb", "tmdb_id"),
        Index("idx_series_lib", "library_id"),
    )


class ScanRun(Base):
    __tablename__ = "scan_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), default="full")
    scope: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[str] = mapped_column(String(32), default=utcnow)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(16), default="running")
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScanRunLibrary(Base):
    __tablename__ = "scan_run_library"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
