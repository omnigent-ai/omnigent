"""Thingy guides and cross-record rules, enforced on every write.

A thingy revision may declare a Markdown ``guide`` for agents and ``constraints``
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
thingy. The guard holds the derived state the rules need (counts and unique
keys) and, while a change is in flight, the whole change. Every step of a change
is idempotent, so any later writer that finds an unfinished change completes it
first: a writer that stops half-way leaves neither a rule nor the derived state
broken. A rule only refuses writes that make it worse, so data that predates a
rule stays editable.

The guard record rotates every ``EPOCH_WRITES`` writes because a record holds a
bounded number of revisions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from omnigent.stores.thingy_store import ThingyFiles
from omnigent.thingies.documents import (
    CapacityExceeded,
    Conflict,
    CorruptData,
    Document,
    ImmutableDocumentStore,
    InvalidInput,
    NotFound,
)

MAX_GUIDE_BYTES = 16 * 1024
MAX_CONSTRAINTS = 16
MAX_MESSAGE_LENGTH = 300
MAX_WHERE_FIELDS = 4
MAX_UNIQUE_FIELDS = 3
MAX_RECORDS_LIMIT = 10_000
# Records one change may touch, including cascades.
MAX_PLAN_OPERATIONS = 50
# Guard state, including an in-flight change, must fit in one store document.
MAX_GUARD_BYTES = 30 * 1024
EPOCH_WRITES = 100
GUARD_ATTEMPTS = 8
_ON_DELETE = ("restrict", "cascade", "unset")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EPOCH = re.compile(r"^e[0-9]{6}$")
_GUARD_COLLECTION = "epochs"
_STALE = "stale"


class ConstraintViolation(Conflict):
    """A write would break one of the thingy's rules."""

    def __init__(self, rule: dict) -> None:
        super().__init__(rule["message"], reason="constraint_violation")
        self.rule = rule
        self.details = {
            "reason": "CONSTRAINT_VIOLATION",
            "constraint": rule["name"],
            "kind": rule["kind"],
            "collection": rule["collection"],
        }


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
        if collection not in collections:
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
            if target not in collections:
                raise InvalidInput(
                    f"Constraint {name} references a collection the revision does not declare"
                )
            on_delete = item.get("on_delete", "restrict")
            if on_delete not in _ON_DELETE:
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


@dataclass
class _Plan:
    """The derived state after a change, and the record writes that make up the change."""

    counts: dict[str, int]
    unique: dict[str, dict[str, str]]
    operations: list[dict] = field(default_factory=list)


class ConstraintGuard:
    """Serializes writes to the collections a thingy's rules mention (see module docstring)."""

    def __init__(
        self,
        files: ThingyFiles,
        guard_root: str,
        data: ImmutableDocumentStore,
        rules: list[dict],
        actor: str,
    ) -> None:
        self.files = files
        self.guard_root = guard_root
        self.guard = ImmutableDocumentStore(files, guard_root)
        self.data = data
        self.rules = rules
        self.actor = actor
        self.digest = _digest(rules)
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
        for _ in range(GUARD_ATTEMPTS):
            epoch, state = self._current()
            current = state.value
            if current.get("sealed"):
                self._advance(epoch, current)
                continue
            if current.get("pending") is not None:
                self._finish(epoch, state)
                continue
            if current.get("rules") != self.digest:
                self._rebuild(epoch, state)
                continue
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
            plan = self._plan(primary, current)
            if plan is None:
                # A retry of an applied write or a stale precondition: the store answers it
                # without changing any record, so the derived state stays as it is.
                return self._apply(primary)
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
                continue
            return self._run(epoch, started)
        raise Conflict("Other changes to this thingy are in progress; try again", reason="busy")

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
        plan = _Plan(
            counts=dict(state.get("counts", {})),
            unique={name: dict(keys) for name, keys in state.get("unique", {}).items()},
            operations=[primary],
        )
        visited = {(collection, record_id)}
        queue: list[tuple[str, str, Any, Any]] = [(collection, record_id, before, after)]
        while queue:
            changed_collection, changed_id, old, new = queue.pop(0)
            for rule in self.rules:
                if rule["collection"] == changed_collection:
                    self._check(rule, plan, changed_id, old, new)
                if (
                    rule["kind"] == "reference"
                    and rule["target"] == changed_collection
                    and old is not None
                    and new is None
                ):
                    for dependent in self._dependents(rule, changed_id):
                        key = (rule["collection"], dependent.id)
                        if key in visited:
                            continue
                        if rule["on_delete"] == "restrict":
                            raise ConstraintViolation(rule)
                        visited.add(key)
                        operation = {
                            "collection": rule["collection"],
                            "id": dependent.id,
                            "if_version": dependent.version,
                            "operation_id": self._derived_operation(primary, len(plan.operations)),
                            "actor": self.actor,
                        }
                        if rule["on_delete"] == "cascade":
                            plan.operations.append({**operation, "kind": "delete"})
                            queue.append((rule["collection"], dependent.id, dependent.value, None))
                        else:
                            plan.operations.append(
                                {**operation, "kind": "unset", "field": rule["field"]}
                            )
                            remaining = {
                                k: v for k, v in dependent.value.items() if k != rule["field"]
                            }
                            queue.append(
                                (rule["collection"], dependent.id, dependent.value, remaining)
                            )
                        if len(plan.operations) > MAX_PLAN_OPERATIONS:
                            raise CapacityExceeded(
                                f"This change would update more than {MAX_PLAN_OPERATIONS} records"
                            )
        return plan

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
            if old_key is not None and keys.get(old_key) == record_id:
                del keys[old_key]
            if new_key is not None:
                holder = keys.get(new_key)
                if holder is not None and holder != record_id:
                    raise ConstraintViolation(rule)
                keys[new_key] = record_id
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

    def _records(self, collection: str) -> list[Document]:
        records: list[Document] = []
        cursor = None
        while True:
            page = self.data.list(collection, 200, cursor)
            records.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return records

    def _dependents(self, rule: dict, target_id: str) -> list[Document]:
        return [
            record
            for record in self._records(rule["collection"])
            if record.value.get(rule["field"]) == target_id
        ]

    # -- applying ---------------------------------------------------------------

    def _apply(self, operation: dict) -> dict:
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
        if kind == "delete":
            return self.data.delete(
                collection, record_id, operation["if_version"], operation_id, actor
            ).to_dict()
        # Derived from the immutable version the plan saw, so a repeat sends the same request.
        source = self.data.get_version(collection, record_id, operation["if_version"])
        if source is None:
            raise NotFound("The record to update was deleted")
        value = {key: item for key, item in source.value.items() if key != operation["field"]}
        return self.data.replace(
            collection, record_id, value, operation["if_version"], operation_id, actor
        ).to_dict()

    def _execute(self, operations: list[dict]) -> tuple[dict | None, Exception | None]:
        """Applies a change; the follow-on writes (cascades) run only once its own write landed."""
        result: dict | None = None
        failure: Exception | None = None
        for index, operation in enumerate(operations):
            try:
                outcome = self._apply(operation)
            except (Conflict, NotFound, CapacityExceeded, InvalidInput) as exc:
                failure = failure or exc
                if index == 0:
                    break
                continue
            if index == 0:
                result = outcome
        return result, failure

    def _run(self, epoch: str, started: Document) -> dict:
        result, failure = self._execute(started.value["pending"])
        self._clear(epoch, started, failed=failure is not None)
        if result is None:
            raise (
                failure
                if failure is not None
                else CorruptData("A guarded change produced no result")
            )
        return result

    def _finish(self, epoch: str, state: Document) -> None:
        _, failure = self._execute(state.value["pending"])
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

    # -- derived state and rotation -------------------------------------------

    def _derived_state(self) -> dict:
        counts: dict[str, int] = {}
        unique: dict[str, dict[str, str]] = {}
        cache: dict[str, list[Document]] = {}
        for rule in self.rules:
            if rule["kind"] not in {"max_records", "unique"}:
                continue
            records = cache.setdefault(rule["collection"], self._records(rule["collection"]))
            if rule["kind"] == "max_records":
                counts[rule["name"]] = sum(self._matches(rule, record.value) for record in records)
            else:
                keys: dict[str, str] = {}
                for record in records:
                    key = self._unique_key(rule, record.value)
                    if key is not None:
                        keys.setdefault(key, record.id)
                unique[rule["name"]] = keys
        return {"rules": self.digest, "counts": counts, "unique": unique}

    def _current(self) -> tuple[str, Document]:
        try:
            paths = self.files.list_paths(f"{self.guard_root}/{_GUARD_COLLECTION}")
        except NotFound:
            paths = []
        epochs = sorted(
            name for name in (path.rsplit("/", 1)[-1] for path in paths) if _EPOCH.fullmatch(name)
        )
        if not epochs:
            initial = {**self._derived_state(), "pending": None, "writes": 0}
            self._create_epoch("e000001", initial)
            return self._current()
        epoch = epochs[-1]
        state = self.guard.get(_GUARD_COLLECTION, epoch)
        if state is None or not isinstance(state.value, dict):
            raise CorruptData("Thingy rule state is missing")
        return epoch, state

    def _create_epoch(self, epoch: str, value: dict) -> None:
        if len(_canonical(value)) > MAX_GUARD_BYTES:
            raise CapacityExceeded("The thingy's unique keys exceed their storage limit")
        with contextlib.suppress(Conflict):  # Another writer created it.
            self.guard.create(
                _GUARD_COLLECTION, epoch, value, f"g-epoch-{epoch}", "omnigent-thingy-rules"
            )

    def _rebuild(self, epoch: str, state: Document) -> None:
        value = {
            **state.value,
            **self._derived_state(),
            "pending": None,
            "writes": state.value.get("writes", 0) + 1,
        }
        if len(_canonical(value)) > MAX_GUARD_BYTES:
            raise CapacityExceeded("The thingy's unique keys exceed their storage limit")
        with contextlib.suppress(Conflict):
            self.guard.replace(
                _GUARD_COLLECTION,
                epoch,
                value,
                state.version,
                self._guard_operation(state, "rebuild"),
                self.actor,
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
