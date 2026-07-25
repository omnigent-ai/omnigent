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
API and emit a `resource` stanza. Packages with no sdist (e.g. `cel-expr-python`,
which is Bazel-built and has no PyPI sdist) are skipped — omnigent degrades
gracefully without them, matching the hand-tuned formula.

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
    resources: list[tuple[str, str, str]] = []
    for name, ver in sorted(closure.items()):
        if name in excluded:
            continue
        files = pypi_release_files(name, ver, api_base)
        sdist = pick_sdist(files)
        if not sdist:
            # No sdist (e.g. cel-expr-python, Bazel-built) — skip. omnigent
            # degrades gracefully without it, matching the hand-tuned formula.
            print(
                f"::warning::{name}=={ver} has no sdist on PyPI — skipping (no resource).",
                file=sys.stderr,
            )
            continue
        resources.append((name, rewrite_url(sdist[0], rewrites), sdist[1]))

    # No trailing newline: the template's blank lines frame the resource block.
    resources_str = "\n".join(resource_stanza(n, u, s) for n, u, s in resources)

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
        api_base=api_base,
        url_rewrites=url_rewrites,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(formula)
    print(f"Wrote {args.out} ({len(formula)} bytes).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
