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
import os
import shutil
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_VERSION = 2
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


def backup_state(state_dir: Path, destination: Path) -> dict[str, object]:
    """Copy the non-secret state into `destination`, with a hash manifest."""

    if not state_dir.is_dir():
        raise StateError("the state directory is not there to back up")
    files_dir = destination / "files"
    files_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for name in BACKUP_INCLUDES:
        if _is_secret(name):  # pragma: no cover - the lists are held disjoint
            raise StateError(f"{name} is credential state and is never backed up")
        source = state_dir / name
        if source.is_symlink() or not source.is_file():
            continue
        if source.stat().st_size > MAX_FILE_BYTES:
            raise StateError(f"{name} is larger than a backup carries")
        shutil.copy2(source, files_dir / name)
        entries.append({"name": name, "sha256": _digest(source), "bytes": source.stat().st_size})
    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "files": entries,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )
    return manifest


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
    version = manifest.get("version")
    if type(version) is not int or version > MANIFEST_VERSION:
        raise StateError("this backup was written by a later version")
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


def verify_backup(destination: Path) -> tuple[str, ...]:
    """Names whose bytes no longer match the manifest. Empty means intact."""

    mismatched: list[str] = []
    for entry in _entries(_manifest(destination)):
        name = str(entry.get("name", ""))
        stored = destination / "files" / name
        if not stored.is_file() or _digest(stored) != entry.get("sha256"):
            mismatched.append(name)
    return tuple(mismatched)


Replace = Callable[[Path, Path], None]


def _replace(source: Path, target: Path) -> None:
    os.replace(source, target)


def restore_state(
    destination: Path, state_dir: Path, *, replace: Replace = _replace
) -> tuple[str, ...]:
    """Put the backed-up work back, having proved the whole set first.

    Everything is staged beside its target and validated before anything moves,
    so a backup with one bad file changes nothing at all rather than leaving
    half of the state from one point in time and half from another. Whatever
    happens, no staged file is left behind.
    """

    mismatched = verify_backup(destination)
    if mismatched:
        raise StateError("this backup does not match its manifest: " + ", ".join(mismatched))
    if not state_dir.is_dir():
        raise StateError("the state directory is not there to restore into")

    state_root = state_dir.resolve()
    staged: list[tuple[Path, Path]] = []
    restored: list[str] = []
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
            staged.append((staging, target))
        for staging, target in staged:
            replace(staging, target)
            with suppress(OSError):
                target.chmod(0o600)
            restored.append(target.name)
    except StateError:
        raise
    except OSError as exc:
        raise StateError("the restore could not be completed") from exc
    finally:
        for staging, _ in staged:
            with suppress(OSError):
                staging.unlink(missing_ok=True)
    return tuple(restored)


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
