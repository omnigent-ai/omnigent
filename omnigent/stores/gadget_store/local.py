"""Local filesystem implementation of :class:`~omnigent.stores.gadget_store.GadgetStore`.

Layout under ``storage_location`` (by default ``<data_dir>/gadgets``)::

    gadgets/<id>/gadget.json        # metadata, replaced atomically on rename
    gadgets/<id>/files/...          # the gadget's GadgetFiles scope
    owners/<owner key>/<id>          # empty marker: which gadgets a user owns
    users/<user key>/...             # one user's GadgetFiles scope (their shelf)
    .tmp/                            # staging for atomic writes; never listed
    .trash/                          # deleted scopes being removed; emptied on start

Owner and user keys are SHA-256 prefixes of the user ID, so any ID is a safe
directory name. A create-only write stages the file in ``.tmp`` and hard-links
it into place, which fails when the target exists and never exposes a partly
written file, so the data directory must support hard links.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from omnigent.stores.gadget_store import (
    MAX_FILE_BYTES,
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

_GADGET_ID = re.compile(r"[0-9a-f]{32}")
_METADATA_FILE = "gadget.json"
_METADATA_KIND = "omnigent.gadget.v1"
# A gadget directory without metadata this old is left over from an interrupted creation.
_ORPHAN_AGE_S = 60 * 60


def _user_key(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


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
        if trash.is_dir():
            shutil.rmtree(trash, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                trash.unlink()

    def list_paths(self, path: str) -> list[str]:
        target = self._resolve(path)
        try:
            with os.scandir(target) as entries:
                return sorted(entry.name for entry in entries)
        except (FileNotFoundError, NotADirectoryError):
            raise NotFound("Gadget directory not found") from None

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

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: The root directory, e.g.
            ``"/home/alice/.omnigent/gadgets"``; created if missing.
        """
        super().__init__(storage_location)
        self._root = Path(storage_location)
        self._root.mkdir(parents=True, exist_ok=True)
        self._tmp_dir = self._root / ".tmp"
        # Renames read and rewrite the metadata file, and a deletion moves it away;
        # serialize them in this process.
        self._metadata_lock = threading.Lock()
        self._sweep()

    def _sweep(self) -> None:
        """Remove what an interrupted deletion or creation left: trash, and gadget
        directories without metadata that are old enough not to be a create in progress."""
        shutil.rmtree(self._root / ".trash", ignore_errors=True)
        cutoff = time.time() - _ORPHAN_AGE_S
        try:
            entries = list(os.scandir(self._root / "gadgets"))
        except FileNotFoundError:
            return
        for entry in entries:
            if _GADGET_ID.fullmatch(entry.name) is None or not entry.is_dir(follow_symlinks=False):
                continue
            directory = Path(entry.path)
            with contextlib.suppress(OSError):
                if (
                    not (directory / _METADATA_FILE).exists()
                    and directory.stat().st_mtime < cutoff
                ):
                    shutil.rmtree(directory, ignore_errors=True)

    def _gadget_dir(self, gadget_id: str) -> Path:
        return self._root / "gadgets" / gadget_id

    def _owner_dir(self, owner_id: str) -> Path:
        return self._root / "owners" / _user_key(owner_id)

    def _read_metadata(self, gadget_id: str) -> Gadget | None:
        if not isinstance(gadget_id, str) or _GADGET_ID.fullmatch(gadget_id) is None:
            return None
        files = LocalGadgetFiles(self._gadget_dir(gadget_id), self._tmp_dir)
        try:
            raw = files.read_json(_METADATA_FILE)
        except NotFound:
            return None
        owner, name, created_at = raw.get("owner_id"), raw.get("name"), raw.get("created_at")
        if (
            raw.get("kind") != _METADATA_KIND
            or raw.get("id") != gadget_id
            or not isinstance(owner, str)
            or not isinstance(name, str)
            or not isinstance(created_at, int | float)
        ):
            raise CorruptData("Invalid gadget metadata")
        return Gadget(id=gadget_id, owner_id=owner, name=name, created_at=float(created_at))

    @staticmethod
    def _metadata(gadget: Gadget) -> dict:
        return {
            "kind": _METADATA_KIND,
            "id": gadget.id,
            "owner_id": gadget.owner_id,
            "name": gadget.name,
            "created_at": gadget.created_at,
        }

    def create(self, owner_id: str, name: str) -> Gadget:
        gadget = Gadget(id=uuid.uuid4().hex, owner_id=owner_id, name=name, created_at=time.time())
        directory = self._gadget_dir(gadget.id)
        (directory / "files").mkdir(parents=True)
        _replace_file(self._tmp_dir, directory / _METADATA_FILE, self._metadata(gadget))
        owner_dir = self._owner_dir(owner_id)
        owner_dir.mkdir(parents=True, exist_ok=True)
        (owner_dir / gadget.id).touch()
        return gadget

    def get(self, gadget_id: str) -> Gadget | None:
        return self._read_metadata(gadget_id)

    def list_owned(self, owner_id: str) -> list[Gadget]:
        try:
            names = os.listdir(self._owner_dir(owner_id))
        except FileNotFoundError:
            return []
        owned = []
        for name in names:
            gadget = self._read_metadata(name)
            # The marker can outlive a deletion that stopped half-way.
            if gadget is not None and gadget.owner_id == owner_id:
                owned.append(gadget)
        return sorted(owned, key=lambda gadget: (gadget.created_at, gadget.id))

    def rename(self, gadget_id: str, name: str) -> Gadget | None:
        with self._metadata_lock:
            current = self._read_metadata(gadget_id)
            if current is None:
                return None
            renamed = Gadget(current.id, current.owner_id, name, current.created_at)
            try:
                _replace_file(
                    self._tmp_dir,
                    self._gadget_dir(gadget_id) / _METADATA_FILE,
                    self._metadata(renamed),
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
            gadget = self._read_metadata(gadget_id)
            if gadget is None:
                return False
            trash.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self._gadget_dir(gadget_id), trash)
            except FileNotFoundError:
                return False
        with contextlib.suppress(FileNotFoundError):
            (self._owner_dir(gadget.owner_id) / gadget_id).unlink()
        shutil.rmtree(trash, ignore_errors=True)
        return True

    def gadget_files(self, gadget_id: str) -> GadgetFiles:
        if _GADGET_ID.fullmatch(gadget_id) is None:
            raise NotFound("Gadget not found")
        return LocalGadgetFiles(
            self._gadget_dir(gadget_id) / "files", self._tmp_dir, create_root=False
        )

    def _user_dir(self, user_id: str) -> Path:
        return self._root / "users" / _user_key(user_id)

    def user_files(self, user_id: str) -> GadgetFiles:
        return LocalGadgetFiles(self._user_dir(user_id), self._tmp_dir)

    def delete_user_files(self, user_id: str) -> None:
        trash = self._root / ".trash" / f"user-{_user_key(user_id)}-{uuid.uuid4().hex}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(self._user_dir(user_id), trash)
        except FileNotFoundError:
            return
        shutil.rmtree(trash, ignore_errors=True)
