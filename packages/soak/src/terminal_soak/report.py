"""Soak report (spec B2c §3.3): samples.jsonl (+ restarts.json) -> markdown verdict.

Usage: python -m terminal_soak.report samples.jsonl [--restarts restarts.json]
       [--window-hours 6] [--manual] > soak-report.md

Moves/hour use wall time (the ECS ``kpis`` are sim-time based). ``rss_mb`` is the
peak RSS (ru_maxrss), so it can only grow: criterion 2 compares the last sample with
the hour-2 sample (the second sample when the run is shorter than 2 h).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SERVICES = ("sim", "gateway", "chaos", "ecs", "soak")
RSS_FACTOR = 1.2
TOLERANCE = 0.2
MAX_RECOVERY_S = 120.0
STUCK_S = 600.0  # needs_release/unknown_outcomes > 0 for longer than 10 min
NM = "niet gemeten"


@dataclass
class Criterion:
    name: str
    passed: bool | None  # None = niet gemeten (or handmatig)
    detail: str


@dataclass
class Window:
    start: str
    end: str
    moves_per_h: float
    rss_mb: float | None
    offline: int
    needs_release: int
    unknown: int
    partial: bool = False  # trailing window shorter than the window length


@dataclass
class Cut:
    at: str
    recovered_s: float


@dataclass
class Report:
    start: str | None
    end: str | None
    hours: float
    samples: int
    criteria: list[Criterion]
    windows: list[Window] = field(default_factory=list)
    cuts: list[Cut] = field(default_factory=list)
    ecs_resets_seen: int = 0
    skipped_windows: int = 0  # windows without two samples that have a status
    no_status_samples: int = 0


def _t(s: dict) -> datetime:
    return datetime.fromisoformat(s["wall_time"])


def _st(s: dict, key: str) -> int:
    return int((s.get("status") or {}).get(key) or 0)


def _rss(s: dict) -> float | None:
    v = (s.get("health") or {}).get("rss_mb")
    return float(v) if v is not None else None


def _has_status(s: dict) -> bool:
    """The driver writes ``status == {}`` when the ECS was unreachable: no data."""
    return (s.get("status") or {}).get("done") is not None


def _windows(samples: list[dict], hours: float) -> tuple[list[Window], int]:
    if len(samples) < 2:
        return [], 0
    t0, tn = _t(samples[0]), _t(samples[-1])
    step = timedelta(hours=hours)
    out: list[Window] = []
    skipped = 0
    k = 0
    while t0 + k * step < tn:
        lo, hi = t0 + k * step, min(t0 + (k + 1) * step, tn)
        inside = [s for s in samples if lo <= _t(s) <= hi and _has_status(s)]
        k += 1
        if len(inside) < 2 or _t(inside[-1]) <= _t(inside[0]):
            skipped += 1
            continue
        a, b = inside[0], inside[-1]
        dh = (_t(b) - _t(a)).total_seconds() / 3600.0
        out.append(Window(a["wall_time"], b["wall_time"], (_st(b, "done") - _st(a, "done")) / dh,
                          _rss(b), _st(b, "offline"), _st(b, "needs_release"), _st(b, "unknown_outcomes"),
                          partial=hi - lo < step))
    return out, skipped


def _c1(restarts: dict[str, int] | None) -> Criterion:
    name = "1 Geen container-herstart"
    if restarts is None:
        return Criterion(name, None, f"{NM} (geen restarts-bestand)")
    bad = {k: restarts[k] for k in SERVICES if restarts.get(k)}
    if bad:
        return Criterion(name, False, "herstarts: " + ", ".join(f"{k}={v}" for k, v in bad.items()))
    missing = [k for k in SERVICES if k not in restarts]
    if missing:
        return Criterion(name, None, f"{NM} voor {', '.join(missing)}")
    return Criterion(name, True, "restart_count 0 voor " + ", ".join(SERVICES))


def _c2(samples: list[dict]) -> Criterion:
    name = "2 RSS stabiel"
    if len(samples) < 3:
        return Criterion(name, None, f"{NM} (< 3 meetpunten)")
    t0 = _t(samples[0])
    # the reference must be an earlier sample than the last one
    ref = next((s for s in samples[:-1] if _t(s) - t0 >= timedelta(hours=2)), None)
    ref_label = "uur 2"
    if ref is None:
        ref, ref_label = samples[1], "tweede meetpunt"
    r, last = _rss(ref), _rss(samples[-1])
    if r is None or last is None or r <= 0:
        return Criterion(name, None, f"{NM} (geen rss_mb)")
    ok = last <= RSS_FACTOR * r
    return Criterion(name, ok, f"laatste {last:.1f} MB vs {ref_label} {r:.1f} MB (limiet {RSS_FACTOR * r:.1f} MB)")


def _c3(windows: list[Window]) -> Criterion:
    name = "3 Doorvoer stabiel"
    if not windows:
        return Criterion(name, None, f"{NM} (geen venster)")
    judged = [w for w in windows if not w.partial]
    if not judged:  # run shorter than one window: only "moves happen"
        w = windows[0]
        return Criterion(name, w.moves_per_h > 0,
                         f"alleen deelvenster, {w.moves_per_h:.1f} moves/uur (geen ±20 %-toets)")
    first = judged[0].moves_per_h
    if first <= 0:
        return Criterion(name, False, "eerste venster 0 moves/uur")
    dev = max(abs(w.moves_per_h - first) / first for w in judged)
    ok = dev <= TOLERANCE
    extra = " (deelvenster niet meegeteld)" if len(judged) < len(windows) else ""
    return Criterion(name, ok, f"{len(judged)} venster(s), eerste {first:.1f} moves/uur, "
                               f"grootste afwijking {dev * 100:.0f} % (max {TOLERANCE * 100:.0f} %){extra}")


def _c4(samples: list[dict], cuts: list[Cut]) -> Criterion:
    name = "4 Cuts hersteld"
    if not samples:
        return Criterion(name, None, NM)
    last = samples[-1]
    with_status = [x for x in samples if _has_status(x)]
    if not with_status:
        return Criterion(name, None, "niet gemeten (geen status in laatste meetpunt)")
    alarm_src = with_status[-1]
    slow = [c for c in cuts if c.recovered_s > MAX_RECOVERY_S]
    open_since = last.get("cut_open_since")
    lost = int((alarm_src.get("alarms_by_kind") or {}).get("LINK_LOST", 0))
    problems = []
    if slow:
        problems.append(f"{len(slow)} cut(s) > {MAX_RECOVERY_S:.0f} s hersteld (max {max(c.recovered_s for c in slow):.0f} s)")
    if open_since:
        problems.append(f"cut open sinds {open_since}")
    if lost:
        problems.append(f"LINK_LOST actief ({lost})")
    if problems:
        return Criterion(name, False, "; ".join(problems))
    mx = max((c.recovered_s for c in cuts), default=0.0)
    return Criterion(name, True, f"{len(cuts)} cut(s), langste herstel {mx:.0f} s, geen LINK_LOST aan het eind")


def _c5(samples: list[dict], manual: bool) -> Criterion:
    name = "5 Geen vastzittend werk"
    if not samples:
        return Criterion(name, None, NM)
    samples = [x for x in samples if _has_status(x)]
    if not samples:
        return Criterion(name, None, "niet gemeten (geen status in laatste meetpunt)")
    last = samples[-1]
    nr, uo = _st(last, "needs_release"), _st(last, "unknown_outcomes")
    if manual:
        return Criterion(name, None, f"handmatig (AUTO_RELEASE=0): needs_release={nr}, unknown_outcomes={uo}")
    if nr == 0 and uo == 0:
        return Criterion(name, True, "needs_release 0, unknown_outcomes 0")
    since = _t(last)
    for s in reversed(samples):
        if _st(s, "needs_release") == 0 and _st(s, "unknown_outcomes") == 0:
            break
        since = _t(s)
    mins = (_t(last) - since).total_seconds() / 60.0
    note = f", minstens {mins:.0f} min zichtbaar" if mins * 60 > STUCK_S else ""
    return Criterion(name, False, f"needs_release={nr}, unknown_outcomes={uo} in laatste meetpunt{note}")


def _c6(samples: list[dict]) -> Criterion:
    name = "6 UI bereikbaar"
    if not samples:
        return Criterion(name, None, NM)
    bad = sum(1 for s in samples if not s.get("ui_ok"))
    return Criterion(name, bad == 0, f"ui_ok in {len(samples) - bad}/{len(samples)} meetpunten")


def evaluate(samples: list[dict], restarts: dict[str, int] | None, *, window_hours: float = 6.0,
             manual: bool = False) -> Report:
    windows, skipped = _windows(samples, window_hours)
    cuts = [Cut(r["at"], float(r["recovered_s"])) for r in (samples[-1].get("recoveries") or [])] if samples else []
    criteria = [_c1(restarts), _c2(samples), _c3(windows), _c4(samples, cuts), _c5(samples, manual), _c6(samples)]
    hours = (_t(samples[-1]) - _t(samples[0])).total_seconds() / 3600.0 if samples else 0.0
    return Report(samples[0]["wall_time"] if samples else None, samples[-1]["wall_time"] if samples else None,
                  hours, len(samples), criteria, windows, cuts,
                  int(samples[-1].get("ecs_resets_seen") or 0) if samples else 0,
                  skipped, sum(1 for x in samples if not _has_status(x)))


def _mark(p: bool | None) -> str:
    return "–" if p is None else ("✅" if p else "❌")


def render_markdown(rep: Report) -> str:
    L = ["# Soak-rapport", "",
         f"- Start: {rep.start or '-'}", f"- Einde: {rep.end or '-'}",
         f"- Duur: {rep.hours:.2f} uur", f"- Meetpunten: {rep.samples}", "",
         "## Criteria", "", "| # | Criterium | Uitslag | Detail |", "|---|---|---|---|"]
    for c in rep.criteria:
        num, _, label = c.name.partition(" ")
        L.append(f"| {num} | {label} | {_mark(c.passed)} | {c.detail} |")
    L += ["", "## Vensters", "", "| Venster | Van | Tot | Moves/uur | RSS (MB) | Offline | Needs release | Unknown |",
          "|---|---|---|---|---|---|---|---|"]
    for i, w in enumerate(rep.windows, 1):
        rss = "-" if w.rss_mb is None else f"{w.rss_mb:.1f}"
        label = f"{i} (deel)" if w.partial else str(i)
        L.append(f"| {label} | {w.start} | {w.end} | {w.moves_per_h:.1f} | {rss} | {w.offline} | {w.needs_release} | {w.unknown} |")
    if not rep.windows:
        L.append("| - | - | - | niet gemeten | - | - | - | - |")
    L += ["", "## Cuts", "", "| Cut | Begin | Herstel (s) |", "|---|---|---|"]
    for i, c in enumerate(rep.cuts, 1):
        L.append(f"| {i} | {c.at} | {c.recovered_s:.1f} |")
    if not rep.cuts:
        L.append("| - | geen cuts gezien | - |")
    notes = []
    if rep.no_status_samples:
        notes.append(f"- {rep.no_status_samples} meetpunt(en) zonder status (ECS onbereikbaar) genegeerd voor vensters en criteria 4/5.")
    if rep.skipped_windows:
        notes.append(f"- {rep.skipped_windows} venster(s) overgeslagen: minder dan twee meetpunten met status.")
    L += ["", "## Kanttekeningen", "", *notes,
          "- rss_mb is de piek-RSS (ru_maxrss) en kan alleen stijgen; criterium 2 vergelijkt het laatste meetpunt met uur 2 (bij een run korter dan 2 uur met het tweede meetpunt).",
          f"- ECS-herstarts gezien door de driver (ecs_resets_seen): {rep.ecs_resets_seen}.",
          "- Moves/uur is gemeten in wandtijd (Δ status.done); YARD_FULL wordt niet in de samples vastgelegd en dus niet door dit rapport beoordeeld.", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="terminal_soak.report")
    ap.add_argument("samples")
    ap.add_argument("--restarts")
    ap.add_argument("--window-hours", type=float, default=6.0)
    ap.add_argument("--manual", action="store_true", help="AUTO_RELEASE=0: criterium 5 is handmatig")
    args = ap.parse_args(argv)
    with open(args.samples) as f:
        samples = [json.loads(line) for line in f if line.strip()]
    restarts = None
    if args.restarts:
        with open(args.restarts) as f:
            restarts = json.load(f)
    sys.stdout.write(render_markdown(evaluate(samples, restarts, window_hours=args.window_hours,
                                              manual=args.manual)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
