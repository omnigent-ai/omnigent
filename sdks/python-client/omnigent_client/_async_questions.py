"""Persisted Codex async question metadata (not a pending RPC)."""

from typing import NotRequired, TypedDict


class AsyncUserInputQuestion(TypedDict):
    title: str
    options: NotRequired[list[str] | None]


class AsyncUserInput(TypedDict):
    reply_id: NotRequired[str]
    call_id: str
    questions: list[AsyncUserInputQuestion]
