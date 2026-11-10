import sys
from pathlib import Path

# share the in-memory FakePort helper between the ECS, sim and runner tests
sys.path.insert(0, str(Path(__file__).resolve().parent / "ecs"))


def pytest_sessionfinish(session, exitstatus):
    """Skip pytest's exit-time gc passes over the leftover asyncua address spaces.

    Every asyncua Server builds the standard address space (~1M objects); the OPC UA
    tests leave ~10-17M of those reachable at exit. pytest's unraisable-exception
    cleanup then runs gc.collect() five times (~3 s each), a 12-16 s exit delay.
    Freezing moves all live objects to the permanent generation so those passes are
    cheap; the process frees them on exit anyway."""
    import gc

    gc.collect()
    gc.freeze()
