"""Check what an answer claims against the code of the build that produced it.

The referee pulls every CLI invocation, YAML block, dotted config key, tool
name, environment variable, doc path, and URL out of an answer and checks each
one against facts read from that build by ``referee_probe.py``:

- commands and flags against the installed click tree,
- YAML blocks and config keys by loading them through the build's own spec
  loaders, which report keys the parser never reads,
- tool, builtin, and harness names against the build's registries,
- ``OMNIGENT_*`` variables against the names the package reads,
- doc paths and GitHub URLs against the build's source tree, and other
  omnigent.ai URLs with an HTTP request.

A finding is a claim that does not exist in that build. Claims the answer
itself says do not exist ("there is no ``omnigent schedule`` command") are
kept but marked ``negated`` and are not counted as fabrications.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROBE = Path(__file__).with_name("referee_probe.py")

#: Finding kinds that count toward the fabrication rate.
FABRICATION_KINDS = frozenset(
    {
        "command",
        "option",
        "config_key",
        "tool",
        "builtin",
        "harness",
        "env_var",
        "doc_path",
        "url",
        "python_ref",
    }
)

_FENCE_RE = re.compile(r"```([\w+.-]*)[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_SHELL_LANGS = frozenset(
    {"", "bash", "sh", "shell", "console", "zsh", "terminal", "text", "cmd", "shellsession"}
)
_YAML_LANGS = frozenset({"yaml", "yml"})
_PROGRAMS = frozenset({"omnigent", "omni"})
_WRAPPERS = frozenset({"sudo", "env", "time", "exec", "command", "uvx", "nohup"})
_SYS_TOOL_RE = re.compile(r"\bsys_[a-z0-9_]*[a-z0-9]\b(?!\*)")
_ENV_RE = re.compile(r"\bOMNIGENT_[A-Z0-9_]*[A-Z0-9]\b")
_URL_RE = re.compile(r"https?://[^\s)\]>`'\"]+")
_DOC_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:docs|deploy|omnigent|examples|feature-map)/[\w./-]+\.md|README\.md)\b"
)
_DOTTED_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+$")
_FILE_SUFFIXES = (
    ".yaml",
    ".yml",
    ".md",
    ".json",
    ".py",
    ".toml",
    ".txt",
    ".sh",
    ".db",
    ".log",
    ".ai",
    ".com",
    ".io",
    ".org",
    ".dev",
    ".html",
    ".js",
    ".ts",
)
_PLACEHOLDER_RE = re.compile(r"^(<.*>|\{.*\}|\[.*\]|\.\.\.|…|\$\{?\w+\}?|X+|N)$")
_NEGATION_RE = re.compile(
    r"\b(no|not|never|without|instead of|rather than|deprecated|removed|unsupported|"
    r"doesn't|does not|isn't|is not|aren't|are not|don't|do not|"
    r"there's no|there is no|cannot|can't)\b",
    re.IGNORECASE,
)

#: Where a fragment whose top-level key is not a root key may have been cut
#: from. Name maps (tools, policies) are left out: they accept any key, so
#: nesting there would hide invented keys.
_FRAGMENT_PARENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "sandbox": (("os_env",),),
    "type": (("os_env", "sandbox"), ("os_env",)),
    "write_paths": (("os_env", "sandbox"),),
    "read_paths": (("os_env", "sandbox"),),
    "allow_network": (("os_env", "sandbox"),),
    "egress_rules": (("os_env", "sandbox"),),
    "cwd": (("os_env",),),
    "harness": (("executor",), ("executor", "config")),
    "model": (("executor",),),
    "profile": (("executor",),),
    "auth": (("executor",),),
    "config": (("executor",),),
    "reasoning_effort": (("executor",),),
    "builtins": (("tools",),),
    "labels": (("guardrails",),),
    "ask_timeout": (("guardrails",),),
}


def _candidate_parents(
    block: Mapping[str, Any], root_keys: frozenset[str]
) -> list[tuple[str, ...]]:
    """Placements to try for *block*: the root, plus parents its keys suggest."""
    keys = {str(k) for k in block}
    if not root_keys or keys <= root_keys:
        return [()]
    parents: list[tuple[str, ...]] = [()]
    for key in sorted(keys - root_keys):
        for parent in _FRAGMENT_PARENTS.get(key, ()):
            if parent not in parents:
                parents.append(parent)
    return parents


_SINGLE_BASE: dict[str, Any] = {
    "name": "probe",
    "prompt": "probe",
    "executor": {"harness": "claude-native"},
}
_BUNDLE_BASE: dict[str, Any] = {
    "spec_version": 1,
    "name": "probe",
    "prompt": "probe",
    "executor": {"type": "omnigent", "config": {"harness": "claude-native"}},
}


@dataclass(frozen=True)
class Surface:
    """Facts about one build, read by ``referee_probe.py surface``."""

    version: str
    cli: Mapping[str, Any]
    tools: frozenset[str]
    spec_builtins: frozenset[str]
    harnesses: frozenset[str]
    env_vars: frozenset[str]
    repo_paths: frozenset[str]
    config_set_keys: frozenset[str] = frozenset()
    spec_root_keys: frozenset[str] = frozenset()
    string_literals: frozenset[str] = frozenset()

    @classmethod
    def probe(cls, python: Path, *, source: Path) -> Surface:
        """
        Read the facts of the build installed for *python*.

        :param python: The build's interpreter, e.g. ``.eval/venv/a3/bin/python``.
        :param source: A checkout of the build's source commit, for doc paths.
        """
        facts = json.loads(_run_probe(python, "surface", None))
        files = subprocess.run(
            ["git", "-C", str(source), "ls-files"], capture_output=True, text=True, check=True
        ).stdout.split()
        return cls(
            version=facts["version"],
            cli=facts["cli"],
            tools=frozenset(facts["tools"]),
            spec_builtins=frozenset(facts["spec_builtins"]),
            harnesses=frozenset(facts["harnesses"]),
            env_vars=frozenset(facts["env_vars"]) | _deploy_env_vars(source, files),
            string_literals=frozenset(facts.get("string_literals", ())),
            repo_paths=frozenset(files),
            config_set_keys=frozenset(facts.get("config_set_keys", ())),
            spec_root_keys=frozenset(facts["spec_root_keys"]),
        )

    def to_json(self) -> dict[str, Any]:
        """Serializable form, for caching next to results."""
        return {
            "version": self.version,
            "cli": self.cli,
            "tools": sorted(self.tools),
            "spec_builtins": sorted(self.spec_builtins),
            "harnesses": sorted(self.harnesses),
            "env_vars": sorted(self.env_vars),
            "string_literals": sorted(self.string_literals),
            "repo_paths": sorted(self.repo_paths),
            "config_set_keys": sorted(self.config_set_keys),
            "spec_root_keys": sorted(self.spec_root_keys),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Surface:
        return cls(
            version=raw["version"],
            cli=raw["cli"],
            tools=frozenset(raw["tools"]),
            spec_builtins=frozenset(raw["spec_builtins"]),
            harnesses=frozenset(raw["harnesses"]),
            env_vars=frozenset(raw["env_vars"]),
            string_literals=frozenset(raw.get("string_literals", ())),
            repo_paths=frozenset(raw["repo_paths"]),
            config_set_keys=frozenset(raw.get("config_set_keys", ())),
            spec_root_keys=frozenset(raw.get("spec_root_keys", ())),
        )

    def command_exists(self, path: Sequence[str]) -> bool:
        node: Mapping[str, Any] = self.cli
        for name in path:
            node = node["commands"].get(name)  # type: ignore[assignment]
            if node is None:
                return False
        return True


@dataclass(frozen=True)
class Finding:
    """One claim that does not exist in the build."""

    kind: str
    text: str
    detail: str = ""
    negated: bool = False


@dataclass
class Review:
    """What the referee found in one answer."""

    commands: list[tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    config_paths: set[str] = field(default_factory=set)
    tools_named: set[str] = field(default_factory=set)
    yaml_blocks: int = 0
    yaml_invalid: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def fabrications(self) -> list[Finding]:
        """Findings that count toward the fabrication rate."""
        return [f for f in self.findings if f.kind in FABRICATION_KINDS and not f.negated]

    def uses_command(self, path: Sequence[str], options: Sequence[str] = ()) -> bool:
        """Whether a valid invocation of *path* carries every option in *options*."""
        target = tuple(path)
        return any(p == target and set(options) <= set(opts) for p, opts in self.commands)

    def to_json(self) -> dict[str, Any]:
        return {
            "commands": [[list(p), list(o)] for p, o in self.commands],
            "config_paths": sorted(self.config_paths),
            "tools_named": sorted(self.tools_named),
            "yaml_blocks": self.yaml_blocks,
            "yaml_invalid": self.yaml_invalid,
            "findings": [f.__dict__ for f in self.findings],
        }


def _run_probe(python: Path, command: str, payload: Any) -> str:
    # abspath, not resolve(): resolving a venv's python symlink leaves the venv.
    result = subprocess.run(
        [os.path.abspath(python), "-I", str(PROBE), command],
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        cwd="/",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"referee probe {command} failed: {result.stderr[-2000:]}")
    return result.stdout


# ── Extraction ──────────────────────────────────────────────────────────


@dataclass
class _Extracted:
    shell_lines: list[tuple[str, bool]] = field(default_factory=list)  # (line, negated)
    yaml_blocks: list[Any] = field(default_factory=list)
    inline: list[tuple[str, bool]] = field(default_factory=list)


def _negated_at(text: str, start: int) -> bool:
    """Whether a negation shortly precedes position *start* in the same sentence."""
    before = text[max(0, start - 80) : start]
    sentence = re.split(r"[.;!?\n]", before)[-1]
    return bool(_NEGATION_RE.search(" ".join(sentence.split()[-6:])))


def _extract(answer: str) -> _Extracted:
    out = _Extracted()
    for match in _FENCE_RE.finditer(answer):
        lang, body = match.group(1).lower(), match.group(2)
        loaded = _yaml_mapping(body) if lang in _YAML_LANGS or lang == "" else None
        if lang in _YAML_LANGS or (
            lang == "" and loaded is not None and not _looks_like_shell(body)
        ):
            if loaded is not None:
                out.yaml_blocks.append(loaded)
            else:
                out.yaml_blocks.append(_YamlSyntaxError(body))
            continue
        if lang in _SHELL_LANGS:
            body = body.replace("\\\n", " ")
            out.shell_lines.extend((line, False) for line in body.splitlines())
    prose = _FENCE_RE.sub(" ", answer)
    for match in _INLINE_RE.finditer(prose):
        span = match.group(1).strip()
        negated = _negated_at(prose, match.start())
        out.inline.append((span, negated))
        if span.split(" ", 1)[0] in _PROGRAMS or span.startswith(("$ omni", "uv run omni")):
            out.shell_lines.append((span, negated))
    return out


@dataclass(frozen=True)
class _YamlSyntaxError:
    body: str


def _yaml_mapping(body: str) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _looks_like_shell(body: str) -> bool:
    first = body.strip().split(None, 1)[0] if body.strip() else ""
    return first.lstrip("$") in _PROGRAMS or first in {
        "uv",
        "pip",
        "curl",
        "brew",
        "cd",
        "export",
        "git",
    }


# ── CLI ─────────────────────────────────────────────────────────────────


def _segments(line: str) -> Iterable[list[str]]:
    line = re.sub(r"^\s*[$%>]\s*", "", line)
    line = re.split(r"\s+#", line, maxsplit=1)[0]
    for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", line):
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        while tokens and (
            ("=" in tokens[0] and not tokens[0].startswith("-")) or tokens[0] in _WRAPPERS
        ):
            tokens = tokens[1:]
        if tokens[:2] == ["uv", "run"]:
            tokens = tokens[2:]
        if tokens and Path(tokens[0]).name in _PROGRAMS:
            yield tokens


def _is_placeholder(token: str) -> bool:
    return (
        bool(_PLACEHOLDER_RE.match(token)) or token.startswith(("<", "$")) or token.endswith(">")
    )


def _url_or_path(token: str) -> bool:
    """A URL or path, which a group may take as an argument (``omnigent host <url>``)."""
    return (
        "://" in token
        or token.startswith(("/", "./", "~"))
        or bool(re.match(r"^[\w.-]+\.[a-z]{2,}(:\d+)?(/|$)|^[\w.-]+:\d+(/|$)", token))
    )


def _walk_cli(
    tokens: Sequence[str], root: Mapping[str, Any]
) -> tuple[tuple[str, ...], list[str], list[Finding]]:
    """Validate one invocation; return its command path, options, and findings."""
    node, path, options, findings = root, [], [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--":
            break
        if _is_placeholder(token):
            if node["commands"] and not path[len(path) :]:
                break  # a placeholder where a subcommand goes: nothing more to check
            continue
        if token.startswith("-") and len(token) > 1:
            name = token.split("=", 1)[0]
            known = name in node["options"] or (node is not root and name in root["options"])
            if known:
                options.append(name)
                if name not in node["flags"] and "=" not in token and name not in root["flags"]:
                    index += 1  # the option's value
            elif not node["passthrough"]:
                where = " ".join(("omnigent", *path))
                findings.append(Finding("option", f"{where} {name}", "no such option"))
            continue
        if node["commands"]:
            sub = node["commands"].get(token)
            if sub is None and _url_or_path(token):
                continue  # a group that takes a URL, e.g. `omnigent host https://...`
            if sub is None:
                where = " ".join(("omnigent", *path, token))
                findings.append(Finding("command", where, "no such command"))
                break
            node = sub
            path.append(token)
            continue
        # A positional argument; later tokens may still be options.
    return tuple(path), options, findings


# ── Review ──────────────────────────────────────────────────────────────


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _nest(fragment: Mapping[str, Any], parents: Sequence[str]) -> dict[str, Any]:
    nested: Any = dict(fragment)
    for parent in reversed(parents):
        nested = {parent: nested}
    return nested


def _paths(mapping: Any, prefix: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(mapping, Mapping):
        for key, value in mapping.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            found.add(child)
            found |= _paths(value, child)
    return found


def _own_unread(block: Any, parents: Sequence[str], result: Mapping[str, Any]) -> list[str]:
    """Unread keys that belong to *block* placed under *parents*.

    A load that fails part-way leaves base keys unread too, and those say
    nothing about the answer.
    """
    prefix = ".".join(parents)
    own = {f"{prefix}.{p}" if prefix else p for p in _paths(block)}
    return [key for key in result["unread"] if key in own]


def _scalars(mapping: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(mapping, Mapping):
        for key, value in mapping.items():
            found.add(str(key))
            found |= _scalars(value)
    elif isinstance(mapping, list):
        for item in mapping:
            found |= _scalars(item)
    elif mapping is not None:
        found.add(str(mapping))
    return {s for s in found if len(s) >= 3}


class Referee:
    """
    Reviews answers against one build.

    :param surface: The build's facts.
    :param python: The build's interpreter, used to load YAML and import paths.
    :param site_routes: Page paths in the omnigent.ai source repo, e.g.
        ``"/docs/policies/overview"``; empty skips omnigent.ai link checks.
    """

    def __init__(self, surface: Surface, python: Path, site_routes: Iterable[str] = ()) -> None:
        self.surface = surface
        self.python = python
        # Routes in the omnigent.ai source repo; empty skips omnigent.ai checks.
        self.site_routes = frozenset(site_routes) | (_SITE_EXTRAS if site_routes else frozenset())

    def review(
        self, answers: Sequence[str], agent_dirs: Sequence[Sequence[str]] = ()
    ) -> list[Review]:
        """
        Review each answer; YAML and import checks run in one batch per call.

        :param answers: Answer texts.
        :param agent_dirs: Per answer, agent directories the agent wrote to
            load as-is (may be empty).
        """
        reviews = [Review() for _ in answers]
        extracted = [_extract(answer) for answer in answers]
        yaml_jobs: list[tuple[int, int, tuple[str, ...], dict[str, Any] | None, bool]] = []
        yaml_payload: list[dict[str, Any]] = []
        pyrefs: list[tuple[int, str]] = []

        for index, (answer, ex) in enumerate(zip(answers, extracted, strict=True)):
            review = reviews[index]
            for line, negated in ex.shell_lines:
                for tokens in _segments(line):
                    path, options, findings = _walk_cli(tokens[1:], self.surface.cli)
                    if (
                        path in (("config", "set"), ("config", "unset"))
                        and self.surface.config_set_keys
                    ):
                        for token in tokens[3:]:
                            name = token.split("=", 1)[0]
                            if token.startswith("-") or _is_placeholder(name):
                                continue
                            if name not in self.surface.config_set_keys:
                                findings.append(
                                    Finding(
                                        "config_key",
                                        name,
                                        "not a key `omnigent config set` accepts",
                                    )
                                )
                            break
                    review.findings.extend(
                        Finding(f.kind, f.text, f.detail, negated) for f in findings
                    )
                    if not findings and path:
                        review.commands.append((path, tuple(options)))
            for name in set(_SYS_TOOL_RE.findall(answer)):
                if name in self.surface.tools:
                    review.tools_named.add(name)
                else:
                    negated = _negated_at(answer, answer.find(name))
                    review.findings.append(Finding("tool", name, "no such tool", negated))
            for name in set(_ENV_RE.findall(answer)):
                if name not in self.surface.env_vars:
                    negated = _negated_at(answer, answer.find(name))
                    review.findings.append(
                        Finding("env_var", name, "the package never reads it", negated)
                    )
            for doc in set(_DOC_PATH_RE.findall(_URL_RE.sub(" ", answer))):
                if doc not in self.surface.repo_paths:
                    review.findings.append(Finding("doc_path", doc, "not in the source tree"))
            for url in set(_URL_RE.findall(answer)):
                problem = self._check_url(url.rstrip(".,:;"))
                if problem:
                    review.findings.append(Finding("url", url, problem))
            for span, negated in ex.inline:
                if not _DOTTED_KEY_RE.match(span) or span.endswith(_FILE_SUFFIXES):
                    continue
                if span.startswith("omnigent."):
                    pyrefs.append((index, span))
                    continue
                root = span.split(".", 1)[0]
                if root in USER_CONFIG_ROOTS:
                    missing = self._unread_user_key(span)
                    if missing:
                        review.findings.append(
                            Finding("config_key", span, f"no reader uses {missing!r}", negated)
                        )
                    else:
                        review.config_paths.add(span)
                    continue
                if root == "sandbox":
                    span = f"os_env.{span}"  # informal shorthand for the agent's sandbox
                elif root not in self.surface.spec_root_keys:
                    continue  # e.g. `os.environ`: not a config key
                fragment = _nest({span.rsplit(".", 1)[-1]: "probe-value"}, span.split(".")[:-1])
                for single in (True, False):
                    base = _SINGLE_BASE if single else _BUNDLE_BASE
                    yaml_jobs.append((index, -1, (span, str(negated)), fragment, single))
                    yaml_payload.append(
                        {
                            "text": yaml.safe_dump(_deep_merge(base, fragment)),
                            "single_file": single,
                        }
                    )
            for block_index, block in enumerate(ex.yaml_blocks):
                review.yaml_blocks += 1
                if isinstance(block, _YamlSyntaxError):
                    review.yaml_invalid += 1
                    review.findings.append(
                        Finding("yaml_syntax", block.body[:120], "not valid YAML")
                    )
                    continue
                if not self._is_agent_config(block):
                    self._check_user_config_block(review, block)
                    continue
                # Without spec_version the block may be a single-file agent or a
                # bundle config.yaml shown without its header; accept either.
                formats = (False,) if "spec_version" in block else (True, False)
                for single in formats:
                    for parents in _candidate_parents(block, self.surface.spec_root_keys):
                        candidate = _nest(block, parents)
                        base = _SINGLE_BASE if single else _BUNDLE_BASE
                        yaml_jobs.append((index, block_index, parents, block, single))
                        yaml_payload.append(
                            {
                                "text": yaml.safe_dump(_deep_merge(base, candidate)),
                                "single_file": single,
                            }
                        )
            for directory in agent_dirs[index] if index < len(agent_dirs) else ():
                yaml_jobs.append((index, -2, (directory,), None, False))
                yaml_payload.append({"path": directory})

        results = json.loads(_run_probe(self.python, "yaml", yaml_payload)) if yaml_payload else []
        self._apply_yaml(reviews, yaml_jobs, results)
        if pyrefs:
            found = json.loads(_run_probe(self.python, "pyrefs", [ref for _, ref in pyrefs]))
            for (index, ref), ok in zip(pyrefs, found, strict=True):
                if not ok:
                    reviews[index].findings.append(Finding("python_ref", ref, "does not import"))
        return reviews

    def validate_key(
        self, kind: str, values: Sequence[str], options: Sequence[str] = ()
    ) -> list[str]:
        """
        Problems with an answer-key check against this build: every command,
        flag, tool, and config key it names must exist here.

        :param kind: The check kind, e.g. ``"cli"``.
        :param values: The check's values.
        :param options: For ``cli``: required flags.
        """
        problems: list[str] = []
        if kind == "cli":
            for value in values:
                path = value.split()
                if not self.surface.command_exists(path):
                    problems.append(f"no command `omnigent {value}`")
                    continue
                node: Mapping[str, Any] = self.surface.cli
                for name in path:
                    node = node["commands"][name]
                problems += [
                    f"`omnigent {value}` has no {o}" for o in options if o not in node["options"]
                ]
        elif kind in ("tool", "tool_called"):
            known = (
                self.surface.tools
                | self.surface.spec_builtins
                | {"Skill", "Bash", "WebFetch", "Read", "Write", "Edit"}
            )
            problems += [f"no tool {v}" for v in values if v not in known]
        elif kind == "config":
            reviews = self.review([" ".join(f"`{v}`" for v in values)])
            problems += [
                f"config key {f.text} is never read"
                for f in reviews[0].findings
                if f.kind == "config_key"
            ]
        return problems

    def _unread_user_key(self, dotted: str) -> str | None:
        """The root or leaf of a user-config key that no reader names, if any.

        Middle segments are skipped: they are often user-chosen names, such as
        a provider name under ``providers``.
        """
        if not self.surface.string_literals:
            return None
        parts = dotted.split(".")
        for part in {parts[0], parts[-1]}:
            if part not in self.surface.string_literals:
                return part
        return None

    def _check_user_config_block(self, review: Review, block: Mapping[str, Any]) -> None:
        """Check the leaf keys of a ``~/.omnigent/config.yaml`` block against reader names."""
        roots = {str(k) for k in block} & USER_CONFIG_ROOTS
        if not roots:
            return
        for root in sorted(roots):
            for path in sorted(set(_leaf_paths(block[root], root))):
                missing = self._unread_user_key(path)
                if missing:
                    review.findings.append(
                        Finding("config_key", path, f"no reader uses {missing!r}")
                    )
                else:
                    review.config_paths.add(path)

    def _is_agent_config(self, block: Mapping[str, Any]) -> bool:
        keys = {str(k) for k in block}
        user_roots = USER_CONFIG_ROOTS - {"server", "host", "model", "harness"}
        if keys & {
            "name",
            "prompt",
            "instructions",
            "spec_version",
            "executor",
            "os_env",
            "policies",
            "guardrails",
            "skills",
        }:
            return True
        if keys & user_roots and not keys & {"sandbox", "tools", "harness"}:
            return False
        return bool(
            keys
            & {"sandbox", "tools", "harness", "type", "write_paths", "allow_network", "builtins"}
        )

    def _apply_yaml(
        self, reviews: list[Review], jobs: list[Any], results: list[dict[str, Any]]
    ) -> None:
        by_block: dict[
            tuple[int, int], list[tuple[Any, dict[str, Any], dict[str, Any] | None]]
        ] = {}
        dotted: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
        for (index, block_index, parents, block, _single), result in zip(
            jobs, results, strict=True
        ):
            if block_index == -1:
                span, negated = parents
                dotted.setdefault((index, span, negated), []).append(result)
            elif block_index == -2:
                reviews[index].yaml_blocks += 1
                if result["error"] or result["unread"]:
                    reviews[index].yaml_invalid += 1
                for key in result["unread"]:
                    reviews[index].findings.append(
                        Finding("config_key", key, f"never read in {parents[0]}")
                    )
                if result["error"]:
                    reviews[index].findings.append(
                        Finding("config_value", parents[0], result["error"])
                    )
            else:
                by_block.setdefault((index, block_index), []).append((parents, result, block))
        for (index, span, negated), outcomes in dotted.items():
            leaf_read = any(
                span not in r["unread"] and not any(span.startswith(u) for u in r["unread"])
                for r in outcomes
            )
            if leaf_read:
                reviews[index].config_paths.add(span)
            else:
                reviews[index].findings.append(
                    Finding("config_key", span, "never read by the spec parser", negated == "True")
                )
        for (index, _block_index), candidates in by_block.items():
            review = reviews[index]
            block = candidates[0][2]
            self._check_names(review, block)

            clean = [(p, r) for p, r, _ in candidates if r["error"] is None]
            if clean:
                parents, result = min(
                    clean, key=lambda c: (len(_own_unread(block, *c)), len(c[0]))
                )
                unread = _own_unread(block, parents, result)
                prefix = ".".join(parents)
                review.config_paths |= {
                    path
                    for path in (f"{prefix}.{p}" if prefix else p for p in _paths(block))
                    if not any(path == u or path.startswith(u + ".") for u in unread)
                }
                if unread:
                    review.yaml_invalid += 1
                for key in unread:
                    shown = (
                        key[len(prefix) + 1 :] if prefix and key.startswith(prefix + ".") else key
                    )
                    review.findings.append(
                        Finding("config_key", shown, "never read by the spec parser")
                    )
                continue
            # Every placement failed to load; keys after the failure were never
            # parsed, so report the error rather than unread keys.
            review.yaml_invalid += 1
            error = candidates[0][1]["error"] or ""
            if any(value in error for value in _scalars(block)):
                review.findings.append(
                    Finding("config_value", error[:200], "the spec loader rejects it")
                )
            else:
                review.findings.append(
                    Finding("config_error", error[:200], "load failed; cause unclear")
                )

    def _check_names(self, review: Review, block: Mapping[str, Any]) -> None:
        tools = block.get("tools")
        builtins = tools.get("builtins") if isinstance(tools, Mapping) else None
        for entry in builtins if isinstance(builtins, list) else []:
            name = entry.get("name") if isinstance(entry, Mapping) else entry
            if isinstance(name, str) and name not in self.surface.spec_builtins:
                review.findings.append(Finding("builtin", name, "not a builtin a spec can enable"))
        executor = block.get("executor")
        if isinstance(executor, Mapping):
            config = executor.get("config")
            harness = executor.get("harness") or (
                config.get("harness") if isinstance(config, Mapping) else None
            )
            if isinstance(harness, str) and harness not in self.surface.harnesses:
                review.findings.append(Finding("harness", harness, "not a known harness"))

    # ── URLs ────────────────────────────────────────────────────────────

    def _check_url(self, url: str) -> str | None:
        github = re.match(
            r"https?://(?:raw\.githubusercontent\.com/omnigent-ai/omnigent/|github\.com/omnigent-ai/omnigent/(?:blob|tree)/)([^/]+)/(.+)",
            url,
        )
        if github:
            ref, path = github.group(1), github.group(2).split("#")[0].split("?")[0]
            if "<" in ref or "{" in ref or "<" in path:
                return None
            return None if _git_has(ref, path) else f"{path} is not at {ref}"
        site = re.match(r"https?://(?:www\.)?omnigent\.ai(/[^?#]*)?", url)
        if site and self.site_routes and not re.search(r"[<{]", url):
            path = (site.group(1) or "/").rstrip("/") or "/"
            return None if path in self.site_routes else f"no {path} page on omnigent.ai"
        return None


#: Top-level keys of ``~/.omnigent/config.yaml`` that some reader looks up.
#: Its readers are scattered and nested keys have no schema, so dotted keys
#: under these roots are accepted without further checks.
USER_CONFIG_ROOTS = frozenset(
    {
        "auth",
        "providers",
        "host",
        "server",
        "tui",
        "runner",
        "telemetry",
        "inference",
        "harness",
        "model",
        "default_agent",
        "opencode_model",
        "session_title_instructions",
        "auto_open_conversation",
        "copilot",
        "acp",
    }
)


def _leaf_paths(node: Any, prefix: str) -> list[str]:
    """Dotted paths of the scalar-valued keys under *node*."""
    if isinstance(node, Mapping):
        found: list[str] = []
        for key, value in node.items():
            found.extend(_leaf_paths(value, f"{prefix}.{key}"))
        return found
    if isinstance(node, list):
        return [path for item in node for path in _leaf_paths(item, prefix)]
    return [prefix]


_DEPLOY_ENV_SUFFIXES = (".py", ".sh", ".yaml", ".yml", ".toml", ".json", ".env")


def _deploy_env_vars(source: Path, files: Iterable[str]) -> frozenset[str]:
    """``OMNIGENT_*`` names read by deploy and install code outside the package."""
    found: set[str] = set()
    for name in files:
        if not name.startswith(("deploy/", "scripts/")):
            continue
        if not (name.endswith(_DEPLOY_ENV_SUFFIXES) or Path(name).name.startswith("Dockerfile")):
            continue
        try:
            found.update(_ENV_RE.findall((source / name).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return frozenset(found)


#: Generated at site build time, so absent from the site repo's tree.
_SITE_EXTRAS = frozenset({"/docs", "/llms.txt", "/llms-full.txt"})

_GIT_CACHE: dict[tuple[str, str], bool] = {}


def _git_has(ref: str, path: str) -> bool:
    """Whether *path* exists at *ref* (``main`` means ``origin/main``) in this repo."""
    key = (ref, path.rstrip("/"))
    if key not in _GIT_CACHE:
        resolved = "origin/main" if ref == "main" else ref
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{resolved}:{key[1]}"],
            capture_output=True,
            cwd=Path(__file__).resolve().parent,
            check=False,
        )
        _GIT_CACHE[key] = result.returncode == 0
    return _GIT_CACHE[key]
