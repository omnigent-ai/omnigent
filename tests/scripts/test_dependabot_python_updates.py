"""Default eligibility preserves Python manifests and validates new transitive pins."""

from __future__ import annotations

import copy
import json

import pytest
import tomllib

from tests.scripts.test_dependabot_automerge import (
    LOCK,
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


@pytest.fixture
def ordinary_policy():
    return json.loads((ROOT / ".github/dependabot-automerge.json").read_text())


@pytest.fixture
def python_graph_lock():
    return (
        LOCK.replace(
            'name = "ruff"',
            'dependencies = [{ name = "helper", version = "1.0.0" }]\nname = "ruff"',
            1,
        )
        + """
[[package]]
name = "workspace"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "ruff" }, { name = "other-consumer" }]
[[package]]
name = "other-consumer"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "helper", version = "1.1.0" }]
[[package]]
name = "helper"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
[[package]]
name = "helper"
version = "1.1.0"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/helper.whl", hash = "sha256:helper", size = 42 }]
"""
    )


@pytest.mark.parametrize(
    "name,old,new,allowed",
    [
        ("unlisted-library", "1.2.3", "1.5.0", True),
        ("unlisted-library", "0.2.3", "0.2.4", True),
        ("unlisted-library", "0.2.3", "0.3.0", True),
        ("unlisted-library", "1.2.3", "2.0.0", False),
        ("unlisted-library", "1.2.3", "1.3.0rc1", False),
        ("unlisted-library", "1.2.3", "1.2.2", False),
        ("claude-agent-sdk", "0.1.2", "0.1.3", False),
        ("ruff", "0.15.1", "0.17.0", True),
    ],
)
def test_default_updates_and_explicit_exceptions(
    policy_module, ordinary_policy, name, old, new, allowed
):
    if allowed:
        policy_module.package_upgrade(name, old, new, ordinary_policy["python"])
    else:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.package_upgrade(name, old, new, ordinary_policy["python"])


@pytest.mark.parametrize("ecosystem", ["python", "npm", "cargo", "actions"])
def test_zero_minor_policy_is_explicit_and_overridable(policy_module, ordinary_policy, ecosystem):
    policy = ordinary_policy[ecosystem]
    policy_module.package_upgrade("ordinary", "0.2.0", "0.3.0", policy)
    policy["manual"].append("ordinary")
    with pytest.raises(policy_module.NeedsReview, match="requires human review"):
        policy_module.package_upgrade("ordinary", "0.2.0", "0.3.0", policy)


@pytest.mark.parametrize("pin,allowed", [("1.2.0", True), ("1.0.0", False), ("2.0.0", False)])
def test_new_parallel_version_preserves_upgrade_policy(
    policy_module, ordinary_policy, pin, allowed
):
    previous = [{"version": "1.1.0"}]
    current = [*previous, {"version": pin}]
    if allowed:
        assert policy_module.package_pairs(
            "ordinary", previous, current, ordinary_policy["python"]
        )[-1] == ({}, {"version": pin})
    else:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.package_pairs("ordinary", previous, current, ordinary_policy["python"])


@pytest.mark.parametrize(
    "fault", [None, "artifact-metadata", "source", "metadata", "duplicate", "multiple"]
)
def test_upgrading_one_python_resolution_preserves_retained_sibling(
    policy_module, ordinary_policy, python_graph_lock, fault
):
    before = tomllib.loads(python_graph_lock)
    after = copy.deepcopy(before)
    after["package"][0]["dependencies"][0]["version"] = "1.0.1"
    after["package"][-2]["version"] = "1.0.1"
    sibling = after["package"][-1]
    if fault == "artifact-metadata":
        sibling["wheels"][0]["hash"] = "sha256:wrong"
    elif fault == "source":
        sibling["source"] = {"registry": "https://other.example/simple"}
    elif fault == "metadata":
        sibling["resolution-markers"] = ["sys_platform == 'linux'"]
    elif fault == "duplicate":
        after["package"].append(copy.deepcopy(sibling))
    elif fault == "multiple":
        sibling["version"] = "1.2.0"
    if fault:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.lock_changes(before, after, ordinary_policy)
    else:
        changes = policy_module.lock_changes(before, after, ordinary_policy)
        assert {(p["name"], p["version"]) for p in changes} == {("helper", "1.0.1")}


@pytest.mark.parametrize("suffix", ["", "; python_version >= '3.12'", ".*", ",<2"])
def test_python_exact_pins_preserve_requirement_shape(policy_module, ordinary_policy, suffix):
    before, after = f"ordinary==1.2.0{suffix}", f"ordinary==1.3.0{suffix}"
    if suffix in {".*", ",<2"}:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.python_requirement_upgrade(before, after, ordinary_policy["python"])
    else:
        assert policy_module.python_requirement_upgrade(
            before, after, ordinary_policy["python"]
        ) == ("ordinary", "==1.2.0", "==1.3.0")


@pytest.mark.parametrize(
    "new,allowed",
    [
        ("library[extra]>=1.3,<2; python_version >= '3.12'", True),
        ("library[other]>=1.3,<2; python_version >= '3.12'", False),
        ("library[extra]>=1.3,<3; python_version >= '3.12'", False),
        ("library[extra]>=1.3,<2; python_version >= '3.13'", False),
        ("library[extra]==1.3; python_version >= '3.12'", False),
    ],
)
def test_floor_updates_preserve_extras_markers_and_upper_bounds(
    policy_module, ordinary_policy, new, allowed
):
    before = {"project": {"dependencies": ["library[extra]>=1.2,<2; python_version >= '3.12'"]}}
    after = {"project": {"dependencies": [new]}}
    if allowed:
        assert policy_module.python_manifest_changes(before, after, ordinary_policy["python"])
    else:
        with pytest.raises(policy_module.NeedsReview):
            policy_module.python_manifest_changes(before, after, ordinary_policy["python"])


@pytest.mark.parametrize("fault", ["direct-add", "script", "group", "include"])
def test_manifest_changes_do_not_expand_other_configuration(policy_module, ordinary_policy, fault):
    before = {
        "project": {"dependencies": ["library>=1.2,<2"]},
        "dependency-groups": {"test": ["pytest>=9"]},
    }
    after = copy.deepcopy(before)
    if fault == "direct-add":
        after["project"]["dependencies"].append("new>=1")
    elif fault == "script":
        after["project"]["scripts"] = {"run": "other:main"}
    elif fault == "group":
        after["dependency-groups"]["new"] = []
    else:
        after["dependency-groups"]["test"][0] = {"include-group": "other"}
    with pytest.raises(policy_module.NeedsReview):
        policy_module.python_manifest_changes(before, after, ordinary_policy["python"])


@pytest.mark.parametrize("fault", ["downgrade", "marker", "remove-marker", "extras", "source"])
def test_patch_preserves_existing_dependency_edge_policy(
    policy_module, ordinary_policy, python_graph_lock, fault
):
    before = tomllib.loads(python_graph_lock)
    reference = before["package"][0]["dependencies"][0]
    reference.update(marker="sys_platform == 'linux'", extra=["feature"])
    if fault == "downgrade":
        reference["version"] = "1.1.0"
    after = copy.deepcopy(before)
    after["package"][0]["version"] = "0.15.2"
    changed = after["package"][0]["dependencies"][0]
    if fault == "downgrade":
        changed["version"] = "1.0.0"
    elif fault == "marker":
        changed["marker"] = "sys_platform == 'darwin'"
    elif fault == "remove-marker":
        del changed["marker"]
    elif fault == "extras":
        changed["extra"] = ["different"]
    else:
        changed["source"] = {"registry": "https://other.example/simple"}
    with pytest.raises(policy_module.NeedsReview):
        policy_module.lock_changes(before, after, ordinary_policy)


@pytest.mark.parametrize("grandchild_version", ["1.0.0", "1.2.0"])
def test_existing_child_pins_preserve_nested_upgrade_policy(
    policy_module, ordinary_policy, python_graph_lock, grandchild_version
):
    before = tomllib.loads(python_graph_lock)
    for package in before["package"]:
        if package["name"] == "helper":
            pin = "1.1.0" if package["version"] == "1.0.0" else grandchild_version
            package["dependencies"] = [{"name": "leaf", "version": pin}]
    before["package"].extend(
        {
            "name": "leaf",
            "version": pin,
            "source": {"registry": "https://pypi.org/simple"},
        }
        for pin in ("1.0.0", "1.1.0", "1.2.0")
    )
    after = copy.deepcopy(before)
    after["package"][0]["version"] = "0.15.2"
    after["package"][0]["dependencies"][0]["version"] = "1.1.0"
    assert before["package"][1:] == after["package"][1:]
    if grandchild_version == "1.0.0":
        with pytest.raises(policy_module.NeedsReview, match=r"Not an upgrade: 1\.1\.0 -> 1\.0\.0"):
            policy_module.lock_changes(before, after, ordinary_policy)
    else:
        changes = policy_module.lock_changes(before, after, ordinary_policy)
        assert {(package["name"], package["version"]) for package in changes} == {
            ("ruff", "0.15.2"),
            ("helper", "1.1.0"),
            ("leaf", "1.2.0"),
        }


def test_new_transitive_conditional_versions_require_review(
    policy_module, ordinary_policy, python_graph_lock
):
    before = tomllib.loads(python_graph_lock)
    after = copy.deepcopy(before)
    after["package"][0]["version"] = "0.15.2"
    after["package"][0]["dependencies"].extend(
        [
            {"name": "new-helper", "version": "1.0.0", "marker": "sys_platform == 'linux'"},
            {"name": "new-helper", "version": "2.0.0", "marker": "sys_platform != 'linux'"},
        ]
    )
    for pin, marker in (
        ("1.0.0", "sys_platform == 'linux'"),
        ("2.0.0", "sys_platform != 'linux'"),
    ):
        after["package"].append(
            {
                "name": "new-helper",
                "version": pin,
                "source": {"registry": "https://pypi.org/simple"},
                "resolution-markers": [marker],
            }
        )
    with pytest.raises(policy_module.NeedsReview, match="multiple changed versions"):
        policy_module.lock_changes(before, after, ordinary_policy)


@pytest.mark.parametrize(
    "parent,fault",
    [
        ("new-package", "missing-version"),
        ("new-package", "wrong-source"),
        ("workspace", "missing-version"),
    ],
)
def test_graph_edges_resolve_exact_package_identity(
    policy_module, ordinary_policy, python_graph_lock, parent, fault
):
    before = tomllib.loads(python_graph_lock)
    if parent == "workspace":
        workspace = next(
            package for package in before["package"] if package["name"] == "workspace"
        )
        workspace["dependencies"][0]["version"] = "0.15.1"
    after = copy.deepcopy(before)
    after["package"][0]["version"] = "0.15.2"
    if parent == "new-package":
        reference = {"name": "helper", "version": "1.1.0"}
        after["package"][0]["dependencies"].append({"name": "new-bridge"})
        after["package"].append(
            {
                "name": "new-bridge",
                "version": "1.0.0",
                "source": {"registry": "https://pypi.org/simple"},
                "dependencies": [reference],
            }
        )
    else:
        workspace = next(package for package in after["package"] if package["name"] == "workspace")
        reference = workspace["dependencies"][0]
    if fault == "missing-version" and parent == "new-package":
        reference["version"] = "9.9.9"
    elif fault == "wrong-source":
        reference["source"] = {"registry": "https://other.example/simple"}
    with pytest.raises(policy_module.NeedsReview):
        policy_module.lock_changes(before, after, ordinary_policy)


@pytest.mark.parametrize("finding", ["clean", "high", "missing-inventory"])
@pytest.mark.parametrize("parent", ["existing-package", "new-package"])
def test_changed_edge_to_existing_pin_uses_worker_advisory_inventory(
    tmp_path, api, python_graph_lock, finding, parent
):
    updated = python_graph_lock.replace("0.15.1", "0.15.2")
    if parent == "existing-package":
        updated = updated.replace(
            'dependencies = [{ name = "helper", version = "1.0.0" }]',
            'dependencies = [{ name = "helper", version = "1.1.0" }]',
            1,
        )
    else:
        updated = (
            updated.replace(
                'dependencies = [{ name = "helper", version = "1.0.0" }]',
                'dependencies = [{ name = "helper", version = "1.0.0" }, { name = "new-bridge" }]',
                1,
            )
            + """
[[package]]
name = "new-bridge"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "helper", version = "1.1.0" }]
wheels = [{ url = "https://files.pythonhosted.org/bridge.whl", hash = "sha256:bridge", size = 42 }]
"""
        )
    set_changed_files(api, {"uv.lock": (python_graph_lock, updated)})
    release = copy.deepcopy(api["pypi"])
    release["info"]["version"] = "1.1.0"
    release["urls"][0]["url"] = "https://files.pythonhosted.org/helper.whl"
    release["urls"][0]["digests"]["sha256"] = "helper"
    api["registry"] = {"https://pypi.org/pypi/helper/1.1.0/json": release}
    if parent == "new-package":
        bridge_release = copy.deepcopy(api["pypi"])
        bridge_release["info"]["version"] = "1.0.0"
        bridge_release["urls"][0]["url"] = "https://files.pythonhosted.org/bridge.whl"
        bridge_release["urls"][0]["digests"]["sha256"] = "bridge"
        api["registry"]["https://pypi.org/pypi/new-bridge/1.0.0/json"] = bridge_release
    report = api["report"]["Results"][0]
    if finding != "missing-inventory":
        report["Packages"].append({"Name": "helper", "Version": "1.1.0"})
    if parent == "new-package":
        report["Packages"].append({"Name": "new-bridge", "Version": "1.0.0"})
    if finding == "high":
        report["Vulnerabilities"].append(
            {
                "PkgName": "helper",
                "InstalledVersion": "1.1.0",
                "Severity": "HIGH",
                "VulnerabilityID": "changed-edge-advisory",
            }
        )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) is (finding == "clean"), (
        process.stdout
    )
    if finding == "high":
        assert "changed-edge-advisory requires human review" in process.stdout
    elif finding == "missing-inventory":
        assert "Trivy did not inventory every changed package" in process.stdout
    else:
        assert "helper==1.1.0" in process.stdout


@pytest.mark.parametrize(
    "finding", ["clean", "high", "missing-inventory", "young", "bad-hash", "unmatched-floor"]
)
@pytest.mark.parametrize("operator,suffix", [(">=", ""), ("==", ""), (">=", ", <1")])
def test_manifest_and_new_transitive_use_real_worker_checks(
    tmp_path, api, finding, operator, suffix
):
    root = """
[[package]]
name = "workspace"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "ruff" }]
[package.metadata]
requires-dist = [{ name = "ruff", specifier = ">=0.15.1" }]
"""
    if suffix:
        root = root.replace(">=0.15.1", "<1,>=0.15.1")
    old = LOCK + root.replace(">=", operator)
    new = (
        old.replace("0.15.1", "0.15.2").replace(
            'source = { registry = "https://pypi.org/simple" }',
            'dependencies = [{ name = "ordinary-transitive" }]\n'
            'source = { registry = "https://pypi.org/simple" }',
            1,
        )
        + """
[[package]]
name = "ordinary-transitive"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
{ url = "https://files.pythonhosted.org/transitive.whl", hash = "sha256:transitive", size = 42 },
]
"""
    )
    manifest = f'[project]\ndependencies = ["ruff{operator}0.15.1{suffix}"]\n'
    new_manifest = manifest.replace(
        "0.15.1", "0.15.3" if finding == "unmatched-floor" else "0.15.2"
    )
    set_changed_files(api, {"uv.lock": (old, new), "pyproject.toml": (manifest, new_manifest)})
    release = copy.deepcopy(api["pypi"])
    release["info"]["version"] = "1.0.0"
    release["urls"][0].update(url="https://files.pythonhosted.org/transitive.whl")
    release["urls"][0]["digests"]["sha256"] = "transitive"
    api["registry"] = {"https://pypi.org/pypi/ordinary-transitive/1.0.0/json": release}
    report = api["report"]["Results"][0]
    if finding != "missing-inventory":
        report["Packages"].append({"Name": "ordinary-transitive", "Version": "1.0.0"})
    if finding == "high":
        report["Vulnerabilities"].append(
            {
                "PkgName": "ordinary-transitive",
                "InstalledVersion": "1.0.0",
                "Severity": "HIGH",
                "VulnerabilityID": "new-advisory",
            }
        )
    elif finding == "young":
        release["urls"][0]["upload_time_iso_8601"] = "2100-01-01T00:00:00Z"
    elif finding == "bad-hash":
        release["urls"][0]["digests"]["sha256"] = "wrong"
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    merged = any(call[:2] == ["pr", "merge"] for call in calls)
    assert merged is (finding == "clean"), process.stdout
