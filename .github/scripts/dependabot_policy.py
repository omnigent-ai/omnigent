"""Validate dependency diffs as data using policy and registry metadata from trusted sources."""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

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
    require(bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value)), f"Unstable version: {value}")
    parts = tuple(map(int, value.split(".")))
    while len(parts) > 3 and parts[-1] == 0:
        parts = parts[:-1]
    return parts + (0,) * max(0, 3 - len(parts))


def upgrade(old: str, new: str, minor: bool = False, *, zero_minor: bool = False) -> None:
    before, after = version(old), version(new)
    require(after > before, f"Not an upgrade: {old} -> {new}")
    require(before[0] == after[0], "Major updates require review")
    require(
        before[1] == after[1] or (minor and (before[0] > 0 or zero_minor)),
        "Minor update requires review",
    )


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def require_package(name: str, policy: dict) -> None:
    require(not matches(name, policy.get("manual", [])), f"{name} requires human review")
    require(
        policy.get("default") in {"patch", "minor"}
        or matches(name, policy.get("minor", []) + policy.get("patch", [])),
        f"{name} requires human review",
    )


def package_upgrade(name: str, old: str, new: str, policy: dict) -> None:
    require_package(name, policy)
    minor = policy.get("default") == "minor" or matches(name, policy.get("minor", []))
    try:
        upgrade(old, new, minor, zero_minor=matches(name, policy.get("zero_minor", [])))
    except NeedsReview as exc:
        raise NeedsReview(f"{name} {old} -> {new}: {exc}") from exc


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


def canonical_specifier(value: str) -> str:
    return ",".join(sorted(re.sub(r"\s+", "", value).split(",")))


def python_requirement_upgrade(old: str, new: str, policy: dict) -> tuple[str, str, str]:
    pattern = r"([A-Za-z0-9_.-]+)(\[[^\]]+\])?(\s*(?:>=|==)\s*)([0-9]+(?:\.[0-9]+){0,3})(.*)"
    previous, current = re.fullmatch(pattern, old), re.fullmatch(pattern, new)
    require(
        previous is not None and current is not None,
        "Only existing Python floors or exact pins may rise",
    )
    name, extras, operator, floor, remainder = previous.groups()
    new_name, new_extras, new_operator, new_floor, new_remainder = current.groups()
    require(
        (name, extras, operator, remainder) == (new_name, new_extras, new_operator, new_remainder),
        "Python dependency extras, markers or range policy changed",
    )
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    operator = operator.strip()
    suffix = remainder.split(";", 1)[0].rstrip()
    require(operator != "==" or not suffix, "Python exact pins must name a single release")
    package_upgrade(normalized, floor, new_floor, policy)
    return (
        normalized,
        f"{operator}{floor}{suffix}",
        f"{operator}{new_floor}{suffix}",
    )


def python_manifest_changes(before: dict, after: dict, policy: dict) -> dict:
    expected = copy.deepcopy(before)
    changes = {}

    def requirements(old, new):
        require(len(old) == len(new), "Python direct dependencies were added or removed")
        for index, (previous, current) in enumerate(zip(old, new, strict=True)):
            if previous == current:
                continue
            require(
                isinstance(previous, str) and isinstance(current, str),
                "Dependency group includes changed",
            )
            name, old_spec, new_spec = python_requirement_upgrade(previous, current, policy)
            require(
                name not in changes or changes[name] == (old_spec, new_spec),
                "Conflicting Python floors",
            )
            changes[name] = (old_spec, new_spec)
            old[index] = current

    project = expected.get("project", {})
    updated_project = after.get("project", {})
    requirements(project.get("dependencies", []), updated_project.get("dependencies", []))
    for section in ("optional-dependencies",):
        groups = project.get(section, {})
        updated_groups = updated_project.get(section, {})
        require(groups.keys() == updated_groups.keys(), "Python extras changed")
        for name, dependencies in groups.items():
            requirements(dependencies, updated_groups[name])
    groups = expected.get("dependency-groups", {})
    updated_groups = after.get("dependency-groups", {})
    require(groups.keys() == updated_groups.keys(), "Python dependency groups changed")
    for name, dependencies in groups.items():
        requirements(dependencies, updated_groups[name])
    require(expected == after, "Python manifest settings changed beyond dependency floors")
    return changes


def package_pairs(
    name: str, previous: list[dict], current: list[dict], policy: dict
) -> list[tuple[dict, dict]]:
    """Match retained releases before pairing a single changed resolution."""
    old = {package["version"]: package for package in previous}
    new = {package["version"]: package for package in current}
    require(
        len(old) == len(previous) and len(new) == len(current),
        f"{name}: duplicate or ambiguous package versions",
    )
    removed, added = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    require(
        len(removed) <= 1 and len(added) <= 1,
        f"{name}: multiple changed versions need review",
    )
    if added and not removed and previous:
        pin = added[0]
        baselines = [entry for entry in old if version(entry)[0] == version(pin)[0]] or list(old)
        package_upgrade(name, max(baselines, key=version), pin, policy)
    pairs = [(old[pin], new[pin]) for pin in sorted(old.keys() & new.keys())]
    if removed or added:
        pairs.append((old[removed[0]] if removed else {}, new[added[0]] if added else {}))
    return pairs


def lock_changes(before: dict, after: dict, policy: dict, *, manifest_changes=None) -> list[dict]:
    require(
        {k: v for k, v in before.items() if k != "package"}
        == {k: v for k, v in after.items() if k != "package"},
        "Lockfile resolution settings changed",
    )
    allow_graph = policy["python"].get("default") in {"patch", "minor"}
    manifest_changes = manifest_changes or {}
    indexes = []
    for lock in (before, after):
        by_name = {}
        for package in lock["package"]:
            by_name.setdefault(package["name"], []).append(package)
        indexes.append(by_name)
    old, new = indexes
    require(allow_graph or old.keys() == new.keys(), "Packages were added or removed")
    changes, replacements = [], {}
    pairs = [
        (name, previous, current)
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
        for previous, current in package_pairs(
            name, old.get(name, []), new.get(name, []), policy["python"]
        )
    ]
    for name, previous, current in pairs:
        if previous == current:
            continue
        if not previous or not current:
            require_package(name, policy["python"])
            for package in (previous, current):
                if not package:
                    continue
                require(
                    package["source"] == {"registry": "https://pypi.org/simple"},
                    f"{name} is not from PyPI",
                )
                version(package["version"])
            if current:
                changes.append(current)
            continue
        require(previous["source"] == current["source"], f"{name} changed its source")
        if previous["version"] == current["version"]:
            continue
        package_upgrade(name, previous["version"], current["version"], policy["python"])
        require(
            current["source"] == {"registry": "https://pypi.org/simple"},
            f"{name} is not from PyPI",
        )
        mutable = {"version", "sdist", "wheels"}
        if allow_graph:
            mutable |= {"dependencies", "optional-dependencies"}
        require(
            {k: v for k, v in previous.items() if k not in mutable}
            == {k: v for k, v in current.items() if k not in mutable},
            f"{name} changed its source, dependencies, or resolution markers",
        )
        replacements[name] = (previous["version"], current["version"])
        changes.append(current)

    def dependency_groups(package):
        groups = {"dependencies": package.get("dependencies", [])}
        for section in ("optional-dependencies", "dev-dependencies"):
            groups.update(
                {
                    f"{section}/{group}": entries
                    for group, entries in package.get(section, {}).items()
                }
            )
        return groups

    def referenced_package(reference, index):
        candidates = index.get(reference["name"], [])
        for field in ("version", "source"):
            if field in reference:
                candidates = [
                    package for package in candidates if package[field] == reference[field]
                ]
        require(
            len(candidates) == 1, f"{reference['name']}: ambiguous or missing Python dependency"
        )
        return candidates[0]

    pending_pairs = [(previous, current) for _, previous, current in pairs if current]
    compared = set()
    while pending_pairs:
        previous, current = pending_pairs.pop()
        pair = (previous.get("name"), previous.get("version"), current["name"], current["version"])
        if pair in compared:
            continue
        compared.add(pair)
        name = current["name"]
        old_groups, new_groups = dependency_groups(previous), dependency_groups(current)
        for group, references in new_groups.items():
            previous_references = old_groups.get(group, [])
            previous_by_name = {}
            for reference in previous_references:
                previous_by_name.setdefault(reference["name"], []).append(reference)
            current_by_name = {}
            for reference in references:
                current_by_name.setdefault(reference["name"], []).append(reference)
            for child, current_references in current_by_name.items():
                old_references = previous_by_name.get(child, [])

                # Version/source references may follow upgrades; extras and
                # platform markers must preserve the existing edge's meaning.
                def edge_metadata(reference):
                    return {
                        key: value
                        for key, value in reference.items()
                        if key not in {"version", "source"}
                    }

                if old_references:
                    require(
                        len(old_references) == len(current_references)
                        and all(
                            edge_metadata(old_reference) == edge_metadata(new_reference)
                            for old_reference, new_reference in zip(
                                old_references, current_references, strict=True
                            )
                        ),
                        f"{name}: dependency extras or markers changed for {child}",
                    )
                for index, reference in enumerate(current_references):
                    if old_references and old_references[index] == reference:
                        continue
                    resolved = referenced_package(reference, new)
                    require(
                        resolved["source"] == {"registry": "https://pypi.org/simple"},
                        f"{child} is not from PyPI",
                    )
                    require_package(child, policy["python"])
                    version(resolved["version"])
                    if old_references:
                        old_resolved = referenced_package(old_references[index], old)
                        require(
                            old_resolved["source"] == resolved["source"],
                            f"{child} changed its source",
                        )
                        if old_resolved["version"] != resolved["version"]:
                            package_upgrade(
                                child,
                                old_resolved["version"],
                                resolved["version"],
                                policy["python"],
                            )
                            pending_pairs.append((old_resolved, resolved))
                    else:
                        pending_pairs.append(({}, resolved))
                    changes.append(resolved)

    def update_references(value, *, replace=True):
        if isinstance(value, list):
            return [update_references(item, replace=replace) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: update_references(item, replace=replace) for key, item in value.items()}
        if "specifier" in result:
            result["specifier"] = canonical_specifier(result["specifier"])
        name = value.get("name")
        if replace and name in replacements and value.get("version") == replacements[name][0]:
            result["version"] = replacements[name][1]
        if (
            replace
            and name in manifest_changes
            and result.get("specifier") == canonical_specifier(manifest_changes[name][0])
        ):
            result["specifier"] = canonical_specifier(manifest_changes[name][1])
        return result

    for name, previous, current in pairs:
        if not previous or not current or previous["version"] != current["version"]:
            continue
        require(
            update_references(previous) == update_references(current, replace=False),
            f"{name}: unchanged package metadata changed",
        )
    pending = [package for package in after["package"] if "registry" not in package["source"]]
    seen = set()
    while pending:
        package = pending.pop()
        identity = (package["name"], package["version"])
        if identity in seen:
            continue
        seen.add(identity)
        for references in dependency_groups(package).values():
            pending.extend(referenced_package(reference, new) for reference in references)
    if seen:
        require(
            {(package["name"], package["version"]) for package in changes} <= seen,
            "New Python packages are unreachable",
        )
    require(bool(changes), "No eligible package upgrades")
    return list({(package["name"], package["version"]): package for package in changes}.values())


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
    manifests = {
        "pyproject.toml",
        "sdks/python-client/pyproject.toml",
        "sdks/ui/pyproject.toml",
        "integrations/slack/pyproject.toml",
    }
    require(
        "uv.lock" in files and files <= manifests | {"uv.lock"},
        "Only Python manifests and uv.lock qualify",
    )
    manifest_changes = {}
    for path in sorted(files - {"uv.lock"}):
        before_manifest, after_manifest = (tomllib.loads(text) for text in read(path))
        for name, specs in python_manifest_changes(
            before_manifest, after_manifest, policy["python"]
        ).items():
            require(
                name not in manifest_changes or manifest_changes[name] == specs,
                "Conflicting Python floors",
            )
            manifest_changes[name] = specs
    before, after = (tomllib.loads(text) for text in read("uv.lock"))
    changes = lock_changes(before, after, policy, manifest_changes=manifest_changes)
    updated_names = {package["name"] for package in changes}
    require(
        manifest_changes.keys() <= updated_names,
        "Python manifest floor has no matching lock upgrade",
    )
    for name, (_old_spec, new_spec) in manifest_changes.items():
        floor = re.match(r"(?:>=|==)([0-9.]+)", new_spec)[1]
        require(
            any(
                package["name"] == name and version(package["version"]) == version(floor)
                for package in changes
            ),
            f"{name}: manifest floor does not match the new locked version",
        )
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
        ["ci.yml", "lint.yml", "integration.yml", "e2e.yml", "e2e-ui.yml", "docker-build.yml"],
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
            if dependency["version"].startswith("link:"):
                continue
            pending.append(package_key(name, dependency["version"]))
    seen = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        require(
            key in lock["snapshots"] and key.split("(", 1)[0] in lock["packages"],
            f"Missing npm dependency resolution: {key}",
        )
        snapshot = lock["snapshots"][key]
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
    require(
        before["importers"].keys() == after["importers"].keys(),
        "pnpm workspace membership changed",
    )
    closures = []
    for lock in (before, after):
        closures.append(
            {name: npm_reachable(lock, sections) for name, sections in lock["importers"].items()}
        )
    old_reachable = {key.split("(", 1)[0] for keys in closures[0].values() for key in keys}
    old_web = {key.split("(", 1)[0] for key in closures[0].get("web", set())}
    new_web = {key.split("(", 1)[0] for key in closures[1].get("web", set())}
    removed = old_packages.keys() - new_packages.keys()
    added = new_packages.keys() - old_packages.keys()
    require(added <= new_web, "Added npm packages are not reachable from web")
    require(removed & old_reachable <= old_web, "Removed npm packages have non-web consumers")
    for key in old_packages.keys() & new_packages.keys():
        require(old_packages[key] == new_packages[key], f"Existing npm resolution changed: {key}")

    for importer, sections in before["importers"].items():
        if importer == "web":
            continue
        require(sections == after["importers"][importer], f"{importer}: non-web consumer changed")
        require(
            closures[0][importer] == closures[1][importer]
            and all(
                before["snapshots"][key] == after["snapshots"][key]
                for key in closures[0][importer]
            ),
            f"A changed package is shared with {importer}; human review required",
        )

    workspace = read_yaml(read("pnpm-workspace.yaml")[0])
    overrides = set(workspace.get("overrides", {})) | set(before.get("overrides", {}))

    def eligible(name):
        require_package(name, policy["npm"])
        # Overrides can select a parent, a version range, or a scoped package.
        selectors = [
            part for selector in overrides for part in re.split(r">(?=@?[A-Za-z])", selector)
        ]
        require(
            not any(
                matches(name, [selector]) or selector.startswith(name + "@")
                for selector in selectors
            ),
            f"{name}: a compatibility override requires review",
        )

    inventory = set(added) | (new_web - old_web)
    added_versions, removed_versions, old_versions = {}, {}, {}
    for keys, index in (
        (added, added_versions),
        (removed & old_reachable, removed_versions),
        (old_packages, old_versions),
    ):
        for key in keys:
            name, _, pin = key.rpartition("@")
            index.setdefault(name, []).append(pin)
    previous_packages = {}
    for name, pins in added_versions.items():
        if name not in old_versions:
            continue
        current = sorted(pins, key=version)
        previous = sorted(removed_versions.get(name, []), key=version)
        if len(previous) == len(current):
            pairs = zip(previous, current, strict=True)
        elif previous:
            # Unequal version sets have no unique pairing; every pair must qualify.
            pairs = ((old, new) for old in previous for new in current)
        else:
            # A parallel resolution must advance the highest existing version
            # in its major line, rather than bypassing policy by retaining it.
            pairs = []
            for pin in current:
                baselines = [
                    old for old in old_versions[name] if version(old)[0] == version(pin)[0]
                ] or old_versions[name]
                pairs.append((max(baselines, key=version), pin))
        for old, new in pairs:
            package_upgrade(name, old, new, policy["npm"])
            previous_packages.setdefault(f"{name}@{new}", set()).add(f"{name}@{old}")

    def snapshot_reference(name, pin):
        return pin if "@" in pin.split("(", 1)[0] else f"{name}@{pin}"

    def reference(name, pin):
        return snapshot_reference(name, pin).split("(", 1)[0]

    compared = set()

    def replacement(name, old, new):
        previous, current = reference(name, old), reference(name, new)
        if previous != current:
            old_name, old_pin = npm_key(previous)
            new_name, new_pin = npm_key(current)
            require(old_name == new_name, f"{name}: npm dependency source changed")
            eligible(new_name)
            package_upgrade(new_name, old_pin, new_pin, policy["npm"])
            inventory.add(current)
        pair = snapshot_reference(name, old), snapshot_reference(name, new)
        if pair not in compared:
            compared.add(pair)
            validate_children([before["snapshots"][pair[0]]], after["snapshots"][pair[1]])

    def validate_children(candidates, snapshot):
        # Prefer the context with the fewest changed child resolutions.
        previous = (
            min(
                candidates,
                key=lambda candidate: sum(
                    pin != snapshot.get(section, {}).get(name)
                    for section in ("dependencies", "optionalDependencies")
                    for name, pin in candidate.get(section, {}).items()
                ),
            )
            if candidates
            else {}
        )
        for section in ("dependencies", "optionalDependencies"):
            for name, pin in snapshot.get(section, {}).items():
                if name in previous.get(section, {}):
                    replacement(name, previous[section][name], pin)
                else:
                    descendants = npm_reachable(after, {"dependencies": {name: {"version": pin}}})
                    inventory.update(key.split("(", 1)[0] for key in descendants)
        return previous

    # Unchanged releases can resolve newer children and different peer contexts,
    # but cannot acquire unrelated dependencies or change lockfile markers.
    snapshot_sections = {"dependencies", "optionalDependencies", "transitivePeerDependencies"}
    old_snapshots = {}
    for key, snapshot in before["snapshots"].items():
        old_snapshots.setdefault(key.split("(", 1)[0], []).append(snapshot)
    for key, snapshot in after["snapshots"].items():
        base = key.split("(", 1)[0]
        if key in before["snapshots"] and snapshot == before["snapshots"][key]:
            continue
        require(key in closures[1].get("web", set()), "Unreachable npm snapshot changed")
        if base not in old_packages:
            candidates = [
                previous
                for previous_base in previous_packages.get(base, set())
                for previous in old_snapshots.get(previous_base, [])
            ]
            validate_children(candidates, snapshot)
            continue
        candidates = (
            [before["snapshots"][key]]
            if key in before["snapshots"]
            else old_snapshots.get(base, [])
        )
        peers = set(new_packages[base].get("peerDependencies", {}))
        compatible = []
        for previous in candidates:
            if {k: v for k, v in previous.items() if k not in snapshot_sections} != {
                k: v for k, v in snapshot.items() if k not in snapshot_sections
            }:
                continue
            if all(
                (previous.get(section, {}).keys() ^ snapshot.get(section, {}).keys()) <= peers
                for section in ("dependencies", "optionalDependencies")
            ):
                compatible.append(previous)
        require(bool(compatible), f"Existing npm snapshot metadata changed: {key}")
        previous = validate_children(compatible, snapshot)
        if previous.get("transitivePeerDependencies") != snapshot.get(
            "transitivePeerDependencies"
        ):
            require(
                key not in before["snapshots"]
                or any(
                    previous.get(section, {}) != snapshot.get(section, {})
                    for section in ("dependencies", "optionalDependencies")
                ),
                f"Unexplained npm peer metadata changed: {key}",
            )
    old_manifest, new_manifest = (json.loads(text) for text in read("web/package.json"))
    require(old_manifest.keys() == new_manifest.keys(), "Web manifest sections changed")
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        old = old_manifest.get(section, {})
        new = new_manifest.get(section, {})
        require(old.keys() == new.keys(), "Web manifest package set changed")
        for name, spec in old.items():
            if spec != new[name]:
                previous_requirement = re.fullmatch(r"([\^~]?)([0-9]+(?:\.[0-9]+){2})", spec)
                current_requirement = re.fullmatch(r"([\^~]?)([0-9]+(?:\.[0-9]+){2})", new[name])
                require(
                    previous_requirement is not None
                    and current_requirement is not None
                    and previous_requirement[1] == current_requirement[1],
                    f"{name}: npm version requirement shape changed",
                )
                package_upgrade(
                    name, previous_requirement[2], current_requirement[2], policy["npm"]
                )
        if section in old_manifest:
            old_manifest[section] = new
    require(old_manifest == new_manifest, "Web manifest scripts or other settings changed")
    old_manifest = json.loads(read("web/package.json")[0])
    expected = copy.deepcopy(before["importers"]["web"])
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        old = expected.get(section, {})
        new = after["importers"]["web"].get(section, {})
        require(old.keys() == new.keys(), "Web importer package set changed")
        for name, dependency in old.items():
            current = new[name]
            if dependency == current:
                continue
            if (
                reference(name, dependency["version"]) != reference(name, current["version"])
                or dependency["specifier"] != current["specifier"]
            ):
                require(
                    re.fullmatch(r"[\^~]?[0-9]+(?:\.[0-9]+){2}", dependency["specifier"]),
                    f"{name}: npm version requirement needs review",
                )
            require(
                dependency["specifier"] == old_manifest.get(section, {}).get(name)
                and current["specifier"] == new_manifest.get(section, {}).get(name),
                "Manifest/lock mismatch",
            )
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", current["specifier"]):
                require(
                    reference(name, current["version"]) == f"{name}@{current['specifier']}",
                    f"{name}: exact manifest pin does not match the lockfile",
                )
            replacement(name, dependency["version"], current["version"])
            dependency["version"] = current["version"]
            dependency["specifier"] = current["specifier"]
    require(expected == after["importers"]["web"], "pnpm importer settings changed")

    for key in sorted(added | (removed & old_reachable)):
        name, _pin = npm_key(key)
        eligible(name)

    require(bool(inventory), "No eligible npm package upgrades")
    metadata_by_name = {}
    for key in sorted(inventory):
        name, pin = npm_key(key)
        eligible(name)
        if name not in metadata_by_name:
            metadata_by_name[name] = registry_json(
                f"https://registry.npmjs.org/{quote(name, safe='')}"
            )
        metadata = metadata_by_name[name]
        release = metadata["versions"][pin]
        require(release["name"] == name and release["version"] == pin, "npm release mismatch")
        require(not release.get("deprecated"), f"{name}: release deprecated")
        require(
            new_packages[key]["resolution"] == {"integrity": release["dist"]["integrity"]}
            and release["dist"]["tarball"].startswith("https://registry.npmjs.org/"),
            f"{name}: npm integrity or source mismatch",
        )
        aged(metadata["time"][pin], policy["cooldown_days"], datetime.now(UTC))
    return decision(
        "npm",
        [dict(zip(("name", "version"), npm_key(key), strict=True)) for key in sorted(inventory)],
        "pnpm-lock.yaml",
        "pnpm",
        ["ci.yml", "lint.yml", "web-tests.yml", "e2e.yml", "e2e-ui.yml", "docker-build.yml"],
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
    workflows = {"ci.yml", "lint.yml", "integration.yml", "e2e.yml", "e2e-ui.yml"}
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
                prefix == new_prefix and name == new_name,
                "Action identity or YAML structure changed",
            )
            require(old_sha != new_sha, "Only action SHA upgrades qualify")
            changes.add((name, old_sha, new_sha))
            require_package(name, policy["actions"])
            if not for_scan:
                require(
                    path in policy["action_test_paths"],
                    f"{path}: Action changes need mapped PR CI",
                )
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
            package_upgrade(name, old, new, policy["actions"])
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
    cargo = policy["cargo"]
    targets = files & cargo["targets"].keys()
    require(len(targets) == 1, "Cargo updates must affect one tested lockfile")
    target = next(iter(targets))
    manifest = target.removesuffix("Cargo.lock") + "Cargo.toml"
    require(
        files <= {target, manifest}, "Rust manifest or untested workspace changes require review"
    )
    before, after = (tomllib.loads(text) for text in read(target))
    require(
        {k: v for k, v in before.items() if k != "package"}
        == {k: v for k, v in after.items() if k != "package"},
        "Cargo lockfile settings changed",
    )
    old, new = {}, {}
    for lock, index in ((before, old), (after, new)):
        for package in lock["package"]:
            index.setdefault(package["name"], []).append(package)

    def dependency_names(package):
        return {reference.split()[0] for reference in package.get("dependencies", [])}

    packages, upgrades = [], {}
    pairs = [
        (name, previous, current)
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
        for previous, current in package_pairs(name, old.get(name, []), new.get(name, []), cargo)
    ]
    for name, previous, current in pairs:
        if previous == current:
            continue
        if not current:
            require_package(name, cargo)
            require(
                previous.get("source") == "registry+https://github.com/rust-lang/crates.io-index",
                f"{name}: removing a local or git package requires review",
            )
            continue
        if previous:
            upgraded = previous["version"] != current["version"]
            mutable = {"dependencies"}
            if upgraded:
                package_upgrade(name, previous["version"], current["version"], cargo)
                mutable |= {"version", "checksum"}
                upgrades[name] = (previous, current)
            else:
                require(
                    dependency_names(previous) == dependency_names(current),
                    f"{name}: unchanged Cargo version changed dependency names",
                )
            require(
                {k: v for k, v in previous.items() if k not in mutable}
                == {k: v for k, v in current.items() if k not in mutable},
                f"{name}: Cargo source, checksum or metadata changed",
            )
            if (
                not upgraded
                and current.get("source")
                != "registry+https://github.com/rust-lang/crates.io-index"
            ):
                continue
        else:
            require_package(name, cargo)
            version(current["version"])
            require(
                current.keys() <= {"name", "version", "source", "checksum", "dependencies"},
                f"{name}: unexpected Cargo package metadata",
            )
        packages.append(current)

    def identity(package):
        return package["name"], package["version"], package.get("source")

    def graph(index):
        edges, resolved = {}, {}
        for name, entries in index.items():
            identities = {(entry["version"], entry.get("source")) for entry in entries}
            require(len(identities) == len(entries), f"{name}: duplicate Cargo package identity")
            edges[name] = set()
            for entry in entries:
                resolved[identity(entry)] = set()
                for reference in entry.get("dependencies", []):
                    parts = reference.split(maxsplit=2)
                    require(bool(parts), "Empty Cargo dependency reference")
                    candidates = index.get(parts[0], [])
                    if len(parts) >= 2:
                        candidates = [p for p in candidates if p["version"] == parts[1]]
                    if len(parts) == 3:
                        candidates = [p for p in candidates if f"({p.get('source')})" == parts[2]]
                    require(
                        len(candidates) == 1,
                        f"Cargo dependency {reference}: dangling or ambiguous reference",
                    )
                    edges[name].add(parts[0])
                    resolved[identity(entry)].add(identity(candidates[0]))
        pending, seen = list(old.keys() & new.keys()), set()
        while pending:
            name = pending.pop()
            if name not in seen:
                seen.add(name)
                pending.extend(edges[name] - seen)
        return seen, resolved

    old_reachable, old_edges = graph(old)
    new_reachable, new_edges = graph(new)
    require(old.keys() - new.keys() <= old_reachable, "Removed Cargo packages were not transitive")
    require(new.keys() - old.keys() <= new_reachable, "Added Cargo packages are not transitive")
    replacements = {
        identity(previous): identity(current) for previous, current in upgrades.values()
    }
    for package_id in old_edges.keys() & new_edges.keys():
        expected_edges = {
            replacements.get(dependency, dependency) for dependency in old_edges[package_id]
        }
        require(
            expected_edges == new_edges[package_id],
            f"{package_id[0]}: unchanged Cargo version changed dependency resolution",
        )

    pending_pairs = list(replacements.items())
    compared, newly_consumed = set(), set()
    while pending_pairs:
        previous_id, current_id = pending_pairs.pop()
        if (previous_id, current_id) in compared:
            continue
        compared.add((previous_id, current_id))
        previous_dependencies, current_dependencies = old_edges[previous_id], new_edges[current_id]
        newly_consumed.update(current_dependencies - previous_dependencies)
        shared_names = {dep[0] for dep in previous_dependencies} & {
            dep[0] for dep in current_dependencies
        }
        for name in shared_names:
            previous = {dep for dep in previous_dependencies if dep[0] == name}
            current = {dep for dep in current_dependencies if dep[0] == name}
            if previous == current:
                continue
            require(
                len(previous) == len(current) == 1,
                f"{name}: multiple conditional Cargo edge versions need review",
            )
            previous_dependency, current_dependency = next(iter(previous)), next(iter(current))
            require(
                previous_dependency[2] == current_dependency[2],
                f"{name}: Cargo dependency source changed",
            )
            package_upgrade(name, previous_dependency[1], current_dependency[1], cargo)
            pending_pairs.append((previous_dependency, current_dependency))

    locked = {identity(package): package for entries in new.values() for package in entries}
    inventory = {identity(package): package for package in packages}
    added_ids = new_edges.keys() - old_edges.keys() - set(replacements.values())
    pending = list(newly_consumed | added_ids)
    consumed = set()
    while pending:
        package_id = pending.pop()
        if package_id in consumed:
            continue
        consumed.add(package_id)
        inventory[package_id] = locked[package_id]
        pending.extend(new_edges[package_id] - consumed)
    packages = sorted(
        inventory.values(), key=lambda package: (package["name"], package["version"])
    )

    if manifest in files:
        old_manifest, new_manifest = (tomllib.loads(text) for text in read(manifest))
        expected = copy.deepcopy(old_manifest)

        def requirement(value):
            match = re.fullmatch(r"([\^~=]?)([0-9]+(?:\.[0-9]+){0,2})", value)
            require(match is not None, f"Cargo version requirement needs review: {value}")
            prefix, pin = match.groups()
            components = pin.split(".")
            return prefix, len(components), ".".join(components + ["0"] * (3 - len(components)))

        def satisfies(pin, prefix, precision, lower):
            resolved, minimum = version(pin), version(lower)
            if prefix == "=":
                return resolved[:precision] == minimum[:precision]
            boundary = 0 if precision == 1 else 1
            if prefix != "~":
                boundary = next((i for i, part in enumerate(minimum) if part), precision - 1)
            return resolved >= minimum and resolved[: boundary + 1] == minimum[: boundary + 1]

        def dependency_versions(previous, current):
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                old_dependencies, new_dependencies = (
                    previous.get(section, {}),
                    current.get(section, {}),
                )
                require(
                    old_dependencies.keys() == new_dependencies.keys(),
                    "Cargo direct dependency set changed",
                )
                for alias, old_spec in old_dependencies.items():
                    new_spec = new_dependencies[alias]
                    if old_spec == new_spec:
                        continue
                    require(
                        type(old_spec) is type(new_spec),
                        f"{alias}: Cargo dependency settings changed",
                    )
                    if isinstance(old_spec, dict):
                        require(
                            not {"git", "path", "workspace"} & old_spec.keys(),
                            f"{alias}: only crates.io dependency version edits qualify",
                        )
                        require(
                            old_spec.get("registry", "crates-io") == "crates-io",
                            "Only crates.io packages qualify",
                        )
                        name = old_spec.get("package", alias)
                        old_pin, new_pin = old_spec.get("version", ""), new_spec.get("version", "")
                    else:
                        name, old_pin, new_pin = alias, old_spec, new_spec
                    old_prefix, old_precision, old_version = requirement(old_pin)
                    new_prefix, new_precision, new_version = requirement(new_pin)
                    require(
                        (old_prefix, old_precision) == (new_prefix, new_precision),
                        f"{name}: Cargo version requirement shape changed",
                    )
                    package_upgrade(name, old_version, new_version, cargo)
                    require(
                        name in upgrades,
                        f"{name}: manifest update has no matching Cargo lock upgrade",
                    )
                    old_package, new_package = upgrades[name]
                    require(
                        satisfies(old_package["version"], old_prefix, old_precision, old_version)
                        and satisfies(
                            new_package["version"], new_prefix, new_precision, new_version
                        ),
                        f"{name}: manifest version does not match Cargo lock upgrade",
                    )
                    if isinstance(old_spec, dict):
                        old_spec["version"] = new_pin
                    else:
                        old_dependencies[alias] = new_pin

        dependency_versions(expected, new_manifest)
        dependency_versions(expected.get("workspace", {}), new_manifest.get("workspace", {}))
        for platform, settings in expected.get("target", {}).items():
            dependency_versions(settings, new_manifest.get("target", {}).get(platform, {}))
        require(
            expected == new_manifest,
            "Cargo manifest source, features, workspace or settings changed",
        )

    require(bool(upgrades), "No eligible Cargo package upgrades")
    for current in packages:
        name = current["name"]
        require(
            current.get("source") == "registry+https://github.com/rust-lang/crates.io-index",
            "Only crates.io packages qualify",
        )
        require_package(name, cargo)
        version(current["version"])
        release = registry_json(f"https://crates.io/api/v1/crates/{name}/{current['version']}")[
            "version"
        ]
        require(release["num"] == current["version"], "Crate version mismatch")
        require(
            not release["yanked"] and release["checksum"] == current.get("checksum"),
            "Crate integrity mismatch",
        )
        aged(release["created_at"], policy["cooldown_days"], datetime.now(UTC))

    required = cargo["targets"][target]
    return decision(
        "cargo",
        packages,
        target,
        "cargo",
        required["workflows"],
        required["checks"],
    )


def evaluate_changes(read, files: set[str], policy: dict) -> dict:
    if "uv.lock" in files:
        return python_changes(read, files, policy)
    if "pnpm-lock.yaml" in files:
        return npm_changes(read, files, policy)
    if ".pre-commit-config.yaml" in files:
        return hook_changes(read, files, policy)
    if files & policy["cargo"]["targets"].keys():
        return cargo_changes(read, files, policy)
    if any(path.startswith(".github/") for path in files):
        return action_changes(read, files, policy)
    raise NeedsReview("Unsupported dependency category; human review required")
