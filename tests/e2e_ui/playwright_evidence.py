"""Test-bound Playwright evidence for managed and directly-created contexts."""

from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import BrowserContext as AsyncBrowserContext
from playwright.sync_api import Browser, BrowserContext

VERIFY_RUN_DIR_ENV = "OMNIGENT_VERIFY_RUN_DIR"
_MAX_EVENTS = 2_000
_MAX_TEXT = 1_000
MAX_TRACE_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TRACE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TRACE_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_TRACE_MEMBERS = 10_000
MAX_TRACE_ARCHIVE_DEPTH = 4
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)(\s*[:=]\s*)\S+"),
    re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b"),
)
_LONG_BLOB = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{80,}(?![A-Za-z0-9+/=])")
_HIGH_ENTROPY_SEGMENT = re.compile(r"^[A-Za-z0-9._~+=-]{24,}$")
_LOCAL_PATH = re.compile(r"(?:(?:/Users|/home)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)[^\s]*")
_TRACE_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|token|password|secret)\b"
        r"\s*[:=]\s*)[A-Za-z0-9._~+/=-]+"
    ),
)
_TRACE_SECRET_LITERAL = re.compile(
    r"(?i)\b[A-Za-z0-9._~+/=-]*(?:secret|password|token)"
    r"[A-Za-z0-9._~+/=-]*\b"
)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def redact_url(raw_url: str) -> str:
    """Retain routing metadata without credentials, queries, fragments, or bodies."""
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return "<redacted-url>"
    if parsed.scheme in {"data", "blob"}:
        return f"{parsed.scheme}:[redacted]"
    if parsed.scheme == "file":
        return "file:///[redacted]"
    if not parsed.scheme:
        return raw_url.split("?", 1)[0].split("#", 1)[0][:_MAX_TEXT]
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{netloc}:{port}"
    segments = []
    for segment in parsed.path.split("/"):
        if _HIGH_ENTROPY_SEGMENT.fullmatch(segment) or any(
            pattern.search(segment) for pattern in _SECRET_PATTERNS
        ):
            digest = hashlib.sha256(segment.encode("utf-8")).hexdigest()[:12]
            segments.append(f"<redacted-{digest}>")
        else:
            segments.append(redact_text(segment))
    return urlunsplit((parsed.scheme, netloc, "/".join(segments)[:_MAX_TEXT], "", ""))


def redact_text(raw_text: object) -> str:
    """Bound and redact diagnostic text before it reaches an evidence file."""
    text = str(raw_text)
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(bearer"):
            text = pattern.sub(r"\1[redacted]", text)
        elif "api[_-]?key" in pattern.pattern:
            text = pattern.sub(r"\1\2[redacted]", text)
        else:
            text = pattern.sub("[redacted-secret]", text)
    text = _LONG_BLOB.sub("[redacted-blob]", text)
    text = _LOCAL_PATH.sub("<local-path>", text)
    text = re.sub(
        r"https?://[^\s\"')]+",
        lambda match: redact_url(match.group(0)),
        text,
    )
    if len(text) > _MAX_TEXT:
        return f"{text[:_MAX_TEXT]}…[truncated]"
    return text


def _known_secret_values() -> tuple[str, ...]:
    values = [
        os.environ[key]
        for key in ("OPENAI_API_KEY", "DATABRICKS_TOKEN", "CURSOR_API_KEY")
        if os.environ.get(key)
    ]
    config_path = os.environ.get("DATABRICKS_CONFIG_FILE")
    selected = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if config_path and selected:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(config_path, encoding="utf-8")
            values.extend(
                value
                for key, value in parser.items(selected)
                if any(word in key.lower() for word in ("token", "password", "secret")) and value
            )
        except (configparser.Error, OSError):
            pass
    return tuple(dict.fromkeys(values))


def _redact_json_secrets(value: Any, known: tuple[str, ...], *, sensitive: bool = False) -> Any:
    if sensitive:
        return "[redacted-sensitive-value]"
    if isinstance(value, dict):
        return {
            key: _redact_json_secrets(
                item,
                known,
                sensitive=any(
                    word in str(key).lower()
                    for word in ("token", "password", "secret", "api_key", "authorization")
                ),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_secrets(item, known) for item in value]
    if isinstance(value, str):
        for secret in known:
            value = value.replace(secret, "[redacted-known-secret]")
        for pattern in _TRACE_SECRET_PATTERNS:
            value = pattern.sub(r"\1[redacted]", value)
        return _TRACE_SECRET_LITERAL.sub("[redacted-secret-literal]", value)
    return value


def _redact_trace_text(text: str, known: tuple[str, ...]) -> str:
    lines = text.splitlines()
    if lines:
        try:
            parsed = [json.loads(line) for line in lines if line.strip()]
        except json.JSONDecodeError:
            parsed = []
        if parsed and len(parsed) == len([line for line in lines if line.strip()]):
            return "\n".join(
                json.dumps(_redact_json_secrets(item, known), separators=(",", ":"))
                for item in parsed
            ) + ("\n" if text.endswith("\n") else "")
    try:
        parsed_document = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return json.dumps(
            _redact_json_secrets(parsed_document, known),
            separators=(",", ":"),
        )
    for secret in known:
        text = text.replace(secret, "[redacted-known-secret]")
    for pattern in _TRACE_SECRET_PATTERNS:
        text = pattern.sub(r"\1[redacted]", text)
    return _TRACE_SECRET_LITERAL.sub("[redacted-secret-literal]", text)


def _sanitize_trace_zip(
    source: zipfile.ZipFile,
    destination: zipfile.ZipFile,
    known: tuple[str, ...],
    budget: dict[str, int],
    *,
    depth: int,
) -> None:
    if depth > MAX_TRACE_ARCHIVE_DEPTH:
        raise ValueError("trace archive nesting exceeds limit")
    members = source.infolist()
    budget["members"] += len(members)
    if budget["members"] > MAX_TRACE_MEMBERS:
        raise ValueError("trace archive member count exceeds limit")
    seen: set[str] = set()
    for info in members:
        member = Path(info.filename)
        if (
            info.filename in seen
            or member.is_absolute()
            or ".." in member.parts
            or info.flag_bits & 0x1
            or info.file_size < 0
        ):
            raise ValueError("unsafe trace member")
        seen.add(info.filename)
        if info.file_size > MAX_TRACE_MEMBER_BYTES:
            raise ValueError("trace archive member exceeds limit")
        budget["expanded"] += info.file_size
        if budget["expanded"] > MAX_TRACE_EXPANDED_BYTES:
            raise ValueError("trace archive expanded bytes exceed limit")
        if info.is_dir():
            destination.writestr(info, b"")
            continue
        payload = bytearray()
        with source.open(info) as member_source:
            while chunk := member_source.read(1024 * 1024):
                if (
                    len(payload) + len(chunk) > info.file_size
                    or len(payload) + len(chunk) > MAX_TRACE_MEMBER_BYTES
                ):
                    raise ValueError("trace archive member exceeded declared size")
                payload.extend(chunk)
        if len(payload) != info.file_size:
            raise ValueError("trace archive member size did not match declaration")
        sanitized = bytes(payload)
        nested_source_buffer = io.BytesIO(sanitized)
        if zipfile.is_zipfile(nested_source_buffer):
            nested_source_buffer.seek(0)
            nested_destination_buffer = io.BytesIO()
            with (
                zipfile.ZipFile(nested_source_buffer, "r") as nested_source,
                zipfile.ZipFile(
                    nested_destination_buffer,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as nested_destination,
            ):
                _sanitize_trace_zip(
                    nested_source,
                    nested_destination,
                    known,
                    budget,
                    depth=depth + 1,
                )
            sanitized = nested_destination_buffer.getvalue()
            if len(sanitized) > MAX_TRACE_MEMBER_BYTES:
                raise ValueError("sanitized nested trace archive exceeds member limit")
        else:
            try:
                text = sanitized.decode("utf-8")
            except UnicodeDecodeError as exc:
                if any(value.encode() in sanitized for value in known):
                    raise ValueError("known secret in binary trace member") from exc
            else:
                sanitized = _redact_trace_text(text, known).encode("utf-8")
        if any(value.encode() in sanitized for value in known):
            raise ValueError("known secret survived trace redaction")
        destination.writestr(info, sanitized)


def _sanitize_trace_archive(path: Path) -> None:
    """Bound and redact a trace ZIP in one streaming transformation."""
    if path.stat().st_size > MAX_TRACE_ARCHIVE_BYTES:
        raise ValueError("trace archive exceeds top-level size limit")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    known = _known_secret_values()
    try:
        with (
            zipfile.ZipFile(path, "r") as source,
            zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as destination,
        ):
            _sanitize_trace_zip(
                source,
                destination,
                known,
                {"members": 0, "expanded": 0},
                depth=1,
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _test_directory(run_dir: Path, nodeid: str) -> Path:
    digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()
    return run_dir / "playwright" / f"test-{digest[:20]}"


class ContextMetadata:
    """Collect bounded, redacted browser metadata for one test context."""

    def __init__(self, nodeid: str, style: str, context_dir: Path | None) -> None:
        self.nodeid = redact_text(nodeid)
        self.nodeid_sha256 = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()
        self.style = style
        self.context_dir = context_dir
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.limitations = [
            "Request and response headers and bodies are intentionally not captured.",
            "Console and page-error text is bounded and redacted heuristically.",
        ]
        self._truncated = False

    @property
    def enabled(self) -> bool:
        return self.context_dir is not None

    def add(self, event_type: str, **metadata: object) -> None:
        if not self.enabled:
            return
        if len(self.events) >= _MAX_EVENTS:
            self._truncated = True
            return
        self.events.append(
            {
                "type": event_type,
                "elapsed_ms": round((time.monotonic() - self.started) * 1_000, 3),
                **metadata,
            }
        )

    def attach_page(self, page: Any) -> None:
        if not self.enabled:
            return
        page.on(
            "console",
            lambda message: self.add(
                "console",
                level=redact_text(message.type),
                text=redact_text(message.text),
            ),
        )
        page.on(
            "pageerror",
            lambda error: self.add("page_error", message=redact_text(error)),
        )
        page.on(
            "request",
            lambda request: self.add(
                "request",
                method=redact_text(request.method),
                resource_type=redact_text(request.resource_type),
                url=redact_url(request.url),
            ),
        )
        page.on(
            "response",
            lambda response: self.add(
                "response",
                method=redact_text(response.request.method),
                resource_type=redact_text(response.request.resource_type),
                status=int(response.status),
                url=redact_url(response.url),
            ),
        )
        page.on(
            "requestfailed",
            lambda request: self.add(
                "request_failed",
                method=redact_text(request.method),
                resource_type=redact_text(request.resource_type),
                url=redact_url(request.url),
                failure=redact_text(request.failure or ""),
            ),
        )

    def write(self) -> None:
        if self.context_dir is None:
            return
        if self._truncated:
            self.limitations.append(f"Browser metadata was capped at {_MAX_EVENTS} events.")
        _atomic_json(
            self.context_dir / "metadata.json",
            {
                "schema_version": 1,
                "nodeid": self.nodeid,
                "nodeid_sha256": self.nodeid_sha256,
                "context_style": self.style,
                "lifecycle": "closed",
                "events": self.events,
                "limitations": self.limitations,
            },
        )


def _evidence_context_dir(nodeid: str, index: int) -> Path | None:
    raw_run_dir = os.environ.get(VERIFY_RUN_DIR_ENV)
    if not raw_run_dir:
        return None
    run_dir = Path(raw_run_dir).resolve()
    test_dir = _test_directory(run_dir, nodeid)
    test_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        context_dir = test_dir / f"context-{index}-{secrets.token_hex(12)}"
        try:
            context_dir.mkdir()
        except FileExistsError:
            continue
        return context_dir
    raise RuntimeError("Could not allocate a unique Playwright evidence directory.")


class SyncEvidenceBrowser:
    """Function-scoped Browser facade that records every created context."""

    def __init__(
        self,
        browser: Browser,
        artifacts_recorder: Any,
        nodeid: str,
        context_args: dict[str, Any],
        mark_context: Callable[[], None],
    ) -> None:
        self._browser = browser
        self._artifacts_recorder = artifacts_recorder
        self._nodeid = nodeid
        self._context_args = context_args
        self._mark_context = mark_context
        self._contexts: list[BrowserContext] = []
        self._context_count = 0

    def _create_context(self, style: str, **kwargs: Any) -> BrowserContext:
        self._context_count += 1
        self._mark_context()
        metadata = ContextMetadata(
            self._nodeid,
            style,
            _evidence_context_dir(self._nodeid, self._context_count),
        )
        options = {**self._context_args, **kwargs}
        context = self._browser.new_context(**options)
        context.on("page", metadata.attach_page)
        original_close = context.close

        def close(*args: Any, **close_kwargs: Any) -> None:
            if context in self._contexts:
                self._contexts.remove(context)
                trace_count = len(getattr(self._artifacts_recorder, "_traces", ()))
                screenshot_count = len(getattr(self._artifacts_recorder, "_screenshots", ()))
                self._artifacts_recorder.on_will_close_browser_context(context)
                if metadata.context_dir is not None:
                    recorder_artifacts = (
                        (
                            "trace",
                            getattr(self._artifacts_recorder, "_traces", ())[trace_count:],
                            ".zip",
                        ),
                        (
                            "screenshot",
                            getattr(self._artifacts_recorder, "_screenshots", ())[
                                screenshot_count:
                            ],
                            ".png",
                        ),
                    )
                    for kind, sources, suffix in recorder_artifacts:
                        for index, source in enumerate(sources, start=1):
                            source_path = Path(source)
                            recorder_root = Path(
                                self._artifacts_recorder._pw_artifacts_folder.name
                            ).resolve(strict=True)
                            resolved_source = source_path.resolve(strict=True)
                            resolved_source.relative_to(recorder_root)
                            if resolved_source.is_file() and not source_path.is_symlink():
                                destination = (
                                    metadata.context_dir / f"recorder-{kind}-{index}{suffix}"
                                )
                                shutil.copyfile(resolved_source, destination)
                                if kind == "trace":
                                    try:
                                        _sanitize_trace_archive(destination)
                                    except (OSError, ValueError, zipfile.BadZipFile) as exc:
                                        destination.unlink(missing_ok=True)
                                        metadata.limitations.append(
                                            "Trace privacy transformation failed "
                                            f"({type(exc).__name__}); trace was deleted."
                                        )
                metadata.write()
            original_close(*args, **close_kwargs)

        object.__setattr__(context, "close", close)
        self._contexts.append(context)
        self._artifacts_recorder.on_did_create_browser_context(context)
        return context

    def new_context(self, **kwargs: Any) -> BrowserContext:
        return self._create_context("direct-sync", **kwargs)

    def new_page(self, **kwargs: Any) -> Any:
        return self.new_context(**kwargs).new_page()

    def close_contexts(self) -> None:
        for context in self._contexts.copy():
            context.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)


class AsyncEvidenceBrowser:
    """Async Browser facade for tests that launch Playwright directly."""

    def __init__(
        self,
        browser: AsyncBrowser,
        nodeid: str,
        mark_context: Callable[[], None],
    ) -> None:
        self._browser = browser
        self._nodeid = nodeid
        self._mark_context = mark_context
        self._contexts: list[AsyncBrowserContext] = []
        self._context_count = 0

    async def new_context(self, **kwargs: Any) -> AsyncBrowserContext:
        self._context_count += 1
        self._mark_context()
        context_dir = _evidence_context_dir(self._nodeid, self._context_count)
        if context_dir is not None and "record_video_dir" not in kwargs:
            kwargs["record_video_dir"] = str(context_dir / "videos")
        context = await self._browser.new_context(**kwargs)
        metadata = ContextMetadata(self._nodeid, "direct-async", context_dir)
        context.on("page", metadata.attach_page)
        if context_dir is not None:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        original_close = context.close

        async def close(*args: Any, **close_kwargs: Any) -> None:
            if context in self._contexts:
                self._contexts.remove(context)
                if context_dir is not None:
                    for index, page in enumerate(context.pages, start=1):
                        try:
                            await page.screenshot(
                                path=context_dir / f"screenshot-{index}.png",
                                full_page=True,
                                timeout=5_000,
                            )
                        except Exception as exc:
                            metadata.limitations.append(
                                f"Screenshot capture failed ({type(exc).__name__})."
                            )
                    try:
                        trace_path = context_dir / "trace.zip"
                        await context.tracing.stop(path=trace_path)
                        _sanitize_trace_archive(trace_path)
                    except Exception as exc:
                        (context_dir / "trace.zip").unlink(missing_ok=True)
                        metadata.limitations.append(
                            "Trace capture or privacy transformation failed "
                            f"({type(exc).__name__}); trace was deleted."
                        )
                await original_close(*args, **close_kwargs)
                metadata.write()
                return
            await original_close(*args, **close_kwargs)

        object.__setattr__(context, "close", close)
        self._contexts.append(context)
        return context

    async def new_page(self, **kwargs: Any) -> Any:
        context = await self.new_context(**kwargs)
        return await context.new_page()

    async def close(self, *args: Any, **kwargs: Any) -> None:
        for context in self._contexts.copy():
            await context.close()
        await self._browser.close(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)
