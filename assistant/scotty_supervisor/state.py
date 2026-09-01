"""Backing up the work, and actually putting it back.

The earlier version of this could take a backup and describe one, but restoring
was a preview: nothing was ever written. A backup nobody has restored from is a
backup nobody has tested, so this restores for real — and does it the careful
way, staging every file and validating the whole set before a single one is
moved into place.

What is never restored is a credential. A token rotated since the backup must
stay rotated; putting the old one back would resurrect access somebody revoked.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from .journal import Journal, JournalError, replace_all

MANIFEST_VERSION = 3
MAX_FILE_BYTES = 64 * 1024 * 1024

#: Exactly what a backup carries: this deployment's own non-secret work.
BACKUP_INCLUDES: tuple[str, ...] = (
    "workflows.json",
    "personas.json",
    "setup-staging.json",
    "reminders.db",
    "approvals.db",
    "property-effects.db",
    "budgets.db",
    "workflow-runs.db",
)

#: Never copied and never written back, whatever the include list says. The
#: per-user OAuth names are spelled out as well as caught by the fragments
#: below, so that this list and the runtime's read identically by inspection.
SECRET_NAMES: tuple[str, ...] = (
    "private.json",
    "google-oauth.json",
    "google-oauth.main_operator.json",
    "google-oauth.employee.json",
    "google-consent.json",
    "google-consent.main_operator.json",
    "google-consent.employee.json",
    "credentials.json",
    "state.db",
)

_SECRET_FRAGMENTS = ("oauth", "token", "secret", "credential", "consent", "session")


class StateError(RuntimeError):
    """The backup cannot be trusted, so nothing is written."""


def _is_secret(name: str) -> bool:
    lowered = name.casefold()
    return name in SECRET_NAMES or any(fragment in lowered for fragment in _SECRET_FRAGMENTS)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_database(name: str) -> bool:
    return name.endswith(".db")


def _snapshot(source: Path, target: Path) -> tuple[str, str]:
    """Copy one live SQLite database as a consistent moment.

    Copying the file byte for byte is what the earlier version did, and in
    write-ahead mode the recent commits are not in the file: they are in a
    sidecar the copy never took. A probe showed a backup that passed its own
    hash manifest and was missing four hundred committed rows.

    SQLite's own online backup takes a snapshot of the whole database as of one
    moment, including whatever is still in the log, while writers carry on.
    """

    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30.0)
    try:
        integrity = origin.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]) != "ok":
            raise StateError(f"{source.name} does not pass its own integrity check")
        schema = "\n".join(
            str(row[0])
            for row in origin.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        )
        copy = sqlite3.connect(target, timeout=30.0)
        try:
            origin.backup(copy)
        finally:
            copy.close()
    except sqlite3.DatabaseError as exc:
        raise StateError(f"{source.name} could not be read as a database") from exc
    finally:
        origin.close()
    return "ok", hashlib.sha256(schema.encode("utf-8")).hexdigest()


def backup_state(state_dir: Path, destination: Path) -> dict[str, object]:
    """Copy the non-secret state into `destination`, as one consistent moment.

    Databases are snapshotted through SQLite's own backup API rather than
    copied, so what lands is a state that existed rather than a smear of
    several. Everything in one manifest, described as one generation, so a
    restore puts back a set of files that were true together.
    """

    if not state_dir.is_dir():
        raise StateError("the state directory is not there to back up")
    files_dir = destination / "files"
    files_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    entries: list[dict[str, object]] = []
    for name in BACKUP_INCLUDES:
        if _is_secret(name):  # pragma: no cover - the lists are held disjoint
            raise StateError(f"{name} is credential state and is never backed up")
        source = state_dir / name
        if source.is_symlink() or not source.is_file():
            continue
        if source.stat().st_size > MAX_FILE_BYTES:
            raise StateError(f"{name} is larger than a backup carries")
        target = files_dir / name
        entry: dict[str, object] = {"name": name}
        if _is_database(name):
            integrity, schema = _snapshot(source, target)
            entry["integrity"] = integrity
            entry["schema"] = schema
        else:
            shutil.copy2(source, target)
        entry["sha256"] = _digest(target)
        entry["bytes"] = target.stat().st_size
        entries.append(entry)
    manifest = {
        "version": MANIFEST_VERSION,
        # One name for one set of files that were true together. A restore
        # writes a generation, never a mixture of two.
        "generation": generation,
        "created_at": datetime.now(UTC).isoformat(),
        "files": entries,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )
    return manifest


def _entries(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise StateError("this backup's manifest is malformed")
    entries: list[Mapping[str, object]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise StateError("this backup's manifest is malformed")
        entries.append(entry)
    return entries


def _manifest(destination: Path) -> Mapping[str, object]:
    path = destination / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise StateError("this backup has no manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("this backup's manifest is unreadable") from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise StateError("this backup's manifest is malformed")
    return manifest


def verify_backup(destination: Path) -> tuple[str, ...]:
    """Names whose bytes no longer match the manifest. Empty means intact."""

    mismatched: list[str] = []
    for entry in _entries(_manifest(destination)):
        name = str(entry.get("name", ""))
        stored = destination / "files" / name
        if not stored.is_file() or _digest(stored) != entry.get("sha256"):
            mismatched.append(name)
    return tuple(mismatched)


def restore_state(destination: Path, state_dir: Path) -> tuple[str, ...]:
    """Put the backed-up work back as one generation, or leave the old one.

    The version this replaces staged every file and then replaced the live ones
    one at a time. An injected failure on the second replacement left the first
    restored and the rest current -- a generation nobody ever ran, and one
    nobody could name afterwards either.

    Now everything is staged and checked first, and the cutover goes through a
    journal: a record of what is about to move and a copy of every byte it will
    overwrite. A failure puts all of it back. A process that dies mid-cutover
    leaves the record, and the next one puts all of it back.
    """

    mismatched = verify_backup(destination)
    if mismatched:
        raise StateError("this backup does not match its manifest: " + ", ".join(mismatched))
    if not state_dir.is_dir():
        raise StateError("the state directory is not there to restore into")

    # A cutover somebody's process died in the middle of. Undo it before
    # starting another, so the generation this one replaces is a whole one.
    Journal(state_dir).recover()

    state_root = state_dir.resolve()
    staged: list[tuple[Path, Path]] = []
    try:
        for entry in _entries(_manifest(destination)):
            name = str(entry.get("name", ""))
            if name not in BACKUP_INCLUDES or _is_secret(name):
                raise StateError(f"{name} is not something a restore may write")
            target = (state_dir / name).resolve()
            if target.parent != state_root:
                raise StateError("a restore may only write inside the state directory")
            staging = state_dir / f".{name}.{uuid.uuid4().hex}.staging"
            shutil.copy2(destination / "files" / name, staging)
            if _digest(staging) != entry.get("sha256"):
                raise StateError(f"{name} did not stage intact")
            if _is_database(name):
                _prove_database(staging, name)
            staging.chmod(0o600)
            staged.append((staging, target))
        moved = replace_all(state_dir, staged)
    except StateError:
        raise
    except JournalError as exc:
        raise StateError("the restore could not be completed and was undone") from exc
    except OSError as exc:
        raise StateError("the restore could not be completed") from exc
    finally:
        for staging, _ in staged:
            with suppress(OSError):
                staging.unlink(missing_ok=True)
    return tuple(Path(item).name for item in moved)


def _prove_database(path: Path, name: str) -> None:
    """A staged database has to open and pass its own check before it counts."""

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.DatabaseError as exc:
        raise StateError(f"{name} did not restore as a readable database") from exc
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]) != "ok":
            raise StateError(f"{name} does not pass its own integrity check")
    except sqlite3.DatabaseError as exc:
        raise StateError(f"{name} did not restore as a readable database") from exc
    finally:
        connection.close()


def restorable(destination: Path) -> tuple[str, ...]:
    """What this backup would put back, without putting anything back."""

    return tuple(str(entry.get("name", "")) for entry in _entries(_manifest(destination)))


__all__ = [
    "BACKUP_INCLUDES",
    "MANIFEST_VERSION",
    "SECRET_NAMES",
    "StateError",
    "backup_state",
    "restorable",
    "restore_state",
    "verify_backup",
]
