"""KPI collection for a simulation run (event-driven counters + periodic samples).

AGV KPIs come from job events, using the dispatcher's ``job_log`` for job types,
locations and assignment times:

- ``agv_wait_sts_s_mean``: AGV arrival in an STS lane (TRANSPORT to ``STS-..``
  completed) until that order's crane job (DISCHARGE/LOAD) completed.
- ``agv_wait_tp_s_mean``: LOAD orders, AGV arrival at the TP (empty TRANSPORT to
  ``TP/..`` completed) until the box is on the AGV (``tp_to_agv`` transfer, reported
  via ``observe_transfer``).
- ``agv_travel_s_mean``: TRANSPORT duration, assignment until JOB_COMPLETED.
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from terminal_ecs.dispatcher import Dispatcher
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind
from terminal_model.types import JobType, Kind, State

SAMPLE_INTERVAL_S = 60.0
_CRANE_JOBS = frozenset({"DISCHARGE", "LOAD"})


def _job_type(job_id: str | None) -> str | None:
    parts = (job_id or "").rsplit("-", 2)
    return parts[1] if len(parts) == 3 else None


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


class KpiCollector:
    def __init__(
        self,
        port: EquipmentPort,
        dispatcher: Dispatcher,
        *,
        faults_total: Callable[[], int] | None = None,
    ) -> None:
        self.port = port
        self.dispatcher = dispatcher
        self._faults_total = faults_total
        self._moves: dict[str, int] = defaultdict(int)
        self._first_completed: float | None = None
        self._last_move: float | None = None
        self._busy_obs: dict[Kind, int] = defaultdict(int)
        self._total_obs: dict[Kind, int] = defaultdict(int)
        self._sts_arrival: dict[str, float] = {}  # container -> AGV arrived in the STS lane
        self._tp_arrival: dict[str, float] = {}  # container -> AGV arrived at the TP (LOAD)
        self._tp_pickup: dict[str, float] = {}  # container -> box put on the AGV at the TP
        self._wait_sts: list[float] = []
        self._wait_tp: list[float] = []
        self._travel: list[float] = []
        port.subscribe(self._on_event)

    def _on_event(self, ev: EquipmentEvent) -> None:
        if ev.kind is not EventKind.JOB_COMPLETED:
            return
        if self._first_completed is None:
            self._first_completed = ev.sim_time
        logged = self.dispatcher.job_log.get(ev.job_id or "")
        jtype = logged.job.type.value if logged else _job_type(ev.job_id)
        if jtype in _CRANE_JOBS:
            self._moves[ev.equipment_id] += 1
            self._last_move = ev.sim_time
            if logged and (arrived := self._sts_arrival.pop(logged.job.container_id, None)) is not None:
                self._wait_sts.append(ev.sim_time - arrived)
        if logged is None or logged.job.type is not JobType.TRANSPORT:
            return
        job = logged.job
        self._travel.append(ev.sim_time - logged.assigned_at)
        if job.to_loc.startswith("STS-"):
            self._sts_arrival[job.container_id] = ev.sim_time
        elif job.to_loc.startswith("TP/") and job.from_loc.startswith("AGV-"):  # empty leg of a LOAD
            self._tp_arrival[job.container_id] = ev.sim_time
            self._pair_tp(job.container_id)

    def observe_transfer(self, kind: str, equipment_id: str, container_id: str) -> None:
        """Box hand-over reported by the dispatcher (chain it into ``on_transfer``)."""
        if kind == "tp_to_agv":
            self._tp_pickup[container_id] = self.port.now()
            self._pair_tp(container_id)

    def _pair_tp(self, cid: str) -> None:
        # The dispatcher reports tp_to_agv while handling the arrival, i.e. possibly
        # before this collector sees that JOB_COMPLETED: pair them in either order.
        if cid in self._tp_arrival and cid in self._tp_pickup:
            self._wait_tp.append(max(0.0, self._tp_pickup.pop(cid) - self._tp_arrival.pop(cid)))

    def sample(self) -> None:
        committed = self.dispatcher.committed_agvs()
        for eid in self.port.equipment_ids():
            snap = self.port.snapshot(eid)
            busy = snap.state is State.BUSY or (snap.kind is Kind.AGV and eid in committed)
            self._total_obs[snap.kind] += 1
            self._busy_obs[snap.kind] += busy

    def _utilisation(self, kind: Kind) -> float:
        total = self._total_obs[kind]
        return self._busy_obs[kind] / total if total else 0.0

    def finish(self, duration_s: float) -> dict[str, float]:
        hours = duration_s / 3600.0
        ids = self.port.equipment_ids()
        kpis: dict[str, float] = {}
        total_moves = 0
        per_sts: dict[str, float] = {}
        for eid in ids:
            if self.port.snapshot(eid).kind is Kind.STS:
                n = self._moves.get(eid, 0)
                total_moves += n
                per_sts[f"moves_per_hour_{eid}"] = n / hours if hours else 0.0
        kpis["moves_per_hour_total"] = sum(per_sts.values())
        kpis.update(per_sts)
        kpis["agv_wait_sts_s_mean"] = _mean(self._wait_sts)
        kpis["agv_wait_tp_s_mean"] = _mean(self._wait_tp)
        kpis["agv_travel_s_mean"] = _mean(self._travel)
        for kind in (Kind.STS, Kind.AGV, Kind.ASC):
            kpis[f"utilisation_{kind.value}"] = self._utilisation(kind)
        if self._last_move is not None and self._first_completed is not None:
            kpis["vessel_turnaround_s"] = self._last_move - self._first_completed
        else:
            kpis["vessel_turnaround_s"] = 0.0
        status = self.dispatcher.status()
        kpis["faults"] = float(self._faults_total()) if self._faults_total else 0.0
        kpis["replans"] = float(status["replans"])
        energy = sum(self.port.snapshot(eid).energy_kwh for eid in ids)
        kpis["energy_kwh_per_move"] = energy / total_moves if total_moves else 0.0
        kpis["containers_done"] = float(len(self.dispatcher.completed))
        kpis["containers_failed"] = float(status["failed"])
        kpis["containers_stuck"] = float(status["stuck"])
        return kpis

    @staticmethod
    def to_csv_row(path: Path, run_id: int, kpis: dict) -> None:
        path = Path(path)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(["run_id", *kpis])
            writer.writerow([run_id, *kpis.values()])
