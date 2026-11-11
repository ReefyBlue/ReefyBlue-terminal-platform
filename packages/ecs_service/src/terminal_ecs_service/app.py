"""FastAPI app of the ECS service (spec §3, §5, §7).

Every endpoint is ``async def`` and runs on the loop that owns ``EcsCore``, so the
Dispatcher, KPI collector and allocator need no locks. A ``ConnectionError`` from
the equipment link inside an endpoint is handled like one in the loop:
``core.on_link_lost()`` and 503 — unless the command already took effect
(work orders taken, release, resolve): then ``core`` handles the lost link and the
endpoint answers with success (a retry then gets 503 while the link is down, and
404/200 per the new state after reconnecting).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import anyio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from terminal_ecs.alarms import ReleaseRefused
from terminal_ecs.workorders import WorkOrder, WorkOrderError
from terminal_model.locations import SlotLoc, parse_location
from terminal_model.types import JobType

from terminal_ecs_service.core import EcsCore
from terminal_ecs_service.schemas import (
    AcceptedOut,
    AlarmOut,
    BlockOut,
    HealthOut,
    LayoutOut,
    PositionOut,
    RefusedOut,
    ReleaseOut,
    ResolveIn,
    ResolveOut,
    StsOut,
    WorkOrderErrorOut,
    WorkOrderErrorsOut,
    WorkOrderIn,
)

__all__ = ["create_app", "validate_orders"]

log = logging.getLogger(__name__)

T = TypeVar("T")

_NOT_CONNECTED = {"detail": "not connected to the equipment"}
_503 = {"description": "Not connected to the equipment"}


def _refused(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(RefusedOut(code=code, message=message).model_dump(), status_code=status)


def _error(container_id: str | None, code: str, message: str) -> dict:
    return WorkOrderErrorOut(container_id=container_id, code=code, message=message).model_dump()


def validate_orders(core: EcsCore, orders: list[WorkOrderIn]) -> list[dict]:
    """Every rejected order of the batch (spec §7), at most one error per order, in
    the precedence of ``Dispatcher.submit``: duplicate, unknown STS, invalid slot.
    May raise ``ConnectionError`` (reads the fleet)."""
    fleet = core.equipment()
    sts = {e["equipment_id"] for e in fleet if e["kind"] == "STS"}
    # Blocks the allocator knows and an ASC serves (the Dispatcher's own check).
    asc_blocks = {
        str(e["extra"].get("block_id") or e["equipment_id"].split("-", 1)[1])
        for e in fleet if e["kind"] == "ASC"
    }
    blocks = set(core.allocator.blocks()) & asc_blocks
    errors: list[dict] = []
    seen: set[str] = set()
    for o in orders:
        cid = o.container_id
        duplicate = cid in seen or cid in core.known_containers
        seen.add(cid)
        if duplicate:
            errors.append(_error(cid, "DUPLICATE_CONTAINER", f"{cid}: container already has a work order"))
            continue
        if o.sts_id not in sts:
            errors.append(_error(cid, "UNKNOWN_STS", f"{cid}: {o.sts_id} is not an STS of the fleet"))
            continue
        try:
            loc = parse_location(o.yard_slot)
        except ValueError:
            loc = None
        if not isinstance(loc, SlotLoc) or loc.block not in blocks:
            errors.append(_error(cid, "INVALID_SLOT", f"{cid}: {o.yard_slot!r} is not a yard slot of this terminal"))
    return errors


def create_app(core: EcsCore, *, run_loop: bool = True) -> FastAPI:
    """The API around ``core``. With ``run_loop`` the lifespan runs ``core.run()`` as a
    task and cancels it on shutdown (which closes the equipment link); tests pass
    ``run_loop=False`` and drive ``core.step()`` themselves."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(core.run(), name="ecs-core") if run_loop else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="terminal ECS service", lifespan=lifespan)
    app.state.core = core

    def guarded(fn: Callable[[], T]) -> T | JSONResponse:
        try:
            return fn()
        except ConnectionError as e:
            log.warning("equipment link failed during request: %s", e)
            core.on_link_lost()
            return JSONResponse(_NOT_CONNECTED, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path != "/workorders":
            return await request_validation_exception_handler(request, exc)
        body = exc.body if isinstance(exc.body, list) else None
        errors = []
        for err in exc.errors():
            loc = err.get("loc", ())
            cid = None
            if body is not None and len(loc) >= 2 and isinstance(loc[1], int) and loc[1] < len(body):
                item = body[loc[1]]
                cid = item.get("container_id") if isinstance(item, dict) else None
            field = ".".join(str(p) for p in loc[1:])
            errors.append(_error(cid if isinstance(cid, str) else None, "INVALID_ORDER",
                                 f"{field}: {err.get('msg', 'invalid')}"))
        return JSONResponse({"errors": errors}, status_code=422)

    @app.post("/workorders", status_code=202, response_model=AcceptedOut, responses={
        422: {"model": WorkOrderErrorsOut, "description": "Batch rejected (nothing accepted)"},
        500: {"model": WorkOrderErrorsOut, "description": "Dispatcher rejected a validated batch"},
        503: {"description": "Not connected to the equipment"},
    })
    async def post_workorders(orders: list[WorkOrderIn]) -> Any:
        if not core.connected:
            return JSONResponse(_NOT_CONNECTED, status_code=503)

        def submit() -> Any:
            errors = validate_orders(core, orders)
            if errors:
                return JSONResponse({"errors": errors}, status_code=422)
            try:
                core.submit([WorkOrder(o.container_id, JobType(o.type), o.sts_id, o.yard_slot) for o in orders])
            except WorkOrderError as e:  # validation missed it: spec §7
                log.error("work order batch failed in the Dispatcher after validation: %s", e)
                return JSONResponse({"errors": [_error(None, e.code, str(e))]}, status_code=500)
            return AcceptedOut(accepted=len(orders))

        return guarded(submit)

    @app.get("/status")
    async def get_status() -> Any:
        return guarded(core.status)

    @app.get("/kpis")
    async def get_kpis() -> Any:
        if core.kpi is None:
            return JSONResponse(_NOT_CONNECTED, status_code=503)
        return guarded(core.kpis)

    @app.get("/equipment")
    async def get_equipment() -> Any:
        return guarded(core.equipment)

    @app.get("/alarms", response_model=list[AlarmOut])
    async def get_alarms() -> Any:
        """Open alarms, oldest first (``since_sim_time``, then kind); last-known while
        the link is down, ``[]`` before the first connection."""
        return guarded(lambda: sorted(core.alarms(), key=lambda a: (
            a["since_sim_time"], a["kind"], a["equipment_id"], a["job_id"] or "")))

    @app.post("/equipment/{equipment_id}/release", response_model=ReleaseOut, responses={
        404: {"model": RefusedOut, "description": "UNKNOWN_EQUIPMENT"},
        409: {"model": RefusedOut, "description": "RELEASE_REFUSED (FAULT/OFFLINE or unresolved job)"},
        503: _503,
    })
    async def post_release(equipment_id: str) -> Any:
        if not core.connected or core.dispatcher is None:
            return JSONResponse(_NOT_CONNECTED, status_code=503)

        def release() -> Any:
            try:
                core.release(equipment_id)
            except KeyError:
                return _refused("UNKNOWN_EQUIPMENT", f"{equipment_id} is not equipment of the fleet", 404)
            except ReleaseRefused as e:
                return _refused("RELEASE_REFUSED", e.reason, 409)
            return ReleaseOut(released=equipment_id)

        return guarded(release)

    @app.post("/jobs/{job_id}/resolve", response_model=ResolveOut, responses={
        404: {"model": RefusedOut, "description": "UNKNOWN_JOB (no job with an unknown outcome)"},
        503: _503,
    })
    async def post_resolve(job_id: str, body: ResolveIn) -> Any:
        if not core.connected or core.dispatcher is None:
            return JSONResponse(_NOT_CONNECTED, status_code=503)

        def resolve() -> Any:
            try:
                core.resolve_job(job_id, body.outcome)
            except KeyError:
                return _refused("UNKNOWN_JOB", f"{job_id} has no unknown outcome", 404)
            return ResolveOut(job_id=job_id, outcome=body.outcome)

        return guarded(resolve)

    @app.get("/health", response_model=HealthOut, response_model_exclude_none=True,
             responses={503: {"model": HealthOut}})
    async def get_health() -> Any:
        if not core.connected:
            return JSONResponse({"connected": False, "sim_time": None}, status_code=503)
        if core.time_reversed:  # sim restarted under this ECS: unusable until restarted
            return JSONResponse({"connected": True, "sim_time": core.port.now(), "error": "sim_time_reversed"},
                                status_code=503)
        return HealthOut(connected=True, sim_time=core.port.now())

    @app.get("/layout", response_model=LayoutOut)
    async def get_layout() -> LayoutOut:
        """Static terminal geometry; needs no equipment link."""
        lo = core.layout
        return LayoutOut(
            quay_length_m=lo.quay_length_m,
            grid_cell_m=lo.grid_cell_m,
            grid_cols=lo.grid_cols,
            grid_rows=lo.grid_rows,
            sts=[
                StsOut(equipment_id=s.equipment_id, x=s.position.x, y=s.position.y,
                       lane_cells=(tuple(s.lane_cells[0]), tuple(s.lane_cells[1])))
                for s in lo.sts
            ],
            blocks=[
                BlockOut(block=b.block, asc_id=b.asc_id,
                         tp_ws=PositionOut(x=b.tp_ws.x, y=b.tp_ws.y), tp_ws_cell=tuple(b.tp_ws_cell),
                         bays=b.bays, rows=b.rows, tiers=b.tiers)
                for b in lo.blocks
            ],
            agv_start_cells=[tuple(c) for c in lo.agv_start_cells],
        )

    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        q = core.broadcaster.register()
        try:
            try:
                first = core.status_message()
            except ConnectionError as e:
                log.warning("equipment link failed during WS handshake: %s", e)
                core.on_link_lost()
                first = core.status_message()  # unbound: answers from the last-known state
            await ws.send_json(first)

            async with anyio.create_task_group() as tg:

                async def pump() -> None:
                    try:
                        while True:
                            await ws.send_json(await q.get())
                    except WebSocketDisconnect:
                        tg.cancel_scope.cancel()

                async def watch() -> None:  # notices a disconnect even while no message flows
                    while (await ws.receive())["type"] != "websocket.disconnect":
                        pass
                    tg.cancel_scope.cancel()

                tg.start_soon(pump)
                tg.start_soon(watch)
        except WebSocketDisconnect:
            pass
        finally:
            core.broadcaster.unregister(q)

    return app
