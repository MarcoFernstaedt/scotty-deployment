"""Bounded health inspection and narrow repair of Scotty-owned state only.

Everything in this module is deliberately non-generic. It can inspect and
reconcile the small set of files and rows Scotty itself owns inside its own
profile-local state directory, and nothing else. It never reads credentials,
never executes a shell, never touches Docker, systemd, the firewall, the host,
maintainer-private systems, or another client's state, and never synthesizes a
privileged command. When a genuine repair needs privilege, it stops at a
redacted diagnosis and names the one fixed root-owned recovery command.

Receipts are fixed redacted vocabulary. No filesystem path, identifier, or
provider value ever leaves this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from .approvals import ApprovalStore
from .config import ConfigError, RuntimeConfig
from .policy import Principal, Role
from .reminders import ReminderStore

#: The one root-owned recovery path a privileged diagnosis may name.
OPERATOR_RECOVERY_COMMAND = "sudo /usr/local/sbin/scotty-start"

#: Exactly the repairs Scotty may perform on its own state.
ALLOWED_REPAIRS: tuple[str, ...] = (
    "recover_workflows",
    "rebuild_cache",
    "repair_state_permissions",
)

#: Repairs that are recognisable but permanently outside Scotty's authority.
#: They exist so the refusal is a specific redacted diagnosis instead of a
#: generic unknown-operation error.
_PRIVILEGED_REPAIRS: frozenset[str] = frozenset(
    {
        "run_shell",
        "run_command",
        "restart_service",
        "restart_container",
        "install_package",
        "read_secrets",
        "rotate_secrets",
        "repair_docker",
        "repair_systemd",
        "repair_firewall",
        "repair_imperator",
        "repair_vaultwarden",
        "repair_host",
    }
)

_REPAIR_ROLES: frozenset[Role] = frozenset({Role.MAINTAINER, Role.MAIN_OPERATOR})

_STATE_FILE_MODE = 0o600
_STATE_DIR_MODE = 0o700


class SelfRepairError(RuntimeError):
    """A repair is unauthorized, unknown, privileged, or unsafe to perform."""


def _sqlite_state(path: Path) -> str:
    """Report a redacted integrity verdict for one Scotty-owned database."""

    if not path.is_file() or path.is_symlink():
        return "missing"
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return "unreadable"
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error:
        return "failed"
    finally:
        connection.close()
    if row is None or row[0] != "ok":
        return "failed"
    return "ok"


def _sqlite_count(path: Path, statement: str) -> int:
    if not path.is_file() or path.is_symlink():
        return 0
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return 0
    try:
        row = connection.execute(statement).fetchone()
    except sqlite3.Error:
        return 0
    finally:
        connection.close()
    return int(row[0]) if row is not None else 0


def _remove_owned_entry(entry: Path) -> None:
    """Delete one cache entry without ever following a symlink out of the tree."""

    if entry.is_symlink():
        entry.unlink()
        return
    if entry.is_dir():
        for child in sorted(entry.iterdir()):
            _remove_owned_entry(child)
        entry.rmdir()
        return
    entry.unlink()


class SelfRepairManager:
    """Inspect and repair only Scotty's own client-owned state."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        private_path: str | os.PathLike[str],
        approvals: ApprovalStore,
        reminders: ReminderStore,
        *,
        provider_status: Callable[[], Mapping[str, bool]],
    ) -> None:
        self.state_dir = Path(state_dir)
        self.private_path = Path(private_path)
        self.approvals = approvals
        self.reminders = reminders
        self.provider_status = provider_status

    # -- inspection -----------------------------------------------------

    def _configuration(self) -> tuple[bool, str]:
        """Re-validate the private configuration without exposing its values."""

        try:
            raw = json.loads(self.private_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "configuration is unreadable or malformed"
        if not isinstance(raw, Mapping):
            return False, "configuration is not an object"
        try:
            RuntimeConfig.from_mapping(raw)
        except ConfigError as exc:
            # ConfigError names a field, never a configured value.
            return False, str(exc)[:200]
        return True, ""

    def _providers(self) -> dict[str, str]:
        status = self.provider_status()
        if not isinstance(status, Mapping):
            raise SelfRepairError("provider status is unavailable")
        return {
            str(name): ("connected" if bool(connected) else "not connected")
            for name, connected in sorted(status.items())
        }

    def _cache_dir(self) -> Path:
        return self.state_dir / "cache"

    def health(self) -> dict[str, object]:
        """Return a fixed redacted view of Scotty-owned state."""

        valid, error = self._configuration()
        cache = self._cache_dir()
        interrupted_approvals = _sqlite_count(
            self.approvals.path, "SELECT COUNT(*) FROM proposals WHERE status='executing'"
        )
        interrupted_reminders = _sqlite_count(
            self.reminders.path, "SELECT COUNT(*) FROM reminders WHERE status='dispatching'"
        )
        report: dict[str, object] = {
            "configuration_valid": valid,
            "approvals_integrity": _sqlite_state(self.approvals.path),
            "reminders_integrity": _sqlite_state(self.reminders.path),
            "cache_present": cache.is_dir() and not cache.is_symlink(),
            "interrupted_workflows": interrupted_approvals + interrupted_reminders,
            "providers": self._providers(),
            "available_repairs": list(ALLOWED_REPAIRS),
        }
        if not valid:
            report["configuration_error"] = error
        return report

    # -- repair ---------------------------------------------------------

    def repair(self, principal: Principal, action: object) -> dict[str, object]:
        """Perform exactly one bounded repair as an authorized principal."""

        if not isinstance(principal, Principal) or principal.role not in _REPAIR_ROLES:
            raise SelfRepairError("only the main operator or maintainer may repair Scotty state")
        if type(action) is not str or not action:
            raise SelfRepairError("repair action is malformed")
        if action in _PRIVILEGED_REPAIRS:
            raise SelfRepairError(
                "that repair needs privilege Scotty does not hold; "
                f"an operator must run {OPERATOR_RECOVERY_COMMAND}"
            )
        if action not in ALLOWED_REPAIRS:
            raise SelfRepairError("that repair is not a Scotty-owned repair")
        if action == "recover_workflows":
            return self._recover_workflows()
        if action == "rebuild_cache":
            return self._rebuild_cache()
        return self._repair_state_permissions()

    def _recover_workflows(self) -> dict[str, object]:
        """Move interrupted work to `unknown`; never blindly retry an effect."""

        try:
            approvals = self.approvals.recover_interrupted()
            reminders = self.reminders.recover_interrupted()
        except sqlite3.Error as exc:
            raise SelfRepairError("Scotty-owned workflow state could not be recovered") from exc
        return {
            "status": "repaired",
            "component": "workflows",
            "recovered_approvals": approvals,
            "recovered_reminders": reminders,
        }

    def _rebuild_cache(self) -> dict[str, object]:
        """Empty and recreate the Scotty-owned cache directory in place."""

        cache = self._cache_dir()
        if cache.is_symlink():
            raise SelfRepairError("the Scotty cache path is not a Scotty-owned directory")
        if cache.exists() and not cache.is_dir():
            raise SelfRepairError("the Scotty cache path is not a Scotty-owned directory")
        try:
            if cache.is_dir():
                for entry in sorted(cache.iterdir()):
                    _remove_owned_entry(entry)
            else:
                cache.mkdir(mode=_STATE_DIR_MODE, parents=True)
            os.chmod(cache, _STATE_DIR_MODE)
        except OSError as exc:
            raise SelfRepairError("the Scotty cache could not be rebuilt") from exc
        return {"status": "repaired", "component": "cache"}

    def _repair_state_permissions(self) -> dict[str, object]:
        """Restore owner-only modes on Scotty-owned state, nothing else."""

        targets: list[tuple[Path, int]] = [
            (self.private_path, _STATE_FILE_MODE),
            (self.approvals.path, _STATE_FILE_MODE),
            (self.reminders.path, _STATE_FILE_MODE),
        ]
        cache = self._cache_dir()
        if cache.is_dir() and not cache.is_symlink():
            targets.append((cache, _STATE_DIR_MODE))
        corrected = 0
        for path, mode in targets:
            if path.is_symlink() or not path.exists():
                continue
            try:
                if path.stat().st_mode & 0o777 != mode:
                    os.chmod(path, mode)
                    corrected += 1
            except OSError as exc:
                raise SelfRepairError(
                    "Scotty-owned state permissions could not be restored"
                ) from exc
        return {"status": "repaired", "component": "permissions", "corrected": corrected}
