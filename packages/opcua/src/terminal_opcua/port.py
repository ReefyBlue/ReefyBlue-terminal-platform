"""``EquipmentPort`` over OPC UA: the client side of the aggregation gateway.

The port owns a background thread running its own asyncio loop with one asyncua
client. Every public method is synchronous and safe to call from any thread
except the port's own loop thread. Events arrive on the loop thread, are buffered
in a queue and delivered to subscribers on the caller's thread by ``drain_events``.

Event pairing: the gateway writes ``LastEvent`` then bumps ``EventSeq``. Both are
monitored with a server-side queue (no coalescing); ``LastEvent`` values are kept
in a FIFO per equipment and each ``EventSeq`` notification pops the next one.
``EventSeq`` notifications are handled one at a time (``asyncio.Lock``) so events
keep their creation order even when the direct-read fallback awaits.

Connection loss (detected by asyncua's watchdog) sets ``_lost``; from then on
``snapshot``, ``wait_for_sim_time`` and every server call raise ``ConnectionError``
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
from dataclasses import dataclass, field
from typing import Any, Self, TypeVar

from asyncua import Client, Node, ua
from terminal_model.port import EquipmentEvent, EquipmentSnapshot
from terminal_model.types import Job, Kind, Position, State

from terminal_opcua.codec import event_from_json, extra_from_json, job_to_json
from terminal_opcua.nodeset import EQUIPMENT_VARS, NAMESPACE_URI

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 3.0       # reads and method calls; above the gateway's 2 s forward timeout,
                           # so the gateway's answer (False) wins over a local timeout
SETUP_TIMEOUT_S = 10.0     # browse + subscribe after the session is open
CLOSE_TIMEOUT_S = 2.0
SUBSCRIPTION_PERIOD_MS = 20
EVENT_QUEUE = 100          # server-side queue per monitored item (as the gateway uses)
POLL_S = 0.01

T = TypeVar("T")


@dataclass
class _Equipment:
    equipment_id: str
    obj: Node
    variables: dict[str, Node]
    methods: dict[str, ua.NodeId]
    initial_seq: int = 0
    last_seq: int = 0
    fifo: collections.deque[str] = field(default_factory=collections.deque)

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
        self._by_node: dict[ua.NodeId, tuple[_Equipment | None, str]] = {}
        self._clock_methods: dict[str, ua.NodeId] = {}
        self._clock: Node | None = None
        self._events: queue.SimpleQueue[EquipmentEvent] = queue.SimpleQueue()
        self._callbacks: list[Callable[[EquipmentEvent], None]] = []
        self._now = 0.0
        self._connected = False
        self._lost = False
        self._seq_lock: asyncio.Lock | None = None

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
        self._seq_lock = asyncio.Lock()
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

        self._clock = top["Clock"]
        clock_kids = await _children(self._clock, client)
        sim_time = clock_kids[ua.NodeClass.Variable]["SimTime"]
        self._clock_methods = {n: m.nodeid for n, m in clock_kids[ua.NodeClass.Method].items()}

        eqs = list(self._equipment.values())
        values = await client.read_values([*(e.variables["EventSeq"] for e in eqs), sim_time])
        for eq, seq in zip(eqs, values, strict=False):
            eq.initial_seq = eq.last_seq = int(seq)
        self._now = float(values[-1])

        watched: list[Node] = []
        for eq in eqs:
            for name in ("LastEvent", "EventSeq"):
                node = eq.variables[name]
                self._by_node[node.nodeid] = (eq, name)
                watched.append(node)
        self._by_node[sim_time.nodeid] = (None, "SimTime")
        sub = await client.create_subscription(SUBSCRIPTION_PERIOD_MS, _Handler(self))
        self._subscription = sub
        if watched:
            await sub.subscribe_data_change(watched, queuesize=EVENT_QUEUE, sampling_interval=0.0)
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
        self._client = self._subscription = self._clock = None
        self._equipment, self._by_node, self._clock_methods = {}, {}, {}

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

    # -- beyond the protocol -----------------------------------------------------------
    def drain_events(self) -> int:
        """Deliver every buffered event, in arrival order, to all callbacks; return the count."""
        n = 0
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
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
        eq, name = self._by_node.get(node.nodeid, (None, ""))
        if name == "SimTime":
            self._now = float(val)
        elif eq is None:
            return
        elif name == "LastEvent":
            eq.fifo.append(val)
        elif name == "EventSeq":
            await self._on_event_seq(eq, int(val))

    async def _on_event_seq(self, eq: _Equipment, seq: int) -> None:
        if self._seq_lock is None:  # only before _connect ran (tests)
            self._seq_lock = asyncio.Lock()
        async with self._seq_lock:
            await self._handle_event_seq(eq, seq)

    async def _handle_event_seq(self, eq: _Equipment, seq: int) -> None:
        if eq.fifo:
            payload = eq.fifo.popleft()
        elif seq <= eq.initial_seq:
            return
        else:
            log.warning("%s: EventSeq %d without a queued LastEvent; reading it directly",
                        eq.equipment_id, seq)
            payload = await eq.variables["LastEvent"].read_value()
        if seq <= eq.initial_seq:
            return  # initial notification after subscribing: state, not an event
        if seq > eq.last_seq + 1:
            log.warning("%s: EventSeq gap %d -> %d (%d event(s) missed)",
                        eq.equipment_id, eq.last_seq, seq, seq - eq.last_seq - 1)
        elif seq <= eq.last_seq:
            log.warning("%s: EventSeq %d not ahead of %d", eq.equipment_id, seq, eq.last_seq)
        eq.last_seq = max(eq.last_seq, seq)
        try:
            ev = event_from_json(payload)
        except (ValueError, TypeError) as e:
            log.warning("%s: EventSeq %d with undecodable LastEvent %r: %s",
                        eq.equipment_id, seq, payload, e)
            return
        self._events.put(ev)
