"""One asyncua server per simulated Equipment."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from asyncua import Server, ua
from terminal_model.locations import SlotLoc, parse_location
from terminal_model.port import EquipmentEvent
from terminal_model.types import Kind
from terminal_opcua.codec import job_from_json
from terminal_opcua.nodeset import (
    EquipmentHandlers,
    EventEmitter,
    block_folder,
    create_control_object,
    create_equipment_object,
    ensure_folders,
    get_var,
    register_types,
    write_snapshot,
)

from terminal_sim.equipment.base import Equipment

log = logging.getLogger(__name__)


class EquipmentServer:
    def __init__(self, equipment: Equipment, *, port: int, host: str = "0.0.0.0") -> None:
        self.equipment = equipment
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
        idx, types = await register_types(server)
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
        self._obj = await create_equipment_object(server, idx, types, parent, snap, handlers)
        self._emitter = EventEmitter(self._obj)
        await server.start()
        self._server = server

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def sync(self) -> None:
        assert self._obj is not None and self._emitter is not None, "server not started"
        await write_snapshot(self._obj, self.equipment.snapshot())
        pending, self._pending = self._pending, []
        for ev in pending:
            await self._emitter.emit(ev)
            await asyncio.sleep(0)


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
            for loc in placed:
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
