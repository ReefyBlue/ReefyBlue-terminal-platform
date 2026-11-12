"""``EquipmentPort`` over OPC UA: the client side of the aggregation gateway.

The port owns a background thread running its own asyncio loop with one asyncua
client. Every public method is synchronous and safe to call from any thread
except the port's own loop thread. Events arrive on the loop thread, are buffered
in a queue and delivered to subscribers on the caller's thread by ``drain_events``.

Events: one OPC UA event subscription on the gateway's ``Server`` object (filter
``TerminalEventType``, server-side queue, no coalescing). Each notification is
decoded synchronously and queued, so delivery order is notification order. The
``Seq`` field is per equipment; a jump of more than one is logged as a gap.

Connection loss (detected by asyncua's watchdog) sets ``_lost``; from then on
``snapshot``, ``wait_for_sim_time``, ``drain_events`` (after delivering what was
buffered) and every server call raise ``ConnectionError``
at once instead of waiting out a call timeout. The port does not reconnect.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Self, TypeVar

from asyncua import Client, Node, ua
from terminal_model.port import EquipmentEvent, EquipmentSnapshot
from terminal_model.types import Job, Kind, Position, State

from terminal_opcua.codec import extra_from_json, job_to_json
from terminal_opcua.events import (
    EventTypes,
    event_from_ua,
    resolve_event_types,
    subscribe_terminal_events,
)
from terminal_opcua.nodeset import EQUIPMENT_VARS, NAMESPACE_URI

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 3.0       # reads and method calls; above the gateway's 2 s forward timeout,
                           # so the gateway's answer (False) wins over a local timeout
SETUP_TIMEOUT_S = 10.0     # browse + subscribe after the session is open
CLOSE_TIMEOUT_S = 2.0
SUBSCRIPTION_PERIOD_MS = 20
POLL_S = 0.01

T = TypeVar("T")


@dataclass
class _Equipment:
    equipment_id: str
    obj: Node
    variables: dict[str, Node]
    methods: dict[str, ua.NodeId]
    last_seq: int = 0  # last Seq received (0: none yet)

    @property
    def snapshot_nodes(self) -> list[Node]:
        return [self.variables[n] for n in EQUIPMENT_VARS]


async def _children(node: Node, client: Client) -> dict[ua.NodeClass, dict[str, Node]]:
    """Children of ``node`` grouped by node class, keyed by browse name (one browse)."""
    out: dict[ua.NodeClass, dict[str, Node]] = collections.defaultdict(dict)
    for d in await node.get_children_descriptions():
        out[d.NodeClass][d.BrowseName.Name] = client.get_node(d.NodeId)
    return out


class _Handler:
    """asyncua subscription handler; runs on the port's loop thread."""

    def __init__(self, port: OpcUaEquipmentPort) -> None:
        self.port = port

    async def datachange_notification(self, node: Node, val: Any, data: Any) -> None:
        try:
            await self.port._on_datachange(node, val)
        except Exception:
            log.exception("handling datachange for %s failed", node)

    async def event_notification(self, event: Any) -> None:
        try:
            self.port._on_event(event)
        except Exception:
            log.exception("handling event %s failed", event)

    def status_change_notification(self, status: Any) -> None:
        log.warning("subscription status changed: %s", status)


class OpcUaEquipmentPort:
    """``EquipmentPort`` backed by the OPC UA gateway at ``endpoint``."""

    def __init__(self, endpoint: str, *, connect_timeout_s: float = 2.0) -> None:
        self.endpoint = endpoint
        self.connect_timeout_s = connect_timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Client | None = None
        self._subscription: Any = None
        self._equipment: dict[str, _Equipment] = {}
        self._by_node: dict[ua.NodeId, str] = {}
        self._clock_methods: dict[str, ua.NodeId] = {}
        self._clock: Node | None = None
        # block -> (rack ids in zone order, their ContainerId nodes, the zone's Online node)
        self._zones: dict[str, tuple[list[str], list[Node], Node]] = {}
        self._zone_offline: set[str] = set()  # blocks whose stale occupancy was logged
        self._events: queue.SimpleQueue[EquipmentEvent] = queue.SimpleQueue()
        self._callbacks: list[Callable[[EquipmentEvent], None]] = []
        self._now = 0.0
        self._connected = False
        self._lost = False
        self._types: EventTypes | None = None

    # -- lifecycle -------------------------------------------------------------------
    def connect(self) -> None:
        """Open the session, browse the equipment and subscribe; ``ConnectionError`` on failure."""
        if self._loop is not None:
            raise RuntimeError("already connected")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="opcua-port", daemon=True)
        thread.start()
        self._loop, self._thread = loop, thread
        self._lost = False
        fut = asyncio.run_coroutine_threadsafe(self._connect(), loop)
        try:
            fut.result(timeout=self.connect_timeout_s + SETUP_TIMEOUT_S)
        except Exception as e:
            fut.cancel()
            self.close()
            raise ConnectionError(f"cannot connect to {self.endpoint}: {e!r}") from e
        self._connected = True
        log.info("connected to %s (%d equipment)", self.endpoint, len(self._equipment))

    async def _connect(self) -> None:
        client = Client(self.endpoint, timeout=self.connect_timeout_s)
        client.connection_lost_callback = self._on_connection_lost
        await asyncio.wait_for(client.connect(), self.connect_timeout_s)
        self._client = client
        idx = await client.get_namespace_index(NAMESPACE_URI)
        terminal = await client.nodes.objects.get_child(f"{idx}:Terminal")
        top = (await _children(terminal, client))[ua.NodeClass.Object]

        async def objects(folder: Node) -> list[tuple[str, Node]]:
            return sorted((await _children(folder, client))[ua.NodeClass.Object].items())

        found = [*await objects(top["Quay"]), *await objects(top["Fleet"])]
        ascs: list[tuple[str, Node]] = []
        for name, block in await objects(top["Yard"]):
            if name.startswith("Block-"):
                ascs.extend(await objects(block))
        found.extend(sorted(ascs))
        for eid, obj in found:
            kids = await _children(obj, client)
            variables = kids[ua.NodeClass.Variable]
            missing = [n for n in EQUIPMENT_VARS if n not in variables]
            if missing:
                raise ValueError(f"{eid}: missing variables {missing}")
            methods = {n: m.nodeid for n, m in kids[ua.NodeClass.Method].items()}
            self._equipment[eid] = _Equipment(eid, obj, variables, methods)

        if "TransferZones" in top:
            for block, zone in await objects(top["TransferZones"]):
                zkids = (await _children(zone, client))
                racks = []
                for rack_id, rack in (await _children(zkids[ua.NodeClass.Object]["Racks"], client))[
                        ua.NodeClass.Object].items():
                    rvars = (await _children(rack, client))[ua.NodeClass.Variable]
                    racks.append((int(await rvars["Index"].read_value()), rack_id,
                                  rvars["ContainerId"]))
                racks.sort(key=lambda r: r[0])
                self._zones[block] = ([r[1] for r in racks], [r[2] for r in racks],
                                      zkids[ua.NodeClass.Variable]["Online"])

        self._clock = top["Clock"]
        clock_kids = await _children(self._clock, client)
        sim_time = clock_kids[ua.NodeClass.Variable]["SimTime"]
        self._clock_methods = {n: m.nodeid for n, m in clock_kids[ua.NodeClass.Method].items()}

        self._now = float(await sim_time.read_value())
        self._types = await resolve_event_types(client)

        self._by_node[sim_time.nodeid] = "SimTime"
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, _Handler(self))
        self._subscription = sub
        await subscribe_terminal_events(client, sub, self._types)
        await sub.subscribe_data_change(sim_time, sampling_interval=0.0)

    async def _on_connection_lost(self, exc: Exception) -> None:
        self._lost = True
        log.warning("connection to %s lost: %r", self.endpoint, exc)

    def _check_lost(self) -> None:
        if self._lost:
            raise ConnectionError(f"connection to {self.endpoint} lost")

    def close(self) -> None:
        """Disconnect and stop the loop thread; safe to call more than once."""
        loop, thread = self._loop, self._thread
        if loop is None:
            self._events = queue.SimpleQueue()  # undelivered events belong to this session
            return
        self._connected = False
        if loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
            try:
                fut.result(timeout=CLOSE_TIMEOUT_S + 1.0)
            except Exception as e:
                log.debug("disconnect from %s failed: %r", self.endpoint, e)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=CLOSE_TIMEOUT_S)
        if not loop.is_running():
            loop.close()
        # after the loop thread stopped, so no late notification lands in the new queue
        self._events = queue.SimpleQueue()  # undelivered events belong to this session
        self._loop = self._thread = None
        self._client = self._subscription = self._clock = self._types = None
        self._equipment, self._by_node, self._clock_methods = {}, {}, {}
        self._zones, self._zone_offline = {}, set()

    async def _disconnect(self) -> None:
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), CLOSE_TIMEOUT_S)
            except Exception as e:
                log.debug("disconnect from %s failed: %r", self.endpoint, e)
        me = asyncio.current_task()
        rest = [t for t in asyncio.all_tasks() if t is not me]
        for t in rest:
            t.cancel()
        await asyncio.gather(*rest, return_exceptions=True)

    def __enter__(self) -> Self:
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- loop plumbing ---------------------------------------------------------------
    def _run(self, coro: Coroutine[Any, Any, T], timeout: float = CALL_TIMEOUT_S) -> T:
        if self._loop is None or not self._connected:
            coro.close()
            raise RuntimeError("port not connected")
        if self._lost:
            coro.close()
            self._check_lost()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def _get(self, equipment_id: str) -> _Equipment:
        if not self._connected:
            raise RuntimeError("port not connected")
        return self._equipment[equipment_id]  # KeyError for unknown id

    def _call(self, obj: Node, method: ua.NodeId, *args: Any) -> Any:
        """Call ``method`` on ``obj``; raises on timeout or UA error."""
        return self._run(obj.call_method(method, *args))

    def _call_bool(self, label: str, obj: Node, methods: dict[str, ua.NodeId],
                   name: str, *args: Any) -> bool:
        try:
            return bool(self._call(obj, methods[name], *args))
        except Exception as e:
            log.warning("%s: %s failed: %r", label, name, e)
            return False

    # -- EquipmentPort -----------------------------------------------------------------
    def equipment_ids(self) -> list[str]:
        """STS, then AGV, then ASC; each group in id order."""
        return list(self._equipment)

    def snapshot(self, equipment_id: str) -> EquipmentSnapshot:
        eq = self._get(equipment_id)
        assert self._client is not None
        values = self._run(self._client.read_values(eq.snapshot_nodes))
        v = dict(zip(EQUIPMENT_VARS, values, strict=True))
        return EquipmentSnapshot(
            equipment_id=v["Id"] or equipment_id,
            kind=Kind(v["Kind"]),
            vendor=v["Vendor"],
            state=State(v["State"]),
            position=Position(float(v["PositionX"]), float(v["PositionY"])),
            current_job_id=v["CurrentJobId"] or None,
            energy_kwh=float(v["EnergyKwh"]),
            extra=extra_from_json(v["Extra"]) if v["Extra"] else {},
        )

    def assign_job(self, equipment_id: str, job: Job) -> bool:
        eq = self._get(equipment_id)
        return self._call_bool(equipment_id, eq.obj, eq.methods, "AssignJob", job_to_json(job))

    def cancel_job(self, equipment_id: str) -> bool:
        eq = self._get(equipment_id)
        return self._call_bool(equipment_id, eq.obj, eq.methods, "CancelJob")

    def reset(self, equipment_id: str) -> bool:
        eq = self._get(equipment_id)
        return self._call_bool(equipment_id, eq.obj, eq.methods, "Reset")

    def subscribe(self, callback: Callable[[EquipmentEvent], None]) -> None:
        self._callbacks.append(callback)

    def now(self) -> float:
        """Last SimTime received (read once at connect, then by subscription)."""
        return self._now

    # -- RackOccupancyPort --------------------------------------------------------------
    def rack_occupancy(self, block: str) -> dict[str, str | None]:
        """``{rack_id: container_id | None}`` of ``block``'s transfer zone (``TransferZone``
        objects, mirrored by the gateway), racks in zone order; ``KeyError`` for a block
        without one. While the gateway's link to the zone is down (``Online`` False) the last
        mirrored values are returned and a warning is logged once per outage."""
        if not self._connected:
            raise RuntimeError("port not connected")
        rack_ids, nodes, online = self._zones[block]
        assert self._client is not None
        values = self._run(self._client.read_values([online, *nodes]))
        if not values[0]:
            if block not in self._zone_offline:
                self._zone_offline.add(block)
                log.warning("transfer zone %s offline at the gateway: occupancy may be stale", block)
        else:
            self._zone_offline.discard(block)
        return {rid: (str(v) if v else None) for rid, v in zip(rack_ids, values[1:], strict=True)}

    def zone_blocks(self) -> list[str]:
        """Blocks with a ``TransferZone`` object (empty on a legacy gateway)."""
        return list(self._zones)

    # -- beyond the protocol -----------------------------------------------------------
    def drain_events(self) -> int:
        """Deliver every buffered event, in arrival order, to all callbacks; return the count.
        ``ConnectionError`` after the delivery if the connection is (or has been) lost."""
        n = 0
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                self._check_lost()
                return n
            n += 1
            for cb in list(self._callbacks):
                cb(ev)

    def prefill_yard(self, slots: list[tuple[str, str]]) -> bool:
        """``Terminal/Clock.PrefillYard`` with ``[(slot, container_id), ...]``."""
        if not self._connected or self._clock is None:
            raise RuntimeError("port not connected")
        payload = json.dumps([{"slot": s, "container_id": c} for s, c in slots])
        return self._call_bool("Clock", self._clock, self._clock_methods, "PrefillYard", payload)

    def start_sim(self) -> bool:
        """``Terminal/Clock.Start``: let a paused sim clock run; True if the sim accepted."""
        if not self._connected or self._clock is None:
            raise RuntimeError("port not connected")
        return self._call_bool("Clock", self._clock, self._clock_methods, "Start")

    def stop_sim(self) -> bool:
        """``Terminal/Clock.Stop``; True if the call went through."""
        if not self._connected or self._clock is None:
            raise RuntimeError("port not connected")
        try:
            self._call(self._clock, self._clock_methods["Stop"])
        except Exception as e:
            log.warning("Clock: Stop failed: %r", e)
            return False
        return True

    def wait_for_sim_time(self, t: float, timeout_s: float) -> bool:
        """Block until ``now() >= t``; False after ``timeout_s``.
        ``ConnectionError`` as soon as the connection is (or has been) lost."""
        deadline = time.monotonic() + timeout_s
        while self._now < t:
            self._check_lost()
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_S)
        return True

    # -- notifications (loop thread) ---------------------------------------------------
    async def _on_datachange(self, node: Node, val: Any) -> None:
        if self._by_node.get(node.nodeid) == "SimTime":
            self._now = float(val)

    def _on_event(self, event: Any) -> None:
        """Decode, gap-check and queue one event notification (synchronous: keeps order)."""
        if self._types is None:
            return
        try:
            seq, ev = event_from_ua(event, self._types)
        except ValueError as e:
            log.warning("undecodable event skipped: %s", e)
            return
        eq = self._equipment.get(ev.equipment_id)
        if eq is None:
            log.warning("event for unknown equipment %r skipped", ev.equipment_id)
            return
        if eq.last_seq and seq > eq.last_seq + 1:
            log.warning("%s: Seq gap %d -> %d", eq.equipment_id, eq.last_seq, seq)
        eq.last_seq = max(eq.last_seq, seq)
        self._events.put(ev)
