#!/usr/bin/env python3
"""Generate the `omnigent` Homebrew formula for a released PyPI version.

Splices the volatile parts of `Formula/omnigent.rb` — the stable `url`/`sha256`
and every dependency `resource` stanza — into the hand-tuned template
(`omnigent.rb.template`). The structural parts (desc, depends_on, install, test)
are owned by the template; this script owns the bits that change every release.

Resolution: `uv pip compile` computes the exact transitive closure of
`omnigent[<extras>]==<version>` for each target platform (macOS arm + intel by
default — the brew tap's `brew test-bot` matrix). The per-platform closures are
unioned; for each package we then fetch the sdist URL + sha256 from the PyPI JSON
API and emit a `resource` stanza. Every package in the closure must publish an
sdist: the formula builds each resource from source, so one dropped for lack of
an sdist ships a venv missing that dependency, which surfaces as an ImportError
(or a silently disabled feature) at runtime rather than a red build. A missing
sdist is therefore a hard error; `--allow-no-sdist NAME` waives it for a package
omnigent genuinely works without.

Excluded from `resource` generation (provided by the brewed Python environment,
NOT built as virtualenv resources — keep in sync with the template's
`depends_on ... => :no_linkage` and the brewed packages' transitive build deps
like cffi/pycparser, which need libffi that this formula doesn't depend on):
``omnigent`` (the stable url itself) and ``certifi, cryptography, pydantic,
pydantic-core, rpds-py, cffi, pycparser``.

Run by `.github/workflows/homebrew-tap-pr.yml` on `release: published`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Default brew build matrix: macOS Apple Silicon + Intel (the tap's
# `brew test-bot` runs on macos-15 / macos-15-intel / macos-26). The union of the
# two closures captures platform-marker deps needed on either arch. Add
# `x86_64-unknown-linux-gnu` here if the tap re-enables Linux builds.
DEFAULT_PLATFORMS = ["aarch64-apple-darwin", "x86_64-apple-darwin"]
# Extras bundled as resources. The base install already pulls the Claude and
# OpenAI Agents harnesses; this adds the opt-in `cursor` harness (pure-Python
# sdist). antigravity is NOT bundled — no sdist (platform wheels only), no
# Intel-macOS build; `pip install omnigent[antigravity]` instead.
DEFAULT_EXTRAS = ["cursor"]
# Resolve for the brewed Python so `requires-python` markers match the formula's
# `python@3.14` (and the `virtualenv_create(libexec, "python3.14")` in install).
DEFAULT_PYTHON_VERSION = "3.14"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
PYPI_JSON_API = "https://pypi.org/pypi"

# Packages provided by the brewed Python environment (system site-packages),
# not built as virtualenv resources. `cffi`/`pycparser` are listed because cffi
# builds against libffi (not a dep of this formula) — they come from the brewed
# `cryptography`/`cffi` formulae instead. See module docstring.
BREWED_EXCLUSIONS = {
    "certifi",
    "cryptography",
    "pydantic",
    "pydantic-core",
    "rpds-py",
    "cffi",
    "pycparser",
}
# omnigent is the stable `url` itself, so it's never a resource.
SELF_EXCLUSIONS = {"omnigent"}

# Packages pinned to an upstream platform wheel instead of the sdist, emitted as
# an arch-conditional `resource` (the template's install block pip-installs any
# `.whl` resource from its cached download).
#
# google-re2 (required by cel-python, which backs CEL policy evaluation) has an
# sdist that cannot be built here: its setup.py shells out to `bazel` whenever
# GITHUB_ACTIONS is set — always true under `brew test-bot` — and the non-bazel
# path needs re2 + abseil + pybind11 headers and C++17, which it never requests.
# The upstream macOS wheels statically link re2 and abseil, so they need no build
# toolchain and no brewed `abseil` (whose ABI breaks on most releases, which
# would force a formula `revision` bump every time it moved).
WHEEL_REQUIRED = {"google-re2"}

# Compiled extensions we PREFER to take as an upstream wheel, falling back to the
# sdist when no compatible wheel exists (e.g. right after a python@X.Y bump,
# before upstream publishes cpXY wheels). Building these is the bulk of the
# formula's cost -- grpcio alone dwarfs everything else on a 3-core bottle
# builder -- and every wheel here has enough Mach-O header padding for Homebrew
# to rewrite its install name during keg relocation.
#
# jiter, tiktoken and watchfiles are deliberately NOT here: their wheels are
# maturin-built with no install-name padding, so relocation dies with "Failed
# changing dylib ID" (omnigent issue #866). They are built from source with
# -headerpad_max_install_names instead, which is how every bottled release up to
# 0.6.0 shipped them. Verify with:
#   install_name_tool -id <long Cellar path> <extracted .so>
PREFER_WHEEL = {
    "argon2-cffi-bindings",
    "grpcio",
    "httptools",
    "markupsafe",
    "protobuf",
    "pyyaml",
    "regex",
    "uvloop",
    "zstandard",
}

# Packages pinned to the PURE-PYTHON (`py3-none-any`) wheel on purpose.
#
# pendulum is the awkward case: its maturin wheel cannot be relocated (see
# above), and its sdist does not link against python 3.14 -- pyo3 leaves
# _Py_NoneStruct/_Py_Dealloc/_Py_TrueStruct undefined and the arm64 link fails.
# Its pure-Python wheel ships no extension module at all, so there is nothing to
# relocate and nothing to build. Only cel-python pulls it in, for CEL timestamp
# arithmetic, so the slower implementation is not on any hot path.
PURE_WHEEL = {"pendulum"}

# uv target platform -> (Homebrew arch block, wheel platform-tag arch suffix).
_ARCH_BLOCKS = {
    "aarch64-apple-darwin": ("on_arm", "arm64"),
    "x86_64-apple-darwin": ("on_intel", "x86_64"),
}

# name-version[-build]-pytag-abitag-platformtag.whl (PEP 427).
_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>[^-]+?)(?:-(?P<build>\d[^-]*))?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)

_PLACEHOLDERS = (
    "__OMNIGENT_URL__",
    "__OMNIGENT_SHA256__",
    "__RESOURCES__",
)


def normalize_name(name: str) -> str:
    """PEP 503 normalized project name (lowercase, runs of [-_.] -> -)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _http_get_json(url: str, retries: int = 5, timeout: int = 30) -> dict:
    """GET a JSON document with simple retry/backoff."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            last_err = e
            # 404 is a hard "not on PyPI" — don't retry into a 5-minute wait.
            if e.code == 404:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
        time.sleep(2**attempt)
    raise RuntimeError(f"fetch failed for {url}: {last_err}")


def pypi_release_files(name: str, version: str, api_base: str = PYPI_JSON_API) -> list[dict]:
    """Return the `urls` list for a (name, version) release from the PyPI JSON API.

    `api_base` defaults to the public PyPI JSON API; point it at a mirror's
    `/pypi` (via `--pypi-api` / `--proxy`) to fetch sdist URLs + sha256 through
    a proxy. Download URLs fetched from a mirror are then host-rewritten to
    `files.pythonhosted.org` (see `rewrite_url`) so the formula pins public URLs.
    """
    data = _http_get_json(f"{api_base}/{normalize_name(name)}/{version}/json")
    return data.get("urls", [])


def pick_sdist(files: list[dict]) -> tuple[str, str] | None:
    """Pick the sdist (url, sha256). Prefer .tar.gz; take the only sdist if one."""
    sdists = [f for f in files if f.get("packagetype") == "sdist"]
    if not sdists:
        return None
    for f in sdists:
        if f["url"].endswith(".tar.gz"):
            return f["url"], f["digests"]["sha256"]
    f = sdists[0]
    return f["url"], f["digests"]["sha256"]


def _abi_compatible(py: str, abi: str, python_tag: str) -> bool:
    """Is a wheel's (pytag, abitag) usable by CPython `python_tag` (e.g. cp314)?

    Accepts the exact CPython tag, a stable-ABI (`abi3`) wheel built for that
    version or older, and pure-Python `py3-none`. Free-threaded builds (`cp314t`)
    are excluded: the brewed python is not free-threaded, and equality on the abi
    tag keeps them out.
    """
    if abi == python_tag:
        return True
    if abi == "abi3" and py.startswith("cp") and py[2:].isdigit():
        return int(py[2:]) <= int(python_tag[2:])
    return py == "py3" and abi == "none"


def _wheel_arches(plat: str) -> tuple[frozenset[str], tuple[int, int]] | None:
    """Arches a macOS wheel platform tag covers, plus its deployment target."""
    if plat == "any":
        return frozenset({"arm64", "x86_64"}), (0, 0)
    m = re.match(r"macosx_(\d+)_(\d+)_(arm64|x86_64|universal2|intel)$", plat)
    if not m:
        return None
    arches = {
        "arm64": {"arm64"},
        "x86_64": {"x86_64"},
        "intel": {"x86_64"},
        "universal2": {"arm64", "x86_64"},
    }[m.group(3)]
    return frozenset(arches), (int(m.group(1)), int(m.group(2)))


def pick_macos_wheels(
    files: list[dict], python_tag: str, arches: list[str]
) -> dict[str, tuple[str, str]] | None:
    """Best macOS wheel per arch: {arch: (url, sha256)}, or None if any is missing.

    Ranked by (native before pure-Python, then lowest deployment target). A wheel
    built for an older `macosx_<major>_<minor>` minimum installs on every newer
    macOS the tap builds for while the reverse is not true. Pure-Python
    `py3-none-any` wheels sort last on purpose: when a package ships both (e.g.
    protobuf, pendulum) the `any` wheel is the slow fallback implementation, and
    it would otherwise always win by having no deployment target at all.

    A `universal2` (or `any`) wheel satisfies both arches with one file, which the
    caller renders as a single unconditional url.
    """
    best: dict[str, tuple[tuple[int, int, int], str, str]] = {}
    for f in files:
        if f.get("packagetype") != "bdist_wheel":
            continue
        m = _WHEEL_RE.match(f["filename"])
        if not m or not _abi_compatible(m.group("py"), m.group("abi"), python_tag):
            continue
        covered = _wheel_arches(m.group("plat"))
        if not covered:
            continue
        covered_arches, target = covered
        pure = 1 if m.group("abi") == "none" else 0
        rank = (pure, *target)
        for arch in arches:
            if arch in covered_arches and (arch not in best or rank < best[arch][0]):
                best[arch] = (rank, f["url"], f["digests"]["sha256"])
    if any(arch not in best for arch in arches):
        return None
    return {arch: (url, sha) for arch, (_, url, sha) in best.items()}


def rewrite_url(url: str, rewrites: list[tuple[str, str]]) -> str:
    """Apply `from -> to` substitutions to a download URL, in order.

    Used to turn an internal PyPI proxy's download URLs back into public
    `files.pythonhosted.org` URLs so the formula pins installable public URLs
    even when resolution + metadata fetch went through the proxy (the proxy
    mirrors PyPI's `/packages/<2>/<2>/<hash>/file` path verbatim, only the host
    differs; the sha256 is the file's content hash, so it's valid for the public
    URL too).
    """
    for old, new in rewrites:
        url = url.replace(old, new)
    return url


def resource_stanza(name: str, url: str, sha256: str, indent: int = 2) -> str:
    """A `resource "<name>" do … end` stanza, class-body indented."""
    pad = " " * indent
    return f'{pad}resource "{name}" do\n{pad}  url "{url}"\n{pad}  sha256 "{sha256}"\n{pad}end'


def wheel_resource_stanza(name: str, per_arch: list[tuple[str, str, str]], indent: int = 2) -> str:
    """An arch-conditional `resource` stanza: one `on_arm`/`on_intel` block each.

    `per_arch` is [(brew_block, url, sha256), ...]. `Resource` includes
    `OnSystem::MacOSAndLinux`, so these blocks are valid inside a resource.
    """
    pad = " " * indent
    lines = [f'{pad}resource "{name}" do']
    for block, url, sha256 in per_arch:
        lines += [
            f"{pad}  {block} do",
            f'{pad}    url "{url}"',
            f'{pad}    sha256 "{sha256}"',
            f"{pad}  end",
        ]
    lines.append(f"{pad}end")
    return "\n".join(lines)


def resolve_closure(
    version: str,
    platforms: list[str],
    extras: list[str],
    python_version: str,
    index_url: str,
    uv: str,
) -> dict[str, str]:
    """Union of `uv pip compile` resolutions per platform -> {name: version}.

    Runs `uv pip compile` with `--no-config` (ignore the repo's uv.toml cooldown,
    which would block the just-released version) against the public index. If a
    package resolves to different versions across platforms, the highest PEP 440
    version wins and a warning is printed (rare for sdists).
    """
    extras_spec = f"[{','.join(extras)}]" if extras else ""
    requirement = f"omnigent{extras_spec}=={version}"
    closure: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "req.in").write_text(requirement + "\n")
        for plat in platforms:
            out = tmp / f"req.{plat.replace('-', '_')}.out"
            cmd = [
                uv,
                "pip",
                "compile",
                "--no-config",
                "--no-header",
                "--no-annotate",
                "--python-version",
                python_version,
                "--python-platform",
                plat,
                "--default-index",
                index_url,
                str(tmp / "req.in"),
                "-o",
                str(out),
            ]
            # Surface uv's output on failure instead of swallowing it — a
            # resolution failure (version conflict, a dep with no Python 3.14
            # distribution, a requires-python cap, or no network to PyPI) is
            # otherwise undebuggable. Raise a RuntimeError (one clean line) rather
            # than letting CalledProcessError dump the full subprocess traceback.
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "(no output)").strip()
                raise RuntimeError(
                    f"`uv pip compile` failed for {plat} (python {python_version}); "
                    f"requirement: {requirement}\n{detail}"
                )
            for line in out.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "==" not in line:
                    continue
                name, ver = line.split("==", 1)
                # uv strips extras and markers by default, but defend against
                # `name[extra]==ver` (take the bare name before '[') and against
                # a trailing ` ; marker` on the version.
                name = name.split("[", 1)[0].strip()
                name = normalize_name(name)
                ver = ver.split(";", 1)[0].strip()
                if name in closure and closure[name] != ver:
                    kept = max(closure[name], ver, key=_pep440_key)
                    print(
                        f"::warning::{name} resolved to {closure[name]} on one "
                        f"platform and {ver} on {plat}; keeping {kept}.",
                        file=sys.stderr,
                    )
                    ver = kept
                closure[name] = ver
    return closure


def _pep440_key(version: str):
    """A best-effort PEP 440 sort key for picking the max of two versions."""
    nums = re.findall(r"\d+", version)
    return tuple(int(n) for n in nums)


def render_template(template: str, url: str, sha256: str, resources: str) -> str:
    # Catch a drifted template up front: every placeholder must be present before
    # we substitute, and none must remain after (the latter is belt-and-suspenders
    # since str.replace removes all occurrences, but it guards against a future
    # placeholder that contains regex-special chars or partial overlaps).
    missing = [p for p in _PLACEHOLDERS if p not in template]
    if missing:
        raise RuntimeError(f"template missing placeholder(s): {missing}")
    out = template
    out = out.replace("__OMNIGENT_URL__", url)
    out = out.replace("__OMNIGENT_SHA256__", sha256)
    out = out.replace("__RESOURCES__", resources)
    leftover = [p for p in _PLACEHOLDERS if p in out]
    if leftover:
        raise RuntimeError(f"template placeholders left unsubstituted: {leftover}")
    return out


def generate(
    version: str,
    template_path: Path,
    platforms: list[str],
    extras: list[str],
    python_version: str,
    index_url: str,
    uv: str,
    exclude: set[str],
    allow_no_sdist: set[str] | None = None,
    api_base: str = PYPI_JSON_API,
    url_rewrites: list[tuple[str, str]] | None = None,
) -> str:
    template = template_path.read_text()

    # Defensive: accept a leading `v` even though the workflow strips it.
    if version.startswith("v"):
        version = version[1:]

    extras_spec = f"[{','.join(extras)}]" if extras else ""
    print(
        f"Resolving omnigent{extras_spec}=={version} for {', '.join(platforms)} "
        f"(python {python_version})…",
        file=sys.stderr,
    )
    closure = resolve_closure(version, platforms, extras, python_version, index_url, uv)
    print(f"Resolved {len(closure)} packages.", file=sys.stderr)

    rewrites = url_rewrites or []
    if rewrites:
        print(f"URL rewrites: {rewrites}", file=sys.stderr)

    # Stable sdist for omnigent itself.
    omnigent_files = pypi_release_files("omnigent", version, api_base)
    sdist = pick_sdist(omnigent_files)
    if not sdist:
        raise RuntimeError(
            f"omnigent=={version} has no sdist on PyPI — cannot set the stable url."
        )
    stable_url, stable_sha = sdist
    stable_url = rewrite_url(stable_url, rewrites)
    print(f"omnigent {version}: {stable_url}", file=sys.stderr)

    # Every resolved package (other than omnigent itself and the brewed set) ->
    # a sdist resource stanza. `exclude` is the caller-supplied set (CLI --exclude);
    # it augments the built-in brewed set and the always-excluded self package.
    excluded = BREWED_EXCLUSIONS | exclude | SELF_EXCLUSIONS
    waived = allow_no_sdist or set()
    python_tag = "cp" + python_version.replace(".", "")
    resources: list[tuple[str, str]] = []
    missing_sdist: list[str] = []
    for name, ver in sorted(closure.items()):
        if name in excluded:
            continue
        files = pypi_release_files(name, ver, api_base)

        # Wheel-pinned packages. One `universal2`/`abi3` wheel usually covers both
        # arches, so emit a plain url and only fall back to on_arm/on_intel blocks
        # when upstream ships separate per-arch wheels.
        # Deliberate pure-Python wheel: no extension module, nothing to relocate.
        if name in PURE_WHEEL:
            pure = next((f for f in files if f["filename"].endswith("-py3-none-any.whl")), None)
            if not pure:
                raise RuntimeError(
                    f"{name}=={ver} publishes no py3-none-any wheel, but it is in "
                    f"PURE_WHEEL because neither its platform wheel nor its sdist "
                    f"is usable here. Re-check the comment on PURE_WHEEL."
                )
            resources.append(
                (
                    name,
                    resource_stanza(
                        name, rewrite_url(pure["url"], rewrites), pure["digests"]["sha256"]
                    ),
                )
            )
            continue

        if name in WHEEL_REQUIRED or name in PREFER_WHEEL:
            wheels = pick_macos_wheels(files, python_tag, [_ARCH_BLOCKS[p][1] for p in platforms])
            if wheels is None:
                if name in WHEEL_REQUIRED:
                    raise RuntimeError(
                        f"{name}=={ver} has no macOS wheel for {python_tag} on every "
                        f"target arch. It is in WHEEL_REQUIRED because its sdist is "
                        f"unbuildable here, so upstream must publish one or the "
                        f"dependency has to go."
                    )
                # PREFER_WHEEL is best-effort: fall through and build the sdist.
                print(
                    f"::warning::{name}=={ver} has no macOS wheel for {python_tag} on "
                    f"every target arch — falling back to a source build (slow).",
                    file=sys.stderr,
                )
            elif len({url for url, _ in wheels.values()}) == 1:
                url, sha = next(iter(wheels.values()))
                resources.append((name, resource_stanza(name, rewrite_url(url, rewrites), sha)))
                continue
            else:
                per_arch = [
                    (
                        _ARCH_BLOCKS[p][0],
                        rewrite_url(wheels[_ARCH_BLOCKS[p][1]][0], rewrites),
                        wheels[_ARCH_BLOCKS[p][1]][1],
                    )
                    for p in platforms
                ]
                resources.append((name, wheel_resource_stanza(name, per_arch)))
                continue

        sdist = pick_sdist(files)
        if not sdist:
            # Wheel-only dependency: Homebrew can't build it as a resource.
            # Dropping it silently yields a formula that installs green and is
            # missing an import, so fail unless the caller waived it.
            if name in waived:
                print(
                    f"::warning::{name}=={ver} has no sdist on PyPI — waived, no resource.",
                    file=sys.stderr,
                )
                continue
            missing_sdist.append(f"{name}=={ver}")
            continue
        resources.append((name, resource_stanza(name, rewrite_url(sdist[0], rewrites), sdist[1])))

    if missing_sdist:
        raise RuntimeError(
            "no sdist on PyPI for: "
            + ", ".join(missing_sdist)
            + "\nHomebrew builds every resource from source, so these would be "
            "absent from the installed venv. Drop the dependency, move it to an "
            "extra that isn't bundled (see DEFAULT_EXTRAS), or pass "
            "--allow-no-sdist <name> if omnigent works without it."
        )

    # No trailing newline: the template's blank lines frame the resource block.
    resources_str = "\n".join(stanza for _, stanza in resources)

    return render_template(template, stable_url, stable_sha, resources_str)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--version", required=True, help="Released version (e.g. 0.3.0), no leading 'v'."
    )
    ap.add_argument(
        "--template",
        type=Path,
        default=Path(__file__).with_name("omnigent.rb.template"),
        help="Path to the formula template.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("Formula/omnigent.rb"),
        help="Where to write the rendered formula.",
    )
    ap.add_argument(
        "--python-platform",
        action="append",
        default=None,
        help="uv target platform (repeatable). Default: macOS arm + intel.",
    )
    ap.add_argument(
        "--extra",
        action="append",
        default=None,
        help="Extras to bundle (repeatable). Default: cursor.",
    )
    ap.add_argument(
        "--python-version",
        default=DEFAULT_PYTHON_VERSION,
        help=f"uv --python-version (default {DEFAULT_PYTHON_VERSION}).",
    )
    ap.add_argument(
        "--index-url",
        default=None,
        help="PyPI simple index URL for `uv pip compile` (default https://pypi.org/simple; "
        "--proxy presets this).",
    )
    ap.add_argument(
        "--pypi-api",
        default=None,
        help="PyPI JSON API base for sdist URL/sha256 fetch (default https://pypi.org/pypi; "
        "--proxy presets this).",
    )
    ap.add_argument(
        "--url-rewrite",
        nargs=2,
        action="append",
        default=None,
        metavar=("FROM", "TO"),
        help="Rewrite FROM->TO in download URLs (repeatable). For proxy mirrors: "
        "rewrites the mirror host back to files.pythonhosted.org.",
    )
    ap.add_argument(
        "--proxy",
        default=None,
        metavar="HOST",
        help="Convenience preset for an internal PyPI mirror host (e.g. "
        "pypi-proxy.cloud.databricks.com): sets --index-url to https://HOST/simple, "
        "--pypi-api to https://HOST/pypi, and rewrites HOST -> files.pythonhosted.org "
        "in download URLs. Explicit --index-url/--pypi-api/--url-rewrite override.",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=None,
        help="Package name to exclude from resources (repeatable; "
        "added to the built-in brewed set).",
    )
    ap.add_argument(
        "--allow-no-sdist",
        action="append",
        default=None,
        help="Package allowed to have no PyPI sdist (repeatable). Without this, a "
        "wheel-only dependency fails the run instead of vanishing from the formula.",
    )
    ap.add_argument("--uv", default="uv", help="uv binary path.")
    args = ap.parse_args(argv)

    # --proxy HOST presets the index, the JSON API, and a host rewrite so a
    # local run behind an internal mirror produces a formula with public
    # files.pythonhosted.org URLs (the mirror serves the same /packages/<..>/
    # path, only the host differs). Explicit flags override the preset.
    proxy = args.proxy
    index_url = args.index_url or (f"https://{proxy}/simple" if proxy else DEFAULT_INDEX_URL)
    api_base = args.pypi_api or (f"https://{proxy}/pypi" if proxy else PYPI_JSON_API)
    url_rewrites = [tuple(r) for r in (args.url_rewrite or [])]
    if proxy and (proxy, "files.pythonhosted.org") not in url_rewrites:
        url_rewrites.insert(0, (proxy, "files.pythonhosted.org"))

    formula = generate(
        version=args.version,
        template_path=args.template,
        platforms=args.python_platform or DEFAULT_PLATFORMS,
        extras=args.extra or DEFAULT_EXTRAS,
        python_version=args.python_version,
        index_url=index_url,
        uv=args.uv,
        exclude={normalize_name(n) for n in (args.exclude or [])},
        allow_no_sdist={normalize_name(n) for n in (args.allow_no_sdist or [])},
        api_base=api_base,
        url_rewrites=url_rewrites,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(formula)
    print(f"Wrote {args.out} ({len(formula)} bytes).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
