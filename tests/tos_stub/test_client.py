import json

import httpx
import pytest

from terminal_ecs.workorders import WorkOrder
from terminal_model.types import JobType
from terminal_tos_stub.client import EcsClient, EcsRefused, WorkOrderRejected

ORDER = WorkOrder("C1", JobType.DISCHARGE, "STS-01", "A/1/1/1")


def client_for(handler) -> EcsClient:
    return EcsClient("http://ecs", transport=httpx.MockTransport(handler))


def test_post_workorders_serialises_and_returns_accepted():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": 1})

    with client_for(handler) as c:
        assert c.post_workorders([ORDER]) == 1
    assert seen["method"] == "POST" and seen["path"] == "/workorders"
    assert seen["body"] == [
        {"container_id": "C1", "type": "DISCHARGE", "sts_id": "STS-01", "yard_slot": "A/1/1/1"}
    ]


def test_post_workorders_422_raises_with_errors():
    errors = [{"container_id": "C1", "code": "DUPLICATE_CONTAINER", "message": "dup"}]
    c = client_for(lambda r: httpx.Response(422, json={"errors": errors}))
    with pytest.raises(WorkOrderRejected) as exc:
        c.post_workorders([ORDER])
    assert exc.value.errors == errors


def test_post_workorders_503_raises_http_error():
    c = client_for(lambda r: httpx.Response(503, json={"detail": "no gateway"}))
    with pytest.raises(httpx.HTTPStatusError):
        c.post_workorders([ORDER])


def test_status_kpis_equipment():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "/status": {"state": "RUNNING"},
            "/kpis": {"throughput": 1.5},
            "/equipment": [{"id": "STS-01"}],
        }[request.url.path])

    c = client_for(handler)
    assert c.status() == {"state": "RUNNING"}
    assert c.kpis() == {"throughput": 1.5}
    assert c.equipment() == [{"id": "STS-01"}]


def test_health_true_false_and_unreachable():
    assert client_for(lambda r: httpx.Response(200, json={"connected": True, "sim_time": 0.0})).health()
    assert not client_for(lambda r: httpx.Response(503)).health()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert not client_for(boom).health()


def test_wait_healthy_polls_until_200():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503 if len(calls) <= 2 else 200, json={"connected": True, "sim_time": 0.0})

    assert client_for(handler).wait_healthy(5.0, poll_s=0.01) is True
    assert calls == ["/health"] * 3


def test_wait_healthy_times_out():
    c = client_for(lambda r: httpx.Response(503))
    assert c.wait_healthy(0.05, poll_s=0.01) is False


ALARM = {"kind": "NEEDS_RELEASE", "equipment_id": "AGV-01", "job_id": None,
         "since_sim_time": 3.0, "detail": "fault: release required"}


def test_alarms_release_resolve():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/alarms":
            return httpx.Response(200, json=[ALARM])
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json={"released": "AGV-01"})
        return httpx.Response(200, json={"job_id": "J1", "outcome": "failed"})

    with client_for(handler) as c:
        assert c.alarms() == [ALARM]
        assert c.release("AGV-01") == {"released": "AGV-01"}
        assert c.resolve_job("J1", "failed") == {"job_id": "J1", "outcome": "failed"}
    assert seen == [("GET", "/alarms", None), ("POST", "/equipment/AGV-01/release", None),
                    ("POST", "/jobs/J1/resolve", {"outcome": "failed"})]


@pytest.mark.parametrize("status,code", [(409, "RELEASE_REFUSED"), (404, "UNKNOWN_EQUIPMENT")])
def test_release_refused_raises(status, code):
    c = client_for(lambda r: httpx.Response(status, json={"code": code, "message": "equipment reports FAULT"}))
    with pytest.raises(EcsRefused) as exc:
        c.release("AGV-01")
    assert (exc.value.status, exc.value.code, exc.value.message) == (status, code, "equipment reports FAULT")


def test_resolve_404_raises_refused_and_422_http_error():
    c = client_for(lambda r: httpx.Response(404, json={"code": "UNKNOWN_JOB", "message": "J9"}))
    with pytest.raises(EcsRefused) as exc:
        c.resolve_job("J9", "completed")
    assert exc.value.code == "UNKNOWN_JOB"
    c = client_for(lambda r: httpx.Response(422, json={"detail": []}))
    with pytest.raises(httpx.HTTPStatusError):
        c.resolve_job("J1", "maybe")
    c = client_for(lambda r: httpx.Response(503, json={"detail": "not connected"}))
    with pytest.raises(httpx.HTTPStatusError):
        c.release("AGV-01")


def test_containers_health_json_ui_ok():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/containers":
            return httpx.Response(200, json={"next": 7, "items": []})
        if request.url.path == "/health":
            return httpx.Response(503, json={"connected": False, "sim_time": None, "rss_mb": 50.0, "uptime_s": 3.0})
        return httpx.Response(404)

    with client_for(handler) as c:
        assert c.containers(5) == {"next": 7, "items": []}
        assert c.health_json() == {"connected": False, "sim_time": None, "rss_mb": 50.0, "uptime_s": 3.0}
        assert c.ui_ok() is False
    assert seen == [("/containers", {"since": "5"}), ("/health", {}), ("/ui/", {})]
    assert client_for(lambda r: httpx.Response(200, text="<html>")).ui_ok() is True

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert client_for(boom).ui_ok() is False


def test_client_wraps_an_existing_http_client():
    http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"open": 0})))
    c = EcsClient(http=http)
    assert c.status() == {"open": 0}
