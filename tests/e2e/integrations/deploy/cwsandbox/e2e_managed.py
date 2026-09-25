#!/usr/bin/env python3
"""
End-to-end test: create a managed session and wait for an assistant reply
from the child sandbox the Omnigent server provisions.

Two modes:

  1. --server <url>: use an EXISTING omnigent server already configured with
     sandbox.provider=cwsandbox and the desired serverless or CKS placement.
     Uses OMNIGENT_API_TOKEN or credentials saved by `omnigent login <url>`.
     Register an openai-agents probe as described in deploy/cwsandbox/README.md.

         python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
             --server http://my-omnigent:6767

  2. --image <image>: spin the server up inside a serverless sandbox with an HTTPS service.
     sandbox runs a prebaked image with this checkout's Omnigent + the cwsandbox SDK
     (build/push first; pass --image), and the driver injects the LLM creds.
     This mode seeds a W&B inference agent. See the README's image build command.

         export WANDB_API_KEY=...        # provisions the sandboxes
         export WANDB_INFERENCE_KEY=...      # the agent's LLM credential
         python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
             --image docker.io/<you>/omnigent-cwsandbox:test
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from urllib.parse import urlparse

import httpx
from cwsandbox import AuthStrategy, EgressRule, Endpoint, NetworkOptions, Sandbox, Service

SERVER_PORT = 6767
CONFIG_HOME = "/root/.omnigent"
WANDB_BASE_URL = "https://api.inference.wandb.ai/v1"
WANDB_MODEL = "Qwen/Qwen3-Coder-480B-A35B-Instruct"
CLIENT = httpx.Client()

PROMPT = "What is 2+2? Reply with ONLY the number, nothing else."


def _child_env(wandb_key: str) -> dict[str, str]:
    """Env injected into every managed CHILD sandbox — the single source of truth.

    The launcher forwards these by NAME from the server process env. OPENAI_*
    reach the harness automatically; the HARNESS_* knobs ride
    OMNIGENT_RUNNER_ENV_PASSTHROUGH. The config's `sandbox.cwsandbox.env` name
    list and the server sandbox's env values both derive from this dict.
    """
    return {
        "OPENAI_API_KEY": wandb_key,
        "OPENAI_BASE_URL": WANDB_BASE_URL,
        "HARNESS_OPENAI_AGENTS_MODEL": WANDB_MODEL,
        # W&B is chat/completions-compatible, not the Responses API.
        "HARNESS_OPENAI_AGENTS_USE_RESPONSES": "0",
        # Tell the in-child host to forward the HARNESS_* knobs to the runner.
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": (
            "HARNESS_OPENAI_AGENTS_MODEL,HARNESS_OPENAI_AGENTS_USE_RESPONSES"
        ),
    }


def log(msg: str) -> None:
    print(msg, flush=True)


def start_server_sandbox(
    image: str, sandbox_key: str, wandb_key: str, admin_password: str
) -> tuple[Sandbox, str]:
    """Provision the server sandbox (public service) carrying the child-env values."""
    log(f"[1/6] provisioning server sandbox from {image}")
    api_url = os.environ.get("CWSANDBOX_BASE_URL", "https://api.cwsandbox.com")
    sb = Sandbox.run(
        "sleep",
        "infinity",
        auth=AuthStrategy.WANDB,
        container_image=image,
        max_lifetime_seconds=3600,
        placement_mode="serverless",
        resources={"cpu": "2", "memory": "4Gi"},
        services=[
            Service(
                port=SERVER_PORT,
                name="omnigent",
                visibility="public",
                endpoint=Endpoint(kind="https", auth="open", request_timeout_seconds=900),
            )
        ],
        network=NetworkOptions(egress=[EgressRule(dns_name=urlparse(api_url).hostname)]),
        environment_variables={
            "WANDB_API_KEY": sandbox_key,
            "OMNIGENT_CWSANDBOX_AUTH_STRATEGY": "wandb",
            "CWSANDBOX_BASE_URL": api_url,
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_AUTH_ENABLED": "1",
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": "admin",
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": admin_password,
            "OMNIGENT_CWSANDBOX_HOST_IMAGE": image,
            # Values the launcher passes through (by name) into each child:
            **_child_env(wandb_key),
        },
        tags=["omnigent-e2e", "server"],
    )
    try:
        sb.wait()
        url = next(
            (
                url
                for port, name, url in sb.service_urls
                if port == SERVER_PORT and name == "omnigent"
            ),
            None,
        )
        if not url:
            raise RuntimeError("server sandbox has no HTTPS service URL")
        log(f"      sandbox={sb.sandbox_id} endpoint={url}")
        return sb, url
    except BaseException:
        sb.stop().result()
        raise


def _write(sb: Sandbox, path: str, content: str) -> None:
    b64 = base64.b64encode(content.encode()).decode()
    sb.exec(
        ["bash", "-lc", f"mkdir -p $(dirname {path}) && echo {b64} | base64 -d > {path}"]
    ).result()


def configure_and_start_server(sb: Sandbox, server_url: str, wandb_key: str) -> None:
    """Write config + an openai-agents agent, then launch `omnigent server`."""
    log(f"[2/6] starting omnigent server (server_url={server_url})")
    child_env_names = ", ".join(_child_env(wandb_key))
    egress_hosts = ",".join(
        [
            urlparse(server_url).hostname or "",
            urlparse(WANDB_BASE_URL).hostname or "",
        ]
    )
    _write(
        sb,
        f"{CONFIG_HOME}/config.yaml",
        "sandbox:\n"
        "  provider: cwsandbox\n"
        f"  server_url: {server_url}\n"
        "  cwsandbox:\n"
        f"    env: [{child_env_names}]\n",
    )
    # Agent bound to the openai-agents harness + the W&B model. executor.auth
    # (ApiKeyAuth) is what the runner's gateway routing reads for base_url +
    # api_key — the bare OPENAI_BASE_URL env is ignored, so this is required
    # to target W&B instead of defaulting to api.openai.com.
    _write(
        sb,
        "/root/e2e-agent/agent.yaml",
        "name: e2e-probe\n"
        "prompt: You are a terse calculator. Answer with only the number.\n"
        "executor:\n"
        "  harness: openai-agents\n"
        f"  model: {WANDB_MODEL}\n"
        "  auth:\n"
        "    type: api_key\n"
        "    api_key: ${OPENAI_API_KEY}\n"
        f"    base_url: {WANDB_BASE_URL}\n",
    )
    start = (
        f"OMNIGENT_CONFIG_HOME={CONFIG_HOME} "
        f"OMNIGENT_ACCOUNTS_BASE_URL={server_url} "
        f"OMNIGENT_CWSANDBOX_EGRESS_HOSTS={egress_hosts} "
        f"setsid nohup omnigent server --host 0.0.0.0 --port {SERVER_PORT} "
        f"--config {CONFIG_HOME}/config.yaml --no-open --agent /root/e2e-agent "
        "> /tmp/omnigent-server.log 2>&1 < /dev/null & echo started"
    )
    sb.exec(["bash", "-lc", start]).result()


def wait_server_ready(
    base: str, sb: Sandbox | None, timeout_s: float = 120.0, admin_password: str | None = None
) -> dict:
    log(f"[3/6] waiting for {base}/v1/info")
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            if admin_password:
                login = CLIENT.post(
                    f"{base}/auth/login",
                    json={
                        "username": "admin",
                        "password": admin_password,
                    },
                    timeout=5.0,
                )
                if login.status_code != 200:
                    last = f"login HTTP {login.status_code}"
                    time.sleep(3.0)
                    continue
            r = CLIENT.get(f"{base}/v1/info", timeout=5.0)
            if r.status_code == 200:
                log(f"      ready: {json.dumps(r.json())}")
                return r.json()
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(3.0)
    log(f"      not ready ({last}); server log:")
    _dump_server_logs(sb)
    raise SystemExit("server never became ready")


def pick_agent(base: str, agent_id: str | None = None) -> str:
    resp = CLIENT.get(f"{base}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    if not agents:
        raise SystemExit("no agents registered on the server to bind a session to")
    if agent_id:
        chosen = next((a for a in agents if a.get("id") == agent_id), None)
        if chosen is None:
            raise SystemExit(f"agent_id {agent_id!r} not found on the server")
    else:
        candidates = [a for a in agents if a.get("harness") == "openai-agents"]
        chosen = next((a for a in candidates if a.get("name") == "e2e-probe"), None)
        if chosen is None and len(candidates) == 1:
            chosen = candidates[0]
        if chosen is None:
            raise SystemExit(
                "No unambiguous openai-agents probe found. Register e2e-probe from "
                "deploy/cwsandbox/README.md or pass an openai-agents --agent-id."
            )
    if chosen.get("harness") != "openai-agents":
        raise SystemExit(
            "This probe expects the openai-agents harness. Native agents need their own "
            "CLI credentials and setup; use the probe in deploy/cwsandbox/README.md."
        )
    log(f"      agent_id={chosen['id']} ({chosen.get('name')})")
    return chosen["id"]


def create_managed_session(base: str, agent_id: str) -> str:
    log("[4/6] creating managed session with a prompt")
    body = {
        "agent_id": agent_id,
        "host_type": "managed",
        "initial_items": [
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": PROMPT}]},
            }
        ],
    }
    r = CLIENT.post(f"{base}/v1/sessions", json=body, timeout=180.0)
    if r.status_code >= 300:
        raise SystemExit(f"create session failed: HTTP {r.status_code}: {r.text[:600]}")
    conv_id = r.json()["id"]
    log(f"      session={conv_id}")
    return conv_id


def _dump_server_logs(sb: Sandbox | None) -> None:
    if sb is None:
        log("      (external server — check its own logs)")
        return
    out = sb.exec(
        [
            "bash",
            "-lc",
            "tail -50 ~/.omnigent/logs/cli/cli-*.log 2>/dev/null; "
            "echo '--- stdout ---'; tail -15 /tmp/omnigent-server.log",
        ]
    ).result()
    log(out.stdout)


def wait_host_online(
    base: str, conv_id: str, sb: Sandbox | None, timeout_s: float = 360.0
) -> bool:
    log("[5/6] waiting for the managed host to register (child sandbox)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            d = CLIENT.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json()
            if d.get("host_online"):
                log(f"      ✓ host online: host_id={d.get('host_id')}")
                log(f"        runner_id={d.get('runner_id')}")
                return True
            if d.get("last_task_error"):
                log(f"      launch error: {d['last_task_error']}")
                break
        except httpx.HTTPError:
            pass
        time.sleep(5.0)
    log("      host did not come online; server logs:")
    _dump_server_logs(sb)
    return False


def _assistant_text(items: list[dict]) -> str:
    """Extract concatenated assistant text from session items."""
    out = []
    for it in items:
        if it.get("type") != "message":
            continue
        data = it.get("data") or {}
        if data.get("role") != "assistant":
            continue
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                out.append(block["text"])
            elif isinstance(block, str):
                out.append(block)
    return " ".join(out).strip()


def wait_for_reply(
    base: str,
    conv_id: str,
    sb: Sandbox | None,
    timeout_s: float = 180.0,
) -> str | None:
    """Poll session items until the agent posts an assistant reply."""
    log("[6/6] waiting for the agent to run the LLM turn and reply")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            d = CLIENT.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json()
            # Check for a failure FIRST — an error that arrives after partial
            # assistant text must not be masked by returning that text.
            if d.get("last_task_error"):
                log(f"      task error: {d['last_task_error']}")
                break
            # Only accept the reply once the turn has FINISHED (status back to
            # idle), so an intermediate/streaming block isn't a false PASS.
            text = _assistant_text(d.get("items") or [])
            if text and d.get("status") == "idle":
                return text
        except httpx.HTTPError:
            pass
        time.sleep(4.0)
    log("      no reply.")
    try:
        items = CLIENT.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json().get("items", [])
        log(f"      raw items: {json.dumps(items)[:1500]}")
    except httpx.HTTPError:
        pass
    log("      server logs:")
    _dump_server_logs(sb)
    return None


def authenticate(server: str) -> None:
    """Use an explicit API token or the normal CLI login for this server."""
    from omnigent.cli_auth import load_token, refresh_stored_token

    token = (
        os.environ.get("OMNIGENT_API_TOKEN") or refresh_stored_token(server) or load_token(server)
    )
    if token:
        CLIENT.headers["Authorization"] = f"Bearer {token}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=None,
        help="Use an EXISTING omnigent server at this URL (e.g. http://host:6767) "
        "instead of spinning one up in a CW sandbox. The server must already be "
        "configured with sandbox.provider=cwsandbox.",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Prebaked omnigent+cwsandbox image (required only when spinning up the server).",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Bind the session to this agent id (default: e2e-probe). "
        "Use an openai-agents agent with model credentials configured.",
    )
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    external = args.server is not None
    if external:
        authenticate(args.server)
    admin_password = None if external else secrets.token_urlsafe(32)
    sandbox_key = os.environ.get("WANDB_API_KEY")
    wandb_key = os.environ.get("WANDB_INFERENCE_KEY")
    if not external:
        # Self-hosted mode spins up the server sandbox + injects the LLM creds.
        if not args.image:
            print("ERROR: --image is required unless --server is given", file=sys.stderr)
            return 2
        if not sandbox_key or not wandb_key:
            print("ERROR: set WANDB_API_KEY and WANDB_INFERENCE_KEY", file=sys.stderr)
            return 2

    sb: Sandbox | None = None
    if external:
        base = args.server.rstrip("/")
        log(f"[*] using existing omnigent server at {base}")
    else:
        sb, base = start_server_sandbox(args.image, sandbox_key, wandb_key, admin_password)

    conv_id = None
    reply = None
    try:
        if not external:
            configure_and_start_server(sb, base, wandb_key)
        wait_server_ready(base, sb, admin_password=admin_password)
        agent_id = pick_agent(base, args.agent_id)
        conv_id = create_managed_session(base, agent_id)
        if wait_host_online(base, conv_id, sb):
            reply = wait_for_reply(base, conv_id, sb)
    finally:
        if not args.keep:
            if conv_id is not None:
                try:
                    response = CLIENT.delete(f"{base}/v1/sessions/{conv_id}", timeout=60.0)
                    response.raise_for_status()
                    log("deleted test session and requested managed sandbox cleanup")
                except httpx.HTTPError as exc:
                    log(f"  warning: test session cleanup failed: {exc}")
            if sb is not None:
                sb.stop().result()
                log("stopped server sandbox")

    print("\n" + "=" * 60)
    if reply:
        print("E2E PASSED — agent completed a turn in the managed sandbox.")
        print(f"Prompt: {PROMPT}")
        print(f"Reply:  {reply!r}")
        return 0
    print("E2E FAILED — see logs above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
