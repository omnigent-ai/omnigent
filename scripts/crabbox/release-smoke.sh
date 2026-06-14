#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"
prepare_backend
ensure_node_toolchain

release_dir="$CRABBOX_ARTIFACT_DIR/release-smoke"
rm -rf dist "$release_dir"
mkdir -p "$release_dir"

log "building web UI"
rm -rf omnigent/server/static/web-ui
npm --prefix ap-web ci
npm --prefix ap-web run build

log "building Python distributions"
uv build --out-dir dist
uv build sdks/python-client --out-dir dist
uv build sdks/ui --out-dir dist

log "checking distribution metadata"
uvx twine check dist/*

log "asserting web UI is bundled in the core wheel"
uv run python - <<'PY'
import glob
import sys
import zipfile

whls = sorted(glob.glob("dist/omnigent-*.whl"))
if not whls:
    sys.exit("no core omnigent wheel found in dist/")
whl = whls[-1]
names = zipfile.ZipFile(whl).namelist()
ui = [n for n in names if "server/static/web-ui/" in n]
has_index = any(n.endswith("server/static/web-ui/index.html") for n in ui)
print(f"{whl}: {len(ui)} web-ui files, index.html present = {has_index}")
sys.exit(0 if (ui and has_index) else "WEB-UI BUNDLE MISSING FROM WHEEL")
PY

log "smoke-installing built wheels"
uv venv --python 3.12 /tmp/omnigent-crabbox-smoke
uv pip install --python /tmp/omnigent-crabbox-smoke/bin/python dist/*.whl
/tmp/omnigent-crabbox-smoke/bin/omnigent --version | tee "$release_dir/omnigent-version.txt"
