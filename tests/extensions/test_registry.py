from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import omnigent.extensions.registry as registry
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    CommandContribution,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)


class _Distribution:
    def __init__(
        self,
        name: str,
        version: str | None = None,
        files: tuple[str, ...] | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.files = files


class _EntryPoint:
    def __init__(
        self,
        name: str,
        loader: Callable[[], ExtensionManifest] | object,
        *,
        distribution: str | None = None,
        version: str | None = None,
        module: str | None = None,
        files: tuple[str, ...] | None = None,
    ) -> None:
        self.name = name
        self._loader = loader
        self.module = module
        self.value = f"{module}:get_manifest" if module else ""
        self.dist = _Distribution(distribution, version, files) if distribution else None

    def load(self) -> Callable[[], ExtensionManifest] | object:
        return self._loader


@pytest.fixture(autouse=True)
def _reset_plugin_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    registry.reset_plugin_state_for_tests()
    monkeypatch.setattr(registry, "_builtin_manifests", lambda: ())
    monkeypatch.setattr(registry, "VERSION", "0.11.0.dev0")
    yield
    registry.reset_plugin_state_for_tests()


def _manifest(
    extension_id: str = "acme.review",
    *,
    page_suffix: str = "dashboard",
    command_suffix: str = "open",
) -> ExtensionManifest:
    page_id = f"{extension_id}.{page_suffix}"
    command_id = f"{extension_id}.{command_suffix}"
    return ExtensionManifest(
        id=extension_id,
        display_name="Acme Review",
        distribution=f"omnigent-{extension_id.replace('.', '-')}",
        version="1.2.0",
        extension_api=EXTENSION_API_VERSION,
        requires_omnigent=">=0.11,<1",
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset({ExtensionPermission.NAVIGATION, ExtensionPermission.STORAGE_USER}),
        activation_events=(f"onPage:{page_id}", f"onCommand:{command_id}"),
        pages=(
            PageContribution(
                id=page_id,
                title="Review dashboard",
                route=page_suffix,
                view="review-dashboard",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id=f"{extension_id}.primary-nav",
                label="Code Review",
                page=page_id,
                icon="search",
                order=350,
            ),
        ),
        commands=(CommandContribution(id=command_id, title="Open review"),),
    )


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *entry_points: _EntryPoint,
) -> None:
    monkeypatch.setattr(registry, "_entry_points", lambda: entry_points)


def test_modern_entry_point_discovery_selects_extension_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = importlib.metadata.EntryPoint(
        name="review",
        value="acme_review.plugin:get_manifest",
        group=registry.ENTRY_POINT_GROUP,
    )
    ignored = importlib.metadata.EntryPoint(
        name="other",
        value="other.plugin:get_manifest",
        group="other.group",
    )
    discovered = importlib.metadata.EntryPoints((ignored, expected))

    def entry_points(**kwargs: str) -> importlib.metadata.EntryPoints:
        assert kwargs == {"group": registry.ENTRY_POINT_GROUP}
        return discovered.select(**kwargs)

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", entry_points)

    assert registry._entry_points() == (expected,)


def test_discovers_and_caches_valid_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def contribution() -> ExtensionManifest:
        nonlocal calls
        calls += 1
        return _manifest()

    _install_entry_points(monkeypatch, _EntryPoint("review", contribution))

    first = registry.plugin_state()
    second = registry.plugin_state()

    assert first is second
    assert calls == 1
    assert first.manifests == (_manifest(),)
    assert first.load_errors == {}
    assert registry.extension_manifest("acme.review") == _manifest()
    assert registry.extension_manifest("missing.extension") is None


def test_plugin_state_initializes_once_across_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def contribution() -> ExtensionManifest:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return _manifest()

    _install_entry_points(monkeypatch, _EntryPoint("review", contribution))

    with ThreadPoolExecutor(max_workers=8) as executor:
        states = list(executor.map(lambda _index: registry.plugin_state(), range(16)))

    assert calls == 1
    assert all(state is states[0] for state in states)


def test_reentrant_registry_read_rejects_plugin_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def contribution() -> ExtensionManifest:
        registry.plugin_state()
        return _manifest()

    _install_entry_points(monkeypatch, _EntryPoint("review", contribution))

    state = registry.plugin_state()

    assert state.manifests == ()
    assert "cannot be read during discovery" in state.load_errors["review"]


def test_accepts_manifest_object_without_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(monkeypatch, _EntryPoint("review", _manifest()))

    assert registry.extension_manifests() == (_manifest(),)


def test_records_verified_asset_package(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    _install_entry_points(
        monkeypatch,
        _EntryPoint(
            "review",
            manifest,
            distribution=manifest.distribution,
            version=manifest.version,
            module="acme_review.plugin",
            files=("acme_review/__init__.py", "acme_review/dist/extension.js"),
        ),
    )

    state = registry.plugin_state()

    assert state.asset_package(manifest.id) == "acme_review"


def test_does_not_record_unverified_or_core_asset_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unverified = _manifest("acme.unverified")
    core = _manifest("acme.core")
    _install_entry_points(
        monkeypatch,
        _EntryPoint(
            "unverified",
            unverified,
            distribution=unverified.distribution,
            module="not_owned.plugin",
            files=("different_package/__init__.py",),
        ),
        _EntryPoint(
            "core",
            core,
            distribution=core.distribution,
            module="omnigent.extensions",
            files=("omnigent/__init__.py",),
        ),
    )

    state = registry.plugin_state()

    assert state.asset_package(unverified.id) is None
    assert state.asset_package(core.id) is None


def test_records_bad_return_type_without_breaking_healthy_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(
        monkeypatch,
        _EntryPoint("bad", lambda: object()),
        _EntryPoint("review", _manifest),
    )

    state = registry.plugin_state()

    assert state.manifests == (_manifest(),)
    assert "expected ExtensionManifest" in state.load_errors["bad"]


def test_records_raising_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> ExtensionManifest:
        raise RuntimeError("extension import failed")

    _install_entry_points(monkeypatch, _EntryPoint("broken", broken))

    state = registry.plugin_state()

    assert state.manifests == ()
    assert state.load_errors["broken"] == "extension import failed"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "review"}, "publisher-qualified"),
        ({"id": "Acme.Review"}, "publisher-qualified"),
        ({"display_name": ""}, "display_name"),
        ({"distribution": " omnigent-review"}, "distribution"),
        ({"version": "not a version"}, "invalid version"),
        ({"extension_api": EXTENSION_API_VERSION + 1}, "extension API"),
        ({"extension_api": True}, "extension API"),
        ({"requires_omnigent": "definitely-not-a-range"}, "invalid Omnigent requirement"),
        ({"requires_omnigent": "<0"}, "requires Omnigent"),
    ],
)
def test_rejects_invalid_manifest_fields(
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
    message: str,
) -> None:
    manifest = replace(_manifest(), **change)
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    state = registry.plugin_state()

    assert state.manifests == ()
    assert message in state.load_errors["review"]


@pytest.mark.parametrize(
    "change",
    [
        {"id": None},
        {"display_name": 123},
        {"version": b"1.0"},
        {"requires_omnigent": None},
    ],
)
def test_non_string_manifest_fields_raise_typed_error(change: dict[str, object]) -> None:
    with pytest.raises(registry.ExtensionValidationError, match="must be a string"):
        registry.validate_manifest(replace(_manifest(), **change))


def test_non_string_contribution_field_raises_typed_error() -> None:
    page = replace(_manifest().pages[0], title=None)  # type: ignore[arg-type]

    with pytest.raises(registry.ExtensionValidationError, match="must be a string"):
        registry.validate_manifest(replace(_manifest(), pages=(page,)))


@pytest.mark.parametrize("value", ["line\nbreak", "nul\x00byte", "bidi\u202eoverride"])
def test_rejects_non_printable_display_text(value: str) -> None:
    with pytest.raises(registry.ExtensionValidationError, match="non-printable"):
        registry.validate_manifest(replace(_manifest(), display_name=value))


@pytest.mark.parametrize("value", ["../../secret", "view name", "view<script>"])
def test_rejects_unsafe_view_symbol(value: str) -> None:
    page = replace(_manifest().pages[0], view=value)

    with pytest.raises(registry.ExtensionValidationError, match="letters, digits"):
        registry.validate_manifest(replace(_manifest(), pages=(page,)))


def test_rejects_unsafe_icon_symbol() -> None:
    navigation = replace(_manifest().primary_navigation[0], icon="icon\x00name")

    with pytest.raises(registry.ExtensionValidationError, match="non-printable"):
        registry.validate_manifest(replace(_manifest(), primary_navigation=(navigation,)))


def test_rejects_mutable_contribution_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(_manifest(), pages=list(_manifest().pages))  # type: ignore[arg-type]
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "pages must be a tuple" in registry.plugin_state().load_errors["review"]


def test_rejects_wrong_contribution_type(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(_manifest(), commands=(object(),))  # type: ignore[arg-type]
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert (
        "commands contains invalid contribution types"
        in registry.plugin_state().load_errors["review"]
    )


def test_requires_browser_entrypoint_for_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(_manifest(), entrypoints=ExtensionEntrypoints())
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "has no browser entrypoint" in registry.plugin_state().load_errors["review"]


def test_rejects_browser_css_without_browser_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = replace(
        _manifest(),
        pages=(),
        primary_navigation=(),
        activation_events=("onCommand:acme.review.open",),
        entrypoints=ExtensionEntrypoints(browser_css="dist/extension.css"),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "browser CSS but no browser entrypoint" in registry.plugin_state().load_errors["review"]


def test_rejects_unknown_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(
        _manifest(),
        permissions=frozenset({"sessions.delete"}),  # type: ignore[arg-type]
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "unsupported permissions" in registry.plugin_state().load_errors["review"]


def test_rejects_non_integer_navigation_order(monkeypatch: pytest.MonkeyPatch) -> None:
    nav = replace(_manifest().primary_navigation[0], order=3.5)  # type: ignore[arg-type]
    manifest = replace(_manifest(), primary_navigation=(nav,))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "order must be an integer" in registry.plugin_state().load_errors["review"]


@pytest.mark.parametrize(
    "path",
    [
        "/dist/extension.js",
        "../extension.js",
        "dist/../extension.js",
        "dist\\extension.js",
        "dist/extension.js?raw=1",
        "./dist/extension.js",
        "%2e%2e/extension.js",
        "dist/ext\x00ension.js",
        "dist/ext ension.js",
        "C:/extension.js",
    ],
)
def test_rejects_unsafe_browser_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser=path),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "browser entrypoint" in registry.plugin_state().load_errors["review"]


def test_requires_javascript_suffix_for_browser_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser="dist/extension.txt"),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert (
        "browser entrypoint must use one of: .js" in registry.plugin_state().load_errors["review"]
    )


def test_requires_fixed_browser_bundle_location(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(browser="other/extension.js"),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "must be dist/extension.js" in registry.plugin_state().load_errors["review"]


def test_requires_css_suffix_for_browser_styles(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = replace(
        _manifest(),
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.txt",
        ),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert (
        "browser CSS entrypoint must use one of: .css"
        in registry.plugin_state().load_errors["review"]
    )


def test_rejects_unsafe_or_dynamic_route(monkeypatch: pytest.MonkeyPatch) -> None:
    page = replace(_manifest().pages[0], route="dashboard/:section")
    manifest = replace(_manifest(), pages=(page,))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "unsupported route segment" in registry.plugin_state().load_errors["review"]


def test_rejects_contribution_outside_extension_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = replace(_manifest().pages[0], id="other.publisher.dashboard")
    manifest = replace(_manifest(), pages=(page,))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "must be namespaced" in registry.plugin_state().load_errors["review"]


def test_rejects_navigation_reference_to_unknown_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nav = replace(_manifest().primary_navigation[0], page="acme.review.missing")
    manifest = replace(_manifest(), primary_navigation=(nav,))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "references unknown page" in registry.plugin_state().load_errors["review"]


def test_rejects_activation_reference_to_unknown_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = replace(_manifest(), activation_events=("onCommand:acme.review.missing",))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "unknown contribution" in registry.plugin_state().load_errors["review"]


def test_rejects_duplicate_page_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    second = PageContribution(
        id="acme.review.other",
        title="Other",
        route="dashboard",
        view="other",
    )
    manifest = replace(_manifest(), pages=(*_manifest().pages, second))
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "duplicate page routes" in registry.plugin_state().load_errors["review"]


def test_rejects_duplicate_ids_across_contribution_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = CommandContribution(id="acme.review.dashboard", title="Dashboard")
    manifest = replace(
        _manifest(),
        commands=(command,),
        activation_events=("onPage:acme.review.dashboard",),
    )
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    assert "duplicate contribution ids" in registry.plugin_state().load_errors["review"]


def test_rejects_installed_distribution_metadata_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(
        monkeypatch,
        _EntryPoint(
            "review",
            _manifest(),
            distribution="different-package",
            version="9.0.0",
        ),
    )

    error = registry.plugin_state().load_errors["different-package:review"]
    assert "does not match installed distribution" in error


def test_rejects_installed_distribution_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    _install_entry_points(
        monkeypatch,
        _EntryPoint(
            "review",
            manifest,
            distribution=manifest.distribution.replace("-", "_"),
            version="9.0.0",
        ),
    )

    error = registry.plugin_state().load_errors[
        f"{manifest.distribution.replace('-', '_')}:review"
    ]
    assert "does not match installed distribution version" in error


@pytest.mark.parametrize(
    ("running_version", "requirement", "accepted"),
    [
        ("0.11.0.dev0", ">=0.11,<1", True),
        ("0.11.9", ">=0.12", False),
        ("1.0.0rc1", "<1", False),
        ("0.11.0.dev0", "==0.11.0.dev0", False),
    ],
)
def test_core_compatibility_uses_release_line(
    monkeypatch: pytest.MonkeyPatch,
    running_version: str,
    requirement: str,
    accepted: bool,
) -> None:
    monkeypatch.setattr(registry, "VERSION", running_version)
    manifest = replace(_manifest(), requires_omnigent=requirement)
    _install_entry_points(monkeypatch, _EntryPoint("review", manifest))

    state = registry.plugin_state()

    assert bool(state.manifests) is accepted


def test_invalid_running_core_version_has_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "VERSION", "invalid")

    with pytest.raises(registry.ExtensionValidationError, match="Omnigent has invalid version"):
        registry.validate_manifest(_manifest())


def test_accepts_multiple_extensions_in_stable_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = replace(_manifest("zeta.review"), distribution="shared-package")
    second = replace(_manifest("acme.notes"), distribution="shared-package")
    _install_entry_points(
        monkeypatch,
        _EntryPoint("zeta", first),
        _EntryPoint("acme", second),
    )

    state = registry.plugin_state()

    assert [manifest.id for manifest in state.manifests] == ["acme.notes", "zeta.review"]
    assert state.load_errors == {}


def test_rejects_cross_extension_contribution_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _manifest("acme.review")
    second = _manifest("acme.review.sub")
    second = replace(
        second,
        commands=(CommandContribution(id="acme.review.sub.open", title="Open"),),
    )
    first = replace(
        first,
        commands=(CommandContribution(id="acme.review.sub.open", title="Open tools"),),
        activation_events=("onPage:acme.review.dashboard",),
    )
    _install_entry_points(
        monkeypatch,
        _EntryPoint("review", first),
        _EntryPoint("tools", second),
    )

    state = registry.plugin_state()

    assert state.manifests == ()
    assert "contribution:acme.review.sub.open" in state.load_errors["review"]
    assert "contribution:acme.review.sub.open" in state.load_errors["tools"]


def test_rejects_all_community_extensions_in_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _manifest()
    second = replace(first, distribution="other-review", version="2.0.0")
    _install_entry_points(
        monkeypatch,
        _EntryPoint("z-review", second),
        _EntryPoint("a-review", first),
    )

    state = registry.plugin_state()

    assert state.manifests == ()
    assert "extension:acme.review" in state.load_errors["a-review"]
    assert "extension:acme.review" in state.load_errors["z-review"]
    assert "'a-review', 'z-review'" in state.load_errors["a-review"]


def test_rejects_community_collision_with_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = _manifest()
    monkeypatch.setattr(registry, "_builtin_manifests", lambda: (builtin,))
    community = replace(builtin, distribution="community-review")
    _install_entry_points(monkeypatch, _EntryPoint("review", community))

    state = registry.plugin_state()

    assert state.manifests == (builtin,)
    assert "reserved by built-in extension 'acme.review'" in state.load_errors["review"]


def test_accepts_multiple_builtins_from_core_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = replace(_manifest("zeta.review"), distribution="omnigent")
    second = replace(_manifest("acme.notes"), distribution="omnigent")
    monkeypatch.setattr(registry, "_builtin_manifests", lambda: (first, second))
    _install_entry_points(monkeypatch)

    assert [manifest.id for manifest in registry.extension_manifests()] == [
        "acme.notes",
        "zeta.review",
    ]


def test_rejects_colliding_builtin_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _manifest()
    second = replace(first, display_name="Other built-in")
    monkeypatch.setattr(registry, "_builtin_manifests", lambda: (first, second))
    _install_entry_points(monkeypatch)

    with pytest.raises(RuntimeError, match=r"both claim extension:acme\.review"):
        registry.plugin_state()


def test_uses_distribution_in_diagnostics_and_disambiguates_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(
        monkeypatch,
        _EntryPoint("plugin", lambda: object(), distribution="first-dist"),
        _EntryPoint("plugin", lambda: object(), distribution="first-dist"),
    )

    assert set(registry.plugin_state().load_errors) == {
        "first-dist:plugin",
        "first-dist:plugin#2",
    }


def test_public_validate_manifest_raises_typed_error() -> None:
    with pytest.raises(registry.ExtensionValidationError, match="publisher-qualified"):
        registry.validate_manifest(replace(_manifest(), id="invalid"))


def test_load_errors_are_a_stably_ordered_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(
        monkeypatch,
        _EntryPoint("z-bad", lambda: object()),
        _EntryPoint("a-bad", lambda: object()),
    )

    errors = registry.plugin_state().load_errors

    assert isinstance(errors, dict)
    assert list(errors) == ["a-bad", "z-bad"]
