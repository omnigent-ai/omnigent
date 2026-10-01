"""Deprecated alias for :mod:`omnigent.harnesses.registry`; removed in 0.19.0."""

import sys

from omnigent.harnesses import registry as _target

sys.modules[__name__] = _target
