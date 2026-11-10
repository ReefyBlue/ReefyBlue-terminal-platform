import json

import httpx
import pytest

from terminal_ecs.workorders import WorkOrder
from terminal_model.types import JobType
from terminal_tos_stub.client import EcsClient, WorkOrderRejected

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
