# Task 4 report
- port.py: unbind() emits STATE_CHANGED(OFFLINE, LinkReason.LINK_LOST) per known device (cache set OFFLINE first, then events); no-op if already unbound. bind() after an unbind refreshes cache, then emits STATE_CHANGED(fresh state, LINK_RESTORED) for every device; first bind emits nothing. B2a _reconcile and job-id warning removed.
- Kept cache-following of forwarded events and assign_job (harmless).
- Tests: new first_bind/unbind/rebind tests; swaps test adapted; B2a reconcile tests removed; test_ecs_core/test_app adapted to LINK_LOST/RESTORED equipment events; readmit test rewritten to needs_release==1 -> release -> job.
- core.py untouched. tests/ecs_service: 56 passed.
