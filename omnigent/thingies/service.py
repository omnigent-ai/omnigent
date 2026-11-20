"""Thingies for one viewer: metadata, revisions, records, comments, actions and the shelf.

A :class:`ThingyService` is built per request for the calling user. It checks
every operation against :class:`~omnigent.thingies.access.ThingyAccess` and
keeps everything a thingy holds in that thingy's
:class:`~omnigent.stores.thingy_store.ThingyFiles`, laid out as::

    code/revisions/<revision>.json   # immutable uploaded revisions
    code/state/publication/current/  # which revision is published (a document)
    data/<collection>/<id>/          # the thingy's records (documents)
    data/_guard/                     # rule state for collections with constraints
    data/_comments/                  # comments
    data/_sessions/                  # each viewer's thingy session and open actions
    data/_actions/                   # the action ledger

Collection names cannot start with ``_``, so the thingy's records never share a
namespace with the others. Each viewer's shelf (pins, and the thingies shown
beside each of their sessions) lives in their own user files.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import jsonschema

from omnigent.stores.thingy_store import MAX_THINGY_NAME_CHARS, Thingy, ThingyFiles, ThingyStore
from omnigent.thingies.access import NO_ACCESS, ThingyAccess, ThingyPermissions
from omnigent.thingies.actions import (
    validate_actions,
    validate_request,
    validate_session_id,
    validate_update,
)
from omnigent.thingies.constraints import ConstraintGuard, validate_constraints, validate_guide
from omnigent.thingies.documents import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    Document,
    ImmutableDocumentStore,
    InvalidInput,
    NotFound,
    StoreLimits,
)
from omnigent.thingies.progress import (
    OPEN_STATUSES,
    ActionProgress,
    SessionStateReader,
    delivery_of,
    derive_ledger_progress,
    session_tree_status,
)

MAX_HTML_BYTES = 1024 * 1024
MAX_NAMESPACE_ENTRIES = 1024
MAX_COLLECTIONS = 16
MAX_COLLECTION_IDENTITIES = 128
MAX_SCHEMA_BYTES = 16 * 1024
_DATA_STORE_LIMITS = StoreLimits(
    max_records_per_collection=1000, max_list_entries=MAX_NAMESPACE_ENTRIES
)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{32}$")
_THINGY_ID = re.compile(r"^[0-9a-f]{32}$")
# The viewer's pins and session links, in their own files.
SHELF_ROOT = "shelf"
MAX_PINNED_THINGIES = 50
MAX_SESSION_THINGIES = 20
COMMENT_STATUSES = frozenset({"draft", "addressed"})
MAX_COMMENT_CHARS = 4000
MAX_THINGY_COMMENTS = 1000
_KNOWN_CAPABILITIES = frozenset({"data.read", "data.write", "sessions.read", "sessions.create"})
# The web app answers session reads with the viewer's own session list after asking
# them; publishing a revision that requests new ones still needs manage permission.
_SUPPORTED_CAPABILITIES = frozenset({"data.read", "data.write", "sessions.read"})
_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "title",
        "description",
        "default",
    }
)
# A viewer's actions whose progress the web app follows; older ones keep their ledger state.
MAX_OPEN_ACTIONS = 10
# A request the web app has not delivered within this long (the viewer never started the
# New session it opened) is no longer open.
UNDELIVERED_TTL_S = 30 * 60
MAX_PROGRESS_IDS = 20
# What ``describe`` tells an agent to do next; the ``thingy`` skill has the details.
_NEXT_STEPS = (
    "Load the thingy skill and follow it: it says how to handle 'Thingy action' messages, "
    "read and change records with sys_thingy_records, and change or publish this thingy."
)
_NEXT_STEPS_UNPUBLISHED = (
    "Nothing is published yet. Load the thingy skill and follow it to build this thingy: "
    "upload a page with sys_thingy_revision_create, then publish it with sys_thingy_publish "
    "(if_version null)."
)


class ThingyError(Exception):
    """A refused thingy request with its API error code and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FrontendRevisionChanged(Conflict):
    """The frame asked for a revision that is no longer the published one."""

    def __init__(self, published_revision: str) -> None:
        super().__init__("The published thingy changed; load its new version before continuing")
        self.details = {"reason": "REVISION_CHANGED", "published_revision": published_revision}


@dataclass(frozen=True)
class ThingyViewer:
    """The caller.

    :param user_id: e.g. ``"alice@example.com"``; ``"local"`` in single-user mode.
    """

    user_id: str

    @property
    def key(self) -> str:
        """A record ID that stands for the viewer; user IDs may hold any characters."""
        return hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:32]


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInput("Expected a finite JSON value") from exc


def _digest(value: Any, length: int = 32) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:length]


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidInput(f"Invalid {label}")
    return value


def _operation_id(value: Any) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise InvalidInput("Invalid operation ID")
    return value


def _revision_id(value: Any) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise InvalidInput("Invalid frontend revision")
    return value


def _session_id(value: Any) -> str:
    checked = validate_session_id(value)
    if checked is None:
        raise InvalidInput("Invalid session ID")
    return checked


def _thingy_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_THINGY_NAME_CHARS:
        raise InvalidInput(f"Thingy name must contain 1–{MAX_THINGY_NAME_CHARS} characters")
    return value.strip()


def _validate_schema(schema: Any, depth: int = 0, counter: list[int] | None = None) -> None:
    # Remote references and regex/combinator evaluation are deliberately left out.
    counter = [0] if counter is None else counter
    counter[0] += 1
    if not isinstance(schema, dict):
        raise InvalidInput("A schema must be a JSON object")
    if depth > 12 or counter[0] > 256:
        raise InvalidInput("Collection schema is too complex")
    unsupported = sorted(set(schema) - _SCHEMA_KEYS)
    if unsupported:
        # Name both, so a caller drops only what is refused and keeps its other checks.
        raise InvalidInput(
            f"Unsupported schema keyword{'s' if len(unsupported) > 1 else ''}: "
            f"{', '.join(unsupported)[:300]}. Supported: {', '.join(sorted(_SCHEMA_KEYS))}"
        )
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise InvalidInput("Schema properties must be an object")
    for child in properties.values():
        _validate_schema(child, depth + 1, counter)
    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, dict):
            _validate_schema(child, depth + 1, counter)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise InvalidInput("Invalid collection schema") from exc


def _schema_mismatch(collection: str, error: jsonschema.ValidationError) -> InvalidInput:
    """Which field of a record breaks its collection's schema, and why."""
    path = [part if isinstance(part, int) else str(part)[:64] for part in error.absolute_path]
    where = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in path)
    reason = error.message[:200]
    return InvalidInput(
        f"The record does not match the {collection} schema at value{where}: {reason}",
        details={"path": path, "reason": reason, "schema_keyword": error.validator},
    )


def _frontend_input(
    body: dict,
) -> tuple[str, list[str], dict, str | None, list[dict], list[dict]]:
    html = body.get("html")
    if not isinstance(html, str) or not html.strip() or len(html.encode()) > MAX_HTML_BYTES:
        raise InvalidInput("Frontend HTML must be non-empty and at most 1 MiB")
    if type(body.get("sdk_version")) is not int or body["sdk_version"] != 1:
        raise InvalidInput("Only SDK version 1 is supported")
    capabilities = body.get("capabilities", [])
    if not isinstance(capabilities, list) or len(capabilities) > len(_KNOWN_CAPABILITIES):
        raise InvalidInput("Invalid capabilities")
    if any(not isinstance(item, str) or item not in _KNOWN_CAPABILITIES for item in capabilities):
        raise InvalidInput(
            "Unknown capability: use data.read, data.write or sessions.read; actions need "
            "none, only an entry in actions"
        )
    collections = body.get("collections", {})
    if not isinstance(collections, dict) or len(collections) > MAX_COLLECTIONS:
        raise InvalidInput("Invalid collection definitions")
    if len(canonical_bytes(collections)) > MAX_SCHEMA_BYTES:
        raise CapacityExceeded("Collection definitions exceed the schema limit")
    for name, schema in collections.items():
        _identifier(name, "collection")
        _validate_schema(schema)
    guide = validate_guide(body.get("guide"))
    constraints = validate_constraints(body.get("constraints"), collections)
    actions = validate_actions(body.get("actions"), _validate_schema)
    return html, sorted(set(capabilities)), collections, guide, constraints, actions


# A revision derived from another carries these from it unless the request gives them.
_REVISION_FIELDS = (
    "sdk_version",
    "capabilities",
    "collections",
    "guide",
    "constraints",
    "actions",
)
MAX_REVISION_EDITS = 32


def _apply_edits(html: str, edits: Any) -> str:
    """*html* with exact text *edits* applied in order.

    An edit is ``{"old", "new"}``, whose ``old`` text must appear exactly once, or
    ``{"start", "end", "new"}``, which replaces the text between two markers that each
    appear exactly once, keeping the markers.
    """
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_REVISION_EDITS:
        raise InvalidInput(f"edits must be a list of 1 to {MAX_REVISION_EDITS} edits")
    for number, edit in enumerate(edits, start=1):

        def refuse(message: str, reason: str, number: int = number, **extra: Any) -> InvalidInput:
            return InvalidInput(
                f"Edit {number}: {message}", details={"edit": number, "reason": reason, **extra}
            )

        if not isinstance(edit, dict) or not isinstance(edit.get("new"), str):
            raise refuse("needs new text, and old text or start and end markers", "invalid")
        old, start, end = edit.get("old"), edit.get("start"), edit.get("end")
        if isinstance(old, str) and old and start is None and end is None:
            found = html.count(old)
            if found != 1:
                raise (
                    refuse("its old text is not in the HTML", "not_found")
                    if found == 0
                    else refuse(
                        f"its old text appears {found} times; add surrounding text so it "
                        "appears once",
                        "ambiguous",
                        matches=found,
                    )
                )
            html = html.replace(old, edit["new"], 1)
            continue
        if not (isinstance(start, str) and start and isinstance(end, str) and end) or old:
            raise refuse("needs new text, and old text or start and end markers", "invalid")
        for label, marker in (("start", start), ("end", end)):
            found = html.count(marker)
            if found != 1:
                raise refuse(
                    f"its {label} marker appears {found} times; it must appear once",
                    "not_found" if found == 0 else "ambiguous",
                    marker=label,
                    matches=found,
                )
        begin = html.index(start) + len(start)
        finish = html.index(end)
        if finish < begin:
            raise refuse("its end marker comes before its start marker", "invalid")
        html = html[:begin] + edit["new"] + html[finish:]
    return html


def _epoch(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


class ThingyService:
    """One viewer's operations on thingies (see module docstring)."""

    def __init__(
        self,
        store: ThingyStore,
        access: ThingyAccess,
        viewer: ThingyViewer,
        sessions: SessionStateReader | None = None,
        display_names: Callable[[Sequence[str]], dict[str, str]] | None = None,
    ) -> None:
        """
        :param store: Where thingies are kept.
        :param access: Decides the viewer's permissions.
        :param viewer: The caller.
        :param sessions: Reads the state of the viewer's sessions for action
            progress; ``None`` reports progress from the ledger alone.
        :param display_names: Names for user IDs, e.g. a shared thingy's owner;
            ``None`` shows the IDs.
        """
        self.store = store
        self.access = access
        self.viewer = viewer
        self.sessions = sessions
        self._display_names = display_names

    # -- resolving thingies and permissions ------------------------------------

    def _resolve(self, app_id: Any) -> tuple[Thingy, ThingyPermissions]:
        if not isinstance(app_id, str) or _THINGY_ID.fullmatch(app_id) is None:
            raise NotFound("Thingy not found")
        thingy = self.store.get(app_id)
        if thingy is None:
            raise NotFound("Thingy not found")
        permissions = self.access.permissions(self.viewer.user_id, thingy)
        if not permissions.read:
            raise NotFound("Thingy not found")
        return thingy, permissions

    @staticmethod
    def _require(permissions: ThingyPermissions, permission: str) -> None:
        if not getattr(permissions, permission):
            raise ThingyError(
                "PERMISSION_DENIED", "This operation requires additional thingy permissions", 403
            )

    def _files(self, thingy: Thingy) -> ThingyFiles:
        return self.store.thingy_files(thingy.id)

    def _state(self, thingy: Thingy) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(self._files(thingy), "code/state")

    def _publication(self, thingy: Thingy) -> dict | None:
        publication = self._state(thingy).get("publication", "current")
        return publication.to_dict() if publication is not None else None

    def _frontend(self, thingy: Thingy, revision_id: Any) -> dict:
        try:
            value = self._files(thingy).read_json(
                f"code/revisions/{_revision_id(revision_id)}.json"
            )
        except NotFound:
            raise NotFound("Revision not found") from None
        if value.get("id") != revision_id:
            raise CorruptData("Invalid frontend revision")
        try:
            _frontend_input(value)
        except (InvalidInput, CapacityExceeded) as exc:
            raise CorruptData("Stored frontend revision is invalid") from exc
        return value

    def _app(self, thingy: Thingy, permissions: ThingyPermissions) -> dict:
        publication = self._publication(thingy)
        return {
            "id": thingy.id,
            "name": thingy.name,
            "browse_url": f"/thingy/{thingy.id}",
            "owner_id": thingy.owner_id,
            "viewer_id": self.viewer.user_id,
            "viewer_principal_id": self.viewer.key,
            "published_revision": publication["value"]["revision_id"] if publication else None,
            "version": publication["version"] if publication else None,
            "permissions": permissions.to_dict(),
        }

    # -- thingies ---------------------------------------------------------------

    def create(self, name: Any) -> dict:
        thingy = self.store.create(self.viewer.user_id, _thingy_name(name))
        return self._app(thingy, self.access.permissions(self.viewer.user_id, thingy))

    def list_owned(self) -> dict:
        """The viewer's own thingies."""
        owned = self.store.list_owned(self.viewer.user_id)
        return {
            "items": [
                self._app(thingy, self.access.permissions(self.viewer.user_id, thingy))
                for thingy in owned
            ]
        }

    def get(self, app_id: str) -> dict:
        return self._app(*self._resolve(app_id))

    def shared(self) -> dict:
        """Other people's thingies that reach the viewer through their own grant or a group's."""
        shared = self.access.shared_with(self.viewer.user_id)
        owners = sorted({item.thingy.owner_id for item in shared})
        names = self._display_names(owners) if self._display_names and owners else {}
        items = []
        for item in shared:
            if item.thingy.owner_id == self.viewer.user_id:
                continue
            permissions = self.access.permissions(self.viewer.user_id, item.thingy)
            if not permissions.read:
                continue
            items.append(
                {
                    **self._app(item.thingy, permissions),
                    "shared_via": item.shared_via,
                    "owner_email": names.get(item.thingy.owner_id, item.thingy.owner_id),
                }
            )
        return {"items": items}

    def rename(self, app_id: str, body: dict) -> dict:
        """Renames the thingy for everyone; needs the same access as publishing it."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        name = _thingy_name(body.get("name"))
        if name != thingy.name:
            renamed = self.store.rename(thingy.id, name)
            if renamed is None:
                raise NotFound("Thingy not found")
            thingy = renamed
        return self._app(thingy, permissions)

    def delete(self, app_id: str) -> dict:
        """Deletes the thingy for everyone, with its versions, data and comments."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "manage")
        self.store.delete(thingy.id)
        self.access.forget(thingy.id)
        return {"id": app_id, "deleted": True}

    def thingy_session_status(self, app_id: str) -> dict:
        """Whether the viewer's thingy session, with the sessions it started, is working."""
        thingy, _ = self._resolve(app_id)
        record = self._sessions(thingy).get("viewers", self.viewer.key)
        session_id = record.value.get("session_id") if record is not None else None
        if not isinstance(session_id, str):
            return {"session_id": None, "status": "none", "reason": None}
        if self.sessions is None:
            return {
                "session_id": session_id,
                "status": "unavailable",
                "reason": "session_unavailable",
            }
        return {
            "session_id": session_id,
            **session_tree_status(self.sessions.read([session_id]), session_id),
        }

    # -- the viewer's shelf: pinned thingies and the thingies each session shows -----
    # It is the viewer's own state, also about thingies shared with them, so it lives
    # in their files rather than in any thingy's. One document each keeps a sidebar
    # load to a few reads.

    @property
    def _shelf(self) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(self.store.user_files(self.viewer.user_id), SHELF_ROOT)

    @staticmethod
    def _session_key(session_id: str) -> str:
        # Session IDs may hold capitals, which document IDs do not allow.
        return hashlib.sha256(session_id.encode()).hexdigest()[:32]

    def _save_shelf(self, collection: str, doc_id: str, current: Any, value: dict) -> None:
        operation_id = f"{collection}-" + _digest([current.version if current else None, value])
        if current is None:
            self._shelf.create(collection, doc_id, value, operation_id, self.viewer.user_id)
        elif current.value != value:
            self._shelf.replace(
                collection, doc_id, value, current.version, operation_id, self.viewer.user_id
            )

    def _pins(self) -> tuple[Any, list[dict]]:
        current = self._shelf.get("pins", "all")
        items = current.value.get("items") if current is not None else None
        pins = (
            [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        )
        return current, pins

    def _readable_pins(self, pins: list[dict]) -> dict[str, Thingy]:
        """The pinned thingies the viewer can still read, keyed by ID.

        A thingy deleted, or no longer shared with them, since they pinned it drops out,
        and so does its name.
        """
        ids = [pin["id"] for pin in pins if isinstance(pin.get("id"), str)]
        found = self.store.get_many(ids)
        permissions = self.access.permissions_many(self.viewer.user_id, list(found.values()))
        return {
            app_id: thingy
            for app_id, thingy in found.items()
            if permissions.get(app_id, NO_ACCESS).read
        }

    def shelf(self) -> dict:
        """The viewer's pinned thingies, and whether they own any, for the sidebar."""
        _, pins = self._pins()
        readable = self._readable_pins(pins)
        ids = [pin["id"] for pin in pins if pin.get("id") in readable]
        return {
            "pinned": [{"id": app_id, "name": readable[app_id].name} for app_id in ids],
            "has_owned": bool(self.store.list_owned(self.viewer.user_id)),
        }

    def pin(self, app_id: str, body: dict) -> dict:
        """Pins or unpins a thingy the viewer can read, whoever owns it.

        Saving the shelf also drops pins the viewer can no longer read.
        """
        pinned = body.get("pinned")
        if not isinstance(pinned, bool):
            raise InvalidInput("pinned must be true or false")
        current, pins = self._pins()
        readable = self._readable_pins(pins)
        others = [pin for pin in pins if pin.get("id") != app_id and pin.get("id") in readable]
        if pinned:
            thingy, _ = self._resolve(app_id)
            others.append({"id": app_id, "name": thingy.name})
            if len(others) > MAX_PINNED_THINGIES:
                raise CapacityExceeded(f"At most {MAX_PINNED_THINGIES} thingies can be pinned")
        if len(others) != len(pins) or pinned:
            self._save_shelf("pins", "all", current, {"items": others})
        return self.shelf()

    def session_thingies(self, session_id: str) -> dict:
        """The thingies shown beside a session, skipping any the viewer can no longer read."""
        session_id = _session_id(session_id)
        current = self._shelf.get("sessions", self._session_key(session_id))
        ids = current.value.get("thingies") if current is not None else None
        items = []
        for app_id in ids if isinstance(ids, list) else []:
            try:
                thingy, permissions = self._resolve(app_id)
            except (NotFound, CorruptData):
                continue
            items.append(self._app(thingy, permissions))
        return {"session_id": session_id, "items": items}

    def _require_session(self, session_id: str) -> None:
        """Refuses a session the viewer cannot read, or one that is gone or archived.

        Without a session reader (tests, or a server without sessions) any well-formed
        ID is taken.
        """
        if self.sessions is not None and not self.sessions.available(session_id):
            raise NotFound("Session not found")

    def link_session(self, app_id: str, session_id: str, body: dict) -> dict:
        """Shows a thingy beside one of the viewer's sessions, or stops showing it there.

        Showing it there also makes that session the viewer's thingy session when they
        may register one, so actions requested from the thingy's own page reach it. Only
        a session the viewer can read can show it.
        """
        session_id = _session_id(session_id)
        linked = body.get("linked")
        if not isinstance(linked, bool):
            raise InvalidInput("linked must be true or false")
        key = self._session_key(session_id)
        current = self._shelf.get("sessions", key)
        ids = current.value.get("thingies") if current is not None else None
        shown = [value for value in ids if isinstance(value, str)] if isinstance(ids, list) else []
        if linked:
            thingy, permissions = self._resolve(app_id)
            self._require_session(session_id)
            # Showing it again keeps its place, so the session's tabs do not reorder.
            if app_id not in shown:
                shown.append(app_id)
            if len(shown) > MAX_SESSION_THINGIES:
                raise CapacityExceeded(
                    f"A session can show at most {MAX_SESSION_THINGIES} thingies"
                )
            if permissions.publish:
                self._register_thingy_session(thingy, session_id)
        else:
            shown = [value for value in shown if value != app_id]
        self._save_shelf("sessions", key, current, {"session_id": session_id, "thingies": shown})
        return self.session_thingies(session_id)

    # -- comments ---------------------------------------------------------------------
    # Kept with the thingy's data, so everyone it is shared with sees them without any
    # session or host running. Commenting needs the same access as changing its data.

    def _comments(self, thingy: Thingy) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(self._files(thingy), "data/_comments")

    @staticmethod
    def _comment(app_id: str, document: Document) -> dict:
        value = document.value
        anchor = value.get("anchor") if isinstance(value.get("anchor"), dict) else {}
        return {
            "id": document.id,
            "thingy_id": app_id,
            "body": value.get("body"),
            "status": value.get("status"),
            "anchor": anchor,
            "version": document.version,
            "created_by": document.created_by,
            "created_at": _epoch(document.created_at),
            "updated_at": _epoch(document.updated_at),
        }

    def list_comments(self, app_id: str, status: Any = None) -> dict:
        if status is not None and status not in COMMENT_STATUSES:
            raise InvalidInput("status must be draft or addressed")
        thingy, permissions = self._resolve(app_id)
        store = self._comments(thingy)
        items: list[dict] = []
        cursor = None
        while len(items) < MAX_THINGY_COMMENTS:
            page = store.list("comments", limit=200, cursor=cursor)
            items.extend(self._comment(app_id, document) for document in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        items = [item for item in items if status is None or item["status"] == status]
        items.sort(key=lambda item: (item["created_at"], item["id"]))
        return {"items": items, "can_comment": permissions.write_data}

    def create_comment(self, app_id: str, body: dict) -> dict:
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        text = body.get("body")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMENT_CHARS:
            raise InvalidInput(f"A comment needs 1–{MAX_COMMENT_CHARS} characters")
        anchor = body.get("anchor")
        if not isinstance(anchor, dict):
            raise InvalidInput("anchor must describe the commented part of the thingy")
        clean: dict[str, Any] = {
            "text": anchor.get("text"),
            "label": anchor.get("label"),
            "ref": anchor.get("ref"),
            "occurrence": anchor.get("occurrence", 1),
        }
        if not isinstance(clean["text"], str) or not 0 < len(clean["text"]) <= MAX_COMMENT_CHARS:
            raise InvalidInput("anchor.text must be the selected text")
        for key in ("label", "ref"):
            if clean[key] is not None and (
                not isinstance(clean[key], str) or len(clean[key]) > 200
            ):
                raise InvalidInput(f"anchor.{key} must be a short string")
        if type(clean["occurrence"]) is not int or clean["occurrence"] < 1:
            raise InvalidInput("anchor.occurrence must be a positive integer")
        operation_id = _operation_id(body.get("operation_id"))
        comment_id = _digest([self.viewer.user_id, operation_id])
        document = self._comments(thingy).create(
            "comments",
            comment_id,
            {"body": text.strip(), "status": "draft", "anchor": clean},
            operation_id,
            self.viewer.user_id,
        )
        return self._comment(app_id, document)

    def update_comment(self, app_id: str, comment_id: str, body: dict) -> dict:
        """Edits a comment's text (its author only) or marks it draft or addressed."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        store = self._comments(thingy)
        current = store.get("comments", _identifier(comment_id, "comment id"))
        if current is None:
            raise NotFound("Comment not found")
        value = dict(current.value)
        if "body" in body:
            text = body["body"]
            if current.created_by != self.viewer.user_id:
                raise ThingyError("PERMISSION_DENIED", "Only its author can edit a comment", 403)
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMENT_CHARS:
                raise InvalidInput(f"A comment needs 1–{MAX_COMMENT_CHARS} characters")
            value["body"] = text.strip()
        if "status" in body:
            if body["status"] not in COMMENT_STATUSES:
                raise InvalidInput("status must be draft or addressed")
            value["status"] = body["status"]
        if value == current.value:
            return self._comment(app_id, current)
        operation_id = "comment-" + _digest([current.version, value])
        document = store.replace(
            "comments", current.id, value, current.version, operation_id, self.viewer.user_id
        )
        return self._comment(app_id, document)

    def delete_comment(self, app_id: str, comment_id: str) -> dict:
        """Its author, or someone who manages the thingy, can delete a comment."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        store = self._comments(thingy)
        current = store.get("comments", _identifier(comment_id, "comment id"))
        if current is None:
            return {"id": comment_id, "deleted": True}
        if current.created_by != self.viewer.user_id:
            self._require(permissions, "manage")
        store.delete(
            "comments",
            current.id,
            current.version,
            f"delete-{current.version[:40]}",
            self.viewer.user_id,
        )
        return {"id": comment_id, "deleted": True}

    # -- revisions and publication ------------------------------------------------

    def create_revision(self, app_id: str, body: dict) -> dict:
        """Upload a revision: a full one, or one derived from ``base_revision_id``.

        A derived revision takes the base's HTML with ``edits`` applied (or the given
        ``html``) and the base's other fields unless the request gives them, so a small
        change to a large page need not send the page again.
        """
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        if body.get("base_revision_id") is not None:
            base = self._frontend(thingy, body["base_revision_id"])
            if "html" in body and "edits" in body:
                raise InvalidInput("Give html or edits with base_revision_id, not both")
            derived = {field: base[field] for field in _REVISION_FIELDS if field in base}
            derived.update({field: body[field] for field in _REVISION_FIELDS if field in body})
            derived["html"] = (
                body["html"] if "html" in body else _apply_edits(base["html"], body.get("edits"))
            )
            body = derived
        elif "edits" in body:
            raise InvalidInput("edits need base_revision_id, the revision they change")
        html, capabilities, collections, guide, constraints, actions = _frontend_input(body)
        revision_id = uuid.uuid4().hex
        value: dict[str, Any] = {
            "id": revision_id,
            "html": html,
            "sdk_version": 1,
            "capabilities": sorted(set(capabilities)),
            "collections": collections,
        }
        # Revisions without a guide or rules keep the smaller stored shape.
        if guide is not None:
            value["guide"] = guide
        if constraints:
            value["constraints"] = constraints
        if actions:
            value["actions"] = actions
        files = self._files(thingy)
        files.mkdirs("code/revisions")
        files.create_json(f"code/revisions/{revision_id}.json", value)
        return {"id": revision_id}

    def publish(self, app_id: str, body: dict) -> dict:
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        revision = self._frontend(thingy, body.get("revision_id"))
        expected = body.get("if_version")
        if expected is not None and (not isinstance(expected, str) or len(expected) > 2048):
            raise InvalidInput("Invalid publication version")
        store = self._state(thingy)
        # The expected version is immutable: retries after another publish must compute
        # the same value. Binding schema identities in this CAS avoids partial registration.
        previous = (
            store.get_version("publication", "current", expected) if expected is not None else None
        )
        identities = previous.value.get("collection_identities") if previous is not None else {}
        if (
            not isinstance(identities, dict)
            or len(identities) > MAX_COLLECTION_IDENTITIES
            or any(
                not isinstance(name, str)
                or _IDENTIFIER.fullmatch(name) is None
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for name, digest in identities.items()
            )
        ):
            raise CorruptData("Invalid publication schema catalogue")
        identities = dict(identities)
        for name, schema in revision["collections"].items():
            digest = hashlib.sha256(canonical_bytes(schema)).hexdigest()
            if name in identities and identities[name] != digest:
                raise InvalidInput("An existing collection schema cannot change")
            identities[name] = digest
        if len(identities) > MAX_COLLECTION_IDENTITIES:
            raise CapacityExceeded("Thingy has reached its lifetime collection limit")
        approved = sorted(set(revision["capabilities"]) & _SUPPORTED_CAPABILITIES)
        previously_approved = (
            set(previous.value.get("approved_capabilities", [])) if previous else set()
        )
        if (set(approved) - previously_approved) - {"data.read", "data.write"}:
            self._require(permissions, "manage")
        value = {
            "revision_id": revision["id"],
            "digest": hashlib.sha256(canonical_bytes(revision)).hexdigest(),
            "approved_capabilities": approved,
            "collection_identities": identities,
        }
        operation_id = "publish-" + _digest(
            {"revision": revision["id"], "expected": expected, "actor": self.viewer.user_id}
        )
        if expected is None:
            store.create("publication", "current", value, operation_id, self.viewer.user_id)
        else:
            store.replace(
                "publication", "current", value, expected, operation_id, self.viewer.user_id
            )
        return self._app(thingy, permissions)

    def _active(
        self, app_id: str, requested_revision: str | None = None
    ) -> tuple[Thingy, ThingyPermissions, dict, dict]:
        thingy, permissions = self._resolve(app_id)
        publication = self._publication(thingy)
        if publication is None:
            raise NotFound("This thingy has not been published")
        return (
            thingy,
            permissions,
            self._published(thingy, publication, requested_revision),
            publication,
        )

    def _published(
        self, thingy: Thingy, publication: dict, requested_revision: str | None = None
    ) -> dict:
        """The published revision, checked against its publication."""
        revision_id = publication["value"]["revision_id"]
        if requested_revision is not None and requested_revision != revision_id:
            raise FrontendRevisionChanged(revision_id)
        revision = self._frontend(thingy, revision_id)
        if hashlib.sha256(canonical_bytes(revision)).hexdigest() != publication["value"]["digest"]:
            raise CorruptData("Published frontend content changed outside the thingy service")
        approved = publication["value"].get("approved_capabilities", [])
        if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
            raise CorruptData("Published capabilities are invalid")
        return revision

    def content(self, app_id: str) -> dict:
        _, permissions, revision, publication = self._active(app_id)
        granted = (
            set(revision["capabilities"])
            & _SUPPORTED_CAPABILITIES
            & set(publication["value"].get("approved_capabilities", []))
        )
        if not permissions.write_data:
            granted.discard("data.write")
        return {
            "revision_id": revision["id"],
            "html": revision["html"],
            "sdk_version": 1,
            "capabilities": sorted(granted),
        }

    def get_revision(self, app_id: str, revision_id: str | None = None) -> dict:
        if revision_id is None:
            _, _, revision, _ = self._active(app_id)
        else:
            thingy, permissions = self._resolve(app_id)
            publication = self._publication(thingy)
            published = publication["value"]["revision_id"] if publication else None
            # Unpublished revisions are drafts: only those who may publish read them.
            if revision_id != published and not permissions.publish:
                raise NotFound("Revision not found")
            revision = self._frontend(thingy, revision_id)
        return {
            "revision_id": revision["id"],
            "html": revision["html"],
            "sdk_version": 1,
            "capabilities": revision["capabilities"],
            "collections": revision["collections"],
            "guide": revision.get("guide"),
            "constraints": revision.get("constraints", []),
            "actions": revision.get("actions", []),
        }

    def describe(self, app_id: str) -> dict:
        """What an agent needs before working on a thingy, without its source.

        Also for a thingy with nothing published yet, whose session starts by building
        it; ``next_steps`` says so. The how-to lives in the ``thingy`` framework skill.
        """
        thingy, permissions = self._resolve(app_id)
        publication = self._publication(thingy)
        revision = self._published(thingy, publication) if publication is not None else {}
        app = self._app(thingy, permissions)
        return {
            "thingy": {key: app[key] for key in ("id", "name", "browse_url", "permissions")},
            "published_revision": revision.get("id"),
            "guide": revision.get("guide"),
            "collections": revision.get("collections", {}),
            "constraints": revision.get("constraints", []),
            "actions": revision.get("actions", []),
            "next_steps": _NEXT_STEPS_UNPUBLISHED if publication is None else _NEXT_STEPS,
        }

    # -- thingy sessions and the action ledger ---------------------------------------
    # Editing the thingy is what lets a viewer request actions or register their
    # thingy session.

    def _sessions(self, thingy: Thingy) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(self._files(thingy), "data/_sessions")

    def _ledger(self, thingy: Thingy) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(self._files(thingy), "data/_actions")

    def get_thingy_session(self, app_id: str) -> dict:
        """The viewer's own thingy session for this thingy, if they registered one."""
        thingy, _ = self._resolve(app_id)
        record = self._sessions(thingy).get("viewers", self.viewer.key)
        if record is None:
            return {"session_id": None, "version": None}
        session_id = record.value.get("session_id")
        if (
            isinstance(session_id, str)
            and self.sessions is not None
            and not self.sessions.available(session_id)
        ):
            # A deleted or archived session no longer takes the thingy's work; the next
            # one started from the thingy replaces it.
            return {"session_id": None, "version": record.version}
        return {"session_id": session_id, "version": record.version}

    def set_thingy_session(self, app_id: str, body: dict) -> dict:
        """Registers one of the sessions the viewer can read as their thingy session."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        session_id = _session_id(body.get("session_id"))
        self._require_session(session_id)
        return self._register_thingy_session(thingy, session_id)

    def _register_thingy_session(self, thingy: Thingy, session_id: str) -> dict:
        store = self._sessions(thingy)
        record_id = self.viewer.key
        operation_id = "session-" + _digest([record_id, session_id])
        current = store.get("viewers", record_id)
        value = {
            "session_id": session_id,
            "viewer": self.viewer.user_id,
            "open": self._open_records(current),
        }
        if current is None:
            saved = store.create("viewers", record_id, value, operation_id, self.viewer.user_id)
        elif current.value.get("session_id") == session_id:
            saved = current
        else:
            saved = store.replace(
                "viewers", record_id, value, current.version, operation_id, self.viewer.user_id
            )
        return {"session_id": saved.value["session_id"], "version": saved.version}

    @staticmethod
    def _open_records(current: Any) -> list[dict]:
        records = current.value.get("open") if current is not None else None
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, dict)]

    def _save_viewer(
        self, thingy: Thingy, current: Any, open_records: list[dict], operation_id: str
    ) -> None:
        store = self._sessions(thingy)
        value = {
            "session_id": current.value.get("session_id") if current is not None else None,
            "viewer": self.viewer.user_id,
            "open": open_records[-MAX_OPEN_ACTIONS:],
        }
        if current is None:
            store.create("viewers", self.viewer.key, value, operation_id, self.viewer.user_id)
        else:
            store.replace(
                "viewers",
                self.viewer.key,
                value,
                current.version,
                operation_id,
                self.viewer.user_id,
            )

    def _progress(self, thingy: Thingy, open_records: list[dict]) -> dict[str, dict]:
        """Each open action's ledger entry and progress, derived from its sessions' state."""
        ledger = self._ledger(thingy)
        now = int(time.time())
        entries: list[tuple[str, dict, Any]] = []
        documents: dict[str, Any] = {}
        for record in open_records:
            record_id = record.get("id")
            if not isinstance(record_id, str):
                continue
            document = ledger.get("actions", record_id)
            if document is None:
                continue
            documents[record_id] = document
            entries.append((record_id, document.value, delivery_of(record)))
        session_ids = {delivery.session_id for _, _, delivery in entries if delivery is not None}
        for _, value, _ in entries:
            if isinstance(value.get("session_id"), str):
                session_ids.add(value["session_id"])
        states = (
            self.sessions.read(session_ids) if self.sessions is not None and session_ids else {}
        )
        progress = derive_ledger_progress(entries, sessions=states, now=now)
        for record in open_records:
            record_id = record.get("id")
            if not isinstance(record_id, str):
                continue
            undelivered = record_id in progress and progress[record_id].status == "pending"
            if undelivered and now - int(record.get("requested_at") or 0) > UNDELIVERED_TTL_S:
                progress[record_id] = ActionProgress("interrupted", "not_delivered", ())
        return {
            record_id: {"document": documents[record_id], "progress": progress[record_id]}
            for record_id in progress
        }

    def request_action(self, app_id: str, body: dict) -> dict:
        thingy, permissions, revision, _ = self._active(app_id)
        self._require(permissions, "publish")
        name, params = validate_request(
            revision.get("actions", []), body.get("action"), body.get("params")
        )
        operation_id = _operation_id(body.get("operation_id"))
        record_id = _digest([self.viewer.user_id, operation_id])
        digest = _digest([name, params])
        viewer = self._sessions(thingy).get("viewers", self.viewer.key)
        open_records = self._open_records(viewer)
        progress = self._progress(thingy, open_records)
        # Asking again for an action that is still open joins it instead of running it twice.
        # One never delivered (the viewer left the New session it opened) says so, so the
        # web app can hand it to a session again rather than wait for it.
        for record in open_records:
            known = progress.get(str(record.get("id")))
            if (
                known is not None
                and record.get("digest") == digest
                and known["progress"].status in OPEN_STATUSES
                and known["document"].id != record_id
            ):
                return {
                    **known["document"].to_dict(),
                    "merged": True,
                    "delivered": delivery_of(record) is not None,
                }
        value = {
            "action": name,
            "params": params,
            "state": "queued",
            "summary": None,
            "session_id": None,
            "requested_by": self.viewer.user_id,
            "revision_id": revision["id"],
        }
        document = self._ledger(thingy).create(
            "actions", record_id, value, operation_id, self.viewer.user_id
        )
        if not any(record.get("id") == document.id for record in open_records):
            still_open = [
                record
                for record in open_records
                if record.get("id") in progress
                and progress[record["id"]]["progress"].status in OPEN_STATUSES
            ]
            record = {"id": document.id, "digest": digest, "requested_at": int(time.time())}
            self._save_viewer(thingy, viewer, [*still_open, record], "open-" + operation_id)
            delivered = False
        else:
            # A retry of this request: report the delivery its first attempt recorded.
            delivered = any(
                record.get("id") == document.id and delivery_of(record) is not None
                for record in open_records
            )
        return {**document.to_dict(), "merged": False, "delivered": delivered}

    def record_delivery(self, app_id: str, action_id: str, body: dict) -> dict:
        """Records that the web app posted the viewer's action into their thingy session."""
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        action_id = _identifier(action_id, "action ID")
        session_id = _session_id(body.get("session_id"))
        viewer = self._sessions(thingy).get("viewers", self.viewer.key)
        if viewer is None or viewer.value.get("session_id") != session_id:
            raise InvalidInput("Deliver actions only to your registered thingy session")
        open_records = self._open_records(viewer)
        record = next((record for record in open_records if record.get("id") == action_id), None)
        if record is None:
            raise NotFound("That action is not one of your open actions")
        if delivery_of(record) is None:
            delivered = {**record, "session_id": session_id, "delivered_at": int(time.time())}
            open_records = [delivered if item is record else item for item in open_records]
            self._save_viewer(thingy, viewer, open_records, f"deliver-{action_id}")
        return self.action_progress(app_id, [action_id])

    def action_progress(self, app_id: str, ids: Any) -> dict:
        """Progress of actions; the viewer's tracked ones are derived, others keep their state.

        With no IDs, returns the viewer's tracked actions, so a reloaded thingy can show
        them again.
        """
        thingy, _ = self._resolve(app_id)
        if not isinstance(ids, list) or len(ids) > MAX_PROGRESS_IDS:
            raise InvalidInput(f"Ask for at most {MAX_PROGRESS_IDS} action IDs")
        viewer = self._sessions(thingy).get("viewers", self.viewer.key)
        open_records = self._open_records(viewer)
        known = self._progress(thingy, open_records)
        if ids:
            wanted = [_identifier(value, "action ID") for value in ids]
        else:
            wanted = [record["id"] for record in open_records if record.get("id") in known]
        ledger = self._ledger(thingy)
        items: list[dict[str, Any]] = []
        for action_id in dict.fromkeys(wanted):
            entry = known.get(action_id)
            document = entry["document"] if entry else ledger.get("actions", action_id)
            if document is None:
                items.append({"id": action_id, "status": "not_found"})
                continue
            value = document.value
            progress = (
                entry["progress"].to_dict()
                if entry
                else {"status": value.get("state"), "reason": None}
            )
            items.append(
                {
                    "id": action_id,
                    **progress,
                    "action": value.get("action"),
                    "params": value.get("params"),
                    "summary": value.get("summary"),
                    "session_id": value.get("session_id"),
                    # When it was requested, so a reloaded page keeps counting from there.
                    "requested_at": document.created_at,
                    "version": document.version,
                }
            )
        return {"items": items}

    def list_actions(self, app_id: str, limit: Any = 50, cursor: Any = None) -> dict:
        thingy, _ = self._resolve(app_id)
        return self._ledger(thingy).list("actions", limit, cursor).to_dict()

    def update_action(self, app_id: str, body: dict) -> dict:
        thingy, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        record_id = _identifier(body.get("id"), "action ID")
        version = body.get("if_version")
        if not isinstance(version, str) or len(version) > 2048:
            raise InvalidInput("A valid expected version is required")
        store = self._ledger(thingy)
        current = store.get_version("actions", record_id, version)
        if current is None:
            raise NotFound("The action was deleted")
        changes = {
            key: value
            for key, value in body.items()
            if key not in {"id", "if_version", "operation_id"}
        }
        value = validate_update(current.value, changes)
        return store.replace(
            "actions",
            record_id,
            value,
            version,
            _operation_id(body.get("operation_id")),
            self.viewer.user_id,
        ).to_dict()

    # -- the frame's data bridge -----------------------------------------------------

    def rpc(self, app_id: str, revision_id: Any, method: Any, params: Any) -> Any:
        thingy, permissions, revision, publication = self._active(
            app_id, _revision_id(revision_id)
        )
        if not isinstance(params, dict):
            raise InvalidInput("RPC params must be an object")
        reads = {"data.v1.get", "data.v1.list"}
        writes = {"data.v1.create", "data.v1.replace", "data.v1.delete"}
        if method not in reads | writes:
            raise ThingyError("UNSUPPORTED", "This thingy operation is not supported", 400)
        required = "data.write" if method in writes else "data.read"
        if required not in revision["capabilities"] or required not in publication["value"].get(
            "approved_capabilities", []
        ):
            raise ThingyError(
                "PERMISSION_DENIED",
                "The published thingy is not approved for this capability",
                403,
            )
        if method in writes:
            self._require(permissions, "write_data")
        collection = _identifier(params.get("collection"), "collection")
        schema = revision["collections"].get(collection)
        if schema is None:
            raise InvalidInput("Collection is not declared by the published thingy")
        if method in {"data.v1.create", "data.v1.replace"}:
            if "value" not in params:
                raise InvalidInput("Document value is required")
            error = jsonschema.exceptions.best_match(
                jsonschema.Draft202012Validator(schema).iter_errors(params["value"])
            )
            if error is not None:
                raise _schema_mismatch(collection, error)
        files = self._files(thingy)
        store = ImmutableDocumentStore(files, "data", limits=_DATA_STORE_LIMITS)
        rules = revision.get("constraints", [])
        # `_guard` cannot be a collection name, so the guard never shares a directory with records.
        guard = (
            ConstraintGuard(files, "data/_guard", store, rules, self.viewer.user_id)
            if rules
            else None
        )
        if method == "data.v1.list":
            return store.list(collection, params.get("limit", 100), params.get("cursor")).to_dict()
        record_id = params.get("id")
        if method == "data.v1.create" and record_id is None:
            # Stable across retries of one logical operation, unlike a fresh record ID.
            operation_id = _operation_id(params.get("operation_id"))
            record_id = _digest([self.viewer.user_id, operation_id])
        record_id = _identifier(record_id, "record ID")
        if method == "data.v1.get":
            document = store.get(collection, record_id)
            return document.to_dict() if document is not None else None
        operation_id = _operation_id(params.get("operation_id"))
        version = params.get("if_version")
        if method != "data.v1.create" and (not isinstance(version, str) or len(version) > 2048):
            raise InvalidInput("A valid expected version is required")
        if guard is not None and guard.applies(collection):
            return guard.write(
                method.removeprefix("data.v1."),
                collection,
                record_id,
                params.get("value"),
                version if method != "data.v1.create" else None,
                operation_id,
            )
        actor = self.viewer.user_id
        if method == "data.v1.create":
            return store.create(
                collection, record_id, params.get("value"), operation_id, actor
            ).to_dict()
        # Checked above: every write but a create names the version it expects.
        expected = cast(str, version)
        if method == "data.v1.replace":
            return store.replace(
                collection, record_id, params.get("value"), expected, operation_id, actor
            ).to_dict()
        return store.delete(collection, record_id, expected, operation_id, actor).to_dict()
