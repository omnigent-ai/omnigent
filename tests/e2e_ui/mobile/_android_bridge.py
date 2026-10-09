"""Run the Android shell's injected bridge script in plain Chromium.

The shell (``web/android``) injects ``NativeBridgeScript.kt`` into every page and
pushes the OS safe area into CSS variables from ``MainActivity.emitInsets()``.
These helpers reproduce both steps so browser tests exercise the real script.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from playwright.sync_api import Page

_REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_SOURCE = (
    _REPO_ROOT / "web/android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt"
)
# Kotlin templates inside the raw string: the bridge transport object name and
# the escaped literal dollar sign.
_KOTLIN_SUBSTITUTIONS = {
    "${OmnigentBridgeListener.JS_OBJECT_NAME}": "omnigentNativeBridge",
    "${'$'}": "$",
}


def android_bridge_script() -> str:
    """Return the JavaScript the Android shell injects, taken from the Kotlin source.

    :returns: The bridge script with Kotlin template placeholders substituted.
    """
    source = BRIDGE_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'val source: String =\s*"""\n(.*?)"""\.trimIndent\(\)', source, re.S)
    assert match, f"bridge raw string not found in {BRIDGE_SOURCE}"
    script = textwrap.dedent(match.group(1))
    for placeholder, value in _KOTLIN_SUBSTITUTIONS.items():
        script = script.replace(placeholder, value)
    assert "${" not in script, "unsubstituted Kotlin template in the bridge script"
    return script


def emit_android_insets(page: Page, top: int, bottom: int) -> None:
    """Write the OS insets into the page the way ``MainActivity.emitInsets()`` does.

    The shell sets the CSS variables inline rather than calling
    ``__omnigentNativeEmitInsets``, which carries the iOS bar footprints.

    :param page: Page running under the injected Android bridge.
    :param top: Status-bar inset in CSS px.
    :param bottom: Navigation-bar inset in CSS px.
    """
    page.evaluate(
        """([top, bottom]) => {
          const s = document.documentElement.style;
          s.setProperty('--omnigent-safe-top', top + 'px');
          s.setProperty('--omnigent-safe-bottom', bottom + 'px');
          s.setProperty('--omnigent-android-safe-area-top', top + 'px');
          s.setProperty('--omnigent-android-safe-area-bottom', bottom + 'px');
          s.setProperty('--omnigent-android-safe-area-left', '0px');
          s.setProperty('--omnigent-android-safe-area-right', '0px');
        }""",
        [top, bottom],
    )
