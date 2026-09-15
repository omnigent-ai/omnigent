"""Disposable real server/host fixture for provider setup and SDK routing checks.

Only vendor discovery is replaced. Config writes, host tunnels, sessions and SDK
requests use production code. Every child Python process installs the same guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOST_IDS = ("11111111111141118111111111111111", "22222222222242228222222222222222")
_GUARDED = False


def install_guards() -> None:
    """Block ambient credentials, non-local traffic, and unapproved executables."""
    global _GUARDED
    if _GUARDED or getattr(sys, "_provider_fixture_guard_installed", False):
        return
    _GUARDED = True
    root = Path(os.environ["PROVIDER_FIXTURE_ROOT"]).resolve()
    checkout = Path(os.environ["PROVIDER_FIXTURE_CHECKOUT"]).resolve()
    assert os.environ.get("OMNIGENT_DISABLE_KEYRING") == "1"
    assert Path(os.environ["OMNIGENT_CONFIG_HOME"]).resolve().is_relative_to(root)
    allowed_ports = set(json.loads(os.environ["PROVIDER_FIXTURE_PORTS"]))
    socket_root = Path(os.environ["OMNIGENT_HARNESS_TMP_PARENT"]).resolve()
    source_root = Path(__file__).resolve().parents[2]
    user_home = Path.home().resolve()

    def terminal_permitted(command: Any, env: Any, executable: Any) -> bool:
        return False

    def readiness_permitted(command: Any, env: Any, executable: Any) -> bool:
        return False

    def guard(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            address = args[1]
            if isinstance(address, str) and Path(address).resolve().is_relative_to(socket_root):
                return
            if (
                not isinstance(address, tuple)
                or address[0] not in {"127.0.0.1", "::1"}
                or address[1] not in allowed_ports
            ):
                raise RuntimeError("Provider fixture blocked non-fixture network connection")
        if event in {"os.system", "os.posix_spawn", "os.posix_spawnp", "os.exec"}:
            raise RuntimeError("Provider fixture blocked shell execution")
        if event == "subprocess.Popen":
            command = args[1]
            runner = [sys.executable, "-P", "-m", "omnigent.runner._entry"]
            harness = [sys.executable, "-P", "-m", "omnigent.runtime.harnesses._runner"]
            permitted = (
                args[0] == sys.executable
                and isinstance(command, (list, tuple))
                and list(command) == runner
            )
            if (
                args[0] == sys.executable
                and isinstance(command, (list, tuple))
                and list(command[:4]) == harness
            ):
                options = dict(zip(command[4::2], command[5::2], strict=True))
                permitted = (
                    options.get("--harness") in {"openai-agents", "openai-agents-sdk"}
                    and options.get("--module") == "omnigent.inner.openai_agents_sdk_harness"
                    and Path(options.get("--socket", "/")).resolve().is_relative_to(socket_root)
                    and set(options)
                    == {"--harness", "--module", "--socket", "--conversation-id", "--parent-pid"}
                )
            if not permitted and terminal_permitted(command, args[3], args[0]):
                return
            if not permitted and readiness_permitted(command, args[3], args[0]):
                return
            if not permitted:
                raise RuntimeError("Provider fixture blocked unapproved subprocess")
            child_env = args[3]
            if (
                not child_env
                or child_env.get("PROVIDER_FIXTURE_ROOT") != str(root)
                or child_env.get("OMNIGENT_DISABLE_KEYRING") != "1"
                or child_env.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring"
                or str(root / "boot") not in child_env.get("PYTHONPATH", "").split(os.pathsep)
                or not Path(child_env.get("OMNIGENT_CONFIG_HOME", "/"))
                .resolve()
                .is_relative_to(root)
            ):
                raise RuntimeError("Provider fixture runner lost safety environment")
        if event == "ctypes.dlopen" and "Security.framework" in str(args):
            raise RuntimeError("Provider fixture blocked Security framework")
        if event in {
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.rename",
            "os.link",
            "os.symlink",
            "os.chmod",
            "os.truncate",
        }:
            paths = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            for value in paths:
                if not isinstance(value, (str, bytes, os.PathLike)):
                    raise RuntimeError(
                        "Provider fixture blocked filesystem mutation by descriptor"
                    )
                target = Path(os.fsdecode(value)).resolve()
                if not any(target.is_relative_to(base) for base in (root, socket_root)):
                    raise RuntimeError(
                        "Provider fixture blocked mutation outside disposable state"
                    )
        if (
            event in {"open", "os.listdir", "os.scandir"}
            and args
            and isinstance(args[0], (str, bytes, os.PathLike))
        ):
            path = Path(os.fsdecode(args[0])).resolve()
            if event == "open":
                mode, flags = args[1:3]
                writable = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
                )
                if (
                    writable
                    and path != Path(os.devnull)
                    and not any(path.is_relative_to(base) for base in (root, socket_root))
                ):
                    raise RuntimeError("Provider fixture blocked write outside disposable state")
            if any(
                path.is_relative_to(base) for base in (root, checkout, source_root, socket_root)
            ):
                return
            if path.is_relative_to(user_home):
                raise RuntimeError("Provider fixture blocked ambient home access")
            if set(path.parts).intersection(
                {".omnigent", ".codex", ".claude", ".aws", ".databricks", ".ssh", "Keychains"}
            ) or path.name in {
                "auth.json",
                ".credentials.json",
                ".databrickscfg",
                "managed-settings.json",
            }:
                raise RuntimeError("Provider fixture blocked ambient credential file")

    sys.addaudithook(guard)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Provider fixture blocked real Keychain access")

    import keyring
    from keyring.backends.null import Keyring

    keyring.set_keyring(Keyring())
    keyring.get_credential = forbidden
    keyring.get_password = forbidden
    keyring.set_password = forbidden
    keyring.delete_password = forbidden
    import omnigent._platform as platform

    platform.resolve_cli_binary = lambda *args, **kwargs: None
    platform._cli_fallback_dirs = lambda: ()
    import omnigent.onboarding.ambient as ambient

    ambient._detect_providers_now = lambda *args, **kwargs: []
    ambient._codex_config_path = lambda: (
        Path(os.environ["OMNIGENT_CONFIG_HOME"]) / "codex/config.toml"
    )
    ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS = ()
    ambient._claude_login_detected = lambda: False
    ambient._ollama_reachable = lambda: False
    ambient.codex_config_detection = lambda: None
    from omnigent.onboarding import providers

    real_default_chat_model = providers.default_chat_model
    providers.default_chat_model = lambda *args, **kwargs: "fixture-model-no-vendor"
    import omnigent.onboarding.copilot_auth as copilot_auth

    copilot_auth.gh_cli_github_token = lambda *args, **kwargs: None
    import omnigent.onboarding.harness_install as harness_install

    real_harness_cli_installed = harness_install.harness_cli_installed
    harness_install.harness_cli_installed = lambda key, **kwargs: (
        key in {"anthropic", "openai", "pi"}
    )
    harness_install.harness_cli_logged_in = lambda *args, **kwargs: False
    import omnigent.onboarding.harness_readiness as readiness

    readiness.harness_cli_installed = harness_install.harness_cli_installed
    readiness._claude_managed_gateway_configured = lambda: False
    import omnigent.spec.parser as spec_parser

    spec_parser.discover_host_skills = lambda *args, **kwargs: []
    import omnigent.spec.skill_sources as skill_sources

    skill_sources.discover_host_skills = spec_parser.discover_host_skills
    skill_sources.resolve_harness_skills = lambda *args, **kwargs: []
    import omnigent.gateway_inference as gateway

    gateway.gateway_inference_map = dict
    import omnigent.native.native_bridge_common as native_bridge

    native_bridge.reap_orphaned_native_bridge_dirs = lambda: 0
    from omnigent.host.connect import HostProcess

    async def no_catalog(self: Any) -> None:
        return None

    HostProcess._probed_codex_model_options = no_catalog
    HostProcess._probed_claude_model_options = no_catalog

    if os.environ.get("PROVIDER_FIXTURE_READINESS") == "1":
        import runpy

        module = runpy.run_path(str(Path(__file__).with_name("provider_setup_readiness.py")))
        readiness_permitted = module["install"](
            root, Path(sys.executable), real_harness_cli_installed, real_default_chat_model
        )

    if os.environ.get("PROVIDER_FIXTURE_TERMINAL") == "1":
        import runpy

        module = runpy.run_path(str(Path(__file__).with_name("provider_setup_terminal.py")))
        terminal_permitted = module["install"](
            root, socket_root, Path(sys.executable), Path(os.environ["PROVIDER_FIXTURE_TMUX"])
        )
    sys._provider_fixture_guard_installed = True


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProviderSetupRuntime:
    """Start two isolated real hosts and a server without inherited credentials."""

    def __init__(
        self, root: Path, checkout: Path, *, python: Path | None = None, port: int | None = None
    ) -> None:
        self.root = root.resolve()
        self.checkout = checkout.resolve()
        self.python = python or checkout / ".venv/bin/python"
        self.port = port or _port()
        manifest = self.root / "manifest.json"
        self.mock_ports = (
            tuple(json.loads(manifest.read_text())["mock_ports"])
            if manifest.exists()
            else (_port(), _port())
        )
        self.url = f"http://127.0.0.1:{self.port}"
        self.socket_root = Path("/tmp") / (
            "p" + __import__("hashlib").sha256(str(self.root).encode()).hexdigest()[:5]
        )
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[Any] = []

    def prepare(self) -> None:
        """Seed explicit disposable provider configuration, never everyday config."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.socket_root.mkdir(parents=True, exist_ok=True)
        for host in ("host-a", "host-b"):
            (self.socket_root / host).mkdir(exist_ok=True)
        (self.root / "boot").mkdir(exist_ok=True)
        helper = Path(__file__).resolve()
        (self.root / "boot/sitecustomize.py").write_text(
            "import os, runpy\ntry:\n"
            f"    runpy.run_path({str(helper)!r})['install_guards']()\n"
            "except BaseException:\n    import traceback\n"
            "    traceback.print_exc()\n    os._exit(91)\n"
        )
        for component in ("server", "host-a", "host-b"):
            state = self.root / component
            for name in ("config", "data", "tmp", "workspace", "xdg"):
                (state / name).mkdir(parents=True, exist_ok=True)
            config = state / "config/config.yaml"
            if not config.exists():
                import yaml

                providers = {}
                for index, name in enumerate(("fixture-primary", "fixture-secondary")):
                    providers[name] = {
                        "kind": "gateway",
                        "openai": {
                            "base_url": f"http://127.0.0.1:{self.mock_ports[index]}/v1",
                            "api_key": f"fixture-dummy-{index}",
                            "wire_api": "responses",
                            "models": {"default": "gpt-4o-mini"},
                        },
                    }
                providers["fixture-primary"]["default"] = ["openai"]
                config.write_text(yaml.safe_dump({"providers": providers}))
        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "label": "Disposable local provider fixture; vendor discovery disabled",
                    "checkout": str(self.checkout),
                    "url": self.url,
                    "host_ids": HOST_IDS,
                    "mock_ports": self.mock_ports,
                    "root": str(self.root),
                    "workspaces": [str(self.socket_root / host) for host in ("host-a", "host-b")],
                },
                indent=2,
            )
        )

    def environment(self, component: str) -> dict[str, str]:
        state = self.root / (component if component.startswith("host-") else "server")
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                str(path)
                for path in (
                    self.root / "boot",
                    self.checkout,
                    self.checkout / "sdks/python-client",
                    self.checkout / "sdks/ui",
                )
                if path.exists()
            ),
            "PROVIDER_FIXTURE_ROOT": str(self.root),
            "PROVIDER_FIXTURE_CHECKOUT": str(self.checkout),
            "PROVIDER_FIXTURE_PORTS": json.dumps([self.port, *self.mock_ports]),
            "OMNIGENT_DISABLE_KEYRING": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
            "OMNIGENT_CONFIG_HOME": str(state / "config"),
            "OMNIGENT_DATA_DIR": str(state / "data"),
            "TMPDIR": str(state / "tmp"),
            "XDG_CONFIG_HOME": str(state / "xdg"),
            "XDG_CACHE_HOME": str(state / "xdg/cache"),
            "XDG_DATA_HOME": str(state / "xdg/data"),
            "OMNIGENT_RUNNER_ZYGOTE": "0",
            "OMNIGENT_AUTH_ENABLED": "0",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
            "OMNIGENT_FEATURES": "harness_install",
            "OMNIGENT_HARNESS_TMP_PARENT": str(self.socket_root),
            "OPENAI_AGENTS_DISABLE_TRACING": "1",
            "OMNIGENT_WEB_UI_DIST": str(self.checkout / "omnigent/server/static/web-ui"),
        }
        env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(env)
        return env

    def selfcheck(self) -> dict[str, Any]:
        result = subprocess.run(
            [str(self.python), str(Path(__file__).resolve()), "selfcheck"],
            env=self.environment("server"),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def read_cli_summary(self, host: str) -> dict[str, Any]:
        """Read metadata through the same production loader used by the CLI."""
        result = subprocess.run(
            [str(self.python), str(Path(__file__).resolve()), "config-summary"],
            env=self.environment(host),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def start(self) -> None:
        self.prepare()
        self.selfcheck()
        try:
            for component in ("mock-a", "mock-b", "server", "host-a", "host-b"):
                log = (self.root / f"{component}.log").open("wb")
                self.logs.append(log)
                self.processes.append(
                    subprocess.Popen(
                        [str(self.python), str(Path(__file__).resolve()), component],
                        env=self.environment(component),
                        cwd=self.root,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )
            self.wait_ready()
        except BaseException:
            self.stop()
            raise

    def wait_ready(self) -> None:
        import httpx

        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            for proc in self.processes:
                if proc.poll() is not None:
                    raise RuntimeError(f"Fixture component exited; inspect {self.root}/*.log")
            try:
                hosts = httpx.get(f"{self.url}/v1/hosts", timeout=2).json()
                rows = hosts if isinstance(hosts, list) else hosts.get("hosts", [])
                if len(rows) == 2 and all(row.get("status") == "online" for row in rows):
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.3)
        raise RuntimeError(f"Fixture not ready; inspect {self.root}/*.log")

    def stop(self) -> None:
        for proc in reversed(self.processes):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in reversed(self.processes):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        for log in self.logs:
            log.close()
        self.processes.clear()
        self.logs.clear()


def _component(component: str) -> None:
    install_guards()
    root = Path(os.environ["PROVIDER_FIXTURE_ROOT"])
    ports = json.loads(os.environ["PROVIDER_FIXTURE_PORTS"])
    if component == "selfcheck":
        from omnigent.host.connect import HostProcess
        from omnigent.host.identity import HostIdentity
        from omnigent.onboarding.ambient import detect_providers
        from omnigent.onboarding.harness_readiness import configured_harness_map
        from omnigent.onboarding.provider_config import load_config, load_providers

        for event, values in [
            ("ctypes.dlopen", ("/System/Library/Frameworks/Security.framework/Security",)),
            ("subprocess.Popen", ("security", ["security", "dummy"], None, {})),
            ("os.exec", ("/bin/sh", ["sh", "-c", "dummy"], {})),
            ("open", (str(Path.home() / ".codex/auth.json"), "r", 0)),
            ("os.listdir", (str(Path.home() / ".claude"),)),
            ("os.remove", (str(Path.home() / ".omnigent/config.yaml"), -1)),
        ]:
            try:
                sys.audit(event, *values)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Missing fixture guard for " + event)
        assert detect_providers() == []
        assert set(load_providers(load_config())) == {"fixture-primary", "fixture-secondary"}
        readiness = configured_harness_map()
        assert readiness["openai-agents"] is True
        host = HostProcess(
            HostIdentity(host_id=HOST_IDS[0], name="Fixture selfcheck"),
            f"http://127.0.0.1:{ports[0]}",
        )
        assert host._zygote is None
        asyncio.run(host._initialize_capabilities())
        import keyring

        try:
            keyring.get_password("fixture", "tripwire")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Missing keyring tripwire")
        print(
            json.dumps(
                {
                    "selfcheck": "passed",
                    "real_credentials": False,
                    "vendor_discovery": False,
                    "sdk_runtime": True,
                }
            )
        )
    elif component == "config-summary":
        from omnigent.onboarding.provider_config import load_config, load_providers

        config = load_config()
        print(
            json.dumps(
                {
                    "config_path": str(Path(os.environ["OMNIGENT_CONFIG_HOME"]) / "config.yaml"),
                    "providers": [
                        {
                            "name": name,
                            "kind": entry.kind,
                            "defaults": sorted(entry.default_families),
                            "models": {
                                family: block.models
                                for family in ("openai", "anthropic", "gemini")
                                if (block := entry.families.get(family)) is not None
                            },
                        }
                        for name, entry in load_providers(config).items()
                    ],
                }
            )
        )
    elif component.startswith("mock-"):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "provider_setup_mock", Path(__file__).with_name("provider_setup_mock.py")
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.serve(
            ports[1 if component == "mock-a" else 2],
            root / f"{component}-requests.jsonl",
            component,
        )
    elif component == "server":
        from omnigent.cli import cli

        cli.main(
            args=[
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports[0]),
                "--no-open",
                "--config",
                os.environ["OMNIGENT_CONFIG_HOME"] + "/config.yaml",
                "--database-uri",
                f"sqlite:///{root}/server/data/test.db",
                "--artifact-location",
                str(root / "server/data/artifacts"),
            ],
            prog_name="omnigent",
        )
    elif component.startswith("host-"):
        from omnigent.host.connect import HostProcess
        from omnigent.host.identity import HostIdentity

        index = 0 if component == "host-a" else 1
        host = HostProcess(
            HostIdentity(
                host_id=HOST_IDS[index], name=f"Fixture computer {'A' if index == 0 else 'B'}"
            ),
            f"http://127.0.0.1:{ports[0]}",
        )
        host._auth_token_factory = lambda: None
        host._auth_token_factory_resolved = True
        asyncio.run(host.run())
    else:
        raise ValueError(component)


if __name__ == "__main__":
    _component(sys.argv[1])
