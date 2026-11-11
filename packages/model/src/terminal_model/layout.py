"""Metre-based terminal layout (v2): frozen dataclasses, YAML loader and validation.

Units are metres; origin = quay west corner, x along the quay, y landward. Independent of
the legacy grid loader in ``terminal_sim.layout`` (a v2 file carries ``version: 2``).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

ZONE_TYPES = frozenset({
    "berth", "agv_lanes", "agv_parking", "transfer_ws", "block", "truck_interchange",
    "rail_interchange", "rail_terminal", "gate", "ocr_gate", "tech_gate", "customs",
    "problem_parking", "oog", "building", "maintenance_lane",
})

DRIVE_DIRECTIONS = frozenset({"E", "W"})
RACK_SIZES = frozenset({"20", "40", "45"})
LANE_SIDES = frozenset({"E", "W"})

MIN_STS_GAP_M = 27.0
RACK_X_MARGIN_M = 10.0
_EPS = 1e-6


class LayoutError(ValueError):
    """Raised when a layout file is malformed or violates a layout rule."""


@dataclass(frozen=True)
class Berth:
    id: str
    from_m: float
    to_m: float


@dataclass(frozen=True)
class Quay:
    length_m: float
    berths: tuple[Berth, ...]


@dataclass(frozen=True)
class Rails:
    y_ws_m: float
    y_ls_m: float


@dataclass(frozen=True)
class Lane:
    id: str
    x_m: float
    y_m: float
    len_m: float
    width_m: float


@dataclass(frozen=True)
class StsSpec:
    id: str
    x0_m: float
    min_gap_m: float
    berth_ids: tuple[str, ...]
    lanes: tuple[Lane, ...]


@dataclass(frozen=True)
class DriveLane:
    id: str
    y_m: float
    direction: Literal["E", "W"]


@dataclass(frozen=True)
class Buffer:
    id: str
    x_m: float
    y_m: float
    sts_id: str


@dataclass(frozen=True)
class Apron:
    y_from_m: float
    y_to_m: float
    drive_lanes: tuple[DriveLane, ...]
    crossings_every_m: float
    buffers: tuple[Buffer, ...]


@dataclass(frozen=True)
class Rack:
    id: str
    x_m: float
    y_m: float
    size: Literal["20", "40", "45"]
    reefer: bool = False


@dataclass(frozen=True)
class TransferZone:
    block: str
    racks: tuple[Rack, ...]
    approach_lane_id: str


@dataclass(frozen=True)
class Block:
    id: str
    x0_m: float
    y0_m: float
    bays: int
    rows: int
    tiers: int
    asc_ws_id: str
    asc_ls_id: str
    bay_pitch_m: float = 6.5
    row_pitch_m: float = 2.9
    tier_h_m: float = 2.9
    asc_min_gap_bays: int = 2
    maintenance_lane_side: Literal["E", "W"] = "E"
    reefer_rows: tuple[int, ...] = ()

    @property
    def width_m(self) -> float:
        return self.rows * self.row_pitch_m

    @property
    def length_m(self) -> float:
        return self.bays * self.bay_pitch_m

    @property
    def x1_m(self) -> float:
        return self.x0_m + self.width_m

    @property
    def y1_m(self) -> float:
        return self.y0_m + self.length_m


@dataclass(frozen=True)
class TruckHead:
    block: str
    lanes: int
    y_m: float
    len_m: float = 0.0  # landward extent of the truck head (spec §3: y 525..560 -> 35)


@dataclass(frozen=True)
class Zone:
    name: str
    type: str
    polygon: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class OpcUa:
    base_port: int = 48400
    host: str = "localhost"


@dataclass(frozen=True)
class TerminalLayout:
    name: str
    quay: Quay
    sts_rails: Rails
    sts: tuple[StsSpec, ...]
    apron: Apron
    transfer_zones: tuple[TransferZone, ...]
    blocks: tuple[Block, ...]
    landside: tuple[TruckHead, ...]
    zones: tuple[Zone, ...]
    opcua: OpcUa

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)

    def transfer_zone(self, block_id: str) -> TransferZone:
        for z in self.transfer_zones:
            if z.block == block_id:
                return z
        raise KeyError(block_id)

    @property
    def depth_m(self) -> float:
        """Max y of the block ends or truck-head ends (landward extent of the terminal)."""
        return max([b.y1_m for b in self.blocks] + [h.y_m + h.len_m for h in self.landside],
                   default=0.0)


def is_v2(raw: dict) -> bool:
    return raw.get("version") == 2


# --- loading -------------------------------------------------------------------------------


def _build(raw: dict) -> TerminalLayout:
    q = raw["quay"]
    r = raw["sts_rails"]
    a = raw["apron"]
    return TerminalLayout(
        name=str(raw["name"]),
        quay=Quay(float(q["length_m"]), tuple(
            Berth(str(b["id"]), float(b["from_m"]), float(b["to_m"])) for b in q["berths"])),
        sts_rails=Rails(float(r["y_ws_m"]), float(r["y_ls_m"])),
        sts=tuple(
            StsSpec(
                id=str(s["id"]), x0_m=float(s["x0_m"]), min_gap_m=float(s["min_gap_m"]),
                berth_ids=tuple(str(x) for x in s.get("berth_ids", ())),
                lanes=tuple(
                    Lane(str(ln["id"]), float(ln["x_m"]), float(ln["y_m"]),
                         float(ln["len_m"]), float(ln["width_m"]))
                    for ln in s.get("lanes", ())),
            )
            for s in raw["sts"]
        ),
        apron=Apron(
            y_from_m=float(a["y_from_m"]), y_to_m=float(a["y_to_m"]),
            drive_lanes=tuple(
                DriveLane(str(d["id"]), float(d["y_m"]), d["direction"])
                for d in a.get("drive_lanes", ())),
            crossings_every_m=float(a.get("crossings_every_m", 0.0)),
            buffers=tuple(
                Buffer(str(b["id"]), float(b["x_m"]), float(b["y_m"]), str(b["sts_id"]))
                for b in a.get("buffers", ())),
        ),
        transfer_zones=tuple(
            TransferZone(
                block=str(z["block"]), approach_lane_id=str(z["approach_lane_id"]),
                racks=tuple(
                    Rack(str(k["id"]), float(k["x_m"]), float(k["y_m"]), str(k["size"]),
                         bool(k.get("reefer", False)))
                    for k in z.get("racks", ())),
            )
            for z in raw["transfer_zones"]
        ),
        blocks=tuple(
            Block(
                id=str(b["id"]), x0_m=float(b["x0_m"]), y0_m=float(b["y0_m"]),
                bays=int(b["bays"]), rows=int(b["rows"]), tiers=int(b["tiers"]),
                asc_ws_id=str(b["asc_ws_id"]), asc_ls_id=str(b["asc_ls_id"]),
                **{k: float(b[k]) for k in ("bay_pitch_m", "row_pitch_m", "tier_h_m") if k in b},
                **({"asc_min_gap_bays": int(b["asc_min_gap_bays"])} if "asc_min_gap_bays" in b else {}),
                maintenance_lane_side=b.get("maintenance_lane_side", "E"),
                reefer_rows=tuple(int(x) for x in b.get("reefer_rows", ())),
            )
            for b in raw["blocks"]
        ),
        landside=tuple(
            TruckHead(str(h["block"]), int(h["lanes"]), float(h["y_m"]),
                      float(h.get("len_m", 0.0)))
            for h in raw.get("landside") or ()),
        zones=tuple(
            Zone(str(z["name"]), str(z["type"]),
                 tuple((float(p[0]), float(p[1])) for p in z["polygon"]))
            for z in raw.get("zones") or ()),
        opcua=OpcUa(**{k: v for k, v in (raw.get("opcua") or {}).items()
                       if k in ("base_port", "host")}),
    )


def load_terminal(path: Path) -> TerminalLayout:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or not is_v2(raw):
        raise LayoutError(f"{path}: not a v2 terminal layout (missing 'version: 2')")
    try:
        t = _build(raw)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise LayoutError(f"{path}: malformed layout: {type(e).__name__}: {e}") from e
    validate(t)
    return t


# --- validation ----------------------------------------------------------------------------


def _dupes(kind: str, ids: list[str], errs: list[str]) -> None:
    for i, n in Counter(ids).items():
        if n > 1:
            errs.append(f"duplicate {kind} id '{i}' ({n} times)")


def validate(t: TerminalLayout) -> None:
    errs: list[str] = []

    # unique ids
    _dupes("STS", [s.id for s in t.sts], errs)
    _dupes("lane", [ln.id for s in t.sts for ln in s.lanes], errs)
    _dupes("rack", [k.id for z in t.transfer_zones for k in z.racks], errs)
    _dupes("block", [b.id for b in t.blocks], errs)
    _dupes("ASC", [i for b in t.blocks for i in (b.asc_ws_id, b.asc_ls_id)], errs)
    _dupes("buffer", [b.id for b in t.apron.buffers], errs)
    _dupes("zone", [z.name for z in t.zones], errs)

    # berths cover [0, quay length] without gaps/overlaps
    berths = sorted(t.quay.berths, key=lambda b: b.from_m)
    if not berths:
        errs.append("quay has no berths")
    else:
        pos = 0.0
        for b in berths:
            if b.to_m <= b.from_m:
                errs.append(f"berth '{b.id}' has non-positive length ({b.from_m}..{b.to_m})")
            if b.from_m > pos + _EPS:
                errs.append(f"berth gap between {pos} m and {b.from_m} m (before berth '{b.id}')")
            elif b.from_m < pos - _EPS:
                errs.append(f"berth '{b.id}' overlaps previous berth at {b.from_m} m")
            pos = max(pos, b.to_m)
        if abs(pos - t.quay.length_m) > _EPS:
            errs.append(f"berths end at {pos} m but quay length is {t.quay.length_m} m")
    berth_ids = {b.id for b in t.quay.berths}

    # STS
    for y_name, y in (("y_ws_m", t.sts_rails.y_ws_m), ("y_ls_m", t.sts_rails.y_ls_m)):
        if not 0 <= y <= t.apron.y_from_m:
            errs.append(f"STS rail {y_name}={y} outside [0, {t.apron.y_from_m}]")
    for s in t.sts:
        if not s.lanes:
            errs.append(f"STS '{s.id}' has no transfer lane")
        if s.min_gap_m < MIN_STS_GAP_M:
            errs.append(f"STS '{s.id}' min_gap_m {s.min_gap_m} < {MIN_STS_GAP_M}")
        for bid in s.berth_ids:
            if bid not in berth_ids:
                errs.append(f"STS '{s.id}' references unknown berth '{bid}'")
    sts_ids = {s.id for s in t.sts}
    for d in t.apron.drive_lanes:
        if d.direction not in DRIVE_DIRECTIONS:
            errs.append(f"drive lane '{d.id}' direction '{d.direction}' not in E/W")
    for buf in t.apron.buffers:
        if buf.sts_id not in sts_ids:
            errs.append(f"buffer '{buf.id}' references unknown STS '{buf.sts_id}'")

    # blocks
    for b in t.blocks:
        if b.maintenance_lane_side not in LANE_SIDES:
            errs.append(f"block '{b.id}' maintenance_lane_side '{b.maintenance_lane_side}' not in E/W")
        if b.asc_min_gap_bays < 1:
            errs.append(f"block '{b.id}' asc_min_gap_bays {b.asc_min_gap_bays} < 1")
        for r in b.reefer_rows:
            if not 0 <= r <= b.rows - 1:
                errs.append(f"block '{b.id}' reefer row {r} outside 0..{b.rows - 1}")
    for i, a in enumerate(t.blocks):
        for b in t.blocks[i + 1:]:
            if (a.x0_m < b.x1_m and b.x0_m < a.x1_m and a.y0_m < b.y1_m and b.y0_m < a.y1_m):
                errs.append(f"blocks '{a.id}' and '{b.id}' overlap")

    # transfer zones <-> blocks, racks
    block_ids = {b.id for b in t.blocks}
    zone_blocks = [z.block for z in t.transfer_zones]
    _dupes("transfer zone", zone_blocks, errs)
    for bid in sorted(block_ids - set(zone_blocks)):
        errs.append(f"block '{bid}' has no transfer zone")
    lane_ids = {d.id for d in t.apron.drive_lanes}
    for z in t.transfer_zones:
        if z.block not in block_ids:
            errs.append(f"transfer zone references unknown block '{z.block}'")
            continue
        if z.approach_lane_id not in lane_ids:
            errs.append(f"transfer zone '{z.block}' approach lane '{z.approach_lane_id}' unknown")
        b = t.block(z.block)
        for k in z.racks:
            if k.size not in RACK_SIZES:
                errs.append(f"rack '{k.id}' size '{k.size}' not in 20/40/45")
            # The zone lies between the STS rail area and the block head. (The brief names
            # apron.y_to_m as the lower bound, but the demo apron ends at the block head
            # (y 165) while the racks sit at y 110..160, so the apron start is used.)
            if not t.apron.y_from_m <= k.y_m < b.y0_m:
                errs.append(f"rack '{k.id}' y={k.y_m} outside zone of block '{b.id}' "
                            f"[{t.apron.y_from_m}, {b.y0_m})")
            if not b.x0_m - RACK_X_MARGIN_M <= k.x_m <= b.x1_m + RACK_X_MARGIN_M:
                errs.append(f"rack '{k.id}' x={k.x_m} outside block '{b.id}' x-range "
                            f"[{b.x0_m}, {b.x1_m}] +/- {RACK_X_MARGIN_M}")

    for h in t.landside:
        if h.block not in block_ids:
            errs.append(f"truck head references unknown block '{h.block}'")

    # zones
    for z in t.zones:
        if z.type not in ZONE_TYPES:
            errs.append(f"zone '{z.name}' has unknown zone type '{z.type}'")

    if errs:
        raise LayoutError("\n".join(errs))
