"""Native launch permission vocabulary shared with the web UI."""

import json
from importlib.resources import files

# Keep CLI spellings, labels, and launch arguments together in the shared catalog.
PERMISSION_LAUNCH_ARGS: dict[str, dict[str, list[str]]] = {
    harness: {mode["value"]: mode["args"] for mode in modes}
    for harness, modes in json.loads(
        files("omnigent.native").joinpath("permission_modes.json").read_text(encoding="utf-8")
    ).items()
}
