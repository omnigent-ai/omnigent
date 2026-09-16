"""
End-to-end guard: the managed-sandbox ``workspace-prep`` init container must
be able to clone a private repository when ``GIT_TOKEN`` is present in its
environment.

User journey (operator + user):

1. An operator configures ``sandbox.provider: kubernetes`` with a harness
   Secret (``sandbox.kubernetes.secret_name``) that injects ``GIT_TOKEN`` into
   every sandbox container.
2. A user creates a managed session with a private-repository workspace
   (``POST /v1/sessions`` with ``host_type: "managed"`` and a
   ``https://github.com/...`` workspace URL).
3. The launcher submits a Job whose ``workspace-prep`` init container first
   wires the per-user clone credential broker
   (``omnigent.git_credential_github.configure_clone_credentials``) and then
   runs ``git clone``.

Reported bug: the broker's connection probe does not resolve to a conclusive
``connected: false`` (on a deployment without a GitHub connection provider the
credential endpoint 404s; the report blames a host-registration race), so the
fail-closed broker install resets the ``credential.https://github.com.helper``
chain — clearing the host image's system-level ``$GIT_TOKEN`` helper — and
installs a broker that then declines per-op. The clone dies with::

    fatal: could not read Username for 'https://github.com': No such device or address

even though ``GIT_TOKEN`` is set in the init container env, and
``git config --global --list`` shows the broker as the sole github.com helper.

The apiserver and kubelet are stood in for locally (no cluster in CI): a stub
``kubernetes`` SDK on the server subprocess's PYTHONPATH records the Secret and
Job manifests the real launcher submits, and the captured ``workspace-prep``
init-container command is executed locally with the Pod's environment — the
launch token from the captured token Secret, ``GIT_TOKEN`` standing in for the
harness Secret's ``envFrom``, and the host image's exact system git credential
helper (deploy/docker/Dockerfile). ``github.com`` itself is stood in for by a
local TLS server that serves a bare repository over git smart HTTP and requires
token auth like a private repository, reached through a local CONNECT proxy so
the clone URL stays literally ``https://github.com/...`` (which is what makes
the broker's github.com-scoped helper reset govern the clone). Everything else
is real: the server process, the managed-session HTTP journey, the launcher,
the rendered init-container script, and the broker module it runs.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import os
import re
import shlex
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from tests.e2e._k8s_stub_sdk import CAPTURE_ENV_VAR as _CAPTURE_ENV_VAR
from tests.e2e._k8s_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 180.0
_MANIFEST_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5
_INIT_RUN_TIMEOUT_S = 300.0

_REPO_URL = "https://github.com/omni-test/private-widgets"
_GIT_TOKEN = "shared-fleet-git-token"
_HARNESS_SECRET = "omnigent-harness"

# The EXACT system-level credential helper the managed host image installs
# (deploy/docker/Dockerfile): answers `git credential get` from GIT_TOKEN /
# GIT_USERNAME in the environment. The broker install's github.com helper-chain
# reset is what clears it in the buggy path.
_IMAGE_HELPER = (
    '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; '
    'printf "username=%s\\npassword=%s\\n" "${GIT_USERNAME:-x-access-token}" "$GIT_TOKEN"; }; f'
)


def _find_free_port() -> int:
    """Bind port 0 and return the assigned free port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------
# github.com stand-in: TLS smart-HTTP git server requiring token auth, plus a
# CONNECT proxy so `git clone https://github.com/...` lands on it verbatim.
# --------------------------------------------------------------------------


def _generate_github_cert(directory: Path) -> tuple[Path, Path]:
    """Self-signed TLS cert for CN/SAN github.com (verification is disabled
    in the clone env via GIT_SSL_NO_VERIFY; the cert only completes the
    handshake)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "github.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("github.com")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "github-key.pem"
    cert_path = directory / "github-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class _GitHubStandInHandler(BaseHTTPRequestHandler):
    """Private-repo behavior: 401 + Basic challenge without the token, real
    git smart HTTP (via ``git http-backend``) with it."""

    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode()
        except (ValueError, UnicodeDecodeError):
            return False
        return decoded.partition(":")[2] == self.server.expected_token  # type: ignore[attr-defined]

    def _serve(self) -> None:
        if not self._authorized():
            body = b"Repository not found or access denied\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""
        path, _, query = self.path.partition("?")
        env = {
            "GIT_PROJECT_ROOT": str(self.server.project_root),  # type: ignore[attr-defined]
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "REMOTE_USER": "x-access-token",
            "REMOTE_ADDR": self.client_address[0],
            "CONTENT_TYPE": self.headers.get("Content-Type") or "",
            "CONTENT_LENGTH": str(length) if length else "",
            "GATEWAY_INTERFACE": "CGI/1.1",
            "PATH": os.environ.get("PATH", ""),
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["HTTP_GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        proc = subprocess.run(
            ["git", "http-backend"], input=request_body, env=env, capture_output=True, timeout=60
        )
        header_blob, _, body = proc.stdout.partition(b"\r\n\r\n")
        status = "200 OK"
        headers: list[tuple[str, str]] = []
        for line in header_blob.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = value.strip()
            elif name and name.lower() != "content-length":
                headers.append((name, value.strip()))
        self.send_response(int(status.split()[0]))
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet: request logging would interleave with pytest output."""


class _ConnectProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    tls_port: int = 0


class _ConnectProxyHandler(socketserver.BaseRequestHandler):
    """Minimal HTTP CONNECT proxy: tunnels every CONNECT to the TLS stand-in,
    so `https_proxy` routes github.com to it without touching DNS or :443."""

    def handle(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        if not data.split(b"\r\n", 1)[0].startswith(b"CONNECT"):
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        upstream = socket.create_connection(("127.0.0.1", self.server.tls_port))  # type: ignore[attr-defined]
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    buf = src.recv(65536)
                    if not buf:
                        break
                    dst.sendall(buf)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)

        pump_back = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        pump_back.start()
        pump(self.request, upstream)
        pump_back.join(timeout=30)


def _start_fake_github(
    tmp_path: Path, project_root: Path
) -> tuple[ThreadingHTTPServer, _ConnectProxy, int]:
    """Start the TLS git server + CONNECT proxy; return (server, proxy, proxy_port)."""
    cert_path, key_path = _generate_github_cert(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubStandInHandler)
    httpd.daemon_threads = True
    httpd.expected_token = _GIT_TOKEN  # type: ignore[attr-defined]
    httpd.project_root = project_root  # type: ignore[attr-defined]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    proxy = _ConnectProxy(("127.0.0.1", 0), _ConnectProxyHandler)
    proxy.tls_port = httpd.server_address[1]
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    return httpd, proxy, proxy.server_address[1]


def _seed_private_repo(tmp_path: Path) -> Path:
    """Create the bare 'private' repo the fake github.com serves; return its root."""
    root = tmp_path / "repos"
    bare = root / "omni-test" / "private-widgets"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", "--initial-branch=main", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text("private widgets\n")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "seed",
        "GIT_AUTHOR_EMAIL": "seed@example.com",
        "GIT_COMMITTER_NAME": "seed",
        "GIT_COMMITTER_EMAIL": "seed@example.com",
    }
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=seed, check=True, env=env)
    subprocess.run(["git", "add", "."], cwd=seed, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=seed, check=True, env=env)
    subprocess.run(["git", "push", "-q", str(bare), "main:main"], cwd=seed, check=True, env=env)
    return root


# --------------------------------------------------------------------------
# Real server with the stub kubernetes SDK (apiserver stand-in).
# --------------------------------------------------------------------------


def _write_stub_sdk(tmp_path: Path) -> Path:
    """Materialize the stub SDK, extended to capture Secret bodies (the launch
    token rides the token Secret's stringData; the local init-container run
    needs its raw value exactly like the Pod's secretKeyRef would get it)."""
    root = tmp_path / "k8s_stub"
    for rel, source in _STUB_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            source.replace(
                '{"call": "create_namespaced_secret", "namespace": namespace}',
                '{"call": "create_namespaced_secret", "namespace": namespace, "body": body}',
            )
        )
    return root


def _write_server_config(tmp_path: Path, port: int) -> Path:
    """Server config: kubernetes provider + harness Secret carrying GIT_TOKEN."""
    config_path = tmp_path / "server-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "server_url": f"http://127.0.0.1:{port}",
                    "provider": "kubernetes",
                    "kubernetes": {
                        "image": "ghcr.io/omnigent-ai/omnigent-host:e2e",
                        "namespace": "omnigent-sandboxes",
                        "in_cluster": False,
                        "kubeconfig": str(tmp_path / "kubeconfig"),
                        # The stub never reports a running pod. Keep the wait
                        # LONG so the background launch is still pending — the
                        # armed launch token not yet revoked — while this test
                        # runs the init container locally, matching the real
                        # in-cluster timing (init runs while the server polls).
                        "pod_ready_timeout_s": 600,
                        "secret_name": _HARNESS_SECRET,
                    },
                }
            }
        )
    )
    (tmp_path / "kubeconfig").write_text("")
    return config_path


def _spawn_server(
    tmp_path: Path, config_path: Path, port: int, capture_path: Path
) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real ``omnigent server`` subprocess wired to the stub SDK."""
    stub_root = _write_stub_sdk(tmp_path)
    pythonpath = os.pathsep.join(
        [
            str(stub_root),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        _CAPTURE_ENV_VAR: str(capture_path),
        "OPENAI_API_KEY": "unused-no-turn-runs",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(
            _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
        ),
    }
    log_path = tmp_path / "server.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen's lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--config",
            str(config_path),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path


def _wait_for_health(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Wait for /health, failing with the server log if the process dies."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"server exited (code {proc.returncode}) before serving /health:\n"
                f"{log_path.read_text()[-2000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"server did not become healthy:\n{log_path.read_text()[-2000:]}")


def _create_managed_session(base_url: str) -> None:
    """Drive the user journey: a managed session with a private-repo workspace."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    assert agents, "no agents registered on the server to bind a session to"
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agents[0]["id"], "host_type": "managed", "workspace": _REPO_URL},
        timeout=120.0,
    )
    assert response.status_code == 201, (
        f"managed session create failed: HTTP {response.status_code}: {response.text[:500]}"
    )


def _await_capture(capture_path: Path, call: str, log_path: Path) -> dict:
    """Return the first captured stub-SDK record for *call*."""
    deadline = time.monotonic() + _MANIFEST_TIMEOUT_S
    while time.monotonic() < deadline:
        if capture_path.exists():
            records = json.loads(capture_path.read_text())
            matches = [r for r in records if r["call"] == call]
            if matches:
                return matches[0]
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"the launcher never submitted a {call!r} for the managed session:\n"
        f"{log_path.read_text()[-3000:]}"
    )


def _github_helpers(home: Path) -> str:
    """The effective github.com credential helper entries in *home*'s global config."""
    listing = subprocess.run(
        ["git", "config", "--global", "--list"],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in listing.stdout.splitlines() if "credential" in ln]
    return "\n".join(lines) or "(no credential entries)"


def test_workspace_prep_clone_authenticates_with_git_token(tmp_path: Path) -> None:
    """The workspace-prep init container must clone a private repo when
    GIT_TOKEN is in its env — the clone credential broker wiring must not
    disarm the image's GIT_TOKEN helper and strand the clone without any
    credentials."""
    project_root = _seed_private_repo(tmp_path)
    httpd, proxy, proxy_port = _start_fake_github(tmp_path, project_root)
    port = _find_free_port()
    config_path = _write_server_config(tmp_path, port)
    capture_path = tmp_path / "submitted.json"
    proc, log_path = _spawn_server(tmp_path, config_path, port, capture_path)
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        _create_managed_session(base_url)
        manifest = _await_capture(capture_path, "create_namespaced_job", log_path)["manifest"]
        secret_record = _await_capture(capture_path, "create_namespaced_secret", log_path)
        # The stub SDK's capture key for the Secret body differs across
        # revisions ("body" from this test's patch, "manifest" natively).
        secret = secret_record.get("body") or secret_record.get("manifest")
        assert secret is not None, f"no Secret body captured: {sorted(secret_record)}"
        launch_token = secret["stringData"]["OMNIGENT_HOST_TOKEN"]

        pod = manifest["spec"]["template"]["spec"]
        init = pod["initContainers"][0]
        assert init["command"][:2] == ["bash", "-lc"]
        script = init["command"][2]
        # GIT_TOKEN reaches the init container via the harness Secret's envFrom.
        assert init.get("envFrom") == [{"secretRef": {"name": _HARNESS_SECRET}}], (
            "the workspace-prep init container no longer projects the harness "
            f"Secret (GIT_TOKEN): envFrom={init.get('envFrom')!r}"
        )
        # The script shell-quotes the broker wire; recover the host id through
        # shlex so the probe below hits the same endpoint the init container
        # does. Lenient when absent: a fix may legitimately skip the wire when
        # GIT_TOKEN is provided, and then there is no probe to race.
        wire_line = next(
            (ln for ln in script.splitlines() if "configure_clone_credentials" in ln), None
        )
        host_id = ""
        if wire_line is not None:
            wire = shlex.split(wire_line)[2]
            match = re.search(r"configure_clone_credentials\('([^']*)', '([^']*)'\)", wire)
            assert match is not None, f"unparseable broker wire: {wire!r}"
            host_id = match.group(2)

        # The launch token must already resolve when the init container starts:
        # the launcher arms it against the host row BEFORE creating the Job. A
        # 401 here would be the registration race the bug report blames.
        if host_id:
            probe = httpx.get(
                f"{base_url}/v1/hosts/{host_id}/credentials/github",
                headers={MANAGED_HOST_TOKEN_HEADER: launch_token},
                timeout=10.0,
            )
            print(
                f"credential endpoint at init-container time: HTTP {probe.status_code} "
                f"{probe.text[:200]}"
            )
            assert probe.status_code != 401, (
                "the credential endpoint did not recognize the launch token at "
                "init-container time — the host was not registered before the "
                "Job was created (the registration race the bug report blames)"
            )

        # ------------------------------------------------------------------
        # kubelet stand-in: run the captured init-container command locally.
        # ------------------------------------------------------------------
        home = tmp_path / "pod-home"
        home.mkdir()
        image_gitconfig = tmp_path / "image-gitconfig"
        # Written via `git config` (like the Dockerfile's `git config --system`)
        # so git itself handles the config-file escaping of the shell function.
        subprocess.run(
            ["git", "config", "--file", str(image_gitconfig), "credential.helper", _IMAGE_HELPER],
            check=True,
        )
        venv_bin = str(Path(sys.executable).parent)
        # The image's login profile puts the venv python on PATH; mirror that
        # for the `bash -lc` login shell (Debian's /etc/profile resets PATH).
        (home / ".bash_profile").write_text(f'export PATH="{venv_bin}:$PATH"\n')
        pod_env = {
            "HOME": str(home),
            "PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(_REPO_ROOT),
            "OMNIGENT_HOST_TOKEN": launch_token,
            "GIT_TOKEN": _GIT_TOKEN,
            "GIT_CONFIG_SYSTEM": str(image_gitconfig),
            "GIT_SSL_NO_VERIFY": "1",
            "https_proxy": f"http://127.0.0.1:{proxy_port}",
            "no_proxy": "127.0.0.1,localhost",
        }

        # Precondition (stand-in sanity, not the bug): the image helper +
        # GIT_TOKEN alone must be able to clone the private repo.
        control_home = tmp_path / "control-home"
        control_home.mkdir()
        control = subprocess.run(
            ["git", "clone", "--", _REPO_URL, str(control_home / "dst")],
            env={**pod_env, "HOME": str(control_home)},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )
        if control.returncode != 0:
            pytest.fail(
                "environment stand-in broken (not the bug under test): a clone "
                "with the image GIT_TOKEN helper alone failed:\n"
                f"{control.stderr[-1500:]}"
            )

        # The rendered script hardcodes the Pod's HOME mount (/home/omnigent);
        # point it at this test's stand-in for that emptyDir mount.
        script_local = script.replace("/home/omnigent", str(home))
        result = subprocess.run(
            ["bash", "-lc", script_local],
            env=pod_env,
            cwd=home,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=_INIT_RUN_TIMEOUT_S,
        )
        helpers = _github_helpers(home)
        assert result.returncode == 0, (
            "workspace-prep failed to clone the private repo even though "
            "GIT_TOKEN was set in the init container env: "
            "the clone credential broker wiring reset the github.com helper "
            "chain (clearing the image's GIT_TOKEN helper) and then declined "
            "to vend, leaving git with no credentials.\n"
            f"--- init container exit code: {result.returncode}\n"
            f"--- init container stderr (tail):\n{result.stderr[-1500:]}\n"
            f"--- effective global credential config:\n{helpers}"
        )
        clone_dir = home / "workspace" / "private-widgets"
        assert (clone_dir / "README.md").is_file(), (
            f"workspace-prep exited 0 but the clone is missing at {clone_dir}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=30)
        httpd.shutdown()
        proxy.shutdown()
