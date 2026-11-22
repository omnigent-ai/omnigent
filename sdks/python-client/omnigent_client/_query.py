"""Return types for the ``query()`` convenience API.

:class:`QueryResult` is returned from ``query(stream=False)`` (the
default). It carries the final assistant text plus any file artifacts
the agent produced on this turn.

:class:`QueryStream` is returned from ``query(stream=True)``. It is
an async-iterable of text chunks. After the iteration completes, its
``files`` attribute holds the same list of produced files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ._types import File


@dataclass(frozen=True)
class QueryResult:
    """Non-streaming result of a ``query()`` call.

    :param text: The assistant's final text, joined across tool-loop
        iterations. Empty string if the agent produced no text.
    :param files: Files the agent produced on this turn (via the
        ``sys_files_upload`` tool or equivalent). Empty list if none.
        Each entry is a :class:`File` with ``id``, ``filename``,
        ``bytes``, and ``created_at``; use
        :meth:`FilesNamespace.download` or
        :meth:`FilesNamespace.get_content` to retrieve the bytes.
    """

    text: str
    files: list[File] = field(default_factory=list)


class QueryStream:
    """Async-iterable of text chunks from ``query(stream=True)``.

    Iterating yields ``str`` chunks in order as the agent emits them.
    After the iterator is exhausted, :attr:`files` holds the file
    artifacts produced this turn. Files discovered during streaming
    are appended as they arrive, so an in-progress iteration may
    expose a partial list.

    Single-use: iterating a second time raises ``RuntimeError``.
    """

    def __init__(
        self,
        chunks: AsyncIterator[str],
        files: list[File],
    ) -> None:
        """Wrap a text-chunk iterator with a reference to a shared file list.

        :param chunks: Async iterator that yields assistant text
            chunks. Typically produced by the ``Session`` internals.
        :param files: List that will be populated with produced
            :class:`File` entries as the iterator is consumed.
            Passed by reference so the caller and the backing
            generator share the same list.
        """
        self._chunks = chunks
        self._files = files
        self._consumed = False

    def __aiter__(self) -> AsyncIterator[str]:
        """Return the underlying async iterator.

        :raises RuntimeError: If the stream has already been iterated.
        :returns: The wrapped async iterator of text chunks.
        """
        if self._consumed:
            raise RuntimeError(
                "QueryStream has already been iterated. Each stream is "
                "single-use — call client.query(..., stream=True) again "
                "to get a fresh one."
            )
        self._consumed = True
        return self._chunks

    @property
    def files(self) -> list[File]:
        """Files the agent has produced so far this turn.

        Empty until the first file arrives. Fully populated once the
        iterator is exhausted.

        :returns: A shallow copy of the internal list.
        """
        return list(self._files)
