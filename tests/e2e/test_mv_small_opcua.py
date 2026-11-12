"""Sim + gateway + ECS service on the v2 small layout (``terminal_mv_small.yaml``); the runner
drives ``fixtures/mv_small_baseline.yaml`` (3 STS, 12 AGVs, 200 containers) over HTTP.

23 devices (3 STS + 12 AGV + 4 blocks x 2 ASCs): the clock server sits on ``base_port + 23``,
the four transfer zone servers on ``base_port + 24..27``."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
import pytest
from asyncua import Client
from opcua_procs import ROOT, ecs_service, sim_and_gateway
from terminal_model.port import RackOccupancyPort
from terminal_opcua.nodeset import NAMESPACE_URI, V2_KIND_VARS
from terminal_opcua.port import OpcUaEquipmentPort
from terminal_runner.run import run_once_http
from terminal_sim.layout import load_layout
from terminal_sim.scenario import load_scenario

LAYOUT = ROOT / "terminal_mv_small.yaml"
SCENARIO = Path(__file__).parent / "fixtures" / "mv_small_baseline.yaml"

log = logging.getLogger(__name__)


async def _var_names(endpoint: str, path: list[str]) -> set[str]:
    async with Client(endpoint) as client:
        idx = await client.get_namespace_index(NAMESPACE_URI)
        obj = await client.nodes.objects.get_child([f"{idx}:{p}" for p in path])
        return {(await c.read_browse_name()).Name for c in await obj.get_children()}


@pytest.mark.e2e_opcua
def test_mv_small_v2_vars_and_racks_over_opcua(tmp_path):
    """Sim + gateway subprocesses on the v2 layout: the v2 variables of every kind and the
    transfer zones (``rack_occupancy`` from the gateway's mirror) reach an OPC UA port."""
    layout = load_layout(LAYOUT)
    scenario = load_scenario(SCENARIO, layout)
    with sim_and_gateway(SCENARIO, n_sts=scenario.n_sts, n_agv=scenario.n_agv, log_dir=tmp_path,
                         layout=LAYOUT) as procs:
        port = OpcUaEquipmentPort(procs["endpoint"], connect_timeout_s=5.0)
        port.connect()
        try:
            assert isinstance(port, RackOccupancyPort)
            assert port.zone_blocks() == [z.block for z in layout.terminal.transfer_zones]
            for zone in layout.terminal.transfer_zones:
                assert port.rack_occupancy(zone.block) == {r.id: None for r in zone.racks}
            sts, agv, asc = port.snapshot("STS-01"), port.snapshot("AGV-01"), port.snapshot("ASC-A-WS")
            # the ECS rack-mode keys (Extra)
            assert {"holding", "lane_buffer"} <= set(sts.extra)
            assert "loaded_container_id" in agv.extra and "holding" in asc.extra
        finally:
            port.close()
        for kind, path in ((sts.kind, ["Terminal", "Quay", "STS-01"]),
                           (agv.kind, ["Terminal", "Fleet", "AGV-01"]),
                           (asc.kind, ["Terminal", "Yard", "Block-A", "ASC-A-WS"])):
            names = asyncio.run(_var_names(procs["endpoint"], path))
            assert set(V2_KIND_VARS[kind]) <= names, kind
        racks = asyncio.run(_var_names(procs["endpoint"], ["Terminal", "TransferZones", "A", "Racks"]))
        assert racks == {r.id for r in layout.terminal.transfer_zone("A").racks}
    assert procs["returncodes"] == (0, 0), procs["logs"]


@pytest.mark.e2e_opcua
@pytest.mark.xfail(strict=True, reason="C1b Task 4/7: the v2 World2 needs the rack-mode dispatcher")
def test_mv_small_baseline_over_opcua(tmp_path):
    layout = load_layout(LAYOUT)
    scenario = load_scenario(SCENARIO, layout)
    with sim_and_gateway(SCENARIO, n_sts=scenario.n_sts, n_agv=scenario.n_agv, log_dir=tmp_path,
                         layout=LAYOUT) as procs, \
            ecs_service(procs["endpoint"], log_dir=tmp_path, layout=LAYOUT) as ecs:
        served = httpx.get(f"{ecs['url']}/layout", timeout=5.0).json()
        t0 = time.monotonic()
        k = run_once_http(scenario, layout, scenario.seed, ecs=ecs["url"], gateway=procs["endpoint"])
        wall = time.monotonic() - t0
    log.info("mv_small over OPC UA: %s (run %.1f s wall)", k, wall)
    assert procs["returncodes"] == (0, 0), procs["logs"]
    assert ecs["returncode"] == 0, ecs["log"]
    assert served["version"] == 2
    assert len(served["terminal"]["blocks"]) == 4
    assert k["containers_done"] == 200, k
    assert k["containers_failed"] == 0, k
    assert k["timed_out"] == 0, k
