"""``AssignJob`` on the gateway is idempotent for a job the upstream already runs or ran.

The blip flake: the ECS re-issues a job id after a gateway method timeout during a
cut while the upstream had executed it; forwarding the re-issue ran the job again.
The mirror now answers True without forwarding when the mirrored ``CurrentJobId`` or
``LastJobId`` equals the job id.
"""

import json
import logging

import pytest
from asyncua import Server
from terminal_gateway.mirror import EquipmentMirror
from terminal_model.types import Kind
from terminal_opcua.nodeset import ensure_folders, register_types


class CountingUpstream:
    """Stands in for the upstream equipment node: counts ``AssignJob`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def call_method(self, method: str, *args):
        self.calls.append((method, args))
        return True


@pytest.fixture
async def mirror(monkeypatch):
    server = Server()
    await server.init()
    idx, types = await register_types(server)
    folders = await ensure_folders(server, idx, server.nodes.objects)
    m = EquipmentMirror(server, idx, types, folders["Quay"], equipment_id="STS-01",
                        kind=Kind.STS, endpoint="opc.tcp://127.0.0.1:1/unused")
    monkeypatch.setattr(m, "_launch", lambda: None)  # no upstream link task
    await m.start()
    up = CountingUpstream()
    m._client, m._uobj, m._uidx = object(), up, 2  # "connected" to the counting upstream
    m.up = up
    yield m
    await server.stop()


def _job(job_id: str) -> str:
    return json.dumps({"job_id": job_id, "job_type": "DISCHARGE", "container_id": "C1",
                       "source": "VESSEL", "destination": "STS-01"})


async def test_assign_idempotent_on_current_job(mirror, caplog):
    await mirror._write("CurrentJobId", "C1-DISCHARGE-1")
    with caplog.at_level(logging.INFO, logger="terminal_gateway.mirror"):
        assert await mirror._assign(_job("C1-DISCHARGE-1")) is True
    assert mirror.up.calls == []
    assert any("already" in r.getMessage() and "idempotent True" in r.getMessage()
               and r.levelno == logging.INFO for r in caplog.records)


async def test_assign_idempotent_on_last_job(mirror):
    await mirror._write("CurrentJobId", "")
    await mirror._write("LastJobId", "C1-DISCHARGE-1")
    assert await mirror._assign(_job("C1-DISCHARGE-1")) is True
    assert mirror.up.calls == []


async def test_assign_forwards_new_job(mirror):
    await mirror._write("CurrentJobId", "C0-DISCHARGE-1")
    await mirror._write("LastJobId", "C9-DISCHARGE-1")
    assert await mirror._assign(_job("C1-DISCHARGE-1")) is True
    assert mirror.up.calls == [("2:AssignJob", (_job("C1-DISCHARGE-1"),))]


async def test_assign_forwards_when_nothing_mirrored(mirror):
    """Empty mirrored ids (no job yet) never match; malformed JSON is forwarded as is
    (the upstream rejects it)."""
    assert await mirror._assign(_job("C1-DISCHARGE-1")) is True
    assert await mirror._assign("not json") is True
    assert [args for _, args in mirror.up.calls] == [(_job("C1-DISCHARGE-1"),), ("not json",)]
