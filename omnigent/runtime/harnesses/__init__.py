"""Deprecated alias package for :mod:`omnigent.harnesses.runtime`; removed in 0.19.0."""

from __future__ import annotations

import importlib
from typing import Any


def __getattr__(name: str) -> Any:
    return getattr(importlib.import_module("omnigent.harnesses.runtime"), name)
