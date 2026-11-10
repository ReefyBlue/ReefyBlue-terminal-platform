"""CLI: generate work orders for a scenario and submit them to a running ECS service.

Usage: python -m terminal_tos_stub.submit scenario.yaml --layout terminal.yaml --ecs http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import yaml

from terminal_tos_stub.allocator import SimpleSlotAllocator
from terminal_tos_stub.client import EcsClient, WorkOrderRejected
from terminal_tos_stub.generator import generate_workorders


def _load(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal-tos-submit", description=__doc__)
    ap.add_argument("scenario")
    ap.add_argument("--layout", default="terminal.yaml")
    ap.add_argument("--ecs", default="http://localhost:8000")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    scenario = _load(args.scenario)
    layout = _load(args.layout)
    seed = int(scenario["seed"]) if args.seed is None else args.seed
    blocks = layout["blocks"]
    allocator = SimpleSlotAllocator(
        {b["block"]: (int(b["bays"]), int(b["rows"]), int(b["tiers"])) for b in blocks})
    orders = generate_workorders(
        n_containers=int(scenario["n_containers"]),
        discharge_share=float(scenario["discharge_share"]),
        sts_ids=[s["id"] for s in layout["sts"][: int(scenario["n_sts"])]],
        blocks=[b["block"] for b in blocks],
        allocator=allocator,
        rng=random.Random(seed),
    )

    client = EcsClient(args.ecs)
    try:
        if not client.wait_healthy(30):
            print(f"ECS at {args.ecs} not healthy after 30 s", file=sys.stderr)
            return 1
        try:
            accepted = client.post_workorders(orders)
        except WorkOrderRejected as exc:
            print(json.dumps({"errors": exc.errors}, indent=2), file=sys.stderr)
            return 2
        print(json.dumps({"accepted": accepted}))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
