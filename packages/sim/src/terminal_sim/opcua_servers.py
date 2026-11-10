"""One asyncua server per simulated Equipment."""

from __future__ import annotations

import asyncio
import logging

from asyncua import Server

from terminal_model.port import EquipmentEvent
from terminal_model.types import Kind
from terminal_opcua.codec import job_from_json
from terminal_opcua.nodeset import (
    EquipmentHandlers,
    EventEmitter,
    block_folder,
    create_equipment_object,
    ensure_folders,
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
