"""Cargo graph and manifest policy, including the trusted CLI entry points."""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from tests.scripts.test_dependabot_automerge import (
    PREFIX,
    ROOT,
    run_cli,
    set_changed_files,
)
from tests.scripts.test_dependabot_automerge import (
    api as api,
)
from tests.scripts.test_dependabot_automerge import (
    policy_module as policy_module,
)

SIDECAR = "tests/codex_parity/sidecar/Cargo.lock"
OMNIDEV = "dev/omnidev/Cargo.lock"
SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
TARGETS = {
    SIDECAR: {"workflows": ["ci.yml", "e2e-ui.yml"], "checks": ["Pytest (codex-parity)"]},
    OMNIDEV: {"workflows": ["ci.yml"], "checks": ["Rust tests (omnidev)"]},
}


def cargo_lock(packages, lock_version=4):
    return f"version = {lock_version}\n" + "".join(
        "[[package]]\n"
        + "".join(f"{key} = {json.dumps(value)}\n" for key, value in package.items())
        for package in packages
    )


@pytest.fixture
def cargo_update(policy_module, monkeypatch):
    before = [
        {"name": "app", "version": "0.1.0", "dependencies": ["anyhow", "unchanged", "git-helper"]},
        {
            "name": "anyhow",
            "version": "1.2.3",
            "source": SOURCE,
            "checksum": "old",
            "dependencies": ["old-leaf"],
        },
        {"name": "old-leaf", "version": "1.0.0", "source": SOURCE, "checksum": "removed"},
        {"name": "unchanged", "version": "2.0.0", "source": SOURCE, "checksum": "keep"},
        {
            "name": "git-helper",
            "version": "0.1.0",
            "source": "git+https://example.test/helper#abc",
            "dependencies": ["anyhow 1.2.3"],
        },
    ]
    after = copy.deepcopy(before)
    after[1].update(version="1.3.0", checksum="new", dependencies=["new-leaf"])
    after[2].update(name="new-leaf", version="0.8.1", checksum="added")
    after[4]["dependencies"] = ["anyhow 1.3.0"]
    state = {
        "before": before,
        "after": after,
        "target": SIDECAR,
        "files": {},
        "lock_version": 4,
        "policy": {
            "cooldown_days": 7,
            "cargo": {
                "default": "minor",
                "manual": ["manual-*"],
                "zero_minor": ["*"],
                "targets": copy.deepcopy(TARGETS),
            },
        },
        "registry": {
            f"https://crates.io/api/v1/crates/{package['name']}/{package['version']}": {
                "version": {
                    "num": package["version"],
                    "checksum": package["checksum"],
                    "yanked": False,
                    "created_at": "2020-01-01T00:00:00Z",
                }
            }
            for package in after[1:3]
        },
        "calls": [],
    }

    def registry(url):
        state["calls"].append(url)
        return state["registry"][url]

    monkeypatch.setattr(policy_module, "registry_json", registry)

    def evaluate():
        files = {
            state["target"]: (cargo_lock(before), cargo_lock(after, state["lock_version"])),
            **state["files"],
        }
        return policy_module.evaluate_changes(files.__getitem__, set(files), state["policy"])

    return state, evaluate


@pytest.mark.parametrize("target", [SIDECAR, OMNIDEV])
def test_cargo_targets_accept_transitive_graph_churn(cargo_update, target):
    state, evaluate = cargo_update
    state["target"] = target
    result = evaluate()
    assert result == {
        "ecosystem": "cargo",
        "packages": ["anyhow==1.3.0", "new-leaf==0.8.1"],
        "target": target,
        "scanner": "cargo",
        **TARGETS[target],
    }
    assert set(state["calls"]) == set(state["registry"])


@pytest.mark.parametrize("reference", ["anyhow", "anyhow 1.3.0", f"anyhow 1.3.0 ({SOURCE})"])
def test_cargo_resolves_all_lock_reference_formats(cargo_update, reference):
    state, evaluate = cargo_update
    state["after"][0]["dependencies"][0] = reference
    assert evaluate()["ecosystem"] == "cargo"


def test_cargo_keeps_existing_conditional_dependency_resolution(cargo_update, policy_module):
    state, evaluate = cargo_update
    for packages in (state["before"], state["after"]):
        packages[0]["dependencies"].append("conditional 1.0.0")
        packages.extend(
            {"name": "conditional", "version": pin, "source": SOURCE, "checksum": pin}
            for pin in ("1.0.0", "2.0.0")
        )
    assert evaluate()["ecosystem"] == "cargo"
    state["after"][0]["dependencies"][-1] = "conditional 2.0.0"
    with pytest.raises(policy_module.NeedsReview, match="changed dependency resolution"):
        evaluate()


@pytest.mark.parametrize("fault", [None, "checksum", "source", "metadata", "multiple"])
def test_cargo_upgrades_one_version_alongside_retained_sibling(cargo_update, policy_module, fault):
    state, evaluate = cargo_update
    for packages in (state["before"], state["after"]):
        packages[0]["dependencies"][0] = f"anyhow {packages[1]['version']}"
        packages[0]["dependencies"].append("anyhow 1.0.0")
        packages.append(
            {"name": "anyhow", "version": "1.0.0", "source": SOURCE, "checksum": "retained"}
        )
    sibling = state["after"][-1]
    if fault == "multiple":
        sibling["version"] = "1.0.1"
    elif fault:
        sibling[fault] = "changed"
    if fault:
        with pytest.raises(policy_module.NeedsReview):
            evaluate()
    else:
        assert evaluate()["packages"] == ["anyhow==1.3.0", "new-leaf==0.8.1"]


def test_cargo_zero_minor_with_exact_manifest_pin(cargo_update):
    state, evaluate = cargo_update
    for packages, pin in ((state["before"], "0.2.3"), (state["after"], "0.3.0")):
        packages[1]["version"] = pin
        packages[4]["dependencies"] = [f"anyhow {pin}"]
    release = state["registry"].pop("https://crates.io/api/v1/crates/anyhow/1.3.0")
    release["version"]["num"] = "0.3.0"
    state["registry"]["https://crates.io/api/v1/crates/anyhow/0.3.0"] = release
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        '[dependencies]\nanyhow = "=0.2.3"\n',
        '[dependencies]\nanyhow = "=0.3.0"\n',
    )
    assert "anyhow==0.3.0" in evaluate()["packages"]


@pytest.mark.parametrize(
    "old,new", [("=1.2.2", "=1.3.0"), ("=1.2.3", "=1.3.1"), ("=1.2.3", "^1.3.0")]
)
def test_cargo_exact_pin_requires_matching_lock_and_operator(
    cargo_update, policy_module, old, new
):
    state, evaluate = cargo_update
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        f'[dependencies]\nanyhow = "{old}"\n',
        f'[dependencies]\nanyhow = "{new}"\n',
    )
    with pytest.raises(policy_module.NeedsReview):
        evaluate()
    assert state["calls"] == []


@pytest.fixture
def existing_cargo_edge(cargo_update, request):
    state, evaluate = cargo_update
    old_pin, new_pin = request.param
    state["before"][1]["dependencies"].append(f"helper {old_pin}")
    state["after"][1]["dependencies"].append(f"helper {new_pin}")
    for packages in (state["before"], state["after"]):
        packages[0]["dependencies"].extend([f"helper {old_pin}", f"helper {new_pin}"])
        packages.extend(
            {"name": "helper", "version": pin, "source": SOURCE, "checksum": pin}
            for pin in (old_pin, new_pin)
        )
    url = f"https://crates.io/api/v1/crates/helper/{new_pin}"
    state["registry"][url] = {
        "version": {
            "num": new_pin,
            "checksum": new_pin,
            "yanked": False,
            "created_at": "2020-01-01T00:00:00Z",
        }
    }
    return state, evaluate


@pytest.mark.parametrize(
    "existing_cargo_edge",
    [("1.1.0", "1.0.0"), ("1.0.0", "2.0.0")],
    indirect=True,
)
def test_cargo_upgraded_parent_preserves_edge_version_boundaries(
    existing_cargo_edge, policy_module
):
    _state, evaluate = existing_cargo_edge
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("existing_cargo_edge", [("1.0.0", "1.1.0")], indirect=True)
def test_cargo_inventories_newly_consumed_existing_versions(existing_cargo_edge):
    state, evaluate = existing_cargo_edge
    assert "helper==1.1.0" in evaluate()["packages"]
    assert "https://crates.io/api/v1/crates/helper/1.1.0" in state["calls"]


def test_cargo_keeps_unchanged_dependency_consumption_out_of_inventory(cargo_update):
    state, evaluate = cargo_update
    for packages in (state["before"], state["after"]):
        packages[1]["dependencies"].append("unchanged")
    assert "unchanged==2.0.0" not in evaluate()["packages"]
    assert "https://crates.io/api/v1/crates/unchanged/2.0.0" not in state["calls"]


@pytest.mark.parametrize("existing_cargo_edge", [("1.0.0", "1.1.0")], indirect=True)
@pytest.mark.parametrize("fault", ["checksum", "yanked", "cooldown", "manual"])
def test_cargo_validates_newly_consumed_existing_releases(
    existing_cargo_edge, policy_module, fault
):
    state, evaluate = existing_cargo_edge
    release = state["registry"]["https://crates.io/api/v1/crates/helper/1.1.0"]["version"]
    if fault == "manual":
        state["policy"]["cargo"]["manual"].append("helper")
    else:
        release[{"cooldown": "created_at"}.get(fault, fault)] = {
            "checksum": "wrong",
            "yanked": True,
            "cooldown": "2100-01-01T00:00:00Z",
        }[fault]
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("existing_cargo_edge", [("1.0.0", "1.1.0")], indirect=True)
@pytest.mark.parametrize("old_pin,new_pin", [("1.1.0", "1.0.0"), ("1.0.0", "2.0.0")])
def test_cargo_checks_existing_edge_descendants(
    existing_cargo_edge, policy_module, old_pin, new_pin
):
    state, evaluate = existing_cargo_edge
    for packages in (state["before"], state["after"]):
        packages[-2]["dependencies"] = [f"descendant {old_pin}"]
        packages[-1]["dependencies"] = [f"descendant {new_pin}"]
        packages.extend(
            {"name": "descendant", "version": pin, "source": SOURCE, "checksum": pin}
            for pin in (old_pin, new_pin)
        )
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("existing_cargo_edge", [("1.0.0", "1.1.0")], indirect=True)
def test_cargo_inventories_existing_edge_descendants(existing_cargo_edge):
    state, evaluate = existing_cargo_edge
    for packages in (state["before"], state["after"]):
        packages[-1]["dependencies"] = ["descendant"]
        packages.append(
            {"name": "descendant", "version": "1.0.0", "source": SOURCE, "checksum": "descendant"}
        )
    state["registry"]["https://crates.io/api/v1/crates/descendant/1.0.0"] = {
        "version": {
            "num": "1.0.0",
            "checksum": "descendant",
            "yanked": False,
            "created_at": "2020-01-01T00:00:00Z",
        }
    }
    assert "descendant==1.0.0" in evaluate()["packages"]
    assert "https://crates.io/api/v1/crates/descendant/1.0.0" in state["calls"]


@pytest.mark.parametrize("existing_cargo_edge", [("1.0.0", "1.1.0")], indirect=True)
@pytest.mark.parametrize("fault", ["inventory", "advisory"])
def test_cargo_existing_edge_consumption_uses_cli_security_gate(
    tmp_path, api, existing_cargo_edge, fault
):
    state, _evaluate = existing_cargo_edge
    set_changed_files(api, {SIDECAR: (cargo_lock(state["before"]), cargo_lock(state["after"]))})
    api["registry"] = state["registry"]
    report = api["report"]["Results"][0]
    report.update(
        Target=SIDECAR,
        Type="cargo",
        Packages=[
            {"Name": package["name"], "Version": package["version"]}
            for package in state["after"]
            if "checksum" in package
            and not (
                fault == "inventory"
                and package["name"] == "helper"
                and package["version"] == "1.1.0"
            )
        ],
    )
    if fault == "advisory":
        report["Vulnerabilities"].append(
            {
                "PkgName": "helper",
                "InstalledVersion": "1.1.0",
                "Severity": "HIGH",
                "VulnerabilityID": "CVE-existing-edge",
            }
        )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stdout + process.stderr
    assert "human review or retry required" in process.stdout
    assert not any(call[:2] == ["pr", "merge"] for call in calls)
    assert (
        "CVE-existing-edge" if fault == "advisory" else "Trivy did not inventory"
    ) in process.stdout


def test_cargo_checks_releases_when_only_dependency_references_change(cargo_update, policy_module):
    state, evaluate = cargo_update
    state["before"][3]["dependencies"] = ["anyhow 1.2.3"]
    state["after"][3]["dependencies"] = ["anyhow 1.3.0"]
    url = "https://crates.io/api/v1/crates/unchanged/2.0.0"
    state["registry"][url] = {
        "version": {
            "num": "2.0.0",
            "checksum": "keep",
            "yanked": False,
            "created_at": "2020-01-01T00:00:00Z",
        }
    }
    assert "unchanged==2.0.0" in evaluate()["packages"]
    assert url in state["calls"]
    state["registry"][url]["version"]["created_at"] = "2100-01-01T00:00:00Z"
    with pytest.raises(policy_module.NeedsReview, match="cooldown"):
        evaluate()


def test_cargo_removed_transitives_respect_manual_exceptions(cargo_update, policy_module):
    state, evaluate = cargo_update
    state["policy"]["cargo"]["manual"].append("old-leaf")
    with pytest.raises(policy_module.NeedsReview, match="old-leaf requires human review"):
        evaluate()


@pytest.mark.parametrize("prefix", ["", "^", "~", "="])
@pytest.mark.parametrize(
    "section",
    [
        "dependencies",
        "dev-dependencies",
        "build-dependencies",
        "target.'cfg(unix)'.dependencies",
        "workspace.dependencies",
    ],
)
@pytest.mark.parametrize("spec", ["string", "table", "alias"])
def test_cargo_manifest_versions_match_lock_upgrades(
    cargo_update, policy_module, prefix, section, spec
):
    state, evaluate = cargo_update
    # A tilde requirement stays within its minor series.
    if prefix == "~":
        old_version = "1.3.0"
        state["before"][1]["version"] = old_version
        state["before"][4]["dependencies"] = ["anyhow 1.3.0"]
        state["after"][1]["version"] = "1.3.1"
        state["after"][4]["dependencies"] = ["anyhow 1.3.1"]
        release = state["registry"].pop("https://crates.io/api/v1/crates/anyhow/1.3.0")
        release["version"]["num"] = "1.3.1"
        state["registry"]["https://crates.io/api/v1/crates/anyhow/1.3.1"] = release
        new_version = "1.3.1"
    else:
        old_version, new_version = "1.2.3", "1.3.0"
    pin = f'"{prefix}{old_version}"'
    value = (
        pin
        if spec == "string"
        else "{ version = " + pin + ', features = ["std"], default-features = false }'
    )
    name = "anyhow"
    if spec == "alias":
        name = "error-helper"
        value = value.replace("{ version", '{ package = "anyhow", version')
    old = f'[package]\nname = "app"\nversion = "0.1.0"\n[{section}]\n{name} = {value}\n'
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        old,
        old.replace(old_version, new_version),
    )
    assert evaluate()["ecosystem"] == "cargo"


def test_cargo_preserves_unchanged_exact_manifest_pins(cargo_update):
    state, evaluate = cargo_update
    old = '[dependencies]\nanyhow = "1.2.3"\nunchanged = "=2.0.0"\n'
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        old,
        old.replace("1.2.3", "1.3.0"),
    )
    assert evaluate()["ecosystem"] == "cargo"


@pytest.mark.parametrize(
    "old_pin,new_pin", [("1.2", "1.3"), ("^1.2", "^1.3"), ("1", "2"), ("=1.2", "=1.3")]
)
def test_cargo_partial_manifest_versions(cargo_update, policy_module, old_pin, new_pin):
    state, evaluate = cargo_update
    old = f'[dependencies]\nanyhow = "{old_pin}"\n'
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        old,
        old.replace(old_pin, new_pin),
    )
    if new_pin == "2":
        with pytest.raises(policy_module.NeedsReview, match="Major"):
            evaluate()
    else:
        assert evaluate()["ecosystem"] == "cargo"


@pytest.mark.parametrize(
    "fault",
    [
        "checksum",
        "source",
        "metadata",
        "local_version",
        "git_source",
        "dependency_names",
        "dangling",
        "dangling_version",
        "dangling_source",
        "duplicate",
        "orphan",
        "settings",
        "no_upgrade",
        "conditional",
    ],
)
def test_cargo_rejects_unrelated_lock_rewrites(cargo_update, policy_module, fault):
    state, evaluate = cargo_update
    after = state["after"]
    if fault in {"checksum", "source", "metadata"}:
        after[3][{"metadata": "extra"}.get(fault, fault)] = "rewritten"
    elif fault == "local_version":
        after[0]["version"] = "0.1.1"
    elif fault == "git_source":
        after[4]["source"] += "rewritten"
    elif fault == "dependency_names":
        after[0]["dependencies"].append("new-leaf")
    elif fault.startswith("dangling"):
        after[1]["dependencies"] = [
            {
                "dangling": "absent",
                "dangling_version": "new-leaf 9.9.9",
                "dangling_source": "new-leaf 0.8.1 (git+https://example.test/other)",
            }[fault]
        ]
    elif fault == "duplicate":
        after.append(copy.deepcopy(after[3]))
    elif fault == "orphan":
        after[1]["dependencies"] = []
    elif fault == "settings":
        state["lock_version"] = 3
    elif fault == "no_upgrade":
        after[:] = copy.deepcopy(state["before"])
    else:
        state["before"].append(
            {"name": "anyhow", "version": "1.0.0", "source": SOURCE, "checksum": "older"}
        )
        after.append(copy.deepcopy(state["before"][-1]))
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("package_index", [1, 2])
@pytest.mark.parametrize("fault", ["version", "checksum", "yanked", "cooldown"])
def test_cargo_validates_upgraded_and_added_releases(
    cargo_update, policy_module, package_index, fault
):
    state, evaluate = cargo_update
    package = state["after"][package_index]
    release = state["registry"][
        f"https://crates.io/api/v1/crates/{package['name']}/{package['version']}"
    ]["version"]
    release[{"version": "num", "cooldown": "created_at"}.get(fault, fault)] = {
        "version": "9.9.9",
        "checksum": "incorrect",
        "yanked": True,
        "cooldown": "2100-01-01T00:00:00Z",
    }[fault]
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("package_index", [1, 2])
@pytest.mark.parametrize("fault", ["manual", "git", "unstable"])
def test_cargo_applies_package_policy_to_transitives(
    cargo_update, policy_module, package_index, fault
):
    state, evaluate = cargo_update
    package = state["after"][package_index]
    if fault == "manual":
        state["policy"]["cargo"]["manual"].append(package["name"])
    elif fault == "git":
        package["source"] = "git+https://example.test/other#abc"
    else:
        package["version"] += "-rc.1"
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("old_version,new_version", [("1.2.3", "2.0.0"), ("1.2.3", "1.2.2")])
def test_cargo_rejects_incompatible_upgrades(
    cargo_update, policy_module, old_version, new_version
):
    state, evaluate = cargo_update
    state["before"][1]["version"] = old_version
    state["after"][1]["version"] = new_version
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize(
    "new",
    [
        'anyhow = { version = "1.3.0", features = ["other"] }',
        'anyhow = { version = "1.3.0", features = ["std"], git = "https://example.test/other" }',
        'anyhow = { version = "1.3.0", features = ["std"], workspace = true }',
        'anyhow = { version = "1.3.0", features = ["std"], optional = true }',
        'anyhow = { version = "1.3.0", features = ["std"], default-features = false }',
        'anyhow = { version = "1.3.0", features = ["std"] }\nnewdep = "1"',
        'anyhow = { version = "1.4.0", features = ["std"] }',
        'anyhow = { version = "~1.3.0", features = ["std"] }',
    ],
)
def test_cargo_manifest_preserves_dependency_settings(cargo_update, policy_module, new):
    state, evaluate = cargo_update
    old = '[dependencies]\nanyhow = { version = "1.2.3", features = ["std"] }\n'
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        old,
        "[dependencies]\n" + new + "\n",
    )
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize(
    "extra",
    [
        '[workspace]\nresolver = "2"\n',
        '[features]\nnew = ["anyhow/std"]\n',
        "[profile.release]\nlto = true\n",
        '[patch.crates-io]\nanyhow = { git = "https://example.test/other" }\n',
    ],
)
def test_cargo_manifest_preserves_workspace_and_settings(cargo_update, policy_module, extra):
    state, evaluate = cargo_update
    old = '[dependencies]\nanyhow = "1.2.3"\n'
    state["files"][SIDECAR.replace("Cargo.lock", "Cargo.toml")] = (
        old,
        old.replace("1.2.3", "1.3.0") + extra,
    )
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize(
    "extra_path", [OMNIDEV, "other/Cargo.lock", "dev/omnidev/Cargo.toml", "README.md"]
)
def test_cargo_rejects_multiple_or_untested_targets(cargo_update, policy_module, extra_path):
    state, evaluate = cargo_update
    state["files"][extra_path] = ("", "changed")
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize(
    "entry",
    [
        "merge",
        "approval",
        "scan",
        "skipped-consumer",
        "high",
        "missing-report",
        "expired-report",
        "wrong-target",
        "missing-inventory",
    ],
)
@pytest.mark.parametrize("target", [SIDECAR, OMNIDEV])
def test_cargo_graph_updates_use_trusted_cli(tmp_path, api, cargo_update, target, entry):
    state, _evaluate = cargo_update
    manifest = '[dependencies]\nanyhow = { version = "1.2.3", features = ["std"] }\n'
    set_changed_files(
        api,
        {
            target: (cargo_lock(state["before"]), cargo_lock(state["after"])),
            target.replace("Cargo.lock", "Cargo.toml"): (
                manifest,
                manifest.replace("1.2.3", "1.3.0"),
            ),
        },
    )
    api["registry"] = state["registry"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/trivy.yml").read_text())
    scan = workflow["jobs"]["scan"]
    scopes = scan["strategy"]["matrix"]["include"]
    scope = max(
        (item for item in scopes if item["path"] == "." or target.startswith(item["path"] + "/")),
        key=lambda item: len(item["path"]),
    )
    report = copy.deepcopy(api["report"])
    report["Results"][0].update(
        Target=target if scope["path"] == "." else target.removeprefix(scope["path"] + "/"),
        Type="cargo",
        Packages=[
            {"Name": package["name"], "Version": package["version"]}
            for package in state["after"]
            if "checksum" in package
        ],
    )
    if scope["scope"] == "repository":
        api["report"] = report
    else:
        upload = next(
            step for step in scan["steps"] if step.get("name") == "Upload vulnerability reports"
        )
        artifact = upload["with"]["name"].replace("${{ matrix.scope }}", scope["scope"])
        listing = api["routes"][f"{PREFIX}/actions/runs/11/artifacts?per_page=100"]
        listing["total_count"] += 1
        listing["artifacts"].append({"name": artifact, "id": 13, "expired": False})
        api["artifact_reports"] = {"13": report}
    api["routes"]["Rust tests (omnidev)"] = copy.deepcopy(api["routes"]["Pytest (codex-parity)"])
    api["routes"][f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]["jobs"].append(
        {"name": "Rust tests (omnidev)", "conclusion": "success"}
    )
    if entry == "skipped-consumer":
        required_check = TARGETS[target]["checks"][0]
        api["routes"][required_check]["check_runs"][0]["conclusion"] = "skipped"
        for job in api["routes"][f"{PREFIX}/actions/runs/12/jobs?per_page=100&page=1"]["jobs"]:
            if job["name"] == required_check:
                job["conclusion"] = "skipped"
    elif entry in {"missing-report", "expired-report"}:
        artifacts = api["routes"][f"{PREFIX}/actions/runs/11/artifacts?per_page=100"]["artifacts"]
        selected = next(item for item in artifacts if item["name"].endswith(scope["scope"]))
        if entry == "missing-report":
            artifacts.remove(selected)
        else:
            selected["expired"] = True
    elif entry == "wrong-target":
        report["Results"][0]["Target"] = "another/Cargo.lock"
    elif entry == "missing-inventory":
        report["Results"][0]["Packages"] = []
    elif entry == "high":
        report["Results"][0]["Vulnerabilities"].append(
            {
                "PkgName": "anyhow",
                "InstalledVersion": "1.3.0",
                "Severity": "HIGH",
                "VulnerabilityID": "GHSA-scoped-report",
            }
        )
    process, calls = run_cli(
        tmp_path,
        api,
        mode="merge",
        sweep=entry not in {"approval", "scan"},
        approval=entry == "approval",
        scan=entry == "scan",
    )
    assert process.returncode == 0, process.stdout + process.stderr
    if entry == "merge":
        assert calls[-1][:2] == ["pr", "merge"], process.stdout
    elif entry == "approval":
        assert "policy satisfied" in process.stdout
    elif entry != "scan":
        assert "human review or retry required" in process.stdout
        assert not any(call[:2] == ["pr", "merge"] for call in calls)
        reason = {
            "skipped-consumer": "Waiting for successful",
            "high": "GHSA-scoped-report requires human review",
            "missing-report": "Missing or ambiguous Trivy",
            "expired-report": "Missing or ambiguous Trivy",
            "wrong-target": "Trivy did not inventory every changed package",
            "missing-inventory": "Trivy did not inventory every changed package",
        }[entry]
        assert reason in process.stdout
    else:
        assert "eligible=" not in (tmp_path / "outputs").read_text()
