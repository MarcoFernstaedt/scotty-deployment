"""Immutable releases, and going back to one that was accepted.

A release directory is written once and never edited. Its manifest names every
file and its hash, so "is this still the release we accepted?" is a question
with an answer rather than an assumption. Selecting a release is a single
atomic rename, so a machine that lost power halfway through is either on the
old one or the new one and never on half of each.

Rolling back means going to a release somebody accepted. A release that was
merely installed is not a rollback target: it may be exactly the one that broke.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_VERSION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024
CURRENT_LINK = "current"
ACCEPTED_MARKER = "accepted"


class ReleaseError(RuntimeError):
    """A release is not usable, so nothing is selected, installed, or removed."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def publish_release(source: Path, releases: Path, name: str) -> dict[str, object]:
    """Copy one build into its own directory and hash-bind every file.

    A release is published once. Republishing over an existing name is refused
    rather than merged, because a release whose bytes can change is not a thing
    anyone can roll back to with confidence.
    """

    if not name or "/" in name or name in {".", ".."}:
        raise ReleaseError("that is not a usable release name")
    if not source.is_dir():
        raise ReleaseError("the release source is not there")
    target = releases / name
    if target.exists():
        raise ReleaseError(f"release {name} already exists and is immutable")
    staged = releases / f".{name}.{uuid.uuid4().hex}.staging"
    files_dir = staged / "files"
    files_dir.mkdir(mode=0o755, parents=True)
    entries: list[dict[str, object]] = []
    try:
        for path in _files(source):
            relative = path.relative_to(source)
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ReleaseError(f"{relative} is larger than a release carries")
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            recorded = path.stat()
            entries.append(
                {
                    "name": str(relative),
                    "sha256": _digest(path),
                    "bytes": recorded.st_size,
                    "mode": oct(recorded.st_mode & 0o777),
                    # The container account owns the plugin tree it reads. A
                    # rollback that wrote every file back as root would leave a
                    # deployment the runtime cannot open, so who owns a file is
                    # part of what the release records about it.
                    "uid": recorded.st_uid,
                    "gid": recorded.st_gid,
                }
            )
        manifest: dict[str, object] = {
            "version": MANIFEST_VERSION,
            "release": name,
            "published_at": datetime.now(UTC).isoformat(),
            "files": entries,
        }
        (staged / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
        )
        os.replace(staged, target)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return manifest


def _manifest(release: Path) -> Mapping[str, object]:
    path = release / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("that release has no manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("that release's manifest is unreadable") from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise ReleaseError("that release's manifest is malformed")
    return manifest


def _entries(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReleaseError("that release's manifest is malformed")
    entries: list[Mapping[str, object]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise ReleaseError("that release's manifest is malformed")
        entries.append(entry)
    return entries


def verify_release(release: Path) -> tuple[str, ...]:
    """Names whose bytes no longer match the manifest. Empty means intact."""

    mismatched: list[str] = []
    for entry in _entries(_manifest(release)):
        name = str(entry.get("name", ""))
        stored = release / "files" / name
        if not stored.is_file() or _digest(stored) != entry.get("sha256"):
            mismatched.append(name)
    return tuple(mismatched)


def select_release(releases: Path, name: str, *, accepted: bool = False) -> None:
    """Make one release current, atomically, after proving it is intact."""

    release = releases / name
    if not release.is_dir():
        raise ReleaseError(f"release {name} is not installed")
    mismatched = verify_release(release)
    if mismatched:
        raise ReleaseError("that release does not match its manifest: " + ", ".join(mismatched))
    if accepted:
        (release / ACCEPTED_MARKER).write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    link = releases / CURRENT_LINK
    temporary = releases / f".{CURRENT_LINK}.{uuid.uuid4().hex}.tmp"
    temporary.symlink_to(release.resolve(), target_is_directory=True)
    try:
        os.replace(temporary, link)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink()
        raise ReleaseError("the current release could not be selected") from exc


def current_release(releases: Path) -> str:
    link = releases / CURRENT_LINK
    if not link.is_symlink():
        return ""
    return Path(os.readlink(link)).name


def accepted_releases(releases: Path) -> tuple[str, ...]:
    """Every release someone accepted, oldest first."""

    if not releases.is_dir():
        return ()
    return tuple(
        entry.name
        for entry in sorted(releases.iterdir())
        if entry.is_dir() and not entry.is_symlink() and (entry / ACCEPTED_MARKER).is_file()
    )


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """Where to go back to, and in what order. Never executed by this code."""

    current: str
    target: str = ""
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.target)

    def steps(self) -> tuple[str, ...]:
        if not self.available:
            return ()
        return (
            f"stop the container running {self.current}",
            "verify no container is consuming Discord",
            f"select the accepted release {self.target}",
            "install its recorded bytes and start it",
            "verify exactly one consumer, then confirm health",
        )

    def as_json(self) -> dict[str, object]:
        return {
            "current": self.current,
            "target": self.target,
            "available": self.available,
            "reason": self.reason,
            "steps": list(self.steps()),
        }


def rollback(releases: Path) -> RollbackPlan:
    """The newest accepted release that is not the one running now."""

    current = current_release(releases)
    candidates = [name for name in accepted_releases(releases) if name != current]
    if not candidates:
        return RollbackPlan(current=current, reason="there is no accepted release to roll back to")
    return RollbackPlan(current=current, target=candidates[-1])


def _restore_attributes(target: Path, entry: Mapping[str, object]) -> None:
    """Put back the mode, and the ownership when we are root and can."""

    mode = entry.get("mode")
    if isinstance(mode, str):
        with suppress(ValueError, OSError):
            target.chmod(int(mode, 8))
    uid, gid = entry.get("uid"), entry.get("gid")
    if os.geteuid() != 0 or not isinstance(uid, int) or not isinstance(gid, int):
        # Not root, or a release published before ownership was recorded: the
        # bytes are still exact, and nothing pretends otherwise.
        return
    with suppress(OSError):
        os.chown(target, uid, gid)


def install_release(releases: Path, name: str, destination: Path) -> tuple[str, ...]:
    """Put a release's exact recorded bytes in place, or write nothing at all."""

    release = releases / name
    mismatched = verify_release(release)
    if mismatched:
        raise ReleaseError("that release does not match its manifest: " + ", ".join(mismatched))
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    written: list[str] = []
    for entry in _entries(_manifest(release)):
        relative = str(entry.get("name", ""))
        target = (destination / relative).resolve()
        if not str(target).startswith(str(destination.resolve())):
            raise ReleaseError("a release may only write inside its destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release / "files" / relative, target)
        _restore_attributes(target, entry)
        written.append(relative)
    return tuple(written)


__all__ = [
    "ACCEPTED_MARKER",
    "CURRENT_LINK",
    "ReleaseError",
    "RollbackPlan",
    "accepted_releases",
    "current_release",
    "install_release",
    "publish_release",
    "rollback",
    "select_release",
    "verify_release",
]
