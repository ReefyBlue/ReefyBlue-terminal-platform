"""Scenario runner: World (sim) + Dispatcher (ecs) + TOS stub + KPI collector, in-process.

    python -m terminal_runner.run scenarios/baseline.yaml --replications 10 --out out/
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
from dataclasses import replace
from pathlib import Path

from terminal_ecs.dispatcher import Dispatcher
from terminal_model.logging import configure_json_logging
from terminal_sim.equipment.agv import AGV
from terminal_sim.layout import Layout, load_layout
from terminal_sim.scenario import Scenario, load_scenario
from terminal_sim.world import World
from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.generator import generate_workorders, prefill

from terminal_runner.kpi import SAMPLE_INTERVAL_S, KpiCollector

log = logging.getLogger("terminal_runner.run")


def _transfer_hook(world: World):
    def agv(agv_id: str) -> AGV:
        eq = world.equipment[agv_id]
        if not isinstance(eq, AGV):
            raise TypeError(f"{agv_id} is not an AGV")
        return eq

    def on_transfer(kind: str, eid: str, cid: str) -> None:
        if kind == "sts_to_agv":
            if world.transfer_sts_to_agv(eid, cid) is None:
                raise RuntimeError(f"no AGV in lane of {eid} for {cid}")
        elif kind == "agv_to_tp":
            got = world.transfer_agv_to_tp(eid)
            if got != cid:
                raise RuntimeError(f"{eid} unloaded {got} at TP, expected {cid}")
        elif kind == "tp_to_agv":
            agv(eid).load(cid)
        elif kind == "agv_to_sts":
            got = agv(eid).unload()
            if got != cid:
                raise RuntimeError(f"{eid} unloaded {got} at STS, expected {cid}")
        else:
            raise ValueError(f"unknown transfer kind {kind!r}")

    return on_transfer


def run_once(scenario: Scenario, layout: Layout, seed: int) -> dict[str, float]:
    """One run, stepped per KPI sample interval until all orders are DONE/FAILED or
    ``duration_s`` (hard cap). Rates are per simulated hour actually run."""
    scenario = replace(scenario, seed=seed)
    world = World(layout, scenario)
    allocator = SimpleSlotAllocator(layout.blocks)
    orders = generate_workorders(scenario, layout, allocator, rng=random.Random(seed))
    dispatcher = Dispatcher(world.port, allocator)
    kpi = KpiCollector(world.port, dispatcher, faults_total=world.faults_total)
    move_box = _transfer_hook(world)

    def on_transfer(kind: str, eid: str, cid: str) -> None:
        move_box(kind, eid, cid)
        kpi.observe_transfer(kind, eid, cid)

    dispatcher.on_transfer = on_transfer

    prefill(world.yard, orders)
    dispatcher.submit(orders)
    dispatcher.tick()
    t = 0.0
    timed_out = True
    while t < scenario.duration_s:
        t = min(t + SAMPLE_INTERVAL_S, scenario.duration_s)
        world.run(until=t)
        dispatcher.tick()
        kpi.sample()
        # Stop only once every order is DONE or FAILED: orders parked behind faulted
        # equipment have no running job and are not pending, but are still open.
        if dispatcher.status()["open"] == 0:
            timed_out = False
            break
    elapsed = world.now()
    kpis = kpi.finish(elapsed)
    kpis["sim_elapsed_s"] = elapsed
    kpis["timed_out"] = 1.0 if timed_out else 0.0
    return kpis


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def run_replications(
    scenario_path: Path,
    replications: int,
    out_dir: Path,
    layout_path: Path = Path("terminal.yaml"),
) -> Path:
    """Run ``replications`` seeds (``scenario.seed + i``); write ``<name>.csv`` and
    ``<name>-summary.json`` (per KPI mean / p5 / p95) to ``out_dir``. Returns the summary path."""
    scenario = load_scenario(scenario_path)
    layout = load_layout(layout_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{scenario.name}.csv"
    csv_path.unlink(missing_ok=True)

    runs: list[dict[str, float]] = []
    for i in range(replications):
        seed = scenario.seed + i
        kpis = run_once(scenario, layout, seed)
        KpiCollector.to_csv_row(csv_path, i, kpis)
        log.info(
            "run finished",
            extra={
                "run_id": i,
                "seed": seed,
                "containers_done": kpis["containers_done"],
                "moves_per_hour_total": kpis["moves_per_hour_total"],
                "timed_out": kpis["timed_out"],
            },
        )
        runs.append(kpis)

    summary = {}
    for key in runs[0]:
        vals = [r[key] for r in runs]
        summary[key] = {"mean": statistics.mean(vals), "p5": _percentile(vals, 5), "p95": _percentile(vals, 95)}
    summary_path = out_dir / f"{scenario.name}-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary_path


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal_runner.run", description=__doc__.splitlines()[0])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--replications", type=_positive_int, default=1)
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--layout", type=Path, default=Path("terminal.yaml"))
    args = parser.parse_args(argv)
    configure_json_logging()
    print(run_replications(args.scenario, args.replications, args.out, args.layout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
