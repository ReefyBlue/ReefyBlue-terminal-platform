# Task 2 report: TOS-stub as HTTP client

Implemented SimpleSlotAllocator.occupy, client.py (EcsClient, WorkOrderRejected), submit.py (main CLI, own yaml loading, no runner/sim imports); deps httpx>=0.27, pyyaml>=6; uv sync (uv.lock updated).

Tests: test_allocator (2 new), test_client (7), test_submit (4). RED: collection ImportError for client/submit. GREEN: `uv run pytest tests/tos_stub tests/test_architecture.py -q` -> 53 passed.

Self-review: layout YAML sts key is `id` (loader maps it to equipment_id), so submit reads `s["id"]`. health() treats connection errors as False; wait_healthy always polls at least once. submit seed defaults to scenario seed; --seed overrides.
Concerns: none; uv.lock committed with the change.
