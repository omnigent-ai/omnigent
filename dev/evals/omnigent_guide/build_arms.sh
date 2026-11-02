#!/usr/bin/env bash
# Build one non-editable venv per eval arm from a wheel built from an sdist
# (the release path), and write OUT/arms.json for run.py.
#
#   dev/evals/omnigent_guide/build_arms.sh OUT BASE HEAD
#
# BASE is main before the change, HEAD the branch with it. Arms:
#   a0  BASE                      no framework skills ship
#   a1  BASE + package-data line  packaging fix only (stale omnigent-knowledge)
#   a2  HEAD without the load_skill summaries
#   a3  HEAD
#   a4  HEAD, agent shell without network (same venv as a3)
#   t0  v0.18.0          t3  v0.18.0 + HEAD's commits since BASE
#   s0  v0.17.0          s3  v0.17.0 + HEAD's commits since BASE
set -euo pipefail
OUT=$(cd "$1" && pwd)
BASE=$2
HEAD_REF=$3
REPO=$(git rev-parse --show-toplevel)
mkdir -p "$OUT/src" "$OUT/dist" "$OUT/venv"

tree() { # name ref -> path of a detached worktree at ref
  local dir="$OUT/src/$1"
  [ -d "$dir" ] || git -C "$REPO" worktree add --detach "$dir" "$2" >/dev/null
  echo "$dir"
}
picks=$(git -C "$REPO" rev-list --reverse --no-merges "$BASE..$HEAD_REF")

tree a0 "$BASE" >/dev/null
a1=$(tree a1 "$BASE")
if ! grep -q '"omnigent.onboarding"' "$a1/pyproject.toml"; then
  python3 - "$a1/pyproject.toml" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
anchor = '"omnigent.db.migrations" = ["script.py.mako"]\n'
text = path.read_text()
assert text.count(anchor) == 1, "package-data anchor moved"
path.write_text(text.replace(anchor, anchor + '"omnigent.onboarding" = ["agent/skills/**/*"]\n'))
PY
  git -C "$a1" commit -qam "eval arm a1: packaging fix only"
fi
a2=$(tree a2 "$HEAD_REF")
if ! git -C "$a2" diff --quiet "$BASE" -- omnigent/tools/builtins/load_skill.py; then
  git -C "$a2" checkout -q "$BASE" -- omnigent/tools/builtins/load_skill.py tests/tools/builtins/test_load_skill.py
  git -C "$a2" commit -qm "eval arm a2: guide without load_skill summaries"
fi
tree a3 "$HEAD_REF" >/dev/null
for tag in t:v0.18.0 s:v0.17.0; do
  prefix=${tag%%:*} ref=${tag#*:}
  tree "${prefix}0" "$ref" >/dev/null
  dir=$(tree "${prefix}3" "$ref")
  [ -f "$dir/omnigent/spec/framework_skills.py" ] || git -C "$dir" cherry-pick $picks >/dev/null
done

for arm in a0 a1 a2 a3 t0 t3 s0 s3; do
  src="$OUT/src/$arm"
  if [ ! -x "$OUT/venv/$arm/bin/omnigent" ]; then
    (cd "$src" && OMNIGENT_SKIP_WEB_UI=true uv build -q --out-dir "$OUT/dist/$arm")
    uv venv -q --python 3.12 "$OUT/venv/$arm"
    # --no-sources: the SDKs' path sources would otherwise reinstall omnigent editable.
    uv pip install -q --no-sources --python "$OUT/venv/$arm/bin/python" \
      "$OUT/dist/$arm"/omnigent-*.whl "$src/sdks/python-client" "$src/sdks/ui"
  fi
done

python3 - "$OUT" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
arms = {n: {"python": f"venv/{n}/bin/python", "source": f"src/{n}"} for n in ("a0", "a1", "a2", "a3", "t0", "t3", "s0", "s3")}
arms["a4"] = {"python": "venv/a3/bin/python", "source": "src/a3", "network": False}
(out / "arms.json").write_text(json.dumps(arms, indent=2) + "\n")
print(f"wrote {out / 'arms.json'}")
PY
