#!/usr/bin/env python3
"""Select consumers of isolated dependency files; everything else runs full CI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import tomllib

DEPENDENCY_SCOPES = {
    "dev/omnidev/Cargo.lock": "omnidev",
    "dev/omnidev/Cargo.toml": "omnidev",
    "tests/codex_parity/sidecar/Cargo.lock": "sidecar",
    "tests/codex_parity/sidecar/Cargo.toml": "sidecar",
    ".pre-commit-config.yaml": "hooks",
    "uv.lock": "python",
    "pyproject.toml": "python",
    ".github/triage_v2/pyproject.toml": "python",
    "sdks/python-client/pyproject.toml": "python",
    "sdks/ui/pyproject.toml": "python",
    "integrations/slack/pyproject.toml": "python",
    "pnpm-lock.yaml": "javascript",
    "pnpm-workspace.yaml": "javascript",
    "package.json": "javascript",
    "web/package.json": "javascript",
    "web/electron/package.json": "javascript",
    "editors/vscode/package.json": "javascript",
    "deploy/cloudflare/package.json": "javascript",
    "sdks/web-extension/package.json": "javascript",
    "examples/extensions/hello-page/web/package.json": "javascript",
    ".github/ci-deps/package.json": "javascript",
    "web/ios/Gemfile": "ios",
    "web/ios/Gemfile.lock": "ios",
    "web/android/gradle/libs.versions.toml": "android",
    "web/android/gradle/wrapper/gradle-wrapper.properties": "android",
    "web/android/gradle/wrapper/gradle-wrapper.jar": "android",
    "web/android/gradlew": "android",
    "web/android/gradlew.bat": "android",
    "web/android/build.gradle.kts": "android",
    "web/android/app/build.gradle.kts": "android",
    "deploy/docker/Dockerfile": "docker",
    "deploy/docker/Dockerfile.ubi": "docker",
    "deploy/docker/Dockerfile.prebuilt": "docker",
    "deploy/cloudflare/Dockerfile": "docker",
    "deploy/hf-spaces/Dockerfile": "docker",
}


def pin_only(before: str, after: str, pattern: str) -> bool:
    """Require unchanged surrounding instructions and at least one changed pin."""
    normalized = [
        re.sub(pattern, r"\1<PIN>", text, flags=re.MULTILINE) for text in (before, after)
    ]
    return before != after and normalized[0] == normalized[1]


def workspace_pins_only(before: str, after: str) -> bool:
    normalized = []
    for text in (before, after):
        section = ""
        lines = []
        for line in text.splitlines():
            if line and not line[0].isspace() and not line.startswith("#"):
                section = line.split(":", 1)[0]
            if section in {"catalog", "catalogs", "overrides"}:
                line = re.sub(r"^([ \t]+[^:\n]+:[ \t]*)[\"']?[~^]?[0-9].*$", r"\1<PIN>", line)
            lines.append(line)
        normalized.append(lines)
    return before != after and normalized[0] == normalized[1]


def manifest_only(path: str, before: str, after: str) -> bool:
    """Manifest scripts, sources, entry points and build configuration keep full CI."""
    if path.endswith("package.json"):
        values = [json.loads(text) for text in (before, after)]
        sections = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
        for value in values:
            for section in sections:
                if section in value:
                    value[section] = sorted(value[section])
    elif path.endswith("pyproject.toml"):
        values = [tomllib.loads(text) for text in (before, after)]
        for value in values:
            project = value.get("project", {})
            project.pop("dependencies", None)
            project.pop("optional-dependencies", None)
            value.pop("dependency-groups", None)
    elif path.endswith("Cargo.toml"):
        values = [tomllib.loads(text) for text in (before, after)]
        for value in values:
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                for name, dependency in value.get(section, {}).items():
                    if isinstance(dependency, str):
                        value[section][name] = "<VERSION>"
                    elif isinstance(dependency, dict):
                        dependency.pop("version", None)
                        dependency.pop("rev", None)
                        dependency.pop("tag", None)
    else:
        return False
    return values[0] == values[1]


def file_scope(path: str, read=None) -> str:
    if (
        path.startswith((".github/workflows/", ".github/actions/"))
        and path.endswith((".yml", ".yaml"))
        and path != ".pre-commit-config.yaml"
    ):
        if read and pin_only(
            *read(path), r"^(\s*-?\s*uses:\s*[\w.-]+/[\w./-]+@)[\w./-]+(?:[ \t]+#.*)?$"
        ):
            return "actions"
        return "full"
    scope = DEPENDENCY_SCOPES.get(path, "full")
    if path == "pnpm-workspace.yaml":
        return scope if read and workspace_pins_only(*read(path)) else "full"
    if path.endswith(("package.json", "pyproject.toml", "Cargo.toml")):
        return scope if read and manifest_only(path, *read(path)) else "full"
    if path == ".pre-commit-config.yaml":
        return (
            scope
            if read and pin_only(*read(path), r"^(\s*-?\s*rev:\s*)\S+(?:[ \t]+#.*)?$")
            else "full"
        )
    if Path(path).name.startswith("Dockerfile"):
        pattern = r"^((?:FROM(?:\s+--platform=\S+)?\s+|ARG\s+\w*(?:VERSION|IMAGE)\w*=)).+$"
        return scope if read and pin_only(*read(path), pattern) else "full"
    return scope


def python_matrix(backend: bool) -> str:
    groups = json.loads(Path(__file__).with_name("python-matrix.json").read_text())
    return json.dumps({"include": groups if backend else []}, separators=(",", ":"))


def select_scope(files: list[dict], *, read=None, reason: str = "") -> dict[str, str]:
    """Bundles retain the union of consumers; unknown or non-modified files expand it."""
    scopes = {file_scope(file["filename"], read) for file in files}
    paths = {file["filename"] for file in files}
    full = bool(reason) or not files or "full" in scopes
    full |= any(file["status"] != "modified" for file in files)
    backend = full or bool(scopes & {"python", "actions"})
    ui = (
        "full"
        if full or scopes & {"python", "javascript", "actions"}
        else "sidecar"
        if "sidecar" in scopes
        else "none"
    )
    return {
        "dependency": str(not full).lower(),
        "backend": str(backend).lower(),
        "python_matrix": python_matrix(backend),
        "database": str(backend).lower(),
        "integration": str(backend).lower(),
        "e2e": str(backend or "javascript" in scopes).lower(),
        "windows": str(backend).lower(),
        "ios": str(
            full
            or "ios" in scopes
            or any(file["filename"] == ".github/workflows/web-tests.yml" for file in files)
        ).lower(),
        "web": str(full or bool(scopes & {"javascript", "actions"})).lower(),
        "docker": str(
            full
            or bool(scopes & {"python", "javascript", "docker", "actions"})
            or any(path.endswith("pyproject.toml") for path in paths)
        ).lower(),
        "omnidev": str(full or bool(scopes & {"omnidev", "actions"})).lower(),
        "sidecar": str(full or bool(scopes & {"sidecar", "actions"})).lower(),
        "ui": ui,
        "reason": reason
        or f"Dependency scopes: {', '.join(sorted(scopes)) if not full else 'full'}",
    }


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, timeout=30).strip("\n")


def pr_files(event: dict, revision: str) -> list[dict]:
    """Diff the exact tested merge against its base, without a moving API inventory."""
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
    if (
        len(parents) != 3
        or parents[0] != revision
        or parents[2] != event["pull_request"]["head"]["sha"]
    ):
        raise ValueError("Checkout is not the event's PR merge revision")
    changes = git("diff", "--no-renames", "--name-status", "-z", parents[1], parents[0])
    fields = changes.rstrip("\0").split("\0") if changes else []
    return [
        {"filename": name, "status": "modified" if status == "M" else status}
        for status, name in zip(fields[::2], fields[1::2], strict=True)
    ]


def main() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        selection = select_scope([], reason="Push, scheduled and manual runs use full CI")
    else:
        try:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            files = pr_files(event, os.environ["GITHUB_SHA"])
            selection = select_scope(
                files,
                read=lambda path: (git("show", f"HEAD^1:{path}"), git("show", f"HEAD:{path}")),
            )
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            print(f"Cannot determine dependency scope ({type(exc).__name__}); running full CI")
            selection = select_scope([], reason="Scope unavailable; running full CI")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in selection.items():
            output.write(f"{key}={value}\n")
    print(json.dumps(selection, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write("### Dependency CI selection\n\n```json\n")
            output.write(json.dumps(selection, indent=2) + "\n```\n")


if __name__ == "__main__":
    main()
