#!/usr/bin/env python3
"""
End-to-end test: create a managed session against an Omnigent server, and have
the agent run a REAL workload — an LLM turn against the CoreWeave / W&B inference
endpoint — from inside the managed CHILD sandbox the server provisions.

Two modes:

  1. --server <url>: use an EXISTING omnigent server (already configured with
     sandbox.provider=cwsandbox). No public-IP sandbox required — good for W&B
     serverless users who can't expose a public service.

         python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
             --server http://my-omnigent:6767

  2. (default) spin the server up inside a CW Sandbox with a public service. The
     sandbox runs a prebaked image with this fork's omnigent + the cwsandbox SDK
     (build/push first; pass --image), and the driver injects the LLM creds.

         export CWSANDBOX_API_KEY=...        # provisions the sandboxes
         export WANDB_INFERENCE_KEY=...      # the agent's LLM credential
         python tests/e2e/integrations/deploy/cwsandbox/e2e_managed.py \
             --image docker.io/<you>/omnigent-cwsandbox:test
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

import httpx
from cwsandbox import NetworkOptions, Sandbox

SERVER_PORT = 6767
CONFIG_HOME = "/root/.omnigent"
WANDB_BASE_URL = "https://api.inference.wandb.ai/v1"
WANDB_MODEL = "Qwen/Qwen3-Coder-480B-A35B-Instruct"
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


def start_server_sandbox(image: str, cw_key: str, wandb_key: str) -> tuple[Sandbox, str]:
    """Provision the server sandbox (public service) carrying the child-env values."""
    log(f"[1/6] provisioning server sandbox from {image}")
    sb = Sandbox.run(
        "sleep",
        "infinity",
        container_image=image,
        profile_names=["default"],
        max_lifetime_seconds=3600,
        resources={"cpu": "2", "memory": "4Gi"},
        network=NetworkOptions(
            ingress_mode="public", exposed_ports=[SERVER_PORT], egress_mode="internet"
        ),
        environment_variables={
            "CWSANDBOX_API_KEY": cw_key,
            "OMNIGENT_CWSANDBOX_HOST_IMAGE": image,
            # Values the launcher passes through (by name) into each child:
            **_child_env(wandb_key),
        },
        tags=["omnigent-e2e", "server"],
    )
    sb.wait()
    ip = (sb.service_address or "").split(":")[0]
    if not ip:
        raise SystemExit(f"server sandbox {sb.sandbox_id} has no public service address")
    log(f"      sandbox={sb.sandbox_id} public_ip={ip}")
    return sb, ip


def _write(sb: Sandbox, path: str, content: str) -> None:
    b64 = base64.b64encode(content.encode()).decode()
    sb.exec(
        ["bash", "-lc", f"mkdir -p $(dirname {path}) && echo {b64} | base64 -d > {path}"]
    ).result()


def configure_and_start_server(sb: Sandbox, server_url: str, wandb_key: str) -> None:
    """Write config + an openai-agents agent, then launch `omnigent server`."""
    log(f"[2/6] starting omnigent server (server_url={server_url})")
    child_env_names = ", ".join(_child_env(wandb_key))
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
        f"    api_key: {wandb_key}\n"
        f"    base_url: {WANDB_BASE_URL}\n",
    )
    start = (
        f"OMNIGENT_CONFIG_HOME={CONFIG_HOME} OMNIGENT_LOCAL_SINGLE_USER=1 "
        f"setsid nohup omnigent server --host 0.0.0.0 --port {SERVER_PORT} "
        f"--config {CONFIG_HOME}/config.yaml --no-open --agent /root/e2e-agent "
        "> /tmp/omnigent-server.log 2>&1 < /dev/null & echo started"
    )
    sb.exec(["bash", "-lc", start]).result()


def wait_server_ready(base: str, sb: Sandbox | None, timeout_s: float = 120.0) -> dict:
    log(f"[3/6] waiting for {base}/v1/info")
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/v1/info", timeout=5.0)
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
    resp = httpx.get(f"{base}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    if not agents:
        raise SystemExit("no agents registered on the server to bind a session to")
    if agent_id:
        chosen = next((a for a in agents if a.get("id") == agent_id), None)
        if chosen is None:
            raise SystemExit(f"agent_id {agent_id!r} not found on the server")
    else:
        chosen = next((a for a in agents if a.get("name") == "e2e-probe"), agents[0])
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
    r = httpx.post(f"{base}/v1/sessions", json=body, timeout=180.0)
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
            "tail -50 ~/.omnigent/logs/cli-*.log 2>/dev/null; "
            "echo '--- stdout ---'; tail -15 /tmp/omnigent-server.log",
        ]
    ).result()
    log(out.stdout)


def _omnigent_children() -> list:
    """List sandboxes the launcher tags 'omnigent' (managed hosts); [] on error."""
    try:
        return Sandbox.list(tags=["omnigent"]).result()
    except Exception as exc:
        log(f"  (could not list child sandboxes: {exc})")
        return []


def dump_child_logs(exclude: set[str]) -> None:
    """Dump runner/harness logs from a CHILD this run created (not in *exclude*)."""
    # Scope to children NOT present before this run — never touch sandboxes
    # belonging to other runs / deployments that share the 'omnigent' tag.
    children = [c for c in _omnigent_children() if c.sandbox_id not in exclude]
    running = [c for c in children if "RUNNING" in str(getattr(c, "status", "")).upper()]
    target = (running or children or [None])[0]
    if target is None:
        log("  (no child sandbox found for this run)")
        return
    log(f"  --- child sandbox {target.sandbox_id} runner/harness logs ---")
    try:
        out = target.exec(
            [
                "bash",
                "-lc",
                "tail -60 ~/.omnigent/logs/*.log 2>/dev/null; echo '--- host log ---'; "
                "tail -40 /tmp/omnigent-host.log 2>/dev/null",
            ]
        ).result()
        log(out.stdout)
    except Exception as exc:
        log(f"  (could not read child logs: {exc})")


def wait_host_online(
    base: str, conv_id: str, sb: Sandbox | None, timeout_s: float = 360.0
) -> bool:
    log("[5/6] waiting for the managed host to register (child sandbox)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json()
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
    pre_children: set[str],
    timeout_s: float = 180.0,
) -> str | None:
    """Poll session items until the agent posts an assistant reply."""
    log("[6/6] waiting for the agent to run the LLM turn and reply")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json()
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
        items = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=5.0).json().get("items", [])
        log(f"      raw items: {json.dumps(items)[:1500]}")
    except httpx.HTTPError:
        pass
    log("      server logs:")
    _dump_server_logs(sb)
    if sb is not None:
        log("      child sandbox logs:")
        dump_child_logs(pre_children)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=None,
        help="Use an EXISTING omnigent server at this URL (e.g. http://host:6767) "
        "instead of spinning one up in a CW sandbox. The server must already be "
        "configured with sandbox.provider=cwsandbox. No public-IP sandbox needed.",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Prebaked omnigent+cwsandbox image (required only when spinning up the server).",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Bind the session to this agent id (default: the seeded e2e-probe, "
        "else the first registered agent). Useful with --server.",
    )
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    external = args.server is not None
    cw_key = os.environ.get("CWSANDBOX_API_KEY")
    wandb_key = os.environ.get("WANDB_INFERENCE_KEY")
    if not external:
        # Self-hosted mode spins up the server sandbox + injects the LLM creds.
        if not args.image:
            print("ERROR: --image is required unless --server is given", file=sys.stderr)
            return 2
        if not cw_key or not wandb_key:
            print("ERROR: set CWSANDBOX_API_KEY and WANDB_INFERENCE_KEY", file=sys.stderr)
            return 2

    sb: Sandbox | None = None
    if external:
        base = args.server.rstrip("/")
        log(f"[*] using existing omnigent server at {base}")
    else:
        sb, ip = start_server_sandbox(args.image, cw_key, wandb_key)
        base = f"http://{ip}:{SERVER_PORT}"

    # Children carry the shared "omnigent" tag, so snapshot which ones already
    # existed before this run — we only ever touch the ones WE cause to appear,
    # never another run's / deployment's managed hosts.
    pre_children = {c.sandbox_id for c in _omnigent_children()} if sb is not None else set()

    reply = None
    try:
        if not external:
            configure_and_start_server(sb, base, wandb_key)
        wait_server_ready(base, sb)
        agent_id = pick_agent(base, args.agent_id)
        conv_id = create_managed_session(base, agent_id)
        if wait_host_online(base, conv_id, sb):
            reply = wait_for_reply(base, conv_id, sb, pre_children)
    finally:
        # Only tear down sandboxes WE created. An external server owns its own
        # children, so leave them alone.
        if sb is not None and not args.keep:
            log(f"cleaning up server sandbox {sb.sandbox_id} + managed child(ren)")
            try:
                sb.stop().result()
            except Exception as exc:
                log(f"  warning: server cleanup failed: {exc}")
            # Stop only children that appeared during this run (id not in the
            # pre-run snapshot) — never another run's sandboxes.
            for child in _omnigent_children():
                if child.sandbox_id in pre_children:
                    continue
                try:
                    child.stop().result()
                    log(f"  stopped child {child.sandbox_id}")
                except Exception as exc:
                    log(f"  warning: child cleanup failed for {child.sandbox_id}: {exc}")

    print("\n" + "=" * 60)
    if reply:
        print("E2E PASSED — agent ran a real LLM workload in the sandbox.")
        print(f"Prompt: {PROMPT}")
        print(f"Reply:  {reply!r}")
        return 0
    print("E2E FAILED — see logs above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
