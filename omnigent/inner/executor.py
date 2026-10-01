"""Deprecated alias for :mod:`omnigent.core.executor`; removed in 0.19.0."""

import sys

from omnigent.core import executor as _target

sys.modules[__name__] = _target
