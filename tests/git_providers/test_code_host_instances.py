"""Code host instance config: loading, refusals, one-time CLI resolution, and lookup."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.git_providers.code_host_instances import (
    NO_INSTANCES,
    CodeHostInstanceError,
    CodeHostInstances,
    load_code_host_instances,
    parse_code_host_instances,
)
from tests.budgets import budget

KNOWN = frozenset({"line_counts", "linked_pr_diff", "review_threads", "comment_posting"})
SECRET_ARGUMENT = "s3cr3t-argument-value"


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    """An executable file standing in for an instance's CLI."""
    path = tmp_path / "bin" / "example-cli"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _config(program: Path | str, **fields: str) -> str:
    """One instance's TOML running *program*, with *fields* replacing or adding raw TOML."""
    values = {
        "id": '"internal"',
        "provider": '"github"',
        "display_name": '"Internal code host"',
        "hosts": '["code.example.test"]',
        "cli": f'["{program}"]',
        "auth": '"cli"',
        "capabilities": '["line_counts", "review_threads"]',
        **fields,
    }
    return "[[instance]]\n" + "".join(f"{name} = {value}\n" for name, value in values.items())


def _parse(text: str, **options: object) -> CodeHostInstances:
    return parse_code_host_instances(
        text, known_capabilities=KNOWN, host_path=options.pop("host_path", "/usr/bin"), **options
    )


def test_an_instance_loads_with_its_resolved_command_and_ceiling(cli: Path) -> None:
    instances = _parse(_config(cli, env='["HOME"]', env_set='{ LANG = "C.UTF-8" }'))

    (instance,) = instances.instances
    assert instance.id == "internal"
    assert instance.provider == "github"
    assert instance.display_name == "Internal code host"
    assert instance.hosts == frozenset({"code.example.test"})
    assert instance.command == (str(cli),)
    assert instance.passed_environment == ("HOME",)
    assert instance.fixed_environment == {"LANG": "C.UTF-8"}
    assert instance.capabilities == frozenset({"line_counts", "review_threads"})


def test_instances_are_found_by_provider_and_host(cli: Path) -> None:
    instances = _parse(_config(cli, hosts='["code.example.test", "mirror.example.test"]'))

    assert instances.hosts_for("github") == {"code.example.test", "mirror.example.test"}
    assert instances.hosts_for("gitlab") == frozenset()
    assert instances.instance_for("github", "Mirror.Example.Test") is instances.instances[0]
    assert instances.instance_for("gitlab", "code.example.test") is None
    assert instances.instance_for("github", "other.example.test") is None
    assert instances.instance_for("github", None) is None
    assert NO_INSTANCES.instance_for("github", "code.example.test") is None


def test_a_missing_file_declares_no_instances(tmp_path: Path) -> None:
    instances = load_code_host_instances(
        tmp_path / "code_host_instances.toml",
        known_capabilities=KNOWN,
        host_environment={"PATH": "/usr/bin"},
    )

    assert instances is NO_INSTANCES
    assert instances.hosts_for("github") == frozenset()


def test_a_file_is_read_with_the_hosts_path(tmp_path: Path, cli: Path) -> None:
    config = tmp_path / "code_host_instances.toml"
    config.write_text(_config("example-cli"))
    seen: list[str] = []

    def which(name: str, *, path: str) -> str | None:
        seen.append(path)
        return str(cli) if name == "example-cli" else None

    instances = load_code_host_instances(
        config,
        known_capabilities=KNOWN,
        host_environment={"PATH": f"{cli.parent}:/usr/bin"},
        which=which,
    )

    assert instances.instances[0].command == (str(cli),)
    assert seen == [f"{cli.parent}:/usr/bin"]


def test_a_bare_name_resolves_once_and_is_refused_once_it_disappears(cli: Path) -> None:
    lookups: list[str] = []

    def which(name: str, *, path: str) -> str | None:
        lookups.append(name)
        return str(cli)

    (instance,) = _parse(_config("example-cli"), which=which).instances
    assert instance.runnable_command() == [str(cli)]
    cli.unlink()
    replacement = cli.parent.parent / "example-cli"
    replacement.write_text("#!/bin/sh\n")
    replacement.chmod(0o755)

    with pytest.raises(CodeHostInstanceError) as refused:
        instance.runnable_command()

    assert str(refused.value) == (
        "instance internal is refused: its CLI is no longer at the path resolved when the "
        "host started"
    )
    assert lookups == ["example-cli"]


def test_arguments_stay_literal_with_no_shell(cli: Path) -> None:
    (instance,) = _parse(_config(cli, cli=f'["{cli}", "pr; touch /tmp/example"]')).instances

    assert instance.runnable_command() == [str(cli), "pr; touch /tmp/example"]


@pytest.mark.parametrize(
    ("fields", "rule"),
    [
        ({"cli": '"example-cli pr"'}, "cli must be a non-empty list"),
        ({"cli": "[]"}, "cli must be a non-empty list"),
        ({"cli": '["./example-cli"]'}, "use an absolute path or a bare name on PATH"),
        ({"cli": '["bin/example-cli"]'}, "use an absolute path or a bare name on PATH"),
        ({"cli": '["example-cli; true"]'}, "the name isn't on the host's PATH"),
        ({"cli": '["/nonexistent/example-cli"]'}, "it isn't an executable file"),
        ({"cli": f'["/bin/sh", "{"x" * 5000}"]'}, "each argument must be a short string"),
        ({"id": '"Internal"'}, "id must match"),
        ({"provider": '"gitlab"'}, "provider must be one of"),
        ({"auth": '"token"'}, 'auth is refused: it must be "cli"'),
        ({"display_name": '""'}, "display_name is refused"),
        ({"hosts": "[]"}, "it must list at least one host"),
        ({"hosts": '["github.com"]'}, "belongs to its provider by name"),
        ({"hosts": '["Code.example.test"]'}, "a host must be a lower-case host name"),
        ({"hosts": '["code.example.test/path"]'}, "a host must be a lower-case host name"),
        ({"hosts": '["localhost"]'}, "a host must be a lower-case host name"),
        ({"hosts": '["code.example.test", "code.example.test"]'}, "it is listed twice"),
        ({"capabilities": '["merging"]'}, "unknown capability"),
        ({"capabilities": '["line_counts", "line_counts"]'}, "it is listed twice"),
        ({"command": '["/bin/sh"]'}, "unknown field command"),
        ({"env": '["GH_TOKEN"]'}, "token-variable rule"),
        ({"env": '["SERVICE_API_KEY"]'}, "secret-name rule"),
    ],
)
def test_a_broken_rule_refuses_the_file(cli: Path, fields: dict[str, str], rule: str) -> None:
    with pytest.raises(CodeHostInstanceError, match=re.escape(rule)):
        _parse(_config(cli, **fields), which=lambda _name, path: None)


def test_one_broken_instance_refuses_every_instance(cli: Path) -> None:
    good = _config(cli)
    broken = _config(cli, id='"second"', hosts='["other.example.test"]', auth='"token"')

    with pytest.raises(CodeHostInstanceError, match="instance second auth is refused"):
        _parse(good + broken)


@pytest.mark.parametrize(
    ("second", "rule"),
    [
        ({"hosts": '["other.example.test"]'}, "its id is used twice"),
        ({"id": '"second"'}, "a host is also claimed by instance internal"),
    ],
)
def test_instances_cannot_share_an_id_or_a_host(
    cli: Path, second: dict[str, str], rule: str
) -> None:
    with pytest.raises(CodeHostInstanceError, match=re.escape(rule)):
        _parse(_config(cli) + _config(cli, **second))


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("[[instance]\n", "it isn't valid TOML"),
        ("version = 1\n", "unknown field version"),
        ('instance = "x"\n', "use [[instance]] tables"),
    ],
)
def test_a_malformed_file_is_refused(text: str, rule: str) -> None:
    with pytest.raises(CodeHostInstanceError, match=re.escape(rule)):
        _parse(text)


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "code_host_instances.toml"
    config.write_bytes(b"\xff\xfe not text")

    with pytest.raises(CodeHostInstanceError, match="it can't be read"):
        load_code_host_instances(
            config, known_capabilities=KNOWN, host_environment={"PATH": "/usr/bin"}
        )


def test_refusals_never_repeat_a_configured_value(cli: Path) -> None:
    secret_cli = f'["{cli}", "{SECRET_ARGUMENT}{"x" * 5000}"]'
    cases = [
        _config(cli, cli=secret_cli),
        _config(cli, env_set=f'{{ LANG = "C", PROJECT_KEY = "{SECRET_ARGUMENT}" }}'),
        _config(cli, cli=f'["{SECRET_ARGUMENT}/example-cli"]'),
    ]
    for text in cases:
        with pytest.raises(CodeHostInstanceError) as refused:
            _parse(text)
        assert SECRET_ARGUMENT not in str(refused.value)


def test_the_instance_modules_import_only_the_standard_library() -> None:
    roots = [path for path in sys.path if path]
    probe = (
        f"import sys\nsys.path[:0] = {roots!r}\nbefore = set(sys.modules)\n"
        "import omnigent.git_providers.code_host_instances\n"
        "loaded = set(sys.modules) - before\n"
        "print('\\n'.join(sorted(loaded)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
    outside = [
        name
        for name in result.stdout.split()
        if name.partition(".")[0] not in sys.stdlib_module_names
        and name not in {"omnigent", "omnigent._env_compat"}
        and not name.startswith("omnigent.git_providers")
    ]
    assert outside == []
