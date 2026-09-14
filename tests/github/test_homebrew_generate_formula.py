"""Regression tests for Homebrew's Pillow resources and source-build mirrors.

`brew install omnigent-ai/tap/omnigent` fails on macOS with "error building
wheel for pillow": the generated formula ships pillow as an sdist resource, so
Homebrew builds it from source inside its sandbox, where the image-library
headers pillow's build needs (libjpeg, zlib, ...) are not available — the
formula template declares none of them as `depends_on`. Pillow publishes
pre-built macOS wheels for the brewed CPython, so the generator must emit a
wheel resource (the mechanism `generate_formula.py` already uses for grpcio,
pyyaml, etc. via PREFER_WHEEL) instead of the unbuildable sdist.

These tests run the real `generate()` path with the PyPI API and dependency
resolution mocked to a closure containing pillow, and assert on the rendered
formula: the pillow resource must reference wheel files, never the sdist.
They fail while the bug is live and pass once the generator stops emitting
pillow's sdist.

The rendered install method must also retain opt-in mirror setup so release
regeneration cannot revert source-build fixes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

_SCRIPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "homebrew"
_SCRIPT = _SCRIPT_DIR / "generate_formula.py"
_TEMPLATE = _SCRIPT_DIR / "omnigent.rb.template"

_spec = importlib.util.spec_from_file_location("generate_formula", _SCRIPT)
assert _spec and _spec.loader, f"Could not load spec for {_SCRIPT}"
gf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gf
_spec.loader.exec_module(gf)


PILLOW_VERSION = "12.3.0"

# Shape of pillow's PyPI JSON API `urls` list for a release: the sdist plus
# native macOS wheels for the brewed CPython (cp314) on both formula arches.
# (Pillow publishes many more files; this subset is what the generator's
# wheel-vs-sdist selection looks at.)
_PILLOW_FILES = [
    {
        "filename": f"pillow-{PILLOW_VERSION}.tar.gz",
        "packagetype": "sdist",
        "url": f"https://files.pythonhosted.org/packages/aa/bb/pillow-{PILLOW_VERSION}.tar.gz",
        "digests": {"sha256": "a" * 64},
    },
    {
        "filename": f"pillow-{PILLOW_VERSION}-cp314-cp314-macosx_11_0_arm64.whl",
        "packagetype": "bdist_wheel",
        "url": (
            "https://files.pythonhosted.org/packages/cc/dd/"
            f"pillow-{PILLOW_VERSION}-cp314-cp314-macosx_11_0_arm64.whl"
        ),
        "digests": {"sha256": "b" * 64},
    },
    {
        "filename": f"pillow-{PILLOW_VERSION}-cp314-cp314-macosx_10_15_x86_64.whl",
        "packagetype": "bdist_wheel",
        "url": (
            "https://files.pythonhosted.org/packages/ee/ff/"
            f"pillow-{PILLOW_VERSION}-cp314-cp314-macosx_10_15_x86_64.whl"
        ),
        "digests": {"sha256": "c" * 64},
    },
]

_OMNIGENT_FILES = [
    {
        "filename": "omnigent-0.10.0.tar.gz",
        "packagetype": "sdist",
        "url": "https://files.pythonhosted.org/packages/11/22/omnigent-0.10.0.tar.gz",
        "digests": {"sha256": "d" * 64},
    },
]


def _generate_formula_with_pillow(monkeypatch) -> str:
    """Run the real generate() with resolution + PyPI metadata mocked.

    The closure is just pillow (the package under test); every other knob is
    the generator's default release configuration.
    """

    def fake_resolve_closure(version, platforms, extras, python_version, index_url, uv, cooldown):
        return {"pillow": PILLOW_VERSION}

    def fake_pypi_release_files(name, version, api_base=gf.PYPI_JSON_API):
        if name == "omnigent":
            return _OMNIGENT_FILES
        if name == "pillow":
            assert version == PILLOW_VERSION
            return _PILLOW_FILES
        raise AssertionError(f"unexpected PyPI lookup: {name}=={version}")

    monkeypatch.setattr(gf, "resolve_closure", fake_resolve_closure)
    monkeypatch.setattr(gf, "pypi_release_files", fake_pypi_release_files)

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


def _pillow_stanza(formula: str) -> str:
    """The pillow `resource ... do ... end` block from the rendered formula."""
    marker = 'resource "pillow" do'
    assert marker in formula, "generated formula must contain a pillow resource"
    start = formula.index(marker)
    # The stanza's closing `end` is the first line after the marker consisting
    # of the stanza indent + `end`.
    indent = formula[:start].rsplit("\n", 1)[-1]
    end = formula.index(f"\n{indent}end", start)
    return formula[start : end + len(f"\n{indent}end")]


def test_pillow_resource_is_not_the_sdist(monkeypatch) -> None:
    """The formula must not build pillow from source.

    Emitting `pillow-<ver>.tar.gz` makes `brew install omnigent` compile
    pillow inside the Homebrew sandbox, which fails ("error building wheel
    for pillow"): the build needs libjpeg/zlib headers the formula does not
    depend on. Pillow ships macOS wheels for the brewed CPython, so the sdist
    must never be the pinned resource.
    """
    formula = _generate_formula_with_pillow(monkeypatch)
    stanza = _pillow_stanza(formula)
    assert f"pillow-{PILLOW_VERSION}.tar.gz" not in stanza, (
        "the generated formula pins pillow's sdist, so `brew install omnigent` "
        "builds pillow from source and fails with 'error building wheel for "
        "pillow' (no libjpeg/zlib in the formula's depends_on). The generator "
        "must select pillow's pre-built macOS wheels instead."
    )


def test_pillow_resource_pins_macos_wheels(monkeypatch) -> None:
    """The pillow resource must pin the pre-built macOS wheel(s).

    The positive half of the sdist test: with cp314 macOS wheels available on
    PyPI for every target arch, the generator must emit them (a single
    universal url or per-arch on_arm/on_intel blocks) as pillow's resource.
    """
    formula = _generate_formula_with_pillow(monkeypatch)
    stanza = _pillow_stanza(formula)
    wheel_urls = [f["url"] for f in _PILLOW_FILES if f["packagetype"] == "bdist_wheel"]
    assert any(url in stanza for url in wheel_urls), (
        "pillow's resource stanza references no macOS wheel; the formula must "
        "install pillow from a pre-built wheel so `brew install omnigent` does "
        "not compile it from source."
    )


def test_pick_macos_wheels_finds_pillow_cp314_wheels() -> None:
    """Sanity: the wheel selector can serve pillow for every formula arch.

    Guards the fix's mechanism — if wheel selection could not resolve pillow
    for an arch the formula targets, a wheel-preferring fix would silently
    fall back to the broken sdist path again.
    """
    arches = [gf._ARCH_BLOCKS[p][1] for p in gf.DEFAULT_PLATFORMS]
    python_tag = "cp" + gf.DEFAULT_PYTHON_VERSION.replace(".", "")
    wheels = gf.pick_macos_wheels(_PILLOW_FILES, python_tag, arches)
    assert wheels is not None, (
        f"pick_macos_wheels found no {python_tag} macOS wheel for pillow on "
        f"{arches}, but pillow publishes them — the selector must resolve one "
        "per target arch."
    )
    for arch in arches:
        assert wheels[arch][0].endswith(".whl")


def _install_stanza(formula: str) -> str:
    return dedent(formula.split("  def install\n", 1)[1].split("\n  end", 1)[0])


def test_generated_formula_forwards_pip_mirror_before_creating_virtualenv(monkeypatch) -> None:
    install = _install_stanza(_generate_formula_with_pillow(monkeypatch))
    setup = dedent(
        """\
        if Homebrew::EnvConfig.non_default_variable?(:HOMEBREW_PIP_INDEX_URL) &&
           (pip_index_url = Homebrew::EnvConfig.pip_index_url.presence)
          ENV["PIP_INDEX_URL"] = pip_index_url
        end
        """
    ).strip()

    assert setup in install
    assert install.index(setup) < install.index('venv = virtualenv_create(libexec, "python3.14")')


def test_generated_formula_configures_cargo_mirror_before_resources(monkeypatch) -> None:
    install = _install_stanza(_generate_formula_with_pillow(monkeypatch))
    setup = dedent(
        """\
        if (cargo_index = ENV.fetch("HOMEBREW_CARGO_INDEX_URL", nil).presence)
          (buildpath/".cargo").mkpath
          (buildpath/".cargo/config.toml").write <<~TOML
            [source.crates-io]
            replace-with = "mirror"

            [source.mirror]
            registry = "sparse+#{cargo_index}"
          TOML
          ENV["CARGO_HOME"] = buildpath/".cargo"
        end
        """
    ).strip()

    assert setup in install
    assert install.index(setup) < install.index("venv.pip_install sdists")


def test_build_host_mirror_urls_are_not_baked_into_generated_formula(monkeypatch) -> None:
    monkeypatch.delenv("HOMEBREW_PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("HOMEBREW_CARGO_INDEX_URL", raising=False)
    original = _generate_formula_with_pillow(monkeypatch)

    monkeypatch.setenv("HOMEBREW_PIP_INDEX_URL", "https://pypi.example.com/simple")
    monkeypatch.setenv("HOMEBREW_CARGO_INDEX_URL", "https://cargo.example.com/index/")

    assert _generate_formula_with_pillow(monkeypatch) == original
