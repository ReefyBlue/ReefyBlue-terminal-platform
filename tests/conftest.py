import sys
from pathlib import Path

# share the in-memory FakePort helper between the ECS, sim and runner tests
sys.path.insert(0, str(Path(__file__).resolve().parent / "ecs"))
