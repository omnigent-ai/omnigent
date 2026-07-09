"""The per-harness self-declaration the bench probes against.

A :class:`BenchProfile` carries every fact the bench cannot infer: the
test model, the CLI binary to skip-gate on, the transport class, the
static matrix columns (owner, auth, implementation), and the *declared*
verdict for each dimension (the spreadsheet cell, as data).

The bench never hard-codes a harness anywhere else — probes and drivers
are harness-agnostic. Adding an official harness means adding a profile
to :mod:`tests.harness_bench.manifest`; a community / out-of-repo harness
ships its own profile and is selected by name via :func:`resolve_profile`.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from tests.harness_bench.verdict import Verdict


@dataclass(frozen=True)
class BenchProfile:
    """Self-declared bench metadata for one harness.

    :param harness: Harness name as registered in
        ``omnigent.runtime.harnesses._HARNESS_MODULES`` (or resolvable by
        :func:`resolve_profile` for a community harness), e.g.
        ``"claude-sdk"``.
    :param model: A real model id the harness can route this run, threaded
        into the spawn env as ``{env_prefix}MODEL``. The bench cannot
        invent a valid id, so the profile must supply one.
    :param env_prefix: The env-var prefix the harness wrap reads, e.g.
        ``"HARNESS_CLAUDE_SDK_"``. ``{env_prefix}MODEL`` /
        ``{env_prefix}GATEWAY`` / ``{env_prefix}DATABRICKS_PROFILE`` are
        derived from it.
    :param marker: A unique literal the LLM is asked to echo in the basic
        turn / streaming probes. Per-harness so concurrent runs never
        cross-match.
    :param cli_binary: The CLI binary the inner executor requires on PATH,
        e.g. ``"codex"``. ``None`` for pure-Python harnesses. Used to skip
        the live layer when the binary is absent.
    :param transport: Transport class name selecting a driver in
        :mod:`tests.harness_bench.driver`, e.g. ``"sdk-inproc"``. A
        harness on an unknown transport degrades its transport-dependent
        probes to ``SKIPPED``.
    :param owner: Static matrix column — who owns this harness.
    :param auth: Static matrix column — the auth mechanism, e.g.
        ``"Databricks gateway"``.
    :param implementation: Static matrix column, e.g.
        ``"SDK in-process"`` or ``"CLI subprocess (app-server RPC)"``.
    :param declared: The declared verdict per probe name — the spreadsheet
        row as data. Probe names absent from this map are treated as
        ``UNKNOWN`` (no claim), so they never raise drift.
    """

    harness: str
    model: str
    env_prefix: str
    marker: str
    cli_binary: str | None = None
    transport: str = "sdk-inproc"
    owner: str = ""
    auth: str = ""
    implementation: str = ""
    declared: Mapping[str, Verdict] = field(default_factory=dict)

    def declared_for(self, probe_name: str) -> Verdict:
        """Return the declared verdict for *probe_name* (``UNKNOWN`` if unclaimed)."""
        return self.declared.get(probe_name, Verdict.UNKNOWN)


def resolve_profile(name: str) -> BenchProfile:
    """Resolve a harness name to a :class:`BenchProfile`.

    Resolution chain:

    1. An official harness in :mod:`tests.harness_bench.manifest`.
    2. A community harness that ships a profile: *name* is a dotted path
       to either a ``BenchProfile`` instance or a zero-arg
       ``bench_profile()`` factory (e.g.
       ``mypkg.myharness:bench_profile`` or ``mypkg.myharness.PROFILE``).
    3. Any harness registered in the omnigent registry (in-repo or an
       entry-point plugin), resolved by name / alias — the profile is
       derived from the capability model (see
       :func:`tests.harness_bench.manifest._registry_profile`). This is what
       lets ``--harness acp`` or ``--harness rovo`` run with no bench edit.

    This keeps the official list a convenience index, not a gate: any
    registered harness is probeable by name, and any out-of-tree one that
    ships a ``BenchProfile`` is probeable by reference.

    :param name: Harness name / alias, or a dotted path / ``module:attr``.
    :returns: The resolved profile.
    :raises KeyError: If *name* is not a registered harness nor an importable
        profile reference.
    """
    from tests.harness_bench.manifest import OFFICIAL_PROFILES, _registry_profile

    if name in OFFICIAL_PROFILES:
        return OFFICIAL_PROFILES[name]

    resolved = _load_profile_reference(name)
    if resolved is not None:
        return resolved

    from_registry = _registry_profile(name)
    if from_registry is not None:
        return from_registry

    raise KeyError(
        f"unknown harness {name!r}: not a registered omnigent harness, not an "
        f"official profile ({', '.join(sorted(OFFICIAL_PROFILES))}), and not an "
        f"importable BenchProfile reference (try 'module:attr' or 'module.ATTR')"
    )


def _load_profile_reference(name: str) -> BenchProfile | None:
    """Try to import *name* as a ``BenchProfile`` instance or factory.

    Accepts ``module:attr`` and ``module.attr`` spellings. Returns
    ``None`` when *name* does not look like / does not resolve to a
    profile reference, so the caller can raise a single clear error.
    """
    if ":" in name:
        module_path, _, attr = name.partition(":")
    elif "." in name:
        module_path, _, attr = name.rpartition(".")
    else:
        return None

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return None

    target = getattr(module, attr, None)
    if target is None:
        return None
    if isinstance(target, BenchProfile):
        return target
    if callable(target):
        candidate = target()
        if isinstance(candidate, BenchProfile):
            return candidate
    return None
