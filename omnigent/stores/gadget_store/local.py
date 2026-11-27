"""Local filesystem implementation of :class:`~omnigent.stores.gadget_store.GadgetStore`.

Layout under ``storage_location`` (by default ``<data_dir>/gadgets``)::

    gadgets/<id>/gadget.json        # metadata, replaced atomically on rename
    gadgets/<id>/files/...          # the gadget's GadgetFiles scope
    owners/<owner key>/head.json    # the owner's first and newest marker segments
    owners/<owner key>/sNNNNNNNN/<id>  # empty marker: which gadgets a user owns
    users/<user key>/...             # one user's GadgetFiles scope (their shelf)
    .tmp/                            # staging for atomic writes; never listed
    .trash/                          # deleted scopes, removed a bounded amount at a time
    .creating/<id>                   # a create in progress; cleaned up if it stopped
    .locks/<owner key>.lock          # held while one process changes an owner's head

Owner and user keys are SHA-256 prefixes of the user ID and, for an account with a
registration, that registration, so any ID is a safe directory name and a username
registered again starts with nothing. Owner markers fill numbered segments of about
:data:`_OWNER_SEGMENT_SIZE` in creation order (the gadget's metadata names its
segment), and ``head.json`` names the first and newest segments, so a page of a user's
gadgets reads at most two segments, however many gadgets the user has. Every change of a
head happens under that owner's lock file, so processes sharing the directory never undo
each other's. Reading a page
repairs the index: a marker whose gadget is gone is removed, and so is an emptied
segment before the newest.

A create-only write stages the file in ``.tmp`` and hard-links it into place,
which fails when the target exists and never exposes a partly written file, so
the data directory must support hard links.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from filelock import FileLock, Timeout

from omnigent.stores.gadget_store import (
    GADGET_ID_PATTERN,
    MAX_FILE_BYTES,
    MAX_OWNED_PAGE_SIZE,
    CapacityExceeded,
    Conflict,
    CorruptData,
    Gadget,
    GadgetFiles,
    GadgetStore,
    InvalidInput,
    NotFound,
    validate_relative_path,
)

# Any store's ID form is read; this store creates 32 hex digits.
_GADGET_ID = GADGET_ID_PATTERN
_OWNER_SEGMENT = re.compile(r"s[0-9]{8}")
_OWNER_SEGMENT_SIZE = 1000
# How long a remover waits for another to finish with the same record before leaving it.
_EXCLUSIVE_WAIT_S = 5
# Lock files behind GadgetFiles.exclusive; two names in one stripe just wait for each other.
_LOCK_STRIPES = 1024
# The stripe lock files each thread holds, with how deeply.
_HELD_STRIPES = threading.local()
# A page cursor: the segment, and the last gadget ID read in it (none: start of the segment).
_OWNER_CURSOR = re.compile(rf"({_OWNER_SEGMENT.pattern})/((?:{GADGET_ID_PATTERN.pattern})?)")
_METADATA_FILE = "gadget.json"
_OWNER_HEAD = "head.json"
_METADATA_KIND = "omnigent.gadget.v1"
# A gadget directory without metadata this old is left over from an interrupted creation.
_TRASH_ENTRIES_PER_CALL = 2000
_ORPHAN_AGE_S = 60 * 60
# Creation notes looked at per cleanup step (each create and each start runs one).
_SWEEP_NOTES_PER_STEP = 32
# Missing owner segments one retirement skips before recording how far it got.
_HEAD_PROBES_PER_CALL = 8


def _user_key(user_id: str, registration: str | None = None) -> str:
    # A JSON array keeps the parts apart whatever characters a user ID holds.
    parts = [user_id] if registration is None else [user_id, registration]
    material = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value}")


def _encode(value: dict) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidInput("Expected a finite JSON object") from exc
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise CapacityExceeded("Gadget file exceeds its size limit")
    return encoded


def _staged(tmp_dir: Path, data: bytes) -> str:
    """Write *data* to a new file in *tmp_dir* and return its path."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(tmp_dir), prefix="stage-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _replace_file(tmp_dir: Path, target: Path, value: dict) -> None:
    """Write *target* atomically, replacing any previous content."""
    tmp = _staged(tmp_dir, _encode(value))
    try:
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _drain_trash(trash: Path, budget: int | None = None) -> int:
    """Remove files and directories from *trash*, looking at no more than *budget* directory
    entries; how many went.

    Deleting moves things into the trash in one rename, so they disappear at once; their
    files then go a bounded number at a time, on later deletions and starts, so one request
    never removes a large tree. Entries are read one at a time and the first directory met is
    entered, so a huge directory costs no more than the budget; what went is gone, so the next
    call carries on where the tree still has entries.
    """
    budget = _TRASH_ENTRIES_PER_CALL if budget is None else budget
    top = str(trash)
    # The directories entered so far, from the top: an emptied one is removed and its parent
    # read again, which now starts past it.
    path = [top]
    removed = examined = 0
    while path and examined < budget:
        child = None
        try:
            with os.scandir(path[-1]) as entries:
                for entry in entries:
                    examined += 1
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        child = entry.path
                        break
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)
                    removed += 1
                    if examined >= budget:
                        return removed
        except (FileNotFoundError, NotADirectoryError):
            path.pop()
            continue
        if child is not None:
            path.append(child)
            continue
        if len(path) == 1:
            return removed  # The trash itself is empty.
        examined += 1
        with contextlib.suppress(OSError):
            os.rmdir(path[-1])
            removed += 1
        path.pop()
    return removed


# How long a create or listing waits for another process's owner-index update.
_OWNER_INDEX_WAIT_S = 30


class LocalGadgetFiles(GadgetFiles):
    """Create-only JSON files under one directory (see module docstring)."""

    def __init__(self, root: Path, tmp_dir: Path, *, create_root: bool = True) -> None:
        """
        :param root: The scope's directory.
        :param tmp_dir: Staging directory on the same file system.
        :param create_root: Create *root* on first write, as for a user's scope.
            ``False`` for a gadget's scope, which exists while the gadget does: a
            write racing its deletion must not bring the directory back.
        """
        self._root = root
        self._tmp_dir = tmp_dir
        self._create_root = create_root

    @contextlib.contextmanager
    def exclusive(self, name: str) -> Iterator[bool]:
        # Lock files sit beside the store's staging directory. The key is the scope's path
        # within the store, not its absolute path, so stores opened through different
        # paths to the same directory (a symlink, another mount point) share each lock.
        # Locks are striped over a fixed set of files so they don't pile up per record.
        store_root = self._tmp_dir.parent
        scope = os.path.relpath(self._root, store_root)
        digest = hashlib.sha256(f"{scope}\n{name}".encode()).digest()
        stripe = int.from_bytes(digest[:4], "big") % _LOCK_STRIPES
        locks = store_root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        path = str(locks / f"x{stripe:04d}.lock")
        # Nested locks of this thread can land on one stripe; the outer hold covers them
        # rather than waiting on itself.
        held = _HELD_STRIPES.__dict__.setdefault("paths", {})
        if held.get(path):
            held[path] += 1
            try:
                yield True
            finally:
                held[path] -= 1
            return
        lock = FileLock(path, timeout=_EXCLUSIVE_WAIT_S)
        try:
            lock.acquire()
        except Timeout:
            yield False
            return
        held[path] = 1
        try:
            yield True
        finally:
            held.pop(path, None)
            lock.release()

    def _missing_scope(self) -> NotFound:
        if self._create_root:
            return NotFound("Gadget directory not found")
        return NotFound("Gadget not found")

    def _resolve(self, path: str) -> Path:
        target = self._root.joinpath(*validate_relative_path(path))
        # A symlink inside the scope must not lead out of it.
        if not target.resolve().is_relative_to(self._root.resolve()):
            raise InvalidInput("Gadget file path escapes its scope")
        return target

    def read_json(self, path: str) -> dict:
        target = self._resolve(path)
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound("Gadget file not found") from None
        except IsADirectoryError:
            raise CorruptData("Expected a gadget file, found a directory") from None
        if len(raw) > MAX_FILE_BYTES:
            raise CorruptData("Gadget file exceeds its size limit")
        try:
            value = json.loads(raw, parse_constant=_reject_nonfinite)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise CorruptData("Gadget file is not valid JSON") from exc
        if not isinstance(value, dict):
            raise CorruptData("Gadget file must hold a JSON object")
        return value

    def create_json(self, path: str, value: dict) -> None:
        target = self._resolve(path)
        if not self._root.is_dir():
            raise self._missing_scope()
        if not target.parent.is_dir():
            raise NotFound("Gadget directory not found")
        tmp = _staged(self._tmp_dir, _encode(value))
        try:
            # A link into a directory a deletion moved away fails here.
            os.link(tmp, target)
        except FileExistsError:
            raise Conflict("Gadget file already exists", reason="already_exists") from None
        except (FileNotFoundError, NotADirectoryError):
            if not self._root.is_dir():
                raise self._missing_scope() from None
            raise NotFound("Gadget directory not found") from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    supports_delete = True

    def delete_tree(self, path: str) -> None:
        target = self._resolve(path)
        trash = self._tmp_dir.parent / ".trash" / f"tree-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        # Moved out in one rename, so readers never see a partly removed directory.
        try:
            os.replace(target, trash)
        except (FileNotFoundError, NotADirectoryError):
            return
        _drain_trash(trash.parent)

    def list_paths(self, path: str) -> list[str]:
        target = self._resolve(path)
        try:
            with os.scandir(target) as entries:
                return sorted(entry.name for entry in entries)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound("Gadget directory not found") from None

    def list_page(self, path: str, *, after: str | None, limit: int) -> tuple[list[str], bool]:
        # One pass over the directory, keeping only the next *limit* names: memory stays
        # bounded by the page. The file system reads the directory to seek, so the gadget
        # code lists only directories it keeps small (an index's live generations, a cleanup
        # queue segment, a user's few top-level folders); anything that grows is numbered and
        # read by number instead. A database store seeks with its index.
        target = self._resolve(path)
        try:
            with os.scandir(target) as entries:
                wanted = heapq.nsmallest(
                    limit + 1,
                    (entry.name for entry in entries if after is None or entry.name > after),
                )
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound("Gadget directory not found") from None
        return wanted[:limit], len(wanted) > limit

    def mkdirs(self, path: str) -> None:
        self._resolve(path)
        if self._create_root:
            self._root.mkdir(parents=True, exist_ok=True)
        # One level at a time below the root, never with ``parents``: if a deletion moves
        # the scope away meanwhile, the next level fails instead of recreating it.
        current = self._root
        for part in validate_relative_path(path):
            current = current / part
            try:
                current.mkdir(exist_ok=True)
            except FileNotFoundError:
                raise self._missing_scope() from None
            except (FileExistsError, NotADirectoryError):
                # mkdir also reports FileExistsError for a directory that a deletion moved away
                # after os.mkdir saw it; only something still there that is no directory is in
                # the way.
                if current.is_dir():
                    continue
                if not current.exists():
                    raise self._missing_scope() from None
                raise InvalidInput("A gadget file is in the way of that directory") from None


class LocalGadgetStore(GadgetStore):
    """Gadgets as JSON files under a local directory (see module docstring)."""

    def __init__(
        self, storage_location: str, *, maintain: bool = True, read_only: bool = False
    ) -> None:
        """
        :param storage_location: The root directory, e.g.
            ``"/home/alice/.omnigent/gadgets"``; created if missing.
        :param maintain: Create the root and run a cleanup step now. ``False`` for a server
            with gadgets off, which only notes deleted accounts' cleanups: it touches
            nothing until something is written.
        :param read_only: Reads repair nothing: owner pages leave stale index entries and
            emptied segments as they are. For tools that only inspect a server's gadgets.
        """
        super().__init__(storage_location)
        self._root = Path(storage_location)
        self._read_only = read_only
        self._tmp_dir = self._root / ".tmp"
        # Renames read and rewrite the metadata file, and a deletion moves it away;
        # serialize them in this process.
        self._metadata_lock = threading.Lock()
        # With the owner's lock file, serializes changes to an owner's head (_owner_index).
        self._owner_lock = threading.Lock()
        if maintain:
            self._root.mkdir(parents=True, exist_ok=True)
            self._sweep()

    def _sweep(self) -> None:
        """One bounded step of cleanup: some of the trash, and gadgets whose creation
        stopped before their metadata was written.

        A create notes itself in ``.creating`` first and clears the note once the gadget is
        complete, so only those notes are looked at, at most :data:`_SWEEP_NOTES_PER_STEP`
        of them, never the whole ``gadgets`` directory.
        """
        _drain_trash(self._root / ".trash")
        cutoff = time.time() - _ORPHAN_AGE_S
        try:
            notes = os.scandir(self._root / ".creating")
        except FileNotFoundError:
            return
        with notes:
            for examined, note in enumerate(notes):
                if examined >= _SWEEP_NOTES_PER_STEP:
                    break
                with contextlib.suppress(OSError):
                    if note.stat().st_mtime >= cutoff:
                        continue  # Maybe a create still in progress.
                    if _GADGET_ID.fullmatch(note.name) is not None:
                        try:
                            self._finish_interrupted_create(note.name)
                        except Conflict:
                            # Its owner index is busy: the note stays for a later sweep, and
                            # the call this sweep follows (which already committed) succeeds.
                            continue
                    os.unlink(note.path)

    def _finish_interrupted_create(self, gadget_id: str) -> None:
        """Remove what a create that stopped before its owner marker left behind.

        A create returns only after its marker exists, so a gadget without one was never
        handed to anyone; kept, it would be missing from owner pages and account cleanup.
        """
        directory = self._gadget_dir(gadget_id)
        if not directory.is_dir():
            return
        try:
            found = self._read_record(gadget_id)
        except CorruptData:
            found = None
        if found is not None:
            gadget, segment = found
            owner_dir = self._owner_dir(gadget.owner_id, gadget.owner_registration)
            with self._owner_index(owner_dir):
                if (owner_dir / segment / gadget_id).exists():
                    return  # Complete; only clearing its note failed.
                self._detach_orphan(directory)
            return
        self._detach_orphan(directory)

    def _detach_orphan(self, directory: Path) -> None:
        trash = self._root / ".trash" / f"orphan-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.replace(directory, trash)

    def _gadget_dir(self, gadget_id: str) -> Path:
        return self._root / "gadgets" / gadget_id

    def _owner_dir(self, owner_id: str, registration: str | None = None) -> Path:
        return self._root / "owners" / _user_key(owner_id, registration)

    def _read_metadata(self, gadget_id: str) -> Gadget | None:
        found = self._read_record(gadget_id)
        return found[0] if found is not None else None

    def _read_record(self, gadget_id: str) -> tuple[Gadget, str] | None:
        """The gadget and the owner segment holding its marker."""
        if not isinstance(gadget_id, str) or _GADGET_ID.fullmatch(gadget_id) is None:
            return None
        files = LocalGadgetFiles(self._gadget_dir(gadget_id), self._tmp_dir)
        try:
            raw = files.read_json(_METADATA_FILE)
        except NotFound:
            return None
        owner, name, created_at = raw.get("owner_id"), raw.get("name"), raw.get("created_at")
        segment, registration = raw.get("owner_segment"), raw.get("owner_registration")
        if (
            raw.get("kind") != _METADATA_KIND
            or raw.get("id") != gadget_id
            or not isinstance(owner, str)
            or not isinstance(name, str)
            or not isinstance(created_at, int | float)
            or not isinstance(segment, str)
            or _OWNER_SEGMENT.fullmatch(segment) is None
            or not (registration is None or isinstance(registration, str))
        ):
            raise CorruptData("Invalid gadget metadata")
        gadget = Gadget(
            id=gadget_id,
            owner_id=owner,
            name=name,
            created_at=float(created_at),
            owner_registration=registration,
        )
        return gadget, segment

    @staticmethod
    def _metadata(gadget: Gadget, segment: str) -> dict:
        return {
            "kind": _METADATA_KIND,
            "id": gadget.id,
            "owner_id": gadget.owner_id,
            "name": gadget.name,
            "created_at": gadget.created_at,
            "owner_segment": segment,
            "owner_registration": gadget.owner_registration,
        }

    def _new_id(self) -> str:
        """A new gadget's ID; a subclass may name gadgets differently within the contract."""
        return uuid.uuid4().hex

    @staticmethod
    def _segment_name(number: int) -> str:
        return f"s{number:08d}"

    def _read_head(self, owner_dir: Path) -> tuple[int, int]:
        """The owner's first and newest segment numbers; ``(0, 0)`` for a new owner."""
        try:
            raw = json.loads((owner_dir / _OWNER_HEAD).read_text())
        except FileNotFoundError:
            return 0, 0
        except (OSError, ValueError) as exc:
            raise CorruptData("Invalid gadget owner index") from exc
        first, last = raw.get("first") if isinstance(raw, dict) else None, None
        if isinstance(raw, dict):
            last = raw.get("last")
        if type(first) is not int or type(last) is not int or not 0 <= first <= last:
            raise CorruptData("Invalid gadget owner index")
        return first, last

    @contextlib.contextmanager
    def _owner_index(self, owner_dir: Path) -> Iterator[None]:
        """Hold *owner_dir*'s head for a read-modify-write, in this process and across others
        sharing the directory."""
        locks = self._root / ".locks"
        locks.mkdir(exist_ok=True)
        with self._owner_lock:
            lock = FileLock(str(locks / f"{owner_dir.name}.lock"), timeout=_OWNER_INDEX_WAIT_S)
            try:
                lock.acquire()
            except Timeout:
                # Another process holds it: the caller's retryable busy answer, not a failure.
                raise Conflict(
                    "The gadget owner index is busy; try again", reason="busy"
                ) from None
            try:
                yield
            finally:
                lock.release()

    def _write_head(self, owner_dir: Path, first: int, last: int) -> None:
        owner_dir.mkdir(parents=True, exist_ok=True)
        _replace_file(self._tmp_dir, owner_dir / _OWNER_HEAD, {"first": first, "last": last})

    def _segment_with_room(self, owner_dir: Path) -> str:
        first, last = self._read_head(owner_dir)
        newest = self._segment_name(last)
        try:
            used = len(os.listdir(owner_dir / newest))
        except FileNotFoundError:
            used = 0
        if used < _OWNER_SEGMENT_SIZE:
            if not (owner_dir / _OWNER_HEAD).exists():
                self._write_head(owner_dir, first, last)
            return newest
        self._write_head(owner_dir, first, last + 1)
        return self._segment_name(last + 1)

    def create(self, owner_id: str, name: str, *, owner_registration: str | None = None) -> Gadget:
        gadget = Gadget(
            id=self._new_id(),
            owner_id=owner_id,
            name=name,
            created_at=time.time(),
            owner_registration=owner_registration,
        )
        owner_dir = self._owner_dir(owner_id, owner_registration)
        # Noted first, so a create that stops before its metadata is found and cleaned up
        # without listing every gadget (see _sweep).
        note = self._root / ".creating" / gadget.id
        note.parent.mkdir(parents=True, exist_ok=True)
        note.touch()
        with self._owner_index(owner_dir):
            segment = self._segment_with_room(owner_dir)
            (owner_dir / segment).mkdir(parents=True, exist_ok=True)
            directory = self._gadget_dir(gadget.id)
            (directory / "files").mkdir(parents=True)
            _replace_file(
                self._tmp_dir, directory / _METADATA_FILE, self._metadata(gadget, segment)
            )
            (owner_dir / segment / gadget.id).touch()
        with contextlib.suppress(OSError):
            note.unlink()
        self._sweep()
        return gadget

    def get(self, gadget_id: str) -> Gadget | None:
        return self._read_metadata(gadget_id)

    def _drop_empty_segment(self, owner_dir: Path, segment: str) -> None:
        """Remove an emptied segment before the newest, moving the first one on past it."""
        with self._owner_index(owner_dir):
            first, last = self._read_head(owner_dir)
            number = int(segment[1:])
            if number >= last:
                return
            try:
                (owner_dir / segment).rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                return  # A gadget was added to it meanwhile.
            if number == first:
                # At most _HEAD_PROBES_PER_CALL missing segments are skipped per call, and the
                # head records how far this got: a later page starting at a missing first
                # segment finds it empty and moves the head on again.
                following = first + 1
                probes = 0
                while (
                    following < last
                    and probes < _HEAD_PROBES_PER_CALL
                    and not (owner_dir / self._segment_name(following)).exists()
                ):
                    following += 1
                    probes += 1
                self._write_head(owner_dir, following, last)

    def list_owned_page(
        self,
        owner_id: str,
        *,
        limit: int,
        after: str | None = None,
        owner_registration: str | None = None,
    ) -> tuple[list[Gadget], str | None]:
        if type(limit) is not int or not 1 <= limit <= MAX_OWNED_PAGE_SIZE:
            raise InvalidInput(f"limit must be an integer in 1..{MAX_OWNED_PAGE_SIZE}")
        owner_dir = self._owner_dir(owner_id, owner_registration)
        first, last = self._read_head(owner_dir)
        start_number, start_id = first, ""
        if after is not None:
            match = _OWNER_CURSOR.fullmatch(after) if isinstance(after, str) else None
            if match is None:
                raise InvalidInput("Invalid cursor")
            start_number, start_id = max(int(match.group(1)[1:]), first), match.group(2)
            if int(match.group(1)[1:]) < first:
                start_id = ""
        owned: list[Gadget] = []
        # Markers can outlive a deletion that stopped half-way; they cost a read but no slot,
        # and are removed as they are found.
        reads_left = 2 * limit
        # Bounded work per page: at most two segments listed and 2 * limit metadata reads;
        # a page can come back short or empty and still continue.
        for number in range(start_number, min(start_number + 2, last + 1)):
            segment = self._segment_name(number)
            try:
                names = sorted(
                    n for n in os.listdir(owner_dir / segment) if _GADGET_ID.fullmatch(n)
                )
            except FileNotFoundError:
                names = []
            last_read = start_id if number == start_number else ""
            if last_read:
                names = [n for n in names if n > last_read]
            elif not names and not self._read_only:
                self._drop_empty_segment(owner_dir, segment)
            for gadget_id in names:
                if len(owned) == limit or reads_left == 0:
                    return owned, f"{segment}/{last_read}"
                reads_left -= 1
                last_read = gadget_id
                gadget = self._read_metadata(gadget_id)
                if (
                    gadget is not None
                    and gadget.owner_id == owner_id
                    and gadget.owner_registration == owner_registration
                ):
                    owned.append(gadget)
                elif not self._read_only:
                    with contextlib.suppress(FileNotFoundError):
                        (owner_dir / segment / gadget_id).unlink()
            if number < last and (len(owned) == limit or reads_left == 0 or number > start_number):
                return owned, f"{self._segment_name(number + 1)}/"
        return owned, None

    def rename(self, gadget_id: str, name: str) -> Gadget | None:
        with self._metadata_lock:
            found = self._read_record(gadget_id)
            if found is None:
                return None
            current, segment = found
            renamed = Gadget(
                current.id, current.owner_id, name, current.created_at, current.owner_registration
            )
            try:
                _replace_file(
                    self._tmp_dir,
                    self._gadget_dir(gadget_id) / _METADATA_FILE,
                    self._metadata(renamed, segment),
                )
            except (FileNotFoundError, NotADirectoryError):
                # Deleted by another process between the read and the write.
                return None
            return renamed

    def delete(self, gadget_id: str) -> bool:
        # Moving the directory out first makes the gadget disappear at once;
        # removing its files can then take as long as it needs.
        trash = self._root / ".trash" / f"{gadget_id}-{uuid.uuid4().hex}"
        with self._metadata_lock:
            found = self._read_record(gadget_id)
            if found is None:
                return False
            gadget, segment = found
            trash.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self._gadget_dir(gadget_id), trash)
            except FileNotFoundError:
                return False
        owner_dir = self._owner_dir(gadget.owner_id, gadget.owner_registration)
        with contextlib.suppress(FileNotFoundError):
            (owner_dir / segment / gadget_id).unlink()
        # The delete has committed: a busy owner index only puts off retiring the emptied
        # segment, which a later delete or page does, rather than failing a done delete.
        with contextlib.suppress(OSError, Conflict):
            if not os.listdir(owner_dir / segment):
                self._drop_empty_segment(owner_dir, segment)
        self._empty_trash()
        return True

    def _empty_trash(self) -> None:
        _drain_trash(self._root / ".trash")

    def gadget_files(self, gadget_id: str) -> GadgetFiles:
        if _GADGET_ID.fullmatch(gadget_id) is None:
            raise NotFound("Gadget not found")
        return LocalGadgetFiles(
            self._gadget_dir(gadget_id) / "files", self._tmp_dir, create_root=False
        )

    def _user_dir(self, user_id: str, registration: str | None = None) -> Path:
        return self._root / "users" / _user_key(user_id, registration)

    def user_files(self, user_id: str, *, owner_registration: str | None = None) -> GadgetFiles:
        return LocalGadgetFiles(self._user_dir(user_id, owner_registration), self._tmp_dir)

    def delete_user_files(self, user_id: str, *, owner_registration: str | None = None) -> None:
        key = _user_key(user_id, owner_registration)
        trash = self._root / ".trash" / f"user-{key}-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(self._user_dir(user_id, owner_registration), trash)
        except FileNotFoundError:
            return
        self._empty_trash()
