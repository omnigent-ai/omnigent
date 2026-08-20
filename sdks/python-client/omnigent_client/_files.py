"""Session-scoped files namespace — upload, list, get, delete."""

from __future__ import annotations

import mimetypes
import pathlib
from urllib.parse import quote

import httpx

from ._errors import raise_for_status, require_json_object, response_body
from ._types import File, PaginatedList


class FilesNamespace:
    """
    Factory for session-scoped file namespaces.

    :param http: Shared async HTTP client.
    :param base_url: Server base URL, e.g. ``"http://localhost:8080"``.
    """

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        """
        Initialize the unbound files namespace.

        :param http: Shared async HTTP client.
        :param base_url: Server base URL, e.g. ``"http://localhost:8080"``.
        :returns: None.
        """
        self._http = http
        self._base = base_url

    def for_session(self, session_id: str) -> SessionFilesNamespace:
        """
        Return a file namespace bound to one session.

        ``/v1/files`` has been removed; callers must scope uploads and
        downloads to the session/conversation that owns the file.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :returns: A :class:`SessionFilesNamespace` bound to that session.
        """
        return SessionFilesNamespace(self._http, self._base, session_id)

    async def upload(self, path: str) -> File:
        """Legacy global upload removed; use ``for_session(id).upload``."""
        raise RuntimeError(
            "/v1/files was removed; use client.files.for_session(session_id).upload(path)"
        )

    async def list(self, *args: object, **kwargs: object) -> list[File]:
        """Legacy global list removed; use ``for_session(id).list``."""
        raise RuntimeError(
            "/v1/files was removed; use client.files.for_session(session_id).list()"
        )

    async def get(self, file_id: str) -> File:
        """Legacy global get removed; use ``for_session(id).get``."""
        raise RuntimeError(
            "/v1/files was removed; use client.files.for_session(session_id).get(file_id)"
        )

    async def get_content(self, file_id: str) -> bytes:
        """Legacy global content download removed; use ``for_session(id).get_content``."""
        raise RuntimeError(
            "/v1/files was removed; use client.files.for_session(session_id).get_content(file_id)"
        )

    async def download(self, file_id: str, to_path: str | pathlib.Path) -> pathlib.Path:
        """Legacy global download removed; use ``for_session(id).download``."""
        raise RuntimeError(
            "/v1/files was removed; use "
            "client.files.for_session(session_id).download(file_id, path)"
        )

    async def delete(self, file_id: str) -> None:
        """Legacy global delete removed; use ``for_session(id).delete``."""
        raise RuntimeError(
            "/v1/files was removed; use client.files.for_session(session_id).delete(file_id)"
        )


class SessionFilesNamespace:
    """
    File operations scoped to a single session.

    :param http: Shared async HTTP client.
    :param base_url: Server base URL, e.g. ``"http://localhost:8080"``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    """

    def __init__(self, http: httpx.AsyncClient, base_url: str, session_id: str) -> None:
        """
        Initialize the session-scoped files namespace.

        :param http: Shared async HTTP client.
        :param base_url: Server base URL, e.g. ``"http://localhost:8080"``.
        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :returns: None.
        """
        self._http = http
        self._base = base_url
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        """
        The session/conversation id this namespace is bound to.

        :returns: Session id, e.g. ``"conv_abc123"``.
        """
        return self._session_id

    @property
    def _path(self) -> str:
        """
        Session-scoped files collection URL.

        :returns: Fully qualified endpoint URL.
        """
        return f"{self._base}/v1/sessions/{quote(self._session_id, safe='')}/resources/files"

    @staticmethod
    def _resource_to_file(data: dict[str, object]) -> File:
        """
        Convert a session file resource response to SDK ``File``.

        :param data: JSON object returned by a session file endpoint.
        :returns: The corresponding :class:`File`.
        """
        metadata = data.get("metadata")
        metadata_dict = metadata if isinstance(metadata, dict) else {}
        return File.from_dict(
            {
                "id": data.get("id", ""),
                "filename": metadata_dict.get("filename", data.get("name", "")),
                "bytes": metadata_dict.get("bytes", 0),
                "created_at": metadata_dict.get("created_at", 0),
            }
        )

    async def upload(self, path: str) -> File:
        """
        Upload a local file into this session's file namespace.

        :param path: Path to the local file, e.g. ``"./report.pdf"``.
        :returns: The uploaded file metadata.
        """
        p = pathlib.Path(path)
        content_type = mimetypes.guess_type(str(p))[0]
        with open(p, "rb") as f:
            resp = await self._http.post(
                self._path,
                files={"file": (p.name, f, content_type)},
                timeout=30.0,
            )
        raise_for_status(resp.status_code, response_body(resp))
        return self._resource_to_file(
            require_json_object(resp, "POST /v1/sessions/{session_id}/resources/files")
        )

    async def list(
        self,
        *,
        limit: int = 20,
        after: str | None = None,
        order: str = "desc",
    ) -> list[File]:
        """
        List files owned by this session.

        :param limit: Maximum number of files to return.
        :param after: Cursor for forward pagination,
            e.g. ``"file_abc123"``.
        :param order: Sort order, e.g. ``"desc"``.
        :returns: File metadata entries.
        """
        params: dict[str, str | int] = {"limit": limit, "order": order}
        if after is not None:
            params["after"] = after
        resp = await self._http.get(self._path, params=params)
        raise_for_status(resp.status_code, response_body(resp))
        page = PaginatedList.from_dict(
            require_json_object(resp, "GET /v1/sessions/{session_id}/resources/files")
        )
        return [self._resource_to_file(d) for d in page.data]

    async def get(self, file_id: str) -> File:
        """
        Get session file metadata by ID.

        :param file_id: Server-issued file id, e.g. ``"file_abc123"``.
        :returns: File metadata.
        """
        resp = await self._http.get(f"{self._path}/{quote(file_id, safe='')}")
        raise_for_status(resp.status_code, response_body(resp))
        return self._resource_to_file(
            require_json_object(resp, "GET /v1/sessions/{session_id}/resources/files/{file_id}")
        )

    async def get_content(self, file_id: str) -> bytes:
        """
        Download session file content.

        :param file_id: Server-issued file id, e.g. ``"file_abc123"``.
        :returns: Raw file bytes.
        """
        resp = await self._http.get(
            f"{self._path}/{quote(file_id, safe='')}/content",
            timeout=30.0,
        )
        if resp.status_code >= 400:
            raise_for_status(resp.status_code, response_body(resp))
        return resp.content

    async def download(self, file_id: str, to_path: str | pathlib.Path) -> pathlib.Path:
        """
        Download file content and write it to disk.

        :param file_id: Server-issued file id, e.g. ``"file_abc123"``.
        :param to_path: Local output path, e.g. ``"./out/report.pdf"``.
        :returns: The path that was written.
        """
        content = await self.get_content(file_id)
        path = pathlib.Path(to_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    async def delete(self, file_id: str) -> None:
        """
        Delete a session-scoped file.

        :param file_id: Server-issued file id, e.g. ``"file_abc123"``.
        :returns: None.
        """
        resp = await self._http.delete(f"{self._path}/{quote(file_id, safe='')}")
        if resp.status_code >= 400:
            raise_for_status(resp.status_code, response_body(resp))
