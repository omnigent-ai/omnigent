#!/usr/bin/env python3
"""
End-to-end happy-path test: the ``lambda_microvm`` managed sandbox provider.

Runs against an EXISTING omnigent server already configured with
``sandbox.provider: lambda_microvm`` (and a built MicroVM image — see
``deploy/aws-lambda-microvm/README.md``). The AWS-side infrastructure a real
MicroVM launch needs — a built image, an execution role, VPC egress connectors
reaching the server — can't be stood up from the test process, so this driver
talks to a server that already has them, mirroring the kubernetes and cwsandbox
managed drivers in this directory.

    python tests/e2e/integrations/deploy/lambda_microvm/e2e_managed.py \
        --server http://my-omnigent:6767

The flow (each step asserts, exits non-zero on failure):

  1. server advertises managed sandboxes with provider ``lambda_microvm``
  2. create a managed session bound to an agent
  3. wait for the launched MicroVM's host to dial back and register
  4. run a real LLM turn from inside the MicroVM and read the assistant reply
  5. post a follow-up turn and assert the SAME host_id serves it — the headline
     feature: ``resume_preserves_host`` means a thawed snapshot reconnects the
     running host WITHOUT a fresh start (``--idle-wait`` forces a real suspend
     first; without it this proves host-reuse, not a forced suspend/resume)
  6. delete the session, which terminates the MicroVM by its real ``microvmId``

This is a manual driver, not a pytest-collected test (filename is ``e2e_*`` not
``test_*``): it needs live AWS + a configured server, so it never runs on the
unit/e2e CI lanes. It satisfies the CONTRIBUTING happy-path e2e requirement for
the new provider the same way the sibling provider drivers do.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

# Fixed by the lambda_microvm launcher (omnigent/onboarding/sandboxes/lambda_microvm.py).
PROVIDER = "lambda_microvm"
PROMPT = "What is 2+2? Reply with ONLY the number, nothing else."


def log(msg: str) -> None:
    print(msg, flush=True)


def check_server(base: str) -> None:
    log(f"[1/6] checking {base}/v1/info")
    info = httpx.get(f"{base}/v1/info", timeout=10.0).json()
    if not info.get("managed_sandboxes_enabled"):
        raise SystemExit("server does not advertise managed sandboxes — is sandbox: configured?")
    if info.get("sandbox_provider") != PROVIDER:
        raise SystemExit(
            f"server's sandbox provider is {info.get('sandbox_provider')!r}, not {PROVIDER!r}"
        )
    log(f"      ✓ managed sandboxes enabled ({PROVIDER})")


def pick_agent(base: str, agent_id: str | None) -> str:
    resp = httpx.get(f"{base}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    if not agents:
        raise SystemExit("no agents registered on the server to bind a session to")
    if agent_id:
        if not any(a.get("id") == agent_id for a in agents):
            raise SystemExit(f"agent_id {agent_id!r} not found on the server")
        return agent_id
    chosen = agents[0]
    log(f"      agent_id={chosen['id']} ({chosen.get('name')})")
    return chosen["id"]


def create_managed_session(base: str, agent_id: str) -> str:
    log("[2/6] creating managed session with a prompt")
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


def wait_host_online(base: str, conv_id: str, timeout_s: float) -> str:
    log("[3/6] waiting for the MicroVM host to register (cold MicroVM boot is slow)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
        if d.get("host_id"):
            log(f"      ✓ host online: host_id={d['host_id']}")
            return d["host_id"]
        status = d.get("sandbox_status") or {}
        if status.get("stage") == "failed":
            raise SystemExit(f"managed launch failed: {status.get('error')}")
        time.sleep(5.0)
    raise SystemExit(f"host did not come online within {timeout_s:.0f}s")


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


def _assistant_messages_from(items: list[dict]) -> list[str]:
    """Per-message assistant texts (one string per assistant message)."""
    msgs: list[str] = []
    for it in items:
        if it.get("type") != "message":
            continue
        data = it.get("data") or {}
        if data.get("role") != "assistant":
            continue
        parts = [
            block["text"] if isinstance(block, dict) and block.get("text") else block
            for block in data.get("content") or []
            if isinstance(block, (dict, str))
        ]
        text = " ".join(p for p in parts if isinstance(p, str)).strip()
        if text:
            msgs.append(text)
    return msgs


def _assistant_messages(base: str, conv_id: str) -> list[str]:
    """Fetch the session and return its per-message assistant texts."""
    d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
    return _assistant_messages_from(d.get("items") or [])


def wait_for_reply(base: str, conv_id: str, timeout_s: float) -> str:
    log("[4/6] waiting for the agent to run the LLM turn from inside the MicroVM")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
            if d.get("last_task_error"):
                raise SystemExit(f"task error: {d['last_task_error']}")
            text = _assistant_text(d.get("items") or [])
            if text and d.get("status") == "idle":
                log(f"      ✓ assistant replied: {text!r}")
                return text
        except httpx.HTTPError:
            pass
        time.sleep(4.0)
    raise SystemExit(f"no assistant reply within {timeout_s:.0f}s")


def followup_turn_reuses_host(base: str, conv_id: str, host_id: str, timeout_s: float) -> None:
    """Post a second turn and assert the SAME host_id serves it.

    ``lambda_microvm`` sets ``resume_preserves_host = True`` — a snapshot thaw
    restores the running host with its still-valid launch token, so a wake
    reconnects the SAME host_id WITHOUT minting a fresh sandbox. This step does
    NOT force a suspend (the platform's idle timer is ``maxIdleDurationSeconds``,
    900s, and can't be driven from here); it posts a follow-up turn and proves
    identity continuity: the SAME host_id serves the new turn (a fresh launch
    would change it). To force an actual suspend/resume cycle, run with a wait
    longer than the idle timeout between turns (``--idle-wait``).

    The assertion is gated on EVIDENCE the follow-up turn actually ran — the new
    answer ('6') appears in a NEW assistant message — so it can't pass vacuously
    on the still-idle state left by the previous turn.
    """
    log("[5/6] follow-up turn: expecting the SAME host_id to serve it")
    before = len(_assistant_messages(base, conv_id))
    r = httpx.post(
        f"{base}/v1/sessions/{conv_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is 3+3? Reply with ONLY the number."}
                ],
            },
        },
        timeout=180.0,
    )
    if r.status_code >= 300:
        raise SystemExit(f"follow-up turn failed: HTTP {r.status_code}: {r.text[:600]}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
        if d.get("last_task_error"):
            raise SystemExit(f"task error on follow-up turn: {d['last_task_error']}")
        msgs = _assistant_messages_from(d.get("items") or [])
        # Require a NEW assistant message whose text answers the follow-up (6),
        # AND the turn to have finished (status back to idle). Without the
        # new-message gate this would pass instantly on the prior turn's idle.
        if len(msgs) > before and d.get("status") == "idle" and "6" in msgs[-1]:
            woke_host = d.get("host_id")
            if woke_host != host_id:
                raise SystemExit(
                    f"follow-up turn served by a NEW host {woke_host!r} (expected the "
                    f"preserved {host_id!r}) — resume_preserves_host semantics are broken"
                )
            log(f"      ✓ same host served the follow-up turn (host_id={woke_host})")
            return
        time.sleep(4.0)
    raise SystemExit(f"follow-up turn did not complete within {timeout_s:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Omnigent server base URL")
    parser.add_argument("--agent-id", default=None, help="Agent to bind (default: first)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Per-step wait (s): host-online, first reply, and the follow-up turn.",
    )
    parser.add_argument("--keep", action="store_true", help="Skip session cleanup")
    parser.add_argument(
        "--skip-wake",
        action="store_true",
        help="Skip the follow-up-turn / host-reuse step (step 5)",
    )
    parser.add_argument(
        "--idle-wait",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep before the follow-up turn so the MicroVM's idle timer "
            "(maxIdleDurationSeconds, 900s) fires and the turn drives a REAL "
            "suspend→resume cycle. Default 0 tests host-reuse without a forced suspend."
        ),
    )
    args = parser.parse_args()
    base = args.server.rstrip("/")

    check_server(base)
    agent_id = pick_agent(base, args.agent_id)
    conv_id = create_managed_session(base, agent_id)
    try:
        host_id = wait_host_online(base, conv_id, args.timeout)
        reply = wait_for_reply(base, conv_id, args.timeout)
        if "4" not in reply:
            raise SystemExit(f"assistant reply {reply!r} does not contain the expected answer '4'")
        if not args.skip_wake:
            if args.idle_wait > 0:
                log(f"      sleeping {args.idle_wait:.0f}s so the idle timer fires a suspend")
                time.sleep(args.idle_wait)
            followup_turn_reuses_host(base, conv_id, host_id, args.timeout)
    finally:
        if args.keep:
            log(f"[6/6] --keep: leaving session {conv_id} (and its MicroVM) running")
        else:
            log(f"[6/6] deleting session {conv_id} (terminates the MicroVM by its real id)")
            try:
                httpx.delete(f"{base}/v1/sessions/{conv_id}", timeout=60.0)
            except httpx.HTTPError as exc:
                log(f"      cleanup failed (MicroVM may linger until its lifetime cap): {exc}")
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
