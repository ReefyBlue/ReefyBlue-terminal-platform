"""CLI: run the ECS service against the OPC UA gateway.

Usage: python -m terminal_ecs_service.serve --gateway opc.tcp://localhost:4840 --layout terminal.yaml \
    --host 0.0.0.0 --port 8000

SIGTERM/SIGINT stop uvicorn; the app's lifespan then cancels ``EcsCore.run()``,
which closes the gateway connection.
"""

from __future__ import annotations

import argparse
import logging
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

__all__ = ["Block", "Layout", "load_layout", "main", "port_factory"]


@dataclass(frozen=True)
class Block:
    block: str
    bays: int
    rows: int
    tiers: int


@dataclass(frozen=True)
class Layout:
    """The part of ``terminal.yaml`` the service needs (``core.LayoutLike``); read here
    because the service may not import ``terminal_sim``."""

    blocks: list[Block]


def load_layout(path: str | Path) -> Layout:
    raw = yaml.safe_load(Path(path).read_text())
    return Layout([Block(str(b["block"]), int(b["bays"]), int(b["rows"]), int(b["tiers"]))
                   for b in raw["blocks"]])


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
    ap.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    args = ap.parse_args(argv)

    configure_json_logging(getattr(logging, args.log_level.upper()))
    core = EcsCore(load_layout(args.layout), connect=port_factory(args.gateway))
    uvicorn.run(create_app(core), host=args.host, port=args.port, log_config=None, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
