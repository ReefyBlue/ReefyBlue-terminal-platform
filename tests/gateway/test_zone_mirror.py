"""The gateway mirrors the sim's v2 equipment objects and ``TransferZone`` servers, and the
OPC UA port serves ``rack_occupancy`` from that mirror (``RackOccupancyPort``)."""

from __future__ import annotations

import asyncio
import datetime
import socket
import time
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from terminal_gateway.gateway import Gateway
from terminal_gateway.serve import GatewayLayout, _Sts, load_gateway_layout
from terminal_model.port import RackOccupancyPort, RackOccupancyUnavailable
from terminal_opcua.nodeset import get_var
from terminal_opcua.port import OpcUaEquipmentPort
from terminal_sim.layout import load_layout
from terminal_sim.opcua_servers import EquipmentServer, ZoneServer
from terminal_sim.scenario import load_scenario
from terminal_sim.world import make_world

ROOT = Path(__file__).resolve().parents[2]

_module_loop = pytest.mark.asyncio(loop_scope="module")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_base_port(n_slots: int) -> int:
    for _ in range(50):
        base = _free_port() % 20000 + 20000
        socks = []
        try:
            for p in range(base, base + n_slots):
                sock = socket.socket()
                socks.append(sock)
                sock.bind(("127.0.0.1", p))
            return base
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
    raise RuntimeError("no free port range")


async def _until(pred, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while not await pred():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(step)
    return True


def test_gateway_layout_carries_the_zones():
    g = load_gateway_layout(ROOT / "terminal_mv_small.yaml")
    sim = load_layout(ROOT / "terminal_mv_small.yaml")
    assert g.v2 is True
    assert [z.block for z in g.zones] == [z.block for z in sim.terminal.transfer_zones]
    legacy = load_gateway_layout(ROOT / "terminal.yaml")
    assert (legacy.v2, legacy.zones) == (False, ())


def test_zone_ports_follow_the_clock():
    g = load_gateway_layout(ROOT / "terminal_mv_small.yaml")
    gw = Gateway(g, upstream_host="h", base_port=30000, n_sts=3, n_agv=12)
    assert gw.control_port == 30023
    assert [(z.block, p) for z, p in gw.zone_table] == [("A", 30024), ("B", 30025), ("C", 30026),
                                                        ("D", 30027)]
    legacy = Gateway(load_gateway_layout(ROOT / "terminal.yaml"), upstream_host="h", base_port=30000)
    assert (legacy.v2, legacy.zone_table, legacy.control_port) == (False, [], 30012)


class Rig:
    async def sync(self) -> None:
        """What SimService does per tick for these servers."""
        await asyncio.gather(self.sts.sync(), *(z.sync() for z in self.zones.values()))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def rig():
    """STS-01 of a ``World2`` (v2 object type) + the zone servers of blocks A and B behind a
    gateway (no clock server: the port only needs the gateway's Clock object)."""
    r = Rig()
    layout = load_layout(ROOT / "terminal_mv_small.yaml")
    scenario = replace(load_scenario(ROOT / "tests/e2e/fixtures/mv_small_baseline.yaml", layout),
                       n_sts=1, n_agv=0)
    r.world = make_world(layout, scenario)
    zones = tuple(z for z in layout.terminal.transfer_zones if z.block in ("A", "B"))
    glayout = GatewayLayout(sts=[_Sts("STS-01")], blocks=[], v2=True, zones=zones)
    base = _free_base_port(4)  # STS-01, clock, zone A, zone B
    r.sts = EquipmentServer(r.world.equipment["STS-01"], port=base, host="127.0.0.1", v2=True)
    r.zones = {z.block: ZoneServer(z, r.world.racks, port=base + 2 + i, host="127.0.0.1")
               for i, z in enumerate(zones)}
    await asyncio.gather(r.sts.start(), *(z.start() for z in r.zones.values()))
    await r.sync()
    r.gateway = Gateway(glayout, port=_free_port(), upstream_host="127.0.0.1", base_port=base,
                        n_sts=1, n_agv=0, host="127.0.0.1", reconnect_s=0.5)
    await r.gateway.start()
    assert await _until(lambda: _true(r.gateway.mirrors["STS-01"].connected
                                      and all(z.connected for z in r.gateway.zones.values())), 10.0)
    r.port = OpcUaEquipmentPort(r.gateway.endpoint, connect_timeout_s=5.0)
    await asyncio.to_thread(r.port.connect)
    yield r
    await asyncio.to_thread(r.port.close)
    await r.gateway.stop()
    await asyncio.gather(r.sts.stop(), *(z.stop() for z in r.zones.values()))


async def _true(v: bool) -> bool:
    return v


async def _occ(r: Rig, block: str) -> dict:
    return await asyncio.to_thread(r.port.rack_occupancy, block)


@_module_loop
async def test_port_is_a_rack_occupancy_port(rig):
    assert isinstance(rig.port, RackOccupancyPort)
    assert rig.port.zone_blocks() == ["A", "B"]
    zone = rig.world.terminal.transfer_zone("A")
    assert await _occ(rig, "A") == {r.id: None for r in zone.racks}  # zone order
    assert list(await _occ(rig, "A")) == [r.id for r in zone.racks]
    with pytest.raises(KeyError):
        await _occ(rig, "C")  # no zone mirrored for C


@_module_loop
async def test_rack_occupancy_follows_the_sim(rig):
    zone = rig.world.terminal.transfer_zone("B")
    rack = zone.racks[1].id
    rig.world.racks.put(rack, "MSKU1234565")
    await rig.sync()
    want = rig.world.port.rack_occupancy("B")  # the in-process port: the same answer
    assert await _until(lambda: _eq(_occ(rig, "B"), want), 3.0)
    assert want[rack] == "MSKU1234565"
    rig.world.racks.take(rack)
    await rig.sync()
    assert await _until(lambda: _eq(_occ(rig, "B"), rig.world.port.rack_occupancy("B")), 3.0)


async def _eq(coro, want) -> bool:
    return await coro == want


@_module_loop
async def test_v2_sts_vars_mirrored(rig):
    sts = rig.world.equipment["STS-01"]
    lane = next(iter(sts.lane_buffer))
    sts.lane_buffer[lane] = "BOX-1"
    sts.holding = "BOX-2"
    try:
        await rig.sync()
        mirror = rig.gateway.mirrors["STS-01"]

        async def mirrored() -> bool:
            return (await mirror._mirrored("Holding") == "BOX-2"
                    and '"BOX-1"' in await mirror._mirrored("LaneBuffer"))

        assert await _until(mirrored, 3.0)
        snap = await asyncio.to_thread(rig.port.snapshot, "STS-01")
        # the ECS rack-mode keys arrive through Extra
        assert snap.extra["holding"] == "BOX-2" and snap.extra["lane_buffer"][lane] == "BOX-1"
        assert await mirror._mirrored("TechnicalStatus") == "GREEN"
    finally:
        sts.lane_buffer[lane] = None
        sts.holding = None
        await rig.sync()


@_module_loop
async def test_zone_link_loss_is_unavailable_never_stale(rig):
    """While the gateway's link to a zone is down the port refuses to answer
    (``RackOccupancyUnavailable``, not the last mirrored values, not ``ConnectionError``)."""
    zone = rig.zones["A"]
    rack = rig.world.terminal.transfer_zone("A").racks[0].id
    rig.world.racks.put(rack, "TGHU0000001")
    await rig.sync()
    assert await _until(lambda: _eq(_occ(rig, "A"), rig.world.port.rack_occupancy("A")), 3.0)
    mirror = rig.gateway.zones["A"]
    await zone.stop()
    try:
        assert await _until(lambda: _true(not mirror.connected), 5.0)
        assert await (await mirror._local(None, "Online")).read_value() is False
        with pytest.raises(RackOccupancyUnavailable):
            await _occ(rig, "A")
        assert (await _occ(rig, "B")) == rig.world.port.rack_occupancy("B")  # other zones answer
    finally:
        rig.world.racks.take(rack)
        await zone.start()
        await zone.sync()
    assert await _until(lambda: _true(mirror.connected), 10.0)
    assert await _until(lambda: _eq(_occ(rig, "A"), rig.world.port.rack_occupancy("A")), 3.0)
    assert await (await mirror._local(None, "Online")).read_value() is True


@_module_loop
async def test_zone_last_data_received_at_mirrored(rig):
    mirror = rig.gateway.zones["B"]
    up = rig.zones["B"]

    async def same() -> bool:
        return (await (await mirror._local(None, "LastDataReceivedAt")).read_value()
                == await (await get_var(up._obj, "LastDataReceivedAt")).read_value())

    rig.world.racks.put(rig.world.terminal.transfer_zone("B").racks[0].id, "STAMP-1")
    await rig.sync()
    try:
        assert await _until(same, 3.0)
        stamp = await (await mirror._local(None, "LastDataReceivedAt")).read_value()
        assert isinstance(stamp, datetime.datetime) and stamp.year >= 2026
    finally:
        rig.world.racks.take(rig.world.terminal.transfer_zone("B").racks[0].id)
        await rig.sync()


@_module_loop
async def test_v2_mirror_link_loss_marks_status_offline(rig):
    """A lost equipment link: State OFFLINE and the derived TechnicalStatus RED /
    OperationalMode Offline; the upstream's values return on reconnect."""
    mirror = rig.gateway.mirrors["STS-01"]
    await rig.sts.stop()
    try:
        async def offline() -> bool:
            return (await mirror._mirrored("State") == "OFFLINE"
                    and await mirror._mirrored("TechnicalStatus") == "RED"
                    and await mirror._mirrored("OperationalMode") == "Offline")

        assert await _until(offline, 5.0)
    finally:
        await rig.sts.start()
        await rig.sync()

    async def back() -> bool:
        return (mirror.connected and await mirror._mirrored("TechnicalStatus") == "GREEN"
                and await mirror._mirrored("OperationalMode") == "Automatic")

    assert await _until(back, 10.0)
