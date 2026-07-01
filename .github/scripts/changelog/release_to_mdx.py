#!/usr/bin/env python3
"""Turn a curated GitHub Release body into an MDX-safe per-version site page.

The website's `/releases/<version>` post is the *concise, curated highlights* —
it mirrors the GitHub Release notes a maintainer already hand-edits in the
draft→edit→publish flow. This module does a small mechanical transform so that
GitHub-flavoured Markdown renders cleanly through the site's MDX pipeline
(`@next/mdx`):

  * unwrap `<https://…>` autolinks (angle brackets are JSX in MDX),
  * escape `{`, `}`, and any remaining `<` so MDX never tries to evaluate them,
  * linkify bare `#1234` references to the PR,
  * prepend a `# vX.Y.Z` heading + a `_Released <date>_` line the index reads.

No LLM, no reflow — the curation is the human's; we only make it MDX-safe.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_AUTOLINK_RE = re.compile(r"<((?:https?://)[^>\s]+)>")
# A bare "#1234" not already part of a word, path, or link. Headings are
# "# Title" (space after #), so they never match.
_PR_REF_RE = re.compile(r"(?<![\w/#])#(\d+)\b")


def mdx_escape(text: str) -> str:
    """Make GitHub-flavoured Markdown safe to parse as MDX."""
    text = _AUTOLINK_RE.sub(r"\1", text)  # <url> -> url (GFM still autolinks bare URLs)
    text = text.replace("{", "&#123;").replace("}", "&#125;")
    # neutralise stray tags; '>' stays (blockquotes)
    return text.replace("<", "&lt;")


def linkify_pr_refs(text: str, repo: str) -> str:
    return _PR_REF_RE.sub(
        lambda m: f"[#{m.group(1)}](https://github.com/{repo}/pull/{m.group(1)})",
        text,
    )


def release_body_to_mdx(tag: str, date: str, body: str, repo: str) -> str:
    """Render the MDX page for one release."""
    transformed = linkify_pr_refs(mdx_escape(body or ""), repo)
    comment = (
        "{/* Auto-generated from the GitHub Release for "
        + tag
        + ". Edit the GitHub Release, not this file. */}"
    )
    header = f"{comment}\n\n# {tag}\n\n_Released {date}_\n\n"
    return header + transformed.strip() + "\n"


def _tag_date(tag: str) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%cs", tag],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="final release tag, e.g. v0.3.0")
    parser.add_argument("--repo", required=True, help="owner/name for PR links")
    parser.add_argument("--date", default=None, help="release date YYYY-MM-DD (default: tag date)")
    parser.add_argument(
        "--body-file", default=None, help="file with the release body (default: stdin)"
    )
    parser.add_argument("--out", required=True, help="output page.mdx path")
    args = parser.parse_args()

    body = Path(args.body_file).read_text() if args.body_file else sys.stdin.read()
    date = args.date or _tag_date(args.tag)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(release_body_to_mdx(args.tag, date, body, args.repo))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
