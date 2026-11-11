# Task 5 report — e2e on the small v2 layout + docs

**Status: DONE_WITH_CONCERNS / partially BLOCKED.** Everything is implemented and committed. The one thing that does not work yet: the OPC UA stack cannot run `terminal_mv_small.yaml` with its full fleet because of the port-slot limit. The new e2e test documents this as a strict xfail.

Branch `feat/plan-c1-terminal-design`.

## Commits
- `c3502fa` feat(sim): scenario fleet capped by the layout; mv_small_baseline.yaml
- `38f1cf0` test(e2e): OPC UA stack on terminal_mv_small.yaml; README layout section

## What changed
1. **Scenario caps (ruling).** `load_scenario(path, layout: Layout | None = None)`:
   - With a layout: `n_sts` must be in 1..len(layout.sts) and `n_agv` in 1..len(layout.agv_start_cells). The error reads `"<field> must be 1..<N> for layout, got <v>"`.
   - Without a layout: only the `>= 1` check.
   - `terminal_sim.serve` and `terminal_runner.run.run_replications` now load the layout first and pass it in.
   - In `tests/sim/test_scenario.py` I dropped the old upper-bound rejects (`n_agv 7`, `n_sts 3` with no layout) and added: a test that without a layout only the lower bounds apply, layout-cap tests for both `terminal.yaml` (2/6) and `terminal_mv_small.yaml` (3/12), and a test that the full mv_small fleet loads.
2. **`tests/e2e/fixtures/mv_small_baseline.yaml`** has the baseline values with `n_sts: 3`, `n_agv: 12`, `name: mv_small_baseline`. `test_mv_small_baseline.py` loads it with the layout, and `mv_small_baseline_seed1.json` is byte-identical.
3. **Gateway.** `load_gateway_layout` had its own v1-only loader and failed on v2 with a KeyError. It now detects `version: 2` and goes through `terminal_model.layout.load_terminal` (allowed import; no simulator import):
   - STS ids
   - one ASC per block (`asc_ws_id`, the same as the sim's grid view)
   - the `opcua` block

   New test file: `tests/gateway/test_serve_layout.py`. It checks legacy, that the v2 ids equal the sim view for both shipped files, and the upstream table.
4. **`tests/e2e/opcua_procs.py`.**
   - `sim_and_gateway(..., layout=ROOT/"terminal.yaml")` passes the layout to the sim (`--layout`) and the gateway (`--config`).
   - `n_equipment` is computed from the layout's blocks.
   - `ecs_service(..., layout=...)` passes `--layout`.
   - `--n-sts/--n-agv` were already passed to the gateway from the caller's `n_sts`/`n_agv`. The new test passes the scenario's values.
5. **`tests/e2e/test_mv_small_opcua.py`** (marker `e2e_opcua`):
   - Runs the stack on `terminal_mv_small.yaml` + `mv_small_baseline.yaml` with `run_once_http`.
   - Asserts `/layout` has `version == 2`, there are 4 `terminal.blocks`, 200 done, 0 failed, not timed out, and clean exit codes.
   - Marked `xfail(strict=True, raises=RuntimeError)`; see Blocker.
6. **README.** New section "Layout in meters (C1a)" (Dutch), placed after the NAS section and before "Verder". It covers:
   - the v2 format, with a mini example
   - the two generated files and `scripts/gen_layouts.py`
   - how the grid is derived (cols/rows, lane cells, tp cell, start cells)
   - that `terminal.yaml` stays the legacy v1 layout
   - the layout-capped scenario fields, with a runner command (verified: 200 done)
   - the OPC UA limitation
   - what C1b and C3 add

   The scenario table rows for `n_sts`/`n_agv` now describe the layout-derived caps.
7. **Spec C1.**
   - §1 criterion 4: `terminal.yaml` keeps its name; there is no `terminal_legacy.yaml`.
   - §3: 24 blocks (A–X, x0 = 25 + 42·i); yard y 165–425; crane track 110–460 ≈ 350 m; landside truck head y 425–460.
   - Also fixed the same stale facts in the §8 C1a row (`terminal_legacy.yaml`) and §9 decision 1 (16 → 24 blocks).
   - Not touched: the plan file and global-constraints, which still say 16 blocks / 525–560 (they are historical plan text).

## Blocker (evidence)
`terminal_sim/serve.py` has `MAX_EQUIPMENT = 12` and `CONTROL_PORT_OFFSET = 12` ("fixed by the spec"; Plan B1 spec §… "controlserver op base_port + 12"). `terminal_gateway/gateway.py` has `CONTROL_PORT_OFFSET = 12`. mv_small with its full fleet is 3 STS + 12 AGV + 4 ASC = 19 devices. Logs from the run:
- sim.log: `ValueError: 19 equipment exceeds the 12 port slots`
- gateway.log: `ValueError: 19 equipment exceeds the 12 port slots` (from `upstream_table`)

**Experiment (reverted, not committed).** I set both constants to 32 and made `free_base_port` check 33 ports. With that, the test **passed** with no other change:
- `containers_done 200`, `failed 0`, `timed_out 0`
- `moves_per_hour_total` 87.4 (about 29 per STS)
- run 165 s wall, 196 s total

So, as far as this test shows, the slot limit is the only obstacle. Options for the controller:
- (a) Move the control slot to a larger fixed offset (e.g. +32 or +64), or put it before the equipment slots. This touches the sim, the gateway, `opcua_procs` (`free_base_port` range of 13, LinkProxy 13 slots), the deploy compose/healthcheck (only 48400 is used there; check before changing) and the Plan B1 spec.
- (b) Size the slot count from the layout (sim and gateway both know the layout).
- (c) Run mv_small over OPC UA with a reduced fleet (e.g. `n_agv 5` → 12 devices). This deviates from the ruling.

When the limit is lifted, the strict xfail turns into a failure. Remove the marker then.

## Test results
- `uv run pytest -q`: **691 passed**, 18 deselected (4 m 25 s). Afterwards I re-ran sim/gateway/runner/architecture after the import-sort fix: 242 passed.
- `uv run pytest -m "e2e_opcua and not slow" tests/e2e -q`: **6 passed, 1 xfailed** (8 m 05 s).
- `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q`: **10 passed** (92 s and 97 s, two consecutive runs).
  - Earlier, 3 runs had 1 error in `test_panel_shows_alarms_and_release_works`: `fault_agv` setup failed with "Start rejected by the sim", and the Start never reached the sim.
  - The first of those overlapped my background e2e run. The failing runs took 168–195 s against about 90 s for passing runs, which points to host load.
  - The same test passed twice in isolation with my changes, and the full suite then passed twice.
  - My changes do not touch that path (legacy `terminal.yaml`, scenario within caps). I believe it is load flakiness, but I did not root-cause it.
- ruff: the files I changed are clean apart from pre-existing BLE001 in `opcua_procs` and C408 in `test_scenario.py`.

## Self-review notes
- Sim and runner callers that pass no layout (many tests use `load_scenario(path)`) still work. Fleet bounds against the actual layout for those callers are enforced downstream as before: the world uses `agv_start_cells`, and the runner's `_check_fleet`/`_fleet_mismatch`.
- `run_once`/`run_once_http` don't call `load_scenario`, so they are unaffected.
- The README sentence "simulator, gateway en ECS-service werken op het 20 m-grid" is a slight simplification: the gateway only needs ids, not the grid.

## Memory-worthy
Nothing new about the user.

---

# Fix report — port-slot ruling (option b)

**Status: DONE.** Commit `a58ed75` feat(opcua): clock server on base_port + n_equipment; mv_small runs over OPC UA.

## Changes
- **Sim** (`terminal_sim/serve.py`): `MAX_EQUIPMENT`/`CONTROL_PORT_OFFSET` removed. The control server listens on `base_port + len(world.equipment)` (STS, AGV, then one ASC per block). Device slots are unchanged.
- **Gateway** (`gateway.py`): `CONTROL_PORT_OFFSET` and the slot check are removed. A new attribute `Gateway.control_port = base_port + len(self.table)` comes from the same `n_sts` + `n_agv` + layout-block formula, and the control mirror uses it.
- **Legacy fleet** 2 + 6 + 4 is still at `+12`. The deploy compose and healthcheck (48400) and every fixture are untouched.
- **Tests:**
  - New `test_control_slot_follows_the_fleet` on both sides (TDD, red first):
    - sim: 1+2+4 → +7, 2+6+4 → +12, mv_small 3+12+4 → +19
    - gateway: legacy → +12, 1+0+4 → +5, mv_small → +19
  - Tests that hard-coded `+12` with smaller fleets now use the derived port: `test_serve.py` (+7 via `CONTROL_OFFSET`), `test_gateway.py` (`rig.gateway.control_port`), `test_opcua_port.py` and `test_port_reconnect_ecs.py` (`len(world.equipment)`).
  - Removed `test_too_many_equipment_raises`, since the limit no longer exists.
- **`opcua_procs`:**
  - `free_base_port(n_slots)`; the range and the LinkProxy table are sized `n_equipment + 1`.
  - **Readiness also waits for the gateway's clock link**: the gateway's `Clock/SpeedFactor` must be > 0. It stays 0.0 until the control mirror has read it from the sim.
  - This fixes the e2e_ui `test_panel_shows_alarms_and_release_works` flake from the first report. Root cause: gateway.log showed `control: Start not forwarded, upstream unavailable` 3 s before `control: connected`, because the control server starts last in the sim and readiness only checked equipment. It predates this task.
- **`test_opcua_port._free_base_port`** now checks that the range is bindable. Two full-suite runs had a module-fixture `OSError` (port collision) there; it was not reproducible in 3 subset runs. This is defensive hardening.
- **`test_mv_small_opcua.py`**: the xfail is removed.
- **Docs:**
  - README: the sim row and the C1a "OPC UA" paragraph describe `base_port + n_equipment`.
  - Plan B1 spec: amendment note on the control slot.
  - Plan C1a Global Constraints line (plan file and the sdd copy): 24 blocks A–X, yard y 165–425; truck heads y 425–460 (depth 460).

## Test results
- `uv run pytest -m e2e_opcua tests/e2e/test_mv_small_opcua.py -q` ×2:
  - run 1: passed (200 done, 0 failed, 88.3 moves/h, 163 s wall)
  - run 2: passed (200 done, 0 failed, 86.9 moves/h, 166 s wall)
- `uv run pytest -q`: the last full run had **692 passed**. Two earlier runs had OSError port collisions in `test_opcua_port`; that test was then hardened, and `tests/opcua` gives 39 passed ×4.
- `uv run pytest -m "e2e_opcua and not slow" tests/e2e -q`: **7 passed** (twice; the second run came after the readiness change, 11 min).
- `VIEWER_BROWSER=/usr/bin/google-chrome uv run pytest -m e2e_ui tests/ui -q`: **10 passed** after the readiness fix. The run before it had 1 error (the race above).

## Notes
- If a gateway is started with `--n-sts/--n-agv` that differ from the sim's scenario, it now looks for the clock on a different port than the sim uses: the control link stays unavailable and Start is "not forwarded". Before, a mismatch only left some equipment unmirrored. `opcua_procs` passes matching values. Real deployments use the defaults 2/6 = the soak scenario.
- `test_serve.py` still has a pre-existing ruff I001.
