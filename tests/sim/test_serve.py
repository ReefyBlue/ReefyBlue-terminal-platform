import asyncio
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from asyncua import Client
import pytest
from terminal_model.locations import SlotLoc
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario
from terminal_sim.serve import SimService

ROOT = Path(__file__).resolve().parents[2]

SCENARIO = """name: t
seed: 1
n_sts: 1
n_agv: 2
n_containers: 4
share_40ft: 0.5
discharge_share: 1.0
speed_factor: 100
duration_s: 1000
"""


def _free_base_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] % 20000 + 20000  # base..base+n_equipment stay below 40000


def _scenario(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(SCENARIO)
    return p


CONTROL_OFFSET = 7  # SCENARIO's fleet on terminal.yaml: 1 STS + 2 AGV + 4 ASC


async def _control(port):
    c = Client(f"opc.tcp://127.0.0.1:{port + CONTROL_OFFSET}/terminal/control")
    await c.connect()
    idx = await c.get_namespace_index("urn:terminal:model")
    obj = await c.nodes.objects.get_child([f"{idx}:Terminal", f"{idx}:Clock"])
    return c, idx, obj


async def test_sim_time_advances_with_pacing(tmp_path):
    port = _free_base_port()
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=port, host="127.0.0.1", autostart=True)
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(100):  # startup: ~1 s CPU per server
            try:
                c, idx, obj = await _control(port)
                break
            except OSError:
                await asyncio.sleep(0.5)
        try:
            t0 = await (await obj.get_child(f"{idx}:SimTime")).read_value()
            await asyncio.sleep(0.5)
            t1 = await (await obj.get_child(f"{idx}:SimTime")).read_value()
            assert 25 <= t1 - t0 <= 75
            assert await (await obj.get_child(f"{idx}:SpeedFactor")).read_value() == 100.0
        finally:
            await c.disconnect()
    finally:
        svc.stop()
        await asyncio.wait_for(task, 10)


async def test_prefill_yard_occupies(tmp_path):
    port = _free_base_port()
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=port, host="127.0.0.1")
    for s in [*svc.servers.values(), svc.control]:
        await s.start()
    try:
        c, idx, obj = await _control(port)
        try:
            payload = '[{"slot":"A/1/1/1","container_id":"L1"}]'
            assert await obj.call_method(f"{idx}:PrefillYard", payload) is True
            assert svc.world.yard.locate("L1") == SlotLoc("A", 1, 1, 1)
            svc.world.run(until=1.0)
            assert await obj.call_method(f"{idx}:PrefillYard", '[{"slot":"A/1/1/2","container_id":"L2"}]') is False
            assert svc.world.yard.locate("L2") is None
        finally:
            await c.disconnect()
    finally:
        for s in [*svc.servers.values(), svc.control]:
            await s.stop()


async def test_prefill_yard_is_atomic(tmp_path):
    port = _free_base_port()
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=port, host="127.0.0.1")
    for s in [*svc.servers.values(), svc.control]:
        await s.start()
    try:
        c, idx, obj = await _control(port)
        try:
            dup_slot = '[{"slot":"A/1/1/1","container_id":"D1"},{"slot":"A/1/1/1","container_id":"D2"}]'
            assert await obj.call_method(f"{idx}:PrefillYard", dup_slot) is False
            dup_cid = '[{"slot":"A/1/1/1","container_id":"D1"},{"slot":"A/1/1/2","container_id":"D1"}]'
            assert await obj.call_method(f"{idx}:PrefillYard", dup_cid) is False
            bad_block = '[{"slot":"A/1/1/1","container_id":"D1"},{"slot":"Z/1/1/1","container_id":"D2"}]'
            assert await obj.call_method(f"{idx}:PrefillYard", bad_block) is False
            out_of_range = '[{"slot":"A/1/1/1","container_id":"D1"},{"slot":"A/999/1/1","container_id":"D2"}]'
            assert await obj.call_method(f"{idx}:PrefillYard", out_of_range) is False
            for cid in ("D1", "D2"):
                assert svc.world.yard.locate(cid) is None
        finally:
            await c.disconnect()
    finally:
        for s in [*svc.servers.values(), svc.control]:
            await s.stop()


async def test_sim_starts_paused_until_start(tmp_path, caplog):
    port = _free_base_port()
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=port, host="127.0.0.1")
    caplog.set_level("INFO", logger="terminal_sim.serve")
    task = asyncio.create_task(svc.run())
    try:
        for _ in range(100):
            try:
                c, idx, obj = await _control(port)
                break
            except OSError:
                await asyncio.sleep(0.5)
        try:
            sim_time = await obj.get_child(f"{idx}:SimTime")
            assert await sim_time.read_value() == 0.0
            await asyncio.sleep(1.0)
            assert await sim_time.read_value() == 0.0
            assert svc.world.now() == 0.0
            assert any("waiting for Start" in r.getMessage() for r in caplog.records)
            assert await obj.call_method(f"{idx}:Start") is True
            await asyncio.sleep(0.5)
            assert await sim_time.read_value() > 0.0
            assert await obj.call_method(f"{idx}:Start") is True  # idempotent
        finally:
            await c.disconnect()
    finally:
        svc.stop()
        await asyncio.wait_for(task, 10)


async def test_stop_while_paused_ends_run(tmp_path):
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=_free_base_port(), host="127.0.0.1")
    svc.stop()
    await asyncio.wait_for(svc.run(), 60)
    assert svc.world.now() == 0.0


def test_autostart_flag_parsed(monkeypatch, tmp_path):
    from terminal_sim import serve
    seen = {}

    class Fake:
        def __init__(self, *a, **kw):
            seen.update(kw)

    async def fake_amain(service):
        return None

    monkeypatch.setattr(serve, "SimService", Fake)
    monkeypatch.setattr(serve, "_amain", fake_amain)
    serve.main([str(_scenario(tmp_path)), "--autostart"])
    assert seen["autostart"] is True
    serve.main([str(_scenario(tmp_path))])
    assert seen["autostart"] is False


def test_speed_factor_flag_overrides_scenario(monkeypatch, tmp_path):
    from terminal_sim import serve
    seen = {}

    class Fake:
        def __init__(self, scenario, *a, **kw):
            seen["scenario"] = scenario

    async def fake_amain(service):
        return None

    monkeypatch.setattr(serve, "SimService", Fake)
    monkeypatch.setattr(serve, "_amain", fake_amain)
    serve.main([str(_scenario(tmp_path)), "--speed-factor", "3"])
    assert seen["scenario"].speed_factor == 3.0
    serve.main([str(_scenario(tmp_path))])
    assert seen["scenario"].speed_factor == 100.0  # from the yaml
    with pytest.raises(SystemExit):
        serve.main([str(_scenario(tmp_path)), "--speed-factor", "0"])


def test_control_slot_follows_the_fleet(tmp_path):
    """Clock server at ``base_port + n_equipment`` (n_sts + n_agv + one ASC per block)."""
    from dataclasses import replace
    legacy = load_layout(ROOT / "terminal.yaml")
    small = SimService(load_scenario(_scenario(tmp_path)), legacy, base_port=30000)  # 1 + 2 + 4
    assert small.control.port == 30007
    assert [s.port for s in small.servers.values()] == list(range(30000, 30007))
    full = SimService(replace(load_scenario(_scenario(tmp_path)), n_sts=2, n_agv=6), legacy,
                      base_port=30000)
    assert full.control.port == 30012  # the legacy fleet keeps base_port + 12
    mv_small = load_layout(ROOT / "terminal_mv_small.yaml")
    big = SimService(replace(load_scenario(_scenario(tmp_path)), n_sts=3, n_agv=12), mv_small,
                     base_port=30000)
    assert big.control.port == 30019


def test_sigterm_exits_cleanly(tmp_path):
    port = _free_base_port()
    err_file = (tmp_path / "stderr.txt").open("w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "terminal_sim.serve", str(_scenario(tmp_path)),
         "--base-port", str(port), "--host", "127.0.0.1"],
        cwd=ROOT, stderr=err_file, stdout=subprocess.DEVNULL,  # file: a full pipe would block the child
    )
    try:
        deadline = time.monotonic() + 45  # 13 servers start in ~15 s
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port + CONTROL_OFFSET), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("control port never opened")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    err_file.seek(0)
    err = err_file.read()
    assert proc.returncode == 0
    assert "Traceback" not in err
