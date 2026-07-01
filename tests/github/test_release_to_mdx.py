from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "changelog" / "release_to_mdx.py"
)
spec = importlib.util.spec_from_file_location("release_to_mdx", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_mdx_escape_braces_and_angle_brackets() -> None:
    out = mod.mdx_escape("use {config} and <Component> safely")
    assert "{" not in out and "}" not in out
    assert "<" not in out  # escaped to &lt;
    assert "&#123;config&#125;" in out


def test_mdx_escape_unwraps_autolinks() -> None:
    out = mod.mdx_escape("see <https://example.com/x> now")
    assert "https://example.com/x" in out
    assert "<https" not in out and "&lt;https" not in out


def test_mdx_escape_preserves_blockquote_gt() -> None:
    # '>' is left alone so Markdown blockquotes still render.
    assert ">" in mod.mdx_escape("> a quote")


def test_linkify_pr_refs() -> None:
    out = mod.linkify_pr_refs("fixed in #1304 and #20", "omnigent-ai/omnigent")
    assert "[#1304](https://github.com/omnigent-ai/omnigent/pull/1304)" in out
    assert "[#20](https://github.com/omnigent-ai/omnigent/pull/20)" in out


def test_linkify_leaves_headings_alone() -> None:
    # Heading "# v0.3.0" has a space after '#', so it is not a PR ref.
    assert mod.linkify_pr_refs("# v0.3.0", "o/o") == "# v0.3.0"


def test_release_body_to_mdx_structure() -> None:
    body = "### Major new features\n\n* Seven harnesses (#1132, #330)\n"
    page = mod.release_body_to_mdx("v0.3.0", "2026-06-27", body, "omnigent-ai/omnigent")
    assert page.startswith("{/* Auto-generated")
    assert "# v0.3.0" in page
    assert "_Released 2026-06-27_" in page
    assert "### Major new features" in page
    assert "/pull/1132" in page  # PR refs linkified
