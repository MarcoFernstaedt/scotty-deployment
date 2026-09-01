"""Backing up the work, and rolling back to a release that was accepted.

What must survive a bad release is the work: workflows, personas, reminders,
approvals, effect records, and the provenance behind every property card. What
must never end up in a backup is a credential. A token copied into a backup
directory is a token in one more place, and a restore that could put one back
would be a way to resurrect access someone had revoked.

So the file list is an explicit allowlist, the secrets are named separately and
excluded whatever the allowlist says, and every copied file is hash-bound in a
manifest that a restore checks before it writes anything. Rollback is a plan,
not an action: it names the last accepted release and the order to move in, and
a person runs it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

    A restore writes files and does nothing else. It starts no consumer, claims
    no lease, and replays no schedule: bringing state back is not the same as
    bringing a second deployment to life.
    """

    mismatched = verify_backup(destination)
    if mismatched:
        raise BackupError("this backup does not match its manifest: " + ", ".join(mismatched))
    restored: list[str] = []
    state_root = state_dir.resolve()
    for entry in _entries(_manifest(destination)):
        name = str(entry.get("name", ""))
        if name not in BACKUP_INCLUDES or _is_secret(name):
            raise BackupError(f"{name} is not something a restore may write")
        target = (state_dir / name).resolve()
        if target.parent != state_root:
            # A manifest is data, not a path to trust.
            raise BackupError("a restore may only write inside the state directory")
        shutil.copy2(destination / "files" / name, target)
        target.chmod(0o600)
        restored.append(name)
    return tuple(restored)


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """The release to go back to, and the order to do it in. Never executed."""

    current: str
    target: str = ""
    image_digest: str = ""
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.target)

    def steps(self) -> tuple[str, ...]:
        """The exact order. The current container stops before another starts.

        Two Discord consumers on one bot token is the failure this ordering
        exists to prevent, so the stop is never optional and never last.
        """

        if not self.available:
            return ()
        return (
            f"stop the running container for {self.current}",
            "verify no container is consuming Discord",
            f"start the accepted release {self.target} at {self.image_digest}",
            "verify exactly one consumer, then confirm health",
        )

    def as_json(self) -> dict[str, object]:
        return {
            "current": self.current,
            "target": self.target,
            "image_digest": self.image_digest,
            "available": self.available,
            "reason": self.reason,
            "steps": list(self.steps()),
        }


def rollback_plan(releases_dir: Path, *, current: str) -> RollbackPlan:
    """Name the newest independently accepted release that is not the current one."""

    if not releases_dir.is_dir():
        return RollbackPlan(current=current, reason="there is no release directory")
    accepted: list[tuple[str, str]] = []
    for entry in sorted(releases_dir.iterdir()):
        if not entry.is_dir() or entry.name == current or entry.is_symlink():
            continue
        marker = entry / "release.json"
        if not marker.is_file():
            continue
        try:
            body = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(body, Mapping) or body.get("accepted") is not True:
            # Rolling back to something nobody accepted is not a recovery.
            continue
        digest = body.get("image_digest")
        if type(digest) is not str or not digest.startswith("sha256:"):
            continue
        accepted.append((entry.name, digest))
    if not accepted:
        return RollbackPlan(current=current, reason="there is no accepted release to roll back to")
    name, digest = accepted[-1]
    return RollbackPlan(current=current, target=name, image_digest=digest)


def restorable(destination: Path) -> Sequence[str]:
    """What this backup would put back, without putting anything back."""

    return [str(entry.get("name", "")) for entry in _entries(_manifest(destination))]


__all__ = [
    "BACKUP_INCLUDES",
    "MANIFEST_VERSION",
    "SECRET_NAMES",
    "BackupError",
    "RollbackPlan",
    "backup_state",
    "restorable",
    "restore_state",
    "rollback_plan",
    "verify_backup",
]
