"""Parametric builder for the standard ASC terminal (spec §3 / §3.1) and its YAML serialiser.

``standard_terminal(...)`` is the single source of the shipped ``terminal_mv.yaml`` (demo) and
``terminal_mv_small.yaml`` (test layout); ``scripts/gen_layouts.py`` writes them and a test checks
the files still equal the builder output. Metres; origin = quay west corner, x along the quay,
y landward.

Geometry (spec §3 values, generalised where the spec only gives the 1200 m demo):

- Berths: ``n_berths`` equal slices of the quay, ids ``A``, ``B``, ...
- STS rails y 10 (waterside) / 40 (landside). STS ``i`` centre x0 = ``quay_length_m/n_sts*(i+0.5)``
  (1200 m / 8 STS -> 75 + 150·n, as in the spec; derived coordinates are rounded to 1 mm); ``min_gap_m`` 27; ``berth_ids`` = the berth that
  contains x0. Three transfer lanes per STS at x0-3.5 / x0 / x0+3.5, y 45, len 16, width 3.5.
- Apron y 40..165; drive lanes ``DL1``..``DL4`` at y 70/76/82/88, alternating E/W; crossings every
  50 m. Buffers at y 95, at x0 ±8 (and ±16, ±24, ... when ``n_agv_buffers_per_sts`` > 2), ordered
  -8, +8, -16, +16, ...; ids ``BUF-<nn>-<k>``.
- Blocks ``A``, ``B``, ... at x0 = ``first_block_x_m + block_pitch_m·i``, y0 165 (the demo's 24
  blocks span x 25..1020 of the 1200 m quay); ``reefer_rows`` =
  the landward-most row index (``rows-1``); ASC ids ``ASC-<B>-WS`` / ``ASC-<B>-LS``.
- Transfer zone per block: ``racks_per_block`` racks ``RK-<B>-<k>`` (size "40") at y 135, centred
  on the block centre with pitch 3.5 (5 racks -> centre -7 ... +7), plus reefer/exception rack
  ``RK-<B>-R`` at centre +14. Approach lane = the landward-most drive lane (``DL4``).
- Truck heads start at the block end (``y_m`` = block y1 = 165 + 40·6.5 = 425) with 4 lanes and
  ``len_m`` 35, so ``depth_m`` = 460 (the spec's "yard y 165..525" was an arithmetic slip).
- Zones (4-point rectangles, spec §3.1): ``berth`` per berth (x of the berth, y 0..40);
  ``agv_lanes`` = apron; ``agv_parking`` = buffer strip y 92..98; per block ``block``,
  ``transfer_ws`` (block x-range, y 110..160) and ``truck_interchange`` (y 425..460);
  ``maintenance_lane`` in each gap between adjacent blocks. Placeholders: north (landward) strip
  y depth+20..depth+120 — ``rail_interchange`` (y +20..+50) and ``rail_terminal`` (+50..+120)
  over the yard's x-range, and the gate complex (``gate``, ``ocr_gate``, ``tech_gate``, ``customs``,
  ``problem_parking``, side by side) to the north-east; east strip (yard east edge + 20 m .. quay
  end, y 165..depth) split into ``oog``, ``building`` TOB and ``building`` workshop.
"""
from __future__ import annotations

import string
from itertools import pairwise

from terminal_model.layout import (
    Apron,
    Berth,
    Block,
    Buffer,
    DriveLane,
    Lane,
    OpcUa,
    Quay,
    Rack,
    Rails,
    StsSpec,
    TerminalLayout,
    TransferZone,
    TruckHead,
    Zone,
    validate,
)

RAIL_Y_WS_M = 10.0
RAIL_Y_LS_M = 40.0
STS_MIN_GAP_M = 27.0
LANE_Y_M = 45.0
LANE_LEN_M = 16.0
LANE_WIDTH_M = 3.5
APRON_Y_FROM_M = 40.0
APRON_Y_TO_M = 165.0
DRIVE_LANES_Y_M = (70.0, 76.0, 82.0, 88.0)
CROSSINGS_EVERY_M = 50.0
BUFFER_Y_M = 95.0
BUFFER_STEP_M = 8.0
PARKING_Y_M = (92.0, 98.0)
TRANSFER_Y_M = (110.0, 160.0)
RACK_Y_M = 135.0
RACK_PITCH_M = 3.5
REEFER_RACK_OFFSET_M = 14.0
BLOCK_Y0_M = 165.0
TRUCK_HEAD_LEN_M = 35.0
TRUCK_LANES = 4
NORTH_STRIP_M = (20.0, 50.0, 120.0)  # offsets beyond depth: start, rail split, end
EAST_STRIP_GAP_M = 20.0


def _r(v: float) -> float:
    """Round derived coordinates to the millimetre so the shipped YAML has no float noise."""
    return round(float(v), 3) + 0.0


def _rect(x0: float, y0: float, x1: float, y1: float) -> tuple[tuple[float, float], ...]:
    x0, y0, x1, y1 = _r(x0), _r(y0), _r(x1), _r(y1)
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _letters(n: int, what: str) -> str:
    if n > len(string.ascii_uppercase):
        raise ValueError(f"at most {len(string.ascii_uppercase)} {what} supported, got {n}")
    return string.ascii_uppercase[:n]


def standard_terminal(
    *,
    name: str,
    quay_length_m: float,
    n_berths: int,
    n_sts: int,
    n_blocks: int,
    n_agv_buffers_per_sts: int = 2,
    racks_per_block: int = 5,
    block_bays: int = 40,
    block_rows: int = 10,
    block_tiers: int = 5,
    block_pitch_m: float = 42.0,
    first_block_x_m: float = 25.0,
    opcua_base_port: int = 48400,
) -> TerminalLayout:
    """Build (and validate) the standard terminal; see the module docstring for the geometry."""
    if min(n_berths, n_sts, n_blocks, racks_per_block) < 1 or n_agv_buffers_per_sts < 0:
        raise ValueError("counts must be positive")
    q = float(quay_length_m)

    berth_len = q / n_berths
    berths = tuple(
        Berth(bid, _r(berth_len * i), _r(q if i == n_berths - 1 else berth_len * (i + 1)))
        for i, bid in enumerate(_letters(n_berths, "berths")))

    def berth_of(x: float) -> str:
        for b in berths:
            if b.from_m <= x < b.to_m:
                return b.id
        return berths[-1].id

    sts: list[StsSpec] = []
    buffers: list[Buffer] = []
    for i in range(n_sts):
        sid = f"STS-{i + 1:02d}"
        x0 = _r(q / n_sts * (i + 0.5))
        lanes = tuple(
            Lane(f"{sid}-L{k + 1}", _r(x0 + dx), LANE_Y_M, LANE_LEN_M, LANE_WIDTH_M)
            for k, dx in enumerate((-LANE_WIDTH_M, 0.0, LANE_WIDTH_M)))
        sts.append(StsSpec(sid, x0, STS_MIN_GAP_M, (berth_of(x0),), lanes))
        for k in range(n_agv_buffers_per_sts):
            dx = BUFFER_STEP_M * (k // 2 + 1) * (-1 if k % 2 == 0 else 1)
            buffers.append(Buffer(f"BUF-{i + 1:02d}-{k + 1}", _r(x0 + dx), BUFFER_Y_M, sid))

    drive_lanes = tuple(
        DriveLane(f"DL{k + 1}", y, "E" if k % 2 == 0 else "W") for k, y in enumerate(DRIVE_LANES_Y_M))
    approach = drive_lanes[-1].id

    blocks: list[Block] = []
    transfer_zones: list[TransferZone] = []
    landside: list[TruckHead] = []
    for i, bid in enumerate(_letters(n_blocks, "blocks")):
        b = Block(
            id=bid, x0_m=_r(first_block_x_m + block_pitch_m * i), y0_m=BLOCK_Y0_M,
            bays=block_bays, rows=block_rows, tiers=block_tiers,
            asc_ws_id=f"ASC-{bid}-WS", asc_ls_id=f"ASC-{bid}-LS",
            reefer_rows=(block_rows - 1,),
        )
        blocks.append(b)
        centre = b.x0_m + b.width_m / 2
        first = centre - RACK_PITCH_M * (racks_per_block - 1) / 2
        racks = [Rack(f"RK-{bid}-{k + 1}", _r(first + RACK_PITCH_M * k), RACK_Y_M, "40")
                 for k in range(racks_per_block)]
        racks.append(Rack(f"RK-{bid}-R", _r(centre + REEFER_RACK_OFFSET_M), RACK_Y_M, "40", reefer=True))
        transfer_zones.append(TransferZone(bid, tuple(racks), approach))
        landside.append(TruckHead(bid, TRUCK_LANES, _r(b.y1_m), TRUCK_HEAD_LEN_M))

    yard_x0 = blocks[0].x0_m
    yard_x1 = blocks[-1].x1_m
    if yard_x1 > q:
        raise ValueError(f"blocks end at x={yard_x1} m, beyond the {q} m quay")
    depth = max([b.y1_m for b in blocks] + [h.y_m + h.len_m for h in landside])

    zones: list[Zone] = [Zone(f"berth-{b.id}", "berth", _rect(b.from_m, 0, b.to_m, RAIL_Y_LS_M))
                         for b in berths]
    zones.append(Zone("agv-lanes", "agv_lanes", _rect(0, APRON_Y_FROM_M, q, APRON_Y_TO_M)))
    zones.append(Zone("agv-parking", "agv_parking", _rect(0, PARKING_Y_M[0], q, PARKING_Y_M[1])))
    for b in blocks:
        zones.append(Zone(f"transfer-ws-{b.id}", "transfer_ws",
                          _rect(b.x0_m, TRANSFER_Y_M[0], b.x1_m, TRANSFER_Y_M[1])))
        zones.append(Zone(f"block-{b.id}", "block", _rect(b.x0_m, b.y0_m, b.x1_m, b.y1_m)))
        zones.append(Zone(f"truck-interchange-{b.id}", "truck_interchange",
                          _rect(b.x0_m, b.y1_m, b.x1_m, b.y1_m + TRUCK_HEAD_LEN_M)))
    for a, b in pairwise(blocks):
        zones.append(Zone(f"maintenance-lane-{a.id}-{b.id}", "maintenance_lane",
                          _rect(a.x1_m, a.y0_m, b.x0_m, a.y1_m)))

    ny0, nsplit, ny1 = (depth + d for d in NORTH_STRIP_M)
    zones.append(Zone("rail-interchange", "rail_interchange", _rect(yard_x0, ny0, yard_x1, nsplit)))
    zones.append(Zone("rail-terminal", "rail_terminal", _rect(yard_x0, nsplit, yard_x1, ny1)))
    ex0 = yard_x1 + EAST_STRIP_GAP_M
    if ex0 >= q:
        raise ValueError(f"no room east of the yard for gate/OOG zones (yard ends at {yard_x1} m)")
    gate_types = ("gate", "ocr_gate", "tech_gate", "customs", "problem_parking")
    gw = (q - ex0) / len(gate_types)
    for k, zt in enumerate(gate_types):
        zones.append(Zone(zt.replace("_", "-"), zt, _rect(ex0 + gw * k, ny0, ex0 + gw * (k + 1), ny1)))
    east = (("oog", "oog"), ("building-tob", "building"), ("building-workshop", "building"))
    eh = (depth - BLOCK_Y0_M) / len(east)
    for k, (zn, zt) in enumerate(east):
        zones.append(Zone(zn, zt, _rect(ex0, BLOCK_Y0_M + eh * k, q, BLOCK_Y0_M + eh * (k + 1))))

    t = TerminalLayout(
        name=name,
        quay=Quay(q, berths),
        sts_rails=Rails(RAIL_Y_WS_M, RAIL_Y_LS_M),
        sts=tuple(sts),
        apron=Apron(APRON_Y_FROM_M, APRON_Y_TO_M, drive_lanes, CROSSINGS_EVERY_M, tuple(buffers)),
        transfer_zones=tuple(transfer_zones),
        blocks=tuple(blocks),
        landside=tuple(landside),
        zones=tuple(zones),
        opcua=OpcUa(base_port=int(opcua_base_port)),
    )
    validate(t)
    return t


def to_yaml_dict(t: TerminalLayout) -> dict:
    """Plain, insertion-ordered dict (``version: 2``) that ``load_terminal`` reads back to ``t``."""
    f = float
    d = {
        "version": 2,
        "name": t.name,
        "quay": {
            "length_m": f(t.quay.length_m),
            "berths": [{"id": b.id, "from_m": f(b.from_m), "to_m": f(b.to_m)} for b in t.quay.berths],
        },
        "sts_rails": {"y_ws_m": f(t.sts_rails.y_ws_m), "y_ls_m": f(t.sts_rails.y_ls_m)},
        "sts": [
            {
                "id": s.id, "x0_m": f(s.x0_m), "min_gap_m": f(s.min_gap_m),
                "berth_ids": list(s.berth_ids),
                "lanes": [{"id": ln.id, "x_m": f(ln.x_m), "y_m": f(ln.y_m), "len_m": f(ln.len_m),
                           "width_m": f(ln.width_m)} for ln in s.lanes],
            }
            for s in t.sts
        ],
        "apron": {
            "y_from_m": f(t.apron.y_from_m),
            "y_to_m": f(t.apron.y_to_m),
            "drive_lanes": [{"id": d.id, "y_m": f(d.y_m), "direction": str(d.direction)}
                            for d in t.apron.drive_lanes],
            "crossings_every_m": f(t.apron.crossings_every_m),
            "buffers": [{"id": b.id, "x_m": f(b.x_m), "y_m": f(b.y_m), "sts_id": b.sts_id}
                        for b in t.apron.buffers],
        },
        "transfer_zones": [
            {
                "block": z.block,
                "approach_lane_id": z.approach_lane_id,
                "racks": [{"id": k.id, "x_m": f(k.x_m), "y_m": f(k.y_m), "size": str(k.size),
                           "reefer": bool(k.reefer)} for k in z.racks],
            }
            for z in t.transfer_zones
        ],
        "blocks": [
            {
                "id": b.id, "x0_m": f(b.x0_m), "y0_m": f(b.y0_m),
                "bays": int(b.bays), "rows": int(b.rows), "tiers": int(b.tiers),
                "asc_ws_id": b.asc_ws_id, "asc_ls_id": b.asc_ls_id,
                "bay_pitch_m": f(b.bay_pitch_m), "row_pitch_m": f(b.row_pitch_m),
                "tier_h_m": f(b.tier_h_m), "asc_min_gap_bays": int(b.asc_min_gap_bays),
                "maintenance_lane_side": str(b.maintenance_lane_side),
                "reefer_rows": [int(r) for r in b.reefer_rows],
            }
            for b in t.blocks
        ],
        "landside": [{"block": h.block, "lanes": int(h.lanes), "y_m": f(h.y_m), "len_m": f(h.len_m)}
                     for h in t.landside],
        "zones": [{"name": z.name, "type": z.type, "polygon": [[f(x), f(y)] for x, y in z.polygon]}
                  for z in t.zones],
        "opcua": {"base_port": int(t.opcua.base_port), "host": str(t.opcua.host)},
    }
    if t.kinematics:  # optional; omitted so layouts without overrides serialise unchanged
        d["kinematics"] = {k: {n: f(v) for n, v in p.items()} for k, p in t.kinematics.items()}
    return d
