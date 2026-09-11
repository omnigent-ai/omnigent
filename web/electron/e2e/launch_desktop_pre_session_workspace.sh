#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
demo_root="/private/tmp/omnigent-prechat-manual-demo"
state_file="$demo_root/processes.env"
python="$repo_root/.venv/bin/python"
omnidev="$repo_root/dev/omnidev/target/release/omnidev"
electron="$(cd "$repo_root/web/electron" && node -p "require('electron')")"
demo_model="$(PYTHONPATH="$repo_root" "$python" -c 'from omnigent.onboarding.providers import default_chat_model; model = default_chat_model("openai"); assert model; print(model)')"

if [[ -f "$state_file" ]]; then
  echo "Demo already has a process file. Run stop_desktop_pre_session_workspace.sh first." >&2
  exit 1
fi

free_port() {
  "$python" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

server_port="$(free_port)"
mock_port="$(free_port)"
page_port="$(free_port)"
mkdir -p "$demo_root" "$demo_root/seed-config" "$demo_root/gh-config" "$demo_root/page" "$demo_root/electron-profile"

fixture_repo="$demo_root/workspace-fixture"
if [[ ! -d "$fixture_repo/.git" ]]; then
  mkdir -p "$fixture_repo/src"
  printf '# Desktop workspace demo\n' > "$fixture_repo/README.md"
  git -C "$fixture_repo" init -b main
  git -C "$fixture_repo" config user.name 'Desktop Demo'
  git -C "$fixture_repo" config user.email 'desktop-demo@example.invalid'
  git -C "$fixture_repo" add README.md
  git -C "$fixture_repo" commit -m 'Initial demo fixture'
  git -C "$fixture_repo" remote add origin https://github.com/omnigent-ai/omnigent.git
fi
printf 'local change for the workspace panel\n' > "$fixture_repo/src/draft.txt"

cat > "$demo_root/page/index.html" <<'HTML'
<!doctype html><title>Workspace demo</title>
<main style="font:24px system-ui;padding:48px"><h1>Local workspace preview</h1><p>Opened safely before Start.</p></main>
HTML

cat > "$demo_root/hello_world.yaml" <<YAML
name: hello_world
prompt: You are a deterministic local demo assistant.
executor:
  model: $demo_model
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: http://127.0.0.1:$mock_port/v1
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
YAML

cat > "$demo_root/seed-config/config.yaml" <<YAML
auth:
  type: none
providers:
  desktop-demo-mock:
    kind: key
    default: [openai]
    openai:
      base_url: http://127.0.0.1:$mock_port/v1
      api_key: mock-key
      wire_api: responses
      models:
        default: $demo_model
YAML

cat > "$demo_root/host.py" <<'PY'
import omnigent.host.connect as connect
connect.configured_harness_map = lambda: {"openai-agents": True}
connect.gateway_inference_map = lambda: {}
from omnigent.host._daemon_entry import main
main()
PY

cat > "$demo_root/profile.toml" <<TOML
backend_dir = "omnigent"
web_dir = "web"
[server]
command = ["$repo_root/.venv/bin/omnigent", "--log-to-stderr", "server", "--host", "127.0.0.1", "--port", "{server_port}", "--database-uri", "sqlite:///{pod_dir}/data/omnigent/chat.db", "--artifact-location", "{pod_dir}/artifacts", "--agent", "$demo_root/hello_world.yaml"]
[host]
command = ["$python", "$demo_root/host.py", "--server", "http://127.0.0.1:{server_port}"]
[vite]
command = ["/usr/bin/true"]
TOML

cat > "$demo_root/omnidev.exp" <<EXP
log_user 1
set timeout -1
spawn -noecho {$omnidev} --profile {$demo_root/profile.toml} --pod-dir {$demo_root/pod} --clean --no-vite --server-port {$server_port}
trap {send -- "q"; expect eof; exit} SIGTERM
expect eof
EXP

nohup env -u GH_TOKEN -u GITHUB_TOKEN -u ANTHROPIC_API_KEY -u CLAUDE_API_KEY -u CURSOR_API_KEY \
  OMNIGENT_CONFIG_HOME="$demo_root/seed-config" OMNIGENT_DATA_DIR="$demo_root/supervisor-data" \
  OMNIGENT_DISABLE_KEYRING=1 PYTHONPATH="$repo_root" \
  "$python" "$repo_root/tests/server/integration/mock_llm_server.py" "$mock_port" \
  > "$demo_root/mock.log" 2>&1 < /dev/null &
mock_pid=$!
nohup "$python" -m http.server "$page_port" --bind 127.0.0.1 --directory "$demo_root/page" \
  > "$demo_root/page.log" 2>&1 < /dev/null &
page_pid=$!
nohup env -u GH_TOKEN -u GITHUB_TOKEN -u ANTHROPIC_API_KEY -u CLAUDE_API_KEY -u CURSOR_API_KEY \
  OMNIGENT_CONFIG_HOME="$demo_root/seed-config" OMNIGENT_DATA_DIR="$demo_root/supervisor-data" \
  OMNIGENT_DISABLE_KEYRING=1 OMNIGENT_NO_UPDATE_CHECK=1 GH_CONFIG_DIR="$demo_root/gh-config" \
  OPENAI_BASE_URL="http://127.0.0.1:$mock_port/v1" OPENAI_API_KEY=mock-key \
  /usr/bin/expect "$demo_root/omnidev.exp" > "$demo_root/omnidev.log" 2>&1 < /dev/null &
omnidev_pid=$!

for _ in {1..120}; do
  if curl -fsS "http://127.0.0.1:$server_port/health" >/dev/null 2>&1 && \
    curl -fsS "http://127.0.0.1:$server_port/v1/hosts" | grep -q '"online"'; then
    break
  fi
  sleep 0.25
done
curl -fsS -X POST "http://127.0.0.1:$mock_port/mock/set_fallback" \
  -H 'content-type: application/json' \
  --data-binary "$(printf '{\"key\":\"%s\",\"text\":\"DESKTOP_HANDOFF_OK\"}' "$demo_model")" >/dev/null
printf '{"server_url":"http://127.0.0.1:%s"}\n' "$server_port" > "$demo_root/electron-profile/settings.json"
nohup env OMNIGENT_DISABLE_KEYRING=1 OMNIGENT_DESKTOP_VERSION_OVERRIDE=999.0.0 \
  "$electron" "$repo_root/web/electron" --user-data-dir="$demo_root/electron-profile" \
  > "$demo_root/electron.log" 2>&1 < /dev/null &
electron_pid=$!

cat > "$state_file" <<STATE
mock_pid=$mock_pid
page_pid=$page_pid
omnidev_pid=$omnidev_pid
electron_pid=$electron_pid
STATE

echo "Electron demo launched"
echo "Server: http://127.0.0.1:$server_port"
echo "Workspace: $fixture_repo"
echo "Browser demo: http://127.0.0.1:$page_port/"
echo "Model: deterministic local test fixture (DESKTOP_HANDOFF_OK)"
echo "Stop: $repo_root/web/electron/e2e/stop_desktop_pre_session_workspace.sh"
echo "The launcher stays attached while the isolated pod is running."
wait "$omnidev_pid"
