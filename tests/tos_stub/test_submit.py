import pytest

from terminal_model.types import JobType
from terminal_tos_stub import submit
from terminal_tos_stub.client import WorkOrderRejected


class FakeClient:
    instances: list["FakeClient"] = []
    healthy = True
    reject = None

    def __init__(self, base_url, **kw):
        self.base_url = base_url
        self.posted = None
        FakeClient.instances.append(self)

    def wait_healthy(self, timeout_s, poll_s=0.5):
        return FakeClient.healthy

    def post_workorders(self, orders):
        if FakeClient.reject:
            raise WorkOrderRejected(FakeClient.reject)
        self.posted = orders
        return len(orders)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.healthy = True
    FakeClient.reject = None
    monkeypatch.setattr(submit, "EcsClient", FakeClient)


def test_main_posts_generated_orders(capsys):
    assert submit.main(["scenarios/baseline.yaml", "--ecs", "http://x"]) == 0
    posted = FakeClient.instances[0].posted
    assert len(posted) == 200
    assert {o.type for o in posted} == {JobType.DISCHARGE}
    assert {o.sts_id for o in posted} == {"STS-01", "STS-02"}
    assert '"accepted": 200' in capsys.readouterr().out


def test_main_seed_override_is_accepted():
    assert submit.main(["scenarios/baseline.yaml", "--ecs", "http://x", "--seed", "7"]) == 0


def test_main_unhealthy_exits_1(capsys):
    FakeClient.healthy = False
    assert submit.main(["scenarios/baseline.yaml", "--ecs", "http://x"]) == 1
    assert capsys.readouterr().err


def test_main_rejected_exits_2(capsys):
    FakeClient.reject = [{"container_id": "C1", "code": "UNKNOWN_STS", "message": "m"}]
    assert submit.main(["scenarios/baseline.yaml", "--ecs", "http://x"]) == 2
    assert "UNKNOWN_STS" in capsys.readouterr().err
