"""Minimal Step stand-in so scale.py's recipe checks can be tested with no Qt."""
from dataclasses import dataclass


@dataclass
class Step:
    name: str
    pct: float
    target: float
    actual: float = None
    skipped: bool = False
