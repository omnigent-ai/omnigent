"""The formula generator must never silently emit pillow's unbuildable sdist.

`brew install omnigent-ai/tap/omnigent` broke when the generated formula pinned
pillow's sdist: Homebrew builds sdist resources from source inside a sandbox
that only exposes headers for `depends_on` libraries, the formula declares no
jpeg dependency, and pillow's source build dies with
"RequiredDependencyException: ... jpeg". Preferring pillow's wheel fixed the
common case, but a best-effort preference falls back to the sdist when no
compatible macOS wheel exists for the brewed CPython (e.g. right after a
python@X.Y bump, before upstream publishes cpXY wheels) — regenerating exactly
the formula that cannot build, with a green exit code, so the broken artifact
ships silently and the failure surfaces only at install time on users'
machines.

This test drives the real `generate()` path with PyPI metadata mocked to that
wheel-miss state and asserts the generator does not succeed while pinning
pillow's sdist: it must either fail generation loudly (the WHEEL_REQUIRED
treatment) or emit something other than the unbuildable sdist. It fails while
the silent-fallback bug is live and passes once the generator refuses to ship
a pillow sdist resource.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "homebrew"
_SCRIPT = _SCRIPT_DIR / "generate_formula.py"
_TEMPLATE = _SCRIPT_DIR / "omnigent.rb.template"

_spec = importlib.util.spec_from_file_location("generate_formula_pillow_wheel_miss", _SCRIPT)
assert _spec and _spec.loader, f"Could not load spec for {_SCRIPT}"
gf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gf
_spec.loader.exec_module(gf)


PILLOW_VERSION = "12.3.0"

# Shape of pillow's PyPI JSON API `urls` list in the wheel-miss window: the
# sdist is published, but no macOS wheel is compatible with the brewed CPython
# (cp314) — only wheels for a newer CPython and a non-macOS platform exist.
# This is the state every wheel-pinned package passes through right after a
# python@X.Y bump, before upstream publishes cpXY wheels.
_PILLOW_FILES_NO_COMPATIBLE_WHEEL = [
    {
        "filename": f"pillow-{PILLOW_VERSION}.tar.gz",
        "packagetype": "sdist",
        "url": f"https://files.pythonhosted.org/packages/aa/bb/pillow-{PILLOW_VERSION}.tar.gz",
        "digests": {"sha256": "a" * 64},
    },
    {
        "filename": f"pillow-{PILLOW_VERSION}-cp315-cp315-macosx_11_0_arm64.whl",
        "packagetype": "bdist_wheel",
        "url": (
            "https://files.pythonhosted.org/packages/cc/dd/"
            f"pillow-{PILLOW_VERSION}-cp315-cp315-macosx_11_0_arm64.whl"
        ),
        "digests": {"sha256": "b" * 64},
    },
    {
        "filename": f"pillow-{PILLOW_VERSION}-cp315-cp315-macosx_10_15_x86_64.whl",
        "packagetype": "bdist_wheel",
        "url": (
            "https://files.pythonhosted.org/packages/ee/ff/"
            f"pillow-{PILLOW_VERSION}-cp315-cp315-macosx_10_15_x86_64.whl"
        ),
        "digests": {"sha256": "c" * 64},
    },
    {
        "filename": f"pillow-{PILLOW_VERSION}-cp314-cp314-manylinux_2_28_x86_64.whl",
        "packagetype": "bdist_wheel",
        "url": (
            "https://files.pythonhosted.org/packages/11/22/"
            f"pillow-{PILLOW_VERSION}-cp314-cp314-manylinux_2_28_x86_64.whl"
        ),
        "digests": {"sha256": "d" * 64},
    },
]

_OMNIGENT_FILES = [
    {
        "filename": "omnigent-0.10.0.tar.gz",
        "packagetype": "sdist",
        "url": "https://files.pythonhosted.org/packages/11/22/omnigent-0.10.0.tar.gz",
        "digests": {"sha256": "e" * 64},
    },
]


def _generate_formula_without_pillow_wheels(monkeypatch) -> str | None:
    """Run the real generate() with pillow in the wheel-miss state.

    The closure is just pillow; resolution and the PyPI JSON API are mocked
    (the network is not the subject under test), everything else is the
    generator's default release configuration. Returns the rendered formula,
    or None when generation failed loudly (a legitimate outcome — the
    generator refusing to ship an unbuildable formula).
    """

    def fake_resolve_closure(version, platforms, extras, python_version, index_url, uv, cooldown):
        return {"pillow": PILLOW_VERSION}

    def fake_pypi_release_files(name, version, api_base=gf.PYPI_JSON_API):
        if name == "omnigent":
            return _OMNIGENT_FILES
        if name == "pillow":
            assert version == PILLOW_VERSION
            return _PILLOW_FILES_NO_COMPATIBLE_WHEEL
        raise AssertionError(f"unexpected PyPI lookup: {name}=={version}")

    monkeypatch.setattr(gf, "resolve_closure", fake_resolve_closure)
    monkeypatch.setattr(gf, "pypi_release_files", fake_pypi_release_files)

    try:
        return gf.generate(
            version="0.10.0",
            template_path=_TEMPLATE,
            platforms=list(gf.DEFAULT_PLATFORMS),
            extras=list(gf.DEFAULT_EXTRAS),
            python_version=gf.DEFAULT_PYTHON_VERSION,
            index_url=gf.DEFAULT_INDEX_URL,
            uv="uv",
            exclude=set(),
            cooldown=7,
        )
    except RuntimeError:
        return None


def test_pillow_wheel_miss_does_not_silently_pin_the_sdist(monkeypatch) -> None:
    """A pillow wheel miss must not succeed by pinning the unbuildable sdist.

    With no cp314-compatible macOS wheel on PyPI, a best-effort fallback
    prints `::warning:: ... falling back to a source build (slow)` and emits
    `pillow-<ver>.tar.gz` as the resource with a green exit. That sdist can
    never build in the Homebrew sandbox (the formula declares no jpeg
    dependency), so the "slow" fallback is actually a formula that fails
    every `brew install`. The generator must fail generation loudly instead
    — pillow's sdist is unbuildable there, the WHEEL_REQUIRED treatment —
    so the breakage is caught in release CI, not on users' machines.
    """
    formula = _generate_formula_without_pillow_wheels(monkeypatch)
    if formula is None:
        # generate() raised: the loud, at-generation-time failure. Correct.
        return
    assert f"pillow-{PILLOW_VERSION}.tar.gz" not in formula, (
        "with no compatible macOS wheel for pillow, the generator succeeded "
        "and pinned pillow's sdist into the formula. That formula cannot "
        "build (`brew install omnigent` dies with 'RequiredDependencyException: "
        "... jpeg' — the formula declares no jpeg depends_on), so the silent "
        "fallback ships a broken install to every Homebrew user. The generator "
        "must fail loudly at generation time instead of emitting pillow's "
        "sdist as a resource."
    )


def test_pillow_is_wheel_required() -> None:
    """pillow must get the wheel-required, not best-effort, treatment.

    Its sdist can never build in the Homebrew sandbox (no jpeg `depends_on`),
    so a wheel miss must abort generation rather than fall back to the sdist.
    Pins the mechanism so pillow is not quietly moved back to the best-effort
    set, which would re-open the silent-fallback window.
    """
    assert "pillow" in gf.WHEEL_REQUIRED
    assert "pillow" not in gf.PREFER_WHEEL


def test_pillow_wheel_miss_fails_generation_loudly(monkeypatch) -> None:
    """The wheel-miss failure must be a diagnosable error naming pillow.

    The generic half is covered above (no silent sdist); this pins the loud
    path: generate() raises, and the message names the package so the release
    engineer looking at a red CI job knows exactly which dependency to chase.
    """

    def fake_resolve_closure(version, platforms, extras, python_version, index_url, uv, cooldown):
        return {"pillow": PILLOW_VERSION}

    def fake_pypi_release_files(name, version, api_base=gf.PYPI_JSON_API):
        if name == "omnigent":
            return _OMNIGENT_FILES
        return _PILLOW_FILES_NO_COMPATIBLE_WHEEL

    monkeypatch.setattr(gf, "resolve_closure", fake_resolve_closure)
    monkeypatch.setattr(gf, "pypi_release_files", fake_pypi_release_files)

    with pytest.raises(RuntimeError, match="pillow"):
        gf.generate(
            version="0.10.0",
            template_path=_TEMPLATE,
            platforms=list(gf.DEFAULT_PLATFORMS),
            extras=list(gf.DEFAULT_EXTRAS),
            python_version=gf.DEFAULT_PYTHON_VERSION,
            index_url=gf.DEFAULT_INDEX_URL,
            uv="uv",
            exclude=set(),
            cooldown=7,
        )
