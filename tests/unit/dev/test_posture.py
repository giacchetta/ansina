from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest

from ansina.config.settings import DevSettings, S3Settings, Settings, TelemetrySettings
from ansina.dev import posture as posture_module
from ansina.dev.posture import _install_shutdown_hook, dev_mode


def _settings(*, dev_enabled: bool) -> Settings:
    return Settings(
        telemetry=TelemetrySettings(
            enabled=True,
            spool_dir=Path("/tmp/ansina-telemetry"),
            s3=S3Settings(enabled=True, bucket="my-bucket"),
        ),
        dev=DevSettings(enabled=dev_enabled),
    )


def test_dev_mode_disabled_is_a_byte_for_byte_no_op(
    monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    calls: list[str] = []

    def _build(*args: Any, **kwargs: Any) -> None:
        calls.append("build")
        return None

    monkeypatch.setattr(posture_module, "build_vector_sidecar", _build)
    settings = _settings(dev_enabled=False)

    with dev_mode(settings):
        pass

    assert calls == []
    assert captured_logs() == []


def test_dev_mode_enabled_logs_the_banner_even_with_no_sidecar(
    monkeypatch: pytest.MonkeyPatch, captured_logs: Any
) -> None:
    monkeypatch.setattr(posture_module, "build_vector_sidecar", lambda *a, **k: None)
    settings = _settings(dev_enabled=True)

    with dev_mode(settings):
        pass

    lines = captured_logs()
    assert any(
        line["level"] == "WARNING" and "DEV MODE ENABLED" in line["message"]
        for line in lines
    )


class _FakeSidecar:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self) -> None:
        self.stopped = True


def test_dev_mode_enabled_starts_and_stops_the_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _FakeSidecar()
    monkeypatch.setattr(posture_module, "build_vector_sidecar", lambda *a, **k: sidecar)
    settings = _settings(dev_enabled=True)

    with dev_mode(settings):
        assert sidecar.started is True
        assert sidecar.stopped is False

    assert sidecar.stopped is True


def test_dev_mode_stops_the_sidecar_even_when_the_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = _FakeSidecar()
    monkeypatch.setattr(posture_module, "build_vector_sidecar", lambda *a, **k: sidecar)
    settings = _settings(dev_enabled=True)

    with pytest.raises(RuntimeError), dev_mode(settings):
        raise RuntimeError("boom")

    assert sidecar.stopped is True


# --- _install_shutdown_hook -----------------------------------------------------
#
# See `posture.py`'s own module docstring on `_install_shutdown_hook` for *why*
# this exists: `uvicorn.Server.capture_signals()` restores whatever handler was
# active before it ran and redelivers the original signal once its own graceful
# shutdown finishes, so hooking in *ahead of* `uvicorn.run()` is the only way
# cleanup code reliably runs on a real SIGINT/SIGTERM. These tests never send a
# real signal to the test process — `os.kill` is always patched out — and every
# test restores the handlers it touched via its own `finally`/fixture, so no
# signal disposition change ever leaks across tests.


@pytest.fixture
def restore_signal_handlers() -> Any:
    """Snapshots and restores SIGINT/SIGTERM's handlers around a test — belt and
    suspenders alongside each test's own `_uninstall()`/`finally` call, in case a
    test itself fails before reaching it.
    """
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


class _FakeSupervisedProcess:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def test_install_shutdown_hook_installs_a_handler_for_sigint_and_sigterm(
    restore_signal_handlers: None,
) -> None:
    sidecar = _FakeSupervisedProcess()

    uninstall = _install_shutdown_hook(sidecar)  # type: ignore[arg-type]

    assert signal.getsignal(signal.SIGINT) is not signal.SIG_DFL
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
    uninstall()


def test_install_shutdown_hook_uninstall_restores_the_previous_handlers(
    restore_signal_handlers: None,
) -> None:
    sentinel_int = signal.getsignal(signal.SIGINT)
    sentinel_term = signal.getsignal(signal.SIGTERM)
    sidecar = _FakeSupervisedProcess()

    uninstall = _install_shutdown_hook(sidecar)  # type: ignore[arg-type]
    uninstall()

    assert signal.getsignal(signal.SIGINT) is sentinel_int
    assert signal.getsignal(signal.SIGTERM) is sentinel_term


def test_installed_handler_stops_the_sidecar_restores_and_redelivers(
    restore_signal_handlers: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates uvicorn's own `capture_signals()` redelivering the signal once
    its graceful shutdown is done, by invoking the installed handler directly
    (a plain function call — exactly how Python itself would invoke it) rather
    than sending a real signal.
    """
    sidecar = _FakeSupervisedProcess()
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    sentinel_term = signal.getsignal(signal.SIGTERM)

    _install_shutdown_hook(sidecar)  # type: ignore[arg-type]
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    handler(signal.SIGTERM, None)  # simulate redelivery, no real signal sent

    assert sidecar.stop_calls == 1
    # The handler must restore the pre-hook disposition *before* redelivering,
    # so the redelivered signal invokes the original semantics, not itself again.
    assert signal.getsignal(signal.SIGTERM) is sentinel_term
    assert len(kill_calls) == 1
    assert kill_calls[0] == (os.getpid(), signal.SIGTERM)


# --- dev_mode x signal integration ----------------------------------------------


def test_dev_mode_installed_handler_stops_the_real_sidecar(
    monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None
) -> None:
    """End-to-end within `dev_mode()` itself: the handler installed while the
    `with` block's body is running must be the one that actually stops the
    sidecar this invocation built — not a stale one from a previous call.
    """
    sidecar = _FakeSidecar()
    monkeypatch.setattr(posture_module, "build_vector_sidecar", lambda *a, **k: sidecar)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    settings = _settings(dev_enabled=True)

    with dev_mode(settings):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert sidecar.stopped is True

    # The `finally`'s own `sidecar.stop()` call is harmless on an already-stopped
    # fake (no assertion needed beyond "no error raised" — `_FakeSidecar.stop()`
    # has no idempotency guard of its own, unlike the real `SupervisedProcess`,
    # but simply re-setting `stopped = True` is not itself a failure).


def test_dev_mode_with_no_sidecar_installs_no_signal_hook(
    monkeypatch: pytest.MonkeyPatch, restore_signal_handlers: None
) -> None:
    monkeypatch.setattr(posture_module, "build_vector_sidecar", lambda *a, **k: None)
    settings = _settings(dev_enabled=True)
    sentinel_term = signal.getsignal(signal.SIGTERM)

    with dev_mode(settings):
        assert signal.getsignal(signal.SIGTERM) is sentinel_term

    assert signal.getsignal(signal.SIGTERM) is sentinel_term
