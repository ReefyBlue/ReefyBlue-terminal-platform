"""Static browser viewer for the terminal; served by the ECS service at /ui."""

from pathlib import Path

__all__ = ["static_dir"]


def static_dir() -> Path:
    return Path(__file__).parent / "static"
