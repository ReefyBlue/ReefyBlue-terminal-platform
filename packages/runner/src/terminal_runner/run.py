"""Scenario runner: Dispatcher (ecs) + TOS stub + KPI collector, against the sim in-process
or over OPC UA (a running ``terminal_sim.serve`` behind ``terminal_gateway.serve``).

    python -m terminal_runner.run scenarios/baseline.yaml --replications 10 --out out/
    python -m terminal_runner.run scenarios/baseline.yaml --port opcua --gateway opc.tcp://localhost:4840
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.workorders import WorkOrder
from terminal_model.locations import parse_location
from terminal_model.logging import configure_json_logging
from terminal_model.types import JobType, State
from terminal_opcua.port import OpcUaEquipmentPort
from terminal_sim.layout import Layout, load_layout
from terminal_sim.scenario import Scenario, load_scenario
from terminal_sim.world import World
from terminal_sim.yard import Yard
from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.generator import generate_workorders

from terminal_runner.kpi import SAMPLE_INTERVAL_S, KpiCollector

log = logging.getLogger("terminal_runner.run")

CONNECT_ATTEMPTS = 10
CONNECT_RETRY_S = 2.0
READY_TIMEOUT_S = 30.0  # wall: wait for the gateway to mirror every device as non-Fault
DRAIN_POLL_S = 0.005  # wall: deliver events to the ECS this often while sim time runs
_sleep = time.sleep  # patched in tests

RunFn = Callable[[Scenario, Layout, int], dict[str, float]]


def make_allocator(layout: Layout) -> SimpleSlotAllocator:
    """TOS-stub slot allocator for the layout's blocks (layout order)."""
    return SimpleSlotAllocator({b.block: (b.bays, b.rows, b.tiers) for b in layout.blocks})


def make_workorders(scenario: Scenario, layout: Layout, allocator: SimpleSlotAllocator, rng: random.Random) -> list[WorkOrder]:
    """Adapt Layout/Scenario to the TOS stub's plain arguments."""
    return generate_workorders(
        n_containers=scenario.n_containers,
        discharge_share=scenario.discharge_share,
        sts_ids=[s.equipment_id for s in layout.sts[: scenario.n_sts]],
        blocks=[b.block for b in layout.blocks],
        allocator=allocator,
        rng=rng,
    )


def prefill(yard: Yard, orders: list[WorkOrder]) -> None:
    """Place the containers of all LOAD orders in the yard before the run."""
    for order in orders:
        if order.type is JobType.LOAD:
            yard.occupy(parse_location(order.yard_slot), order.container_id)


def run_once(scenario: Scenario, layout: Layout, seed: int) -> dict[str, float]:
    """One run, stepped per KPI sample interval until all orders are DONE/FAILED or
    ``duration_s`` (hard cap). Rates are per simulated hour actually run."""
    scenario = replace(scenario, seed=seed)
    world = World(layout, scenario)
    allocator = make_allocator(layout)
    orders = make_workorders(scenario, layout, allocator, random.Random(seed))
    dispatcher = Dispatcher(world.port, allocator)
    # No on_transfer hook: the sim moves boxes itself (World.handover).
    kpi = KpiCollector(world.port, dispatcher, faults_total=world.faults_total)

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


def _connect(gateway: str) -> OpcUaEquipmentPort:
    """``CONNECT_ATTEMPTS`` tries ``CONNECT_RETRY_S`` apart, then exit 1 with a message."""
    last: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        port = OpcUaEquipmentPort(gateway)
        try:
            port.connect()
            return port
        except ConnectionError as e:
            last = e
            log.warning("gateway %s not reachable (attempt %d/%d): %s",
                        gateway, attempt, CONNECT_ATTEMPTS, e)
            if attempt < CONNECT_ATTEMPTS:
                _sleep(CONNECT_RETRY_S)
    msg = f"gateway {gateway} unreachable after {CONNECT_ATTEMPTS} attempts: {last}"
    log.error(msg)
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _wait_ready(port: OpcUaEquipmentPort) -> None:
    """Wait until no device is FAULT (the gateway mirrors an unreachable server as Fault);
    after ``READY_TIMEOUT_S`` go on and let the ECS treat the rest as faulted."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        down = [e for e in port.equipment_ids() if port.snapshot(e).state is State.FAULT]
        if not down:
            return
        if time.monotonic() >= deadline:
            log.warning("starting with equipment in FAULT: %s", down)
            return
        _sleep(0.2)


def _advance(port: OpcUaEquipmentPort, t: float, timeout_s: float) -> None:
    """Block until sim time ``t``, delivering events to the ECS every ``DRAIN_POLL_S`` so it
    reacts as it does in-process (it dispatches from its event callback)."""
    deadline = time.monotonic() + timeout_s
    try:
        while not port.wait_for_sim_time(t, timeout_s=DRAIN_POLL_S):
            port.drain_events()
            if time.monotonic() >= deadline:
                log.error("sim time stalled at %.1f s, waiting for %.1f s (%.0f s wall)",
                          port.now(), t, timeout_s)
                raise RuntimeError("sim time stalled")
    except ConnectionError as e:
        log.error("sim time stalled at %.1f s: %s", port.now(), e)
        raise RuntimeError(f"sim time stalled: {e}") from e
    port.drain_events()


def run_once_opcua(scenario: Scenario, layout: Layout, seed: int, *, gateway: str) -> dict[str, float]:
    """One run against a live sim process behind the gateway at ``gateway``.

    The sim starts paused at sim time 0: the runner places the LOAD containers
    (``PrefillYard``), submits the orders and runs the first dispatch at sim time 0,
    then calls ``Start``; from there the sim paces itself (``speed_factor``). Times and
    rates are relative to the sim time ``t0`` at ``Start`` (0, unless the sim was
    started with ``--autostart``). The sim must serve the same scenario file (the sim
    uses its seed for equipment timings, the runner for the work orders). At the end
    the sim is told to ``Stop``."""
    scenario = replace(scenario, seed=seed)
    allocator = make_allocator(layout)
    orders = make_workorders(scenario, layout, allocator, random.Random(seed))
    port = _connect(gateway)
    try:
        _wait_ready(port)
        slots = [(o.yard_slot, o.container_id) for o in orders if o.type is JobType.LOAD]
        if slots and not port.prefill_yard(slots):
            raise RuntimeError(
                f"PrefillYard rejected for {len(slots)} LOAD containers (the sim accepts it only "
                "at sim time 0: was it started with --autostart?)")
        port.drain_events()  # events from before the run: no subscriber yet, dropped
        dispatcher = Dispatcher(port, allocator)
        kpi = KpiCollector(port, dispatcher, faults_total=None)
        t0 = port.now()
        dispatcher.submit(orders)
        dispatcher.tick()  # first assignments at t0, as in-process
        if not port.start_sim():
            raise RuntimeError(f"Start rejected by the sim behind {gateway}")
        log.info("run started at sim time %.1f s", t0, extra={"gateway": gateway, "seed": seed})
        window_timeout = max(30.0, 120 * SAMPLE_INTERVAL_S / scenario.speed_factor)
        t = t0
        cap = t0 + scenario.duration_s
        timed_out = True
        while t < cap:
            t = min(t + SAMPLE_INTERVAL_S, cap)
            _advance(port, t, window_timeout)
            dispatcher.tick()
            kpi.sample()
            if dispatcher.status()["open"] == 0:
                timed_out = False
                break
        elapsed = port.now() - t0
        kpis = kpi.finish(elapsed)  # before Stop: the gateway marks stopped servers Fault
        kpis["sim_elapsed_s"] = elapsed
        kpis["timed_out"] = 1.0 if timed_out else 0.0
        if not port.stop_sim():
            log.warning("Stop on %s failed; the sim process keeps running", gateway)
        return kpis
    finally:
        port.close()


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
    run: RunFn = run_once,
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
        kpis = run(scenario, layout, seed)
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
    parser.add_argument("--port", choices=("inprocess", "opcua"), default="inprocess",
                        help="in-process World, or OPC UA via the gateway (default: inprocess)")
    parser.add_argument("--gateway", default="opc.tcp://localhost:4840",
                        help="gateway endpoint for --port opcua (default: %(default)s)")
    args = parser.parse_args(argv)
    run: RunFn = run_once
    if args.port == "opcua":
        if args.replications > 1:
            parser.error("--replications > 1 is not supported with --port opcua in B1: "
                         "each replication needs a fresh sim process")
        run = partial(run_once_opcua, gateway=args.gateway)
    configure_json_logging()
    print(run_replications(args.scenario, args.replications, args.out, args.layout, run=run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
