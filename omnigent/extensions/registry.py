"""Discovery and deterministic validation for installed extensions."""

from __future__ import annotations

import importlib.metadata
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from omnigent.extensions.api import (
    EXTENSION_API_VERSION,
    CommandContribution,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    PageContribution,
    PrimaryNavigationContribution,
)
from omnigent.version import VERSION

ENTRY_POINT_GROUP = "omnigent.extensions"
SUPPORTED_EXTENSION_API_VERSIONS = frozenset({EXTENSION_API_VERSION})

_logger = logging.getLogger(__name__)
_state: ExtensionPluginState | None = None
_state_condition = threading.Condition()
_building_thread_id: int | None = None

_ID_SEGMENT = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_EXTENSION_ID_RE = re.compile(rf"^{_ID_SEGMENT}(?:\.{_ID_SEGMENT})+$")
_CONTRIBUTION_ID_RE = re.compile(rf"^{_ID_SEGMENT}(?:[.-]{_ID_SEGMENT})+$")
_ROUTE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
_ASSET_SEGMENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_SYMBOL_RE = _ASSET_SEGMENT_RE
_ACTIVATION_EVENT_RE = re.compile(r"^(onPage|onCommand):(.+)$")
_MAX_TEXT_LENGTH = 512
_MAX_ASSET_DEPTH = 16


class ExtensionValidationError(ValueError):
    """An extension manifest is structurally invalid or incompatible."""


def _entry_point_distribution_name(entry_point: Any) -> str | None:
    """Return an entry point's installed distribution name when available."""
    dist = getattr(entry_point, "dist", None)
    if dist is None:
        return None
    name = getattr(dist, "name", None)
    if isinstance(name, str) and name:
        return name
    metadata = getattr(dist, "metadata", None)
    if metadata is not None:
        metadata_name = metadata.get("Name")
        if isinstance(metadata_name, str) and metadata_name:
            return metadata_name
    return None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    """Return installed extension entry points in deterministic order."""
    discovered = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    return tuple(
        sorted(
            discovered,
            key=lambda item: (
                _entry_point_distribution_name(item) or "",
                item.name,
                getattr(item, "value", ""),
            ),
        )
    )


def _builtin_manifests() -> tuple[ExtensionManifest, ...]:
    """Return core-owned manifests routed through the public validation path."""
    return ()


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ExtensionValidationError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ExtensionValidationError(f"{field_name} must be non-empty and trimmed")
    if len(value) > _MAX_TEXT_LENGTH:
        raise ExtensionValidationError(
            f"{field_name} must be at most {_MAX_TEXT_LENGTH} characters"
        )
    if not value.isprintable():
        raise ExtensionValidationError(f"{field_name} contains non-printable characters")


def _require_symbol(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if not _SYMBOL_RE.fullmatch(value):
        raise ExtensionValidationError(
            f"{field_name} must contain only letters, digits, dots, underscores, and hyphens"
        )


def _validate_extension_id(extension_id: str) -> None:
    _require_text(extension_id, "extension id")
    if not _EXTENSION_ID_RE.fullmatch(extension_id):
        raise ExtensionValidationError(
            f"extension id {extension_id!r} must be a lowercase, publisher-qualified id"
        )


def _validate_contribution_id(extension_id: str, contribution_id: str, kind: str) -> None:
    _require_text(contribution_id, f"{kind} id")
    if not _CONTRIBUTION_ID_RE.fullmatch(contribution_id):
        raise ExtensionValidationError(
            f"{kind} id {contribution_id!r} must contain only lowercase namespaced segments"
        )
    if not contribution_id.startswith(f"{extension_id}."):
        raise ExtensionValidationError(
            f"{kind} id {contribution_id!r} must be namespaced under {extension_id!r}"
        )


def _validate_relative_path(
    value: str,
    field_name: str,
    *,
    route: bool = False,
    suffixes: frozenset[str] | None = None,
) -> None:
    _require_text(value, field_name)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("./")
        or value != path.as_posix()
        or len(path.parts) > _MAX_ASSET_DEPTH
    ):
        raise ExtensionValidationError(f"{field_name} {value!r} is not a safe relative path")
    if route and len(path.parts) != 1:
        raise ExtensionValidationError(f"{field_name} must contain exactly one segment")
    segment_pattern = _ROUTE_SEGMENT_RE if route else _ASSET_SEGMENT_RE
    if any(not segment_pattern.fullmatch(part) for part in path.parts):
        if route:
            raise ExtensionValidationError(
                f"{field_name} {value!r} contains an unsupported route segment"
            )
        raise ExtensionValidationError(f"{field_name} {value!r} is not a safe relative path")
    if suffixes is not None and path.suffix.lower() not in suffixes:
        expected = ", ".join(sorted(suffixes))
        raise ExtensionValidationError(f"{field_name} must use one of: {expected}")


def _validate_page(extension_id: str, page: PageContribution) -> None:
    _validate_contribution_id(extension_id, page.id, "page")
    _require_text(page.title, f"page {page.id!r} title")
    _validate_relative_path(page.route, f"page {page.id!r} route", route=True)
    _require_symbol(page.view, f"page {page.id!r} view")


def _validate_navigation(
    extension_id: str,
    navigation: PrimaryNavigationContribution,
    *,
    page_ids: set[str],
) -> None:
    _validate_contribution_id(extension_id, navigation.id, "primary navigation")
    _require_text(navigation.label, f"primary navigation {navigation.id!r} label")
    _require_text(navigation.page, f"primary navigation {navigation.id!r} page")
    if navigation.page not in page_ids:
        raise ExtensionValidationError(
            f"primary navigation {navigation.id!r} references unknown page {navigation.page!r}"
        )
    if navigation.icon is not None:
        _require_symbol(navigation.icon, f"primary navigation {navigation.id!r} icon")
    if navigation.when is not None:
        _require_text(navigation.when, f"primary navigation {navigation.id!r} when")
    if isinstance(navigation.order, bool) or not isinstance(navigation.order, int):
        raise ExtensionValidationError(
            f"primary navigation {navigation.id!r} order must be an integer"
        )


def _validate_command(extension_id: str, command: CommandContribution) -> None:
    _validate_contribution_id(extension_id, command.id, "command")
    _require_text(command.title, f"command {command.id!r} title")


def _reject_duplicates(values: Iterable[str], kind: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(repr(item) for item in sorted(duplicates))
        raise ExtensionValidationError(f"duplicate {kind}: {rendered}")


def validate_manifest(manifest: ExtensionManifest) -> None:
    """Validate one manifest independently of other installed extensions."""
    if not isinstance(manifest.entrypoints, ExtensionEntrypoints):
        raise ExtensionValidationError("entrypoints must be ExtensionEntrypoints")
    for field_name in ("pages", "primary_navigation", "commands", "activation_events"):
        if not isinstance(getattr(manifest, field_name), tuple):
            raise ExtensionValidationError(f"{field_name} must be a tuple")
    if not isinstance(manifest.permissions, frozenset):
        raise ExtensionValidationError("permissions must be a frozenset")
    for field_name, values, expected_type in (
        ("pages", manifest.pages, PageContribution),
        ("primary_navigation", manifest.primary_navigation, PrimaryNavigationContribution),
        ("commands", manifest.commands, CommandContribution),
    ):
        invalid = [
            type(value).__name__ for value in values if not isinstance(value, expected_type)
        ]
        if invalid:
            raise ExtensionValidationError(
                f"{field_name} contains invalid contribution types: " + ", ".join(invalid)
            )

    _validate_extension_id(manifest.id)
    _require_text(manifest.display_name, "display_name")
    _require_text(manifest.distribution, "distribution")
    _require_text(manifest.version, "version")
    _require_text(manifest.requires_omnigent, "requires_omnigent")

    try:
        Version(manifest.version)
    except InvalidVersion as exc:
        raise ExtensionValidationError(
            f"extension {manifest.id!r} has invalid version {manifest.version!r}"
        ) from exc

    if (
        isinstance(manifest.extension_api, bool)
        or not isinstance(manifest.extension_api, int)
        or manifest.extension_api not in SUPPORTED_EXTENSION_API_VERSIONS
    ):
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_EXTENSION_API_VERSIONS))
        raise ExtensionValidationError(
            f"extension {manifest.id!r} targets unsupported extension API "
            f"{manifest.extension_api!r}; supported versions: {supported}"
        )

    try:
        core_range = SpecifierSet(manifest.requires_omnigent)
    except InvalidSpecifier as exc:
        raise ExtensionValidationError(
            f"extension {manifest.id!r} has invalid Omnigent requirement "
            f"{manifest.requires_omnigent!r}"
        ) from exc
    try:
        # Compatibility ranges target a release line. Treat a local dev/RC build
        # as its base release so normal ranges also work in development and CI.
        compatibility_version = Version(Version(VERSION).base_version)
    except InvalidVersion as exc:
        raise ExtensionValidationError(f"Omnigent has invalid version {VERSION!r}") from exc
    if not core_range.contains(compatibility_version):
        raise ExtensionValidationError(
            f"extension {manifest.id!r} requires Omnigent {manifest.requires_omnigent}; "
            f"running version is {VERSION}"
        )

    if manifest.pages and manifest.entrypoints.browser is None:
        raise ExtensionValidationError(
            f"extension {manifest.id!r} contributes pages but has no browser entrypoint"
        )
    if manifest.entrypoints.browser is not None:
        _validate_relative_path(
            manifest.entrypoints.browser,
            "browser entrypoint",
            suffixes=frozenset({".js"}),
        )
        if manifest.entrypoints.browser != "dist/extension.js":
            raise ExtensionValidationError("browser entrypoint must be dist/extension.js")
    if manifest.entrypoints.browser_css is not None:
        if manifest.entrypoints.browser is None:
            raise ExtensionValidationError(
                f"extension {manifest.id!r} has browser CSS but no browser entrypoint"
            )
        _validate_relative_path(
            manifest.entrypoints.browser_css,
            "browser CSS entrypoint",
            suffixes=frozenset({".css"}),
        )
        if manifest.entrypoints.browser_css != "dist/extension.css":
            raise ExtensionValidationError("browser CSS entrypoint must be dist/extension.css")

    invalid_permissions = sorted(
        repr(permission)
        for permission in manifest.permissions
        if not isinstance(permission, ExtensionPermission)
    )
    if invalid_permissions:
        raise ExtensionValidationError(
            f"extension {manifest.id!r} requests unsupported permissions: "
            + ", ".join(invalid_permissions)
        )

    for page in manifest.pages:
        _validate_page(manifest.id, page)
    for command in manifest.commands:
        _validate_command(manifest.id, command)

    page_ids = {page.id for page in manifest.pages}
    command_ids = {command.id for command in manifest.commands}
    for navigation in manifest.primary_navigation:
        _validate_navigation(manifest.id, navigation, page_ids=page_ids)
    navigation_ids = {navigation.id for navigation in manifest.primary_navigation}

    _reject_duplicates((page.id for page in manifest.pages), "page ids")
    _reject_duplicates((page.route for page in manifest.pages), "page routes")
    _reject_duplicates((command.id for command in manifest.commands), "command ids")
    _reject_duplicates(
        (navigation.id for navigation in manifest.primary_navigation),
        "primary navigation ids",
    )
    _reject_duplicates(
        (*page_ids, *command_ids, *navigation_ids),
        "contribution ids",
    )

    for event in manifest.activation_events:
        _require_text(event, "activation event")
    _reject_duplicates(manifest.activation_events, "activation events")
    for event in manifest.activation_events:
        match = _ACTIVATION_EVENT_RE.fullmatch(event)
        if match is None:
            raise ExtensionValidationError(f"unsupported activation event {event!r}")
        kind, target = match.groups()
        known = page_ids if kind == "onPage" else command_ids
        if target not in known:
            raise ExtensionValidationError(
                f"activation event {event!r} references an unknown contribution"
            )


def _validate_distribution_metadata(entry_point: Any, manifest: ExtensionManifest) -> None:
    """Ensure a manifest describes the distribution that registered it."""
    installed_name = _entry_point_distribution_name(entry_point)
    if installed_name is not None and canonicalize_name(installed_name) != canonicalize_name(
        manifest.distribution
    ):
        raise ExtensionValidationError(
            f"manifest distribution {manifest.distribution!r} does not match installed "
            f"distribution {installed_name!r}"
        )

    dist = getattr(entry_point, "dist", None)
    installed_version = getattr(dist, "version", None)
    if isinstance(installed_version, str) and installed_version:
        try:
            versions_match = Version(installed_version) == Version(manifest.version)
        except InvalidVersion as exc:
            raise ExtensionValidationError(
                f"installed distribution {installed_name or manifest.distribution!r} has "
                f"invalid version {installed_version!r}"
            ) from exc
        if not versions_match:
            raise ExtensionValidationError(
                f"manifest version {manifest.version!r} does not match installed "
                f"distribution version {installed_version!r}"
            )


def _manifest_claims(manifest: ExtensionManifest) -> tuple[str, ...]:
    contribution_ids = (
        *(page.id for page in manifest.pages),
        *(command.id for command in manifest.commands),
        *(item.id for item in manifest.primary_navigation),
    )
    return (
        f"extension:{manifest.id}",
        *(f"contribution:{item}" for item in contribution_ids),
        *(f"route:{manifest.id}/{page.route}" for page in manifest.pages),
    )


def _diagnostic_key(entry_point: Any, used: set[str]) -> str:
    dist_name = _entry_point_distribution_name(entry_point)
    base = f"{dist_name}:{entry_point.name}" if dist_name else entry_point.name
    key = base
    suffix = 2
    while key in used:
        key = f"{base}#{suffix}"
        suffix += 1
    used.add(key)
    return key


def _entry_point_asset_package(entry_point: Any) -> str | None:
    """Return the top-level package when its declaring distribution owns it."""
    module = getattr(entry_point, "module", None)
    if not isinstance(module, str) or not module:
        value = getattr(entry_point, "value", None)
        if not isinstance(value, str) or not value:
            return None
        module = value.partition(":")[0]
    package = module.split(".", 1)[0]
    if package == "omnigent":
        return None

    dist = getattr(entry_point, "dist", None)
    files = getattr(dist, "files", None)
    owned_packages = (
        {str(file).replace("\\", "/").split("/", 1)[0] for file in files}
        if files is not None
        else set()
    )
    read_text = getattr(dist, "read_text", None)
    if callable(read_text):
        top_level = read_text("top_level.txt")
        if isinstance(top_level, str):
            owned_packages.update(line.strip() for line in top_level.splitlines() if line.strip())
    return package if package in owned_packages else None


def _load_community_manifests() -> tuple[
    list[tuple[str, ExtensionManifest, str | None]], dict[str, str]
]:
    candidates: list[tuple[str, ExtensionManifest, str | None]] = []
    errors: dict[str, str] = {}
    used_keys: set[str] = set()
    for entry_point in _entry_points():
        key = _diagnostic_key(entry_point, used_keys)
        try:
            loaded = entry_point.load()
            manifest = loaded() if callable(loaded) else loaded
            if not isinstance(manifest, ExtensionManifest):
                raise TypeError(
                    f"entry point returned {type(manifest).__name__}, expected ExtensionManifest"
                )
            validate_manifest(manifest)
            _validate_distribution_metadata(entry_point, manifest)
            candidates.append((key, manifest, _entry_point_asset_package(entry_point)))
        # Entry points are an external package boundary: one plugin may raise
        # any exception, and must not prevent healthy extensions from loading.
        except Exception as exc:  # noqa: BLE001
            errors[key] = str(exc)
            _logger.warning(
                "could not load extension entry point %s (%s)",
                key,
                exc,
                exc_info=True,
            )
    return candidates, errors


def _validate_builtin_collisions(builtins: tuple[ExtensionManifest, ...]) -> None:
    claims: dict[str, str] = {}
    for manifest in builtins:
        for claim in _manifest_claims(manifest):
            previous = claims.get(claim)
            if previous is not None:
                raise RuntimeError(
                    f"built-in extensions {previous!r} and {manifest.id!r} both claim {claim}"
                )
            claims[claim] = manifest.id


def _collision_errors(
    builtins: tuple[ExtensionManifest, ...],
    candidates: list[tuple[str, ExtensionManifest, str | None]],
) -> dict[str, str]:
    builtin_claims: dict[str, str] = {}
    for manifest in builtins:
        for claim in _manifest_claims(manifest):
            builtin_claims[claim] = manifest.id

    community_claims: dict[str, list[str]] = defaultdict(list)
    for owner, manifest, _asset_package in candidates:
        for claim in _manifest_claims(manifest):
            community_claims[claim].append(owner)

    conflicts: dict[str, list[str]] = defaultdict(list)
    for claim, owners in community_claims.items():
        if claim in builtin_claims:
            for owner in owners:
                conflicts[owner].append(
                    f"{claim} is reserved by built-in extension {builtin_claims[claim]!r}"
                )
        if len(owners) > 1:
            rendered_owners = ", ".join(repr(item) for item in sorted(owners))
            for owner in owners:
                conflicts[owner].append(f"{claim} is claimed by {rendered_owners}")

    return {
        owner: "extension contribution collision: " + "; ".join(sorted(messages))
        for owner, messages in conflicts.items()
    }


def _build_plugin_state() -> ExtensionPluginState:
    """Discover and validate a fresh extension registry state."""
    builtins = tuple(sorted(_builtin_manifests(), key=lambda item: item.id))
    for manifest in builtins:
        validate_manifest(manifest)
    _validate_builtin_collisions(builtins)

    candidates, load_errors = _load_community_manifests()
    collisions = _collision_errors(builtins, candidates)
    for owner, error in collisions.items():
        load_errors[owner] = error
        _logger.warning("could not register extension entry point %s (%s)", owner, error)

    accepted = [
        (manifest, asset_package)
        for owner, manifest, asset_package in candidates
        if owner not in collisions
    ]
    manifests = tuple(
        sorted((*builtins, *(item[0] for item in accepted)), key=lambda item: item.id)
    )
    asset_packages = {
        manifest.id: asset_package
        for manifest, asset_package in accepted
        if asset_package is not None
    }
    return ExtensionPluginState(
        manifests=manifests,
        load_errors=dict(sorted(load_errors.items())),
        asset_packages=dict(sorted(asset_packages.items())),
    )


def plugin_state() -> ExtensionPluginState:
    """Return the process-cached built-in and installed extension registry."""
    global _building_thread_id, _state
    current_thread_id = threading.get_ident()
    with _state_condition:
        while _state is None and _building_thread_id is not None:
            if _building_thread_id == current_thread_id:
                raise RuntimeError("extension registry cannot be read during discovery")
            _state_condition.wait()
        if _state is not None:
            return _state
        _building_thread_id = current_thread_id

    try:
        built = _build_plugin_state()
    except BaseException:
        with _state_condition:
            _building_thread_id = None
            _state_condition.notify_all()
        raise

    with _state_condition:
        _state = built
        _building_thread_id = None
        _state_condition.notify_all()
        return _state


def extension_manifests() -> tuple[ExtensionManifest, ...]:
    """Return all accepted extension manifests in stable ID order."""
    return plugin_state().manifests


def extension_manifest(extension_id: str) -> ExtensionManifest | None:
    """Return one accepted extension manifest by ID."""
    return plugin_state().get(extension_id)


def reset_plugin_state_for_tests() -> None:
    """Clear the process-cached registry state."""
    global _state
    with _state_condition:
        if _building_thread_id == threading.get_ident():
            raise RuntimeError("extension registry cannot be reset during discovery")
        while _building_thread_id is not None:
            _state_condition.wait()
        _state = None
