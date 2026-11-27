"""Gadget guides and cross-record rules, enforced on every write.

A gadget revision may declare a Markdown ``guide`` for agents and ``constraints``
next to its collection schemas. The schemas already check each record; these
rules span records:

* ``max_records``: at most ``max`` live records in a collection, optionally only
  those whose top-level fields equal ``where``.
* ``unique``: no two live records share the values of ``fields``.
* ``reference``: ``field`` names a live record of ``target``. Deleting that
  record is refused (``restrict``), deletes the referencing records too
  (``cascade``), or removes the field from them (``unset``).

The document store only offers per-record compare-and-swap, so writes to the
collections these rules mention are serialized through one guard record per
gadget. The guard holds the derived state the rules need (counts and unique
keys) and, while a change is in flight, the whole change. Every step of a change
is idempotent, so any later writer that finds an unfinished change completes it
first: a writer that stops half-way leaves neither a rule nor the derived state
broken. A rule only refuses writes that make it worse, so data that predates a
rule stays editable.

The guard record rotates every ``EPOCH_WRITES`` writes on a store that can delete; an
epoch two behind the current one is retired (deleted and removed through the document
store's fenced removal), so a gadget keeps at most a few epoch records however many
writes it takes. A store that can't delete gives nothing back for a retired epoch, so
there an epoch lasts until its record nears its revision limit, and the guard's records
times that limit (10,000 times 10 million revisions by default, about two per write)
bounds how many guarded writes a gadget can ever take. Its state, with a change in flight,
must fit in ``MAX_GUARD_BYTES``: each unique key costs its digest plus the holding
record's ID, so how many records a ``unique`` rule allows depends on their IDs and on
the size of the record being written, not only on the collection's record limit. Rules
published over more data than that are live but over capacity: the state keeps no counts
or keys, deletes (with their reference rules) still work, and other writes are refused
until a delete makes room, when the next write recounts.

A ``unique`` value that data from before the rule already repeats keeps every record
holding it, so those records stay editable as long as they keep that value, and the value
is not free for another record until all of them have moved off it.

Each ``reference`` rule keeps an index of the records naming each target, written as part
of every guarded change, so deleting a target reads only the records that name it. A
target's entries are numbered without gaps (``refs/g<generation>/<rule>/<target>/n/...``)
and read by number, so a lookup examines only the entries it reads, never a directory
listing; ``ids/<record>`` says which entry is a record's, so it is added once and a change
moving the record away removes it. An entry whose record no longer names the target stays
(numbers have no gaps) and is skipped: a lookup reads at most ``MAX_STALE_INDEX_ENTRIES``
of those, and one that would read more has the index rebuilt without them.
When the rules change, the index needs rebuilding, or a write fails half-way, the derived
state is rebuilt a page at a time into a new index generation, with its progress saved in
the guard, so each step costs bounded work and a stopped rebuild resumes; generations
older than the one it committed are then removed (never newer ones, which a later rebuild
may be writing).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from omnigent.gadgets.documents import (
    DEFAULT_LIMITS,
    CapacityExceeded,
    Conflict,
    CorruptData,
    Document,
    ImmutableDocumentStore,
    InvalidInput,
    NotFound,
    StoreLimits,
)
from omnigent.stores.gadget_store import GadgetFiles

MAX_GUIDE_BYTES = 16 * 1024
MAX_CONSTRAINTS = 16
MAX_MESSAGE_LENGTH = 300
MAX_WHERE_FIELDS = 4
MAX_UNIQUE_FIELDS = 3
MAX_RECORDS_LIMIT = 10_000
# Records one change may touch, including cascades.
MAX_PLAN_OPERATIONS = 50
# Left-behind reference-index entries one lookup reads before it gives up and has the index
# rebuilt; with the live ones it stops at, a lookup reads a bounded number of entries.
MAX_STALE_INDEX_ENTRIES = 3 * (MAX_PLAN_OPERATIONS + 1)
# Guard state, including an in-flight change, must fit in one store document.
MAX_GUARD_BYTES = 30 * 1024
EPOCH_WRITES = 100
# On a store that can't delete, an epoch is sealed this many revisions before its record's
# revision limit: one pass of the write loop adds a couple of revisions at most before it
# checks again, and a stale mark adds one outside it.
_ROTATION_HEADROOM = 64
# Tries at taking the guard; between lost races a writer waits a random, growing time, so a
# burst of writers spreads out instead of colliding again (under 5 s in total).
GUARD_ATTEMPTS = 16
GUARD_BACKOFF_S = 0.01
GUARD_BACKOFF_MAX_S = 0.4
_ON_DELETE = ("restrict", "cascade", "unset")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EPOCH = re.compile(r"^e[0-9]{6,12}$")


class _Absent:
    """No record: what a create replaces and a delete leaves. Distinct from a record whose
    JSON value is ``null``, which is live."""

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT: Any = _Absent()


def _json_key(value: Any) -> Any:
    """*value* with equal JSON numbers spelled alike: ``1`` and ``1.0`` and ``-0.0`` and
    ``0`` are one number to JSON, so to rules. Booleans stay booleans."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number) if number.is_integer() else number
    if isinstance(value, list):
        return [_json_key(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_key(item) for key, item in value.items()}
    return value


def _json_equal(left: Any, right: Any) -> bool:
    """Whether two JSON values are equal as JSON: numbers by value, never a boolean as one."""
    return _canonical(_json_key(left)) == _canonical(_json_key(right))


UNGUARDED_HOLD_S = 60
"""How long an unguarded write's reservation holds rebuilds back (see ``mark_stale``)."""


def _live_reservations(state: dict, now: float) -> dict[str, int]:
    """The unguarded writes' reservations younger than :data:`UNGUARDED_HOLD_S`."""
    held = state.get("unguarded")
    if not isinstance(held, dict):
        return {}
    return {
        token: at
        for token, at in held.items()
        if isinstance(token, str) and type(at) is int and now - at < UNGUARDED_HOLD_S
    }


def _epoch_name(number: int) -> str:
    return f"e{number:06d}"


_GENERATION = re.compile(r"^g([0-9]{1,12})$")
_GUARD_COLLECTION = "epochs"
_STALE = "stale"
OVER_CAPACITY_MESSAGE = (
    "This collection has more records under its rules than a gadget can track. Delete "
    "some records, or publish a version without the unique or count rule."
)
_INITIAL_STATE = {"rules": _STALE, "counts": {}, "unique": {}, "pending": None, "writes": 0}
# Containers the guard's state wraps around a record value (the state, its pending operations,
# one operation, its fields), and room for the bookkeeping beside it.
_GUARD_DEPTH_HEADROOM = 8
# The guard's own records; tests shrink it. Deeper and larger than a record, since a pending
# operation holds a record the public limits accept whole.
GUARD_LIMITS: StoreLimits = dataclasses.replace(
    DEFAULT_LIMITS,
    max_json_depth=DEFAULT_LIMITS.max_json_depth + _GUARD_DEPTH_HEADROOM,
    max_json_nodes=DEFAULT_LIMITS.max_json_nodes * 4,
)
# Records one rebuild step reads, and the steps one write may take before giving up as busy.
REBUILD_PAGE = 200
REBUILD_STEPS_PER_WRITE = 64
# Bumped when the derived state gains something a rebuild must fill in (the reference index,
# its generations, every holder of a repeated unique value), or its keys change spelling
# (equal JSON numbers keyed alike).
_STATE_FORMAT = 5
# Entries per directory of a target's reference index.
_INDEX_PER_SEGMENT = 1000
_INDEX_APPEND_ATTEMPTS = 16
# Retired epochs whose marker a stopped writer left out, marked per call.
_FLOOR_REPAIRS_PER_CALL = 4


class _IndexNeedsRebuild(Exception):
    """A reference lookup found more left-behind entries than one request may read."""


class ConstraintViolation(Conflict):
    """A write would break one of the gadget's rules."""

    def __init__(self, rule: dict) -> None:
        super().__init__(rule["message"], reason="constraint_violation")
        self.rule = rule
        self.details = {
            "reason": "CONSTRAINT_VIOLATION",
            "constraint": rule["name"],
            "kind": rule["kind"],
            "collection": rule["collection"],
        }


class IncompleteChange(Conflict):
    """A change was saved, but not yet every record its rules update; the next write retries."""

    def __init__(self) -> None:
        super().__init__(
            "The change was saved, but the records that depend on it are not all updated yet. "
            "They are updated with the next change; try again to finish now.",
            reason="incomplete_change",
        )


def _back_off(attempt: int) -> None:
    """Wait after the *attempt*-th lost race for the guard, with full jitter."""
    time.sleep(random.uniform(0, min(GUARD_BACKOFF_MAX_S, GUARD_BACKOFF_S * 2**attempt)))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value: Any, length: int = 64) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()[:length]


def _generation_number(name: str) -> int | None:
    """The number in a reference index generation's directory name, ``g<number>``."""
    match = _GENERATION.fullmatch(name)
    return int(match.group(1)) if match else None


def validate_guide(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode()) > MAX_GUIDE_BYTES:
        raise InvalidInput(
            f"The guide must be Markdown text of at most {MAX_GUIDE_BYTES // 1024} KiB"
        )
    return value if value.strip() else None


def _scalar(value: Any) -> bool:
    if isinstance(value, bool | str):
        return True
    if isinstance(value, int):
        return abs(value) <= 2**53 - 1
    return isinstance(value, float) and math.isfinite(value)


def validate_constraints(value: Any, collections: dict) -> list[dict]:
    """Normalizes declared rules; every name and field must exist in the revision."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CONSTRAINTS:
        raise InvalidInput(f"Constraints must be a list of at most {MAX_CONSTRAINTS} rules")
    rules: list[dict] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise InvalidInput("Each constraint must be an object")
        name, kind, collection = item.get("name"), item.get("kind"), item.get("collection")
        if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None or name in names:
            raise InvalidInput("Each constraint needs a unique name like wip_limit")
        names.add(name)
        if not isinstance(collection, str) or collection not in collections:
            raise InvalidInput(
                f"Constraint {name} names a collection the revision does not declare"
            )
        schema = collections[collection]
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}

        def declared(field_name: Any, name: str = name, properties: dict = properties) -> str:
            if not isinstance(field_name, str) or field_name not in properties:
                raise InvalidInput(
                    f"Constraint {name} uses a field its collection schema does not declare"
                )
            return field_name

        message = item.get("message")
        if message is not None and (
            not isinstance(message, str) or not 0 < len(message) <= MAX_MESSAGE_LENGTH
        ):
            raise InvalidInput(
                f"Constraint {name} needs a message of at most {MAX_MESSAGE_LENGTH} characters"
            )
        rule: dict[str, Any] = {"name": name, "kind": kind, "collection": collection}
        if kind == "max_records":
            allowed = {"name", "kind", "collection", "max", "where", "message"}
            limit = item.get("max")
            if type(limit) is not int or not 0 <= limit <= MAX_RECORDS_LIMIT:
                raise InvalidInput(f"Constraint {name} needs a max from 0 to {MAX_RECORDS_LIMIT}")
            where = item.get("where", {})
            if not isinstance(where, dict) or len(where) > MAX_WHERE_FIELDS:
                raise InvalidInput(
                    f"Constraint {name} can match at most {MAX_WHERE_FIELDS} fields"
                )
            for key, expected in where.items():
                declared(key)
                if not _scalar(expected):
                    raise InvalidInput(
                        f"Constraint {name} matches fields only against strings, numbers "
                        "or booleans"
                    )
            rule.update(max=limit, where=dict(sorted(where.items())))
            matching = ", ".join(
                f"{key} = {json.dumps(expected)}" for key, expected in rule["where"].items()
            )
            default = f"{collection} holds at most {limit} records" + (
                f" with {matching}" if matching else ""
            )
        elif kind == "unique":
            allowed = {"name", "kind", "collection", "fields", "message"}
            fields = item.get("fields")
            if (
                not isinstance(fields, list)
                or not 0 < len(fields) <= MAX_UNIQUE_FIELDS
                or not all(isinstance(field_name, str) for field_name in fields)
                or len(set(fields)) != len(fields)
            ):
                raise InvalidInput(
                    f"Constraint {name} needs 1 to {MAX_UNIQUE_FIELDS} distinct fields"
                )
            rule["fields"] = [declared(field_name) for field_name in fields]
            default = (
                f"Another {collection} record already has the same {', '.join(rule['fields'])}"
            )
        elif kind == "reference":
            allowed = {"name", "kind", "collection", "field", "target", "on_delete", "message"}
            reference_field = declared(item.get("field"))
            target = item.get("target")
            if not isinstance(target, str) or target not in collections:
                raise InvalidInput(
                    f"Constraint {name} references a collection the revision does not declare"
                )
            on_delete = item.get("on_delete", "restrict")
            if not isinstance(on_delete, str) or on_delete not in _ON_DELETE:
                raise InvalidInput(
                    f"Constraint {name} on_delete must be one of {', '.join(_ON_DELETE)}"
                )
            if on_delete == "unset" and reference_field in schema.get("required", []):
                raise InvalidInput(f"Constraint {name} cannot unset a required field")
            rule.update(field=reference_field, target=target, on_delete=on_delete)
            default = f"{collection}.{reference_field} must name an existing {target} record"
        else:
            raise InvalidInput(f"Constraint {name} kind must be max_records, unique or reference")
        if set(item) - allowed:
            raise InvalidInput(f"Constraint {name} has unsupported keys")
        rule["message"] = message or default
        rules.append(rule)
    return rules


def _canonical_size(value: Any) -> int:
    return len(_canonical(value))


def _field(value: Any, name: str) -> Any:
    """*name* of a record value, or ``None``: a value that isn't an object has no fields."""
    return value.get(name) if isinstance(value, dict) else None


def _holders(entry: Any) -> list[str]:
    """The records holding a unique value: one ID, or a list when earlier data repeats it."""
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, list):
        return [holder for holder in entry if isinstance(holder, str)]
    return []


def _set_holders(keys: dict, key: str, holders: list[str]) -> None:
    if not holders:
        keys.pop(key, None)
    else:
        keys[key] = holders[0] if len(holders) == 1 else holders


@dataclass
class _Plan:
    """The derived state after a change, and the record writes that make up the change."""

    counts: dict[str, int]
    unique: dict[str, dict[str, str]]
    operations: list[dict] = field(default_factory=list)


class ConstraintGuard:
    """Serializes writes to the collections a gadget's rules mention (see module docstring)."""

    def __init__(
        self,
        files: GadgetFiles,
        guard_root: str,
        data: ImmutableDocumentStore,
        rules: list[dict],
        actor: str,
        validate: Callable[[str, str, Any], None] | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> None:
        """
        :param is_current: Whether these rules are still the published ones, read again
            when the state belongs to other rules: only the published rules' writers take
            the state over from them (rebuilding it); a writer whose rules were replaced
            since its request was checked is refused instead. ``None``: always current.
        """
        self.is_current = is_current or (lambda: True)
        self.files = files
        # Raises InvalidInput when a value a rule would write breaks its collection's schema.
        self.validate = validate
        self.guard_root = guard_root
        self.guard = ImmutableDocumentStore(files, guard_root, limits=GUARD_LIMITS)
        self.data = data
        self.rules = rules
        self.actor = actor
        self.digest = _digest({"rules": rules, "format": _STATE_FORMAT})
        self.collections = {rule["collection"] for rule in rules} | {
            rule["target"] for rule in rules if rule["kind"] == "reference"
        }

    def applies(self, collection: str) -> bool:
        return collection in self.collections

    # -- public write path ---------------------------------------------------

    def write(
        self,
        method: str,
        collection: str,
        record_id: str,
        value: Any,
        if_version: str | None,
        operation_id: str,
    ) -> dict:
        lost = 0
        attempts = 0
        rebuild_steps = 0
        recounted = False
        while attempts < GUARD_ATTEMPTS:
            epoch, state = self._current()
            current = state.value
            if current.get("sealed"):
                attempts += 1
                self._advance(epoch, current)
                continue
            if current.get("pending") is not None:
                attempts += 1
                self._finish(epoch, state)
                continue
            progress = current.get("rebuild")
            foreign = (isinstance(progress, dict) and progress.get("digest") != self.digest) or (
                current.get("rules") not in (self.digest, _STALE)
            )
            if foreign and not self.is_current():
                # Rules published after this request was checked are being rebuilt: the live
                # counts and keys are cleared meanwhile, and a write now would also land behind
                # pages the rebuild already read. Refused before anything changes; a retry runs
                # under the published rules.
                raise Conflict(
                    "The gadget's rules changed while this request was on its way; try again",
                    reason="busy",
                )
            if current.get("rules") != self.digest:
                # Each step reads one page; a write helps finish the rebuild before its own.
                rebuild_steps += 1
                if rebuild_steps > REBUILD_STEPS_PER_WRITE:
                    break
                self._rebuild_step(epoch, state)
                continue
            full = current.get("over_capacity")
            if isinstance(full, dict) and method != "delete":
                # Only a delete can bring the derived state back within its limit: recount
                # once if one landed since it went over, otherwise refuse straight away.
                if recounted or not full.get("deletes"):
                    raise CapacityExceeded(
                        OVER_CAPACITY_MESSAGE, details={"reason": "rule_state_full"}
                    )
                recounted = True
                self.mark_stale()
                continue
            attempts += 1
            if self._rotation_due(epoch, current):
                self._seal(epoch, state)
                continue
            primary = {
                "kind": method,
                "collection": collection,
                "id": record_id,
                "if_version": if_version,
                "operation_id": operation_id,
                "actor": self.actor,
            }
            if method != "delete":
                primary["value"] = value
            # A retry the store already answered (even for a record deleted since) or a write
            # that can't land changes no record, so it must not reach the derived state; the
            # store answers it without writing.
            answered = self._answer(primary)
            if answered is not None:
                return answered
            try:
                plan = self._plan(primary, current)
            except _IndexNeedsRebuild:
                # Rebuilding writes a new index generation without the entries left behind;
                # this write then helps with it like any other.
                self.mark_stale()
                continue
            if plan is None:
                # The record changed between the store's answer and the plan's read: ask
                # again rather than let a write the plan didn't count reach the store.
                lost += 1
                _back_off(lost)
                continue
            pending = {
                **current,
                "counts": plan.counts,
                "unique": plan.unique,
                "pending": plan.operations,
                "writes": current.get("writes", 0) + 1,
            }
            if isinstance(full, dict):
                # Over its limit, the state keeps no counts or keys, only how many deletes
                # might have made room since.
                pending.update(
                    counts={},
                    unique={},
                    over_capacity={"deletes": int(full.get("deletes", 0)) + 1},
                )
            if len(_canonical(pending)) > MAX_GUARD_BYTES:
                raise CapacityExceeded("This change is too large for a collection with rules")
            try:
                started = self.guard.replace(
                    _GUARD_COLLECTION,
                    epoch,
                    pending,
                    state.version,
                    self._guard_operation(state, "start", primary),
                    self.actor,
                )
            except Conflict:
                lost += 1
                _back_off(lost)
                continue
            return self._run(epoch, started)
        raise Conflict(
            "Other changes to this gadget kept it busy; try again in a moment", reason="busy"
        )

    # -- planning -------------------------------------------------------------

    def _plan(self, primary: dict, state: dict) -> _Plan | None:
        collection, record_id = primary["collection"], primary["id"]
        existing = self.data.get(collection, record_id)
        if primary["kind"] == "create":
            if existing is not None:
                return None
            before = _ABSENT
        else:
            if existing is None or existing.version != primary["if_version"]:
                return None
            before = existing.value
        after = primary.get("value") if primary["kind"] != "delete" else _ABSENT
        generation = int(state.get("ref_generation", 0))
        plan = _Plan(
            counts=dict(state.get("counts", {})),
            unique={name: dict(keys) for name, keys in state.get("unique", {}).items()},
            operations=[primary],
        )
        # The records this plan deletes or updates, and their planned operation and value.
        gone: set[tuple[str, str]] = {(collection, record_id)} if after is _ABSENT else set()
        planned: dict[tuple[str, str], tuple[dict, Any]] = {}
        queue: list[tuple[str, str, Any, Any]] = [(collection, record_id, before, after)]
        changes: list[tuple[str, str, Any, Any]] = []
        while queue:
            changed_collection, changed_id, old, new = queue.pop(0)
            changes.append((changed_collection, changed_id, old, new))
            for rule in self.rules:
                if rule["collection"] == changed_collection:
                    self._check(rule, plan, changed_id, old, new)
            if old is _ABSENT or new is not _ABSENT:
                continue  # Only a record that was there and is deleted meets inbound rules.
            # Every reference rule on the deleted record, gathered per dependent first, so
            # a dependent referencing it through several fields meets all of their rules.
            effects: dict[tuple[str, str], tuple[Document, list[dict]]] = {}
            for rule in self.rules:
                if rule["kind"] != "reference" or rule["target"] != changed_collection:
                    continue
                for dependent in self._dependents(rule, changed_id, generation):
                    key = (rule["collection"], dependent.id)
                    effects.setdefault(key, (dependent, []))[1].append(rule)
            for key, (dependent, rules) in effects.items():
                if key in gone:
                    continue
                restricting = [rule for rule in rules if rule["on_delete"] == "restrict"]
                if restricting:
                    raise ConstraintViolation(restricting[0])
                operation: dict[str, Any] | None
                operation, current = planned.get(key, (None, dependent.value))
                if operation is None:
                    if (
                        self.data.revision_count(key[0], dependent.id)
                        >= self.data.limits.max_revisions_per_record
                    ):
                        # Its update could never be written, and the change would wait on it
                        # for good: refused before anything is written.
                        raise CapacityExceeded(
                            f"{key[0]} record {dependent.id} has no room for the update this "
                            "change makes to it"
                        )
                    operation = {
                        "collection": key[0],
                        "id": dependent.id,
                        "if_version": dependent.version,
                        "operation_id": self._derived_operation(primary, len(plan.operations)),
                        "actor": self.actor,
                    }
                    plan.operations.append(operation)
                # Which deleted record each field named: a repeat after the dependent changed
                # updates it only while it still names that record.
                targets = {
                    **operation.get("targets", {}),
                    **{r["field"]: changed_id for r in rules},
                }
                operation["targets"] = targets
                # And which collection: two rules on one field can target different ones.
                operation["target_collections"] = {
                    **operation.get("target_collections", {}),
                    **{r["field"]: changed_collection for r in rules},
                }
                if any(rule["on_delete"] == "cascade" for rule in rules):
                    operation["kind"] = "delete"
                    operation.pop("fields", None)
                    gone.add(key)
                    planned[key] = (operation, None)
                    queue.append((key[0], dependent.id, current, _ABSENT))
                else:
                    fields = sorted({*operation.get("fields", []), *(r["field"] for r in rules)})
                    operation["kind"], operation["fields"] = "unset", fields
                    remaining = {k: v for k, v in current.items() if k not in fields}
                    if self.validate is not None:
                        # The record left behind must still match its schema, checked before
                        # anything is written.
                        self.validate(key[0], dependent.id, remaining)
                    planned[key] = (operation, remaining)
                    queue.append((key[0], dependent.id, current, remaining))
                if len(plan.operations) > MAX_PLAN_OPERATIONS:
                    raise CapacityExceeded(
                        f"This change would update more than {MAX_PLAN_OPERATIONS} records"
                    )
        plan.operations.extend(self._index_operations(changes, generation))
        return plan

    def _index_operations(
        self, changes: list[tuple[str, str, Any, Any]], generation: int
    ) -> list[dict]:
        """The reference index entries a change adds and drops, after its record writes."""
        operations: list[dict] = []
        for changed_collection, changed_id, old, new in changes:
            for rule in self.rules:
                if rule["kind"] != "reference" or rule["collection"] != changed_collection:
                    continue
                before = self._reference_of(rule, old)
                after = self._reference_of(rule, new)
                if before == after:
                    continue
                for kind, target in (("ref_remove", before), ("ref_add", after)):
                    if target is not None:
                        operation = {
                            "kind": kind,
                            "directory": self._index_directory(rule, generation, target),
                            "id": changed_id,
                        }
                        if kind == "ref_remove":
                            # What a remover checks before deleting, so it never deletes
                            # the entry of a record that names the target again.
                            operation.update(
                                collection=changed_collection,
                                id=changed_id,
                                field=rule["field"],
                                target=target,
                            )
                        operations.append(operation)
        return operations

    @staticmethod
    def _reference_of(rule: dict, value: Any) -> str | None:
        target = _field(value, rule["field"])
        return target if isinstance(target, str) and _IDENTIFIER.fullmatch(target) else None

    def _index_root(self) -> str:
        return f"{self.guard_root}/refs"

    def _index_directory(self, rule: dict, generation: int, target_id: str) -> str:
        return f"{self._index_root()}/g{generation}/{_digest(rule, 16)}/{target_id}"

    @staticmethod
    def _entry_path(directory: str, number: int) -> str:
        segment = (number - 1) // _INDEX_PER_SEGMENT + 1
        return f"{directory}/n/s{segment:08d}/{number:08d}"

    def _entry(self, directory: str, number: int) -> str | None:
        """The record ID entry *number* names, or ``None`` past the last entry."""
        try:
            raw = self.files.read_json(self._entry_path(directory, number))
        except NotFound:
            return None
        record_id = raw.get("id")
        if not isinstance(record_id, str):
            raise CorruptData("Invalid reference index entry")
        return record_id

    def _entries_end(self, directory: str) -> int:
        """How many entries *directory* has: numbered without gaps, so the last is found by
        reading entries 1, 2, 4, ... and bisecting, about ``2 * log2(entries)`` reads."""
        if self._entry(directory, 1) is None:
            return 0
        low, high = 1, 2
        while self._entry(directory, high) is not None:
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if self._entry(directory, middle) is not None:
                low = middle
            else:
                high = middle
        return low

    def _index_add(self, directory: str, record_id: str) -> None:
        """Index *record_id* under *directory*'s target, once.

        The entry goes after the last; a writer that loses that number takes the next. Its
        ``ids`` marker, written after, says the record is indexed; one stopped before writing
        it only leaves a second entry for the record, which a lookup reads as one.
        """
        marker = f"{directory}/ids/{record_id}"
        try:
            number = self.files.read_json(marker).get("n")
        except NotFound:
            number = None
        if type(number) is int and self._entry(directory, number) == record_id:
            return
        end = self._entries_end(directory)
        for _attempt in range(_INDEX_APPEND_ATTEMPTS):
            path = self._entry_path(directory, end + 1)
            try:
                self.files.mkdirs(path.rsplit("/", 1)[0])
                self.files.create_json(path, {"id": record_id})
                break
            except Conflict:
                end = max(end + 1, self._entries_end(directory))
        else:
            raise Conflict("The gadget's rules are busy; try again shortly", reason="busy")
        self.files.mkdirs(f"{directory}/ids")
        if self.files.supports_delete:
            with contextlib.suppress(NotFound):
                self.files.delete_tree(marker)
        with contextlib.suppress(Conflict):
            self.files.create_json(marker, {"n": end + 1})

    def _check(self, rule: dict, plan: _Plan, record_id: str, old: Any, new: Any) -> None:
        kind = rule["kind"]
        if kind == "max_records":
            delta = int(self._matches(rule, new)) - int(self._matches(rule, old))
            count = plan.counts.get(rule["name"], 0) + delta
            if delta > 0 and count > rule["max"]:
                raise ConstraintViolation(rule)
            plan.counts[rule["name"]] = max(count, 0)
        elif kind == "unique":
            keys = plan.unique.setdefault(rule["name"], {})
            old_key, new_key = self._unique_key(rule, old), self._unique_key(rule, new)
            # Keeping its value: allowed even for a value earlier data already repeats, and
            # even when the state over capacity keeps no holders to say so.
            if new_key is not None and old_key == new_key:
                return
            if old_key is not None:
                _set_holders(
                    keys, old_key, [h for h in _holders(keys.get(old_key)) if h != record_id]
                )
            if new_key is not None:
                holders = _holders(keys.get(new_key))
                if any(holder != record_id for holder in holders):
                    raise ConstraintViolation(rule)
                _set_holders(keys, new_key, [record_id])
        elif kind == "reference" and isinstance(new, dict):
            target_id = new.get(rule["field"])
            if target_id is None or (
                isinstance(old, dict) and old.get(rule["field"]) == target_id
            ):
                return
            if (
                not isinstance(target_id, str)
                or _IDENTIFIER.fullmatch(target_id) is None
                or self.data.get(rule["target"], target_id) is None
            ):
                raise ConstraintViolation(rule)

    @staticmethod
    def _matches(rule: dict, value: Any) -> bool:
        if value is _ABSENT:
            return False
        if not rule["where"]:
            return True  # Every live record counts, whatever its value, null included.
        return isinstance(value, dict) and all(
            key in value and _json_equal(value[key], expected)
            for key, expected in rule["where"].items()
        )

    @staticmethod
    def _unique_key(rule: dict, value: Any) -> str | None:
        if not isinstance(value, dict) or any(value.get(name) is None for name in rule["fields"]):
            return None
        return _digest([_json_key(value[name]) for name in rule["fields"]], 24)

    def _dependents(self, rule: dict, target_id: str, generation: int) -> list[Document]:
        """The live records whose *rule* field names *target_id*, read from the rule's index.

        Reads entries by number, so it examines only the entries it reads. Stops after one
        more than a change may touch, which is enough to refuse or cap the change. Entries
        whose record no longer names the target (or that repeat a record) are skipped; a
        lookup that meets more than ``MAX_STALE_INDEX_ENTRIES`` of those raises
        ``_IndexNeedsRebuild`` rather than guess from part of the index.
        """
        directory = self._index_directory(rule, generation, target_id)
        found: list[Document] = []
        seen: set[str] = set()
        stale = 0
        number = 0
        while len(found) <= MAX_PLAN_OPERATIONS:
            number += 1
            record_id = self._entry(directory, number)
            if record_id is None:
                return found
            record = (
                self.data.get(rule["collection"], record_id)
                if record_id not in seen and _IDENTIFIER.fullmatch(record_id)
                else None
            )
            if record is not None and _field(record.value, rule["field"]) == target_id:
                seen.add(record_id)
                found.append(record)
                continue
            stale += 1
            if stale > MAX_STALE_INDEX_ENTRIES:
                raise _IndexNeedsRebuild()
        return found

    # -- applying ---------------------------------------------------------------

    def _answer(self, primary: dict) -> dict | None:
        """The store's answer to *primary* when it changes nothing; ``None`` when it would."""
        result = self.data.answer(
            primary["kind"],
            primary["collection"],
            primary["id"],
            primary.get("value"),
            primary.get("if_version"),
            primary["operation_id"],
            primary["actor"],
        )
        return None if result is None else result.to_dict()

    def _apply(self, operation: dict) -> dict:
        if operation["kind"] == "ref_add":
            if "directory" in operation:
                self._index_add(operation["directory"], operation["id"])
            # A plan saved before the index was numbered writes into a generation no lookup
            # reads (the state format changed), so it needs nothing.
            return {}
        if operation["kind"] == "ref_remove":
            if "directory" in operation:
                self._drop_entry(
                    operation["directory"],
                    operation["collection"],
                    operation["id"],
                    operation["field"],
                    operation["target"],
                )
            return {}
        kind, collection, record_id = operation["kind"], operation["collection"], operation["id"]
        actor, operation_id = operation["actor"], operation["operation_id"]
        if kind == "create":
            return self.data.create(
                collection, record_id, operation["value"], operation_id, actor
            ).to_dict()
        if kind == "replace":
            return self.data.replace(
                collection,
                record_id,
                operation["value"],
                operation["if_version"],
                operation_id,
                actor,
            ).to_dict()
        # A rule's update of a dependent (a cascade delete or an unset) whose dependent is
        # gone, or whose planned version was reclaimed, is settled against the record as it
        # is now: a removed dependent needs nothing more.
        if kind == "delete":
            try:
                return self.data.delete(
                    collection, record_id, operation["if_version"], operation_id, actor
                ).to_dict()
            except Conflict:
                return self._apply_again(operation)
            except NotFound:
                if not operation.get("targets"):
                    raise
                return self._apply_again(operation)
        # Derived from the immutable version the plan saw, so a repeat sends the same request.
        try:
            source = self.data.get_version(collection, record_id, operation["if_version"])
        except (InvalidInput, NotFound):
            return self._apply_again(operation)
        if source is None:
            return self._apply_again(operation)
        # Older pending plans named one field.
        fields = set(operation.get("fields") or [operation["field"]])
        if not isinstance(source.value, dict):
            # Not an object, so it names nothing: whatever the plan saw is no longer there.
            return self._apply_again(operation)
        value = {key: item for key, item in source.value.items() if key not in fields}
        try:
            return self.data.replace(
                collection, record_id, value, operation["if_version"], operation_id, actor
            ).to_dict()
        except (Conflict, NotFound):
            return self._apply_again(operation)

    def _drop_entry(
        self, directory: str, collection: str, record_id: str, field: str, target_id: str
    ) -> None:
        """Forget that *record_id* is indexed under *directory*'s target, unless its record
        names *target_id* again.

        Only the ``ids`` marker goes: entries are numbered without gaps, so the record's
        entry stays and lookups skip it. Checked and removed under the record's lock, which
        every write of the record holds: a record moved away and back is indexed again after
        its write, so a remover that saw it away, or that replays an old plan, never removes
        the marker it needs now. A store that cannot delete, or a lock not taken, keeps it.
        """
        if not self.files.supports_delete:
            return
        with self.data.record_lock(collection, record_id) as held:
            if not held:
                return
            current = self.data.get(collection, record_id)
            if current is not None and _field(current.value, field) == target_id:
                return
            with contextlib.suppress(NotFound):
                self.files.delete_tree(f"{directory}/ids/{record_id}")

    def _target_exists(
        self, collection: str, field: str, target_id: Any, target_collection: str | None = None
    ) -> bool:
        """Whether the record *field* of *collection* names is live: in *target_collection*,
        the one the plan's deleted record was in, or (a plan saved without it) any
        collection a reference rule on the field targets."""
        if target_collection is not None:
            if not isinstance(target_id, str):
                return False
            with contextlib.suppress(InvalidInput):
                return self.data.get(target_collection, target_id) is not None
            return False
        for rule in self.rules:
            if (
                rule["kind"] == "reference"
                and rule["collection"] == collection
                and rule["field"] == field
                and isinstance(target_id, str)
            ):
                with contextlib.suppress(InvalidInput):
                    if self.data.get(rule["target"], target_id) is not None:
                        return True
        return False

    def _apply_again(self, operation: dict) -> dict:
        """A rule's update of a dependent that changed after the plan: done against its
        current version while it still names the deleted record, and dropped once it doesn't.

        Only derived updates carry ``targets``; a primary write's conflict stands.
        """
        targets = operation.get("targets")
        collection, record_id = operation["collection"], operation["id"]
        if not targets:
            # An unset saved by an older plan, without its targets: done once the record is
            # gone. A primary write's conflict stands.
            if operation["kind"] == "unset" and self.data.get(collection, record_id) is None:
                return {}
            raise Conflict("record changed before it could be updated", reason="concurrent_write")
        current = self.data.get(collection, record_id)
        if current is None:
            return {}
        # Only edits of the record the plan saw: one deleted and created again since (by a
        # writer that finished this change, then others) is not the plan's to update, even
        # when it names the same target again. Its later write conflicts if the record is
        # recreated after this check, and the next attempt checks again.
        try:
            planned = self.data.incarnation(collection, record_id, operation["if_version"])
        except InvalidInput:
            return {}  # The planned version's history is gone: the record was removed since.
        if planned != self.data.incarnation(collection, record_id):
            return {}
        # A field naming its target again is only the plan's while that target is still
        # gone: a writer that finished this change may have created the target again since,
        # and the reference is then valid, however many edits ago the plan's version was.
        naming = [
            field
            for field, target in targets.items()
            if _field(current.value, field) == target
            and not self._target_exists(
                collection, field, target, operation.get("target_collections", {}).get(field)
            )
        ]
        if not naming or not isinstance(current.value, dict):
            return {}
        operation_id = f"{operation['operation_id']}.{_digest(current.version)[:16]}"
        if operation["kind"] == "delete":
            return self.data.delete(
                collection, record_id, current.version, operation_id, operation["actor"]
            ).to_dict()
        value = {key: item for key, item in current.value.items() if key not in naming}
        if self.validate is not None:
            self.validate(collection, record_id, value)
        return self.data.replace(
            collection, record_id, value, current.version, operation_id, operation["actor"]
        ).to_dict()

    def _execute(self, operations: list[dict]) -> tuple[dict | None, Exception | None, bool]:
        """Applies a change; the follow-on writes (cascades) run only once its own write landed.

        Returns its own write's result, the first failure, and whether that write landed.
        """
        result: dict | None = None
        failure: Exception | None = None
        for index, operation in enumerate(operations):
            try:
                outcome = self._apply(operation)
            except (Conflict, NotFound, CapacityExceeded, InvalidInput) as exc:
                failure = failure or exc
                if index == 0:
                    return None, failure, False
                continue
            if index == 0:
                result = outcome
        return result, failure, True

    def _run(self, epoch: str, started: Document) -> dict:
        result, failure, landed = self._execute(started.value["pending"])
        if landed and failure is not None:
            # The change itself was saved, but not every record its rules update: the plan
            # stays pending, and the next writer (or a retry of this one) finishes it.
            raise IncompleteChange() from failure
        self._clear(epoch, started, failed=failure is not None)
        if result is None:
            raise (
                failure
                if failure is not None
                else CorruptData("A guarded change produced no result")
            )
        return result

    def _finish(self, epoch: str, state: Document) -> None:
        _, failure, landed = self._execute(state.value["pending"])
        if landed and failure is not None:
            return  # Still unfinished; it stays pending for the next writer.
        self._clear(epoch, state, failed=failure is not None)

    def _clear(self, epoch: str, state: Document, *, failed: bool) -> None:
        value = {**state.value, "pending": None, "writes": state.value.get("writes", 0) + 1}
        if failed:
            # The derived state counted a write that did not land; recount before the next change.
            value["rules"] = _STALE
        with contextlib.suppress(Conflict):  # Another writer finished this change first.
            self.guard.replace(
                _GUARD_COLLECTION,
                epoch,
                value,
                state.version,
                self._guard_operation(state, "clear"),
                self.actor,
            )

    def mark_stale(
        self,
        *,
        unguarded_write: bool = False,
        reserve: str | None = None,
        release: str | None = None,
    ) -> None:
        """Mark the derived state stale, so the next guarded write rebuilds it.

        For an *unguarded_write*, one the published rules don't cover (no rules, or rules
        only for other collections) that went straight to the records: publishing earlier
        rules again must recount it, even when those rules match the digest the state last
        saw. State derived for this guard's own rules is left alone then, because they don't
        cover the collection written and any other rules differ from it and rebuild anyway.
        A change left pending stays pending: the next guarded writer finishes it before it
        rebuilds, so a change that can't finish yet never fails this. A gadget that never had
        rules has no state, and nothing is written.

        An unguarded write *reserve*s a token before it writes and *release*s it after: while
        a reservation is younger than :data:`UNGUARDED_HOLD_S`, no rebuild commits, so one
        that read the records before the write landed can't take the place of the mark the
        write would make after it. A release that never comes (the mark failed, the process
        stopped) only holds rebuilds that long; the next one then reads the landed write.
        """
        if not self._epochs():
            return
        for attempt in range(GUARD_ATTEMPTS):
            epoch, state = self._current()
            current = state.value
            if current.get("sealed") is True:
                # A sealed epoch is never changed: its successor is made from it as it was
                # sealed, so a mark here could be lost. Make the successor, then mark that.
                self._advance(epoch, current)
                continue
            # A rebuild under way may already have read the record this write changed, so
            # it starts over even when the state is stale or the rules match.
            rebuilding = isinstance(current.get("rebuild"), dict)
            settled = not rebuilding and (
                current.get("rules") == _STALE
                or (unguarded_write and current.get("rules") == self.digest)
            )
            reservations = _live_reservations(current, time.time())
            if reserve is not None:
                reservations[reserve] = int(time.time())
            if release is not None:
                reservations.pop(release, None)
            if settled and reserve is None and release is None:
                return
            value = {**current, "unguarded": reservations}
            if not settled:
                value.update(rules=_STALE, rebuild=None, over_capacity=None)
            try:
                self.guard.replace(
                    _GUARD_COLLECTION,
                    epoch,
                    value,
                    state.version,
                    self._guard_operation(state, "stale"),
                    self.actor,
                )
                return
            except Conflict:
                _back_off(attempt)
        raise Conflict("The gadget's rule state is busy; try again")

    # -- derived state and rotation -------------------------------------------

    def _rebuild_step(self, epoch: str, state: Document) -> None:
        """Rebuild the derived state by one page of records, saving the progress.

        Counts, unique keys and the reference index are rebuilt from the records, one
        collection and one page at a time. Writers wait (helping) until it is done.
        """
        value = state.value
        progress = value.get("rebuild")
        if not isinstance(progress, dict) or progress.get("digest") != self.digest:
            progress = {
                "digest": self.digest,
                "generation": int(value.get("ref_generation", 0)) + 1,
                "todo": sorted({rule["collection"] for rule in self.rules}),
                "cursor": None,
                "counts": {r["name"]: 0 for r in self.rules if r["kind"] == "max_records"},
                "unique": {r["name"]: {} for r in self.rules if r["kind"] == "unique"},
            }
        else:
            progress = {
                **progress,
                "todo": list(progress["todo"]),
                "counts": dict(progress["counts"]),
                "unique": {name: dict(keys) for name, keys in progress["unique"].items()},
            }
            if progress["todo"]:
                collection = str(progress["todo"][0])
                cursor = progress["cursor"]
                page = self.data.list(
                    collection, REBUILD_PAGE, cursor if isinstance(cursor, str) else None
                )
                for record in page.items:
                    self._rebuild_record(progress, collection, record)
                if (
                    not progress.get("over")
                    and _canonical_size({**value, "counts": {}, "unique": {}, "rebuild": progress})
                    > MAX_GUARD_BYTES
                ):
                    # More unique keys than the state can hold: keep rebuilding the
                    # reference index, which lives outside it, and stop counting.
                    progress.update(over=True, counts={}, unique={})
                if page.next_cursor is not None:
                    progress["cursor"] = page.next_cursor
                else:
                    progress["todo"].pop(0)
                    progress["cursor"] = None
        writes = value.get("writes", 0) + 1
        generation = progress["generation"]
        assert isinstance(generation, int)
        if progress["todo"]:
            # The old counts and keys no longer describe the rules being rebuilt, and no write
            # reads them until the rebuild commits (writes help with it first, and a pending
            # change was finished before it began), so they make room for the new ones.
            updated = {**value, "counts": {}, "unique": {}, "rebuild": progress, "writes": writes}
        elif _live_reservations(value, time.time()):
            # An unguarded write is on its way: this rebuild may have read the records before
            # it landed, so it doesn't commit; the write's own mark restarts it.
            raise Conflict(
                "A write to this gadget is still finishing; try again shortly", reason="busy"
            )
        else:
            updated = {
                **value,
                "rules": self.digest,
                "counts": progress["counts"],
                "unique": progress["unique"],
                "ref_generation": progress["generation"],
                "rebuild": None,
                "pending": None,
                "over_capacity": None,
                "writes": writes,
            }
            if progress.get("over") or _canonical_size(updated) > MAX_GUARD_BYTES:
                # The rules are live, but their counts and keys don't fit: deletes still
                # work (the reference index is complete), and other writes are refused
                # until deletes make room. See ``write``.
                updated.update(counts={}, unique={}, over_capacity={"deletes": 0})
        if _canonical_size(updated) > MAX_GUARD_BYTES:
            raise CapacityExceeded("The gadget's rule state exceeds its storage limit")
        with contextlib.suppress(Conflict):  # Another writer took the step.
            self.guard.replace(
                _GUARD_COLLECTION,
                epoch,
                updated,
                state.version,
                self._guard_operation(state, "rebuild"),
                self.actor,
            )
            if not progress["todo"]:
                self._retire_index_generations(generation)

    def _retire_index_generations(self, current: int) -> None:
        """Remove the index generations older than *current*, the one the rebuild committed.

        Generations only grow: a rebuild writes ``ref_generation + 1`` of the state it
        started from, so every rebuild begun after this one committed, or still running,
        writes a higher generation, whatever rules it is for. Removing only lower ones
        therefore never removes entries a later rebuild wrote, however late this runs. The
        index's top level holds only generations, and each rebuild removes the ones below
        it, so listing it reads a handful of names. Each removal is one ``delete_tree``,
        which the store does in bounded work; a store that cannot delete keeps them, and
        lookups never read them again.
        """
        if not self.files.supports_delete:
            return
        root = self._index_root()
        with contextlib.suppress(NotFound):
            names, _more = self.files.list_page(root, after=None, limit=MAX_CONSTRAINTS * 4)
            for name in names:
                number = _generation_number(name)
                # Anything else is an index from before generations came first.
                if number is None or number < current:
                    with contextlib.suppress(NotFound):
                        self.files.delete_tree(f"{root}/{name}")

    def _rebuild_record(self, progress: dict, collection: str, record: Document) -> None:
        counting = not progress.get("over")
        for rule in self.rules:
            if rule["collection"] != collection:
                continue
            if rule["kind"] in ("max_records", "unique") and not counting:
                continue
            if rule["kind"] == "max_records" and self._matches(rule, record.value):
                progress["counts"][rule["name"]] += 1
            elif rule["kind"] == "unique":
                key = self._unique_key(rule, record.value)
                if key is not None:
                    keys = progress["unique"][rule["name"]]
                    # Every holder of a value earlier data repeats, so none of them frees it.
                    holders = _holders(keys.get(key))
                    if record.id not in holders:
                        _set_holders(keys, key, [*holders, record.id])
            elif rule["kind"] == "reference":
                target = self._reference_of(rule, record.value)
                if target is not None:
                    directory = self._index_directory(rule, int(progress["generation"]), target)
                    self._index_add(directory, record.id)

    def _epochs(self) -> list[str]:
        """The epochs a write needs, oldest first: the oldest two still to retire and the
        newest two. Found by probing numbered epochs, never by listing them all, so a
        backlog left by retirements that were held up costs no more to find.

        Epochs are made in order, and retired in order (see :meth:`_current`), so the ones
        still there run without gaps from the first not yet retired (``retired/<n>.json``
        markers, also in order) to the newest. A store that can't delete keeps them all.
        """
        root = f"{self.guard_root}/{_GUARD_COLLECTION}"
        floor = self._epoch_floor(root) if self.files.supports_delete else 1
        newest = self._newest_epoch_number(root, floor)
        if not newest:
            return []
        wanted = {floor, floor + 1, newest - 1, newest}
        return [_epoch_name(n) for n in sorted(wanted) if floor <= n <= newest]

    def _marker_exists(self, path: str) -> bool:
        try:
            self.files.read_json(path)
        except (NotFound, CorruptData):
            return False
        return True

    def _first_unretired(self) -> int:
        """The lowest n without a ``retired/<n>.json`` marker, in O(log n) probes."""
        markers = f"{self.guard_root}/retired"
        if not self._marker_exists(f"{markers}/1.json"):
            return 1
        low, high = 1, 2
        while self._marker_exists(f"{markers}/{high}.json"):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if self._marker_exists(f"{markers}/{middle}.json"):
                low = middle
            else:
                high = middle
        return high

    def _mark_retired(self, number: int) -> None:
        self.files.mkdirs(f"{self.guard_root}/retired")
        with contextlib.suppress(Conflict):  # Another writer marked it.
            self.files.create_json(f"{self.guard_root}/retired/{number}.json", {})

    def _epoch_floor(self, root: str) -> int:
        """The first epoch not yet retired. An epoch removed whose marker a stopped writer
        never wrote is marked now (a few per call)."""
        floor = self._first_unretired()
        for _ in range(_FLOOR_REPAIRS_PER_CALL):
            if self._epoch_exists(root, floor) or not self._epoch_exists(root, floor + 1):
                break
            self._mark_retired(floor)
            floor += 1
        return floor

    def _epoch_exists(self, root: str, number: int) -> bool:
        try:
            self.files.list_page(f"{root}/{_epoch_name(number)}", after=None, limit=1)
        except NotFound:
            return False
        return True

    def _newest_epoch_number(self, root: str, start: int = 1) -> int:
        """The highest epoch number from *start*, or 0 for none, in O(log epochs) probes.

        The epochs from *start* to the newest are all there (see :meth:`_epochs`).
        """
        if not self._epoch_exists(root, start):
            return 0
        low, high = start, start + 1
        while self._epoch_exists(root, high):
            low, high = high, start + 2 * (high - start)
        # low exists, high does not.
        while high - low > 1:
            middle = (low + high) // 2
            if self._epoch_exists(root, middle):
                low = middle
            else:
                high = middle
        return low

    def _current(self) -> tuple[str, Document]:
        epochs = self._epochs()
        if not epochs:
            self._create_epoch("e000001", dict(_INITIAL_STATE))
            return self._current()
        # Sealed epochs two behind the current one have no readers left: retire a couple per
        # call, so the guard keeps a few records however many writes the gadget takes.
        for old in epochs[:-2][:2]:
            # In order: one held up stops the others, so the epochs left run without gaps.
            if not self.guard.retire(_GUARD_COLLECTION, old, "omnigent-gadget-rules"):
                break
            if self.files.supports_delete:
                self._mark_retired(int(old[1:]))
        epoch = epochs[-1]
        state = self.guard.get(_GUARD_COLLECTION, epoch)
        if state is None:
            # A writer that stopped between making the epoch's directory and its first
            # revision: publish that revision again, as the same idempotent create.
            self._resume_epoch(epochs)
            state = self.guard.get(_GUARD_COLLECTION, epoch)
        if state is None or not isinstance(state.value, dict):
            raise CorruptData("Gadget rule state is missing")
        return epoch, state

    def _resume_epoch(self, epochs: list[str]) -> None:
        """Finish creating the newest epoch, which has no committed revision yet.

        The first epoch starts from the initial state; a later one from its sealed
        predecessor, exactly as ``_advance`` writes it. A predecessor that is missing or
        not sealed is damaged committed state, which stays an error.
        """
        if len(epochs) == 1:
            self._create_epoch(epochs[0], dict(_INITIAL_STATE))
            return
        previous = self.guard.get(_GUARD_COLLECTION, epochs[-2])
        if (
            previous is None
            or not isinstance(previous.value, dict)
            or previous.value.get("sealed") is not True
            or int(epochs[-1][1:]) != int(epochs[-2][1:]) + 1
        ):
            raise CorruptData("Gadget rule state is missing")
        self._advance(epochs[-2], previous.value)

    def _create_epoch(self, epoch: str, value: dict) -> None:
        if len(_canonical(value)) > MAX_GUARD_BYTES:
            raise CapacityExceeded("The gadget's unique keys exceed their storage limit")
        with contextlib.suppress(Conflict):  # Another writer created it.
            self.guard.create(
                _GUARD_COLLECTION, epoch, value, f"g-epoch-{epoch}", "omnigent-gadget-rules"
            )

    def _rotation_due(self, epoch: str, current: dict) -> bool:
        """Whether the epoch is sealed before this write.

        Where old epochs can be retired, rotating often keeps the guard's history short. A
        store that can't delete gives nothing back for a retired epoch, so each new epoch
        would use up one of the guard's records for good: there an epoch lasts until its
        record is nearly full of revisions.
        """
        if self.files.supports_delete:
            return current.get("writes", 0) >= EPOCH_WRITES
        limit = self.guard.limits.max_revisions_per_record
        threshold = limit - min(_ROTATION_HEADROOM, limit // 2)
        return self.guard.revision_count(_GUARD_COLLECTION, epoch) >= threshold

    def _seal(self, epoch: str, state: Document) -> None:
        with contextlib.suppress(Conflict):
            self.guard.replace(
                _GUARD_COLLECTION,
                epoch,
                {**state.value, "sealed": True},
                state.version,
                self._guard_operation(state, "seal"),
                self.actor,
            )

    def _advance(self, epoch: str, sealed: dict) -> None:
        successor = _epoch_name(int(epoch[1:]) + 1)
        self._create_epoch(successor, {**sealed, "sealed": False, "writes": 0})

    def _guard_operation(self, state: Document, purpose: str, primary: dict | None = None) -> str:
        return "g-" + _digest(
            {"state": state.version, "purpose": purpose, "actor": self.actor, "primary": primary},
            40,
        )

    def _derived_operation(self, primary: dict, index: int) -> str:
        return "g-" + _digest(
            {
                "primary": [
                    primary["collection"],
                    primary["id"],
                    primary["operation_id"],
                    primary["actor"],
                ],
                "index": index,
            },
            40,
        )
