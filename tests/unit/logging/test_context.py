from __future__ import annotations

from ansina.logging.context import get_request_id, request_id_scope


def test_no_request_id_outside_scope() -> None:
    assert get_request_id() is None


def test_scope_generates_and_exposes_an_id() -> None:
    with request_id_scope() as request_id:
        assert request_id
        assert get_request_id() == request_id
    assert get_request_id() is None


def test_scope_accepts_an_explicit_id() -> None:
    with request_id_scope("caller-supplied-id") as request_id:
        assert request_id == "caller-supplied-id"
        assert get_request_id() == "caller-supplied-id"


def test_two_scopes_get_different_ids() -> None:
    with request_id_scope() as first:
        pass
    with request_id_scope() as second:
        pass
    assert first != second


def test_nested_scope_restores_outer_id_on_exit() -> None:
    with request_id_scope("outer") as outer:
        with request_id_scope("inner") as inner:
            assert inner == "inner"
            assert get_request_id() == "inner"
        assert get_request_id() == outer
    assert get_request_id() is None
