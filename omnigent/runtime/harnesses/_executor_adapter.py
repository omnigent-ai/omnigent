"""Deprecated alias for :mod:`omnigent.harnesses.runtime._executor_adapter`; removed in 0.19.0."""

import sys

from omnigent.harnesses.runtime import _executor_adapter as _target

sys.modules[__name__] = _target
