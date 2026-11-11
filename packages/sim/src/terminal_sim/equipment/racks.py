"""Transfer racks (one container each) per block transfer zone (pure, no salabim)."""
from __future__ import annotations

from collections.abc import Sequence

from terminal_model.layout import Rack, TransferZone


class RackError(ValueError):
    """Illegal rack operation (occupied, empty, unknown rack/block)."""


class Racks:
    def __init__(self, zones: Sequence[TransferZone]) -> None:
        self._zones: dict[str, TransferZone] = {z.block: z for z in zones}
        self._rack: dict[str, Rack] = {r.id: r for z in zones for r in z.racks}
        self._occ: dict[str, str] = {}

    def _get(self, rack_id: str) -> Rack:
        try:
            return self._rack[rack_id]
        except KeyError:
            raise RackError(f"unknown rack {rack_id!r}") from None

    def free_rack(self, block: str, *, reefer: bool = False) -> Rack | None:
        """First free rack in zone order; reefer boxes only reefer racks, others never."""
        zone = self._zones.get(block)
        if zone is None:
            raise RackError(f"no transfer zone for block {block!r}")
        for r in zone.racks:
            if r.reefer == reefer and r.id not in self._occ:
                return r
        return None

    def put(self, rack_id: str, cid: str) -> None:
        self._get(rack_id)
        if rack_id in self._occ:
            raise RackError(f"rack {rack_id} is occupied by {self._occ[rack_id]}")
        self._occ[rack_id] = cid

    def take(self, rack_id: str) -> str:
        self._get(rack_id)
        if rack_id not in self._occ:
            raise RackError(f"rack {rack_id} is empty")
        return self._occ.pop(rack_id)

    def occupant(self, rack_id: str) -> str | None:
        self._get(rack_id)
        return self._occ.get(rack_id)

    def occupancy(self, block: str) -> tuple[int, int]:
        zone = self._zones.get(block)
        if zone is None:
            raise RackError(f"no transfer zone for block {block!r}")
        return sum(r.id in self._occ for r in zone.racks), len(zone.racks)
