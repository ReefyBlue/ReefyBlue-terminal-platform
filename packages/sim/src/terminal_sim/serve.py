"""Run the Salabim world paced to wall-clock while serving OPC UA."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path

from terminal_model.logging import configure_json_logging
from terminal_model.port import EquipmentEvent

from terminal_sim.layout import Layout, load_layout
from terminal_sim.opcua_servers import ControlServer, EquipmentServer
from terminal_sim.scenario import Scenario, load_scenario
from terminal_sim.world import World

SIM_TICK_S = 1.0
MAX_EQUIPMENT = 12
CONTROL_PORT_OFFSET = 12  # fixed by the spec
LAG_WARN_S = 5.0

log = logging.getLogger("terminal_sim.serve")


class SimService:
    def __init__(
        self, scenario: Scenario, layout: Layout, *, base_port: int = 48400, host: str = "0.0.0.0"
    ) -> None:
        self.scenario = scenario
        self.world = World(layout, scenario)
        assert len(self.world.equipment) <= MAX_EQUIPMENT
        self.servers: dict[str, EquipmentServer] = {
            eid: EquipmentServer(eq, port=base_port + i, host=host)
            for i, (eid, eq) in enumerate(self.world.equipment.items())
        }
        self.control = ControlServer(
            self.world, port=base_port + CONTROL_PORT_OFFSET, on_stop=self.stop, host=host
        )
        self.world.port.subscribe(self._route)
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False

    def _route(self, ev: EquipmentEvent) -> None:
        server = self.servers.get(ev.equipment_id)
        if server is not None:
            server.on_emit(ev)

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def _sync(self) -> None:
        await asyncio.gather(*[s.sync() for s in self.servers.values()], self.control.sync())

    async def run(self) -> None:
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()
        started: list = []
        try:
            started = [*self.servers.values(), self.control]
            await asyncio.gather(*[s.start() for s in started])
            await self._sync()
            period = SIM_TICK_S / self.scenario.speed_factor
            deadline = time.monotonic()
            last_warn = -LAG_WARN_S
            while not self._stop_event.is_set():
                self.world.run(until=self.world.now() + SIM_TICK_S)
                await self._sync()
                deadline += period
                delay = deadline - time.monotonic()
                if delay < -LAG_WARN_S and time.monotonic() - last_warn >= LAG_WARN_S:
                    log.warning("sim is %.1f s behind wall clock", -delay)
                    last_warn = time.monotonic()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=max(delay, 0.0))
                except TimeoutError:
                    pass
        finally:
            await asyncio.gather(*[s.stop() for s in started], return_exceptions=True)


async def _amain(service: SimService) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stop)
    await service.run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal_sim.serve")
    ap.add_argument("scenario", type=Path)
    ap.add_argument("--layout", type=Path, default=Path("terminal.yaml"))
    ap.add_argument("--base-port", type=int, default=48400)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args(argv)
    configure_json_logging()
    service = SimService(
        load_scenario(args.scenario), load_layout(args.layout),
        base_port=args.base_port, host=args.host,
    )
    asyncio.run(_amain(service))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
