"""Activation grants, dependency behavior, and extension failure isolation."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from omnigent.extensions import (
    ExtensionDependency,
    ExtensionEntrypoints,
    ExtensionHooks,
    ExtensionManifest,
    ExtensionPermission,
    ExtensionPluginState,
    SessionStatusNotification,
)
from omnigent.extensions.manager import ExtensionLifecycleMiddleware, ExtensionManager
from omnigent.extensions.runtime import notify_session_status


def _manifest(identifier: str = "acme.observer", **changes) -> ExtensionManifest:
    return replace(
        ExtensionManifest(
            id=identifier,
            display_name=identifier,
            owner="team-observability",
            distribution=f"omnigent-{identifier.replace('.', '-')}",
            version="1.0.0",
            requires_omnigent=">=0.19,<1",
            extension_api=1,
        ),
        **changes,
    )


def _grant(manifest: ExtensionManifest, **changes) -> dict:
    return {
        "enabled": True,
        "version": f"=={manifest.version}",
        "permissions": sorted(item.value for item in manifest.permissions),
        **changes,
    }


def _manager(*manifests: ExtensionManifest, configuration=None) -> ExtensionManager:
    return ExtensionManager(
        ExtensionPluginState(
            manifests=manifests, asset_packages={item.id: "test_extension" for item in manifests}
        ),
        configuration
        if configuration is not None
        else {item.id: _grant(item) for item in manifests},
    )


def _runtime(monkeypatch, activate, identifier="acme.observer") -> ExtensionManifest:
    module = ModuleType(f"test_extension.{identifier.replace('.', '_')}")
    module.activate = activate
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return _manifest(
        identifier,
        entrypoints=ExtensionEntrypoints(server=f"{module.__name__}:activate"),
        permissions=frozenset({ExtensionPermission.EVENTS_SUBSCRIBE}),
    )


def test_backend_requires_explicit_enablement(incident_state, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "omnigent_incident_response.hooks", raising=False)
    manager = ExtensionManager(incident_state)
    assert manager.state.manifests == ()
    assert manager.agents == {}
    assert manager.disabled == ("acme.incident-response",)
    assert "omnigent_incident_response.hooks" not in sys.modules


def test_existing_browser_only_enablement_is_preserved() -> None:
    state = ExtensionPluginState(manifests=(_manifest(),))
    assert ExtensionManager(state).state.manifests == state.manifests
    assert ExtensionManager(state, {}).state.manifests == ()


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ({"enabled": True, "version": "==2.0.0"}, "does not satisfy"),
        ({"enabled": "yes"}, "invalid extension settings"),
        ({"enabled": True, "unknown": "secret-value"}, "invalid extension settings"),
        ({"enabled": True, "version": "invalid"}, "invalid extension settings"),
    ],
)
def test_invalid_grants_are_isolated_and_redacted(setting, message) -> None:
    rejected = _manifest()
    healthy = _manifest("acme.healthy")
    manager = _manager(
        rejected,
        healthy,
        configuration={rejected.id: setting, healthy.id: _grant(healthy)},
    )
    assert tuple(manager.active) == (healthy.id,)
    assert message in manager.errors[rejected.id]
    assert "secret-value" not in str(manager.errors)


def test_unknown_enabled_and_required_extensions_are_reported() -> None:
    manager = ExtensionManager(
        ExtensionPluginState(manifests=()),
        {"acme.missing": {"enabled": True, "required": True}},
    )
    assert "not installed" in manager.errors["acme.missing"]
    with pytest.raises(RuntimeError, match=r"required extensions are unavailable: acme\.missing"):
        manager.ensure_ready()


def test_missing_permission_grant_disables_backend(incident_state, incident_manifest) -> None:
    manager = ExtensionManager(incident_state, {incident_manifest.id: {"enabled": True}})
    assert "not granted" in manager.errors[incident_manifest.id]
    assert not manager.agents


def test_invalid_config_container_fails_closed() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        ExtensionManager(ExtensionPluginState(manifests=()), ["acme.observer"])


def test_dependencies_are_prepared_in_deterministic_order() -> None:
    base = _manifest("zeta.base")
    dependent = _manifest("acme.product", dependencies=(ExtensionDependency(base.id, ">=1,<2"),))
    manager = _manager(dependent, base)
    assert tuple(manager.active) == (base.id, dependent.id)
    assert manager.versions_for(dependent) == {dependent.id: "1.0.0", base.id: "1.0.0"}


@pytest.mark.parametrize("version", [">=2", ">=1"])
def test_missing_or_incompatible_dependency_rejects_only_dependents(version) -> None:
    base = _manifest("zeta.base")
    dependent = _manifest("acme.product", dependencies=(ExtensionDependency(base.id, version),))
    unrelated = _manifest("acme.unrelated")
    settings = {item.id: _grant(item) for item in (dependent, unrelated)}
    if version == ">=2":
        settings[base.id] = _grant(base)
    manager = _manager(dependent, base, unrelated, configuration=settings)
    assert dependent.id not in manager.active
    assert unrelated.id in manager.active
    assert "dependency" in manager.errors[dependent.id]


def test_dependency_cycle_and_transitive_dependents_are_quarantined() -> None:
    first = _manifest("acme.first", dependencies=(ExtensionDependency("acme.second", ">=1"),))
    second = _manifest("acme.second", dependencies=(ExtensionDependency(first.id, ">=1"),))
    third = _manifest("acme.third", dependencies=(ExtensionDependency(first.id, ">=1"),))
    healthy = _manifest("acme.healthy")
    manager = _manager(first, second, third, healthy)
    assert set(manager.errors) == {first.id, second.id, third.id}
    assert set(manager.active) == {healthy.id}


def test_server_entrypoint_must_belong_to_verified_distribution() -> None:
    manifest = _manifest(entrypoints=ExtensionEntrypoints(server="other_package:activate"))
    manager = _manager(manifest)
    assert "must belong" in manager.errors[manifest.id]


async def test_lifecycle_receives_isolated_configuration_and_shuts_down(monkeypatch) -> None:
    notifications = []
    received = asyncio.Event()
    contexts = []
    stopped = []

    async def activate(context):
        contexts.append(context)

        async def observe(event):
            notifications.append(event)
            received.set()

        async def shutdown():
            stopped.append(context.extension_id)

        return ExtensionHooks(on_session_status=observe, shutdown=shutdown)

    manifest = _runtime(monkeypatch, activate)
    settings = {manifest.id: _grant(manifest, config={"labels": ["test"]})}
    manager = _manager(manifest, configuration=settings)
    assert not contexts
    await manager.start()
    try:
        manager.publish(SessionStatusNotification("session-1", "idle", "response-1"))
        await asyncio.wait_for(received.wait(), timeout=2)
        assert notifications == [SessionStatusNotification("session-1", "idle", "response-1")]
        assert contexts[0].extension_id == manifest.id
        with pytest.raises(TypeError):
            contexts[0].config["new-key"] = "value"
        contexts[0].config["labels"].append("local")
        assert settings[manifest.id]["config"] == {"labels": ["test"]}
    finally:
        await manager.stop()
    assert stopped == [manifest.id]
    assert not manager._workers


async def test_runtime_import_is_skipped_for_disabled_extension(monkeypatch) -> None:
    async def unexpected(_context):
        pytest.fail("disabled runtime was activated")

    manifest = _runtime(monkeypatch, unexpected)
    manager = _manager(manifest, configuration={manifest.id: {"enabled": False}})
    await manager.start()
    await manager.stop()
    assert not manager.active


@pytest.mark.parametrize("required", [False, True])
async def test_runtime_failure_isolated_or_blocks_required_startup(monkeypatch, required) -> None:
    async def activate(_context):
        raise RuntimeError("synthetic secret from an extension")

    failed = _runtime(monkeypatch, activate)
    dependent = _manifest("acme.product", dependencies=(ExtensionDependency(failed.id, ">=1"),))
    healthy = _manifest("acme.healthy")
    manager = _manager(
        failed,
        dependent,
        healthy,
        configuration={
            failed.id: _grant(failed, required=required),
            dependent.id: _grant(dependent),
            healthy.id: _grant(healthy),
        },
    )
    try:
        if required:
            with pytest.raises(RuntimeError, match="required extensions"):
                await manager.start()
        else:
            await manager.start()
        assert set(manager.active) == {healthy.id}
        assert "synthetic secret" not in str(manager.errors)
    finally:
        await manager.stop()


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_failed_observer_is_quarantined_without_affecting_other_extensions(
    monkeypatch, failure
) -> None:
    monkeypatch.setattr("omnigent.extensions.manager.HOOK_TIMEOUT_SECONDS", 0.05)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def activate(_context):
        async def observe(_event):
            entered.set()
            if failure == "error":
                raise RuntimeError("observer failure")
            await blocked.wait()

        return ExtensionHooks(on_session_status=observe)

    failed = _runtime(monkeypatch, activate)
    healthy = _manifest("acme.healthy")
    manager = _manager(failed, healthy)
    await manager.start()
    queue = manager._queues[failed.id]
    try:
        manager.publish(SessionStatusNotification("session-1", "running"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(queue.join(), timeout=2)
        assert failed.id not in manager.active
        assert healthy.id in manager.active
    finally:
        await manager.stop()


async def test_session_observer_requires_declared_permission(monkeypatch) -> None:
    async def observe(_event):
        pass

    async def activate(_context):
        return ExtensionHooks(on_session_status=observe)

    manifest = replace(_runtime(monkeypatch, activate), permissions=frozenset())
    manager = _manager(manifest)
    await manager.start()
    try:
        assert manifest.id not in manager.active
        assert not manager._workers
    finally:
        await manager.stop()


async def test_slow_observer_queue_is_bounded_and_overflow_quarantines_it(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.extensions.manager.STATUS_QUEUE_SIZE", 1)
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def activate(_context):
        async def observe(_event):
            entered.set()
            await blocked.wait()

        return ExtensionHooks(on_session_status=observe)

    manifest = _runtime(monkeypatch, activate)
    manager = _manager(manifest)
    await manager.start()
    try:
        event = SessionStatusNotification("session-1", "running")
        manager.publish(event)
        await asyncio.wait_for(entered.wait(), timeout=2)
        manager._enqueue(event)
        manager._enqueue(event)
        assert manifest.id not in manager.active
        assert "overflowed" in manager.errors[manifest.id]
    finally:
        await manager.stop()


async def test_shutdown_reverses_dependency_order_and_continues_after_failure(monkeypatch) -> None:
    calls = []

    async def activate(context):
        calls.append(f"start:{context.extension_id}")

        async def shutdown():
            calls.append(f"stop:{context.extension_id}")
            if context.extension_id == "acme.product":
                raise RuntimeError("cleanup failed")

        return ExtensionHooks(shutdown=shutdown)

    base = _runtime(monkeypatch, activate, "zeta.base")
    dependent = replace(
        _runtime(monkeypatch, activate, "acme.product"),
        dependencies=(ExtensionDependency(base.id, ">=1"),),
    )
    manager = _manager(dependent, base)
    await manager.start()
    await manager.stop()
    assert calls == [
        "start:zeta.base",
        "start:acme.product",
        "stop:acme.product",
        "stop:zeta.base",
    ]


async def test_quarantine_shuts_down_dependents_before_the_failed_runtime(monkeypatch) -> None:
    stopped = []
    shutdown_complete = asyncio.Event()

    async def activate(context):
        async def observe(_event):
            if context.extension_id == "zeta.base":
                raise RuntimeError("synthetic observer failure")

        async def shutdown():
            stopped.append(context.extension_id)
            if context.extension_id == "zeta.base":
                shutdown_complete.set()

        return ExtensionHooks(on_session_status=observe, shutdown=shutdown)

    base = _runtime(monkeypatch, activate, "zeta.base")
    dependent = replace(
        _runtime(monkeypatch, activate, "acme.product"),
        dependencies=(ExtensionDependency(base.id, ">=1"),),
    )
    manager = _manager(base, dependent)
    await manager.start()
    try:
        manager.publish(SessionStatusNotification("session-1", "idle"))
        await asyncio.wait_for(shutdown_complete.wait(), timeout=2)
        assert stopped == [dependent.id, base.id]
        assert not manager.active
    finally:
        await manager.stop()
    assert stopped == [dependent.id, base.id]


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_notification_context_is_app_scoped(monkeypatch, scope_type) -> None:
    delivered = {"acme.first": [], "acme.second": []}
    done = {identifier: asyncio.Event() for identifier in delivered}

    async def activate(context):
        async def observe(event):
            delivered[context.extension_id].append(event.session_id)
            done[context.extension_id].set()

        return ExtensionHooks(on_session_status=observe)

    manifests = [_runtime(monkeypatch, activate, identifier) for identifier in delivered]
    managers = [_manager(manifest) for manifest in manifests]
    for manager in managers:
        await manager.start()
    try:

        async def invoke(manager, identifier):
            async def app(_scope, _receive, _send):
                notify_session_status(identifier, "idle", None)

            async def receive():
                return {"type": "http.disconnect"}

            async def send(_message):
                pass

            await ExtensionLifecycleMiddleware(app, manager)({"type": scope_type}, receive, send)

        await asyncio.gather(
            *(
                invoke(manager, manifest.id)
                for manager, manifest in zip(managers, manifests, strict=True)
            )
        )
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in done.values())), timeout=2
        )
        assert delivered == {"acme.first": ["acme.first"], "acme.second": ["acme.second"]}
    finally:
        for manager in managers:
            await manager.stop()
