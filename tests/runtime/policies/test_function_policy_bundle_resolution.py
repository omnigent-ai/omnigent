"""
Bundle-local resolution of pack-shipped function-policy modules.

An agent pack registered from a directory carries its policy module
INSIDE the bundle, while the author writes ``function.path`` relative
to their own repo root (e.g. ``agents.mypack.policies.custom_policy``).
Resolving that path with a bare ``importlib`` import only works when
the evaluating process happens to have the pack's repo root on
``sys.path`` — a server launched by a service manager (cwd=$HOME)
crashes resolution and fail-closed denies every message with the
generic ``Denied by policy (policy evaluation error).`` reason.

These tests pin the bundle fallback: when the environment import
fails and the policy spec carries a ``bundle_root``, the module is
located inside the bundle by dotted-path suffix and loaded under a
per-bundle anchor so same-named modules from different bundles never
collide.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omnigent.policies.function import (
    resolve_function_policy,
    resolve_function_target,
)
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
    PolicyAction,
)

_POLICY_MODULE = textwrap.dedent(
    '''\
    """Pack-local policy module, shipped inside the bundle."""


    def my_factory(keyword: str = "forbidden", reason: str = "blocked"):
        """Factory returning an input policy that denies on *keyword*."""

        def policy(event):
            if event.get("type") != "request":
                return {"result": "ALLOW"}
            if keyword in str(event.get("data") or "").lower():
                return {"result": "DENY", "reason": reason}
            return {"result": "ALLOW"}

        return policy
    '''
)


def _build_bundle(root: Path) -> Path:
    """Create a minimal extracted pack bundle with a policies module.

    Mirrors what ``omnigent server --agent <pack-dir>`` materializes:
    the bundle root holds the pack's own files only (``policies/``),
    NOT the author's repo-root package chain the dotted path names.

    :param root: Directory to populate as the bundle root.
    :returns: *root*, populated.
    """
    policies_dir = root / "policies"
    policies_dir.mkdir(parents=True)
    (policies_dir / "__init__.py").write_text("")
    (policies_dir / "custom_policy.py").write_text(_POLICY_MODULE)
    return root


def _pack_spec(
    bundle_root: Path | None,
    arguments: dict[str, str] | None = None,
) -> FunctionPolicySpec:
    """Build the spec shape a parsed pack produces for its guardrail."""
    return FunctionPolicySpec(
        name="pack_guard",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(
            # Repo-root-relative, exactly as pack authors write it.
            path="agents.mypack.policies.custom_policy.my_factory",
            # Factory form: {} calls the factory with its defaults.
            arguments=arguments if arguments is not None else {},
        ),
        bundle_root=str(bundle_root) if bundle_root is not None else None,
    )


@pytest.mark.asyncio
async def test_pack_local_path_resolves_from_bundle_and_policy_runs(
    tmp_path: Path,
) -> None:
    """A repo-root-relative dotted path resolves from the bundle.

    The environment cannot import ``agents.mypack`` (the package
    chain exists only in the author's repo), so resolution must fall
    back to the module file shipped in the bundle — and the built
    policy must actually evaluate with the configured factory
    arguments.
    """
    bundle = _build_bundle(tmp_path / "bundle")
    policy = resolve_function_policy(
        _pack_spec(bundle, arguments={"keyword": "forbidden", "reason": "pack says no"})
    )

    denied = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="this is forbidden text"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert denied.action == PolicyAction.DENY
    assert denied.reason == "pack says no"

    allowed = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="hello there"),
        {"labels": {}, "conversation_id": "c"},
    )
    assert allowed.action == PolicyAction.ALLOW


def test_pack_local_path_without_bundle_root_still_fails_loud(
    tmp_path: Path,
) -> None:
    """No ``bundle_root`` → the environment import error propagates.

    Untrusted specs (uploaded bundles, DB policy rows) are never
    stamped, so they must keep today's fail-loud behavior instead of
    probing an arbitrary filesystem location.
    """
    _build_bundle(tmp_path / "bundle")  # exists, but the spec doesn't point at it
    with pytest.raises(ModuleNotFoundError):
        resolve_function_policy(_pack_spec(None))


@pytest.mark.asyncio
async def test_same_named_modules_in_two_bundles_do_not_collide(
    tmp_path: Path,
) -> None:
    """Two bundles shipping ``policies/custom_policy.py`` stay isolated.

    Both modules land in ``sys.modules`` under distinct per-bundle
    anchors; each agent's policy must evaluate with ITS OWN module,
    not whichever bundle imported first.
    """
    bundle_a = _build_bundle(tmp_path / "bundle_a")
    bundle_b = _build_bundle(tmp_path / "bundle_b")
    # Give bundle B a module whose default reason differs, so a
    # cross-bundle cache hit is observable.
    (bundle_b / "policies" / "custom_policy.py").write_text(
        _POLICY_MODULE.replace('reason: str = "blocked"', 'reason: str = "b-side"')
    )

    # Both factories run with their module DEFAULTS, so each verdict's
    # reason reveals which bundle's module actually executed.
    policy_a = resolve_function_policy(_pack_spec(bundle_a))
    policy_b = resolve_function_policy(_pack_spec(bundle_b))

    ctx = EvaluationContext(phase=Phase.REQUEST, content="forbidden")
    state = {"labels": {}, "conversation_id": "c"}
    assert (await policy_a.evaluate(ctx, state)).reason == "blocked"
    assert (await policy_b.evaluate(ctx, state)).reason == "b-side"


def test_bundle_miss_error_names_the_searched_bundle(tmp_path: Path) -> None:
    """Module in neither the environment nor the bundle → the error
    names both failures so an operator can tell a typo from a
    missing file."""
    bundle = _build_bundle(tmp_path / "bundle")
    with pytest.raises(ModuleNotFoundError, match="also searched the agent bundle"):
        resolve_function_target(
            "agents.mypack.policies.nonexistent.my_factory",
            bundle_root=str(bundle),
        )


def test_non_identifier_segments_never_probe_the_bundle(tmp_path: Path) -> None:
    """Dotted-path segments double as filesystem components in the
    bundle search; anything that is not a plain identifier must be
    rejected rather than mapped onto the filesystem."""
    bundle = _build_bundle(tmp_path / "bundle")
    with pytest.raises((ModuleNotFoundError, ValueError)):
        resolve_function_target(
            "agents.pol-icies.custom_policy.my_factory",
            bundle_root=str(bundle),
        )


def test_bundle_relative_path_resolves_directly(tmp_path: Path) -> None:
    """A dotted path written relative to the bundle root itself
    (``policies.custom_policy``) resolves without any prefix
    stripping."""
    bundle = _build_bundle(tmp_path / "bundle")
    target = resolve_function_target(
        "policies.custom_policy.my_factory",
        bundle_root=str(bundle),
    )
    assert callable(target)
