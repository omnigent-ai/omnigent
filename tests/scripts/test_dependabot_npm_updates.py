"""Exercise npm graph policy through the shared evaluator and trusted merge CLI."""

from __future__ import annotations

import copy
import json
from urllib.parse import quote

import pytest
import yaml

from tests.scripts.test_dependabot_automerge import (
    BASE,
    HEAD,
    PREFIX,
    ROOT,
    run_cli,
    set_changed_files,
    set_git_files,
)
from tests.scripts.test_dependabot_automerge import api as api
from tests.scripts.test_dependabot_automerge import evaluate_fixture as evaluate_fixture
from tests.scripts.test_dependabot_automerge import expanded_update as expanded_update
from tests.scripts.test_dependabot_automerge import policy_module as policy_module

pytestmark = pytest.mark.parametrize("expanded_update", ["npm"], indirect=True)


def add_package(api, lock, name, pin, **metadata):
    integrity = f"sha512-{name}-{pin}"
    lock["packages"][f"{name}@{pin}"] = {"resolution": {"integrity": integrity}, **metadata}
    lock["snapshots"][f"{name}@{pin}"] = {}
    registry = api["registry"].setdefault(
        f"https://registry.npmjs.org/{quote(name, safe='')}", {"versions": {}, "time": {}}
    )
    registry["versions"][pin] = {
        "name": name,
        "version": pin,
        "dist": {
            "integrity": integrity,
            "tarball": f"https://registry.npmjs.org/{name}/-/package-{pin}.tgz",
        },
    }
    registry["time"][pin] = "2020-01-01T00:00:00Z"


def graph_update(api):
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    add_package(api, before, "debug", "4.0.0")
    before["snapshots"]["clsx@2.1.0"] = {"dependencies": {"debug": "4.0.0"}}
    add_package(api, after, "picocolors", "1.1.0", engines={"node": ">=20"})
    after["snapshots"]["clsx@2.1.1"] = {"dependencies": {"picocolors": "1.1.0"}}
    after["packages"]["clsx@2.1.1"].update(
        engines={"node": ">=20"}, peerDependencies={"picocolors": "^1.0.0"}
    )
    return before, after


def save_locks(api, before, after):
    api["files"]["pnpm-lock.yaml"] = tuple(yaml.safe_dump(lock) for lock in (before, after))


def reused_child_update(api, parent, section="dependencies", name="nanoid"):
    before, after = graph_update(api)
    for lock in (before, after):
        add_package(api, lock, name, "5.0.0")
        lock["importers"]["web"]["dependencies"][name] = {
            "specifier": "^5.0.0",
            "version": "5.0.0",
        }
    api["files"]["web/package.json"] = tuple(
        json.dumps({**manifest, "dependencies": {**manifest["dependencies"], name: "^5.0.0"}})
        for manifest in map(json.loads, api["files"]["web/package.json"])
    )
    snapshot = after["snapshots"]["clsx@2.1.1"]
    if parent == "new":
        add_package(api, after, "new-parent", "1.0.0")
        snapshot["dependencies"]["new-parent"] = "1.0.0"
        snapshot = after["snapshots"]["new-parent@1.0.0"]
    snapshot.setdefault(section, {})["shared-alias"] = f"{name}@5.0.0"
    save_locks(api, before, after)


@pytest.mark.parametrize("parent", ["new", "upgraded"])
@pytest.mark.parametrize("section", ["dependencies", "optionalDependencies"])
@pytest.mark.parametrize("fault", [None, "manual", "integrity", "young"])
def test_reused_child_edges_require_policy_registry_and_inventory(
    evaluate_fixture, policy_module, parent, section, fault
):
    api, evaluate = evaluate_fixture
    name = "@openai/codex" if fault == "manual" else "nanoid"
    reused_child_update(api, parent, section, name)
    metadata = api["registry"][f"https://registry.npmjs.org/{quote(name, safe='')}"]
    if fault == "integrity":
        metadata["versions"]["5.0.0"]["dist"]["integrity"] = "sha512-mismatch"
    elif fault == "young":
        metadata["time"]["5.0.0"] = "2100-01-01T00:00:00Z"
    if fault:
        with pytest.raises(policy_module.NeedsReview):
            evaluate()
    else:
        expected = {"clsx==2.1.1", "picocolors==1.1.0", "nanoid==5.0.0"}
        if parent == "new":
            expected.add("new-parent==1.0.0")
        assert set(evaluate()["packages"]) == expected


@pytest.mark.parametrize("parent", ["new", "upgraded"])
@pytest.mark.parametrize(
    "inventory", ["complete", "missing", "missing_descendant", "high_advisory", "high_descendant"]
)
def test_reused_child_edges_must_pass_merge_advisory_inventory(
    tmp_path, expanded_update, parent, inventory
):
    _ecosystem, api = expanded_update
    reused_child_update(api, parent)
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    for lock in (before, after):
        add_package(api, lock, "leaf", "4.0.0")
        lock["snapshots"]["nanoid@5.0.0"] = {"dependencies": {"leaf": "4.0.0"}}
    save_locks(api, before, after)
    set_changed_files(api, api["files"])
    report = api["report"]["Results"][0]
    report["Packages"].append({"Name": "picocolors", "Version": "1.1.0"})
    if parent == "new":
        report["Packages"].append({"Name": "new-parent", "Version": "1.0.0"})
    if inventory != "missing":
        report["Packages"].append({"Name": "nanoid", "Version": "5.0.0"})
    if inventory != "missing_descendant":
        report["Packages"].append({"Name": "leaf", "Version": "4.0.0"})
    if inventory in {"high_advisory", "high_descendant"}:
        report["Vulnerabilities"].append(
            {
                "PkgName": "leaf" if inventory == "high_descendant" else "nanoid",
                "InstalledVersion": "4.0.0" if inventory == "high_descendant" else "5.0.0",
                "Severity": "HIGH",
                "VulnerabilityID": "CVE-reused-child",
            }
        )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (inventory == "complete"), (
        process.stdout
    )


@pytest.mark.parametrize("parent", ["new", "upgraded"])
@pytest.mark.parametrize("fault", [None, "manual", "integrity", "young"])
def test_reused_child_edge_descendants_require_validation(
    evaluate_fixture, policy_module, parent, fault
):
    api, evaluate = evaluate_fixture
    reused_child_update(api, parent)
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    name = "@openai/codex" if fault == "manual" else "leaf"
    for lock in (before, after):
        add_package(api, lock, name, "4.0.0")
        lock["snapshots"]["nanoid@5.0.0"] = {"optionalDependencies": {name: "4.0.0"}}
    save_locks(api, before, after)
    metadata = api["registry"][f"https://registry.npmjs.org/{quote(name, safe='')}"]
    if fault == "integrity":
        metadata["versions"]["4.0.0"]["dist"]["integrity"] = "sha512-mismatch"
    elif fault == "young":
        metadata["time"]["4.0.0"] = "2100-01-01T00:00:00Z"
    if fault:
        with pytest.raises(policy_module.NeedsReview):
            evaluate()
    else:
        assert "leaf==4.0.0" in evaluate()["packages"]


@pytest.mark.parametrize(
    "old,new,allowed",
    [
        ("4.0.0", "4.1.0", True),
        ("4.0.0", "5.0.0", False),
        ("0.2.0", "0.3.0", True),
        ("4.1.0", "4.0.0", False),
    ],
)
@pytest.mark.parametrize("cycle", [False, True])
def test_existing_child_snapshot_pairs_recursively_enforce_version_policy(
    evaluate_fixture, policy_module, old, new, allowed, cycle
):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    for lock in (before, after):
        for name, pin in (
            ("holder", "1.0.0"),
            ("child", "1.0.0"),
            ("child", "1.1.0"),
            ("leaf", old),
            ("leaf", new),
        ):
            add_package(api, lock, name, pin)
        lock["importers"]["web"]["dependencies"]["holder"] = {
            "specifier": "^1.0.0",
            "version": "1.0.0",
        }
        lock["snapshots"]["holder@1.0.0"] = {
            "dependencies": {"old-child": "child@1.0.0", "new-child": "child@1.1.0"}
        }
        lock["snapshots"]["child@1.0.0"] = {"optionalDependencies": {"leaf": old}}
        lock["snapshots"]["child@1.1.0"] = {"optionalDependencies": {"leaf": new}}
        if cycle:
            lock["snapshots"][f"leaf@{old}"] = {"dependencies": {"child": "1.0.0"}}
            lock["snapshots"][f"leaf@{new}"] = {"dependencies": {"child": "1.1.0"}}
    before["snapshots"]["clsx@2.1.0"]["dependencies"]["child"] = "1.0.0"
    after["snapshots"]["clsx@2.1.1"]["dependencies"]["child"] = "1.1.0"
    save_locks(api, before, after)
    if allowed:
        assert {"child==1.1.0", f"leaf=={new}"} <= set(evaluate()["packages"])
    else:
        with pytest.raises(policy_module.NeedsReview):
            evaluate()


def test_transitive_additions_removals_and_release_metadata(evaluate_fixture):
    api, evaluate = evaluate_fixture
    save_locks(api, *graph_update(api))
    assert evaluate()["packages"] == ["clsx==2.1.1", "picocolors==1.1.0"]


@pytest.mark.parametrize("fault", ["integrity", "source", "young", "deprecated", "prerelease"])
def test_every_added_transitive_version_requires_registry_validation(
    evaluate_fixture, policy_module, fault
):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    metadata = api["registry"]["https://registry.npmjs.org/picocolors"]
    release = metadata["versions"]["1.1.0"]
    if fault == "young":
        metadata["time"]["1.1.0"] = "2100-01-01T00:00:00Z"
    elif fault == "deprecated":
        release["deprecated"] = "Use another package"
    elif fault == "prerelease":
        after["packages"]["picocolors@1.1.0-rc.1"] = after["packages"].pop("picocolors@1.1.0")
        after["snapshots"]["picocolors@1.1.0-rc.1"] = {}
        del after["snapshots"]["picocolors@1.1.0"]
        after["snapshots"]["clsx@2.1.1"]["dependencies"]["picocolors"] = "1.1.0-rc.1"
    else:
        release["dist"]["integrity" if fault == "integrity" else "tarball"] = "untrusted"
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


def test_multiple_transitive_versions_preserve_upgrade_policy(evaluate_fixture):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    for lock in (before, after):
        add_package(api, lock, "picocolors", "1.0.0")
        add_package(api, lock, "nanoid", "5.0.0")
        lock["importers"]["web"]["dependencies"]["nanoid"] = {
            "specifier": "^5.0.0",
            "version": "5.0.0",
        }
        lock["snapshots"]["nanoid@5.0.0"] = {"dependencies": {"picocolors": "1.0.0"}}
    add_package(api, after, "debug", "4.1.0")
    after["snapshots"]["clsx@2.1.1"]["dependencies"]["debug"] = "4.1.0"
    save_locks(api, before, after)
    assert set(evaluate()["packages"]) == {"clsx==2.1.1", "picocolors==1.1.0", "debug==4.1.0"}


@pytest.mark.parametrize("retain_old", [False, True])
@pytest.mark.parametrize("old,new", [("4.0.0", "5.0.0"), ("4.1.0", "4.0.0")])
def test_upgraded_parent_cannot_bypass_transitive_version_policy(
    evaluate_fixture, policy_module, retain_old, old, new
):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    del before["packages"]["debug@4.0.0"]
    del before["snapshots"]["debug@4.0.0"]
    add_package(api, before, "debug", old)
    before["snapshots"]["clsx@2.1.0"]["dependencies"]["debug"] = old
    add_package(api, after, "debug", new)
    after["snapshots"]["clsx@2.1.1"]["dependencies"]["debug"] = new
    if retain_old:
        for lock in (before, after):
            add_package(api, lock, "debug", old)
            add_package(api, lock, "nanoid", "5.0.0")
            lock["importers"]["web/electron"] = {
                "dependencies": {"nanoid": {"specifier": "^5.0.0", "version": "5.0.0"}}
            }
            lock["snapshots"]["nanoid@5.0.0"] = {"dependencies": {"debug": old}}
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("old,new", [("4.0.0", "5.0.0"), ("4.1.0", "4.0.0")])
def test_upgraded_parent_cannot_switch_to_an_ineligible_existing_resolution(
    evaluate_fixture, policy_module, old, new
):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    del before["packages"]["debug@4.0.0"]
    del before["snapshots"]["debug@4.0.0"]
    add_package(api, before, "debug", old)
    before["snapshots"]["clsx@2.1.0"]["dependencies"]["debug"] = old
    for lock in (before, after):
        add_package(api, lock, "debug", new)
        add_package(api, lock, "nanoid", "5.0.0")
        lock["importers"]["web"]["dependencies"]["nanoid"] = {
            "specifier": "^5.0.0",
            "version": "5.0.0",
        }
        lock["snapshots"]["nanoid@5.0.0"] = {"dependencies": {"debug": new}}
    after["snapshots"]["clsx@2.1.1"]["dependencies"]["debug"] = new
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize(
    "fault", ["resolution", "marker", "peer_marker", "unrelated_edge", "downgrade"]
)
def test_existing_resolutions_cannot_be_rewritten(evaluate_fixture, policy_module, fault):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    for lock in (before, after):
        add_package(api, lock, "nanoid", "5.0.0")
        lock["snapshots"]["nanoid@5.0.0"] = {"optional": True}
        lock["importers"]["web"]["dependencies"]["nanoid"] = {
            "specifier": "^5.0.0",
            "version": "5.0.0",
        }
    if fault == "resolution":
        after["packages"]["nanoid@5.0.0"]["resolution"]["integrity"] = "sha512-rewritten"
    elif fault == "marker":
        after["snapshots"]["nanoid@5.0.0"]["optional"] = False
    elif fault == "peer_marker":
        after["snapshots"]["nanoid@5.0.0"]["transitivePeerDependencies"] = ["unexplained"]
    elif fault == "unrelated_edge":
        after["snapshots"]["nanoid@5.0.0"]["dependencies"] = {"picocolors": "1.1.0"}
    else:
        for lock in (before, after):
            add_package(api, lock, "picocolors", "1.0.0")
            add_package(api, lock, "picocolors", "1.2.0")
        before["snapshots"]["nanoid@5.0.0"]["dependencies"] = {"picocolors": "1.2.0"}
        after["snapshots"]["nanoid@5.0.0"]["dependencies"] = {"picocolors": "1.0.0"}
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("specifier", ["^1.0.0", "1.0.0", "catalog:"])
def test_peer_context_changes_with_compatible_upgrades(evaluate_fixture, specifier):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    for lock, pin in ((before, "2.1.0"), (after, "2.1.1")):
        add_package(api, lock, "consumer", "1.0.0", peerDependencies={"clsx": "^2.0.0"})
        del lock["snapshots"]["consumer@1.0.0"]
        lock["snapshots"][f"consumer@1.0.0(clsx@{pin})"] = {"dependencies": {"clsx": pin}}
        lock["importers"]["web"]["dependencies"]["consumer"] = {
            "specifier": specifier,
            "version": f"1.0.0(clsx@{pin})",
        }
    for index in (0, 1):
        manifest = json.loads(api["files"]["web/package.json"][index])
        manifest["dependencies"]["consumer"] = specifier
        files = list(api["files"]["web/package.json"])
        files[index] = json.dumps(manifest)
        api["files"]["web/package.json"] = tuple(files)
    if specifier == "catalog:":
        workspace = "packages: [web]\ncatalog: {consumer: 1.0.0}\n"
        api["files"]["pnpm-workspace.yaml"] = (workspace, workspace)
    save_locks(api, before, after)
    assert evaluate()["packages"] == ["clsx==2.1.1", "picocolors==1.1.0"]


def test_multiple_removed_and_added_versions_pair_in_order(evaluate_fixture):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    add_package(api, before, "debug", "4.1.0")
    before["snapshots"]["clsx@2.1.0"]["dependencies"]["other-debug"] = "debug@4.1.0"
    for name, pin in (("debug", "4.0.1"), ("other-debug", "4.1.1")):
        add_package(api, after, "debug", pin)
        after["snapshots"]["clsx@2.1.1"]["dependencies"][name] = f"debug@{pin}"
    save_locks(api, before, after)
    assert set(evaluate()["packages"]) == {
        "clsx==2.1.1",
        "picocolors==1.1.0",
        "debug==4.0.1",
        "debug==4.1.1",
    }


@pytest.mark.parametrize(
    "name",
    [
        "pdfjs-dist",
        "react-pdf",
        "@openai/codex",
        "electron",
        "electron-builder",
        "electron-builder-squirrel-windows",
        "dmg-builder",
        "electron-updater",
        "electron-winstaller",
        "electron-publish",
        "app-builder-lib",
        "app-builder-bin",
        "builder-util",
        "builder-util-runtime",
        "@vscode/vsce",
        "@vscode/vsce-sign",
        "wrangler",
        "katex",
        "shiki",
        "@shikijs/core",
        "vite",
        "tailwindcss",
        "@tailwindcss/vite",
        "@tailwindcss/node",
        "@tailwindcss/postcss",
    ],
)
def test_manual_package_patterns_require_review(policy_module, expanded_update, name):
    from tests.scripts.test_dependabot_automerge import ROOT

    policy = json.loads((ROOT / ".github/dependabot-automerge.json").read_text())["npm"]
    with pytest.raises(policy_module.NeedsReview, match=f"{name} requires human review"):
        policy_module.require_package(name, policy)


@pytest.mark.parametrize(
    "name", ["@openai/codex", "electron-updater", "wrangler", "@shikijs/core"]
)
@pytest.mark.parametrize("direction", ["added", "removed", "direct"])
def test_sensitive_transitives_stay_manual(evaluate_fixture, policy_module, name, direction):
    api, evaluate = evaluate_fixture
    if direction == "direct":
        api["files"] = {
            path: tuple(
                yaml.safe_dump(json.loads(json.dumps(yaml.safe_load(text)).replace("clsx", name)))
                if path.endswith(".yaml")
                else text.replace("clsx", name)
                for text in pair
            )
            for path, pair in api["files"].items()
        }
        api["registry"] = {
            url.replace("clsx", quote(name, safe="")): json.loads(
                json.dumps(metadata).replace("clsx", name)
            )
            for url, metadata in api["registry"].items()
        }
    else:
        before, after = graph_update(api)
        target, pin = (before, "2.1.0") if direction == "removed" else (after, "2.1.1")
        add_package(api, target, name, "1.0.0")
        target["snapshots"][f"clsx@{pin}"]["dependencies"][name] = "1.0.0"
        save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview, match="human review"):
        evaluate()


def test_ordinary_minor_updates_use_the_parent_default_policy(evaluate_fixture):
    api, evaluate = evaluate_fixture
    api["files"] = {
        path: tuple(
            text.replace("clsx", "ordinary-web-library").replace("2.1.1", "2.2.0")
            for text in contents
        )
        for path, contents in api["files"].items()
    }
    api["registry"] = json.loads(
        json.dumps(api["registry"])
        .replace("clsx", "ordinary-web-library")
        .replace("2.1.1", "2.2.0")
    )
    assert evaluate()["packages"] == ["ordinary-web-library==2.2.0"]


@pytest.mark.parametrize("consumer", [".github/ci-deps", "uncovered/workspace", "."])
@pytest.mark.parametrize("section", ["dependencies", "optionalDependencies"])
def test_shared_transitive_changes_need_non_web_coverage(
    evaluate_fixture, policy_module, consumer, section
):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    for lock, pin in ((before, "2.1.0"), (after, "2.1.1")):
        lock["importers"][consumer] = {
            section: {"old-clsx": {"specifier": "npm:clsx@^2.1.0", "version": f"clsx@{pin}"}}
        }
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview, match="non-web"):
        evaluate()


def test_web_can_upgrade_without_changing_another_consumers_old_version(evaluate_fixture):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    after["packages"]["clsx@2.1.0"] = copy.deepcopy(before["packages"]["clsx@2.1.0"])
    after["snapshots"]["clsx@2.1.0"] = copy.deepcopy(before["snapshots"]["clsx@2.1.0"])
    after["packages"]["debug@4.0.0"] = copy.deepcopy(before["packages"]["debug@4.0.0"])
    after["snapshots"]["debug@4.0.0"] = {}
    for lock in (before, after):
        lock["importers"]["web/electron"] = {
            "dependencies": {"clsx": {"specifier": "^2.1.0", "version": "2.1.0"}}
        }
    save_locks(api, before, after)
    assert evaluate()["packages"] == ["clsx==2.1.1", "picocolors==1.1.0"]


@pytest.mark.parametrize(
    "selector",
    [
        "picocolors@>=1.0.0 <2.0.0",
        "clsx>picocolors",
        "clsx>unchanged-child",
        "clsx@>=2>unchanged-child",
    ],
)
@pytest.mark.parametrize("changed", [False, True])
def test_transitive_compatibility_overrides_remain_manual(
    evaluate_fixture, policy_module, selector, changed
):
    api, evaluate = evaluate_fixture
    locks = graph_update(api)
    workspaces = []
    for index, lock in enumerate(locks):
        override = {selector: "1.1.1" if changed and index else "1.1.0"}
        lock["overrides"] = override
        workspaces.append(yaml.safe_dump({"packages": ["web"], "overrides": override}))
    save_locks(api, *locks)
    api["files"]["pnpm-workspace.yaml"] = tuple(workspaces)
    with pytest.raises(
        policy_module.NeedsReview, match="a compatibility override requires review"
    ):
        evaluate()


@pytest.mark.parametrize(
    "fault", ["direct_add", "direct_remove", "root_manifest", "unreachable", "missing"]
)
def test_only_complete_web_graph_updates_qualify(evaluate_fixture, policy_module, fault):
    api, evaluate = evaluate_fixture
    before, after = graph_update(api)
    old_manifest, new_manifest = (json.loads(text) for text in api["files"]["web/package.json"])
    if fault == "direct_add":
        new_manifest["dependencies"]["picocolors"] = "^1.1.0"
        after["importers"]["web"]["dependencies"]["picocolors"] = {
            "specifier": "^1.1.0",
            "version": "1.1.0",
        }
    elif fault == "direct_remove":
        del new_manifest["dependencies"]["clsx"]
        del after["importers"]["web"]["dependencies"]["clsx"]
    elif fault == "root_manifest":
        api["files"]["package.json"] = ('{"private": true}', '{"private": false}')
    elif fault == "unreachable":
        add_package(api, after, "nanoid", "5.0.0")
    else:
        del after["snapshots"]["picocolors@1.1.0"]
    api["files"]["web/package.json"] = tuple(json.dumps(m) for m in (old_manifest, new_manifest))
    save_locks(api, before, after)
    with pytest.raises(policy_module.NeedsReview):
        evaluate()


@pytest.mark.parametrize("inventory", ["complete", "missing", "high_advisory"])
def test_added_transitive_versions_must_pass_merge_advisory_inventory(
    tmp_path, expanded_update, inventory
):
    _ecosystem, api = expanded_update
    save_locks(api, *graph_update(api))
    set_changed_files(api, api["files"])
    report = api["report"]["Results"][0]
    if inventory != "missing":
        report["Packages"].append({"Name": "picocolors", "Version": "1.1.0"})
    if inventory == "high_advisory":
        report["Vulnerabilities"].append(
            {
                "PkgName": "picocolors",
                "InstalledVersion": "1.1.0",
                "Severity": "HIGH",
                "VulnerabilityID": "CVE-new-transitive",
            }
        )
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (inventory == "complete"), (
        process.stdout
    )


@pytest.mark.parametrize(
    "consumer",
    [
        "web/electron",
        "editors/vscode",
        "sdks/web-extension",
        "examples/extensions/hello-page/web",
        "deploy/cloudflare",
        "shared",
    ],
)
@pytest.mark.parametrize("fault", [None, "scripts"])
def test_shared_workspace_update_requires_consuming_steps(
    evaluate_fixture, policy_module, consumer, fault
):
    api, evaluate = evaluate_fixture
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    consumers = ["web/electron", "editors/vscode"] if consumer == "shared" else [consumer]
    for importer in consumers:
        for lock in (before, after):
            lock["importers"][importer] = copy.deepcopy(lock["importers"]["web"])
        api["files"][f"{importer}/package.json"] = api["files"]["web/package.json"]
    if fault == "scripts":
        old_manifest, new_manifest = api["files"][f"{consumers[0]}/package.json"]
        changed = json.loads(new_manifest)
        changed["scripts"] = {"postinstall": "arbitrary-command"}
        api["files"][f"{consumers[0]}/package.json"] = old_manifest, json.dumps(changed)
    save_locks(api, before, after)
    if fault:
        with pytest.raises(
            policy_module.NeedsReview, match="manifest scripts or other settings changed"
        ):
            evaluate()
        return
    result = evaluate()
    assert result["packages"] == ["clsx==2.1.1"]
    expected = (
        [
            {"workflow": "docker-build.yml", "job": "Docker build", "step": step}
            for step in (
                "Cloudflare Worker and container dry-run",
                "Build and smoke-test the Cloudflare image",
            )
        ]
        if consumer == "deploy/cloudflare"
        else [
            {
                "workflow": "web-tests.yml",
                "job": "web test",
                "step": "Test and build workspace consumers",
            }
        ]
    )
    assert result["consumer_steps"] == expected
    assert all(step["workflow"] in result["workflows"] for step in expected)


@pytest.mark.parametrize("catalog", ["", "build"])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "settings",
        "major",
        "downgrade",
        "lock_mismatch",
        "override",
        "override_lock_mismatch",
        "integrity",
        "source",
        "young",
    ],
)
def test_catalog_and_override_pins_preserve_workspace_policy(
    evaluate_fixture, policy_module, catalog, fault
):
    api, evaluate = evaluate_fixture
    locks = [yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"]]
    workspaces = []
    for lock, pin in zip(locks, ["2.1.0", "2.1.1"], strict=True):
        entries = {"clsx": pin}
        workspace = {"packages": ["web"], "overrides": {"clsx": "catalog:" + catalog}}
        workspace.update({"catalogs": {catalog: entries}} if catalog else {"catalog": entries})
        lock["overrides"] = entries.copy()
        lock["catalogs"] = {catalog or "default": {"clsx": {"specifier": pin, "version": pin}}}
        lock["importers"]["web"]["dependencies"]["clsx"]["specifier"] = "catalog:" + catalog
        workspaces.append(workspace)
    if fault == "settings":
        workspaces[1]["minimumReleaseAge"] = 0
    elif fault in {"major", "downgrade"}:
        entries = workspaces[1]["catalogs"][catalog] if catalog else workspaces[1]["catalog"]
        entries["clsx"] = "3.0.0" if fault == "major" else "2.0.0"
    elif fault == "lock_mismatch":
        locks[1]["catalogs"][catalog or "default"]["clsx"]["specifier"] = "2.1.0"
    elif fault in {"override", "override_lock_mismatch"}:
        workspaces[1]["overrides"]["clsx"] = "^2.1.1" if fault == "override" else "2.1.2"
    elif fault in {"integrity", "source", "young"}:
        metadata = api["registry"]["https://registry.npmjs.org/clsx"]
        if fault == "young":
            metadata["time"]["2.1.1"] = "2100-01-01T00:00:00Z"
        else:
            key = "integrity" if fault == "integrity" else "tarball"
            metadata["versions"]["2.1.1"]["dist"][key] = "untrusted"
    api["files"]["pnpm-workspace.yaml"] = tuple(
        yaml.safe_dump(workspace) for workspace in workspaces
    )
    manifest = json.dumps({"dependencies": {"clsx": "catalog:" + catalog}})
    api["files"]["web/package.json"] = (manifest, manifest)
    save_locks(api, *locks)
    if fault:
        reason = {
            "settings": "pnpm settings changed",
            "major": "Major updates require review",
            "downgrade": "Not an upgrade",
            "lock_mismatch": "catalog/lock mismatch",
            "override": "npm version requirement shape changed",
            "override_lock_mismatch": "compatibility override/lock mismatch",
            "integrity": "npm integrity or source mismatch",
            "source": "npm integrity or source mismatch",
            "young": "cooldown",
        }[fault]
        with pytest.raises(policy_module.NeedsReview, match=reason):
            evaluate()
    else:
        assert evaluate()["packages"] == ["clsx==2.1.1"]


@pytest.mark.parametrize("prefix", ["", "^"])
def test_simple_override_bump_preserves_ordinary_update_policy(evaluate_fixture, prefix):
    api, evaluate = evaluate_fixture
    locks = [yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"]]
    workspaces = []
    for lock, pin in zip(locks, ["2.1.0", "2.1.1"], strict=True):
        lock["overrides"] = {"clsx": prefix + pin}
        workspaces.append(yaml.safe_dump({"packages": ["web"], "overrides": lock["overrides"]}))
    api["files"]["pnpm-workspace.yaml"] = tuple(workspaces)
    save_locks(api, *locks)
    assert evaluate()["packages"] == ["clsx==2.1.1"]


@pytest.mark.parametrize("section", ["catalog", "overrides"])
@pytest.mark.parametrize("value", [None, 211, ["2.1.1"], {"version": "2.1.1"}])
def test_malformed_workspace_value_does_not_abort_later_prs(
    tmp_path, expanded_update, section, value
):
    _, api = expanded_update
    valid_files = copy.deepcopy(api["files"])
    workspace = yaml.safe_dump({"packages": ["web"], section: {"clsx": "^2.1.0"}})
    valid_files["pnpm-workspace.yaml"] = workspace, workspace
    if section == "overrides":
        valid_files["pnpm-lock.yaml"] = tuple(
            yaml.safe_dump({**yaml.safe_load(text), "overrides": {"clsx": "^2.1.0"}})
            for text in valid_files["pnpm-lock.yaml"]
        )
    malformed = copy.deepcopy(valid_files)
    malformed["pnpm-workspace.yaml"] = (
        workspace,
        yaml.safe_dump({"packages": ["web"], section: {"clsx": value}}),
    )
    bad_head = "d" * 40
    pull = copy.deepcopy(api["routes"][f"{PREFIX}/pulls/123"])
    pull.update(number=124)
    pull["head"]["sha"] = bad_head
    api["routes"][f"{PREFIX}/pulls/124"] = pull
    api["routes"][f"{PREFIX}/pulls/124/reviews?per_page=100&page=1"] = []
    api["routes"][f"{PREFIX}/compare/{BASE}...{bad_head}"] = {"merge_base_commit": {"sha": BASE}}
    set_changed_files(api, malformed, number=124, head=bad_head)
    set_changed_files(api, valid_files)
    api["pulls"] = [{"number": 124}, {"number": 123}]
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert (
        "#124: human review or retry required: clsx: npm requirement must be a string"
        in process.stdout
    )
    assert any(call[:3] == ["pr", "merge", "123"] for call in calls), process.stdout


@pytest.mark.parametrize("specifier", ["^2.1.0", "~2.1.0", "2.1.1", "~2.0.0"])
def test_unchanged_simple_overrides_allow_compatible_resolutions(
    evaluate_fixture, policy_module, specifier
):
    api, evaluate = evaluate_fixture
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    for lock in (before, after):
        lock["overrides"] = {"clsx": specifier}
    workspace = yaml.safe_dump({"packages": ["web"], "overrides": {"clsx": specifier}})
    api["files"]["pnpm-workspace.yaml"] = (workspace, workspace)
    save_locks(api, before, after)
    if specifier == "~2.0.0":
        with pytest.raises(
            policy_module.NeedsReview, match="resolution violates compatibility override"
        ):
            evaluate()
    else:
        assert evaluate()["packages"] == ["clsx==2.1.1"]


@pytest.mark.parametrize("conclusion", ["success", "skipped", "failure", "missing"])
@pytest.mark.parametrize("stale", [False, True])
def test_shared_update_through_worker_checks_executed_consumer_step(
    tmp_path, expanded_update, conclusion, stale
):
    _ecosystem, api = expanded_update
    before, after = (yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"])
    consumer = "editors/vscode"
    for lock in (before, after):
        lock["importers"][consumer] = copy.deepcopy(lock["importers"]["web"])
    api["files"][f"{consumer}/package.json"] = api["files"]["web/package.json"]
    save_locks(api, before, after)
    if stale:
        for name in ("web-tests.yml", "security-gate.yml"):
            path = f".github/workflows/{name}"
            workflow = (ROOT / path).read_text()
            api["files"][path] = workflow, workflow
    set_changed_files(api, api["files"])
    jobs = api["routes"]["repos/example/project/actions/runs/16/jobs?per_page=100&page=1"]["jobs"]
    job = next(job for job in jobs if job["name"] == "web test")
    if conclusion != "missing":
        job["steps"] = [{"name": "Test and build workspace consumers", "conclusion": conclusion}]
    if stale:
        tip = "c" * 40
        set_git_files(api, tip, {path: pair[0] for path, pair in api["files"].items()})
        api["routes"][f"{PREFIX}/git/ref/heads/main"]["object"]["sha"] = tip
        api["routes"][f"{PREFIX}/compare/{tip}...{HEAD}"] = {"merge_base_commit": {"sha": BASE}}
        api["routes"][f"{PREFIX}/pulls/123"]["commits"] = 1
        api["routes"][f"{PREFIX}/pulls/123/commits?per_page=100&page=1"] = [
            {"sha": HEAD, "parents": [{"sha": BASE}]}
        ]
    process, calls = run_cli(tmp_path, api, mode="merge", sweep=True)
    assert process.returncode == 0, process.stderr
    assert any(call[:2] == ["pr", "merge"] for call in calls) == (
        conclusion == "success" and not stale
    ), process.stdout
    assert any(call[:3] == ["api", "--method", "PUT"] for call in calls) == (
        conclusion == "success" and stale
    ), process.stdout
    if conclusion != "success":
        assert (
            "waiting for executed consumer step Test and build workspace consumers"
            in process.stdout
        )


@pytest.mark.parametrize(
    "pin,spec,expected",
    [
        ("0.2.4", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.2.2", "~1.2.3", False),
    ],
)
def test_npm_range_boundaries(policy_module, expanded_update, pin, spec, expected):
    assert policy_module.npm_satisfies(pin, spec) is expected


@pytest.mark.parametrize(
    "workspace,spec,reason",
    [
        ({"catalogs": []}, "catalog:build", "pnpm catalogs must be an object"),
        ({"catalog": None}, "catalog:", "pnpm catalog must be an object"),
        ({"catalogs": {"build": []}}, "catalog:build", "pnpm catalog must be an object"),
        ({"catalog": {"clsx": None}}, "catalog:", "npm requirement must be a string"),
    ],
)
def test_catalog_reference_shapes_fail_closed(
    policy_module, expanded_update, workspace, spec, reason
):
    with pytest.raises(policy_module.NeedsReview, match=reason):
        policy_module.npm_specifier("clsx", spec, workspace)


@pytest.mark.parametrize("specifier", [">=1.0.0", "workspace:*"])
def test_unsupported_catalog_requirements_need_review(evaluate_fixture, policy_module, specifier):
    api, evaluate = evaluate_fixture
    locks = [yaml.safe_load(text) for text in api["files"]["pnpm-lock.yaml"]]
    for lock in locks:
        add_package(api, lock, "legacy-catalog", "1.0.0")
        lock["catalogs"] = {
            "default": {"legacy-catalog": {"specifier": specifier, "version": "1.0.0"}}
        }
    workspace = yaml.safe_dump({"packages": ["web"], "catalog": {"legacy-catalog": specifier}})
    api["files"]["pnpm-workspace.yaml"] = (workspace, workspace)
    save_locks(api, *locks)
    with pytest.raises(policy_module.NeedsReview, match="Unsupported npm requirement"):
        evaluate()
