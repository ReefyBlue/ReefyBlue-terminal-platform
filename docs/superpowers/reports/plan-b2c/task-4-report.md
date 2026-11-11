# Task 4 report
Commit aefbb3b feat(soak): terminal_soak package with LinkProxy and chaos cutter.
- RED: tests/soak collection error (no terminal_soak). GREEN after impl; one more RED/fix: spec "cut at 100, restore 110, cut 200" means cut-to-cut period, so next_cut advances by every_s (not restore+every).
- LinkProxy moved verbatim (+ `_reset`) to terminal_soak/linkproxy.py; opcua_procs re-exports it (unused struct/threading imports dropped). Extension: optional `bind` kwarg (listen address, default = host) so chaos can listen on 0.0.0.0 while targeting a resolved host; existing API unchanged.
- chaos: Schedule.run restores the link if stopped mid-cut; every_s<=0 just waits on stop. main resolves target once, SIGTERM/SIGINT handled. Log lines are printed JSON `{"event","at"}` on stdout (configure_json_logging exists but its schema is ts/level/msg, so not used).
- Root pyproject: dev group, sources, pythonpath (packages/soak/src, plus tests/e2e so tests/soak can import opcua_procs). Architecture: soak allowed {terminal_model, terminal_tos_stub}. uv.lock committed.
- Tests: tests/soak + architecture 45 passed; full suite 586 passed, 1 failed = known flaky test_connection_lost_fails_fast (passes alone); blip e2e 2 passed (98s). CLI smoke printed cut/restore lines.
- Note: terminal-soak declares httpx and terminal-tos-stub deps but code imports neither yet.

## Fix report (review round 1)
- Important: chaos.main no longer resolves the target; hostname is passed to `LinkProxy(host=target_host, bind=args.host)` so each connect re-resolves (no stale IP, no crash if `gateway` not yet resolvable). Test: `test_main_passes_hostname_not_ip`.
- (a) linkproxy `_accept`: transient OSError now sleeps 50 ms and continues unless `_closed`.
- (b) Schedule: `next_cut = max(next_cut + every_s, clock())`; `test_schedule_clamps_catch_up_after_pause` fails without the clamp (verified), passes with it.
- (c) `--target` validated via `ap.error` (no host / non-numeric port); `test_main_rejects_bad_target`.
- Added loopback test of `LinkProxy(bind=...)` cut/restore plus the JSON event line format.
- Results: soak+architecture 49 passed; blip e2e 2 passed; full suite 591 passed (flaky test passed this time).
