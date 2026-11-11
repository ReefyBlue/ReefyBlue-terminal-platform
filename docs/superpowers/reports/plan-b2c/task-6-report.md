# Task 6 report: terminal_soak.report
Status: DONE. Files: packages/soak/src/terminal_soak/report.py, tests/soak/test_report.py.
RED: collection error (module missing). GREEN: 13 passed; tests/soak 36 passed; default suite 621 passed, 16 deselected.
Decisions:
- Windows: wall-time, boundaries at start + k*W, first/last sample inside each; boundary samples shared; <2 samples -> skipped.
- Crit 2: reference = first sample >= 2 h after start, else 2nd sample (short runs); <3 samples or no rss -> niet gemeten.
- Crit 3: throughput only (YARD_FULL not in samples, noted in Kanttekeningen); one window passes if >0; first window 0 -> FAIL; no window -> niet gemeten.
- Crit 4: recoveries from last sample (cumulative); 0 cuts passes (mini-soak asserts >=3 itself).
- Crit 5: last sample only; stuck duration added to detail; --manual / manual=True -> passed None, "handmatig".
- Crit 1: missing service key in restarts -> niet gemeten unless some >0 (FAIL).
- evaluate(samples, restarts, *, window_hours=6.0, manual=False); main adds --manual.

## Fix report (review findings)
- Important: samples with empty status (ECS unreachable) are "no data". Criteria 4/5 use the last sample with status (LINK_LOST read from it too); none -> None "niet gemeten (geen status in laatste meetpunt)". Windows pick endpoints only among samples with status.done; windows with <2 usable endpoints are skipped and noted in Kanttekeningen (also count of status-less samples).
- Minor a: criterion 2 reference searched in samples[:-1] (hour 2), else the second sample; never the last sample.
- Minor b: trailing window shorter than window length flagged `partial`, shown "(deel)", excluded from the ±20 % judgement; if the run is shorter than one window only moves/h > 0 is judged.
- Tests: 5 new (RED: 5 failed against old code), GREEN: tests/soak 41 passed; default suite 626 passed, 16 deselected.
