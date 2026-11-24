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

The guard record rotates every ``EPOCH_WRITES`` writes because a record holds a
bounded number of revisions; an epoch two behind the current one is retired (deleted
and removed through the document store's fenced removal), so a gadget keeps at most
a few epoch records however many writes it takes. Its state, with a change in flight,
must fit in ``MAX_GUARD_BYTES``: each unique key costs its digest plus the holding
record's ID, so how many records a ``unique`` rule allows depends on their IDs and on
the size of the record being written, not only on the collection's record limit.

A ``unique`` value that data from before the rule already repeats keeps every record
holding it, so those records stay editable as long as they keep that value, and the value
is not free for another record until all of them have moved off it.

Each ``reference`` rule keeps an index of the records naming each target
(``refs/<rule>/g<generation>/<target>/<record>``), written as part of every guarded
change, so deleting a target reads only the records that name it. Entries a change left
behind are checked against the record and dropped. A lookup reads at most
``MAX_STALE_INDEX_ENTRIES`` of those: one that would read more has the index rebuilt.
When the rules change, the index needs rebuilding, or a write fails half-way, the derived
state is rebuilt a page at a time into a new index generation, with its progress saved in
the guard, so each step costs bounded work and a stopped rebuild resumes; the generations
it replaces are then removed.
"""

from __future__ import annotations

import contextlib
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
# Tries at taking the guard; between lost races a writer waits a random, growing time, so a
# burst of writers spreads out instead of colliding again (under 5 s in total).
GUARD_ATTEMPTS = 16
GUARD_BACKOFF_S = 0.01
GUARD_BACKOFF_MAX_S = 0.4
_ON_DELETE = ("restrict", "cascade", "unset")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EPOCH = re.compile(r"^e[0-9]{6,12}$")
_GUARD_COLLECTION = "epochs"
_STALE = "stale"
_INITIAL_STATE = {"rules": _STALE, "counts": {}, "unique": {}, "pending": None, "writes": 0}
# The guard's own records; tests shrink it.
GUARD_LIMITS: StoreLimits = DEFAULT_LIMITS
# Records one rebuild step reads, and the steps one write may take before giving up as busy.
REBUILD_PAGE = 200
REBUILD_STEPS_PER_WRITE = 64
# Bumped when the derived state gains something a rebuild must fill in (the reference index,
# its generations, every holder of a repeated unique value).
_STATE_FORMAT = 3


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
    ) -> None:
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
            if current.get("rules") != self.digest:
                # Each step reads one page; a write helps finish the rebuild before its own.
                rebuild_steps += 1
                if rebuild_steps > REBUILD_STEPS_PER_WRITE:
                    break
                self._rebuild_step(epoch, state)
                continue
            attempts += 1
            if current.get("writes", 0) >= EPOCH_WRITES:
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
            before = None
        else:
            if existing is None or existing.version != primary["if_version"]:
                return None
            before = existing.value
        after = primary.get("value") if primary["kind"] != "delete" else None
        generation = int(state.get("ref_generation", 0))
        plan = _Plan(
            counts=dict(state.get("counts", {})),
            unique={name: dict(keys) for name, keys in state.get("unique", {}).items()},
            operations=[primary],
        )
        # The records this plan deletes or updates, and their planned operation and value.
        gone: set[tuple[str, str]] = {(collection, record_id)} if after is None else set()
        planned: dict[tuple[str, str], tuple[dict, Any]] = {}
        queue: list[tuple[str, str, Any, Any]] = [(collection, record_id, before, after)]
        changes: list[tuple[str, str, Any, Any]] = []
        while queue:
            changed_collection, changed_id, old, new = queue.pop(0)
            changes.append((changed_collection, changed_id, old, new))
            for rule in self.rules:
                if rule["collection"] == changed_collection:
                    self._check(rule, plan, changed_id, old, new)
            if old is None or new is not None:
                continue
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
                if any(rule["on_delete"] == "cascade" for rule in rules):
                    operation["kind"] = "delete"
                    operation.pop("fields", None)
                    gone.add(key)
                    planned[key] = (operation, None)
                    queue.append((key[0], dependent.id, current, None))
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
                            "path": self._index_path(rule, generation, target, changed_id),
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
        target = value.get(rule["field"]) if isinstance(value, dict) else None
        return target if isinstance(target, str) and _IDENTIFIER.fullmatch(target) else None

    def _index_root(self) -> str:
        return f"{self.guard_root}/refs"

    def _index_directory(self, rule: dict, generation: int, target_id: str) -> str:
        return f"{self._index_root()}/{_digest(rule, 16)}/g{generation}/{target_id}"

    def _index_path(self, rule: dict, generation: int, target_id: str, record_id: str) -> str:
        return f"{self._index_directory(rule, generation, target_id)}/{record_id}"

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
            # Keeping its value: allowed even for a value earlier data already repeats.
            if (
                new_key is not None
                and old_key == new_key
                and record_id in _holders(keys.get(new_key))
            ):
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
        return isinstance(value, dict) and all(
            key in value and value[key] == expected and type(value[key]) is type(expected)
            for key, expected in rule["where"].items()
        )

    @staticmethod
    def _unique_key(rule: dict, value: Any) -> str | None:
        if not isinstance(value, dict) or any(value.get(name) is None for name in rule["fields"]):
            return None
        return _digest([value[name] for name in rule["fields"]], 24)

    def _dependents(self, rule: dict, target_id: str, generation: int) -> list[Document]:
        """The live records whose *rule* field names *target_id*, read from the rule's index.

        Stops after one more than a change may touch, which is enough to refuse or cap the
        change. Entries for records that no longer name the target are dropped as found. A
        lookup that meets more than ``MAX_STALE_INDEX_ENTRIES`` of those raises
        ``_IndexNeedsRebuild`` rather than guess from part of the index.
        """
        directory = self._index_directory(rule, generation, target_id)
        found: list[Document] = []
        after: str | None = None
        stale = 0
        while len(found) <= MAX_PLAN_OPERATIONS:
            try:
                names, more = self.files.list_page(
                    directory, after=after, limit=MAX_PLAN_OPERATIONS + 1
                )
            except NotFound:
                return found
            for name in names:
                after = name
                record = (
                    self.data.get(rule["collection"], name)
                    if _IDENTIFIER.fullmatch(name)
                    else None
                )
                if record is not None and record.value.get(rule["field"]) == target_id:
                    found.append(record)
                    continue
                stale += 1
                if stale > MAX_STALE_INDEX_ENTRIES:
                    raise _IndexNeedsRebuild()
                self._drop_entry(
                    f"{directory}/{name}",
                    rule["collection"] if _IDENTIFIER.fullmatch(name) else None,
                    name,
                    rule["field"],
                    target_id,
                )
            if not more:
                return found
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
            path = operation["path"]
            self.files.mkdirs(path.rsplit("/", 1)[0])
            with contextlib.suppress(Conflict):  # Already there.
                self.files.create_json(path, {})
            return {}
        if operation["kind"] == "ref_remove":
            if "id" in operation:
                self._drop_entry(
                    operation["path"],
                    operation["collection"],
                    operation["id"],
                    operation["field"],
                    operation["target"],
                )
            elif self.files.supports_delete:  # A plan saved before entries named their record.
                with contextlib.suppress(NotFound):
                    self.files.delete_tree(operation["path"])
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
        value = {key: item for key, item in source.value.items() if key not in fields}
        try:
            return self.data.replace(
                collection, record_id, value, operation["if_version"], operation_id, actor
            ).to_dict()
        except (Conflict, NotFound):
            return self._apply_again(operation)

    def _drop_entry(
        self, path: str, collection: str | None, record_id: str, field: str, target_id: str
    ) -> None:
        """Deletes the index entry at *path* unless its record names *target_id* again.

        Checked and deleted under the record's lock, which every write of the record holds:
        a record moved away and back has its entry added again after its write, so a remover
        that saw it away, or that replays an old plan, never deletes the entry it needs now.
        A store that cannot delete, or a lock not taken, keeps the entry; lookups check
        entries against their records.
        """
        if not self.files.supports_delete:
            return
        if collection is None:  # Not a record ID: nothing could need the entry.
            with contextlib.suppress(NotFound):
                self.files.delete_tree(path)
            return
        with self.data.record_lock(collection, record_id) as held:
            if not held:
                return
            current = self.data.get(collection, record_id)
            if current is not None and current.value.get(field) == target_id:
                return
            with contextlib.suppress(NotFound):
                self.files.delete_tree(path)

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
        naming = [field for field, target in targets.items() if current.value.get(field) == target]
        if not naming:
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

    def mark_stale(self, *, unguarded_write: bool = False) -> None:
        """Mark the derived state stale, so the next guarded write rebuilds it.

        For an *unguarded_write*, one the published rules don't cover (no rules, or rules
        only for other collections) that went straight to the records: publishing earlier
        rules again must recount it, even when those rules match the digest the state last
        saw. State derived for this guard's own rules is left alone then, because they don't
        cover the collection written and any other rules differ from it and rebuild anyway.
        A change left pending stays pending: the next guarded writer finishes it before it
        rebuilds, so a change that can't finish yet never fails this. A gadget that never had
        rules has no state, and nothing is written.
        """
        if not self._epochs():
            return
        for attempt in range(GUARD_ATTEMPTS):
            epoch, state = self._current()
            current = state.value
            if current.get("rules") == _STALE or (
                unguarded_write and current.get("rules") == self.digest
            ):
                return
            try:
                self.guard.replace(
                    _GUARD_COLLECTION,
                    epoch,
                    {**current, "rules": _STALE, "rebuild": None},
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
                if page.next_cursor is not None:
                    progress["cursor"] = page.next_cursor
                else:
                    progress["todo"].pop(0)
                    progress["cursor"] = None
        writes = value.get("writes", 0) + 1
        generation = progress["generation"]
        assert isinstance(generation, int)
        if progress["todo"]:
            updated = {**value, "rebuild": progress, "writes": writes}
        else:
            updated = {
                **value,
                "rules": self.digest,
                "counts": progress["counts"],
                "unique": progress["unique"],
                "ref_generation": progress["generation"],
                "rebuild": None,
                "pending": None,
                "writes": writes,
            }
        if len(_canonical(updated)) > MAX_GUARD_BYTES:
            raise CapacityExceeded("The gadget's unique keys exceed their storage limit")
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
        """Remove the index generations and old rules' indexes the finished rebuild replaced.

        Each removal is one ``delete_tree`` of a directory, which the store does in bounded
        work; a store that cannot delete keeps them, and lookups never read them again.
        """
        if not self.files.supports_delete:
            return
        live = {_digest(rule, 16) for rule in self.rules if rule["kind"] == "reference"}
        root = self._index_root()
        with contextlib.suppress(NotFound):
            names, _more = self.files.list_page(root, after=None, limit=MAX_CONSTRAINTS * 4)
            for name in names:
                if name not in live:
                    with contextlib.suppress(NotFound):
                        self.files.delete_tree(f"{root}/{name}")
                    continue
                generations, _ = self.files.list_page(
                    f"{root}/{name}", after=None, limit=MAX_CONSTRAINTS * 4
                )
                for generation in generations:
                    if generation != f"g{current}":
                        with contextlib.suppress(NotFound):
                            self.files.delete_tree(f"{root}/{name}/{generation}")

    def _rebuild_record(self, progress: dict, collection: str, record: Document) -> None:
        for rule in self.rules:
            if rule["collection"] != collection:
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
                    path = self._index_path(rule, int(progress["generation"]), target, record.id)
                    self._apply({"kind": "ref_add", "path": path})

    def _epochs(self) -> list[str]:
        try:
            paths = self.files.list_paths(f"{self.guard_root}/{_GUARD_COLLECTION}")
        except NotFound:
            paths = []
        names = (path.rsplit("/", 1)[-1] for path in paths)
        return sorted((name for name in names if _EPOCH.fullmatch(name)), key=lambda n: int(n[1:]))

    def _current(self) -> tuple[str, Document]:
        epochs = self._epochs()
        if not epochs:
            self._create_epoch("e000001", dict(_INITIAL_STATE))
            return self._current()
        # Sealed epochs two behind the current one have no readers left: retire a couple per
        # call, so the guard keeps a few records however many writes the gadget takes.
        for old in epochs[:-2][:2]:
            self.guard.retire(_GUARD_COLLECTION, old, "omnigent-gadget-rules")
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
        successor = f"e{int(epoch[1:]) + 1:06d}"
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
