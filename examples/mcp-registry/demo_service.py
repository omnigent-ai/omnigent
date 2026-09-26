"""Local-only OAuth and MCP demo. Uses fictional accounts; never deploy publicly."""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context

REDIRECT_URI = "http://localhost:18780/v1/connections/mcp-tracker/callback"

mcp = FastMCP("Demo work tracker", stateless_http=True, json_response=True)
codes: dict[str, dict] = {}
refresh_tokens: dict[str, dict] = {}
access_tokens: dict[str, dict] = {}


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Show which fictional account is making this request and its refresh count."""
    request = ctx.request_context.request
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    account = access_tokens[token]
    return {"account": account["user"], "refresh_count": account["refresh_count"]}


@mcp.tool()
def read_ticket(ticket_id: str) -> dict:
    """Read a fictional ticket from the demo work tracker."""
    return {
        "id": ticket_id,
        "title": "Make connected tools portable across sandboxes",
        "status": "Open",
    }


@mcp.tool()
def delete_ticket(ticket_id: str) -> str:
    """Demonstration of a tool excluded by the administrator's allowlist."""
    return f"Deleted fictional ticket {ticket_id}"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def authenticate_mcp(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        account = access_tokens.get(token)
        if account is None or account["expires_at"] <= time.time():
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/authorize", response_class=HTMLResponse)
def authorize(request: Request):
    fields = "".join(
        f'<input type="hidden" name="{html.escape(k, quote=True)}" '
        f'value="{html.escape(v, quote=True)}">'
        for k, v in request.query_params.items()
    )
    return f"""<!doctype html><html>
    <body style="font:18px system-ui;max-width:620px;margin:80px auto">
    <h1>Connect Demo work tracker</h1>
    <p>This local demo uses fictional accounts and short-lived tokens.</p>
    <form method="post" action="/authorize">{fields}
    <label>Demo account <input name="user" value="alice@example.test"></label>
    <button type="submit">Approve connection</button></form></body></html>"""


@app.post("/authorize")
async def approve(request: Request):
    fields = dict(await request.form())
    redirect = str(fields.get("redirect_uri", ""))
    if redirect != REDIRECT_URI:
        raise HTTPException(400, "Callback does not match the registered demo redirect URI")
    if fields.get("code_challenge_method") != "S256":
        raise HTTPException(400, "PKCE is required")
    code = secrets.token_urlsafe(24)
    codes[code] = {**fields, "expires_at": time.time() + 120}
    return RedirectResponse(
        REDIRECT_URI + "?" + urlencode({"code": code, "state": fields["state"]}), status_code=303
    )


@app.post("/token")
async def token(request: Request):
    fields = dict(await request.form())
    if fields.get("grant_type") == "authorization_code":
        grant = codes.pop(str(fields.get("code")), None)
        verifier = str(fields.get("code_verifier", ""))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if (
            not grant
            or grant["expires_at"] < time.time()
            or challenge != grant["code_challenge"]
            or fields.get("redirect_uri") != grant["redirect_uri"]
            or fields.get("client_id") != grant["client_id"]
        ):
            raise HTTPException(400, "Invalid authorization code")
        account = {"user": grant["user"], "refresh_count": 0}
    elif fields.get("grant_type") == "refresh_token":
        previous = refresh_tokens.pop(str(fields.get("refresh_token")), None)
        if previous is None:
            raise HTTPException(400, "Invalid refresh token")
        account = {**previous, "refresh_count": previous["refresh_count"] + 1}
    else:
        raise HTTPException(400, "Unsupported grant")
    access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    access_tokens[access] = {**account, "expires_at": time.time() + 45}
    refresh_tokens[refresh] = account
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 45,
    }


app.mount("/", mcp.streamable_http_app())

if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18782)
    parser.add_argument("--redirect-uri", default=REDIRECT_URI)
    args = parser.parse_args()
    REDIRECT_URI = args.redirect_uri
    uvicorn.run(app, host="0.0.0.0", port=args.port)
