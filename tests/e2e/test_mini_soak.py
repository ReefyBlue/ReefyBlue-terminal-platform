"""Mini-soak (spec B2c §5): the soak stack for 10 wall minutes, with chaos cuts.

sim (``--autostart``, ``fixtures/soak_mini.yaml`` = ``deploy/scenarios/soak.yaml`` with
``speed_factor: 20``) + gateway; a chaos ``LinkProxy`` in front of the gateway (cut every
120 s for 15 s); the ECS (``--auto-release``) talks to the gateway through that proxy;
the soak ``Driver`` runs in a daemon thread for 10 minutes (sample every 60 s). The
samples then go through ``report.evaluate``. ~11 min; not in the default suite.

The cuts stop a minute before the driver ends, so its last sample sees a settled link
(a cut at the very end would leave ``LINK_LOST`` and unknown outcomes open).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest
from opcua_procs import ROOT, chaos_proxy, ecs_service, sim_and_gateway
from terminal_sim.layout import load_layout
from terminal_soak import report
from terminal_soak.driver import Driver
from terminal_tos_stub.client import EcsClient

pytestmark = [pytest.mark.e2e_opcua, pytest.mark.slow]

SCENARIO = Path(__file__).parent / "fixtures" / "soak_mini.yaml"
HOURS = 10 / 60
CHAOS_EVERY_S = 120.0
CHAOS_FOR_S = 15.0
QUIET_TAIL_S = 60.0  # no cuts in the driver's last minute
JOIN_SLACK_S = 180.0  # HEALTH_WAIT_S of the driver + last tick and sample
CRITERIA = ("Geen container-herstart", "RSS stabiel", "Doorvoer stabiel", "Cuts hersteld",
            "Geen vastzittend werk", "UI bereikbaar")


def _with_status(samples: list[dict]) -> list[dict]:
    return [s for s in samples if (s.get("status") or {}).get("done") is not None]


def test_mini_soak(tmp_path: Path) -> None:
    log_dir = tmp_path
    driver_log = logging.FileHandler(log_dir / "driver.log")
    driver_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    soak_logger = logging.getLogger("terminal_soak")
    soak_logger.addHandler(driver_log)
    soak_logger.setLevel(logging.INFO)
    out: dict = {}
    try:
        with sim_and_gateway(SCENARIO, n_sts=2, n_agv=6, log_dir=log_dir,
                             extra_sim_args=["--autostart"]) as procs, \
                chaos_proxy(int(procs["endpoint"].rsplit(":", 1)[1]), CHAOS_EVERY_S, CHAOS_FOR_S) as chaos, \
                ecs_service(chaos["endpoint"], log_dir=log_dir, auto_release=True) as ecs:
            client = EcsClient(ecs["url"])
            driver = Driver(client, load_layout(ROOT / "terminal.yaml"), low_water=20, batch=30,
                            loop_s=5.0, sample_every_s=60.0, out_dir=log_dir)

            def run() -> None:
                try:
                    out["rc"] = driver.run(HOURS)
                except BaseException as e:  # noqa: BLE001 - reported by the assertion below
                    out["error"] = e

            thread = threading.Thread(target=run, daemon=True)
            started = time.monotonic()
            thread.start()
            quiet_at = started + HOURS * 3600.0 - QUIET_TAIL_S
            while thread.is_alive() and time.monotonic() < quiet_at:
                thread.join(timeout=1.0)
            chaos["stop_cuts"]()
            thread.join(timeout=HOURS * 3600.0 + JOIN_SLACK_S - (time.monotonic() - started))
            if thread.is_alive():
                driver.stop()
                thread.join(timeout=30.0)
            client.close()
            chaos_events = list(chaos["events"])
    finally:
        soak_logger.removeHandler(driver_log)
        driver_log.close()

    logs = f"see {log_dir} (sim.log, gateway.log, ecs.log, driver.log, samples.jsonl)"
    assert not thread.is_alive(), f"driver still running; {logs}"
    assert out.get("rc") == 0, f"driver: {out}; {logs}"

    samples = [json.loads(line) for line in (log_dir / "samples.jsonl").read_text().splitlines() if line.strip()]
    rep = report.evaluate(samples, None)
    md = report.render_markdown(rep)
    (log_dir / "report.md").write_text(md + "\n")
    short = report.evaluate(samples, None, window_hours=2 / 60)  # 2-minute windows: a stall shows
    print(md)  # visible with -s / on failure
    print(f"chaos events: {chaos_events}")

    for name in CRITERIA:
        assert name in md, f"criterion {name!r} missing from the report; {logs}"
    assert len(samples) >= 10, f"{len(samples)} samples; {logs}"

    # cuts: >= 3 seen, every one recovered in < 120 s, none left open
    assert len(rep.cuts) >= 3, f"cuts {rep.cuts}, chaos {chaos_events}; {logs}"
    assert all(c.recovered_s < report.MAX_RECOVERY_S for c in rep.cuts), f"slow recovery {rep.cuts}; {logs}"
    assert samples[-1]["cut_open_since"] is None, f"cut still open at the end; {logs}"

    # no stuck work at the end (auto-release on)
    with_status = _with_status(samples)
    last = with_status[-1]["status"]
    assert with_status[-1] is samples[-1], f"last sample has no status; {logs}"
    assert last["needs_release"] == 0 and last["unknown_outcomes"] == 0, f"stuck work {last}; {logs}"

    # throughput: moves in every window (the 6-h one is partial: judged anyway), and in
    # every full 2-minute window
    assert rep.windows and all(w.moves_per_h > 0 for w in rep.windows), f"windows {rep.windows}; {logs}"
    full = [w for w in short.windows if not w.partial]
    assert full and all(w.moves_per_h > 0 for w in full), f"2-min windows {short.windows}; {logs}"
    done = with_status[-1]["status"]["done"] - with_status[0]["status"]["done"]
    assert done >= 60, f"status.done grew by {done}; {logs}"

    # memory: peak RSS of the ECS
    rss = [s["health"]["rss_mb"] for s in samples]
    assert rss[1] and rss[-1] <= 1.5 * rss[1], f"rss_mb {rss}; {logs}"

    ecs_log = (log_dir / "ecs.log").read_text()
    assert "YARD_FULL" not in ecs_log, f"YARD_FULL in the ECS log; {logs}"
    assert ecs["returncode"] == 0, f"ECS exit {ecs['returncode']}; {logs}"
