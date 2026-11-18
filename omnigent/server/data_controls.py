"""Deployment data-handling controls for regulated deployments.

Three settings, read once at startup from the ``data_controls:`` block of the
server config, each overridable by an environment variable:

- ``include_outputs`` (``OMNIGENT_EXPORT_INCLUDE_OUTPUTS``): ``request`` (the
  default) lets an export request choose whether tool outputs are included;
  ``never`` strips them from every export whatever the request asks for.
- ``allow_downloads`` (``OMNIGENT_ALLOW_DOWNLOADS``): ``false`` refuses the
  raw file download stream with 403 and tells the web UI to hide its download
  buttons. Previews served through the capped viewer read keep working.
- ``content_security_policy`` (``OMNIGENT_CONTENT_SECURITY_POLICY``): ``off``
  (the default), ``report-only`` or ``enforce`` a strict policy on the web UI's
  HTML responses.

An invalid value fails startup: a typo must not silently leave a control off.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

DATA_CONTROLS_KEY = "data_controls"
INCLUDE_OUTPUTS_ENV = "OMNIGENT_EXPORT_INCLUDE_OUTPUTS"
ALLOW_DOWNLOADS_ENV = "OMNIGENT_ALLOW_DOWNLOADS"
CONTENT_SECURITY_POLICY_ENV = "OMNIGENT_CONTENT_SECURITY_POLICY"

DOWNLOADS_DISABLED_MESSAGE = "Downloads are turned off on this server."
#: Replaces a tool's output in an export that leaves outputs out.
OUTPUT_OMITTED = "[output omitted by this server's export settings]"

# The web HTML preview's same-page anchor script (``HTML_PREVIEW_HEAD`` in
# web/src/shell/codeViewerHelpers.ts). A preview frame inherits this policy;
# web/src/shell/htmlPreviewPolicy.test.ts fails if the script changes.
_HTML_PREVIEW_SCRIPT_HASHES = ("'sha256-5ec8LrBNWWIyrl0Dagr+uATS+zwjwo1v5TmEajRD1BA='",)
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})
# Keys of a provider-native tool item that say what ran, not what it returned.
_NATIVE_TOOL_IDENTITY_KEYS = ("type", "id", "status", "name")
#: Item types that carry no tool output; exports keep them unchanged.
OUTPUT_FREE_ITEM_TYPES = frozenset(
    {"message", "function_call", "error", "reasoning", "resource_event", "routing_decision"}
)
#: Item types :func:`omit_item_outputs` rewrites.
OUTPUT_ITEM_TYPES = frozenset(
    {"function_call_output", "terminal_command", "native_tool", "compaction", "slash_command"}
)
# What an item of a type neither set names keeps when outputs are left out.
_ENVELOPE_KEYS = ("id", "type", "status", "response_id", "created_at", "created_by")


class IncludeOutputs(str, Enum):
    """Whether exports may carry tool outputs."""

    REQUEST = "request"
    NEVER = "never"


class ContentSecurityPolicyMode(str, Enum):
    """Whether the web UI's HTML responses carry a Content-Security-Policy."""

    OFF = "off"
    REPORT_ONLY = "report-only"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class DataControls:
    """The deployment's data-handling controls; the defaults change nothing."""

    include_outputs: IncludeOutputs = IncludeOutputs.REQUEST
    allow_downloads: bool = True
    content_security_policy: ContentSecurityPolicyMode = ContentSecurityPolicyMode.OFF

    def export_includes_outputs(self, requested: bool) -> bool:
        """Whether an export that asked for outputs (*requested*) gets them."""
        return requested and self.include_outputs is not IncludeOutputs.NEVER

    def info(self) -> dict[str, Any]:
        """The public block ``GET /v1/info`` serves, so clients can adapt."""
        return {
            "include_outputs": self.include_outputs.value,
            "allow_downloads": self.allow_downloads,
            "content_security_policy": self.content_security_policy.value,
        }


def _setting(
    section: Mapping[str, Any], key: str, env_name: str, environ: Mapping[str, str]
) -> tuple[object, str]:
    """Return a setting's raw value and where it came from; the env var wins."""
    env_value = environ.get(env_name, "").strip()
    if env_value:
        return env_value, env_name
    return section.get(key), f"{DATA_CONTROLS_KEY}.{key}"


def _parse_bool(raw: object, source: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in _TRUE_STRINGS | _FALSE_STRINGS:
        return raw.strip().lower() in _TRUE_STRINGS
    raise ValueError(f"{source} must be true or false, got {raw!r}")


def _parse_csp_mode(raw: object, source: str) -> ContentSecurityPolicyMode:
    # YAML 1.1 reads a bare ``off`` as false, so booleans, and the same words
    # from an environment variable, are accepted too.
    if raw is False:
        return ContentSecurityPolicyMode.OFF
    if raw is True:
        return ContentSecurityPolicyMode.ENFORCE
    value = raw.strip().lower().replace("_", "-") if isinstance(raw, str) else None
    if value in _TRUE_STRINGS:
        return ContentSecurityPolicyMode.ENFORCE
    if value in _FALSE_STRINGS:
        return ContentSecurityPolicyMode.OFF
    for mode in ContentSecurityPolicyMode:
        if value == mode.value:
            return mode
    raise ValueError(f"{source} must be off, report-only or enforce, got {raw!r}")


def _parse_include_outputs(raw: object, source: str) -> IncludeOutputs:
    value = raw.strip().lower() if isinstance(raw, str) else None
    for choice in IncludeOutputs:
        if value == choice.value:
            return choice
    raise ValueError(f"{source} must be request or never, got {raw!r}")


def load_data_controls(
    config: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> DataControls:
    """Read the data-handling controls from the server config and environment.

    :param config: The loaded server config; its optional ``data_controls``
        mapping holds the settings, e.g. ``{"include_outputs": "never"}``.
    :param environ: Environment to read overrides from; ``None`` reads
        :data:`os.environ`.
    :returns: The controls, all at their defaults when nothing is set.
    :raises ValueError: If the block is not a mapping or a value is invalid.
    """
    env = os.environ if environ is None else environ
    section = config.get(DATA_CONTROLS_KEY)
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ValueError(f"{DATA_CONTROLS_KEY} must be a mapping, got {section!r}")

    include_outputs = IncludeOutputs.REQUEST
    raw, source = _setting(section, "include_outputs", INCLUDE_OUTPUTS_ENV, env)
    if raw is not None:
        include_outputs = _parse_include_outputs(raw, source)

    allow_downloads = True
    raw, source = _setting(section, "allow_downloads", ALLOW_DOWNLOADS_ENV, env)
    if raw is not None:
        allow_downloads = _parse_bool(raw, source)

    csp = ContentSecurityPolicyMode.OFF
    raw, source = _setting(section, "content_security_policy", CONTENT_SECURITY_POLICY_ENV, env)
    if raw is not None:
        csp = _parse_csp_mode(raw, source)

    return DataControls(
        include_outputs=include_outputs,
        allow_downloads=allow_downloads,
        content_security_policy=csp,
    )


def app_data_controls(state: Any) -> DataControls:
    """Return the controls an app was built with; an app without them has the defaults."""
    controls = getattr(state, "data_controls", None)
    return controls if isinstance(controls, DataControls) else DataControls()


def require_downloads_allowed(state: Any) -> None:
    """Refuse a raw file download when the deployment turns downloads off.

    :param state: The app's ``request.app.state``.
    :raises OmnigentError: 403 when downloads are off.
    """
    if not app_data_controls(state).allow_downloads:
        raise OmnigentError(DOWNLOADS_DISABLED_MESSAGE, code=ErrorCode.FORBIDDEN)


def omit_item_outputs(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of an API item with what its tool returned taken out.

    The item keeps its type, ids and the call it answers, so an export still
    reads in order and ``omnigent session import`` accepts it; only the output
    text (or a provider-native tool's result) is replaced. A compaction keeps
    its summary, like any assistant text, but loses the verbatim transcript
    copy that holds the tool results it summarized. An item type this module
    doesn't know keeps only its envelope, so a new type fails closed.

    :param item: One flat item from ``ConversationItem.to_api_dict``.
    :returns: The item, unchanged unless it carries a tool output.
    """
    item_type = item.get("type")
    if item_type in OUTPUT_FREE_ITEM_TYPES:
        return dict(item)
    if item_type not in OUTPUT_ITEM_TYPES:
        return {key: item[key] for key in _ENVELOPE_KEYS if key in item}
    stripped = dict(item)
    if item_type == "function_call_output":
        stripped["output"] = OUTPUT_OMITTED
    elif item_type == "terminal_command" and item.get("kind") == "output":
        stripped["stdout"] = OUTPUT_OMITTED
        stripped["stderr"] = None
    elif item_type == "native_tool" and isinstance(item.get("item"), Mapping):
        native = item["item"]
        stripped["item"] = {
            key: native[key] for key in _NATIVE_TOOL_IDENTITY_KEYS if key in native
        }
    elif item_type == "compaction":
        stripped["compacted_messages"] = None
    elif item_type == "slash_command" and item.get("output") is not None:
        stripped["output"] = OUTPUT_OMITTED
    return stripped


# ``(?<![\w-])`` keeps ``data-src=`` from counting as a source attribute.
_INLINE_SCRIPT = re.compile(r"<script\b(?![^>]*(?<![\w-])src\s*=)[^>]*>(.*?)</script>", re.DOTALL)


def inline_script_hashes(html: str) -> list[str]:
    """Return a CSP ``'sha256-…'`` source for each inline script in *html*."""
    return [
        "'sha256-{}'".format(
            base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        )
        for body in _INLINE_SCRIPT.findall(html)
    ]


def content_security_policy(script_hashes: Iterable[str] = ()) -> str:
    """Build the web UI's policy.

    Scripts load only from this origin, plus the hashed inline scripts the
    server writes into the HTML shell and the HTML preview's fixed link helper.
    ``'wasm-unsafe-eval'`` lets the syntax highlighter compile its WebAssembly;
    it does not allow JavaScript ``eval``. Styles allow inline because the
    diagram renderer and the editor inject ``<style>`` elements. Images, media
    and fonts come from this origin or local ``blob:``/``data:`` URLs, so
    remote images do not load.

    :param script_hashes: ``'sha256-…'`` sources from :func:`inline_script_hashes`.
    :returns: The header value.
    """
    script_src = " ".join(
        ["'self'", "'wasm-unsafe-eval'", *script_hashes, *_HTML_PREVIEW_SCRIPT_HASHES]
    )
    directives = [
        "default-src 'self'",
        f"script-src {script_src}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self' data:",
        "connect-src 'self' blob: data:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
    ]
    return "; ".join(directives)


def content_security_policy_header(mode: ContentSecurityPolicyMode) -> str | None:
    """Return the header that carries the policy in *mode*, or ``None`` when off."""
    if mode is ContentSecurityPolicyMode.ENFORCE:
        return "Content-Security-Policy"
    if mode is ContentSecurityPolicyMode.REPORT_ONLY:
        return "Content-Security-Policy-Report-Only"
    return None
