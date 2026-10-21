"""Operator activation, dependency resolution, and isolated extension lifecycles."""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import logging
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.extensions.api import ExtensionManifest, ExtensionPermission, ExtensionPluginState
from omnigent.extensions.packs import (
    ResolvedAgentContribution,
    ResolvedPolicyContribution,
    pack_root,
    resolve_agent,
    resolve_policy,
)
from omnigent.extensions.runtime import (
    ExtensionContext,
    ExtensionHooks,
    SessionStatusNotification,
    _status_sink,
)

_logger = logging.getLogger(__name__)
HOOK_TIMEOUT_SECONDS = 5.0
STATUS_QUEUE_SIZE = 256


class ExtensionSettings(BaseModel):
    """One explicit grant from operator-owned server configuration."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = False
    version: str | None = None
    permissions: list[str] = []
    config: dict[str, JsonValue] = {}
    required: bool = False


class ExtensionManager:
    """Freeze active contributions at startup; never install or reload code in requests."""

    def __init__(
        self,
        state: ExtensionPluginState,
        configuration: object = None,
        *,
        package_roots: Mapping[str, Path] | None = None,
    ) -> None:
        self.discovered = state
        self.settings: dict[str, ExtensionSettings] = {}
        self.errors: dict[str, str] = {}
        self.active: dict[str, ExtensionManifest] = {}
        self.agents: dict[str, ResolvedAgentContribution] = {}
        self.policies: dict[str, ResolvedPolicyContribution] = {}
        self._hooks: dict[str, ExtensionHooks] = {}
        self._queues: dict[str, asyncio.Queue[SessionStatusNotification]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._required: set[str] = set()
        self._package_roots = package_roots or {}
        self._configure(configuration)
        self._prepare()

    @property
    def state(self) -> ExtensionPluginState:
        """Return only active manifests for catalogs and browser asset resolution."""
        return ExtensionPluginState(
            manifests=tuple(sorted(self.active.values(), key=lambda item: item.id)),
            load_errors=self.discovered.load_errors,
            asset_packages=self.discovered.asset_packages,
        )

    @property
    def disabled(self) -> tuple[str, ...]:
        return tuple(
            sorted(item.id for item in self.discovered.manifests if item.id not in self.active)
        )

    def _configure(self, configuration: object) -> None:
        if configuration is not None and not isinstance(configuration, dict):
            raise ValueError("extensions must be a mapping keyed by extension id")
        declarations = configuration or {}
        for extension_id, raw in declarations.items():
            if not isinstance(extension_id, str):
                raise ValueError("extension configuration keys must be strings")
            if isinstance(raw, dict) and raw.get("required") is True:
                self._required.add(extension_id)
            try:
                setting = ExtensionSettings.model_validate(raw)
                if setting.required and not setting.enabled:
                    raise ValueError("a required extension must be enabled")
                if setting.version is not None:
                    SpecifierSet(setting.version)
                self.settings[extension_id] = setting
            except (ValidationError, ValueError, InvalidSpecifier) as exc:
                # ValidationError text can contain operator configuration values.
                self.errors[extension_id] = f"invalid extension settings ({type(exc).__name__})"
        for manifest in self.discovered.manifests:
            if manifest.id in self.settings or manifest.id in self.errors:
                continue
            backend = bool(manifest.agents or manifest.policies or manifest.entrypoints.server)
            # Preserve installation-as-enablement for existing browser-only packages.
            if configuration is None and not backend:
                self.settings[manifest.id] = ExtensionSettings(
                    enabled=True, permissions=sorted(item.value for item in manifest.permissions)
                )

    def _prepare(self) -> None:
        installed = {item.id: item for item in self.discovered.manifests}
        selected: dict[str, ExtensionManifest] = {}
        for extension_id, setting in self.settings.items():
            if not setting.enabled:
                continue
            manifest = installed.get(extension_id)
            if manifest is None:
                self.errors[extension_id] = "enabled extension is not installed or was rejected"
                continue
            if setting.version is not None and not SpecifierSet(setting.version).contains(
                manifest.version, prereleases=True
            ):
                self.errors[extension_id] = (
                    f"installed version {manifest.version} does not satisfy {setting.version}"
                )
                continue
            if not {item.value for item in manifest.permissions}.issubset(setting.permissions):
                self.errors[extension_id] = "operator has not granted all requested permissions"
                continue
            selected[extension_id] = manifest

        visited: set[str] = set()
        visiting: list[str] = []

        def visit(extension_id: str) -> None:
            if extension_id in visited or extension_id in self.errors:
                return
            if extension_id in visiting:
                for member in visiting[visiting.index(extension_id) :]:
                    self.errors[member] = "extension dependency cycle"
                return
            visiting.append(extension_id)
            manifest = selected[extension_id]
            for dependency in sorted(manifest.dependencies, key=lambda item: item.id):
                target = selected.get(dependency.id)
                if target is None:
                    self.errors[extension_id] = f"dependency {dependency.id!r} is not enabled"
                    break
                if not SpecifierSet(dependency.version).contains(target.version, prereleases=True):
                    self.errors[extension_id] = (
                        f"dependency {dependency.id!r} has incompatible version {target.version}"
                    )
                    break
                visit(dependency.id)
                if dependency.id not in self.active:
                    self.errors.setdefault(
                        extension_id, f"dependency {dependency.id!r} is unavailable"
                    )
                    break
            visiting.pop()
            visited.add(extension_id)
            if extension_id not in self.errors:
                self._prepare_manifest(manifest)

        for extension_id in sorted(selected):
            visit(extension_id)

    def versions_for(self, manifest: ExtensionManifest) -> dict[str, str]:
        versions = {manifest.id: manifest.version}
        for dependency in manifest.dependencies:
            target = self.active[dependency.id]
            versions.update(self.versions_for(target))
        return versions

    def _prepare_manifest(self, manifest: ExtensionManifest) -> None:
        try:
            package = self.discovered.asset_package(manifest.id)
            server = manifest.entrypoints.server
            if server is not None:
                module = server.partition(":")[0]
                if package is None or not (module == package or module.startswith(f"{package}.")):
                    raise ValueError("server entrypoint must belong to the extension distribution")
            if manifest.agents or manifest.policies:
                root = pack_root(package, self._package_roots.get(manifest.id))
                policies = {
                    item.id: resolve_policy(manifest, item, root) for item in manifest.policies
                }
                allowed_policies = {
                    key: preset
                    for key, preset in self.policies.items()
                    if preset.extension_id in self.versions_for(manifest)
                }
                allowed_policies.update(policies)
                agents = {
                    item.id: resolve_agent(
                        manifest,
                        item,
                        root,
                        policies=allowed_policies,
                        versions=self.versions_for(manifest),
                    )
                    for item in manifest.agents
                }
                self.policies.update(policies)
                self.agents.update(agents)
            self.active[manifest.id] = manifest
        except Exception as exc:  # noqa: BLE001 - installed package/resource boundary.
            self.errors[manifest.id] = f"feature pack unavailable: {exc}"
            _logger.warning("extension %s could not activate", manifest.id, exc_info=True)

    def ensure_ready(self) -> None:
        unavailable = self._required.difference(self.active)
        if unavailable:
            raise RuntimeError(
                f"required extensions are unavailable: {', '.join(sorted(unavailable))}"
            )

    def _reject(self, extension_id: str, message: str) -> None:
        rejected: list[str] = []

        def remove(identifier: str, reason: str) -> None:
            self.errors[identifier] = reason
            self.active.pop(identifier, None)
            self._queues.pop(identifier, None)
            worker = self._workers.get(identifier)
            if worker is not None and worker is not asyncio.current_task():
                worker.cancel()
            for manifest in tuple(self.active.values()):
                if any(item.id == identifier for item in manifest.dependencies):
                    remove(manifest.id, f"dependency {identifier!r} is unavailable")
            rejected.append(identifier)
            _logger.warning("extension %s quarantined: %s", identifier, reason)

        remove(extension_id, message)
        self.agents = {
            key: item for key, item in self.agents.items() if item.extension_id not in rejected
        }
        self.policies = {
            key: item for key, item in self.policies.items() if item.extension_id not in rejected
        }
        if any(identifier in self._hooks for identifier in rejected):
            task = asyncio.create_task(self._shutdown_extensions(tuple(rejected)))
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)

    async def start(self) -> None:
        """Import server code only after operator grants and resource validation."""
        if self._loop is not None:
            raise RuntimeError("extension manager is already started")
        self.ensure_ready()
        self._loop = asyncio.get_running_loop()
        for extension_id, manifest in tuple(self.active.items()):
            if extension_id not in self.active or manifest.entrypoints.server is None:
                continue
            module, _, symbol = manifest.entrypoints.server.partition(":")
            try:
                activate = getattr(importlib.import_module(module), symbol)
                if not inspect.iscoroutinefunction(activate):
                    raise TypeError(
                        "server entrypoint must be an async activate(context) function"
                    )
                hooks = await asyncio.wait_for(
                    activate(
                        ExtensionContext(
                            extension_id,
                            manifest.version,
                            MappingProxyType(copy.deepcopy(self.settings[extension_id].config)),
                            logging.getLogger(f"omnigent.extension.{extension_id}"),
                        )
                    ),
                    timeout=HOOK_TIMEOUT_SECONDS,
                )
                if not isinstance(hooks, ExtensionHooks):
                    raise TypeError("server entrypoint must return ExtensionHooks")
                self._hooks[extension_id] = hooks
                for callback in (hooks.on_session_status, hooks.shutdown):
                    if callback is not None and not inspect.iscoroutinefunction(callback):
                        raise TypeError("extension lifecycle callbacks must be async functions")
                if hooks.on_session_status is not None:
                    if ExtensionPermission.EVENTS_SUBSCRIBE not in manifest.permissions:
                        raise PermissionError("session observers require events.subscribe")
                    self._queues[extension_id] = asyncio.Queue(maxsize=STATUS_QUEUE_SIZE)
                    self._workers[extension_id] = asyncio.create_task(
                        self._observe(extension_id, hooks), name=f"extension:{extension_id}"
                    )
            except Exception:
                _logger.exception("extension %s server entrypoint failed", extension_id)
                self._reject(extension_id, "server entrypoint failed; see server logs")
        self.ensure_ready()

    def publish(self, event: SessionStatusNotification) -> None:
        """Transfer status metadata from request/background contexts to bounded queues."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: SessionStatusNotification) -> None:
        for extension_id, queue in tuple(self._queues.items()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._reject(extension_id, "session observer queue overflowed")

    async def _observe(self, extension_id: str, hooks: ExtensionHooks) -> None:
        assert hooks.on_session_status is not None
        queue = self._queues[extension_id]
        while extension_id in self.active:
            event = await queue.get()
            try:
                await asyncio.wait_for(
                    hooks.on_session_status(event), timeout=HOOK_TIMEOUT_SECONDS
                )
            except Exception:
                _logger.exception("extension %s session observer failed", extension_id)
                self._reject(extension_id, "session observer failed; see server logs")
                return
            finally:
                queue.task_done()

    async def stop(self) -> None:
        """Stop deliveries and shut down activated runtimes in reverse dependency order."""
        self._loop = None
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            with suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        self._queues.clear()
        if self._cleanup_tasks:
            await asyncio.gather(*tuple(self._cleanup_tasks))
        await self._shutdown_extensions(tuple(reversed(self._hooks)))

    async def _shutdown_extensions(self, extension_ids: tuple[str, ...]) -> None:
        for extension_id in extension_ids:
            hooks = self._hooks.pop(extension_id, None)
            if hooks is None:
                continue
            if hooks.shutdown is not None and inspect.iscoroutinefunction(hooks.shutdown):
                try:
                    await asyncio.wait_for(hooks.shutdown(), timeout=HOOK_TIMEOUT_SECONDS)
                except Exception:
                    _logger.exception("extension %s shutdown failed", extension_id)


class ExtensionLifecycleMiddleware:
    """Bind notifications to the serving app across HTTP, WebSocket, and background work."""

    def __init__(self, app: ASGIApp, manager: ExtensionManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = _status_sink.set(self.manager.publish)
        try:
            await self.app(scope, receive, send)
        finally:
            _status_sink.reset(token)
