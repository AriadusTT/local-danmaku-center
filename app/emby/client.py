from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

#: 可作为「媒体库」接入的类型 → 需要扫描的条目类型
LIBRARY_ITEM_TYPE: dict[str, tuple[str, ...]] = {
    "tvshows": ("Episode",),
    "movies": ("Movie",),
}


def library_item_types(collection_type: str | None) -> tuple[str, ...]:
    """返回该库需要扫描的条目类型；空元组表示这不是媒体库（合集/音乐/家庭视频等）。

    注意 Emby 里 `CollectionType` 为空的库是「混合视频库」，里面既有电影也有剧集，
    这种要两种都扫，不能当成非媒体库丢掉。
    """
    key = (collection_type or "").strip().lower()
    if key in LIBRARY_ITEM_TYPE:
        return LIBRARY_ITEM_TYPE[key]
    if key == "":
        return ("Movie", "Episode")
    return ()


def is_media_library(collection_type: str | None) -> bool:
    return bool(library_item_types(collection_type))

ITEM_FIELDS = (
    "Path,ProviderIds,SeriesName,SeriesId,ParentIndexNumber,IndexNumber,"
    "RunTimeTicks,MediaSources,ProductionYear,DateLastSaved,DateCreated"
)


class EmbyError(RuntimeError):
    pass


@dataclass(slots=True)
class LibraryDTO:
    id: str
    name: str
    collection_type: str | None
    locations: list[str] = field(default_factory=list)

    @property
    def item_types(self) -> tuple[str, ...]:
        return library_item_types(self.collection_type)

    @property
    def scannable(self) -> bool:
        return bool(self.item_types)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "LibraryDTO":
        return cls(
            id=str(raw.get("ItemId") or raw.get("Id") or ""),
            name=raw.get("Name") or "",
            collection_type=raw.get("CollectionType") or None,
            locations=list(raw.get("Locations") or []),
        )


@dataclass(slots=True)
class EmbyItem:
    id: str
    item_type: str
    name: str | None
    series_name: str | None
    series_id: str | None
    season: int | None
    episode: int | None
    year: int | None
    path: str | None
    size_bytes: int | None
    runtime_ticks: int | None
    provider_ids: dict[str, str]
    date_last_saved: str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "EmbyItem":
        sources = raw.get("MediaSources") or []
        first = sources[0] if sources else {}
        return cls(
            id=str(raw.get("Id") or ""),
            item_type=raw.get("Type") or "",
            name=raw.get("Name"),
            series_name=raw.get("SeriesName"),
            series_id=str(raw["SeriesId"]) if raw.get("SeriesId") else None,
            season=raw.get("ParentIndexNumber"),
            episode=raw.get("IndexNumber"),
            year=raw.get("ProductionYear"),
            path=raw.get("Path"),
            size_bytes=first.get("Size"),
            runtime_ticks=raw.get("RunTimeTicks") or first.get("RunTimeTicks"),
            provider_ids=dict(raw.get("ProviderIds") or {}),
            date_last_saved=raw.get("DateLastSaved") or raw.get("DateCreated"),
        )


class EmbyClient:
    """Emby 的唯一出口。只读，不做任何写操作。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        retries: int = 2,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "X-Emby-Token": api_key,
                "Accept": "application/json",
                "User-Agent": "local-danmaku-service/0.1",
            },
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            transport=httpx.AsyncHTTPTransport(retries=retries),
        )

    async def __aenter__(self) -> "EmbyClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.ConnectTimeout as exc:
            raise EmbyError("连接超时：地址不可达或端口不对") from exc
        except httpx.ReadTimeout as exc:
            raise EmbyError("读取超时：Emby 响应太慢") from exc
        except httpx.ConnectError as exc:
            raise EmbyError(f"无法连接：{exc}") from exc
        except httpx.HTTPError as exc:  # 其他网络层错误
            raise EmbyError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 401:
            raise EmbyError("Emby 鉴权失败：请检查 EMBY_API_KEY 是否有效")
        if resp.status_code >= 400:
            raise EmbyError(
                f"Emby 返回 {resp.status_code}（{path}）：{resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise EmbyError(f"Emby 返回非 JSON（{path}）") from exc

    async def ping(self) -> dict[str, Any]:
        return await self._get("/System/Info")

    async def get_libraries(self) -> list[LibraryDTO]:
        raw = await self._get("/Library/VirtualFolders")
        libs = [LibraryDTO.from_raw(x) for x in raw if (x.get("ItemId") or x.get("Id"))]
        return libs

    async def get_series_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """按 Id 批量查条目（用来补 Series 的 ProviderIds）。

        不使用 ParentId 过滤：Emby 里 Series 的父节点和 VirtualFolders 的 ItemId
        并不总是同一个（多级媒体库时会错位），按 Id 查才可靠。
        """
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            data = await self._get(
                "/Items",
                {
                    "Ids": ",".join(chunk),
                    "Recursive": "true",
                    "Fields": "ProviderIds,ProductionYear,Path",
                    "Limit": len(chunk),
                },
            )
            out.extend(data.get("Items") or [])
        return out

    async def count_items(self, library_id: str, item_type: str) -> int:
        data = await self._get(
            "/Items",
            {
                "ParentId": library_id,
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "Limit": 1,
                "StartIndex": 0,
                "EnableTotalRecordCount": "true",
                "Fields": "Path",
            },
        )
        return int(data.get("TotalRecordCount") or 0)

    async def get_primary_image(
        self, item_id: str, max_width: int = 240
    ) -> tuple[bytes, str] | None:
        """取条目的主图（剧集海报 / 电影海报）。没有图时返回 None。"""
        try:
            resp = await self._client.get(
                f"/Items/{item_id}/Images/Primary",
                params={"maxWidth": max_width, "quality": 90},
            )
        except httpx.HTTPError as exc:
            raise EmbyError(f"取封面失败：{exc}") from exc
        if resp.status_code == 401:
            raise EmbyError("Emby 鉴权失败：请检查 API Key")
        if resp.status_code >= 400:
            # 没有剧照/海报时 Emby 可能给 404 也可能给 500，一律当作「没图」
            return None
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    async def iter_items(
        self,
        library_id: str,
        item_type: str,
        *,
        min_date_last_saved: str | None = None,
        page_size: int = 500,
    ) -> AsyncIterator[list[EmbyItem]]:
        """逐页产出条目。调用方负责逐页入库与提交，长任务不会被一次性读爆内存。"""
        start = 0
        while True:
            params: dict[str, Any] = {
                "ParentId": library_id,
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "Fields": ITEM_FIELDS,
                "StartIndex": start,
                "Limit": page_size,
                "EnableTotalRecordCount": "true",
            }
            if min_date_last_saved:
                params["MinDateLastSaved"] = min_date_last_saved

            data = await self._get("/Items", params)
            raw_items = data.get("Items") or []
            if not raw_items:
                return

            items = [EmbyItem.from_raw(x) for x in raw_items]
            yield items

            start += len(items)
            total = data.get("TotalRecordCount")
            if total is not None and start >= int(total):
                return
            if len(items) < page_size:
                return
