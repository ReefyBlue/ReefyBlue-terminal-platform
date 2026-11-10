"""KPI collection for a simulation run (event-driven counters + periodic samples)."""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from terminal_ecs.dispatcher import Dispatcher
from terminal_model.port import EquipmentEvent, EquipmentPort, EventKind
from terminal_model.types import Kind, Position, State

SAMPLE_INTERVAL_S = 60.0
_CRANE_JOBS = frozenset({"DISCHARGE", "LOAD"})


def _job_type(job_id: str | None) -> str | None:
    parts = (job_id or "").split("-")
    return parts[1] if len(parts) > 1 else None


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
        self._transports = 0
        self._first_completed: float | None = None
        self._last_move: float | None = None
        self._busy_obs: dict[Kind, int] = defaultdict(int)
        self._total_obs: dict[Kind, int] = defaultdict(int)
        self._prev_pos: dict[str, Position] = {}
        self._agv_wait_s = 0.0
        self._agv_travel_s = 0.0
        port.subscribe(self._on_event)

    def _on_event(self, ev: EquipmentEvent) -> None:
        if ev.kind is not EventKind.JOB_COMPLETED:
            return
        if self._first_completed is None:
            self._first_completed = ev.sim_time
        jtype = _job_type(ev.job_id)
        if jtype in _CRANE_JOBS:
            self._moves[ev.equipment_id] += 1
            self._last_move = ev.sim_time
        elif jtype == "TRANSPORT":
            self._transports += 1

    def sample(self) -> None:
        for eid in self.port.equipment_ids():
            snap = self.port.snapshot(eid)
            busy = snap.state is State.BUSY
            self._total_obs[snap.kind] += 1
            self._busy_obs[snap.kind] += busy
            if snap.kind is Kind.AGV:
                prev = self._prev_pos.get(eid)
                if busy and prev is not None:
                    if snap.position == prev:
                        self._agv_wait_s += SAMPLE_INTERVAL_S
                    else:
                        self._agv_travel_s += SAMPLE_INTERVAL_S
                self._prev_pos[eid] = snap.position

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
        kpis["agv_wait_s_mean"] = self._agv_wait_s / self._transports if self._transports else 0.0
        kpis["agv_travel_s_mean"] = self._agv_travel_s / self._transports if self._transports else 0.0
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
