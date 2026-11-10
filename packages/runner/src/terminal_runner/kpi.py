"""KPI collection for a simulation run (event-driven counters + periodic samples).

AGV KPIs come from job events, using the dispatcher's ``job_log`` for job types,
locations and assignment times:

- ``agv_wait_sts_s_mean``: AGV arrival in an STS lane (TRANSPORT to ``STS-..``
  completed) until that order's crane job (DISCHARGE/LOAD) completed.
- ``agv_wait_tp_s_mean``: LOAD orders, AGV arrival at the TP (empty TRANSPORT to
  ``TP/..`` completed) until the assignment of that AGV's next TRANSPORT (the leg to
  the STS, per ``job_log``). The sim hands the box over on arrival, so this is the
  time the loaded AGV waits on the TP for the ECS.
- ``agv_travel_s_mean``: TRANSPORT duration, assignment until JOB_COMPLETED.

``faults`` is ``faults_total()`` when given (in-process: the sim's fault counter).
Without it (OPC UA mode, where the counter is not exposed) it is the number of
STATE_CHANGED -> FAULT events seen, which also counts gateway-synthesised faults
for a lost equipment server.
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
        self._first_move: float | None = None
        self._last_move: float | None = None
        self._busy_obs: dict[Kind, int] = defaultdict(int)
        self._total_obs: dict[Kind, int] = defaultdict(int)
        self._sts_arrival: dict[str, float] = {}  # container -> AGV arrived in the STS lane
        self._tp_arrival: dict[str, float] = {}  # job id of an empty leg to a TP -> arrival
        self._wait_sts: list[float] = []
        self._travel: list[float] = []
        self._fault_events = 0
        port.subscribe(self._on_event)

    def _on_event(self, ev: EquipmentEvent) -> None:
        if ev.kind is EventKind.STATE_CHANGED and ev.new_state is State.FAULT:
            self._fault_events += 1
        if ev.kind is not EventKind.JOB_COMPLETED:
            return
        logged = self.dispatcher.job_log.get(ev.job_id or "")
        jtype = logged.job.type.value if logged else _job_type(ev.job_id)
        if jtype in _CRANE_JOBS:
            self._moves[ev.equipment_id] += 1
            if self._first_move is None:
                self._first_move = ev.sim_time
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
            self._tp_arrival[ev.job_id] = ev.sim_time

    def _tp_wait(self) -> list[float]:
        """Pair each TP arrival of a LOAD leg with the next TRANSPORT assigned to that
        AGV (``job_log`` is in assignment order); unpaired arrivals are left out."""
        waits: list[float] = []
        arrived: dict[str, float] = {}  # AGV -> arrival at the TP, awaiting its next leg
        for job_id, logged in self.dispatcher.job_log.items():
            if logged.job.type is not JobType.TRANSPORT:
                continue
            agv = logged.equipment_id
            if (t := arrived.pop(agv, None)) is not None:
                waits.append(max(0.0, logged.assigned_at - t))
            if (t := self._tp_arrival.get(job_id)) is not None:
                arrived[agv] = t
        return waits

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
        kpis["agv_wait_tp_s_mean"] = _mean(self._tp_wait())
        kpis["agv_travel_s_mean"] = _mean(self._travel)
        for kind in (Kind.STS, Kind.AGV, Kind.ASC):
            kpis[f"utilisation_{kind.value}"] = self._utilisation(kind)
        # first to last completed crane move (DISCHARGE/LOAD)
        if self._last_move is not None and self._first_move is not None:
            kpis["vessel_turnaround_s"] = self._last_move - self._first_move
        else:
            kpis["vessel_turnaround_s"] = 0.0
        status = self.dispatcher.status()
        faults = self._faults_total() if self._faults_total is not None else self._fault_events
        kpis["faults"] = float(faults)
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
