# Plan C1b — Equipment Model at Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On v2 layouts the simulator moves STS, Lift-AGVs and ASC pairs with real kinematics, hands containers over via transfer racks and STS lanes, stacks in columns with rehandles, and the ECS drives that process; v1 layouts keep today's behaviour byte-for-byte.

**Architecture:** A `terminal_sim.kinematics` module (trapezoidal axis profiles, Konecranes defaults, per-equipment overrides from the layout). A new v2 world (`terminal_sim.world2`) with `Stack` (columns), `Racks`, `LiftAGV`, `Sts2`, `AscPair` + `BlockTrack`; `World` chooses it when the layout carries `transfer_zones`. The dispatcher gains a **rack mode** (new stages, rack/lane selection, rehandle jobs) selected by the same condition; the legacy path is untouched. OPC UA gets the new variables and a `TransferZone` object per block; KPIs get STS moves/hour, waits and utilisations; the TOS stub gets yard rules. The mv_small fixtures are re-baselined (behaviour change intended), the legacy fixtures stay exact.

**Tech Stack:** Python 3.12, salabim, asyncua, existing uv workspace. No new deps.

**Spec:** `docs/superpowers/specs/2026-11-11-plan-c1-terminal-design.md` (§5, §6, §7; §1 criteria 2, 3)

## Global Constraints

- Legacy (v1 `terminal.yaml`) behaviour unchanged: `tests/e2e/fixtures/plan_a_baseline_seed1.json`, `faults_pre_b2d_seed1.json`, service/opcua equivalence stay byte-identical. Every dispatcher change goes behind `self.rack_mode` and re-baselines `tests/fixtures/ecs_sources.sha256` in the same commit with the reason.
- Kinematics defaults (spec §5, Konecranes order of magnitude): STS trolley v 3.5 m/s a 0.8; hoist laden 1.4 / empty 2.8 m/s a 0.6; gantry 0.75 m/s a 0.2; `twistlock_s 12`. Lift-AGV v 6.0 straight / 3.0 turning / 1.0 in zones, a 1.0, d 1.2; `rack_position_s 8`, `rack_lift_s 12`; length 14.8, width 3.0. ASC gantry 2.4 m/s a 0.5; trolley 1.17 m/s a 0.5; hoist laden 0.75 / empty 1.5 m/s a 0.4; `spreader_lock_s 6`. Overrides via layout `kinematics:` keyed by equipment id or kind.
- Pitches from the layout (bay 6.5, row 2.9, tier 2.9). Vessel: 22 rows, 8 tiers on deck, 10 in hold; vessel bays at the STS's berth, pitch 6.5 m, 50 bays per berth starting at `berth.from_m + 25`.
- Position sampling for OPC UA: every `SAMPLE_DT = 0.5 s` sim time during a move.
- `speed_factor` keeps working (sim time vs wall time unaffected by this plan).
- Success criterion §1.3: baseline on `terminal_mv_small.yaml` (3 STS, 12 AGV, 200 containers) gives `sts_moves_per_hour` mean between 25 and 35 (single-cycle). If outside, the kinematics or process is wrong — not the threshold.
- Commit trailer lines: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_011Q6oL3tzJGQycNuMVuzTtk`.
- Branch `feat/plan-c1b-equipment-model` (base `feat/plan-c1-terminal-design`).

## Review Focus

1. An AGV arriving at a transfer zone whose racks are all occupied — must wait in a buffer (`BUFFER_WAIT`), never block the approach lane or drop the container (Task 4 `test_all_racks_full_agv_waits_in_buffer`).
2. A LOAD of a container with three boxes on top — exactly three `STACK_MOVE` jobs precede the `STACK_OUT`, each to a free column in the same block, and `rehandles_per_load` counts 3 (Task 5 `test_load_under_three_boxes_generates_three_rehandles`).
3. Both ASCs of a block needing the same bays — the LS-ASC yields, no deadlock, no overlap below `asc_min_gap_bays` ever (Task 3 `test_asc_pair_never_closer_than_min_gap` property-style over a random job stream).
4. STS lane buffer: AGV late by 2 minutes — the STS puts the box in the lane buffer and continues with the next move; the late AGV picks it up; `sts_wait_for_agv_s` stays 0 for that move (Task 3 `test_sts_uses_lane_buffer_when_agv_late`).
5. Kinematics with a distance shorter than the accel+decel triangle — `travel_time` returns the triangular-profile time, never negative or NaN (Task 1 `test_short_move_triangular`).

---

## File structure

- Create `packages/sim/src/terminal_sim/kinematics.py`; `packages/sim/src/terminal_sim/stack.py`; `packages/sim/src/terminal_sim/world2.py`; `packages/sim/src/terminal_sim/equipment/{lift_agv.py,sts2.py,asc2.py,block_track.py,racks.py,vessel.py}`.
- Modify `packages/model/src/terminal_model/layout.py` (+`kinematics: dict[str, dict[str, float]]` on `TerminalLayout`, validated keys), `terminal_model/types.py` (+`JobType.STACK_MOVE`, `TRUCK_IN`, `TRUCK_OUT`), `terminal_sim/world.py` (factory), `terminal_sim/layout.py` (keep `terminal` on the legacy `Layout` for v2), `terminal_ecs/{orders.py,dispatcher.py}` (rack mode), `terminal_opcua/{nodeset.py,codec.py}`, `terminal_sim/opcua_servers.py`, `terminal_gateway/mirror.py`, `terminal_opcua/port.py`, `terminal_kpi/kpi.py`, `terminal_tos_stub/{generator.py,allocator.py}`, `terminal_runner/run.py`, README, spec.
- Tests under `tests/sim/`, `tests/ecs/`, `tests/opcua/`, `tests/kpi/`, `tests/tos_stub/`, `tests/e2e/`.

---

### Task 1: Kinematics

**Files:** Create `packages/sim/src/terminal_sim/kinematics.py`; modify `terminal_model/layout.py` (`kinematics` field + validation: keys are equipment ids or kinds `STS|AGV|ASC`, values float dicts with known parameter names); Test `tests/sim/test_kinematics.py`, `tests/model/test_layout_kinematics.py`.

**Interfaces (Produces):**
```python
@dataclass(frozen=True) class Axis: v_max: float; accel: float; decel: float
    def travel_time(self, dist_m: float) -> float      # trapezoid or triangle; 0 for dist 0
    def position_at(self, dist_m: float, t: float) -> float   # 0..dist_m
@dataclass(frozen=True) class StsKin: trolley: Axis; hoist_laden: Axis; hoist_empty: Axis; gantry: Axis; twistlock_s: float
@dataclass(frozen=True) class AgvKin: straight: Axis; turning: Axis; zone: Axis; rack_position_s: float; rack_lift_s: float; length_m: float; width_m: float
@dataclass(frozen=True) class AscKin: gantry: Axis; trolley: Axis; hoist_laden: Axis; hoist_empty: Axis; spreader_lock_s: float
DEFAULTS: dict[str, StsKin|AgvKin|AscKin]   # "STS","AGV","ASC"
def kin_for(layout: TerminalLayout, equipment_id: str, kind: str) -> StsKin|AgvKin|AscKin   # DEFAULTS[kind] overridden by layout.kinematics[kind] then [equipment_id]
def sample_times(total_s: float, dt: float = SAMPLE_DT) -> list[float]
SAMPLE_DT = 0.5
```
- [ ] Tests: `test_trapezoid_time` (100 m, v 6, a 1, d 1.2 → analytic value), `test_short_move_triangular` (Review Focus 5: 2 m), `test_position_at_monotonic`, `test_defaults_match_spec` (values above), `test_override_by_kind_and_id` (layout `kinematics: {AGV: {straight.v_max: 5}, "AGV-01": {straight.v_max: 4}}` → AGV-01 4, AGV-02 5), `test_unknown_kinematics_key_rejected`.
- [ ] Implement → run → commit `feat(sim): kinematic axes with Konecranes defaults and layout overrides`.

---

### Task 2: Stack with columns and transfer racks

**Files:** Create `terminal_sim/stack.py`, `terminal_sim/equipment/racks.py`; Test `tests/sim/test_stack.py`, `tests/sim/test_racks.py`.

**Interfaces (Produces):**
```python
SlotLoc = tuple[str, int, int, int]   # (block, bay, row, tier) tier from 0
class Stack:
    def __init__(self, blocks: Sequence[Block]) -> None
    def put(self, block: str, bay: int, row: int, cid: str) -> SlotLoc        # on top of column; raises if full
    def take(self, cid: str) -> SlotLoc                                        # must be on top; raises otherwise
    def top(self, block: str, bay: int, row: int) -> str | None
    def locate(self, cid: str) -> SlotLoc | None
    def above(self, cid: str) -> list[str]                                     # top-down
    def column_height(self, block: str, bay: int, row: int) -> int
    def free_columns(self, block: str, *, exclude_rows: Iterable[int] = ()) -> list[tuple[int,int]]  # sorted by (height, bay, row)
    def occupancy(self, block: str) -> float
class Racks:
    def __init__(self, zones: Sequence[TransferZone]) -> None
    def free_rack(self, block: str, *, reefer: bool = False) -> Rack | None    # first free; reefer only on reefer racks
    def put(self, rack_id: str, cid: str) -> None; def take(self, rack_id: str) -> str; def occupant(self, rack_id) -> str | None
    def occupancy(self, block: str) -> tuple[int, int]
```
- [ ] Tests: put/take ordering, `above`, `free_columns` sort order and reefer-row exclusion, full column error, racks free/put/take/reefer rule, occupancy. Commit `feat(sim): stack columns and transfer racks`.

---

### Task 3: v2 equipment and world

**Files:** Create `equipment/vessel.py`, `equipment/sts2.py`, `equipment/lift_agv.py`, `equipment/block_track.py`, `equipment/asc2.py`, `world2.py`; modify `world.py` (`World.__new__`/factory `make_world(layout, scenario)` returning `World2` when `layout.terminal is not None`), `terminal_sim/layout.py` (legacy `Layout.terminal: TerminalLayout | None`), `scenario.py` (`vessel_bays`, `twin_20: bool = False`); Test `tests/sim/test_sts2.py`, `test_lift_agv.py`, `test_asc_pair.py`, `test_world2.py`.

**Interfaces (Produces):**
- `Vessel(berth: Berth, bays=50, rows=22, tiers_deck=8, tiers_hold=10)`: `bay_x(i)`, `move_time(sts_kin, bay, row, tier, laden) -> float` (trolley from lane to row, hoist down/up, back).
- `Sts2(Equipment)`: lanes from layout; `lane_buffer: dict[lane_id, str|None]`; jobs `DISCHARGE`(vessel→lane)/`LOAD`(lane→vessel); process: pick lane (free, else wait), `move_time`, `twistlock_s`, put box in lane (on AGV if present else buffer); `extra`: `trolley_pos_m, hoist_pos_m, lane_id, lane_buffer_count, wait_for_agv_s`.
- `LiftAGV(Equipment)`: grid driving with per-cell speed (`straight`/`turning` by heading change/`zone` in STS-lane and transfer cells), `slots: {A: cid|None, B: cid|None}`, jobs `TRANSPORT` with `from_loc` ∈ {`LANE:<id>`, `RACK:<id>`} and `to_loc` likewise; rack ops take `rack_position_s + rack_lift_s`; lane ops wait for the STS (passive); `extra`: `heading_deg, speed_mps, load_a, load_b, charge_pct, route`.
- `BlockTrack(bays, min_gap_bays)`: `claim(asc_id, bay_from, bay_to, deadline_s) -> bool`, `release(asc_id)`, `yield_request(from_asc, to_asc)`; WS has priority; property test for Review Focus 3.
- `Asc2(Equipment)` with `role: "WS"|"LS"`; jobs `STACK_IN` (rack→column), `STACK_OUT` (column→rack), `STACK_MOVE` (column→column), `TRUCK_IN/OUT` (LS only, fixed `truck_cycle_s = 180`); leg times from `AscKin`; `extra`: `bay, row, tier, trolley_pos_m, partner, role`.
- `World2`: same public surface as `World` (`equipment`, `run`, `now`, events, `faults`, `snapshot`) plus `stack: Stack`, `racks: Racks`, `vessels`; handovers: `transfer_sts_to_lane`, `transfer_lane_to_agv`, `transfer_agv_to_rack`, `transfer_rack_to_asc`, …; `Equipment.extra()` includes `loaded_container_id` for AGVs (slot A or B) for B2d compatibility.
- [ ] Tests per component with scripted jobs; `test_world2_discharge_roundtrip` (one container vessel→lane→AGV→rack→stack with timings in the expected ranges: STS move 60–140 s, AGV rack op 20 s), Review Focus 3 and 4 tests. Run legacy sim tests unchanged. Commit `feat(sim): v2 world — STS with lanes and buffer, Lift-AGV with racks, ASC pair on one track`.

---

### Task 4: Dispatcher rack mode — stages, rack/lane selection, buffers

**Files:** Modify `terminal_ecs/orders.py` (+stages `TO_STS_LANE, AT_LANE, TO_RACK, ON_RACK, BUFFER_WAIT, RACK_TO_STS` …, `BOX_ON_AGV` extended, `ROLLBACK` extended), `terminal_ecs/dispatcher.py` (`rack_mode = layout has transfer_zones`; rack-mode planning in separate methods `_plan_rack_discharge/_plan_rack_load`; legacy methods untouched), `terminal_ecs/alarms.py` (none), `tests/fixtures/ecs_sources.sha256`; Test `tests/ecs/test_rack_mode.py` (with `World2` in-process via the sim port).

**Interfaces:** discharge `AWAIT_AGV → TO_STS_LANE → AT_LANE (STS DISCHARGE job) → TO_RACK → ON_RACK (AGV free) → STACKING (WS-ASC STACK_IN) → DONE`; load `AWAIT_ASC → UNSTACKING (STACK_OUT → rack) → ON_RACK → AWAIT_AGV → AGV_TO_RACK → RACK_TO_STS → AT_LANE (STS LOAD) → DONE`. Rack choice `racks.free_rack(block, reefer)`; none → `BUFFER_WAIT` (AGV sent to its STS buffer; re-check each tick; discharge may be re-blocked via `alternative_blocks` from the order). Lane choice: STS lane free or with the matching buffered box. Layout access: dispatcher receives `TerminalLayout | None` (via the legacy `Layout.terminal`).
- [ ] Tests: happy path discharge and load on mv_small in-process (20 containers), Review Focus 1 (`test_all_racks_full_agv_waits_in_buffer`), lane buffer pickup, fault during `ON_RACK` (box stays on rack; order continues after repair), link loss + reconcile unchanged in rack mode (reuse B2d tests with `World2`). Legacy fixtures exact. Commit `feat(ecs): rack-mode dispatching for v2 layouts`.

---

### Task 5: Rehandles, STACK_MOVE, ASC pair assignment, truck in/out

**Files:** Modify `dispatcher.py` (rack mode: `_rehandles_for(cid)` → `STACK_MOVE` jobs to `stack.free_columns` before `STACK_OUT`; WS-ASC for waterside jobs, LS-ASC for `STACK_MOVE` housekeeping and `TRUCK_IN/OUT`; `OrderRecord.rehandles`), `terminal_ecs/workorders.py` (+`WorkOrder.type` `TRUCK_IN|TRUCK_OUT`), hash fixture; Test `tests/ecs/test_rehandles.py`, `tests/ecs/test_truck_orders.py`.
- [ ] Tests: Review Focus 2, `test_rehandle_targets_are_free_columns_same_block`, `test_ls_asc_does_housekeeping_ws_does_waterside`, `test_truck_in_out_fixed_cycle`. Commit `feat(ecs): rehandles via STACK_MOVE and ASC-pair work split`.

---

### Task 6: OPC UA variables and TransferZone objects

**Files:** Modify `terminal_opcua/nodeset.py` (+vars STS `TrolleyPos_m, HoistPos_m, LaneId`; AGV `Heading_deg, Speed_mps, LoadA, LoadB, ChargePct`; ASC `Bay, Row, Tier, TrolleyPos_m, Partner`; new object type `TransferZone` with `Racks[i].ContainerId`), `codec.py` (extra ↔ vars), `terminal_sim/opcua_servers.py` (zone servers on `base_port + n_equipment + 1 + i`; clock stays at `+ n_equipment`), `terminal_gateway/mirror.py` + `gateway.py` (mirror zones), `terminal_opcua/port.py` (`rack_occupancy(block) -> dict[rack_id, str|None]` read from the gateway); Test `tests/opcua/test_v2_vars.py`, `tests/gateway/test_zone_mirror.py`, `tests/e2e/test_mv_small_opcua.py` extended (rack vars visible).
- [ ] Commit `feat(opcua): v2 equipment variables and TransferZone objects end to end`.

---

### Task 7: KPIs, TOS-stub yard rules, runner, fixtures

**Files:** Modify `terminal_kpi/kpi.py` (+`sts_moves_per_hour` per STS + mean, `sts_wait_for_agv_s_mean`, `agv_utilisation_pct`, `agv_wait_rack_s_mean`, `asc_ws_utilisation_pct`, `rehandles_per_load`, `rack_occupancy_pct`), `terminal_tos_stub/generator.py` + `allocator.py` (rack-mode `SlotProvider`: discharge → lowest free column, reefer only in reefer rows, load sequence by bay), `terminal_runner/run.py` (`run_once*` pass `layout.terminal` to the dispatcher), `tests/e2e/fixtures/mv_small_baseline_seed1.json` (re-baseline, reason in commit), `tests/e2e/fixtures/mv_small_baseline.yaml` (`discharge_share 0.5` so loads/rehandles happen); Test `tests/kpi/test_kpi_v2.py`, `tests/tos_stub/test_yard_rules.py`, `tests/e2e/test_mv_small_baseline.py` (+ assert `25 <= sts_moves_per_hour_mean <= 35`, `rehandles_per_load < 1.0`).
- [ ] Commit `feat(kpi,tos_stub): v2 KPIs and yard rules; mv_small baseline re-baselined`.

---

### Task 8: End to end, docs

**Files:** `tests/e2e/test_mv_small_opcua.py` (rack mode through the OPC UA stack and the ECS service; `/equipment` shows `heading_deg` etc.), `README.md` (section "Equipment op schaal (C1b)"), spec §5/§6 amendments for anything ruled during execution, `docs/superpowers/references/c1-terminal/README.md` (what the kinematic defaults are based on).
- [ ] Run `uv run pytest -q`, `-m "e2e_opcua and not slow"`, `-m e2e_ui`; commit `test(e2e): rack-mode stack over OPC UA; C1b docs`.
