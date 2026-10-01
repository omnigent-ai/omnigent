"""Deprecated alias for :mod:`omnigent.harnesses.install_spec`; removed in 0.19.0."""

import sys

from omnigent.harnesses import install_spec as _target

sys.modules[__name__] = _target
