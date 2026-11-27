"""Gadgets for one viewer: metadata, revisions, records, comments, actions and the shelf.

A :class:`GadgetService` is built per request for the calling user. It checks
every operation against :class:`~omnigent.gadgets.access.GadgetAccess` and
keeps everything a gadget holds in that gadget's
:class:`~omnigent.stores.gadget_store.GadgetFiles`, laid out as::

    code/revisions/<revision>.json   # immutable uploaded revisions
    code/state/publication/current/  # which revision is published (a document)
    data/<collection>/<id>/          # the gadget's records (documents)
    data/_guard/                     # rule state for collections with constraints
    data/_comments/                  # comments
    data/_sessions/                  # each viewer's gadget session and open actions
    data/_actions/                   # the action ledger

Collection names cannot start with ``_``, so the gadget's records never share a
namespace with the others. Each viewer's shelf (pins, and the gadgets shown
beside each of their sessions) lives in their own user files.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import jsonschema

from omnigent.gadgets.access import (
    MAX_SHARED_GADGETS,
    NO_ACCESS,
    GadgetAccess,
    GadgetPermissions,
    owned_by,
)
from omnigent.gadgets.accounts import DeletedAccountGadgets
from omnigent.gadgets.actions import (
    validate_actions,
    validate_request,
    validate_session_id,
    validate_update,
)
from omnigent.gadgets.constraints import ConstraintGuard, validate_constraints, validate_guide
from omnigent.gadgets.documents import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    Document,
    ImmutableDocumentStore,
    InvalidInput,
    NotFound,
    StoreLimits,
)
from omnigent.gadgets.progress import (
    OPEN_STATUSES,
    ActionProgress,
    SessionStateReader,
    delivery_of,
    derive_ledger_progress,
    session_tree_status,
)
from omnigent.stores.gadget_store import (
    MAX_GADGET_NAME_CHARS,
    MAX_OWNED_PAGE_SIZE,
    Gadget,
    GadgetFiles,
    GadgetStore,
    StoreError,
    is_gadget_id,
)

_log = logging.getLogger(__name__)

MAX_HTML_BYTES = 1024 * 1024
MAX_NAMESPACE_ENTRIES = 1024
MAX_COLLECTIONS = 16
MAX_COLLECTION_IDENTITIES = 128
MAX_SCHEMA_BYTES = 16 * 1024
_DATA_STORE_LIMITS = StoreLimits(
    max_records_per_collection=1000, max_list_entries=MAX_NAMESPACE_ENTRIES
)
# The gadget's own bookkeeping (publication, comments, sessions, the action ledger) and
# each viewer's shelf. Records rewritten on every action roll into new revision segments.
_STATE_STORE_LIMITS = StoreLimits()
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{32}$")
# The viewer's pins and session links, in their own files.
SHELF_ROOT = "shelf"
MAX_PINNED_GADGETS = 50
MAX_SESSION_GADGETS = 20
COMMENT_STATUSES = frozenset({"draft", "addressed"})
MAX_COMMENT_CHARS = 4000
_KNOWN_CAPABILITIES = frozenset({"data.read", "data.write", "sessions.read"})
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
# What ``describe`` tells an agent to do next; the ``gadget`` skill has the details.
_NEXT_STEPS = (
    "Load the gadget skill and follow it: it says how to handle 'Gadget action' messages, "
    "read and change records with sys_gadget_records, and change or publish this gadget."
)
_NEXT_STEPS_UNPUBLISHED = (
    "Nothing is published yet. Load the gadget skill and follow it to build this gadget: "
    "upload a page with sys_gadget_revision_create, then publish it with sys_gadget_publish "
    "(if_version null)."
)


class GadgetError(Exception):
    """A refused gadget request with its API error code and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FrontendRevisionChanged(Conflict):
    """The frame asked for a revision that is no longer the published one."""

    def __init__(self, published_revision: str) -> None:
        super().__init__("The published gadget changed; load its new version before continuing")
        self.details = {"reason": "REVISION_CHANGED", "published_revision": published_revision}


@dataclass(frozen=True)
class GadgetViewer:
    """The caller.

    :param user_id: e.g. ``"alice@example.com"``; ``"local"`` in single-user mode.
    :param registration: The account's registration on a server with built-in accounts
        (:meth:`PermissionStore.account_registrations`), else ``None``; the viewer owns
        only the gadgets created under it.
    """

    user_id: str
    registration: str | None = None

    @property
    def principal(self) -> list[str]:
        """Who the viewer is across a gadget's records: the user and their registration, so
        a name registered again is someone else while a password change is not."""
        if self.registration is None:
            return [self.user_id]
        return [self.user_id, self.registration]

    @property
    def key(self) -> str:
        """A record ID that stands for the viewer; user IDs may hold any characters."""
        return hashlib.sha256(canonical_bytes(self.principal)).hexdigest()[:32]

    def wrote(self, by: str | None, registration: Any) -> bool:
        """Whether a record written by *by* under *registration* is the viewer's."""
        return by == self.user_id and registration == self.registration


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInput("Expected a finite JSON value") from exc


def _requested_by(value: Any) -> str | None:
    """Who asked for a ledger entry, or ``None`` for one that doesn't say."""
    requester = value.get("requested_by") if isinstance(value, dict) else None
    return requester if isinstance(requester, str) and requester else None


def _requested_by_viewer(value: Any, viewer: GadgetViewer) -> bool:
    """Whether *viewer* asked for a ledger entry; one that names no requester is anyone's."""
    requester = _requested_by(value)
    if requester is None:
        return True
    return viewer.wrote(requester, value.get("requester_registration"))


def _digest(value: Any, length: int = 32) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:length]


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidInput(f"Invalid {label}")
    return value


def _reached_a_session(action: dict) -> bool:
    """Whether a session took this ledger entry: it left ``queued`` or names its session."""
    return action.get("state") != "queued" or bool(action.get("session_id"))


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


def _gadget_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_GADGET_NAME_CHARS:
        raise InvalidInput(f"Gadget name must contain 1–{MAX_GADGET_NAME_CHARS} characters")
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


def _page_limit(value: Any, most: int) -> int:
    if type(value) is not int or not 1 <= value <= most:
        raise InvalidInput(f"limit must be an integer in 1..{most}")
    return value


# Shelf lists keep their order in one document, so a page cursor is a position in it.
_POSITION_CURSOR = re.compile(r"p([0-9]{1,4})")


def _position_cursor(value: Any) -> int:
    if value is None:
        return 0
    match = _POSITION_CURSOR.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise InvalidInput("Invalid cursor")
    return int(match.group(1))


def _encode_position(position: int) -> str:
    return f"p{position}"


def _epoch(timestamp: str) -> float:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


class GadgetService:
    """One viewer's operations on gadgets (see module docstring)."""

    def __init__(
        self,
        store: GadgetStore,
        access: GadgetAccess,
        viewer: GadgetViewer,
        sessions: SessionStateReader | None = None,
        display_names: Callable[[Sequence[str]], dict[str, str]] | None = None,
    ) -> None:
        """
        :param store: Where gadgets are kept.
        :param access: Decides the viewer's permissions.
        :param viewer: The caller.
        :param sessions: Reads the state of the viewer's sessions for action
            progress; ``None`` reports progress from the ledger alone.
        :param display_names: Names for user IDs, e.g. a shared gadget's owner;
            ``None`` shows the IDs.
        """
        self.store = store
        self.access = access
        self.viewer = viewer
        self.sessions = sessions
        self._display_names = display_names
        self._salts: dict[str, str] = {}

    # -- resolving gadgets and permissions ------------------------------------

    def _resolve(self, app_id: Any) -> tuple[Gadget, GadgetPermissions]:
        if not is_gadget_id(app_id):
            raise NotFound("Gadget not found")
        gadget = self.store.get(app_id)
        if gadget is None:
            raise NotFound("Gadget not found")
        permissions = self.access.permissions(self.viewer.user_id, gadget)
        if not permissions.read:
            raise NotFound("Gadget not found")
        return gadget, permissions

    @staticmethod
    def _require(permissions: GadgetPermissions, permission: str) -> None:
        if not getattr(permissions, permission):
            raise GadgetError(
                "PERMISSION_DENIED", "This operation requires additional gadget permissions", 403
            )

    def _files(self, gadget: Gadget) -> GadgetFiles:
        return self.store.gadget_files(gadget.id)

    def _state(self, gadget: Gadget) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(
            self._files(gadget), "code/state", limits=_STATE_STORE_LIMITS
        )

    def _publication(self, gadget: Gadget) -> dict | None:
        publication = self._state(gadget).get("publication", "current")
        return publication.to_dict() if publication is not None else None

    def _frontend(self, gadget: Gadget, revision_id: Any) -> dict:
        try:
            value = self._files(gadget).read_json(
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

    def _app(self, gadget: Gadget, permissions: GadgetPermissions) -> dict:
        publication = self._publication(gadget)
        return {
            "id": gadget.id,
            "name": gadget.name,
            "browse_url": f"/gadget/{gadget.id}",
            "owner_id": gadget.owner_id,
            "created_at": gadget.created_at,
            "viewer_id": self.viewer.user_id,
            "viewer_principal_id": self.viewer.key,
            "published_revision": publication["value"]["revision_id"] if publication else None,
            "version": publication["version"] if publication else None,
            "permissions": permissions.to_dict(),
        }

    # -- gadgets ---------------------------------------------------------------

    def create(self, name: Any) -> dict:
        gadget = self.store.create(
            self.viewer.user_id, _gadget_name(name), owner_registration=self.viewer.registration
        )
        self._after_owned_write()
        return self._app(gadget, self.access.permissions(self.viewer.user_id, gadget))

    def _after_owned_write(self) -> None:
        """A write for the viewer's registration that lands after that account was deleted
        (it was on its way meanwhile) queues the account's cleanup again."""
        DeletedAccountGadgets.after_registration_write(
            self.store, self.viewer.user_id, self.viewer.registration
        )

    def list_owned(self, limit: Any = 100, cursor: Any = None) -> dict:
        """One page of the viewer's own gadgets, in the store's order.

        :returns: ``{"items", "next_cursor"}``.
        """
        owned, next_cursor = self.store.list_owned_page(
            self.viewer.user_id,
            limit=_page_limit(limit, MAX_OWNED_PAGE_SIZE),
            after=cursor,
            owner_registration=self.viewer.registration,
        )
        # One batched lookup for the page, not one per gadget.
        permissions = self.access.permissions_many(self.viewer.user_id, owned)
        return {
            "items": [self._app(gadget, permissions[gadget.id]) for gadget in owned],
            "next_cursor": next_cursor,
        }

    def get(self, app_id: str) -> dict:
        return self._app(*self._resolve(app_id))

    def shared(self, limit: Any = MAX_SHARED_GADGETS, cursor: Any = None) -> dict:
        """One page of other people's gadgets that reach the viewer through their own grant
        or a group's: ``{"items", "next_cursor"}``."""
        if cursor is not None and not isinstance(cursor, str):
            raise InvalidInput("Invalid cursor")
        page = self.access.shared_with(
            self.viewer.user_id, limit=_page_limit(limit, MAX_SHARED_GADGETS), cursor=cursor
        )
        shared = page.items
        owners = sorted({item.gadget.owner_id for item in shared})
        names = self._display_names(owners) if self._display_names and owners else {}
        items = []
        others = [
            item
            for item in shared
            if not owned_by(item.gadget, self.viewer.user_id, self.viewer.registration)
        ]
        found = self.access.permissions_many(self.viewer.user_id, [item.gadget for item in others])
        for item in others:
            permissions = found[item.gadget.id]
            if not permissions.read:
                continue
            items.append(
                {
                    **self._app(item.gadget, permissions),
                    "shared_via": item.shared_via,
                    "owner_email": names.get(item.gadget.owner_id, item.gadget.owner_id),
                }
            )
        return {"items": items, "next_cursor": page.next_cursor}

    def rename(self, app_id: str, body: dict) -> dict:
        """Renames the gadget for everyone; needs the same access as publishing it."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        name = _gadget_name(body.get("name"))
        if name != gadget.name:
            renamed = self.store.rename(gadget.id, name)
            if renamed is None:
                raise NotFound("Gadget not found")
            gadget = renamed
        return self._app(gadget, permissions)

    @contextlib.contextmanager
    def _lifecycle(self, gadget: Gadget) -> Iterator[None]:
        """Hold the gadget against publishing, adoption and a discard of it unbuilt, in this
        process and others sharing its storage, so a discard checks and deletes at once."""
        with self._files(gadget).exclusive("lifecycle") as held:
            if not held:
                raise Conflict("The gadget is busy; try again", reason="busy")
            yield

    def discard_unbuilt(self, app_id: str) -> dict:
        """Deletes the gadget only while nothing was built from it: never published, and no
        gadget session of the viewer's took it. Checked and deleted under the gadget's
        lifecycle lock, which publishing and adoption also take, so another tab publishing
        or adopting it meanwhile keeps it.

        :returns: ``{"id", "deleted"}``; ``deleted`` false when it was kept.
        """
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "manage")
        with self._lifecycle(gadget):
            viewer = self._sessions(gadget).get("viewers", self.viewer.key)
            adopted = viewer is not None and isinstance(viewer.value.get("session_id"), str)
            if self._publication(gadget) is not None or adopted:
                return {"id": app_id, "deleted": False}
            self.access.forget(gadget.id)
            self.store.delete(gadget.id)
        return {"id": app_id, "deleted": True}

    def delete(self, app_id: str) -> dict:
        """Deletes the gadget for everyone, with its versions, data and comments."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "manage")
        # Grants first: a failed cleanup leaves the gadget there to delete again, rather
        # than grant rows for a gadget that's already gone.
        self.access.forget(gadget.id)
        self.store.delete(gadget.id)
        return {"id": app_id, "deleted": True}

    def gadget_session_status(self, app_id: str) -> dict:
        """Whether the viewer's gadget session, with the sessions it started, is working."""
        gadget, _ = self._resolve(app_id)
        record = self._sessions(gadget).get("viewers", self.viewer.key)
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

    # -- the viewer's shelf: pinned gadgets and the gadgets each session shows -----
    # The viewer's own state, so it lives in their files, not any gadget's.

    @property
    def _shelf(self) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(
            self.store.user_files(
                self.viewer.user_id, owner_registration=self.viewer.registration
            ),
            SHELF_ROOT,
            limits=_STATE_STORE_LIMITS,
        )

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
        else:
            return
        self._after_owned_write()

    def _pins(self) -> tuple[Any, list[dict]]:
        current = self._shelf.get("pins", "all")
        items = current.value.get("items") if current is not None else None
        pins = (
            [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        )
        return current, pins

    def _readable_pins(self, pins: list[dict]) -> dict[str, Gadget]:
        """The pinned gadgets the viewer can still read, keyed by ID.

        A gadget deleted, or no longer shared with them, since they pinned it drops out,
        and so does its name.
        """
        ids = [pin["id"] for pin in pins if isinstance(pin.get("id"), str)]
        found = self.store.get_many(ids)
        permissions = self.access.permissions_many(self.viewer.user_id, list(found.values()))
        return {
            app_id: gadget
            for app_id, gadget in found.items()
            if permissions.get(app_id, NO_ACCESS).read
        }

    def _owns_any(self) -> bool:
        return self.store.owns_any(
            self.viewer.user_id, owner_registration=self.viewer.registration
        )

    def shelf(self, limit: Any = MAX_PINNED_GADGETS, cursor: Any = None) -> dict:
        """One page of the viewer's pinned gadgets, and whether they own any, for the sidebar.

        :returns: ``{"pinned", "has_owned", "next_cursor"}``; only the page's gadgets are
            looked up. A page can be short (pins the viewer can no longer read are left
            out) and still continue.
        """
        _, pins = self._pins()
        start = _position_cursor(cursor)
        page = pins[start : start + _page_limit(limit, MAX_PINNED_GADGETS)]
        readable = self._readable_pins(page)
        ids = [pin["id"] for pin in page if pin.get("id") in readable]
        following = start + len(page)
        return {
            "pinned": [{"id": app_id, "name": readable[app_id].name} for app_id in ids],
            "has_owned": self._owns_any(),
            "next_cursor": _encode_position(following) if following < len(pins) else None,
        }

    def pin(self, app_id: str, body: dict) -> dict:
        """Pins or unpins a gadget the viewer can read, whoever owns it.

        Saving the shelf also drops pins the viewer can no longer read.
        """
        pinned = body.get("pinned")
        if not isinstance(pinned, bool):
            raise InvalidInput("pinned must be true or false")
        current, pins = self._pins()
        readable = self._readable_pins(pins)
        others = [pin for pin in pins if pin.get("id") != app_id and pin.get("id") in readable]
        if pinned:
            gadget, _ = self._resolve(app_id)
            others.append({"id": app_id, "name": gadget.name})
            if len(others) > MAX_PINNED_GADGETS:
                raise CapacityExceeded(f"At most {MAX_PINNED_GADGETS} gadgets can be pinned")
        if len(others) != len(pins) or pinned:
            self._save_shelf("pins", "all", current, {"items": others})
        return self.shelf()

    def session_gadgets(
        self, session_id: str, limit: Any = MAX_SESSION_GADGETS, cursor: Any = None
    ) -> dict:
        """One page of the gadgets shown beside a session, skipping any the viewer can no
        longer read: ``{"session_id", "items", "next_cursor"}``."""
        session_id = _session_id(session_id)
        current = self._shelf.get("sessions", self._session_key(session_id))
        ids = current.value.get("gadgets") if current is not None else None
        shown = ids if isinstance(ids, list) else []
        start = _position_cursor(cursor)
        page = shown[start : start + _page_limit(limit, MAX_SESSION_GADGETS)]
        items = []
        for app_id in page:
            try:
                gadget, permissions = self._resolve(app_id)
            except (NotFound, CorruptData):
                continue
            items.append(self._app(gadget, permissions))
        following = start + len(page)
        return {
            "session_id": session_id,
            "items": items,
            "next_cursor": _encode_position(following) if following < len(shown) else None,
        }

    def _require_session(self, session_id: str) -> None:
        """Refuses a session the viewer cannot read, or one that is gone or archived.

        Without a session reader (tests, or a server without sessions) any well-formed
        ID is taken.
        """
        if self.sessions is not None and not self.sessions.available(session_id):
            raise NotFound("Session not found")

    def link_session(self, app_id: str, session_id: str, body: dict) -> dict:
        """Shows a gadget beside one of the viewer's sessions, or stops showing it there.

        Showing it there also makes that session the viewer's gadget session when they
        may register one, so actions requested from the gadget's own page reach it,
        unless *body* says ``"handles_actions": false`` (a session the gadget only cites).
        Only a session the viewer can read can show it.
        """
        session_id = _session_id(session_id)
        linked = body.get("linked")
        if not isinstance(linked, bool):
            raise InvalidInput("linked must be true or false")
        handles_actions = body.get("handles_actions", True)
        if not isinstance(handles_actions, bool):
            raise InvalidInput("handles_actions must be true or false")
        key = self._session_key(session_id)
        current = self._shelf.get("sessions", key)
        ids = current.value.get("gadgets") if current is not None else None
        shown = [value for value in ids if isinstance(value, str)] if isinstance(ids, list) else []
        if linked:
            gadget, permissions = self._resolve(app_id)
            self._require_session(session_id)
            # Showing it again keeps its place, so the session's tabs do not reorder.
            if app_id not in shown:
                shown.append(app_id)
            if len(shown) > MAX_SESSION_GADGETS:
                raise CapacityExceeded(f"A session can show at most {MAX_SESSION_GADGETS} gadgets")
            # Shown beside a session whose agent can't do the viewer's gadget work (someone
            # else's, or shared with edit access), it only shows the gadget there.
            if permissions.publish and handles_actions and self._takes_actions(session_id):
                self._register_gadget_session(gadget, session_id)
        else:
            shown = [value for value in shown if value != app_id]
            if not shown and current is not None:
                # Nothing left beside the session: removed rather than kept empty, so a
                # live session document always means gadgets shown there.
                self._shelf.delete(
                    "sessions",
                    key,
                    current.version,
                    "sessions-" + _digest([current.version, "removed"]),
                    self.viewer.user_id,
                )
                self._after_owned_write()
                return self.session_gadgets(session_id)
        self._save_shelf("sessions", key, current, {"session_id": session_id, "gadgets": shown})
        return self.session_gadgets(session_id)

    # -- comments ---------------------------------------------------------------------
    # Kept with the gadget's data, so everyone it is shared with sees them without any
    # session or host running. Commenting needs the same access as changing its data.

    def _comments(self, gadget: Gadget) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(
            self._files(gadget), "data/_comments", limits=_STATE_STORE_LIMITS
        )

    def _comment(self, app_id: str, document: Document) -> dict:
        value = document.value
        anchor = value.get("anchor") if isinstance(value.get("anchor"), dict) else {}
        return {
            "id": document.id,
            "gadget_id": app_id,
            "body": value.get("body"),
            "status": value.get("status"),
            "anchor": anchor,
            "version": document.version,
            "created_by": document.created_by,
            "yours": self.viewer.wrote(document.created_by, value.get("author_registration")),
            "created_at": _epoch(document.created_at),
            "updated_at": _epoch(document.updated_at),
        }

    def list_comments(
        self, app_id: str, status: Any = None, limit: Any = 100, cursor: Any = None
    ) -> dict:
        """One page of the gadget's comments in the order they were made, optionally only
        one *status*: ``{"items", "next_cursor", "can_comment"}``.

        The filter applies while paging, so a short or empty page can still continue;
        follow ``next_cursor`` to the end before sorting comments by time.
        """
        if status is not None and (not isinstance(status, str) or status not in COMMENT_STATUSES):
            raise InvalidInput("status must be draft or addressed")
        gadget, permissions = self._resolve(app_id)
        page = self._comments(gadget).list("comments", _page_limit(limit, 200), cursor)
        items = [self._comment(app_id, document) for document in page.items]
        return {
            "items": [item for item in items if status is None or item["status"] == status],
            "next_cursor": page.next_cursor,
            "can_comment": permissions.write_data,
        }

    def create_comment(self, app_id: str, body: dict) -> dict:
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        text = body.get("body")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMENT_CHARS:
            raise InvalidInput(f"A comment needs 1–{MAX_COMMENT_CHARS} characters")
        anchor = body.get("anchor")
        if not isinstance(anchor, dict):
            raise InvalidInput("anchor must describe the commented part of the gadget")
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
        comment_id = _digest([*self.viewer.principal, operation_id])
        value: dict[str, Any] = {"body": text.strip(), "status": "draft", "anchor": clean}
        if self.viewer.registration is not None:
            value["author_registration"] = self.viewer.registration
        document = self._comments(gadget).create(
            "comments",
            comment_id,
            value,
            operation_id,
            self.viewer.user_id,
        )
        return self._comment(app_id, document)

    def update_comment(self, app_id: str, comment_id: str, body: dict) -> dict:
        """Edits a comment's text (its author only) or marks it draft or addressed;
        ``if_body`` makes the change only while the comment still reads that text (the
        replace below is conditional on the version read, so an edit between the two is
        refused too)."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        store = self._comments(gadget)
        current = store.get("comments", _identifier(comment_id, "comment id"))
        if current is None:
            raise NotFound("Comment not found")
        value = dict(current.value)
        expected = body.get("if_body")
        if expected is not None and expected != value.get("body"):
            # Marked addressed for text the session got: refused once it was edited since,
            # so the edit is sent too rather than taken as dealt with.
            raise Conflict("The comment was edited since", reason="concurrent_write")
        if "body" in body:
            text = body["body"]
            if not self.viewer.wrote(current.created_by, value.get("author_registration")):
                raise GadgetError("PERMISSION_DENIED", "Only its author can edit a comment", 403)
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMENT_CHARS:
                raise InvalidInput(f"A comment needs 1–{MAX_COMMENT_CHARS} characters")
            value["body"] = text.strip()
        if "status" in body:
            if not isinstance(body["status"], str) or body["status"] not in COMMENT_STATUSES:
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
        """Its author, or someone who manages the gadget, can delete a comment."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "write_data")
        store = self._comments(gadget)
        current = store.get("comments", _identifier(comment_id, "comment id"))
        if current is None:
            return {"id": comment_id, "deleted": True}
        if not self.viewer.wrote(current.created_by, current.value.get("author_registration")):
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
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        if body.get("base_revision_id") is not None:
            base = self._frontend(gadget, body["base_revision_id"])
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
        files = self._files(gadget)
        files.mkdirs("code/revisions")
        files.create_json(f"code/revisions/{revision_id}.json", value)
        return {"id": revision_id}

    def publish(self, app_id: str, body: dict) -> dict:
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        with self._lifecycle(gadget):
            return self._publish(gadget, permissions, body)

    def _publish(self, gadget: Gadget, permissions: Any, body: dict) -> dict:
        revision = self._frontend(gadget, body.get("revision_id"))
        expected = body.get("if_version")
        if expected is not None and (not isinstance(expected, str) or len(expected) > 2048):
            raise InvalidInput("Invalid publication version")
        store = self._state(gadget)
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
            raise CapacityExceeded("Gadget has reached its lifetime collection limit")
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
        return self._app(gadget, permissions)

    def _active(
        self, app_id: str, requested_revision: str | None = None
    ) -> tuple[Gadget, GadgetPermissions, dict, dict]:
        gadget, permissions = self._resolve(app_id)
        publication = self._publication(gadget)
        if publication is None:
            raise NotFound("This gadget has not been published")
        return (
            gadget,
            permissions,
            self._published(gadget, publication, requested_revision),
            publication,
        )

    def _published(
        self, gadget: Gadget, publication: dict, requested_revision: str | None = None
    ) -> dict:
        """The published revision, checked against its publication."""
        revision_id = publication["value"]["revision_id"]
        if requested_revision is not None and requested_revision != revision_id:
            raise FrontendRevisionChanged(revision_id)
        revision = self._frontend(gadget, revision_id)
        if hashlib.sha256(canonical_bytes(revision)).hexdigest() != publication["value"]["digest"]:
            raise CorruptData("Published frontend content changed outside the gadget service")
        approved = publication["value"].get("approved_capabilities", [])
        if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
            raise CorruptData("Published capabilities are invalid")
        return revision

    def content(self, app_id: str) -> dict:
        gadget, permissions, revision, publication = self._active(app_id)
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
            "viewer": {
                "id": self._gadget_user_id(gadget, self.viewer.user_id),
                "display_name": self._shown_name(),
            },
        }

    # -- who gadget code sees ---------------------------------------------------

    def _viewer_salt(self, gadget: Gadget) -> str:
        """The gadget's secret for the user IDs its code sees, made on first use.

        Kept in the gadget's own bookkeeping, which its code never reads, so an ID can't be
        matched against guessed user IDs.
        """
        cached = self._salts.get(gadget.id)
        if cached is not None:
            return cached
        store = self._sessions(gadget)
        current = store.get("meta", "viewer-salt")
        if current is None:
            try:
                current = store.create(
                    "meta",
                    "viewer-salt",
                    {"salt": uuid.uuid4().hex + uuid.uuid4().hex},
                    "viewer-salt",
                    self.viewer.user_id,
                )
            except Conflict:  # Another request made it first.
                current = store.get("meta", "viewer-salt")
        salt = current.value.get("salt") if current is not None else None
        if not isinstance(salt, str) or not salt:
            raise CorruptData("Invalid gadget viewer secret")
        self._salts[gadget.id] = salt
        return salt

    def _gadget_user_id(self, gadget: Gadget, user_id: str | None) -> str | None:
        """What gadget code sees for *user_id*: stable within the gadget, opaque, and
        different in every gadget, so its code can tell people apart without learning who
        they are."""
        if user_id is None:
            return None
        salt = self._viewer_salt(gadget)
        return "u_" + hashlib.sha256(f"{salt}\0{user_id}".encode()).hexdigest()[:32]

    def _shown_name(self) -> str | None:
        """The viewer's name for gadget code: only a real one, never their login or email."""
        if self._display_names is None:
            return None
        user_id = self.viewer.user_id
        name = self._display_names([user_id]).get(user_id)
        if not isinstance(name, str) or not name.strip() or name == user_id or "@" in name:
            return None
        return name

    def _for_gadget_code(self, gadget: Gadget, result: Any) -> Any:
        """*result* of a data call with each record's authors as gadget code sees them."""
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return {**result, "items": [self._for_gadget_code(gadget, i) for i in result["items"]]}
        if isinstance(result, dict) and "created_by" in result:
            return {
                **result,
                "created_by": self._gadget_user_id(gadget, result.get("created_by")),
                "updated_by": self._gadget_user_id(gadget, result.get("updated_by")),
            }
        return result

    def get_revision(self, app_id: str, revision_id: str | None = None) -> dict:
        if revision_id is None:
            _, _, revision, _ = self._active(app_id)
        else:
            gadget, permissions = self._resolve(app_id)
            publication = self._publication(gadget)
            published = publication["value"]["revision_id"] if publication else None
            # Unpublished revisions are drafts: only those who may publish read them.
            if revision_id != published and not permissions.publish:
                raise NotFound("Revision not found")
            revision = self._frontend(gadget, revision_id)
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
        """What an agent needs before working on a gadget, without its source.

        Also for a gadget with nothing published yet, whose session starts by building
        it; ``next_steps`` says so. The how-to lives in the ``gadget`` framework skill.
        """
        gadget, permissions = self._resolve(app_id)
        publication = self._publication(gadget)
        revision = self._published(gadget, publication) if publication is not None else {}
        app = self._app(gadget, permissions)
        return {
            "gadget": {key: app[key] for key in ("id", "name", "browse_url", "permissions")},
            "published_revision": revision.get("id"),
            "published_by": publication["updated_by"] if publication is not None else None,
            "published_at": publication["updated_at"] if publication is not None else None,
            "guide": revision.get("guide"),
            "collections": revision.get("collections", {}),
            "constraints": revision.get("constraints", []),
            "actions": revision.get("actions", []),
            "next_steps": _NEXT_STEPS_UNPUBLISHED if publication is None else _NEXT_STEPS,
        }

    # -- gadget sessions and the action ledger ---------------------------------------
    # Editing the gadget is what lets a viewer request actions or register their
    # gadget session.

    def _sessions(self, gadget: Gadget) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(
            self._files(gadget), "data/_sessions", limits=_STATE_STORE_LIMITS
        )

    def _ledger(self, gadget: Gadget) -> ImmutableDocumentStore:
        return ImmutableDocumentStore(
            self._files(gadget), "data/_actions", limits=_STATE_STORE_LIMITS
        )

    def get_gadget_session(self, app_id: str) -> dict:
        """The viewer's own gadget session for this gadget, if they registered one."""
        gadget, _ = self._resolve(app_id)
        record = self._sessions(gadget).get("viewers", self.viewer.key)
        if record is None:
            return {"session_id": None, "version": None}
        session_id = record.value.get("session_id")
        if isinstance(session_id, str) and not self._takes_actions(session_id):
            # A deleted or archived session, or one since shared with edit access, no longer
            # takes the gadget's work; the next one started from the gadget replaces it.
            return {"session_id": None, "version": record.version}
        return {"session_id": session_id, "version": record.version}

    def _takes_actions(self, session_id: str) -> bool:
        return self.sessions is None or self.sessions.takes_actions(session_id)

    def set_gadget_session(self, app_id: str, body: dict) -> dict:
        """Registers one of the sessions the viewer can read as their gadget session."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        session_id = _session_id(body.get("session_id"))
        self._require_session(session_id)
        if not self._takes_actions(session_id):
            raise GadgetError(
                "PERMISSION_DENIED",
                "That session can't do this gadget's actions: use one of your own sessions "
                "that isn't shared with edit access",
                403,
            )
        return self._register_gadget_session(gadget, session_id)

    def _register_gadget_session(self, gadget: Gadget, session_id: str) -> dict:
        with self._lifecycle(gadget):
            return self._register_gadget_session_locked(gadget, session_id)

    def _register_gadget_session_locked(self, gadget: Gadget, session_id: str) -> dict:
        store = self._sessions(gadget)
        record_id = self.viewer.key
        current = store.get("viewers", record_id)
        value = {
            "session_id": session_id,
            "viewer": self.viewer.user_id,
            "open": self._open_records(current),
        }
        # Named by the version it replaces and the value it writes: a retry of this change
        # repeats it, and returning to an earlier session later is a change of its own.
        operation_id = "session-" + _digest(
            [record_id, current.version if current is not None else None, value]
        )
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
        self, gadget: Gadget, current: Any, open_records: list[dict], operation_id: str
    ) -> None:
        store = self._sessions(gadget)
        value = {
            "session_id": current.value.get("session_id") if current is not None else None,
            "viewer": self.viewer.user_id,
            "open": open_records,
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

    def _progress(self, gadget: Gadget, open_records: list[dict]) -> dict[str, dict]:
        """Each open action's ledger entry and progress, derived from its sessions' state."""
        ledger = self._ledger(gadget)
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
        gadget, permissions, revision, _ = self._active(app_id)
        self._require(permissions, "publish")
        operation_id = _operation_id(body.get("operation_id"))
        record_id = _digest([*self.viewer.principal, operation_id])
        ledger = self._ledger(gadget)
        # A retry is answered from what it first asked for, before the published revision
        # is consulted: a newer publication (even an HTML-only one) must not turn the
        # identical request into a different one.
        existing = self._requested_action(ledger, record_id, body)
        if existing is None:
            name, params = validate_request(
                revision.get("actions", []), body.get("action"), body.get("params")
            )
        else:
            name, params = existing.value["action"], existing.value["params"]
        digest = _digest([name, params])
        viewer = self._sessions(gadget).get("viewers", self.viewer.key)
        open_records = self._open_records(viewer)
        progress = self._progress(gadget, open_records)
        # Asking again for an action that is still open joins it instead of running it twice.
        # One never delivered (the viewer left the New session it opened) says so, so the
        # web app can hand it to a session again rather than wait for it.
        for record in open_records if existing is None else ():
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
        still_open = [
            record
            for record in open_records
            if record.get("id") in progress
            and progress[record["id"]]["progress"].status in OPEN_STATUSES
        ]

        def refuse_if_full() -> None:
            # Refused before it is followed: the viewer record follows at most this many, and
            # an action it no longer follows could neither be delivered nor tracked.
            if len(still_open) >= MAX_OPEN_ACTIONS:
                raise CapacityExceeded(
                    f"You already have {MAX_OPEN_ACTIONS} actions open on this gadget; "
                    "ask again once one of them has finished",
                    details={"reason": "too_many_open_actions", "limit": MAX_OPEN_ACTIONS},
                )

        if existing is not None:
            document = existing
        else:
            refuse_if_full()
            value = {
                "action": name,
                "params": params,
                "state": "queued",
                "summary": None,
                "session_id": None,
                "requested_by": self.viewer.user_id,
                "revision_id": revision["id"],
            }
            if self.viewer.registration is not None:
                value["requester_registration"] = self.viewer.registration
            document = ledger.create(
                "actions", record_id, value, operation_id, self.viewer.user_id
            )
        tracked = any(record.get("id") == document.id for record in open_records)
        if existing is not None and not tracked and _reached_a_session(existing.value):
            # A retry of a request whose action a session took and that has since finished
            # (finished actions leave the viewer record): the first receipt, sent nowhere again.
            return {**document.to_dict(), "merged": False, "delivered": True}
        if not tracked:
            # A retry whose first try never got followed counts against the limit too, so
            # following it never pushes out another open action.
            refuse_if_full()
            record = {"id": document.id, "digest": digest, "requested_at": int(time.time())}
            # Bounded and stable per request: the caller's operation ID may already use the
            # store's whole length limit.
            self._save_viewer(gadget, viewer, [*still_open, record], f"open-{record_id}")
            delivered = False
        else:
            # A retry of this request: report the delivery its first attempt recorded.
            delivered = any(
                record.get("id") == document.id and delivery_of(record) is not None
                for record in open_records
            )
        return {**document.to_dict(), "merged": False, "delivered": delivered}

    def _requested_action(self, ledger: Any, record_id: str, body: dict) -> Any:
        """The ledger entry this operation ID already requested, if any.

        :raises Conflict: ``operation_reuse`` when it asked for a different action or params.
        """
        existing = ledger.get("actions", record_id)
        if existing is None:
            return None
        params = {} if body.get("params") is None else body.get("params")
        if (existing.value.get("action"), existing.value.get("params")) != (
            body.get("action"),
            params,
        ):
            raise Conflict(
                "operation_id was already used for a different request",
                reason="operation_reuse",
            )
        return existing

    def record_delivery(self, app_id: str, action_id: str, body: dict) -> dict:
        """Records that the web app posted the viewer's action into their gadget session."""
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        action_id = _identifier(action_id, "action ID")
        session_id = _session_id(body.get("session_id"))
        viewer = self._sessions(gadget).get("viewers", self.viewer.key)
        if viewer is None or viewer.value.get("session_id") != session_id:
            raise InvalidInput("Deliver actions only to your registered gadget session")
        open_records = self._open_records(viewer)
        record = next((record for record in open_records if record.get("id") == action_id), None)
        if record is None:
            raise NotFound("That action is not one of your open actions")
        if delivery_of(record) is None:
            # The viewer's delivery count orders deliveries made in the same second.
            sequence = 1 + max(
                (
                    item["delivery_sequence"]
                    for item in open_records
                    if type(item.get("delivery_sequence")) is int
                ),
                default=0,
            )
            delivered = {
                **record,
                "session_id": session_id,
                "delivered_at": int(time.time()),
                "delivery_sequence": sequence,
            }
            open_records = [delivered if item is record else item for item in open_records]
            self._save_viewer(gadget, viewer, open_records, f"deliver-{action_id}")
        return self.action_progress(app_id, [action_id])

    def action_progress(self, app_id: str, ids: Any) -> dict:
        """Progress of actions; the viewer's tracked ones are derived, others keep their state.

        With no IDs, returns the viewer's tracked actions, so a reloaded gadget can show
        them again.
        """
        gadget, _ = self._resolve(app_id)
        if not isinstance(ids, list) or len(ids) > MAX_PROGRESS_IDS:
            raise InvalidInput(f"Ask for at most {MAX_PROGRESS_IDS} action IDs")
        viewer = self._sessions(gadget).get("viewers", self.viewer.key)
        open_records = self._open_records(viewer)
        known = self._progress(gadget, open_records)
        if ids:
            wanted = [_identifier(value, "action ID") for value in ids]
        else:
            wanted = [record["id"] for record in open_records if record.get("id") in known]
        ledger = self._ledger(gadget)
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
        """The gadget's whole ledger, readable by everyone who can read the gadget.

        Each entry says whether the caller requested it (``yours``): only those are the
        caller's to carry out (see :meth:`update_action`).
        """
        gadget, _ = self._resolve(app_id)
        page = self._ledger(gadget).list("actions", limit, cursor).to_dict()
        for item in page["items"]:
            item["yours"] = _requested_by_viewer(item["value"], self.viewer)
        return page

    def update_action(self, app_id: str, body: dict) -> dict:
        gadget, permissions = self._resolve(app_id)
        self._require(permissions, "publish")
        record_id = _identifier(body.get("id"), "action ID")
        version = body.get("if_version")
        if not isinstance(version, str) or len(version) > 2048:
            raise InvalidInput("A valid expected version is required")
        store = self._ledger(gadget)
        current = store.get_version("actions", record_id, version)
        if current is None:
            raise NotFound("The action was deleted")
        # An action runs in its requester's gadget session, with their access: another
        # person's session (even the owner's) may read it but not take it on.
        if not _requested_by_viewer(current.value, self.viewer):
            raise GadgetError(
                "PERMISSION_DENIED",
                "Someone else requested this action; only their gadget session carries it out",
                403,
            )
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
        """A data call from gadget code (or an agent's ``sys_gadget_records``); records'
        authors come back as the gadget-scoped IDs its code sees as the viewer's."""
        gadget, permissions, revision, publication = self._active(
            app_id, _revision_id(revision_id)
        )
        result = self._rpc(gadget, permissions, revision, publication, method, params)
        return self._for_gadget_code(gadget, result)

    def _rpc(
        self,
        gadget: Gadget,
        permissions: GadgetPermissions,
        revision: dict,
        publication: dict,
        method: Any,
        params: Any,
    ) -> Any:
        if not isinstance(params, dict):
            raise InvalidInput("RPC params must be an object")
        reads = {"data.v1.get", "data.v1.list"}
        writes = {"data.v1.create", "data.v1.replace", "data.v1.delete"}
        if method not in reads | writes:
            raise GadgetError("UNSUPPORTED", "This gadget operation is not supported", 400)
        required = "data.write" if method in writes else "data.read"
        if required not in revision["capabilities"] or required not in publication["value"].get(
            "approved_capabilities", []
        ):
            raise GadgetError(
                "PERMISSION_DENIED",
                "The published gadget is not approved for this capability",
                403,
            )
        if method in writes:
            self._require(permissions, "write_data")
        collection = _identifier(params.get("collection"), "collection")
        schema = revision["collections"].get(collection)
        if schema is None:
            raise InvalidInput("Collection is not declared by the published gadget")
        if method in {"data.v1.create", "data.v1.replace"}:
            if "value" not in params:
                raise InvalidInput("Document value is required")
            error = jsonschema.exceptions.best_match(
                jsonschema.Draft202012Validator(schema).iter_errors(params["value"])
            )
            if error is not None:
                raise _schema_mismatch(collection, error)
        files = self._files(gadget)
        store = ImmutableDocumentStore(files, "data", limits=_DATA_STORE_LIMITS)
        rules = revision.get("constraints", [])
        # `_guard` cannot be a collection name, so the guard never shares a directory with records.
        collections = revision["collections"]

        def validate(dependent: str, dependent_id: str, value: Any) -> None:
            error = jsonschema.exceptions.best_match(
                jsonschema.Draft202012Validator(collections.get(dependent, {})).iter_errors(value)
            )
            if error is not None:
                raise InvalidInput(
                    f"Deleting this record would leave {dependent} record {dependent_id} "
                    f"not matching its schema: {error.message[:200]}",
                    details={"collection": dependent, "id": dependent_id},
                )

        def still_published() -> bool:
            publication = self._publication(gadget)
            return (
                publication is not None
                and publication["value"].get("revision_id") == revision["id"]
            )

        guard = (
            ConstraintGuard(
                files,
                "data/_guard",
                store,
                rules,
                self.viewer.user_id,
                validate,
                is_current=still_published,
            )
            if rules
            else None
        )
        if method == "data.v1.list":
            return store.list(collection, params.get("limit", 100), params.get("cursor")).to_dict()
        record_id = params.get("id")
        if method == "data.v1.create" and record_id is None:
            # Stable across retries of one logical operation, unlike a fresh record ID.
            operation_id = _operation_id(params.get("operation_id"))
            record_id = _digest([*self.viewer.principal, operation_id])
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
        # Rules published again later must count this write: the state they would reuse
        # never saw it. Marked before the write, so a write the mark can't follow is refused
        # before anything changes; marked again after it (below) in case rules rebuilt in
        # between.
        stale_marker = guard or ConstraintGuard(files, "data/_guard", store, [], actor)
        # Reserved until the mark after the write, so a rebuild can't commit in between.
        reservation = _digest([collection, record_id, operation_id, actor], 24)
        stale_marker.mark_stale(unguarded_write=True, reserve=reservation)
        if method == "data.v1.create":
            result = store.create(collection, record_id, params.get("value"), operation_id, actor)
        elif method == "data.v1.replace":
            # Checked above: every write but a create names the version it expects.
            result = store.replace(
                collection, record_id, params.get("value"), cast(str, version), operation_id, actor
            )
        else:
            result = store.delete(collection, record_id, cast(str, version), operation_id, actor)
        try:
            stale_marker.mark_stale(unguarded_write=True, release=reservation)
        except StoreError:
            # The write landed and the state was marked before it; its reservation keeps any
            # rebuild from committing until it expires, after which a rebuild reads the write.
            _log.warning("could not mark a gadget's rule state stale after a write", exc_info=True)
        return result.to_dict()
