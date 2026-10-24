"""Shared response building blocks. JSON is camelCase to match the frontend's TypeScript types."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Provenance = Literal["sample", "live"]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PAGE = 10_000
MAX_QUERY_LENGTH = 200


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class Page[T](ApiModel):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int

    @classmethod
    def build(cls, items: list[T], *, page: int, page_size: int, total: int) -> "Page[T]":
        return cls(items=items, page=page, page_size=page_size, total=total, total_pages=math.ceil(total / page_size))


class PageParams(BaseModel):
    """Bounded pagination shared by every collection endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    page: int = Field(default=1, ge=1, le=MAX_PAGE, description="1-based page number.")
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, alias="pageSize")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class ErrorDetail(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorInfo
