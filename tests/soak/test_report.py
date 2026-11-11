from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from terminal_soak import report

T0 = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
OK_RESTARTS = {"sim": 0, "gateway": 0, "chaos": 0, "ecs": 0, "soak": 0}


def make_samples(n, *, per_hour=50, rss=None, cuts=(), step_h=1.0, done_per_hour=None,
                 needs_release=0, unknown=0, ui_ok=True, link_lost=0, cut_open=None):
    """n samples ``step_h`` apart; done grows by ``per_hour`` (or done_per_hour(i)) per hour."""
    out, done = [], 0
    for i in range(n):
        if i:
            rate = done_per_hour(i) if done_per_hour else per_hour
            done += int(rate * step_h)
        t = T0 + timedelta(hours=i * step_h)
        out.append({
            "wall_time": t.isoformat(), "sim_time": i * 3600.0,
            "status": {"done": done, "failed": 0, "open": 40, "offline": 0,
                       "needs_release": needs_release, "unknown_outcomes": unknown, "connected": True},
            "kpis": {}, "health": {"rss_mb": (rss[i] if rss else 100.0), "uptime_s": i * 3600, "connected": True},
            "alarms_by_kind": {"LINK_LOST": link_lost} if link_lost else {},
            "ui_ok": ui_ok, "cuts_seen": len(cuts),
            "recoveries": [{"at": T0.isoformat(), "recovered_s": c} for c in cuts],
            "cut_open_since": cut_open, "yard": {}, "batches_posted": i, "ecs_resets_seen": 0,
        })
    return out


def crit(rep, n):
    return rep.criteria[n - 1]


def test_all_pass_on_steady_samples():
    rep = report.evaluate(make_samples(13, cuts=[20.0, 35.5]), OK_RESTARTS)
    assert [c.passed for c in rep.criteria] == [True] * 6
    assert len(rep.criteria) == 6
    assert len(rep.windows) == 2 and rep.windows[0].moves_per_h == 50
    assert [c.recovered_s for c in rep.cuts] == [20.0, 35.5]


def test_rss_growth_fails():
    rss = [100.0, 100.0, 100.0] + [100.0 + 10 * i for i in range(1, 11)]
    rep = report.evaluate(make_samples(13, rss=rss), OK_RESTARTS)
    assert crit(rep, 2).passed is False
    assert crit(rep, 1).passed is True


def test_rss_compares_with_hour_2_sample():
    rss = [10.0, 50.0, 100.0] + [110.0] * 10  # warm-up growth before hour 2 is fine
    assert crit(report.evaluate(make_samples(13, rss=rss), OK_RESTARTS), 2).passed is True


def test_throughput_drop_fails():
    rep = report.evaluate(make_samples(13, done_per_hour=lambda i: 50 if i <= 6 else 30), OK_RESTARTS)
    assert crit(rep, 3).passed is False
    assert rep.windows[1].moves_per_h < 0.8 * rep.windows[0].moves_per_h


def test_throughput_within_20_percent_passes():
    rep = report.evaluate(make_samples(13, done_per_hour=lambda i: 50 if i <= 6 else 42), OK_RESTARTS)
    assert crit(rep, 3).passed is True


def test_cut_not_recovered_fails():
    assert crit(report.evaluate(make_samples(4, cuts=[20.0, 150.0]), OK_RESTARTS), 4).passed is False
    open_cut = report.evaluate(make_samples(4, cut_open=T0.isoformat()), OK_RESTARTS)
    assert crit(open_cut, 4).passed is False
    lost = report.evaluate(make_samples(4, link_lost=1), OK_RESTARTS)
    assert crit(lost, 4).passed is False


def test_restarts_none_is_not_measured():
    rep = report.evaluate(make_samples(4), None)
    assert crit(rep, 1).passed is None
    assert "niet gemeten" in crit(rep, 1).detail
    assert crit(report.evaluate(make_samples(4), {**OK_RESTARTS, "ecs": 1}), 1).passed is False


def test_needs_release_and_unknown_fail_and_manual():
    assert crit(report.evaluate(make_samples(4, needs_release=2), OK_RESTARTS), 5).passed is False
    assert crit(report.evaluate(make_samples(4, unknown=1), OK_RESTARTS), 5).passed is False
    man = report.evaluate(make_samples(4, needs_release=2), OK_RESTARTS, manual=True)
    assert crit(man, 5).passed is None and "handmatig" in crit(man, 5).detail


def test_ui_not_ok_fails():
    s = make_samples(4)
    s[2]["ui_ok"] = False
    assert crit(report.evaluate(s, OK_RESTARTS), 6).passed is False


def test_report_single_sample():
    rep = report.evaluate(make_samples(1), OK_RESTARTS)
    assert crit(rep, 2).passed is None and crit(rep, 3).passed is None
    assert rep.windows == []
    assert "niet gemeten" in report.render_markdown(rep)
    empty = report.evaluate([], None)
    assert [c.passed for c in empty.criteria] == [None] * 6
    report.render_markdown(empty)


def test_short_run_single_window_and_second_sample_for_rss():
    rep = report.evaluate(make_samples(11, step_h=1 / 60, per_hour=600, rss=[50.0] + [100.0] * 10), OK_RESTARTS)
    assert len(rep.windows) == 1
    assert crit(rep, 2).passed is True  # second sample (100) is the reference on runs < 2 h


def test_markdown_has_six_criteria():
    md = report.render_markdown(report.evaluate(make_samples(13, cuts=[20.0]), OK_RESTARTS))
    for n in range(1, 7):
        assert f"| {n} |" in md
    assert "✅" in md and "Kanttekeningen" in md and "ecs_resets_seen" in md and "piek" in md.lower()
    assert "Venster" in md and "Cuts" in md


def test_main_reads_files(tmp_path, capsys):
    p = tmp_path / "samples.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in make_samples(5)) + "\n")
    r = tmp_path / "restarts.json"
    r.write_text(json.dumps(OK_RESTARTS))
    assert report.main([str(p), "--restarts", str(r), "--window-hours", "2"]) == 0
    assert "| 1 |" in capsys.readouterr().out
