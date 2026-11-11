"""CLI: run the ECS service against the OPC UA gateway.

Usage: python -m terminal_ecs_service.serve --gateway opc.tcp://localhost:4840 --layout terminal.yaml \
    --host 0.0.0.0 --port 8000 [--auto-release]

SIGTERM/SIGINT stop uvicorn; the app's lifespan then cancels ``EcsCore.run()``,
which closes the gateway connection. uvicorn re-raises the signal afterwards: SIGTERM hits
a handler installed in ``main`` (exit 0), SIGINT keeps the default (KeyboardInterrupt, exit 130).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
import yaml
from terminal_model.logging import configure_json_logging
from terminal_opcua.port import OpcUaEquipmentPort

from terminal_ecs_service.app import create_app
from terminal_ecs_service.core import EcsCore

__all__ = ["Block", "Layout", "Position", "Sts", "load_layout", "main", "port_factory"]


@dataclass(frozen=True)
class Position:
    x: float
    y: float


@dataclass(frozen=True)
class Sts:
    equipment_id: str
    position: Position
    lane_cells: tuple[tuple[int, int], tuple[int, int]]


@dataclass(frozen=True)
class Block:
    block: str
    bays: int
    rows: int
    tiers: int
    asc_id: str
    tp_ws: Position
    tp_ws_cell: tuple[int, int]


@dataclass(frozen=True)
class Layout:
    """The part of ``terminal.yaml`` the service needs (``core.LayoutLike``): the allocator's
    blocks and the geometry served by ``GET /layout``; read here because the service may not
    import ``terminal_sim``."""

    blocks: list[Block]
    quay_length_m: float
    grid_cell_m: float
    grid_cols: int
    grid_rows: int
    sts: list[Sts]
    agv_start_cells: list[tuple[int, int]]


def _cell(c) -> tuple[int, int]:
    return int(c[0]), int(c[1])


def load_layout(path: str | Path) -> Layout:
    raw = yaml.safe_load(Path(path).read_text())
    return Layout(
        blocks=[Block(str(b["block"]), int(b["bays"]), int(b["rows"]), int(b["tiers"]),
                      asc_id=str(b["asc_id"]),
                      tp_ws=Position(float(b["tp_ws"]["x"]), float(b["tp_ws"]["y"])),
                      tp_ws_cell=_cell(b["tp_ws_cell"]))
                for b in raw["blocks"]],
        quay_length_m=float(raw["quay"]["length_m"]),
        grid_cell_m=float(raw["grid"]["cell_m"]),
        grid_cols=int(raw["grid"]["cols"]),
        grid_rows=int(raw["grid"]["rows"]),
        sts=[Sts(str(s["id"]), Position(float(s["x"]), float(s["y"])),
                 (_cell(s["lane_cells"][0]), _cell(s["lane_cells"][1])))
             for s in raw["sts"]],
        agv_start_cells=[_cell(c) for c in raw["agv_start_cells"]],
    )


def port_factory(gateway: str) -> Callable[[], OpcUaEquipmentPort]:
    """A fresh connected port per call; ``ConnectionError`` if the gateway is unreachable."""

    def connect() -> OpcUaEquipmentPort:
        port = OpcUaEquipmentPort(gateway)
        port.connect()
        return port

    return connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal-ecs-service", description="ECS as a service (HTTP + WS).")
    ap.add_argument("--gateway", default="opc.tcp://localhost:4840")
    ap.add_argument("--layout", default="terminal.yaml")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--auto-release", action="store_true",
                    help="re-admit equipment after FAULT -> IDLE and count unknown job outcomes as completed "
                         "(unattended runs); default: the operator releases via POST "
                         "/equipment/{id}/release and resolves via POST /jobs/{id}/resolve")
    ap.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    args = ap.parse_args(argv)

    configure_json_logging(getattr(logging, args.log_level.upper()))
    core = EcsCore(load_layout(args.layout), connect=port_factory(args.gateway),
                   auto_release=args.auto_release)
    # uvicorn restores the pre-run() handler and re-raises the captured signal into it after its
    # graceful shutdown; with the default SIGTERM handler that would exit -15, so exit 0 instead.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    uvicorn.run(create_app(core), host=args.host, port=args.port, log_config=None, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
