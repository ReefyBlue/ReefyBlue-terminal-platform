"""Run the gateway until SIGTERM/SIGINT."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path

import yaml
from terminal_model.layout import is_v2, load_terminal
from terminal_model.logging import configure_json_logging

from terminal_gateway.gateway import Gateway

log = logging.getLogger("terminal_gateway.serve")


@dataclass(frozen=True)
class _Sts:
    equipment_id: str


@dataclass(frozen=True)
class _Block:
    block: str
    asc_id: str


@dataclass(frozen=True)
class GatewayLayout:
    sts: list[_Sts]
    blocks: list[_Block]
    opcua_base_port: int = 48400
    opcua_host: str = "localhost"


def load_gateway_layout(path: Path) -> GatewayLayout:
    """The equipment ids and ``opcua`` block of a layout file (no simulator import).

    A v2 (metre) layout gives the same ids as the simulator's grid view: one ASC per block,
    its waterside ASC (``asc_ws_id``)."""
    raw = yaml.safe_load(Path(path).read_text())
    if isinstance(raw, dict) and is_v2(raw):
        t = load_terminal(path)
        return GatewayLayout(
            sts=[_Sts(s.id) for s in t.sts],
            blocks=[_Block(b.id, b.asc_ws_id) for b in t.blocks],
            opcua_base_port=t.opcua.base_port,
            opcua_host=t.opcua.host,
        )
    opcua = raw.get("opcua") or {}
    return GatewayLayout(
        sts=[_Sts(s["id"]) for s in raw["sts"]],
        blocks=[_Block(b["block"], b["asc_id"]) for b in raw["blocks"]],
        opcua_base_port=int(opcua.get("base_port", 48400)),
        opcua_host=str(opcua.get("host", "localhost")),
    )


async def _amain(gateway: Gateway) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await gateway.start()
    try:
        await stop.wait()
    finally:
        log.info("gateway stopping")
        await gateway.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal_gateway.serve")
    ap.add_argument("--config", type=Path, default=Path("terminal.yaml"))
    ap.add_argument("--port", type=int, default=4840)
    ap.add_argument("--host", default="0.0.0.0", help="bind address of the gateway server")
    ap.add_argument("--upstream-host", default=None, help="default: opcua.host from the config")
    ap.add_argument("--base-port", type=int, default=None, help="default: opcua.base_port")
    ap.add_argument("--n-sts", type=int, default=2)
    ap.add_argument("--n-agv", type=int, default=6)
    args = ap.parse_args(argv)
    configure_json_logging()
    layout = load_gateway_layout(args.config)
    gateway = Gateway(
        layout, port=args.port, host=args.host,
        upstream_host=args.upstream_host or layout.opcua_host,
        base_port=args.base_port if args.base_port is not None else layout.opcua_base_port,
        n_sts=args.n_sts, n_agv=args.n_agv,
    )
    asyncio.run(_amain(gateway))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
