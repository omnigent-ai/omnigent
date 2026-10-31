"""Content-free evidence for exceptions escaping runner session initialization."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from omnigent.errors import OmnigentError


@dataclass
class SessionInitDiagnostics:
    """Track one shared initialization task without retaining its request body."""

    phase: str = "validate_request"


def session_init_exception_fields(exc: Exception) -> dict[str, str | int | bool]:
    """Read a bounded exception chain without messages, paths, URLs, or payloads."""
    fields: dict[str, str | int | bool] = {}
    current: BaseException | None = exc
    seen: set[int] = set()
    for prefix in ("exception", "exception_cause", "exception_cause_2", "exception_cause_3"):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        fields[f"{prefix}_type"] = (
            name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", name) else "other"
        )
        if isinstance(current, OSError) and type(current.errno) is int:
            if -(2**31) <= current.errno < 2**31:
                fields[f"{prefix}_errno"] = current.errno
        status = (
            current.response.status_code
            if isinstance(current, httpx.HTTPStatusError)
            else current.http_status
            if isinstance(current, OmnigentError)
            else None
        )
        if type(status) is int and 100 <= status <= 599:
            fields[f"{prefix}_http_status"] = status
        traceback = current.__traceback__
        for _ in range(32):
            if traceback is None:
                break
            module = traceback.tb_frame.f_globals.get("__name__")
            function = traceback.tb_frame.f_code.co_name
            if (
                isinstance(module, str)
                and re.fullmatch(r"omnigent\.[A-Za-z_0-9.]{1,240}", module)
                and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", function)
            ):
                fields[f"{prefix}_origin_module"] = module
                fields[f"{prefix}_origin_function"] = function
                fields[f"{prefix}_origin_line"] = traceback.tb_lineno
            traceback = traceback.tb_next
        current = current.__cause__ or (
            current.__context__ if not current.__suppress_context__ else None
        )
    fields["exception_chain_truncated"] = current is not None
    return fields
