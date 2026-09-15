"""Opt-in fake Codex version and empty chat catalog for guarded browser tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests._helpers.provider_setup_runtime import ProviderSetupRuntime


def _codex_body(python: Path) -> str:
    return (
        f"#!{python}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] != ['--version']:\n"
        "    raise SystemExit(97)\n"
        "root = Path(os.environ['PROVIDER_FIXTURE_ROOT'])\n"
        "with (root / 'codex-version-probes.jsonl').open('a') as log:\n"
        "    log.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "print('codex-cli 0.100.0')\n"
    )


class ProviderSetupReadinessRuntime(ProviderSetupRuntime):
    """Run host A with an outdated dummy Codex and host B without Codex."""

    def prepare(self) -> None:
        super().prepare()
        binary_dir = self.root / "fixture-bin"
        binary_dir.mkdir(exist_ok=True)
        codex = binary_dir / "codex"
        codex.write_text(_codex_body(self.python))
        codex.chmod(0o700)
        tmux = binary_dir / "tmux"
        tmux.write_text("#!/bin/sh\nexit 98\n")
        tmux.chmod(0o700)

    def environment(self, component: str) -> dict[str, str]:
        env = super().environment(component)
        if component == "host-a":
            env["PATH"] = str(self.root / "fixture-bin") + ":/usr/bin:/bin"
            env["PROVIDER_FIXTURE_READINESS"] = "1"
            env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = ",".join(env)
        return env

    def selfcheck(self) -> dict[str, Any]:
        result = super().selfcheck()
        subprocess.run(
            [str(self.python), str(Path(__file__).resolve()), "guard-selfcheck"],
            env=self.environment("host-a"),
            cwd=self.root,
            check=True,
            timeout=30,
        )
        return result


def install(
    root: Path,
    python: Path,
    real_harness_cli_installed: Callable[..., bool],
    real_default_chat_model: Callable[..., str | None],
) -> Callable[[Any, Any, Any], bool]:
    """Permit one local version probe and use production readiness/catalog rules."""
    assert os.environ.get("PROVIDER_FIXTURE_READINESS") == "1"
    assert os.environ.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1"
    assert Path(os.environ["OMNIGENT_CONFIG_HOME"]).resolve().is_relative_to(root)
    fake = root / "fixture-bin/codex"
    expected_body = _codex_body(python)

    def resolve(binary: str, **_kwargs: Any) -> str | None:
        return str(fake) if binary == "codex" and fake.is_file() else None

    import omnigent._platform as platform
    import omnigent.onboarding.harness_install as harness_install
    import omnigent.onboarding.harness_readiness as readiness
    from omnigent.onboarding import providers

    platform.resolve_cli_binary = resolve
    harness_install.resolve_cli_binary = resolve
    readiness.resolve_cli_binary = resolve

    def cli_installed(key: str, **kwargs: Any) -> bool:
        if key == "openai":
            return real_harness_cli_installed(key, **kwargs)
        return key in {"anthropic", "pi"}

    harness_install.harness_cli_installed = cli_installed
    readiness.harness_cli_installed = cli_installed
    providers.default_chat_model = real_default_chat_model
    providers._fetch_provider_catalog = lambda provider: (
        {
            "schema_version": 1,
            "models": {"fixture-image-only": {"mode": "image"}},
        }
        if provider == "openai"
        else {}
    )

    def permitted(command: Any, env: Any, executable: Any) -> bool:
        if not isinstance(command, (list, tuple)) or list(command) != [str(fake), "--version"]:
            return False
        if executable != str(fake) or fake.read_text() != expected_body:
            return False
        effective_env = os.environ if env is None else env
        return (
            effective_env.get("PROVIDER_FIXTURE_ROOT") == str(root)
            and effective_env.get("PROVIDER_FIXTURE_READINESS") == "1"
            and effective_env.get("OMNIGENT_DISABLE_KEYRING") == "1"
            and effective_env.get("PYTHON_KEYRING_BACKEND") == "keyring.backends.null.Keyring"
            and effective_env.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1"
            and str(root / "boot") in effective_env.get("PYTHONPATH", "").split(os.pathsep)
            and effective_env.get("PROVIDER_FIXTURE_CHECKOUT")
            == os.environ.get("PROVIDER_FIXTURE_CHECKOUT")
            and effective_env.get("PROVIDER_FIXTURE_PORTS")
            == os.environ.get("PROVIDER_FIXTURE_PORTS")
            and effective_env.get("OMNIGENT_CONFIG_HOME") == os.environ.get("OMNIGENT_CONFIG_HOME")
            and Path(effective_env.get("OMNIGENT_CONFIG_HOME", "/")).resolve().is_relative_to(root)
            and "HOME" not in effective_env
            and "CODEX_HOME" not in effective_env
        )

    return permitted


def _guard_selfcheck() -> None:
    from tests._helpers.provider_setup_runtime import install_guards

    install_guards()
    root = Path(os.environ["PROVIDER_FIXTURE_ROOT"])
    fake = root / "fixture-bin/codex"
    command = [str(fake), "--version"]

    def audit(argv: list[str], env: dict[str, str] | None = None) -> None:
        sys.audit("subprocess.Popen", str(fake), argv, None, env)

    audit(command)
    tmux = root / "fixture-bin/tmux"
    for executable, argv, env in (
        (str(fake), [str(fake), "login"], None),
        (str(tmux), [str(tmux), "-V"], None),
        (str(fake), command, {**os.environ, "HOME": "/unsafe"}),
        (str(fake), command, {**os.environ, "PYTHON_KEYRING_BACKEND": "unsafe"}),
        (str(fake), command, {**os.environ, "PYTHONPATH": "/unsafe"}),
        (str(fake), command, {**os.environ, "OMNIGENT_CONFIG_HOME": "/unsafe"}),
        (str(fake), command, {**os.environ, "PROVIDER_FIXTURE_PORTS": "[]"}),
    ):
        try:
            sys.audit("subprocess.Popen", executable, argv, None, env)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Provider fixture allowed an unsafe Codex subprocess")

    original = fake.read_text()
    try:
        fake.write_text("corrupted fixture executable\n")
        try:
            audit(command)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Provider fixture allowed a modified Codex executable")
    finally:
        fake.write_text(original)

    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "codex-cli 0.100.0"
    ledger = root / "codex-version-probes.jsonl"
    assert [json.loads(line) for line in ledger.read_text().splitlines()] == [
        {"argv": ["--version"]}
    ]
    ledger.unlink()


if __name__ == "__main__" and sys.argv[1:] == ["guard-selfcheck"]:
    _guard_selfcheck()
