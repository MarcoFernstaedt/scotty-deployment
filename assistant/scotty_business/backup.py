"""Backing up the work, and putting it back.

What must survive a bad release is the work: workflows, personas, reminders,
approvals, effect records, and the provenance behind every property card. What
must never end up in a backup is a credential. A token copied into a backup
directory is a token in one more place, and a restore that could put one back
would be a way to resurrect access someone had revoked.

So the file list is an explicit allowlist, the secrets are named separately and
excluded whatever the allowlist says, and every copied file is hash-bound in a
manifest that a restore checks before it writes anything, and the whole set is
staged and validated before a single file is moved into place.

Rollback is not here at all. Releases live on the host, root-owned and outside
every mount this process can reach, and the host supervisor selects them. This
module can say what an operator would run; it cannot see a release, and code
that pretended to read one would only ever report that there were none.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path


class BackupError(RuntimeError):
    """A backup or restore cannot be trusted, so nothing is written."""


#: Exactly what a backup carries. Everything here is the deployment's own
#: non-secret work; nothing here is a credential, a token, or a session.
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

#: Never copied, whatever else changes. Named separately so that adding a file
#: to the include list can never quietly add a secret to a backup.
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

#: Any file whose name matches one of these is treated as secret even if it is
#: not in the list above, so a new per-user token file is excluded on sight.
_SECRET_FRAGMENTS = ("oauth", "token", "secret", "credential", "consent", "session")

MANIFEST_VERSION = 1
MAX_FILE_BYTES = 64 * 1024 * 1024


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
        raise BackupError("the state directory is not there to back up")
    files_dir = destination / "files"
    files_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for name in BACKUP_INCLUDES:
        if _is_secret(name):  # pragma: no cover - the lists are held disjoint
            raise BackupError(f"{name} is credential state and is never backed up")
        source = state_dir / name
        if source.is_symlink() or not source.is_file():
            # An absent file is simply absent: a deployment that has never had
            # a workflow has nothing to restore, which is not a failure.
            continue
        if source.stat().st_size > MAX_FILE_BYTES:
            raise BackupError(f"{name} is larger than a backup carries")
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


def _entries(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("this backup's manifest is malformed")
    entries: list[Mapping[str, object]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise BackupError("this backup's manifest is malformed")
        entries.append(entry)
    return entries


def _manifest(destination: Path) -> Mapping[str, object]:
    path = destination / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise BackupError("this backup has no manifest")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("this backup's manifest is unreadable") from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise BackupError("this backup's manifest is malformed")
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
    """Put the backed-up work back, after proving the backup is intact.

    Every file is staged and checked first, and only then moved into place, so
    a restore that fails halfway leaves the state directory as it was rather
    than half of one backup and half of another.

    A restore writes files and does nothing else. It starts no consumer, claims
    no lease, and replays no schedule: bringing state back is not the same as
    bringing a second deployment to life.
    """

    mismatched = verify_backup(destination)
    if mismatched:
        raise BackupError("this backup does not match its manifest: " + ", ".join(mismatched))
    state_root = state_dir.resolve()
    staging = state_dir / f".restore.{uuid.uuid4().hex}"
    staged: list[tuple[Path, Path]] = []
    try:
        staging.mkdir(mode=0o700, parents=True)
        for entry in _entries(_manifest(destination)):
            name = str(entry.get("name", ""))
            if name not in BACKUP_INCLUDES or _is_secret(name):
                raise BackupError(f"{name} is not something a restore may write")
            target = (state_dir / name).resolve()
            if target.parent != state_root:
                # A manifest is data, not a path to trust.
                raise BackupError("a restore may only write inside the state directory")
            source = destination / "files" / name
            held = staging / name
            shutil.copy2(source, held)
            held.chmod(0o600)
            if _digest(held) != entry.get("sha256"):
                raise BackupError(f"{name} did not survive being staged")
            staged.append((held, target))
        for held, target in staged:
            os.replace(held, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tuple(target.name for _, target in staged)


#: What an operator runs to roll back. Releases are root-owned and live outside
#: every mount this process has, which is the point: a runtime that could select
#: its own release could roll forward onto one nobody accepted.
ROLLBACK_COMMAND = "sudo /usr/local/sbin/scotty-supervisor rollback"


def rollback_guidance() -> dict[str, object]:
    """The fixed management step for a rollback, which this process never runs."""

    return {
        "available": False,
        "reason": "releases are held on the host and are not visible from the runtime",
        "operator_command": ROLLBACK_COMMAND,
        "steps": [
            f"run {ROLLBACK_COMMAND} to see the accepted release it would return to",
            f"run {ROLLBACK_COMMAND} --execute to carry it out",
            "read the reported state: verified, failed, or unknown",
        ],
    }


def restorable(destination: Path) -> Sequence[str]:
    """What this backup would put back, without putting anything back."""

    return [str(entry.get("name", "")) for entry in _entries(_manifest(destination))]


__all__ = [
    "BACKUP_INCLUDES",
    "MANIFEST_VERSION",
    "ROLLBACK_COMMAND",
    "SECRET_NAMES",
    "BackupError",
    "backup_state",
    "restorable",
    "restore_state",
    "rollback_guidance",
    "verify_backup",
]
