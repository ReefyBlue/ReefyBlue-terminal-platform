"""The ECS over ``OpcUaEquipmentPort`` survives a gateway <-> equipment-server blip.

The gateway reports a lost upstream link as STATE_CHANGED -> OFFLINE (B2d): the
Dispatcher shows the device offline but does not replan; on reconnect the gateway
reports the upstream's real state (IDLE here) and the device is usable again
without an operator release (no FAULT, no job running during the blip).
"""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import test_opcua_port as base
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_gateway.gateway import Gateway
from terminal_model.port import EventKind
from terminal_model.types import JobType, State
from terminal_opcua.port import OpcUaEquipmentPort
from terminal_sim.layout import load_layout
from terminal_sim.opcua_servers import ControlServer, EquipmentServer
from terminal_sim.scenario import load_scenario
from terminal_sim.world import World
from terminal_tos_stub.allocator import SimpleSlotAllocator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from tcp_proxy import TcpProxy  # noqa: E402

ROOT = base.ROOT
AGV = "AGV-01"

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_dispatcher_uses_agv_again_after_link_blip():
    layout = load_layout(ROOT / "terminal.yaml")
    scenario = replace(load_scenario(ROOT / "scenarios/baseline.yaml"), n_sts=1, n_agv=1)
    world = World(layout, scenario)
    port_base = base._free_base_port()
    servers: dict[str, EquipmentServer] = {}
    proxy = None
    for i, (eid, eq) in enumerate(world.equipment.items()):
        if eid == AGV:  # gateway -> proxy on base + i -> AGV server on its own port
            servers[eid] = EquipmentServer(eq, port=base._free_port(), host="127.0.0.1")
            proxy = TcpProxy(port_base + i, servers[eid].port)
        else:
            servers[eid] = EquipmentServer(eq, port=port_base + i, host="127.0.0.1")
    assert proxy is not None
    world.port.subscribe(lambda ev: servers[ev.equipment_id].on_emit(ev))
    control = ControlServer(world, port=port_base + base.CONTROL_PORT_OFFSET,
                            on_stop=lambda: None, host="127.0.0.1", on_start=lambda: True)
    await proxy.start()
    await asyncio.gather(*(s.start() for s in servers.values()), control.start())
    await asyncio.gather(*(s.sync() for s in servers.values()), control.sync())
    gateway = Gateway(layout, port=base._free_port(), upstream_host="127.0.0.1",
                      base_port=port_base, n_sts=1, n_agv=1, host="127.0.0.1", reconnect_s=1.0)
    await gateway.start()
    port = OpcUaEquipmentPort(gateway.endpoint, connect_timeout_s=5.0)
    events = []
    try:
        assert await base._until(lambda: all(m.connected for m in gateway.mirrors.values()),
                                 10.0, step=0.05)
        await asyncio.to_thread(port.connect)
        port.subscribe(events.append)
        allocator = SimpleSlotAllocator({b.block: (b.bays, b.rows, b.tiers) for b in layout.blocks})
        dispatcher = await asyncio.to_thread(Dispatcher, port, allocator)

        async def drain_until(pred, timeout):
            async def step():
                await asyncio.to_thread(port.drain_events)  # dispatcher calls block on the port
                return pred()
            deadline = asyncio.get_running_loop().time() + timeout
            while not await step():
                if asyncio.get_running_loop().time() > deadline:
                    return False
                await asyncio.sleep(0.02)
            return True

        def agv_states():
            return [(e.new_state, e.reason) for e in events
                    if e.equipment_id == AGV and e.kind is EventKind.STATE_CHANGED]

        proxy.cut()
        assert await drain_until(
            lambda: (State.OFFLINE, "UPSTREAM_DISCONNECTED") in agv_states(), 5.0), events
        status = await asyncio.to_thread(dispatcher.status)
        assert status["equipment"][AGV] is State.OFFLINE
        assert (status["offline"], status["replans"], status["needs_release"]) == (1, 0, 0)

        proxy.restore()
        assert await drain_until(
            lambda: (State.IDLE, "UPSTREAM_RECONNECTED") in agv_states(), 6.0), events
        status = await asyncio.to_thread(dispatcher.status)
        assert status["equipment"][AGV] is State.IDLE
        assert (status["offline"], status["replans"], status["needs_release"],
                status["unknown_outcomes"]) == (0, 0, 0, 0)
        assert dispatcher.alarms() == []

        # the only AGV is usable again: a discharge order gets it
        order = WorkOrder("C1", JobType.DISCHARGE, "STS-01", allocator.next_slot("A"))
        await asyncio.to_thread(dispatcher.submit, [order])
        assigned = [lj.equipment_id for lj in dispatcher.job_log.values()]
        assert AGV in assigned, assigned
        assert any(j.type is JobType.TRANSPORT for j in dispatcher.jobs.values())
    finally:
        await asyncio.to_thread(port.close)
        await gateway.stop()
        await asyncio.gather(*(s.stop() for s in [*servers.values(), control]))
        await proxy.stop()
