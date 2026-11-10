"""Mirrors of one upstream OPC UA server (an equipment or the clock/control server)."""

from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import Callable

from asyncua import Client, Node, Server, ua
from terminal_model.port import EquipmentEvent, EquipmentSnapshot, EventKind
from terminal_model.types import Kind, Position, State
from terminal_opcua.codec import event_to_json
from terminal_opcua.nodeset import (
    EQUIPMENT_VARS,
    KIND_VARS,
    NAMESPACE_URI,
    EquipmentHandlers,
    Types,
    create_control_object,
    create_equipment_object,
    get_var,
    to_variant,
)

log = logging.getLogger(__name__)

SUBSCRIPTION_PERIOD_MS = 50
METHOD_TIMEOUT_S = 2.0
RECONNECT_S = 5.0
CONNECT_TIMEOUT_S = 2.0
WATCHDOG_S = 1.0
EVENT_QUEUE = 100  # server-side FIFO per monitored item: no coalescing between publishes
FAULT_REASON = "UPSTREAM_DISCONNECTED"


async def _safe_disconnect(client: Client) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 2.0)
    except Exception:  # dead transport: nothing to close cleanly
        log.debug("disconnect of %s failed", client.server_url, exc_info=True)


class _UpstreamLink:
    """Owns one asyncua client: connect, run until lost, reconnect every ``reconnect_s``."""

    name = "upstream"

    def __init__(self, endpoint: str, *, reconnect_s: float = RECONNECT_S) -> None:
        self.endpoint = endpoint
        self.reconnect_s = reconnect_s
        self._client: Client | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _launch(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"mirror:{self.name}")

    async def _run(self) -> None:
        failures = 0
        while not self._stopping:
            client = Client(self.endpoint, timeout=CONNECT_TIMEOUT_S, watchdog_intervall=WATCHDOG_S)
            lost = asyncio.Event()

            async def on_lost(_exc: Exception, lost=lost) -> None:
                lost.set()

            client.connection_lost_callback = on_lost
            was_up = opened = False
            try:
                await client.connect()  # cleans up its own socket on failure
                opened = True
                await self._on_connected(client)
                self._client = client
                was_up = True
                failures = 0
                log.info("%s: connected to %s", self.name, self.endpoint)
                await lost.wait()
                log.warning("%s: connection to %s lost", self.name, self.endpoint)
            except asyncio.CancelledError:
                self._client = None
                if opened:
                    await _safe_disconnect(client)
                raise
            except Exception as e:  # refused, timeout, browse failure: retry later
                failures += 1
                level = logging.WARNING if failures == 1 or was_up else logging.DEBUG
                log.log(level, "%s: upstream %s unavailable: %r", self.name, self.endpoint, e)
            self._client = None
            if opened:
                await _safe_disconnect(client)
            if not self._stopping:
                await self._on_lost()
                await asyncio.sleep(self.reconnect_s)

    async def _on_connected(self, client: Client) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _on_lost(self) -> None:
        return None

    async def _call(self, obj: Node | None, idx: int, method: str, *args):
        """Forward a method call upstream; ``None`` on any failure (logged)."""
        if obj is None or self._client is None:
            log.warning("%s: %s not forwarded, upstream unavailable", self.name, method)
            return None
        try:
            return await asyncio.wait_for(obj.call_method(f"{idx}:{method}", *args), METHOD_TIMEOUT_S)
        except Exception as e:
            log.warning("%s: forwarding %s failed: %r", self.name, method, e)
            return None

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


class EquipmentMirror(_UpstreamLink):
    """Gateway-side copy of one equipment object, kept in sync with its upstream server.

    EventSeq is mirrored verbatim, except that a synthesized Fault event (upstream lost)
    takes ``last + 1``. After such an event the upstream counter is shifted by an
    offset so the gateway sequence stays strictly increasing per event (see report).
    """

    def __init__(
        self, server: Server, idx: int, types: Types, parent: Node, *,
        equipment_id: str, kind: Kind, endpoint: str, reconnect_s: float = RECONNECT_S,
    ) -> None:
        super().__init__(endpoint, reconnect_s=reconnect_s)
        self.server = server
        self.idx = idx
        self.types = types
        self.parent = parent
        self.equipment_id = equipment_id
        self.name = equipment_id
        self.kind = kind
        self.var_names = (*EQUIPMENT_VARS, *KIND_VARS[kind])
        self.obj: Node | None = None
        self._sim_time = 0.0
        self._seq = 0          # last EventSeq written on the gateway
        self._offset = 0       # gateway seq = upstream seq + offset
        self._fault_announced = False
        self._write_lock = asyncio.Lock()
        self._path: list[str] = []
        self._uobj: Node | None = None
        self._uidx = 0
        self._names: dict[ua.NodeId, str] = {}
        self._le_queue: collections.deque[str] = collections.deque()
        self._last_upstream_le = ""

    def set_sim_time(self, t: float) -> None:
        self._sim_time = float(t)

    async def start(self) -> None:
        snap = EquipmentSnapshot(
            equipment_id=self.equipment_id, kind=self.kind, vendor="", state=State.FAULT,
            position=Position(0.0, 0.0), current_job_id=None, energy_kwh=0.0, extra={},
        )
        handlers = EquipmentHandlers(
            assign_job=self._assign, cancel_job=self._cancel, reset=self._reset)
        self.obj = await create_equipment_object(
            self.server, self.idx, self.types, self.parent, snap, handlers)
        self._path = await self._upstream_path()
        self._launch()

    async def _upstream_path(self) -> list[str]:
        """Browse names from ``Terminal`` down to the parent folder (same on upstream)."""
        names: list[str] = []
        node = self.parent
        while True:
            name = (await node.read_browse_name()).Name
            names.append(name)
            if name == "Terminal":
                break
            node = await node.get_parent()
            if node is None:
                raise ValueError(f"{self.equipment_id}: parent is not under Terminal")
        return [*reversed(names), self.equipment_id]

    # -- methods forwarded upstream ------------------------------------------------
    async def _forward_bool(self, method: str, *args) -> bool:
        return bool(await self._call(self._uobj, self._uidx, method, *args))

    async def _assign(self, job_json: str) -> bool:
        return await self._forward_bool("AssignJob", job_json)

    async def _cancel(self) -> bool:
        return await self._forward_bool("CancelJob")

    async def _reset(self) -> bool:
        return await self._forward_bool("Reset")

    # -- writes -------------------------------------------------------------------
    async def _write(self, name: str, value: object) -> None:
        assert self.obj is not None
        await (await get_var(self.obj, name)).write_value(to_variant(name, value))

    async def _write_event(self, last_event: str, seq: int) -> None:
        """LastEvent first, then EventSeq (clients read LastEvent on EventSeq change)."""
        await self._write("LastEvent", last_event)
        if seq != self._seq:
            self._seq = seq
            await self._write("EventSeq", seq)

    def _map_seq(self, upstream: int) -> int:
        mapped = int(upstream) + self._offset
        if mapped < self._seq:
            log.warning("%s: upstream EventSeq %d went backwards; holding %d",
                        self.equipment_id, upstream, self._seq)
            return self._seq
        return mapped

    async def _on_connected(self, client: Client) -> None:
        uidx = await client.get_namespace_index(NAMESPACE_URI)
        uobj = await client.nodes.objects.get_child([f"{uidx}:{n}" for n in self._path])
        nodes = [await uobj.get_child(f"{uidx}:{n}") for n in self.var_names]
        values = await client.read_values(nodes)
        current = dict(zip(self.var_names, values, strict=True))
        async with self._write_lock:
            for name, value in current.items():
                if name not in ("LastEvent", "EventSeq"):
                    await self._write(name, value)
            upstream_seq = int(current["EventSeq"])
            if upstream_seq + self._offset < self._seq:
                self._offset = self._seq - upstream_seq
                log.warning("%s: upstream EventSeq %d is behind the gateway's %d; "
                            "offsetting upstream sequence by %d",
                            self.equipment_id, upstream_seq, self._seq, self._offset)
            self._last_upstream_le = current["LastEvent"]
            self._le_queue.clear()
            await self._write_event(current["LastEvent"], upstream_seq + self._offset)
            self._fault_announced = False
        self._names = {n.nodeid: name for n, name in zip(nodes, self.var_names, strict=True)}
        self._uobj, self._uidx = uobj, uidx
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, self)
        await sub.subscribe_data_change(
            nodes, queuesize=EVENT_QUEUE, sampling_interval=0.0)

    async def datachange_notification(self, node: Node, val, data) -> None:
        name = self._names.get(node.nodeid)
        if name is None or self.obj is None:
            return
        try:
            if name == "LastEvent":
                # paired with the EventSeq notification that follows it (upstream
                # always writes LastEvent before EventSeq)
                self._le_queue.append(val)
                return
            if name == "EventSeq":
                async with self._write_lock:
                    if self._le_queue:
                        self._last_upstream_le = self._le_queue.popleft()
                    await self._write_event(self._last_upstream_le, self._map_seq(val))
                return
            await self._write(name, val)
        except Exception:
            log.exception("%s: mirroring %s failed", self.equipment_id, name)

    async def _on_lost(self) -> None:
        self._uobj = None
        if self._fault_announced or self.obj is None:
            return
        self._fault_announced = True
        ev = EquipmentEvent(
            EventKind.STATE_CHANGED, self.equipment_id, self._sim_time,
            reason=FAULT_REASON, new_state=State.FAULT)
        try:
            async with self._write_lock:
                await self._write("State", State.FAULT.value)
                await self._write_event(event_to_json(ev), self._seq + 1)
        except Exception:
            log.exception("%s: writing synthesized Fault failed", self.equipment_id)


class ControlMirror(_UpstreamLink):
    """``Terminal/Clock`` on the gateway: mirrors SimTime/SpeedFactor, forwards Stop/PrefillYard."""

    name = "control"

    def __init__(
        self, server: Server, idx: int, types: Types, parent: Node, *, endpoint: str,
        on_sim_time: Callable[[float], None], reconnect_s: float = RECONNECT_S,
    ) -> None:
        super().__init__(endpoint, reconnect_s=reconnect_s)
        self.server = server
        self.idx = idx
        self.types = types
        self.parent = parent
        self.on_sim_time = on_sim_time
        self.obj: Node | None = None
        self._uobj: Node | None = None
        self._uidx = 0
        self._names: dict[ua.NodeId, str] = {}

    async def start(self) -> None:
        self.obj = await create_control_object(
            self.server, self.idx, self.types, self.parent, stop=self._stop, prefill=self._prefill)
        self._launch()

    async def _stop(self) -> None:
        await self._call(self._uobj, self._uidx, "Stop")

    async def _prefill(self, json_str: str) -> bool:
        return bool(await self._call(self._uobj, self._uidx, "PrefillYard", json_str))

    async def _write(self, name: str, value: float) -> None:
        assert self.obj is not None
        await (await get_var(self.obj, name)).write_value(
            ua.Variant(float(value), ua.VariantType.Double))
        if name == "SimTime":
            self.on_sim_time(float(value))

    async def _on_connected(self, client: Client) -> None:
        uidx = await client.get_namespace_index(NAMESPACE_URI)
        uobj = await client.nodes.objects.get_child([f"{uidx}:Terminal", f"{uidx}:Clock"])
        names = ("SimTime", "SpeedFactor")
        nodes = [await uobj.get_child(f"{uidx}:{n}") for n in names]
        for name, value in zip(names, await client.read_values(nodes), strict=True):
            await self._write(name, value)
        self._names = {n.nodeid: name for n, name in zip(nodes, names, strict=True)}
        self._uobj, self._uidx = uobj, uidx
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, self)
        await sub.subscribe_data_change(nodes, sampling_interval=0.0)

    async def datachange_notification(self, node: Node, val, data) -> None:
        name = self._names.get(node.nodeid)
        if name is None or self.obj is None:
            return
        try:
            await self._write(name, val)
        except Exception:
            log.exception("control: mirroring %s failed", name)

    async def _on_lost(self) -> None:
        self._uobj = None
