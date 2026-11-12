"""One asyncua server per simulated Equipment."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from collections.abc import Callable

from asyncua import Server, ua
from terminal_model.layout import TransferZone
from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import EquipmentEvent
from terminal_model.types import Kind
from terminal_opcua.codec import job_from_json
from terminal_opcua.events import EventEmitter
from terminal_opcua.nodeset import (
    EquipmentHandlers,
    block_folder,
    create_control_object,
    create_equipment_object,
    create_zone_object,
    ensure_folders,
    get_var,
    register_types,
    write_snapshot,
    write_zone,
    zones_folder,
)

from terminal_sim.equipment.base import Equipment

log = logging.getLogger(__name__)


class EquipmentServer:
    """``v2``: the object is of the kind's v2 subtype (``STS2Type``...) with the C1b variables,
    and ``LastDataReceivedAt`` is the sim time of each sync."""

    def __init__(
        self, equipment: Equipment, *, port: int, host: str = "0.0.0.0", v2: bool = False,
    ) -> None:
        self.equipment = equipment
        self.v2 = v2
        self.port = port
        self.host = host
        self.endpoint = f"opc.tcp://{host}:{port}/terminal/{equipment.equipment_id}"
        self._server: Server | None = None
        self._obj = None
        self._emitter: EventEmitter | None = None
        self._pending: list[EquipmentEvent] = []

    def on_emit(self, ev: EquipmentEvent) -> None:
        """Synchronous; called from the sim loop. Events are flushed by sync()."""
        self._pending.append(ev)

    def _assign(self, job_json: str) -> bool:
        try:
            job = job_from_json(job_json)
        except ValueError as e:
            log.warning("%s: rejected AssignJob: %s", self.equipment.equipment_id, e)
            return False
        return self.equipment.assign_job(job)

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        idx, types = await register_types(server, v2=self.v2)
        folders = await ensure_folders(server, idx, server.nodes.objects)
        snap = self.equipment.snapshot()
        if snap.kind == Kind.STS:
            parent = folders["Quay"]
        elif snap.kind == Kind.AGV:
            parent = folders["Fleet"]
        else:
            parent = await block_folder(server, idx, folders["Yard"], snap.extra["block_id"])
        handlers = EquipmentHandlers(
            assign_job=self._assign,
            cancel_job=self.equipment.cancel_job,
            reset=self.equipment.reset,
        )
        self._obj = await create_equipment_object(
            server, idx, types, parent, snap, handlers, v2=self.v2)
        self._emitter = await EventEmitter.create(
            server, types.events, self._obj, self.equipment.equipment_id)
        await server.start()
        self._server = server

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def sync(self) -> None:
        assert self._obj is not None and self._emitter is not None, "server not started"
        if self.v2:
            await write_snapshot(self._obj, self.equipment.snapshot(), v2=True,
                                 sim_time=float(self.equipment.env.now()))
        else:
            await write_snapshot(self._obj, self.equipment.snapshot())
        pending, self._pending = self._pending, []
        for ev in pending:
            await self._emitter.emit(ev)
            await asyncio.sleep(0)


class ZoneServer:
    """One transfer zone of a v2 world (``World2``): ``Terminal/TransferZones/<block>`` with the
    occupant of each rack, refreshed by ``sync``. ``racks`` is the world's ``Racks``."""

    def __init__(self, zone: TransferZone, racks, *, port: int, host: str = "0.0.0.0") -> None:
        self.zone = zone
        self.racks = racks
        self.port = port
        self.host = host
        self.endpoint = f"opc.tcp://{host}:{port}/terminal/zone/{zone.block}"
        self._server: Server | None = None
        self._obj = None
        self._last: dict[str, str | None] | None = None  # occupancy at the last write

    def occupancy(self) -> dict[str, str | None]:
        return {r.id: self.racks.occupant(r.id) for r in self.zone.racks}

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        idx, types = await register_types(server, v2=True)
        folders = await ensure_folders(server, idx, server.nodes.objects)
        parent = await zones_folder(server, idx, folders["Terminal"])
        self._obj = await create_zone_object(
            server, idx, types, parent, self.zone.block,
            [(r.id, r.reefer, r.size) for r in self.zone.racks])
        self._last = None
        await self._write()
        await server.start()
        self._server = server

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def _write(self) -> None:
        """Write the occupancy; ``LastDataReceivedAt`` (UTC now) when it changed (or first)."""
        occ = self.occupancy()
        changed = datetime.datetime.now(datetime.UTC) if occ != self._last else None
        await write_zone(self._obj, occ, changed_at=changed)
        self._last = occ

    async def sync(self) -> None:
        assert self._obj is not None, "server not started"
        await self._write()


class ControlServer:
    """Clock + control server: ``Terminal/Clock`` with SimTime, SpeedFactor, Stop, PrefillYard,
    Start. ``Start`` calls ``on_start`` (False without one)."""

    def __init__(
        self, world, *, port: int, on_stop: Callable[[], None], host: str = "0.0.0.0",
        on_start: Callable[[], bool] | None = None,
    ) -> None:
        self.world = world
        self._on_start = on_start
        self.port = port
        self.host = host
        self.endpoint = f"opc.tcp://{host}:{port}/terminal/control"
        self._on_stop = on_stop
        self._server: Server | None = None
        self._obj = None

    def _start(self) -> bool:
        if self._on_start is None:
            log.warning("Start not supported by this control server")
            return False
        return bool(self._on_start())

    def _prefill(self, json_str: str) -> bool:
        if self.world.now() > 0:
            log.warning("PrefillYard rejected: simulation already running")
            return False
        try:
            items = json.loads(json_str)
            parsed = []
            for item in items:
                loc = parse_location(item["slot"])
                if not isinstance(loc, SlotLoc):
                    raise TypeError(f"not a slot: {item['slot']!r}")
                parsed.append((loc, str(item["container_id"])))
        except (ValueError, KeyError, TypeError) as e:
            log.warning("PrefillYard rejected: %s", e)
            return False
        yard = self.world.yard
        placed: list[SlotLoc] = []
        try:
            slots: set[SlotLoc] = set()
            cids: set[str] = set()
            for loc, cid in parsed:  # validate the whole batch before touching the yard
                if not yard.is_free(loc) or loc in slots:
                    raise ValueError("SLOT_OCCUPIED")
                if yard.locate(cid) is not None or cid in cids:
                    raise ValueError("CONTAINER_PLACED")
                slots.add(loc)
                cids.add(cid)
            for loc, cid in parsed:
                yard.occupy(loc, cid)
                placed.append(loc)
        except (ValueError, KeyError) as e:
            for loc in reversed(placed):  # top first: a v2 stack only releases tops
                yard.release(loc)
            log.warning("PrefillYard rejected: %r", e)
            return False
        return True

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        idx, types = await register_types(server)
        folders = await ensure_folders(server, idx, server.nodes.objects)
        self._obj = await create_control_object(
            server, idx, types, folders["Terminal"], stop=self._on_stop, prefill=self._prefill,
            start=self._start,
        )
        await server.start()
        self._server = server

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def sync(self) -> None:
        assert self._obj is not None, "server not started"
        await (await get_var(self._obj, "SimTime")).write_value(
            ua.Variant(float(self.world.now()), ua.VariantType.Double)
        )
        await (await get_var(self._obj, "SpeedFactor")).write_value(
            ua.Variant(float(self.world.scenario.speed_factor), ua.VariantType.Double)
        )
