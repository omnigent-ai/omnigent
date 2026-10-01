"""Deprecated alias for :mod:`omnigent.core.loader`; removed in 0.19.0."""

import sys

from omnigent.core import loader as _target

sys.modules[__name__] = _target
