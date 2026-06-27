#!/usr/bin/env python3
"""AI-judge validation harness for the GTM control plane.

Independently verifies all five control-plane features end-to-end against
a *running* Omnigent server (local or the deployed Databricks App). For
each feature it exercises the API (and, where applicable, asserts the
bypass path is blocked), then emits a per-feature pass/fail verdict with
evidence (request/response snippets). Any fail blocks sign-off (non-zero
exit).

It does NOT depend on the control-plane code — it is a black-box client,
so it validates the deployed surface the way a real caller would. The
``ProcessHarness`` mode additionally boots a local server in-process so
the judge can run in CI without a deployed workspace; the
``--base-url`` mode points at an already-running server (the deployed
App).

Usage:
    # Against a locally booted server (default; hermetic):
    python deploy/databricks/control_plane_judge.py --mode local

    # Against the deployed Databricks App (header auth via the Apps proxy
    # means identity can't be spoofed from outside; for direct API testing
    # against the app you must run from inside the proxy or pass a bearer
    # token — see --help):
    python deploy/databricks/control_plane_judge.py --mode remote \
        --base-url https://<app>.databricksapps.com --token "$TOKEN"

Identity in local mode is injected via the ``X-Forwarded-Email`` header
(header auth). The judge seeds two template agents and two
session-scoped agents, configures roles via env, and drives the same
flows the spec's acceptance criteria describe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# The control_plane package lives in this script's sibling src/ dir. Local
# mode imports it (to wire an in-process server), so make the script runnable
# standalone — `python deploy/databricks/control_plane_judge.py --mode local`
# — without requiring the caller to pre-set PYTHONPATH.
_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


@dataclass
class Verdict:
    """One feature's pass/fail with evidence."""

    feature: str
    passed: bool
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, evidence: Any) -> None:
        self.checks.append({"check": name, "ok": bool(ok), "evidence": evidence})
        if not ok:
            self.passed = False


class Judge:
    """Black-box verifier driving a running server via HTTP."""

    def __init__(self, client, *, admin: str, contributor: str, consumer: str, audience: str):
        self._c = client
        self.admin = admin
        self.contributor = contributor
        self.consumer = consumer
        self.audience = audience
        self.verdicts: list[Verdict] = []

    def _h(self, email: str) -> dict[str, str]:
        return {"X-Forwarded-Email": email}

    # ── individual feature checks ─────────────────────────────────

    def check_roles(self) -> Verdict:
        v = Verdict("1. Three-tier role model", True)
        for email, expected in (
            (self.admin, "admin"),
            (self.contributor, "contributor"),
            (self.consumer, "consumer"),
        ):
            r = self._c.get("/v1/control-plane/me", headers=self._h(email))
            role = r.json().get("role") if r.status_code == 200 else None
            v.add(
                f"{email} resolves to {expected}",
                role == expected,
                {"status": r.status_code, "role": role},
            )
        # unauthenticated → 401
        r = self._c.get("/v1/control-plane/me")
        v.add("unauthenticated /me is 401", r.status_code == 401, {"status": r.status_code})
        # consumer is use-only
        r = self._c.get("/v1/control-plane/me", headers=self._h(self.consumer))
        caps = r.json().get("capabilities", {}) if r.status_code == 200 else {}
        v.add("consumer cannot publish", caps.get("can_publish") is False, {"capabilities": caps})
        return v

    def check_visibility(self, restricted_agent_id: str) -> Verdict:
        v = Verdict("2. Per-agent visibility", True)
        # admin restricts the agent
        r = self._c.patch(
            f"/v1/control-plane/agents/{restricted_agent_id}/visibility",
            headers=self._h(self.admin),
            json={
                "visibility": "restricted",
                "audience": {"users": [self.audience], "groups": []},
            },
        )
        v.add(
            "admin sets restricted visibility",
            r.status_code == 200,
            {"status": r.status_code, "body": _short(r)},
        )
        # consumer cannot read the management list
        r2 = self._c.get("/v1/control-plane/agents", headers=self._h(self.consumer))
        v.add("consumer denied mgmt list (403)", r2.status_code == 403, {"status": r2.status_code})
        return v

    def check_enforcement_list(self, restricted_agent_id: str, org_agent_id: str) -> Verdict:
        v = Verdict("4a. Enforcement — list filtering", True)
        consumer_ids = _agent_ids(
            self._c.get("/v1/agents?limit=1000", headers=self._h(self.consumer))
        )
        v.add(
            "consumer does NOT see restricted agent",
            restricted_agent_id not in consumer_ids,
            {"ids": sorted(consumer_ids)},
        )
        v.add(
            "consumer sees org agent", org_agent_id in consumer_ids, {"ids": sorted(consumer_ids)}
        )
        aud_ids = _agent_ids(self._c.get("/v1/agents?limit=1000", headers=self._h(self.audience)))
        v.add(
            "audience user sees restricted agent",
            restricted_agent_id in aud_ids,
            {"ids": sorted(aud_ids)},
        )
        return v

    def check_enforcement_launch(self, restricted_agent_id: str, org_agent_id: str) -> Verdict:
        v = Verdict("4b. Enforcement — launch authorization (bypass blocked)", True)
        # The decisive bypass test.
        r = self._c.post(
            "/v1/sessions", headers=self._h(self.consumer), json={"agent_id": restricted_agent_id}
        )
        v.add(
            "direct POST /v1/sessions for restricted agent is 403",
            r.status_code == 403,
            {"status": r.status_code, "body": _short(r)},
        )
        # org agent must not be blocked by the control plane.
        r2 = self._c.post(
            "/v1/sessions", headers=self._h(self.consumer), json={"agent_id": org_agent_id}
        )
        v.add(
            "org agent launch not blocked by CP", r2.status_code != 403, {"status": r2.status_code}
        )
        # Use-only consumer: a multipart bundle upload (bring-your-own-agent)
        # must be denied for the consumer tier (the shipped P1 deny). This is
        # the deny the policy exists to enforce — exercise it directly.
        r3 = self._c.post(
            "/v1/sessions",
            headers=self._h(self.consumer),
            files={"bundle": ("agent.tar.gz", b"x", "application/gzip")},
            data={"metadata": "{}"},
        )
        v.add(
            "consumer multipart bundle upload denied (403)",
            r3.status_code == 403,
            {"status": r3.status_code, "body": _short(r3)},
        )
        return v

    def check_registration(self, source_session_id: str | None) -> Verdict:
        v = Verdict("3. Delegated registration", True)
        # consumer cannot publish
        r = self._c.post(
            "/v1/control-plane/agents/publish",
            headers=self._h(self.consumer),
            json={
                "source_session_id": source_session_id or "x",
                "name": "consumer-attempt",
                "visibility": "org",
            },
        )
        v.add("consumer publish denied (403)", r.status_code == 403, {"status": r.status_code})
        if source_session_id:
            r2 = self._c.post(
                "/v1/control-plane/agents/publish",
                headers=self._h(self.contributor),
                json={
                    "source_session_id": source_session_id,
                    "name": "judge-published-agent",
                    "description": "published by the judge",
                    "visibility": "org",
                },
            )
            v.add(
                "contributor publish succeeds",
                r2.status_code == 200,
                {"status": r2.status_code, "body": _short(r2)},
            )
            if r2.status_code == 200:
                new_id = r2.json().get("agent_id")
                # audit row written
                a = self._c.get("/v1/control-plane/audit", headers=self._h(self.admin))
                actions = [e for e in a.json().get("data", []) if e.get("agent_id") == new_id]
                v.add("publish audit row written", bool(actions), {"audit": actions[:3]})
        return v

    def check_usage(self) -> Verdict:
        v = Verdict("5. Org-wide usage visibility", True)
        r = self._c.get("/v1/control-plane/usage", headers=self._h(self.consumer))
        v.add("consumer usage denied (403)", r.status_code == 403, {"status": r.status_code})
        r2 = self._c.get("/v1/control-plane/usage", headers=self._h(self.admin))
        ok = r2.status_code == 200 and "data" in r2.json() and "totals" in r2.json()
        v.add("admin usage report shape ok", ok, {"status": r2.status_code, "body": _short(r2)})
        return v

    def run(self, *, restricted_agent_id, org_agent_id, source_session_id) -> list[Verdict]:
        self.verdicts = [
            self.check_roles(),
            self.check_visibility(restricted_agent_id),
            self.check_enforcement_list(restricted_agent_id, org_agent_id),
            self.check_enforcement_launch(restricted_agent_id, org_agent_id),
            self.check_registration(source_session_id),
            self.check_usage(),
        ]
        return self.verdicts


def _short(resp) -> str:
    try:
        return json.dumps(resp.json())[:300]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:300]


def _agent_ids(resp) -> set[str]:
    if resp.status_code != 200:
        return set()
    try:
        return {a["id"] for a in resp.json().get("data", [])}
    except Exception:  # noqa: BLE001
        return set()


def render_report(verdicts: list[Verdict]) -> str:
    lines = ["# Control-Plane AI-Judge Report", ""]
    all_pass = all(v.passed for v in verdicts)
    n_pass = sum(v.passed for v in verdicts)
    overall = "PASS ✅" if all_pass else "FAIL ❌"
    lines.append(f"**Overall: {overall}**  ({n_pass}/{len(verdicts)} features passed)")
    lines.append("")
    lines.append("| Feature | Verdict | Checks |")
    lines.append("|---|---|---|")
    for v in verdicts:
        n_ok = sum(c["ok"] for c in v.checks)
        lines.append(
            f"| {v.feature} | {'PASS' if v.passed else 'FAIL'} | {n_ok}/{len(v.checks)} |"
        )
    lines.append("")
    for v in verdicts:
        lines.append(f"## {v.feature} — {'PASS' if v.passed else 'FAIL'}")
        for c in v.checks:
            mark = "✓" if c["ok"] else "✗"
            lines.append(f"- {mark} {c['check']} — `{json.dumps(c['evidence'])[:200]}`")
        lines.append("")
    return "\n".join(lines)


def _run_local() -> list[Verdict]:
    """Boot an in-process server on SQLite, seed data, and judge it.

    Hermetic — no workspace needed. Mirrors the production wiring.
    """
    import os
    import tempfile
    from pathlib import Path

    os.environ["OMNIGENT_AUTH_PROVIDER"] = "header"
    os.environ["OMNIGENT_CP_ADMIN_USERS"] = "admin@db.com"
    os.environ["OMNIGENT_CP_CONTRIBUTOR_GROUPS"] = "gtm-contributors"
    # Pin the shipped consumer-upload policy so the judge tests the real deny.
    os.environ["OMNIGENT_CP_CONSUMER_UPLOAD"] = "deny"

    from fastapi.testclient import TestClient

    import control_plane.identity as cp_identity
    from control_plane.config import ControlPlaneConfig
    from control_plane.wiring import attach_control_plane
    from omnigent.db.utils import generate_agent_id, get_or_create_engine
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    tmp = Path(tempfile.mkdtemp())
    db_uri = f"sqlite:///{tmp / 'judge.db'}"
    get_or_create_engine(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    artifact = LocalArtifactStore(str(tmp / "a"))
    auth = UnifiedAuthProvider(source="header", local_single_user=False)
    app = create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conv_store,
        artifact_store=artifact,
        agent_cache=AgentCache(artifact_store=artifact, cache_dir=tmp / "c"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=perm_store,
        auth_provider=auth,
    )
    cp_identity.set_group_overrides({"carol@db.com": ["gtm-contributors"]})
    attach_control_plane(
        app,
        db_uri=db_uri,
        agent_store=agent_store,
        conversation_store=conv_store,
        permission_store=perm_store,
        auth_provider=auth,
        config=ControlPlaneConfig.from_env(),
    )
    perm_store.ensure_user("admin@db.com")

    org_id = generate_agent_id()
    agent_store.create(
        agent_id=org_id, name="judge-org-agent", bundle_location="loc/org", description="org"
    )
    res_id = generate_agent_id()
    agent_store.create(
        agent_id=res_id, name="judge-secret-agent", bundle_location="loc/res", description="secret"
    )

    # session-scoped agent owned by the contributor (publish source)
    sa_id = generate_agent_id()
    created = conv_store.create_session_with_agent(
        agent_id=sa_id,
        agent_name="carol-session-agent",
        agent_bundle_location="bundle/carol",
        agent_description="private",
    )
    src_session = created.conversation.id
    perm_store.ensure_user("carol@db.com")
    perm_store.grant("carol@db.com", src_session, 4)

    client = TestClient(app, raise_server_exceptions=True)
    judge = Judge(
        client,
        admin="admin@db.com",
        contributor="carol@db.com",
        consumer="dave@db.com",
        audience="bob@db.com",
    )
    return judge.run(
        restricted_agent_id=res_id, org_agent_id=org_id, source_session_id=src_session
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["local", "remote"], default="local")
    ap.add_argument("--base-url", default=None, help="Server base URL (remote mode)")
    ap.add_argument("--token", default=None, help="Bearer token (remote mode)")
    ap.add_argument("--admin", default="admin@db.com")
    ap.add_argument("--contributor", default="carol@db.com")
    ap.add_argument("--consumer", default="dave@db.com")
    ap.add_argument("--audience", default="bob@db.com")
    ap.add_argument("--restricted-agent-id", default=None, help="(remote) agent id to restrict")
    ap.add_argument("--org-agent-id", default=None, help="(remote) org-visible agent id")
    ap.add_argument(
        "--source-session-id", default=None, help="(remote) contributor-owned session to publish"
    )
    ap.add_argument("--out", default=None, help="Write the markdown report to this path")
    args = ap.parse_args()

    if args.mode == "local":
        verdicts = _run_local()
    else:
        import httpx

        if not args.base_url:
            ap.error("remote mode requires --base-url")
        headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
        client = httpx.Client(
            base_url=args.base_url, headers=headers, timeout=30.0, follow_redirects=False
        )
        judge = Judge(
            client,
            admin=args.admin,
            contributor=args.contributor,
            consumer=args.consumer,
            audience=args.audience,
        )
        verdicts = judge.run(
            restricted_agent_id=args.restricted_agent_id,
            org_agent_id=args.org_agent_id,
            source_session_id=args.source_session_id,
        )

    report = render_report(verdicts)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
    all_pass = all(v.passed for v in verdicts)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
