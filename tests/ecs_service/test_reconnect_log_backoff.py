"""Reconnect log noise: failed attempts log at info on attempts 1-4, then at most once
per ``RECONNECT_LOG_EVERY_S`` wall seconds; a successful connect resets the count. The
retry timing itself is unchanged (``CONNECT_RETRY_S``)."""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace

from fake_port import FakePort
from terminal_ecs_service import core as core_mod
from terminal_ecs_service.core import CONNECT_RETRY_S, EcsCore
from terminal_model.types import Kind, Position

LAYOUT = SimpleNamespace(blocks=[SimpleNamespace(block="A", bays=2, rows=2, tiers=2)])
MSG = "equipment link not available"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _infos(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.levelno == logging.INFO and r.getMessage().startswith(MSG)]


def _down():
    raise ConnectionError("gateway down")


async def _fail(core, clock, n: int) -> float:
    """``n`` failed attempts ``CONNECT_RETRY_S`` apart (as ``run()``); returns the
    elapsed wall time from the first to the last attempt."""
    start = clock.t
    for i in range(n):
        if i:
            clock.t += CONNECT_RETRY_S
        assert await core.connect_once() is False
    return clock.t - start


def test_constant():
    assert core_mod.RECONNECT_LOG_EVERY_S == 30.0


async def test_reconnect_logging_backs_off(monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(core_mod, "_monotonic", clock)
    core = EcsCore(LAYOUT, connect=_down)
    with caplog.at_level(logging.INFO, logger="terminal_ecs_service.core"):
        elapsed = await _fail(core, clock, 20)
    assert elapsed == 38.0
    assert len(_infos(caplog)) == 4 + math.floor(elapsed / 30)


async def test_reconnect_logging_long_outage(monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(core_mod, "_monotonic", clock)
    core = EcsCore(LAYOUT, connect=_down)
    with caplog.at_level(logging.INFO, logger="terminal_ecs_service.core"):
        elapsed = await _fail(core, clock, 300)  # ~10 min
    assert len(_infos(caplog)) == 4 + math.floor(elapsed / 30)


async def test_reconnect_log_counter_resets_on_success(monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(core_mod, "_monotonic", clock)
    up = {"ok": False}
    fake = FakePort({"STS-01": (Kind.STS, Position(0, 0))})

    def connect():
        if not up["ok"]:
            raise ConnectionError("gateway down")
        return fake

    core = EcsCore(LAYOUT, connect=connect)
    with caplog.at_level(logging.INFO, logger="terminal_ecs_service.core"):
        await _fail(core, clock, 10)
        assert len(_infos(caplog)) == 4
        up["ok"] = True
        clock.t += CONNECT_RETRY_S
        assert await core.connect_once() is True
        core.on_link_lost()
        up["ok"] = False
        caplog.clear()
        clock.t += CONNECT_RETRY_S
        await _fail(core, clock, 6)
    assert len(_infos(caplog)) == 4


class _BindFails(FakePort):
    def equipment_ids(self):
        raise ConnectionError("lost while binding")


async def test_reconnect_log_counter_not_reset_by_failed_bind(monkeypatch, caplog):
    """``connect`` returning a port whose bind fails is not a success: the outage goes
    on and its attempts stay quiet."""
    clock = Clock()
    monkeypatch.setattr(core_mod, "_monotonic", clock)
    state = {"bind_fails": False}
    bad = _BindFails({"STS-01": (Kind.STS, Position(0, 0))})

    def connect():
        if state["bind_fails"]:
            return bad
        raise ConnectionError("gateway down")

    core = EcsCore(LAYOUT, connect=connect)
    with caplog.at_level(logging.INFO, logger="terminal_ecs_service.core"):
        await _fail(core, clock, 6)
        state["bind_fails"] = True
        clock.t += CONNECT_RETRY_S
        assert await core.connect_once() is False
        state["bind_fails"] = False
        caplog.clear()
        clock.t += CONNECT_RETRY_S
        await _fail(core, clock, 3)
    assert _infos(caplog) == []
