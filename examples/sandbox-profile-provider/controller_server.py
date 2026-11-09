"""Local controller simulator. Stores sandbox assignments outside Omnigent's DB.

Run with --state /tmp/controller-state.json. No containers or hosts are started.
The service token is read from SANDBOX_CONTROLLER_TOKEN; bind only to loopback.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request

from omnigent.community.sandbox.profile_demo import CatalogConfig, FileCatalogSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import SandboxProfileContext


def create_controller(state_file: Path, catalog_file: Path) -> FastAPI:
    lock = threading.RLock()
    state = json.loads(state_file.read_text()) if state_file.exists() else {}

    def persist():
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(state_file)

    def service(request: Request):
        token = os.environ.get("SANDBOX_CONTROLLER_TOKEN", "")
        if not token or not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            raise HTTPException(401, "Invalid service credential")

    app = FastAPI(dependencies=[Depends(service)])

    def context(request: Request) -> SandboxProfileContext:
        user = request.headers.get("X-Sandbox-User")
        try:
            workspace = int(request.headers["X-Sandbox-Workspace"])
        except (KeyError, ValueError):
            raise HTTPException(403, "Missing execution identity") from None
        if not user:
            raise HTTPException(403, "Missing execution identity")
        return SandboxProfileContext(user_id=user, workspace_id=workspace)

    def choices(request: Request):
        return FileCatalogSandboxLauncher(
            config=CatalogConfig(catalog_file=str(catalog_file))
        ).list_profiles(context(request))

    def exact(request: Request, profile_id: str, revision: str):
        caller = context(request)
        current = next((p for p in choices(request) if p.id == profile_id), None)
        if current is None:
            raise HTTPException(403, "Profile access denied")
        if current.revision == revision:
            return current.model_dump()
        with lock:
            for record in state.values():
                profile = record["sandbox_profile"]
                if (
                    record["workspace_id"] == caller.workspace_id
                    and profile["id"] == profile_id
                    and profile["revision"] == revision
                ):
                    return profile
        raise HTTPException(409, "Revision unavailable")

    def sandbox(request: Request, sandbox_id: str):
        record = state.get(sandbox_id)
        if record is None:
            raise HTTPException(404, "Sandbox gone")
        # Status and cleanup use the service credential, independent of catalog health.
        if request.headers.get("X-Sandbox-User") is not None:
            caller = context(request)
            if (record["user_id"], record["workspace_id"]) != (
                caller.user_id,
                caller.workspace_id,
            ):
                raise HTTPException(403, "Sandbox access denied")
            exact(request, record["sandbox_profile"]["id"], record["sandbox_profile"]["revision"])
        return record

    @app.get("/profiles")
    def profiles(request: Request):
        return {"profiles": [p.model_dump() for p in choices(request)]}

    @app.get("/profiles/{profile_id}")
    def profile(request: Request, profile_id: str, revision: str):
        return {"profile": exact(request, profile_id, revision)}

    @app.post("/sandboxes")
    def create(request: Request, body: dict):
        selected = body["sandbox_profile"]
        accepted = exact(request, selected["id"], selected["revision"])
        caller = context(request)
        with lock:
            sandbox_id = uuid4().hex
            record = {
                "sandbox_id": sandbox_id,
                "sandbox_profile": accepted,
                "user_id": caller.user_id,
                "workspace_id": caller.workspace_id,
                "running": True,
            }
            state[sandbox_id] = record
            persist()
        return {"sandbox_id": sandbox_id, "sandbox_profile": accepted}

    @app.get("/sandboxes/{sandbox_id}")
    def get(request: Request, sandbox_id: str):
        with lock:
            record = sandbox(request, sandbox_id)
            return {
                "sandbox_id": sandbox_id,
                "sandbox_profile": record["sandbox_profile"],
                "running": record["running"],
            }

    @app.post("/sandboxes/{sandbox_id}/{operation}")
    def resume(request: Request, sandbox_id: str, operation: str, body: dict):
        if operation not in ("resume", "host"):
            raise HTTPException(404)
        context(request)
        with lock:
            record = sandbox(request, sandbox_id)
            expected = {k: record["sandbox_profile"][k] for k in ("id", "revision")}
            if body.get("sandbox_profile") != expected:
                raise HTTPException(409, "Sandbox assignment mismatch")
            record["running"] = True
            persist()
        # Never persist or print bootstrap tokens sent to the simulated host operation.
        return {"workspace": "/workspace/demo"}

    @app.delete("/sandboxes/{sandbox_id}")
    def delete(sandbox_id: str):
        with lock:
            if sandbox_id not in state:
                raise HTTPException(404)
            state.pop(sandbox_id)
            persist()
        return {}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path(__file__).with_name("catalog.json"))
    parser.add_argument("--port", type=int, default=18991)
    args = parser.parse_args()
    uvicorn.run(create_controller(args.state, args.catalog), host="127.0.0.1", port=args.port)
