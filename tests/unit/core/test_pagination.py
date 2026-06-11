"""Tests for backend/core/pagination.py"""
from __future__ import annotations

from backend.core.pagination import Page, PaginationParams, get_pagination


def test_pagination_params_offset_and_limit():
    """offset/limit derive from page and per_page."""
    params = PaginationParams(page=3, per_page=20)
    assert params.offset == 40
    assert params.limit == 20


def test_pagination_first_page_has_zero_offset():
    params = PaginationParams(page=1, per_page=50)
    assert params.offset == 0


def test_get_pagination_builds_params():
    """The dependency wraps validated query values in PaginationParams."""
    params = get_pagination(page=2, per_page=25)
    assert params.page == 2
    assert params.per_page == 25


def test_page_create_computes_page_count():
    """Page count rounds up for partial final pages."""
    params = PaginationParams(page=1, per_page=10)
    page = Page.create(items=list(range(10)), total=25, params=params)
    assert page.pages == 3
    assert page.total == 25
    assert page.page == 1


def test_page_create_empty_results_is_one_page():
    """Zero results still reports one (empty) page, never zero."""
    params = PaginationParams(page=1, per_page=10)
    page = Page.create(items=[], total=0, params=params)
    assert page.pages == 1
    assert page.items == []
