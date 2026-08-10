"""Desktop raw-JSON fallback behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from playwright.sync_api import Page, Route

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _production_theme_script() -> str:
    """Load the exact script Electron injects after a JSON document is ready."""
    result = subprocess.run(
        [
            "node",
            "-e",
            "process.stdout.write(require('./web/electron/src/json-document-theme.js')"
            ".FORCE_LIGHT_JSON_DOCUMENT_SCRIPT)",
        ],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_raw_401_json_fallback_uses_light_palette(page: Page) -> None:
    """A generated JSON document remains readable under dark OS appearance."""
    page.emulate_media(color_scheme="dark")

    def fulfill_json(route: Route) -> None:
        route.fulfill(
            status=401,
            content_type="application/json",
            body=json.dumps({"error_code": 401, "message": "unsupported credential type"}),
        )

    page.route("https://example.test/raw-json", fulfill_json)
    response = page.goto("https://example.test/raw-json")

    assert response is not None
    assert response.status == 401
    assert page.evaluate("() => document.contentType") == "application/json"
    assert page.evaluate("() => matchMedia('(prefers-color-scheme: dark)').matches") is True
    assert page.evaluate(_production_theme_script()) is True

    palette = page.evaluate(
        """
        () => ({
          rootBackground: getComputedStyle(document.documentElement).backgroundColor,
          rootColor: getComputedStyle(document.documentElement).color,
          rootScheme: document.documentElement.style.colorScheme,
          bodyBackground: getComputedStyle(document.body).backgroundColor,
          bodyColor: getComputedStyle(document.body).color,
        })
        """
    )
    assert palette == {
        "rootBackground": "rgb(255, 255, 255)",
        "rootColor": "rgb(17, 17, 17)",
        "rootScheme": "light only",
        "bodyBackground": "rgb(255, 255, 255)",
        "bodyColor": "rgb(17, 17, 17)",
    }
