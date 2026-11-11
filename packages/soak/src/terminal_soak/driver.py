"""Soak work driver (spec B2c §3.2): keeps the ECS supplied with work for days and
writes a measuring point to ``<out>/samples.jsonl`` every ``sample_every_s``.

Every ``loop_s`` wall seconds ``tick()``:

1. pages ``GET /containers`` from its cursor and folds the items into its ``Yard``
   (when the ECS answers ``next < since`` it restarted or pruned the history: paging
   starts again from 0);
2. compares the open ``LINK_LOST`` alarms with the previous tick: none -> some is a
   cut, some -> none its recovery (duration in wall seconds);
3. if ``/status`` answers and ``open < low_water``: posts ``make_batch()``.

``make_batch()`` writes the orders itself (ids ``SOAK-<batch>-<i>``): only DISCHARGE
while fewer than ``batch // 2`` slots are taken, else DISCHARGE/LOAD alternating;
a DISCHARGE takes a free slot, a LOAD a stored box (falling back to the other type
when none is available). ``generate_workorders`` is not used: it numbers its own ids
and puts all DISCHARGEs first.

``tick(now)``/``sample(now)`` take the wall time (epoch seconds) as an argument and
never sleep; ``run(hours)`` is the only place with real waits. No ECS error ends a
tick or a sample.

Usage: python -m terminal_soak.driver --ecs http://ecs:8000 --layout terminal.yaml --out /data/soak
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import httpx
from terminal_ecs.workorders import WorkOrder
from terminal_model.types import JobType
from terminal_tos_stub.client import EcsClient, WorkOrderRejected

from terminal_soak.yard import SlotState, Yard

log = logging.getLogger("terminal_soak.driver")

CONTAINERS_PAGE = 500  # the ECS's page size for GET /containers
HEALTH_WAIT_S = 120.0
_SOAK_ID = re.compile(r"^SOAK-(\d+)-\d+$")

T = TypeVar("T")


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat()


class Driver:
    def __init__(
        self,
        client: EcsClient,
        layout: Any,
        *,
        low_water: int = 40,
        batch: int = 60,
        loop_s: float = 10.0,
        sample_every_s: float = 3600.0,
        out_dir: Path | str,
    ) -> None:
        self.client = client
        self.layout = layout
        self.low_water = low_water
        self.batch = batch
        self.loop_s = loop_s
        self.sample_every_s = sample_every_s
        self.out_dir = Path(out_dir)
        self.yard = Yard.from_layout(layout)
        self._blocks = self.yard.blocks()
        self._sts = [s.equipment_id for s in layout.sts]
        self._cursor = 0
        self._batch_no = 0  # last batch number used (or seen in /containers)
        self.batches_posted = 0
        self.ecs_resets_seen = 0  # /containers answered next < since
        self.cuts_seen = 0
        self.recoveries: list[dict] = []
        self._cut_since: float | None = None
        self._stop = threading.Event()

    # -- helpers -----------------------------------------------------------------------
    def _safe(self, what: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except (httpx.HTTPError, ValueError, KeyError) as e:  # ValueError: a non-JSON body
            log.warning("%s failed: %s", what, e)
            return None

    def _fleet(self) -> None:
        """Restrict STS and blocks to the fleet the ECS serves (an STS or block without
        equipment would be rejected as UNKNOWN_STS / INVALID_SLOT)."""
        fleet = self._safe("GET /equipment", self.client.equipment)
        if not fleet:
            return
        sts = {e["equipment_id"] for e in fleet if e["kind"] == "STS"}
        ascs = {e["equipment_id"] for e in fleet if e["kind"] == "ASC"}
        self._sts = [s for s in (x.equipment_id for x in self.layout.sts) if s in sts] or self._sts
        self._blocks = [b.block for b in self.layout.blocks if b.asc_id in ascs] or self._blocks

    def stop(self) -> None:
        self._stop.set()

    # -- yard sync ---------------------------------------------------------------------
    def _poll_containers(self) -> None:
        """Page ``GET /containers`` from the cursor into the yard."""
        reset = False
        while True:
            page = self._safe("GET /containers", lambda: self.client.containers(self._cursor))
            if page is None:
                return
            nxt, items = int(page["next"]), page["items"]
            if nxt < self._cursor and not reset:
                log.warning("/containers answered next=%d < since=%d (ECS restarted?): paging from 0",
                            nxt, self._cursor)
                self.ecs_resets_seen += 1
                self._cursor = 0
                reset = True
                continue
            for it in items:
                self.yard.apply(it["container_id"], it["state"], it["job_type"], it["yard_slot"])
                if (m := _SOAK_ID.match(it["container_id"])) is not None:
                    self._batch_no = max(self._batch_no, int(m.group(1)))
            self._cursor = nxt
            if len(items) < CONTAINERS_PAGE:
                return

    def resume(self) -> None:
        """Rebuild the yard from ``GET /containers?since=0`` (a restarted driver
        continues where the ECS is) and read the fleet."""
        self.yard = Yard.from_layout(self.layout)
        self._cursor = 0
        self._fleet()
        self._poll_containers()
        log.info("resumed: %d slots taken (%d loadable), last batch %d",
                 self.yard.occupied_count, self.yard.loadable_count, self._batch_no)

    # -- work --------------------------------------------------------------------------
    def _discharge(self, cid: str, sts: str, block: str) -> WorkOrder | None:
        slot = self.yard.next_slot(block)
        if slot is None:
            return None
        self.yard.reserve_in(slot, cid)
        return WorkOrder(cid, JobType.DISCHARGE, sts, slot)

    def _load(self, cid: str, sts: str, block: str) -> WorkOrder | None:
        for b in [block] + [x for x in self._blocks if x != block]:
            picked = self.yard.pick_occupied(b)
            if picked is not None:
                self.yard.reserve_out(picked[0], cid)
                return WorkOrder(cid, JobType.LOAD, sts, picked[0])
        return None

    def make_batch(self) -> list[WorkOrder]:
        """The next batch, its slots reserved in the yard (see the module docstring)."""
        b = self._batch_no + 1
        discharge_only = self.yard.occupied_count < self.batch // 2
        orders: list[WorkOrder] = []
        for i in range(self.batch):
            cid = f"SOAK-{b}-{i}"
            sts = self._sts[i % len(self._sts)]
            block = self._blocks[i % len(self._blocks)]
            if discharge_only or i % 2 == 0:
                order = self._discharge(cid, sts, block) or self._load(cid, sts, block)
            else:
                order = self._load(cid, sts, block) or self._discharge(cid, sts, block)
            if order is None:
                break  # yard full and nothing to load
            orders.append(order)
        if orders:
            self._batch_no = b
        return orders

    def _rollback(self, orders: list[WorkOrder]) -> None:
        """Undo the reservations of a batch the ECS did not take."""
        for o in orders:
            entry = self.yard.state(o.yard_slot)
            if entry is None or entry[1] != o.container_id:
                continue
            if entry[0] is SlotState.IN:
                self.yard.free(o.yard_slot)
            elif entry[0] is SlotState.OUT:
                self.yard.occupy(o.yard_slot, o.container_id)

    def _submit(self) -> None:
        st = self._safe("GET /status", self.client.status_if_connected)
        if st is None or st.get("open", 0) >= self.low_water:
            return
        orders = self.make_batch()
        if not orders:
            log.warning("yard full and nothing to load: no batch")
            return
        try:
            accepted = self.client.post_workorders(orders)
        except WorkOrderRejected as e:
            log.error("batch %d rejected: %s", self._batch_no, e.errors[:3])
            self._rollback(orders)
            return
        except httpx.HTTPError as e:
            # Maybe taken (e.g. a timeout after the ECS accepted): if so, the next
            # /containers poll re-reserves the slots of its open orders.
            log.warning("posting batch %d failed: %s", self._batch_no, e)
            self._rollback(orders)
            return
        self.batches_posted += 1
        log.info("batch %d posted: %d orders (open was %d)", self._batch_no, accepted, st.get("open", 0))

    def _watch_alarms(self, now: float) -> None:
        alarms = self._safe("GET /alarms", self.client.alarms)
        if alarms is None:
            return
        link_lost = any(a.get("kind") == "LINK_LOST" for a in alarms)
        if link_lost and self._cut_since is None:
            self._cut_since = now
            self.cuts_seen += 1
            log.info("cut %d seen (LINK_LOST raised)", self.cuts_seen)
        elif not link_lost and self._cut_since is not None:
            rec = {"at": _iso(self._cut_since), "recovered_s": round(now - self._cut_since, 3)}
            self.recoveries.append(rec)
            self._cut_since = None
            log.info("cut recovered after %.0f s", rec["recovered_s"])

    def tick(self, now: float) -> None:
        self._poll_containers()
        self._watch_alarms(now)
        self._submit()

    # -- measuring ---------------------------------------------------------------------
    def sample(self, now: float) -> dict:
        """Append one measuring point (spec §3.2) to ``out_dir/samples.jsonl``."""
        status = self._safe("GET /status", self.client.status_if_connected) or {}
        kpis = self._safe("GET /kpis", self.client.kpis) or {}
        health = self._safe("GET /health", self.client.health_json) or {}
        alarms = self._safe("GET /alarms", self.client.alarms) or []
        line = {
            "wall_time": _iso(now),
            "sim_time": status.get("sim_time", health.get("sim_time")),
            "status": status,
            "kpis": kpis,
            "health": {"rss_mb": health.get("rss_mb"), "uptime_s": health.get("uptime_s"),
                       "connected": bool(health.get("connected", False))},
            "alarms_by_kind": dict(Counter(a.get("kind") for a in alarms)),
            "ui_ok": bool(self._safe("GET /ui/", self.client.ui_ok)),
            "cuts_seen": self.cuts_seen,
            "recoveries": list(self.recoveries),
            # extras (additive): an unrecovered cut, the yard, the driver's own counters
            "cut_open_since": _iso(self._cut_since) if self._cut_since is not None else None,
            "yard": {"occupied": self.yard.occupied_count, "loadable": self.yard.loadable_count,
                     "capacity": self.yard.capacity},
            "batches_posted": self.batches_posted,
            "ecs_resets_seen": self.ecs_resets_seen,
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with (self.out_dir / "samples.jsonl").open("a") as f:
            f.write(json.dumps(line) + "\n")
        return line

    def run(self, hours: float, *, clock: Callable[[], float] = time.time) -> int:
        """Wait for a healthy ECS (max ``HEALTH_WAIT_S``), resume, then tick every
        ``loop_s`` and sample at start, every ``sample_every_s`` and at the end, for
        ``hours`` or until ``stop()``. 1 when the ECS never became healthy."""
        if not self.client.wait_healthy(HEALTH_WAIT_S):
            log.error("ECS not healthy after %.0f s", HEALTH_WAIT_S)
            return 1
        self.resume()
        start = clock()
        end = start + hours * 3600.0
        self.sample(start)
        next_sample = start + self.sample_every_s
        while True:
            now = clock()
            self.tick(now)
            if now >= next_sample:
                self.sample(now)
                next_sample = max(next_sample + self.sample_every_s, now)  # no catch-up burst
            remaining = end - clock()
            if remaining <= 0 or self._stop.wait(min(self.loop_s, remaining)):
                break
        self.sample(clock())
        return 0


def main(argv: list[str] | None = None) -> int:
    from terminal_sim.layout import load_layout

    ap = argparse.ArgumentParser(prog="terminal_soak.driver", description=__doc__.split("\n\n")[0])
    ap.add_argument("--ecs", default="http://localhost:8000")
    ap.add_argument("--layout", default="terminal.yaml")
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--out", default="/data/soak")
    ap.add_argument("--low-water", type=int, default=40)
    ap.add_argument("--batch", type=int, default=60)
    ap.add_argument("--loop", type=float, default=10.0)
    ap.add_argument("--sample-every", type=float, default=3600.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    client = EcsClient(args.ecs)
    driver = Driver(client, load_layout(args.layout), low_water=args.low_water, batch=args.batch,
                    loop_s=args.loop, sample_every_s=args.sample_every, out_dir=Path(args.out))
    previous = signal.signal(signal.SIGTERM, lambda *_: driver.stop())  # last sample, then exit
    try:
        return driver.run(args.hours)
    finally:
        signal.signal(signal.SIGTERM, previous)
        client.close()


if __name__ == "__main__":
    sys.exit(main())
