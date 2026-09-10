#!/usr/bin/env bash

set -euo pipefail

audit_dir=$(mktemp -d)
trap 'rm -rf "$audit_dir"' EXIT

# Export both compatible resolution sets; the antigravity extra conflicts with
# protobuf constraints in the default set. Editable workspace packages are not
# third-party dependencies and cannot be consumed by pip-audit.
uv export --frozen --format requirements-txt \
  --all-extras --no-extra antigravity --all-groups > "$audit_dir/full.txt"
grep -v '^-e ' "$audit_dir/full.txt" > "$audit_dir/audit.txt"

uv export --frozen --format requirements-txt \
  --extra antigravity --extra all --no-default-groups > "$audit_dir/antigravity-full.txt"
grep -v '^-e ' "$audit_dir/antigravity-full.txt" > "$audit_dir/antigravity-audit.txt"

uvx pip-audit --requirement "$audit_dir/audit.txt" \
  --no-deps --disable-pip --vulnerability-service osv
uvx pip-audit --requirement "$audit_dir/antigravity-audit.txt" \
  --no-deps --disable-pip --vulnerability-service osv
