from __future__ import annotations

from pathlib import Path

from fake_port import FakeSlots, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_model.types import Job, JobType, Position, State
from terminal_runner.kpi import KpiCollector


def setup(faults=None):
    port = make_port()
    disp = Dispatcher(port, FakeSlots())
    return port, disp, KpiCollector(port, disp, faults_total=faults)


def run_job(port, eid: str, jtype: JobType, n: int = 1, cid: str = "C0001") -> None:
    job = Job(f"{cid}-{jtype.value}-{n}", jtype, cid, "a", "b")
    assert port.assign_job(eid, job)
    port.complete(eid)


def test_moves_per_hour():
    port, disp, kpi = setup()
    for i in range(10):
        port.advance(300)
        run_job(port, "STS-01", JobType.DISCHARGE, cid=f"C{i:04d}")
    k = kpi.finish(3600.0)
    assert k["moves_per_hour_total"] == 10.0
    assert k["moves_per_hour_STS-01"] == 10.0
    assert "moves_per_hour_STS-02" not in k


def test_utilisation_from_samples():
    port, disp, kpi = setup()
    for _ in range(3):
        port.set_state("AGV-01", State.BUSY)
        port.set_state("AGV-02", State.BUSY)
        kpi.sample()
    port.set_state("AGV-01", State.IDLE)
    port.set_state("AGV-02", State.IDLE)
    kpi.sample()
    k = kpi.finish(240.0)
    assert k["utilisation_AGV"] == 0.75
    assert k["utilisation_STS"] == 0.0


def test_utilisation_without_samples_is_zero():
    port, disp, kpi = setup()
    assert kpi.finish(60.0)["utilisation_ASC"] == 0.0


def test_agv_wait_and_travel_split():
    port, disp, kpi = setup()
    port.set_state("AGV-01", State.BUSY)
    kpi.sample()  # first sample: no previous position -> nothing attributed
    kpi.sample()  # same position -> wait
    kpi.sample()  # wait
    port.set_position("AGV-01", Position(50, 50))
    kpi.sample()  # moved -> travel
    port.advance(240)
    # complete one TRANSPORT job
    port.set_state("AGV-01", State.IDLE)
    run_job(port, "AGV-01", JobType.TRANSPORT, n=2)
    k = kpi.finish(240.0)
    assert k["agv_wait_s_mean"] == 120.0
    assert k["agv_travel_s_mean"] == 60.0


def test_agv_means_zero_without_transports():
    port, disp, kpi = setup()
    k = kpi.finish(60.0)
    assert k["agv_wait_s_mean"] == 0.0
    assert k["agv_travel_s_mean"] == 0.0


def test_turnaround():
    port, disp, kpi = setup()
    port.advance(100)
    run_job(port, "AGV-01", JobType.TRANSPORT)  # first completion of any kind
    port.advance(400)
    run_job(port, "STS-01", JobType.DISCHARGE)
    port.advance(500)
    run_job(port, "STS-01", JobType.LOAD, n=2)  # last crane move at t=1000
    port.advance(300)
    run_job(port, "ASC-A", JobType.STACK_IN)  # later, non-crane: ignored for end
    assert kpi.finish(2000.0)["vessel_turnaround_s"] == 900.0


def test_turnaround_zero_without_moves():
    port, disp, kpi = setup()
    run_job(port, "AGV-01", JobType.TRANSPORT)
    assert kpi.finish(60.0)["vessel_turnaround_s"] == 0.0


def test_faults_replans_and_counts():
    port, disp, kpi = setup(faults=lambda: 7)
    k = kpi.finish(60.0)
    assert k["faults"] == 7.0
    assert k["replans"] == 0.0
    assert k["containers_done"] == 0.0
    assert k["containers_failed"] == 0.0
    assert setup()[2].finish(60.0)["faults"] == 0.0


def test_csv_header_once(tmp_path: Path):
    port, disp, kpi = setup()
    kpis = kpi.finish(3600.0)
    path = tmp_path / "kpi.csv"
    kpi.to_csv_row(path, 1, kpis)
    kpi.to_csv_row(path, 2, kpis)
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0].split(",")[0] == "run_id"
    assert lines[0].split(",")[1:] == list(kpis)
    assert lines[1].split(",")[0] == "1"
    assert lines[2].split(",")[0] == "2"
