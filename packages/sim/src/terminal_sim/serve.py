"""Run the Salabim world paced to wall-clock while serving OPC UA.

The clock starts paused at sim time 0 (so ``PrefillYard`` can still be called) and
runs once ``Terminal/Clock.Start`` is called, or immediately with ``--autostart``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal
import time
from pathlib import Path

from terminal_model.logging import configure_json_logging
from terminal_model.port import EquipmentEvent

from terminal_sim.layout import Layout, load_layout
from terminal_sim.opcua_servers import ControlServer, EquipmentServer, ZoneServer
from terminal_sim.scenario import Scenario, load_scenario
from terminal_sim.world import make_world
from terminal_sim.world2 import World2

SIM_TICK_S = 1.0
LAG_WARN_S = 5.0

log = logging.getLogger("terminal_sim.serve")


class SimService:
    def __init__(
        self, scenario: Scenario, layout: Layout, *, base_port: int = 48400, host: str = "0.0.0.0",
        autostart: bool = False,
    ) -> None:
        self.scenario = scenario
        self.world = make_world(layout, scenario)
        # Ports: equipment i (World order STS, AGV, ASC) on base_port + i, the clock server on
        # base_port + n_equipment (n_sts + n_agv + one ASC per block, two - WS and LS - on a v2
        # layout; the gateway derives the same). The legacy fleet 2 + 6 + 4 keeps base_port + 12.
        # A v2 world also serves its transfer zones (layout order) on base_port + n_equipment +
        # 1 + i, and its equipment as the v2 object types.
        v2 = isinstance(self.world, World2)
        self.servers: dict[str, EquipmentServer] = {
            eid: EquipmentServer(eq, port=base_port + i, host=host, v2=v2)
            for i, (eid, eq) in enumerate(self.world.equipment.items())
        }
        n_equipment = len(self.world.equipment)
        self.control = ControlServer(
            self.world, port=base_port + n_equipment, on_stop=self.stop, host=host,
            on_start=self.start,
        )
        self.zones: list[ZoneServer] = []
        if v2:
            self.zones = [
                ZoneServer(z, self.world.racks, port=base_port + n_equipment + 1 + i, host=host)
                for i, z in enumerate(self.world.terminal.transfer_zones)
            ]
        self.world.port.subscribe(self._route)
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False
        self._start_event: asyncio.Event | None = None
        self._start_requested = autostart

    def _route(self, ev: EquipmentEvent) -> None:
        server = self.servers.get(ev.equipment_id)
        if server is not None:
            server.on_emit(ev)

    def start(self) -> bool:
        """Let the clock run (idempotent); True."""
        if not self._start_requested:
            log.info("Start received at sim time %.1f", self.world.now())
        self._start_requested = True
        if self._start_event is not None:
            self._start_event.set()
        return True

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    async def _sync(self) -> None:
        await asyncio.gather(*[s.sync() for s in self.servers.values()], self.control.sync(),
                             *[z.sync() for z in self.zones])

    async def run(self) -> None:
        self._stop_event = asyncio.Event()
        self._start_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()
        if self._start_requested:
            self._start_event.set()
        started: list = []
        try:
            started = [*self.servers.values(), self.control, *self.zones]
            await asyncio.gather(*[s.start() for s in started])
            await self._sync()
            if not self._start_event.is_set() and not self._stop_event.is_set():
                log.info("waiting for Start (sim time %.1f)", self.world.now())
                waits = [asyncio.create_task(self._start_event.wait()),
                         asyncio.create_task(self._stop_event.wait())]
                _, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
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
    ap.add_argument("--autostart", action="store_true",
                    help="run the clock at once instead of waiting for Terminal/Clock.Start")
    ap.add_argument("--speed-factor", type=float, default=None,
                    help="override the scenario's speed_factor (sim seconds per wall second)")
    args = ap.parse_args(argv)
    if args.speed_factor is not None and args.speed_factor <= 0:
        ap.error("--speed-factor must be > 0")
    configure_json_logging()
    layout = load_layout(args.layout)
    scenario = load_scenario(args.scenario, layout)
    if args.speed_factor is not None:
        scenario = dataclasses.replace(scenario, speed_factor=args.speed_factor)
    service = SimService(
        scenario, layout,
        base_port=args.base_port, host=args.host, autostart=args.autostart,
    )
    asyncio.run(_amain(service))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
