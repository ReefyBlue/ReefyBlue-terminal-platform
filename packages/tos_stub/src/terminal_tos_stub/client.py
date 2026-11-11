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


class EcsRefused(Exception):
    """An operator request was refused (HTTP 404/409); ``code`` and ``message`` from the body
    (``UNKNOWN_EQUIPMENT``, ``UNKNOWN_JOB``, ``RELEASE_REFUSED``)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


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

    def alarms(self) -> list[dict]:
        return self._get("/alarms")

    def _operator_post(self, path: str, body: dict | None = None) -> dict:
        resp = self._http.post(path, json=body)
        if resp.status_code in (404, 409):
            data = resp.json()
            raise EcsRefused(resp.status_code, data.get("code", ""), data.get("message", ""))
        resp.raise_for_status()  # 422 (invalid outcome), 503 (not connected)
        return resp.json()

    def release(self, equipment_id: str) -> dict:
        """``{"released": id}``; ``EcsRefused`` on 404 (unknown) / 409 (refused)."""
        return self._operator_post(f"/equipment/{equipment_id}/release")

    def resolve_job(self, job_id: str, outcome: str) -> dict:
        """``{"job_id", "outcome"}``; ``EcsRefused`` on 404, ``httpx.HTTPStatusError`` on 422."""
        return self._operator_post(f"/jobs/{job_id}/resolve", {"outcome": outcome})

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
