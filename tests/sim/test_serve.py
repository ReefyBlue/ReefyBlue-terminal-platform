import asyncio
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from asyncua import Client
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
speed_factor: 200
duration_s: 1000
"""


def _free_base_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1] % 20000 + 20000  # base..base+12 stay below 40000


def _scenario(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(SCENARIO)
    return p


async def _control(port):
    c = Client(f"opc.tcp://127.0.0.1:{port + 12}/terminal/control")
    await c.connect()
    idx = await c.get_namespace_index("urn:terminal:model")
    obj = await c.nodes.objects.get_child([f"{idx}:Terminal", f"{idx}:Clock"])
    return c, idx, obj


async def test_sim_time_advances_with_pacing(tmp_path):
    port = _free_base_port()
    svc = SimService(load_scenario(_scenario(tmp_path)), load_layout(ROOT / "terminal.yaml"),
                     base_port=port, host="127.0.0.1")
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
            assert 60 <= t1 - t0 <= 140
            assert await (await obj.get_child(f"{idx}:SpeedFactor")).read_value() == 200.0
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
                socket.create_connection(("127.0.0.1", port + 12), timeout=0.5).close()
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
