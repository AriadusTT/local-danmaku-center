from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LibraryPatch(BaseModel):
    enabled: bool


class ScanRequest(BaseModel):
    mode: Literal["full", "incremental"] = "incremental"
    library_ids: list[str] | None = Field(
        default=None, description="null 表示使用已启用的库集合"
    )
