from terminal_model.port import EquipmentPort


class _Full:
    def equipment_ids(self): return []
    def snapshot(self, equipment_id): raise KeyError(equipment_id)
    def assign_job(self, equipment_id, job): return False
    def cancel_job(self, equipment_id): return False
    def reset(self, equipment_id): return False
    def subscribe(self, callback): pass
    def now(self): return 0.0


class _NoNow:
    def equipment_ids(self): return []
    def snapshot(self, equipment_id): raise KeyError(equipment_id)
    def assign_job(self, equipment_id, job): return False
    def cancel_job(self, equipment_id): return False
    def reset(self, equipment_id): return False
    def subscribe(self, callback): pass


def test_port_is_runtime_checkable():
    assert isinstance(_Full(), EquipmentPort)
    assert not isinstance(_NoNow(), EquipmentPort)
