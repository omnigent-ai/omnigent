"""Script stub for terminals created before the move; removed in 0.19.0.

A terminal's ``pbcopy`` shim runs this file by path, and terminals can outlive
an upgrade. The helper now lives in :mod:`omnigent.terminals.clipboard`.
"""

import runpy

if __name__ == "__main__":
    runpy.run_module("omnigent.terminals.clipboard", run_name="__main__", alter_sys=True)
