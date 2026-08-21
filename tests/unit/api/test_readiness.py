from __future__ import annotations

from ansina.api.readiness import Readiness


def test_no_checks_registered_is_vacuously_ready() -> None:
    readiness = Readiness()

    assert readiness.is_ready is True
    assert readiness.snapshot() == {}


def test_ready_when_every_check_passes() -> None:
    readiness = Readiness()
    readiness.register("a", lambda: True)
    readiness.register("b", lambda: True)

    assert readiness.is_ready is True
    assert readiness.snapshot() == {"a": True, "b": True}


def test_not_ready_when_one_check_fails() -> None:
    readiness = Readiness()
    readiness.register("a", lambda: True)
    readiness.register("b", lambda: False)

    assert readiness.is_ready is False
    assert readiness.snapshot() == {"a": True, "b": False}


def test_checks_are_evaluated_fresh_each_time() -> None:
    readiness = Readiness()
    state = {"ok": False}
    readiness.register("dynamic", lambda: state["ok"])

    assert readiness.is_ready is False
    state["ok"] = True
    assert readiness.is_ready is True
