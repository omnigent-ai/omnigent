#!/usr/bin/env bash
# Live contract probe for the AIGW intelligent-routing API (L3 in
# designs/INTELLIGENT_ROUTING_PLAN.md §6).
#
# Asserts the facts the Omnigent routing client depends on: task_v1 infers a
# scenario from which model arms are offered, each scenario requires its full
# fixed menu, extra non-arm models are tolerated, and an unknown router name
# enumerates the registered routers (so a task_v2 landing is loud).
#
# Not commit-gating — run before demos and whenever AIGW deploys.
#
# Usage:
#   scripts/probe_routing_api.sh                 # eng-ml-inference staging
#   ROUTING_PROFILE=my-ws ROUTING_BASE_URL=https://... scripts/probe_routing_api.sh
#
# Requires: bash, curl, python3, databricks CLI (authenticated profile).

set -uo pipefail

PROFILE="${ROUTING_PROFILE:-eng-ml-inference}"
BASE_URL="${ROUTING_BASE_URL:-https://eng-ml-inference.staging.cloud.databricks.com}"
URL="${BASE_URL%/}/ai-gateway/routing/v1/routes:select"
ROUTER="${ROUTING_ROUTER_NAME:-task_v1}"

CLAUDE_ARMS='{"model":"claude-opus-4-8","harness":"claude-sdk"},{"model":"claude-sonnet-5","harness":"claude-sdk"}'
CODEX_ARMS='{"model":"glm-5-2","harness":"codex"},{"model":"gpt-5-6-sol","harness":"codex"},{"model":"gpt-5-6-luna","harness":"codex"}'
CLAUDE_MODELS="claude-opus-4-8 claude-sonnet-5"
CODEX_MODELS="glm-5-2 gpt-5-6-sol gpt-5-6-luna"

PROMPT='Rename the retry_count variable to attempt_count in one file.'

pass_count=0
fail_count=0

die() {
  echo "FATAL: $*" >&2
  exit 2
}

command -v curl >/dev/null || die "curl not found"
command -v python3 >/dev/null || die "python3 not found"
command -v databricks >/dev/null || die "databricks CLI not found"

echo "probe_routing_api: profile=$PROFILE router=$ROUTER"
echo "                   url=$URL"
echo

TOKEN_JSON="$(databricks auth token -p "$PROFILE" -o json 2>&1)" || die \
  "databricks auth token -p $PROFILE failed: $TOKEN_JSON"
TOKEN="$(printf '%s' "$TOKEN_JSON" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("access_token", ""))
except Exception:
    print("")
')"
[ -n "$TOKEN" ] || die "could not extract access_token for profile $PROFILE"

# post <body> -> sets RESP_BODY / RESP_CODE
post() {
  local body="$1" raw
  raw="$(curl -sS -m 60 -w $'\n__HTTP__%{http_code}' -X POST "$URL" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>&1)"
  RESP_CODE="${raw##*__HTTP__}"
  RESP_BODY="${raw%$'\n'__HTTP__*}"
  case "$RESP_CODE" in
    [0-9][0-9][0-9]) ;;
    *) RESP_CODE="000"; RESP_BODY="$raw" ;;
  esac
}

body_for() {
  # body_for <route_options_json> [router_name]
  local options="$1" router="${2:-$ROUTER}"
  python3 - "$options" "$router" "$PROMPT" <<'PY'
import json, sys
options, router, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "route_options": json.loads("[" + options + "]"),
    "task": {"prompt": prompt},
    "route_selector": {"router_name": router},
}))
PY
}

CHECKER="$(mktemp -t probe_routing_check.XXXXXX.py)"
trap 'rm -f "$CHECKER"' EXIT
cat >"$CHECKER" <<'PY'
import json, sys

code, mode, args = sys.argv[1], sys.argv[2], sys.argv[3:]
raw = sys.stdin.read()
try:
    doc = json.loads(raw)
except Exception:
    print(f"response is not JSON (HTTP {code})")
    sys.exit(1)


def selection():
    sel = doc.get("route_selection") or []
    if not sel:
        raise AssertionError("route_selection is empty")
    opt = sel[0].get("route_option") or {}
    return opt.get("model"), opt.get("harness")


try:
    if mode == "routes_from":
        assert code == "200", f"expected HTTP 200, got {code}"
        model, harness = selection()
        allowed = args
        assert model in allowed, f"picked {model!r}, expected one of {allowed}"
        assert doc.get("rationale"), "rationale missing"
        print(f"picked model={model} harness={harness}")
    elif mode == "missing_arms":
        assert code == "400", f"expected HTTP 400, got {code}"
        msg = doc.get("message") or ""
        assert "full menu" in msg, f"message does not mention the full menu: {msg!r}"
        for arm in args:
            assert arm in msg, f"message does not name missing arm {arm!r}: {msg!r}"
        print(msg)
    elif mode == "unknown_router":
        assert code == "400", f"expected HTTP 400, got {code}"
        msg = doc.get("message") or ""
        assert "unknown router" in msg.lower(), f"unexpected message: {msg!r}"
        for router in args:
            assert router in msg, f"known-routers list omits {router!r}: {msg!r}"
        print(msg)
    else:
        raise AssertionError(f"unknown check mode {mode!r}")
except AssertionError as exc:
    print(str(exc))
    sys.exit(1)
sys.exit(0)
PY

# check <mode> [args...] — reads RESP_BODY on stdin, prints a reason, exits 0/1
check() {
  printf '%s' "$RESP_BODY" | python3 "$CHECKER" "$RESP_CODE" "$@"
}

# case <label> <route_options|--router:NAME options> <check mode + args...>
run_case() {
  local label="$1" options="$2" router="$3"
  shift 3
  local body
  body="$(body_for "$options" "$router")"
  post "$body"
  local detail rc
  detail="$(check "$@")"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    pass_count=$((pass_count + 1))
    echo "PASS  $label"
  else
    fail_count=$((fail_count + 1))
    echo "FAIL  $label"
  fi
  echo "      HTTP $RESP_CODE :: $detail"
  echo "      resp: $(printf '%s' "$RESP_BODY" | head -c 300)"
  echo
}

# (a) full five-arm menu (scenario "both") routes.
run_case "a) task_v1 full 5-arm menu routes" \
  "$CLAUDE_ARMS,$CODEX_ARMS" "$ROUTER" \
  routes_from $CLAUDE_MODELS $CODEX_MODELS

# (b) codex arms only -> a codex arm (harness constraint via the offered menu).
run_case "b) codex-arms-only routes to a codex arm" \
  "$CODEX_ARMS" "$ROUTER" \
  routes_from $CODEX_MODELS

# (c) claude arms only -> a claude arm.
run_case "c) claude-arms-only routes to a claude arm" \
  "$CLAUDE_ARMS" "$ROUTER" \
  routes_from $CLAUDE_MODELS

# (d) partial menu -> 400 naming the missing arms.
run_case "d) partial menu 400s naming missing arms" \
  '{"model":"claude-opus-4-8","harness":"claude-sdk"}' "$ROUTER" \
  missing_arms claude-sonnet-5

# (e) full menu + extra non-arm models is tolerated (catalog superset).
run_case "e) menu + extra non-arm models still routes" \
  "$CLAUDE_ARMS,$CODEX_ARMS,{\"model\":\"gpt-5-5\",\"harness\":\"codex\"},{\"model\":\"claude-haiku-4-5\",\"harness\":\"claude-sdk\"}" \
  "$ROUTER" \
  routes_from $CLAUDE_MODELS $CODEX_MODELS

# (f) unknown router name enumerates the known routers — catches a task_v2
#     landing or task_v1 being retired.
run_case "f) unknown router 400 enumerates known routers (task_v1 present)" \
  "$CLAUDE_ARMS,$CODEX_ARMS" "omnigent_probe_no_such_router" \
  unknown_router task_v1

echo "----"
echo "probe_routing_api: $pass_count passed, $fail_count failed"
[ "$fail_count" -eq 0 ] || exit 1
