"""HTTP client for the ECS service."""

from __future__ import annotations

import time
from typing import Any

import httpx

from terminal_ecs.workorders import WorkOrder


class WorkOrderRejected(Exception):
    """The ECS rejected the work orders (HTTP 422); ``errors`` is the list from the body."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__(f"work orders rejected: {errors}")
        self.errors = errors


class EcsClient:
    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None,
                 timeout_s: float = 5.0) -> None:
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout_s)

    def __enter__(self) -> EcsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def post_workorders(self, orders: list[WorkOrder]) -> int:
        body = [
            {"container_id": o.container_id, "type": o.type.value,
             "sts_id": o.sts_id, "yard_slot": o.yard_slot}
            for o in orders
        ]
        resp = self._http.post("/workorders", json=body)
        if resp.status_code == 422:
            raise WorkOrderRejected(resp.json().get("errors", []))
        resp.raise_for_status()
        return int(resp.json()["accepted"])

    def _get(self, path: str) -> Any:
        resp = self._http.get(path)
        resp.raise_for_status()
        return resp.json()

    def status(self) -> dict:
        return self._get("/status")

    def kpis(self) -> dict[str, float]:
        return self._get("/kpis")

    def equipment(self) -> list[dict]:
        return self._get("/equipment")

    def health(self) -> bool:
        try:
            return self._http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def wait_healthy(self, timeout_s: float, poll_s: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.health():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)
