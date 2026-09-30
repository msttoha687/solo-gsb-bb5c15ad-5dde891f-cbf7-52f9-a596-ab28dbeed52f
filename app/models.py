"""HTTP 请求/响应模型（Pydantic v2）。

所有时间字段必须带时区偏移，服务端统一换算成 UTC 存储与比较。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区偏移，例如 2026-09-30T08:00:00Z 或 +08:00")
    return value.astimezone(timezone.utc)


class RawBatchIn(BaseModel):
    id: str = Field(min_length=1, examples=["RM-001"])
    allergens: list[str] = Field(default_factory=list, examples=[["PEANUT"]])
    pending: bool = Field(default=False, description="待确认状态")

    @field_validator("allergens")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        items = [a.strip().upper() for a in v if a.strip()]
        if any(not a for a in items):
            raise ValueError("过敏原名称不能为空")
        return items


class RawBatchCorrectIn(BaseModel):
    allergens: list[str] = Field(default_factory=list)
    pending: bool = False

    @field_validator("allergens")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        return [a.strip().upper() for a in v if a.strip()]


class ProcessingIn(BaseModel):
    id: str = Field(min_length=1, examples=["PR-001"])
    equipment: str = Field(min_length=1, examples=["LINE-A"])
    start_utc: datetime
    end_utc: datetime
    inputs: list[str] = Field(default_factory=list, description="投入批次 ID（须在开始前可用）")
    outputs: list[str] = Field(min_length=1, description="产出批次 ID（至少一个）")

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @field_validator("outputs", "inputs")
    @classmethod
    def _nonblank(cls, v: list[str]) -> list[str]:
        items = [x.strip() for x in v if x.strip()]
        if len(items) != len(v):
            raise ValueError("批次 ID 不能为空")
        return items


class CleaningIn(BaseModel):
    id: str = Field(min_length=1, examples=["CL-001"])
    equipment: str = Field(min_length=1)
    cleaned_utc: datetime
    verified: bool = Field(default=True, description="已验证的清洗才能清除残留")
    note: str = ""

    @field_validator("cleaned_utc")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _aware_utc(v)


class IssueLabelIn(BaseModel):
    batch_id: str = Field(min_length=1)
    supersedes_id: int | None = Field(default=None, description="被新版替代的旧标签 ID")
