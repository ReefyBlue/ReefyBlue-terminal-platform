"""Aggregation gateway: one OPC UA server exposing every equipment and the clock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from asyncua import Server
from terminal_model.types import Kind
from terminal_opcua.nodeset import (
    block_folder,
    ensure_folders,
    register_types,
    zones_folder,
)

from terminal_gateway.mirror import (
    RECONNECT_S,
    ControlMirror,
    EquipmentMirror,
    ZoneMirror,
)

log = logging.getLogger(__name__)



class _StsLike(Protocol):
    equipment_id: str


class _BlockLike(Protocol):
    block: str
    asc_id: str


class LayoutLike(Protocol):
    """The parts of ``terminal_sim.layout.Layout`` the gateway needs (duck-typed:
    the gateway must not import the simulator)."""

    sts: Sequence[_StsLike]
    blocks: Sequence[_BlockLike]


@dataclass(frozen=True)
class Upstream:
    equipment_id: str
    kind: Kind
    port: int
    block: str | None = None


class _RackLike(Protocol):
    id: str
    reefer: bool
    size: str


class _ZoneLike(Protocol):
    block: str
    racks: Sequence[_RackLike]


def layout_zones(layout: object) -> tuple[bool, list[_ZoneLike]]:
    """``(v2, transfer zones)`` of a layout: a ``GatewayLayout`` says so itself (``v2``,
    ``zones``); a simulator ``Layout`` is v2 when it carries ``terminal``. Legacy: (False, [])."""
    terminal = getattr(layout, "terminal", None)
    v2 = bool(getattr(layout, "v2", False)) or terminal is not None
    zones = getattr(layout, "zones", None)
    if zones is None:
        zones = terminal.transfer_zones if terminal is not None else ()
    return v2, list(zones)


def upstream_table(layout: LayoutLike, *, base_port: int, n_sts: int, n_agv: int) -> list[Upstream]:
    """Equipment in World order (STS, AGV, ASC) with ports ``base_port + i``."""
    table: list[Upstream] = []
    for spec in layout.sts[:n_sts]:
        table.append(Upstream(spec.equipment_id, Kind.STS, base_port + len(table)))
    for i in range(1, n_agv + 1):
        table.append(Upstream(f"AGV-{i:02d}", Kind.AGV, base_port + len(table)))
    for b in layout.blocks:
        table.append(Upstream(b.asc_id, Kind.ASC, base_port + len(table), block=b.block))
    return table


class Gateway:
    def __init__(
        self, layout: LayoutLike, *, port: int = 4840, upstream_host: str, base_port: int,
        n_sts: int = 2, n_agv: int = 6, host: str = "0.0.0.0", reconnect_s: float = RECONNECT_S,
    ) -> None:
        self.port = port
        self.host = host
        self.upstream_host = upstream_host
        self.base_port = base_port
        self.reconnect_s = reconnect_s
        self.endpoint = f"opc.tcp://{host}:{port}/terminal/gateway"
        self.table = upstream_table(layout, base_port=base_port, n_sts=n_sts, n_agv=n_agv)
        # The simulator's clock server: the slot after its equipment (base_port + n_equipment).
        self.control_port = base_port + len(self.table)
        # v2: equipment as the v2 object types, and the sim's zone servers after the clock
        # (base_port + n_equipment + 1 + i, layout order).
        self.v2, zones = layout_zones(layout)
        self.zone_table = [(z, self.control_port + 1 + i) for i, z in enumerate(zones)]
        self.zones: dict[str, ZoneMirror] = {}
        self.mirrors: dict[str, EquipmentMirror] = {}
        self.control: ControlMirror | None = None
        self._server: Server | None = None
        self._last_sim_time = 0.0

    def equipment_ids(self) -> list[str]:
        return [u.equipment_id for u in self.table]

    @property
    def last_sim_time(self) -> float:
        return self._last_sim_time

    def _on_sim_time(self, t: float) -> None:
        self._last_sim_time = t
        for m in self.mirrors.values():
            m.set_sim_time(t)

    def _url(self, port: int, path: str) -> str:
        return f"opc.tcp://{self.upstream_host}:{port}/terminal/{path}"

    async def start(self) -> None:
        """Serve immediately; upstreams are connected in background tasks."""
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("terminal-gateway")
        idx, types = await register_types(server, v2=self.v2)
        folders = await ensure_folders(server, idx, server.nodes.objects)
        await server.start()
        self._server = server
        try:
            await self._create_mirrors(server, idx, types, folders)
        except BaseException:
            await self.stop()
            self.mirrors.clear()
            self.zones.clear()
            self.control = None
            raise
        log.info("gateway serving %s (%d equipment)", self.endpoint, len(self.table))
        log.info("gateway control port %d", self.control_port)

    async def _create_mirrors(self, server: Server, idx: int, types, folders) -> None:
        for u in self.table:
            if u.kind == Kind.STS:
                parent = folders["Quay"]
            elif u.kind == Kind.AGV:
                parent = folders["Fleet"]
            else:
                assert u.block is not None
                parent = await block_folder(server, idx, folders["Yard"], u.block)
            m = EquipmentMirror(
                server, idx, types, parent, equipment_id=u.equipment_id, kind=u.kind,
                endpoint=self._url(u.port, u.equipment_id), reconnect_s=self.reconnect_s,
                v2=self.v2)
            await m.start()
            self.mirrors[u.equipment_id] = m
        self.control = ControlMirror(
            server, idx, types, folders["Terminal"],
            endpoint=self._url(self.control_port, "control"),
            on_sim_time=self._on_sim_time, reconnect_s=self.reconnect_s)
        await self.control.start()
        if self.zone_table:
            parent = await zones_folder(server, idx, folders["Terminal"])
            for zone, port in self.zone_table:
                z = ZoneMirror(server, idx, types, parent, zone=zone,
                               endpoint=self._url(port, f"zone/{zone.block}"),
                               reconnect_s=self.reconnect_s)
                await z.start()
                self.zones[zone.block] = z

    async def stop(self) -> None:
        links = [*self.mirrors.values(), *([self.control] if self.control else []),
                 *self.zones.values()]
        await asyncio.gather(*(m.stop() for m in links), return_exceptions=True)
        if self._server is not None:
            await self._server.stop()
            self._server = None
