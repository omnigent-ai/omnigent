"""Validate dependency diffs as data using policy and registry metadata from trusted sources."""

from __future__ import annotations

import copy
import fnmatch
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

import tomllib
import yaml
from maintainer_approval import gh_json, paginated


class NeedsReview(ValueError):
    """The update needs human review."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise NeedsReview(reason)


def read_yaml(text: str) -> dict:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            require(key not in result, f"Duplicate YAML key: {key}")
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        result = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise NeedsReview(f"Invalid YAML: {exc}") from exc
    require(isinstance(result, dict), "Expected a YAML object")
    return result


def version(value: str) -> tuple[int, ...]:
    require(bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value)), f"Unstable version: {value}")
    return tuple(map(int, value.split(".")))


def upgrade(old: str, new: str, minor: bool = False) -> None:
    before, after = version(old), version(new)
    require(after > before, f"Not an upgrade: {old} -> {new}")
    require(before[0] == after[0], "Major updates require review")
    require(before[1] == after[1] or (minor and before[0] > 0), "Minor update requires review")


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def package_upgrade(name: str, old: str, new: str, policy: dict) -> None:
    minor = matches(name, policy.get("minor", []))
    require(minor or matches(name, policy.get("patch", [])), f"{name} requires human review")
    upgrade(old, new, minor)


def registry_json(url: str) -> dict:
    request = Request(
        url,
        headers={"User-Agent": "omnigent-dependency-updates (github.com/omnigent-ai/omnigent)"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def aged(timestamp: str, days: int, now: datetime) -> None:
    published = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    require(published.tzinfo is not None, "Registry timestamp has no timezone")
    require(published <= now - timedelta(days=days), "Release has not completed the cooldown")


def lock_changes(before: dict, after: dict, policy: dict) -> list[dict]:
    require(
        {k: v for k, v in before.items() if k != "package"}
        == {k: v for k, v in after.items() if k != "package"},
        "Lockfile resolution settings changed",
    )
    indexes = []
    for lock in (before, after):
        by_name = {}
        for package in lock["package"]:
            by_name.setdefault(package["name"], []).append(package)
        indexes.append(by_name)
    old, new = indexes
    require(old.keys() == new.keys(), "Packages were added or removed")
    changes = []
    for name, previous in old.items():
        current = new[name]
        if previous == current:
            continue
        require(len(previous) == len(current) == 1, f"{name} has multiple conditional versions")
        previous, current = previous[0], current[0]
        package_upgrade(name, previous["version"], current["version"], policy["python"])
        require(
            current["source"] == {"registry": "https://pypi.org/simple"},
            f"{name} is not from PyPI",
        )
        mutable = {"version", "sdist", "wheels"}
        require(
            {k: v for k, v in previous.items() if k not in mutable}
            == {k: v for k, v in current.items() if k not in mutable},
            f"{name} changed its source, dependencies, or resolution markers",
        )
        changes.append(current)
    require(bool(changes), "No eligible package upgrades")
    return changes


def validate_release(package: dict, release: dict, cooldown_days: int, now: datetime) -> None:
    name = package["name"]
    require(release["info"]["version"] == package["version"], f"{name}: registry version mismatch")
    files = {entry["url"]: entry for entry in release["urls"]}
    artifacts = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
    require(bool(artifacts), f"{name}: no locked artifacts")
    for artifact in artifacts:
        published = files.get(artifact["url"])
        require(published is not None, f"{name}: artifact is absent from PyPI")
        require(not published["yanked"], f"{name}: artifact is yanked")
        require(
            artifact["hash"] == f"sha256:{published['digests']['sha256']}"
            and artifact["size"] == published["size"],
            f"{name}: artifact does not match PyPI",
        )
        aged(published["upload_time_iso_8601"], cooldown_days, now)


def python_changes(read, files: set[str], policy: dict) -> dict:
    require(files == {"uv.lock"}, "Python manifest changes require review")
    before, after = (tomllib.loads(text) for text in read("uv.lock"))
    changes = lock_changes(before, after, policy)
    for package in changes:
        release = registry_json(
            f"https://pypi.org/pypi/{package['name']}/{package['version']}/json"
        )
        validate_release(package, release, policy["cooldown_days"], datetime.now(UTC))
    return decision(
        "python",
        changes,
        "uv.lock",
        "uv",
        ["ci.yml", "lint.yml", "integration.yml", "e2e.yml"],
        ["Pre-commit checks", "Pytest (runtime-core)", "Integration (openai-agents)"],
    )


def decision(ecosystem, packages, target="", scanner="", workflows=(), checks=()):
    return {
        "ecosystem": ecosystem,
        "packages": [f"{p['name']}=={p['version']}" for p in packages],
        "target": target,
        "scanner": scanner,
        "workflows": list(workflows),
        "checks": list(checks),
    }


def npm_key(key: str) -> tuple[str, str]:
    name, sep, value = key.rpartition("@")
    require(bool(sep) and bool(name), f"Invalid npm package key: {key}")
    version(value)
    return name, value


def npm_reachable(lock: dict, sections: dict) -> set[str]:
    def package_key(name, pin):
        # Aliased references carry the real name before the version.
        return pin if "@" in pin.split("(", 1)[0] else f"{name}@{pin}"

    pending = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, dependency in sections.get(section, {}).items():
            pending.append(package_key(name, dependency["version"]))
    seen = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        snapshot = lock["snapshots"].get(key, {})
        for section in ("dependencies", "optionalDependencies"):
            pending.extend(
                package_key(name, pin) for name, pin in snapshot.get(section, {}).items()
            )
    return seen


def npm_changes(read, files: set[str], policy: dict) -> dict:
    require(
        files <= {"pnpm-lock.yaml", "web/package.json"} and "pnpm-lock.yaml" in files,
        "Only web manifest and root pnpm lock updates qualify",
    )
    before, after = (read_yaml(text) for text in read("pnpm-lock.yaml"))
    allowed_sections = {"packages", "snapshots", "importers"}
    require(
        {k: v for k, v in before.items() if k not in allowed_sections}
        == {k: v for k, v in after.items() if k not in allowed_sections},
        "pnpm settings, catalogs or compatibility overrides changed",
    )
    old_packages, new_packages = before["packages"], after["packages"]
    reachable = {
        key.split("(", 1)[0]
        for sections in before["importers"].values()
        for key in npm_reachable(before, sections)
    }
    # pnpm also prunes stale lock entries that no workspace consumer can reach.
    pruned = old_packages.keys() - new_packages.keys() - reachable
    for key in old_packages.keys() & new_packages.keys():
        require(old_packages[key] == new_packages[key], f"Existing npm resolution changed: {key}")
    removed, added = {}, {}
    for key in old_packages.keys() - new_packages.keys() - pruned:
        name, pin = npm_key(key)
        require(name not in removed, f"Ambiguous npm upgrade: {name}")
        removed[name] = pin
    for key in new_packages.keys() - old_packages.keys():
        name, pin = npm_key(key)
        require(name not in added, f"Ambiguous npm upgrade: {name}")
        added[name] = pin
    require(bool(added) and removed.keys() == added.keys(), "npm package set changed")
    workspace = read_yaml(read("pnpm-workspace.yaml")[0])
    require(
        not any(matches(name, list(workspace.get("overrides", {}))) for name in added),
        "A compatibility override requires review",
    )
    for name, pin in added.items():
        package_upgrade(name, removed[name], pin, policy["npm"])
        old = old_packages[f"{name}@{removed[name]}"]
        new = new_packages[f"{name}@{pin}"]
        require(
            {k: v for k, v in old.items() if k != "resolution"}
            == {k: v for k, v in new.items() if k != "resolution"},
            f"{name}: engine, peer or platform metadata changed",
        )
        metadata = registry_json(f"https://registry.npmjs.org/{quote(name, safe='')}")
        release = metadata["versions"][pin]
        require(release["name"] == name and release["version"] == pin, "npm release mismatch")
        require(not release.get("deprecated"), f"{name}: release deprecated")
        require(
            new["resolution"] == {"integrity": release["dist"]["integrity"]}
            and release["dist"]["tarball"].startswith("https://registry.npmjs.org/"),
            f"{name}: npm integrity or source mismatch",
        )
        aged(metadata["time"][pin], policy["cooldown_days"], datetime.now(UTC))

    def replace_peers(value):
        for name, pin in removed.items():
            value = value.replace(f"({name}@{pin})", f"({name}@{added[name]})")
            value = value.replace(f"({name}@{pin}(", f"({name}@{added[name]}(")
        return value

    # Shared packages must not silently upgrade an untested workspace consumer.
    for importer, sections in before["importers"].items():
        if importer == "web":
            continue
        require(
            not any(
                key.split("(", 1)[0] in {f"{n}@{v}" for n, v in removed.items()}
                for key in npm_reachable(before, sections)
            ),
            f"A changed package is shared with {importer}; human review required",
        )
    expected = copy.deepcopy(before)
    snapshots = {}
    for key, snapshot in expected["snapshots"].items():
        if key.split("(", 1)[0] in pruned:
            continue
        for name, pin in removed.items():
            if key == f"{name}@{pin}" or key.startswith(f"{name}@{pin}("):
                key = f"{name}@{added[name]}" + key[len(f"{name}@{pin}") :]
                break
        for section in ("dependencies", "optionalDependencies"):
            for name, pin in snapshot.get(section, {}).items():
                if name in removed and (
                    pin == removed[name] or pin.startswith(removed[name] + "(")
                ):
                    pin = added[name] + pin[len(removed[name]) :]
                snapshot[section][name] = replace_peers(pin)
        snapshots[replace_peers(key)] = snapshot
    require(snapshots == after["snapshots"], "npm dependency graph changed")
    importers = expected["importers"]
    require(importers.keys() == after["importers"].keys(), "pnpm workspace membership changed")
    old_manifest, new_manifest = (json.loads(text) for text in read("web/package.json"))
    require(old_manifest.keys() == new_manifest.keys(), "Web manifest sections changed")
    changed_specs = {}
    for section in ("dependencies", "devDependencies"):
        old = old_manifest.get(section, {})
        new = new_manifest.get(section, {})
        require(old.keys() == new.keys(), "Web manifest package set changed")
        for name, spec in old.items():
            if spec != new[name]:
                require(
                    name in added and spec[:1] in {"^", "~"} and new[name][:1] == spec[:1],
                    f"{name}: explicit pin or range policy changed",
                )
                package_upgrade(name, spec[1:], new[name][1:], policy["npm"])
                changed_specs[name] = (spec, new[name])
        if section in old_manifest:
            old_manifest[section] = new
    require(old_manifest == new_manifest, "Web manifest scripts or other settings changed")
    for importer, sections in importers.items():
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for name, dependency in sections.get(section, {}).items():
                pin = dependency["version"]
                if name in removed and (
                    pin == removed[name] or pin.startswith(removed[name] + "(")
                ):
                    require(importer == "web", f"{name}: non-web consumer requires review")
                    # Exact manifest pins and catalog constraints are maintained deliberately.
                    require(dependency["specifier"][:1] in {"^", "~"}, f"{name}: explicit pin")
                    dependency["version"] = added[name] + pin[len(removed[name]) :]
                dependency["version"] = replace_peers(dependency["version"])
                if importer == "web" and name in changed_specs:
                    require(
                        dependency["specifier"] == changed_specs[name][0], "Manifest/lock mismatch"
                    )
                    dependency["specifier"] = changed_specs[name][1]
    require(importers == after["importers"], "pnpm importer or dependency range changed")
    return decision(
        "npm",
        [{"name": n, "version": v} for n, v in added.items()],
        "pnpm-lock.yaml",
        "pnpm",
        ["ci.yml", "lint.yml", "web-tests.yml", "e2e-ui.yml", "docker-build.yml"],
        ["Pre-commit checks", "web test", "Browser Contract UI", "Docker build"],
    )


def github_release(repository: str, sha: str, days: int) -> tuple[str, str]:
    tags = paginated(f"repos/{repository}/tags")
    candidates = [
        tag["name"]
        for tag in tags
        if tag["commit"]["sha"] == sha and re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", tag["name"])
    ]
    require(bool(candidates), f"{repository}@{sha}: no stable release tag")
    tag = max(candidates, key=lambda t: version(t.removeprefix("v")))
    release = gh_json(["api", f"repos/{repository}/releases/tags/{tag}"])
    require(not release["draft"] and not release["prerelease"], "Unstable GitHub release")
    aged(release["published_at"], days, datetime.now(UTC))
    return tag.removeprefix("v"), tag


def action_changes(read, files: set[str], policy: dict, *, for_scan: bool = False) -> dict:
    changes = set()
    exercised = set()
    workflows = {"ci.yml", "lint.yml"}
    pattern = re.compile(r"^(\s*-?\s*uses:\s*)([\w-]+/[\w./-]+)@([a-f0-9]{40})(\s*(?:#.*)?)$")
    for path in sorted(files):
        require(
            (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
            or (
                path.startswith(".github/actions/")
                and path.endswith(("/action.yml", "/action.yaml"))
            ),
            "Only action pin changes qualify",
        )
        old, new = (text.splitlines() for text in read(path))
        require(len(old) == len(new), "Workflow structure changed")
        for a, b in zip(old, new, strict=True):
            if a == b:
                continue
            left, right = pattern.fullmatch(a), pattern.fullmatch(b)
            require(
                left is not None and right is not None,
                "Workflow content changed beyond action pins",
            )
            prefix, name, old_sha, _ = left.groups()
            new_prefix, new_name, new_sha, _ = right.groups()
            require(
                prefix == new_prefix and name == new_name and name in policy["actions"],
                "Action identity or YAML structure changed",
            )
            require(old_sha != new_sha, "Only action SHA upgrades qualify")
            changes.add((name, old_sha, new_sha))
            if path in policy["action_test_paths"]:
                exercised.add((name, old_sha, new_sha))
                workflows.add(policy["action_test_paths"][path])
    require(bool(changes), "No action pin changes")
    if not for_scan:
        require(changes == exercised, "Each changed action must be exercised by PR CI")
    packages = []
    for name, old_sha, new_sha in sorted(changes):
        repository = "/".join(name.split("/")[:2])
        old, _ = github_release(repository, old_sha, 0)
        new, _ = github_release(repository, new_sha, 0 if for_scan else policy["cooldown_days"])
        if for_scan:
            require(version(new) > version(old), "Action pin is not an upgrade")
        else:
            upgrade(old, new, minor=True)
        packages.append({"name": name, "version": new})
    return decision(
        "github-actions", packages, workflows=sorted(workflows), checks=["Pre-commit checks"]
    )


def hook_changes(read, files: set[str], policy: dict) -> dict:
    require(files == {".pre-commit-config.yaml"}, "Only pre-commit config changes qualify")
    before, after = (read_yaml(text) for text in read(".pre-commit-config.yaml"))
    expected = copy.deepcopy(before)
    require(len(expected["repos"]) == len(after["repos"]), "Hook repositories changed")
    packages = []
    for old, new in zip(expected["repos"], after["repos"], strict=True):
        if old == new:
            continue
        repo = old["repo"]
        require(
            repo in policy["pre_commit"] and repo == new["repo"], "Hook source requires review"
        )
        old_tag, new_tag = old["rev"], new["rev"]
        upgrade(old_tag.removeprefix("v"), new_tag.removeprefix("v"), minor=True)
        release = gh_json(
            ["api", f"repos/{repo.removeprefix('https://github.com/')}/releases/tags/{new_tag}"]
        )
        require(not release["draft"] and not release["prerelease"], "Unstable hook release")
        aged(release["published_at"], policy["cooldown_days"], datetime.now(UTC))
        old["rev"] = new_tag
        packages.append({"name": repo, "version": new_tag})
    require(expected == after and bool(packages), "Hook commands or settings changed")
    return decision("pre-commit", packages, workflows=["lint.yml"], checks=["Pre-commit checks"])


def cargo_changes(read, files: set[str], policy: dict) -> dict:
    target = policy["cargo"]["target"]
    require(files == {target}, "Rust manifest or untested workspace changes require review")
    before, after = (tomllib.loads(text) for text in read(target))
    old, new = {}, {}
    for lock, index in ((before, old), (after, new)):
        for package in lock["package"]:
            index.setdefault(package["name"], []).append(package)
    require(old.keys() == new.keys(), "Cargo package set changed")
    replacements, packages = {}, []
    for name, previous in old.items():
        current = new[name]
        if previous == current:
            continue
        require(
            len(previous) == len(current) == 1, f"{name}: conditional Cargo versions need review"
        )
        previous, current = previous[0], current[0]
        if previous["version"] == current["version"]:
            continue
        package_upgrade(name, previous["version"], current["version"], policy["cargo"])
        require(
            current.get("source") == "registry+https://github.com/rust-lang/crates.io-index",
            "Only crates.io packages qualify",
        )
        release = registry_json(f"https://crates.io/api/v1/crates/{name}/{current['version']}")[
            "version"
        ]
        require(release["num"] == current["version"], "Crate version mismatch")
        require(
            not release["yanked"] and release["checksum"] == current["checksum"],
            "Crate integrity mismatch",
        )
        aged(release["created_at"], policy["cooldown_days"], datetime.now(UTC))
        replacements[f"{name} {previous['version']}"] = f"{name} {current['version']}"
        packages.append(current)
    expected = copy.deepcopy(before)
    for package in expected["package"]:
        if package["name"] in {p["name"] for p in packages}:
            changed = new[package["name"]][0]
            package["version"], package["checksum"] = changed["version"], changed["checksum"]
        package_deps = package.get("dependencies")
        if package_deps is not None:
            package["dependencies"] = [replacements.get(dep, dep) for dep in package_deps]
    require(bool(packages) and expected == after, "Cargo graph, sources or settings changed")
    return decision(
        "cargo",
        packages,
        target,
        "cargo",
        ["ci.yml", "e2e-ui.yml"],
        ["Pytest (codex-parity)", "Browser Contract UI"],
    )


def evaluate_changes(read, files: set[str], policy: dict) -> dict:
    if "uv.lock" in files:
        return python_changes(read, files, policy)
    if "pnpm-lock.yaml" in files:
        return npm_changes(read, files, policy)
    if ".pre-commit-config.yaml" in files:
        return hook_changes(read, files, policy)
    if policy["cargo"]["target"] in files:
        return cargo_changes(read, files, policy)
    if any(path.startswith(".github/") for path in files):
        return action_changes(read, files, policy)
    raise NeedsReview("Unsupported dependency category; human review required")
