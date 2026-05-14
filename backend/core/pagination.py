"""
Reusable pagination for list endpoints.
Single implementation — never copy-paste offset logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")


@dataclass
class PaginationParams:
    page: int
    per_page: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        return self.per_page


def get_pagination(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=100, description="Items per page"),
) -> PaginationParams:
    """FastAPI dependency for pagination parameters."""
    return PaginationParams(page=page, per_page=per_page)


class Page(BaseModel, Generic[T]):
    """Standard paginated response envelope."""
    items: list[T]
    total: int
    page: int
    per_page: int
    pages: int

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        params: PaginationParams,
    ) -> "Page[T]":
        pages = max(1, (total + params.per_page - 1) // params.per_page)
        return cls(
            items=items,
            total=total,
            page=params.page,
            per_page=params.per_page,
            pages=pages,
        )
