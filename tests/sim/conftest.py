import sys
from pathlib import Path

# share the in-memory FakePort helper with the ECS tests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ecs"))
