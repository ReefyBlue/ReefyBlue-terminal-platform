from __future__ import annotations

from pathlib import Path

from fake_port import FakeSlots, make_port
from terminal_ecs.dispatcher import Dispatcher
from terminal_ecs.orders import LoggedJob
from terminal_ecs.workorders import WorkOrder
from terminal_model.types import Job, JobType, State
from terminal_kpi.kpi import KpiCollector


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


def _discharge(cid="C1", slot="A/1/1/1"):
    return WorkOrder(container_id=cid, type=JobType.DISCHARGE, sts_id="STS-01", yard_slot=slot)


def _load(cid="L1", slot="A/3/1/1"):
    return WorkOrder(container_id=cid, type=JobType.LOAD, sts_id="STS-01", yard_slot=slot)


def test_agv_kpis_from_discharge_events():
    port, disp, kpi = setup()
    disp.submit([_discharge()])  # t=0: AGV-01 sent to the STS
    port.advance(40)
    port.complete("AGV-01")  # arrives in the lane at t=40 (travel 40)
    port.advance(100)
    port.complete("STS-01")  # box on the AGV at t=140 (lane wait 100); leg to the TP issued
    port.advance(60)
    port.complete("AGV-01")  # at the TP at t=200 (travel 60)
    port.complete("ASC-A")
    k = kpi.finish(200.0)
    assert k["containers_done"] == 1.0
    assert k["agv_wait_sts_s_mean"] == 100.0
    assert k["agv_travel_s_mean"] == 50.0
    assert k["agv_wait_tp_s_mean"] == 0.0  # no LOAD orders


def test_agv_kpis_from_load_events():
    port, disp, kpi = setup()
    assert disp.on_transfer is None  # no hooks: the TP wait comes from the job log
    disp.submit([_load()])
    port.advance(10)
    port.complete("ASC-A")  # box at the TP at t=10: AGV-01 sent
    port.advance(30)
    port.complete("AGV-01")  # at the TP at t=40; leg to the STS assigned at once
    port.advance(50)
    port.complete("AGV-01")  # in the lane at t=90
    port.advance(20)
    port.complete("STS-01")  # loaded at t=110
    k = kpi.finish(110.0)
    assert k["containers_done"] == 1.0
    assert (k["agv_wait_tp_s_mean"], k["agv_wait_sts_s_mean"], k["agv_travel_s_mean"]) == (0.0, 20.0, 40.0)


class _LogOnly:
    """Dispatcher stand-in: only the job log (assignment times are set by the test)."""

    def __init__(self):
        self.job_log: dict[str, LoggedJob] = {}

    def assign(self, port, eid: str, job: Job) -> None:
        self.job_log[job.job_id] = LoggedJob(job, eid, port.now())
        assert port.assign_job(eid, job)


def test_agv_wait_at_tp_until_next_transport_assigned():
    port = make_port()
    disp = _LogOnly()
    kpi = KpiCollector(port, disp)
    disp.assign(port, "AGV-01", Job("L1-TRANSPORT-1", JobType.TRANSPORT, "L1", "AGV-01", "TP/A/WS"))
    port.advance(30)
    port.complete("AGV-01")  # at the TP at t=30
    port.advance(5)
    disp.assign(port, "AGV-02", Job("X-TRANSPORT-1", JobType.TRANSPORT, "X", "AGV-02", "TP/B/WS"))  # other AGV
    port.advance(10)
    disp.assign(port, "AGV-01", Job("L1-TRANSPORT-2", JobType.TRANSPORT, "L1", "TP/A/WS", "STS-01"))  # t=45
    port.advance(50)
    port.complete("AGV-01")
    assert kpi._tp_wait() == [15.0]


def test_agv_wait_at_tp_ignores_discharge_legs_and_unpaired_arrivals():
    port = make_port()
    disp = _LogOnly()
    kpi = KpiCollector(port, disp)
    disp.assign(port, "AGV-01", Job("D1-TRANSPORT-1", JobType.TRANSPORT, "D1", "STS-01", "TP/A/WS"))
    port.advance(30)
    port.complete("AGV-01")  # loaded leg of a DISCHARGE: not a TP wait
    disp.assign(port, "AGV-01", Job("L1-TRANSPORT-1", JobType.TRANSPORT, "L1", "AGV-01", "TP/A/WS"))
    port.advance(30)
    port.complete("AGV-01")  # at the TP, but no next transport yet
    assert kpi._tp_wait() == []


def test_agv_utilisation_counts_committed_idle_agvs():
    port, disp, kpi = setup()
    disp.submit([_discharge()])
    port.complete("AGV-01")  # AGV-01 idle in the lane, still committed to C1
    port.set_state("STS-01", State.FAULT)  # crane down: AGV-01 waits
    assert port.snapshot("AGV-01").state is State.IDLE
    assert disp.committed_agvs() == frozenset({"AGV-01"})
    kpi.sample()
    assert kpi.finish(60.0)["utilisation_AGV"] == 0.5  # AGV-01 committed, AGV-02 free


def test_agv_means_zero_without_transports():
    port, disp, kpi = setup()
    k = kpi.finish(60.0)
    assert k["agv_wait_sts_s_mean"] == 0.0
    assert k["agv_wait_tp_s_mean"] == 0.0
    assert k["agv_travel_s_mean"] == 0.0


def test_kpi_key_order_is_stable():
    port, disp, kpi = setup()
    assert list(kpi.finish(60.0)) == [
        "moves_per_hour_total",
        "moves_per_hour_STS-01",
        "agv_wait_sts_s_mean",
        "agv_wait_tp_s_mean",
        "agv_travel_s_mean",
        "utilisation_STS",
        "utilisation_AGV",
        "utilisation_ASC",
        "vessel_turnaround_s",
        "faults",
        "replans",
        "energy_kwh_per_move",
        "containers_done",
        "containers_failed",
        "containers_stuck",
        "sim_elapsed_s",
    ]


def test_turnaround():
    port, disp, kpi = setup()
    port.advance(100)
    run_job(port, "AGV-01", JobType.TRANSPORT)  # earlier, non-crane: ignored for start
    port.advance(400)
    run_job(port, "STS-01", JobType.DISCHARGE)  # first crane move at t=500
    port.advance(500)
    run_job(port, "STS-01", JobType.LOAD, n=2)  # last crane move at t=1000
    port.advance(300)
    run_job(port, "ASC-A", JobType.STACK_IN)  # later, non-crane: ignored for end
    assert kpi.finish(2000.0)["vessel_turnaround_s"] == 500.0


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


def test_faults_counted_from_fault_events_without_total():
    """OPC UA mode has no faults_total: count STATE_CHANGED -> FAULT events instead."""
    port, disp, kpi = setup()
    port.set_state("AGV-01", State.FAULT)
    port.set_state("AGV-01", State.IDLE)
    port.set_state("ASC-A", State.FAULT)
    port.set_state("ASC-A", State.BUSY)
    assert kpi.finish(60.0)["faults"] == 2.0
    port2, disp2, kpi2 = setup(faults=lambda: 5)
    port2.set_state("AGV-01", State.FAULT)
    assert kpi2.finish(60.0)["faults"] == 5.0  # the explicit total wins


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


def test_snapshot_is_non_destructive_and_matches_finish():
    port, disp, kpi = setup()
    for i in range(10):
        port.advance(300)
        run_job(port, "STS-01", JobType.DISCHARGE, cid=f"C{i:04d}")
    kpi.sample()
    a = kpi.snapshot(3600.0)
    b = kpi.snapshot(3600.0)
    assert a == b and kpi.finish(3600.0) == a
    assert list(a) == list(b)
    assert a["moves_per_hour_total"] == 10.0
    assert a["sim_elapsed_s"] == 3600.0
    assert list(a)[-1] == "sim_elapsed_s"
